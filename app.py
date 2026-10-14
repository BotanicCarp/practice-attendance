import os
import secrets
from datetime import datetime, date, timedelta
from functools import wraps
from math import isfinite
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, abort, jsonify
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from dotenv import load_dotenv

load_dotenv()

ROOT = Path(__file__).resolve().parent
# Vercel's deployment directory is read-only. /tmp is writable but ephemeral;
# production photos must eventually move to private object storage.
IS_VERCEL = bool(os.getenv("VERCEL"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/practice-attendance-uploads" if IS_VERCEL else str(ROOT / "private_uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

app = Flask(__name__)
# Never use a predictable fallback.  A missing key is acceptable only for a
# local, ephemeral session; production deployments must provide SECRET_KEY.
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
default_db_path = Path("/tmp/practice-attendance.db") if IS_VERCEL else (ROOT / "instance" / "attendance.db")
db_url = os.getenv("DATABASE_URL", f"sqlite:///{default_db_path.as_posix()}")
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

(ROOT / "instance").mkdir(exist_ok=True)
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Сначала войдите в аккаунт."
login_manager.login_message_category = "warning"
LOGIN_FAILURES = {}
LOGIN_WINDOW = timedelta(minutes=15)
MAX_LOGIN_FAILURES = 5


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False)
    group_name = db.Column(db.String(100), nullable=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student")
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def is_active(self):
        """Flask-Login must honour an administrator disabling an account."""
        return self.active

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)


class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    attendance_date = db.Column(db.Date, nullable=False, default=date.today, index=True)
    checked_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    latitude = db.Column(db.Float, nullable=False)
    longitude = db.Column(db.Float, nullable=False)
    accuracy_m = db.Column(db.Float, nullable=False)
    photo_name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="Присутствует")
    user = db.relationship("User", backref=db.backref("attendance_records", lazy=True))
    __table_args__ = (db.UniqueConstraint("user_id", "attendance_date", name="unique_daily_attendance"),)


class AuditLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    admin_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    attendance_id = db.Column(db.Integer, db.ForeignKey("attendance.id"), nullable=False)
    action = db.Column(db.String(40), nullable=False)
    details = db.Column(db.String(500), nullable=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def admin_required(fn):
    @wraps(fn)
    @login_required
    def wrapped(*args, **kwargs):
        if current_user.role != "admin":
            abort(403)
        return fn(*args, **kwargs)
    return wrapped


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def image_extension(stream):
    """Return the verified extension for a small set of supported image types."""
    header = stream.read(16)
    stream.seek(0)
    if header.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if header.startswith(b"RIFF") and header[8:12] == b"WEBP":
        return "webp"
    return None


def parse_location(data):
    """Validate untrusted browser coordinates before calculating distance."""
    try:
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        accuracy = float(data["accuracy"])
        if not parse_location(data):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in (lat, lon, accuracy)):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or accuracy <= 0:
        return None
    return lat, lon, accuracy


def practice_settings():
    """Read and validate the location settings used by both check-in endpoints."""
    try:
        lat = float(os.environ["PRACTICE_LAT"])
        lon = float(os.environ["PRACTICE_LON"])
        radius = float(os.getenv("PRACTICE_RADIUS_M", "100"))
        max_accuracy = float(os.getenv("MAX_GPS_ACCURACY_M", "100"))
    except (KeyError, ValueError):
        return None
    if not all(isfinite(value) for value in (lat, lon, radius, max_accuracy)):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or radius <= 0 or max_accuracy <= 0:
        return None
    return lat, lon, radius, max_accuracy


def haversine_m(lat1, lon1, lat2, lon2):
    from math import radians, sin, cos, asin, sqrt
    radius = 6371000
    dlat = radians(lat2 - lat1)
    dlon = radians(lon2 - lon1)
    a = sin(dlat / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(dlon / 2) ** 2
    return radius * 2 * asin(sqrt(a))


@app.context_processor
def inject_now():
    return {"now": datetime.utcnow()}


@app.before_request
def reject_disabled_sessions():
    """End an already-issued session as soon as its account is disabled."""
    if current_user.is_authenticated and not current_user.active:
        logout_user()
        flash("Ваш аккаунт отключён. Обратитесь к администратору.", "warning")
        return redirect(url_for("login"))


def login_is_limited(remote_addr):
    now = datetime.utcnow()
    attempts = [stamp for stamp in LOGIN_FAILURES.get(remote_addr, []) if now - stamp < LOGIN_WINDOW]
    LOGIN_FAILURES[remote_addr] = attempts
    return len(attempts) >= MAX_LOGIN_FAILURES


def record_login_failure(remote_addr):
    LOGIN_FAILURES.setdefault(remote_addr, []).append(datetime.utcnow())


@app.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard" if current_user.role == "admin" else "student_dashboard"))
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("admin_dashboard" if current_user.role == "admin" else "student_dashboard"))
    if request.method == "POST":
        remote_addr = request.remote_addr or "unknown"
        if login_is_limited(remote_addr):
            flash("Слишком много попыток входа. Повторите попытку через 15 минут.", "danger")
            return render_template("login.html"), 429
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            LOGIN_FAILURES.pop(remote_addr, None)
            login_user(user)
            return redirect(url_for("admin_dashboard" if user.role == "admin" else "student_dashboard"))
        record_login_failure(remote_addr)
        flash("Неверный логин или пароль.", "danger")
    return render_template("login.html")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Вы вышли из аккаунта.", "success")
    return redirect(url_for("login"))


@app.route("/student")
@login_required
def student_dashboard():
    if current_user.role != "student":
        return redirect(url_for("admin_dashboard"))
    records = Attendance.query.filter_by(user_id=current_user.id).order_by(Attendance.checked_at.desc()).limit(30).all()
    today_record = Attendance.query.filter_by(user_id=current_user.id, attendance_date=date.today()).first()
    return render_template("student_dashboard.html", records=records, today_record=today_record)


@app.route("/api/location-check", methods=["POST"])
@login_required
def location_check():
    if current_user.role != "student":
        return jsonify(ok=False, message="Эта функция доступна только практикантам."), 403
    data = request.get_json(silent=True) or {}
    try:
        lat = float(data["latitude"])
        lon = float(data["longitude"])
        accuracy = float(data["accuracy"])
        if not parse_location(data):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return jsonify(ok=False, message="Не удалось получить координаты. Разрешите доступ к геолокации."), 400

    # Location must be configured by the administrator before check-in is enabled.
    if os.getenv("PRACTICE_LAT") is None or os.getenv("PRACTICE_LON") is None:
        return jsonify(ok=False, message="Администратор ещё не настроил координаты места практики."), 503

    settings = practice_settings()
    if not settings:
        abort(503)
    practice_lat, practice_lon, radius, max_accuracy = settings
    distance = haversine_m(lat, lon, practice_lat, practice_lon)

    if accuracy <= 0 or accuracy > max_accuracy:
        return jsonify(ok=False, message=f"Точность геолокации недостаточная ({accuracy:.0f} м). Попробуйте ещё раз на открытом месте."), 400
    if distance > radius:
        return jsonify(ok=False, message=f"Вы вне разрешённой зоны. Расстояние до места практики: примерно {distance:.0f} м."), 403
    return jsonify(ok=True, message="Местоположение подтверждено.", distance_m=round(distance), accuracy_m=round(accuracy))


@app.route("/attendance", methods=["POST"])
@login_required
def check_in():
    if current_user.role != "student":
        abort(403)
    if Attendance.query.filter_by(user_id=current_user.id, attendance_date=date.today()).first():
        flash("Вы уже отметились сегодня.", "warning")
        return redirect(url_for("student_dashboard"))

    try:
        lat = float(request.form["latitude"])
        lon = float(request.form["longitude"])
        accuracy = float(request.form["accuracy"])
        if not parse_location(request.form):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        flash("Не удалось получить геолокацию. Разрешите доступ к местоположению и повторите попытку.", "danger")
        return redirect(url_for("student_dashboard"))

    if os.getenv("PRACTICE_LAT") is None or os.getenv("PRACTICE_LON") is None:
        flash("Администратор ещё не настроил координаты места практики.", "danger")
        return redirect(url_for("student_dashboard"))

    settings = practice_settings()
    if not settings:
        abort(503)
    practice_lat, practice_lon, radius, max_accuracy = settings
    distance = haversine_m(lat, lon, practice_lat, practice_lon)

    if accuracy <= 0 or accuracy > max_accuracy:
        flash(f"Точность геолокации недостаточная ({accuracy:.0f} м). Попробуйте ещё раз.", "danger")
        return redirect(url_for("student_dashboard"))
    if distance > radius:
        flash(f"Отметка отклонена: вы за пределами зоны практики (примерно {distance:.0f} м).", "danger")
        return redirect(url_for("student_dashboard"))

    photo = request.files.get("pass_photo")
    if not photo or not photo.filename or not allowed_file(photo.filename):
        flash("Загрузите фотографию пропуска в формате JPG, PNG или WEBP.", "danger")
        return redirect(url_for("student_dashboard"))

    # Re-check daily duplicate immediately before insert. DB unique constraint is the final guard.
    filename = secure_filename(photo.filename)
    suffix = filename.rsplit(".", 1)[1].lower()
    detected_suffix = image_extension(photo.stream)
    if not detected_suffix or (suffix == "jpeg" and detected_suffix != "jpg") or (suffix != "jpeg" and suffix != detected_suffix):
        abort(400)
    stored_name = f"user{current_user.id}_{date.today().isoformat()}_{os.urandom(8).hex()}.{suffix}"
    photo_path = UPLOAD_DIR / stored_name
    photo.save(photo_path)

    try:
        record = Attendance(
            user_id=current_user.id,
            attendance_date=date.today(),
            checked_at=datetime.utcnow(),
            latitude=lat,
            longitude=lon,
            accuracy_m=accuracy,
            photo_name=stored_name,
            status="Присутствует",
        )
        db.session.add(record)
        db.session.commit()
    except Exception:
        db.session.rollback()
        if photo_path.exists():
            photo_path.unlink()
        flash("Не удалось сохранить отметку. Возможно, вы уже отметились сегодня.", "danger")
        return redirect(url_for("student_dashboard"))

    flash("Отметка успешно сохранена.", "success")
    return redirect(url_for("student_dashboard"))


@app.route("/admin")
@admin_required
def admin_dashboard():
    selected_date = request.args.get("date", date.today().isoformat())
    try:
        chosen_date = date.fromisoformat(selected_date)
    except ValueError:
        chosen_date = date.today()
    records = Attendance.query.filter_by(attendance_date=chosen_date).order_by(Attendance.checked_at.desc()).all()
    students = User.query.filter_by(role="student").order_by(User.full_name.asc()).all()
    marked_ids = {r.user_id for r in records}
    missing = [s for s in students if s.active and s.id not in marked_ids]
    return render_template("admin_dashboard.html", records=records, students=students, missing=missing, chosen_date=chosen_date)


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        group_name = request.form.get("group_name", "").strip()
        password = request.form.get("password", "")
        if not username or not full_name or len(password) < 10:
            flash("Заполните логин и ФИО; пароль должен содержать минимум 10 символов.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Такой логин уже существует.", "danger")
        else:
            user = User(username=username, full_name=full_name, group_name=group_name, role="student")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Аккаунт практиканта создан.", "success")
            return redirect(url_for("admin_users"))
    users = User.query.filter_by(role="student").order_by(User.full_name.asc()).all()
    return render_template("admin_users.html", users=users)


@app.route("/admin/users/<int:user_id>/toggle", methods=["POST"])
@admin_required
def toggle_user(user_id):
    user = db.session.get(User, user_id)
    if not user or user.role != "student":
        abort(404)
    user.active = not user.active
    db.session.commit()
    flash("Статус аккаунта изменён.", "success")
    return redirect(url_for("admin_users"))


@app.route("/admin/attendance/<int:attendance_id>/photo")
@admin_required
def attendance_photo(attendance_id):
    from flask import send_file
    record = db.session.get(Attendance, attendance_id)
    if not record:
        abort(404)
    photo_path = UPLOAD_DIR / record.photo_name
    if not photo_path.exists():
        abort(404)
    return send_file(photo_path, mimetype="application/octet-stream", as_attachment=True, download_name=record.photo_name)


@app.route("/admin/export.csv")
@admin_required
def export_csv():
    import csv
    from io import StringIO
    from flask import Response
    selected_date = request.args.get("date", date.today().isoformat())
    try:
        chosen_date = date.fromisoformat(selected_date)
    except ValueError:
        chosen_date = date.today()
    output = StringIO()
    writer = csv.writer(output, delimiter=";")
    writer.writerow(["ФИО", "Группа", "Дата", "Время UTC", "Широта", "Долгота", "Точность GPS, м", "Статус"])
    records = Attendance.query.filter_by(attendance_date=chosen_date).order_by(Attendance.checked_at.asc()).all()
    for r in records:
        writer.writerow([r.user.full_name, r.user.group_name or "", r.attendance_date.isoformat(), r.checked_at.strftime("%H:%M:%S"), r.latitude, r.longitude, r.accuracy_m, r.status])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=attendance_{chosen_date.isoformat()}.csv"})


def create_initial_admin():
    username = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
    password = os.getenv("INITIAL_ADMIN_PASSWORD", "")
    if not username or not password:
        return
    existing = User.query.filter_by(username=username).first()
    if not existing:
        admin = User(username=username, full_name=os.getenv("INITIAL_ADMIN_NAME", "Администратор"), role="admin")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()


with app.app_context():
    db.create_all()
    create_initial_admin()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
