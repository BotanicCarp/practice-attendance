import os
import secrets
from datetime import datetime, date, timedelta
from functools import wraps
from math import isfinite
from pathlib import Path

from flask import Flask, render_template, request, redirect, url_for, flash, abort, jsonify
from sqlalchemy import inspect, text
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
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "/tmp/practice-attendance-uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png", "webp"}

app = Flask(__name__, template_folder=str(ROOT), static_folder=None)
# Never use a predictable fallback.  A missing key is acceptable only for a
# local, ephemeral session; production deployments must provide SECRET_KEY.
app.config["SECRET_KEY"] = os.getenv("SECRET_KEY") or secrets.token_urlsafe(48)
default_db_path = Path("/tmp/practice-attendance.db") if IS_VERCEL else (ROOT / "attendance.db")
db_url = os.getenv("DATABASE_URL", f"sqlite:///{default_db_path.as_posix()}").strip()
# Vercel/Supabase connection variables often use postgres:// or postgresql://.
# This project installs psycopg v3 (psycopg[binary]), so explicitly select its
# SQLAlchemy dialect instead of relying on the uninstalled psycopg2 driver.
if db_url.startswith("postgres://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgres://"):]
elif db_url.startswith("postgresql://"):
    db_url = "postgresql+psycopg://" + db_url[len("postgresql://"):]
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True}
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024
app.config["UPLOAD_FOLDER"] = str(UPLOAD_DIR)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "0") == "1"

# Flat project layout: no instance directory is required.
db = SQLAlchemy(app)
csrf = CSRFProtect(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Сначала войдите в аккаунт."
login_manager.login_message_category = "warning"
LOGIN_FAILURES = {}
LOGIN_WINDOW = timedelta(minutes=15)
MAX_LOGIN_FAILURES = 5

# The registration form uses a controlled list instead of free-text entry.
DEFAULT_INSTITUTIONS = [
    "Карагандинский высший политехнический колледж",
    "Темиртауский индустриальный колледж",
    "Карагандинский технический университет",
    "Карагандинский университет Казпотребсоюза",
    "Колледж экономики и статистики",
]


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    full_name = db.Column(db.String(160), nullable=False)
    group_name = db.Column(db.String(100), nullable=True)
    educational_institution = db.Column(db.String(200), nullable=True)
    pass_number = db.Column(db.String(20), nullable=True, index=True)
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
    pass_number = db.Column(db.String(20), nullable=False, default="")
    photo_name = db.Column(db.String(255), nullable=False)
    status = db.Column(db.String(30), nullable=False, default="Присутствует")
    user = db.relationship("User", backref=db.backref("attendance_records", lazy=True))
    __table_args__ = (db.UniqueConstraint("user_id", "attendance_date", name="unique_daily_attendance"),)


class EducationalInstitution(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(200), unique=True, nullable=False)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)


class AppSetting(db.Model):
    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(db.String(500), nullable=False)


class Poll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(1000), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_by = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    creator = db.relationship("User")


class PollOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("poll.id", ondelete="CASCADE"), nullable=False, index=True)
    label = db.Column(db.String(200), nullable=False)
    poll = db.relationship("Poll", backref=db.backref("options", cascade="all, delete-orphan", lazy=True))


class PollVote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey("poll.id", ondelete="CASCADE"), nullable=False, index=True)
    option_id = db.Column(db.Integer, db.ForeignKey("poll_option.id", ondelete="CASCADE"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    option = db.relationship("PollOption")
    user = db.relationship("User")
    __table_args__ = (db.UniqueConstraint("poll_id", "user_id", name="unique_poll_vote"),)


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
    except (KeyError, TypeError, ValueError):
        return None
    if not all(isfinite(value) for value in (lat, lon, accuracy)):
        return None
    if not -90 <= lat <= 90 or not -180 <= lon <= 180 or accuracy <= 0:
        return None
    return lat, lon, accuracy


def setting_value(key, fallback=None):
    row = db.session.get(AppSetting, key)
    return row.value if row else fallback


def institution_names(active_only=True):
    all_rows = EducationalInstitution.query.order_by(EducationalInstitution.name.asc()).all()
    if not all_rows:
        return DEFAULT_INSTITUTIONS
    return [row.name for row in all_rows if row.active or not active_only]


def practice_settings():
    """Prefer administrator-saved coordinates; env vars are a fallback."""
    try:
        lat = float(setting_value("PRACTICE_LAT", os.getenv("PRACTICE_LAT", "")))
        lon = float(setting_value("PRACTICE_LON", os.getenv("PRACTICE_LON", "")))
        radius = float(setting_value("PRACTICE_RADIUS_M", os.getenv("PRACTICE_RADIUS_M", "100")))
        max_accuracy = float(os.getenv("MAX_GPS_ACCURACY_M", "100"))
    except (TypeError, ValueError):
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


@app.route("/style.css")
def style_css():
    from flask import send_from_directory
    return send_from_directory(str(ROOT), "style.css", mimetype="text/css")


@app.route("/attendance.js")
def attendance_js():
    from flask import send_from_directory
    return send_from_directory(str(ROOT), "attendance.js", mimetype="application/javascript")


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
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        # Scope the temporary rate limit to IP + username, so one user's typos
        # do not lock every account behind the same serverless proxy address.
        rate_key = f"{remote_addr}:{username.casefold()}"
        if login_is_limited(rate_key):
            flash("Слишком много попыток входа для этого логина. Подождите 15 минут.", "danger")
            return render_template("login.html"), 429
        user = User.query.filter_by(username=username, active=True).first()
        if user and user.check_password(password):
            LOGIN_FAILURES.pop(rate_key, None)
            login_user(user)
            return redirect(url_for("admin_dashboard" if user.role == "admin" else "student_dashboard"))
        record_login_failure(rate_key)
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
    if not practice_settings():
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

    if not practice_settings():
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

    pass_number = request.form.get("pass_number", "").strip()
    if not pass_number.isdigit() or len(pass_number) > 20:
        flash("Введите номер пропуска, состоящий только из цифр.", "danger")
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
            pass_number=pass_number,
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

    search = request.args.get("q", "").strip()
    institution = request.args.get("institution", "").strip()
    records = Attendance.query.filter_by(attendance_date=chosen_date).order_by(Attendance.checked_at.desc()).all()
    students = User.query.filter_by(role="student").order_by(User.full_name.asc()).all()

    if institution:
        records = [r for r in records if r.user.educational_institution == institution]
        students = [s for s in students if s.educational_institution == institution]
    if search:
        needle = search.casefold()
        records = [r for r in records if needle in r.user.full_name.casefold()
                   or needle in (r.pass_number or "").casefold()
                   or needle in (r.user.group_name or "").casefold()]
        students = [s for s in students if needle in s.full_name.casefold()
                    or needle in (s.group_name or "").casefold()]

    marked_ids = {r.user_id for r in records}
    missing = [s for s in students if s.active and s.id not in marked_ids]
    all_active_students = User.query.filter_by(role="student", active=True).count()
    return render_template(
        "admin_dashboard.html", records=records, students=students, missing=missing,
        chosen_date=chosen_date, search=search, selected_institution=institution,
        institutions=institution_names(), all_active_students=all_active_students,
    )


@app.route("/admin/users", methods=["GET", "POST"])
@admin_required
def admin_users():
    institutions = institution_names()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        full_name = request.form.get("full_name", "").strip()
        group_name = request.form.get("group_name", "").strip()
        institution = request.form.get("educational_institution", "").strip()
        password = request.form.get("password", "")
        if not username or not full_name or not institution or len(password) < 10:
            flash("Заполните ФИО, логин и учебное заведение; пароль должен содержать минимум 10 символов.", "danger")
        elif institution not in institutions:
            flash("Выберите учебное заведение из списка.", "danger")
        elif User.query.filter_by(username=username).first():
            flash("Такой логин уже существует.", "danger")
        else:
            user = User(username=username, full_name=full_name, group_name=group_name or None,
                        educational_institution=institution, role="student")
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            flash("Аккаунт практиканта создан.", "success")
            return redirect(url_for("admin_users"))
    users = User.query.filter_by(role="student").order_by(User.full_name.asc()).all()
    return render_template("admin_users.html", users=users, institutions=institutions)


@app.route("/admin/users/<int:user_id>/edit", methods=["GET", "POST"])
@admin_required
def edit_user(user_id):
    user = db.session.get(User, user_id)
    if not user or user.role != "student":
        abort(404)
    institutions = institution_names()
    if request.method == "POST":
        full_name = request.form.get("full_name", "").strip()
        group_name = request.form.get("group_name", "").strip()
        institution = request.form.get("educational_institution", "").strip()
        if not full_name or not institution:
            flash("Заполните ФИО и учебное заведение.", "danger")
        elif institution not in institutions:
            flash("Выберите учебное заведение из списка.", "danger")
        else:
            user.full_name = full_name
            user.group_name = group_name or None
            user.educational_institution = institution
            db.session.commit()
            flash("Данные практиканта обновлены.", "success")
            return redirect(url_for("admin_users"))
    return render_template("admin_user_edit.html", user=user, institutions=institutions)


@app.route("/admin/institutions", methods=["GET", "POST"])
@admin_required
def admin_institutions():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name or len(name) > 200:
            flash("Введите название учебного заведения (до 200 символов).", "danger")
        elif EducationalInstitution.query.filter_by(name=name).first():
            flash("Такое учебное заведение уже есть в списке.", "warning")
        else:
            db.session.add(EducationalInstitution(name=name))
            db.session.commit()
            flash("Учебное заведение добавлено.", "success")
            return redirect(url_for("admin_institutions"))
    schools = EducationalInstitution.query.order_by(EducationalInstitution.name.asc()).all()
    return render_template("admin_institutions.html", schools=schools)


@app.route("/admin/institutions/<int:school_id>/toggle", methods=["POST"])
@admin_required
def toggle_institution(school_id):
    school = db.session.get(EducationalInstitution, school_id)
    if not school:
        abort(404)
    school.active = not school.active
    db.session.commit()
    flash("Статус учебного заведения изменён.", "success")
    return redirect(url_for("admin_institutions"))


@app.route("/admin/location", methods=["GET", "POST"])
@admin_required
def admin_location():
    if request.method == "POST":
        try:
            lat = float(request.form.get("latitude", ""))
            lon = float(request.form.get("longitude", ""))
            radius = float(request.form.get("radius_m", "100"))
            if not all(isfinite(v) for v in (lat, lon, radius)) or not (-90 <= lat <= 90) or not (-180 <= lon <= 180) or radius <= 0:
                raise ValueError
        except (TypeError, ValueError):
            flash("Проверьте координаты и радиус зоны.", "danger")
        else:
            for key, value in {"PRACTICE_LAT": str(lat), "PRACTICE_LON": str(lon), "PRACTICE_RADIUS_M": str(radius)}.items():
                row = db.session.get(AppSetting, key)
                if row is None:
                    row = AppSetting(key=key, value=value)
                    db.session.add(row)
                else:
                    row.value = value
            db.session.commit()
            flash("Место практики сохранено.", "success")
            return redirect(url_for("admin_location"))
    lat = setting_value("PRACTICE_LAT", os.getenv("PRACTICE_LAT", ""))
    lon = setting_value("PRACTICE_LON", os.getenv("PRACTICE_LON", ""))
    radius = setting_value("PRACTICE_RADIUS_M", os.getenv("PRACTICE_RADIUS_M", "100"))
    latest_records = Attendance.query.order_by(Attendance.checked_at.desc()).all()
    latest_by_user = {}
    for record in latest_records:
        if record.user_id not in latest_by_user:
            latest_by_user[record.user_id] = record
    map_students = [{"name": r.user.full_name, "group": r.user.group_name or "", "lat": r.latitude,
                     "lon": r.longitude, "date": r.attendance_date.isoformat(), "time": r.checked_at.strftime("%H:%M:%S"),
                     "institution": r.user.educational_institution or ""} for r in latest_by_user.values()]
    return render_template("admin_location.html", latitude=lat, longitude=lon, radius=radius, map_students=map_students, maptiler_api_key=os.getenv("MAPTILER_API_KEY", "").strip())


@app.route("/admin/polls", methods=["GET", "POST"])
@admin_required
def admin_polls():
    if request.method == "POST":
        title = request.form.get("title", "").strip()
        description = request.form.get("description", "").strip()
        options = [line.strip() for line in request.form.get("options", "").splitlines() if line.strip()]
        if not title or len(options) < 2:
            flash("Укажите вопрос и минимум два варианта ответа (каждый с новой строки).", "danger")
        else:
            poll = Poll(title=title, description=description or None, created_by=current_user.id)
            for label in options[:20]:
                poll.options.append(PollOption(label=label[:200]))
            db.session.add(poll)
            db.session.commit()
            flash("Опрос создан и опубликован.", "success")
            return redirect(url_for("admin_polls"))
    polls = Poll.query.order_by(Poll.created_at.desc()).all()
    results = {}
    for poll in polls:
        results[poll.id] = {option.id: PollVote.query.filter_by(poll_id=poll.id, option_id=option.id).count() for option in poll.options}
    return render_template("admin_polls.html", polls=polls, results=results)


@app.route("/admin/polls/<int:poll_id>/toggle", methods=["POST"])
@admin_required
def toggle_poll(poll_id):
    poll = db.session.get(Poll, poll_id)
    if not poll:
        abort(404)
    poll.active = not poll.active
    db.session.commit()
    flash("Статус опроса изменён.", "success")
    return redirect(url_for("admin_polls"))


@app.route("/polls", methods=["GET"])
@login_required
def polls_page():
    polls = Poll.query.filter_by(active=True).order_by(Poll.created_at.desc()).all()
    voted = {v.poll_id for v in PollVote.query.filter_by(user_id=current_user.id).all()}
    results = {poll.id: {option.id: PollVote.query.filter_by(poll_id=poll.id, option_id=option.id).count() for option in poll.options} for poll in polls}
    return render_template("polls.html", polls=polls, voted=voted, results=results)


@app.route("/polls/<int:poll_id>/vote", methods=["POST"])
@login_required
def vote_poll(poll_id):
    poll = db.session.get(Poll, poll_id)
    if not poll or not poll.active:
        abort(404)
    if PollVote.query.filter_by(poll_id=poll.id, user_id=current_user.id).first():
        flash("Вы уже проголосовали в этом опросе.", "warning")
        return redirect(url_for("polls_page"))
    try:
        option_id = int(request.form.get("option_id", ""))
    except ValueError:
        abort(400)
    option = PollOption.query.filter_by(id=option_id, poll_id=poll.id).first()
    if not option:
        abort(400)
    db.session.add(PollVote(poll_id=poll.id, option_id=option.id, user_id=current_user.id))
    db.session.commit()
    flash("Ваш голос сохранён.", "success")
    return redirect(url_for("polls_page"))

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


@app.route("/admin/attendance/<int:attendance_id>/preview")
@admin_required
def attendance_photo_preview(attendance_id):
    from flask import send_file
    record = db.session.get(Attendance, attendance_id)
    if not record:
        abort(404)
    photo_path = UPLOAD_DIR / record.photo_name
    if not photo_path.exists():
        abort(404)
    suffix = photo_path.suffix.lower()
    mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(suffix, "application/octet-stream")
    return send_file(photo_path, mimetype=mime, as_attachment=False, download_name=record.photo_name)


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
    writer.writerow(["ФИО", "Номер пропуска", "Учебное заведение", "Группа", "Дата", "Время UTC", "Широта", "Долгота", "Точность GPS, м", "Статус"])
    records = Attendance.query.filter_by(attendance_date=chosen_date).order_by(Attendance.checked_at.asc()).all()
    for r in records:
        writer.writerow([r.user.full_name, r.pass_number or "", r.user.educational_institution or "", r.user.group_name or "", r.attendance_date.isoformat(), r.checked_at.strftime("%H:%M:%S"), r.latitude, r.longitude, r.accuracy_m, r.status])
    return Response("\ufeff" + output.getvalue(), mimetype="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f"attachment; filename=attendance_{chosen_date.isoformat()}.csv"})


def create_initial_admin():
    """Create or synchronize the configured bootstrap administrator.

    On Vercel, SQLite is currently ephemeral, so the environment variables are
    the bootstrap source of truth until a persistent PostgreSQL database is set.
    """
    username = os.getenv("INITIAL_ADMIN_USERNAME", "").strip()
    password = os.getenv("INITIAL_ADMIN_PASSWORD", "")
    if not username or not password:
        app.logger.warning("Initial admin not configured: set INITIAL_ADMIN_USERNAME and INITIAL_ADMIN_PASSWORD.")
        return

    full_name = os.getenv("INITIAL_ADMIN_NAME", "Администратор").strip() or "Администратор"
    admin = User.query.filter_by(username=username).first()
    if admin is None:
        admin = User(username=username, full_name=full_name, role="admin")
        db.session.add(admin)

    # Keep the configured bootstrap account usable if it already existed with
    # an old password or a non-admin role. The Vercel env password is canonical.
    admin.full_name = full_name
    admin.role = "admin"
    admin.active = True
    admin.set_password(password)
    db.session.commit()


def migrate_schema():
    """Add new profile fields to existing databases without deleting records."""
    inspector = inspect(db.engine)
    if "user" not in inspector.get_table_names():
        return
    columns = {column["name"] for column in inspector.get_columns("user")}
    additions = {
        "educational_institution": "VARCHAR(200)",
        "pass_number": "VARCHAR(20)",
    }
    with db.engine.begin() as connection:
        for name, sql_type in additions.items():
            if name not in columns:
                connection.execute(text(f'ALTER TABLE "user" ADD COLUMN {name} {sql_type}'))


with app.app_context():
    db.create_all()
    migrate_schema()
    # Existing SQLite/PostgreSQL installs get the new per-check-in pass number column.
    inspector = inspect(db.engine)
    if "attendance" in inspector.get_table_names():
        attendance_columns = {column["name"] for column in inspector.get_columns("attendance")}
        if "pass_number" not in attendance_columns:
            with db.engine.begin() as connection:
                connection.execute(text("ALTER TABLE attendance ADD COLUMN pass_number VARCHAR(20) NOT NULL DEFAULT ''"))
    if EducationalInstitution.query.count() == 0:
        db.session.add_all([EducationalInstitution(name=name) for name in DEFAULT_INSTITUTIONS])
        db.session.commit()
    create_initial_admin()


if __name__ == "__main__":
    app.run(debug=os.getenv("FLASK_DEBUG", "0") == "1")
