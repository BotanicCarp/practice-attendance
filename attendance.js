(() => {
  const form = document.getElementById("attendance-form");
  if (!form) return;

  const button = document.getElementById("checkin-button");
  const statusBox = document.getElementById("location-status");
  const fileInput = document.getElementById("pass_photo");
  const fileName = document.getElementById("file-name");

  fileInput.addEventListener("change", () => {
    const file = fileInput.files[0];
    fileName.textContent = file ? `${file.name} (${(file.size / 1024 / 1024).toFixed(2)} МБ)` : "Файл не выбран";
    if (file && file.size > 5 * 1024 * 1024) {
      statusBox.textContent = "Файл больше 5 МБ. Выберите фотографию меньшего размера.";
      statusBox.className = "location-status is-error";
      fileInput.value = "";
      fileName.textContent = "Файл не выбран";
    }
  });

  button.addEventListener("click", () => {
    if (!fileInput.files.length) {
      statusBox.textContent = "Сначала загрузите фотографию пропуска.";
      statusBox.className = "location-status is-error";
      fileInput.focus();
      return;
    }
    if (!navigator.geolocation) {
      statusBox.textContent = "Этот браузер не поддерживает геолокацию.";
      statusBox.className = "location-status is-error";
      return;
    }

    button.disabled = true;
    button.textContent = "Проверяем местоположение…";
    statusBox.textContent = "Разрешите доступ к геолокации, если браузер спросит.";
    statusBox.className = "location-status is-pending";

    navigator.geolocation.getCurrentPosition(async (position) => {
      const { latitude, longitude, accuracy } = position.coords;
      try {
        const response = await fetch("/api/location-check", {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "X-CSRFToken": document.querySelector('meta[name="csrf-token"]').content
          },
          body: JSON.stringify({ latitude, longitude, accuracy })
        });
        const result = await response.json();
        if (!response.ok || !result.ok) {
          statusBox.textContent = result.message || "Проверка местоположения не пройдена.";
          statusBox.className = "location-status is-error";
          button.disabled = false;
          button.innerHTML = 'Проверить место и отметить приход <span>→</span>';
          return;
        }

        document.getElementById("latitude").value = latitude;
        document.getElementById("longitude").value = longitude;
        document.getElementById("accuracy").value = accuracy;
        statusBox.textContent = `Местоположение подтверждено. Расстояние: ${result.distance_m} м; точность: ${result.accuracy_m} м. Сохраняем отметку…`;
        statusBox.className = "location-status is-success";
        form.submit();
      } catch (error) {
        statusBox.textContent = "Не удалось связаться с сервером. Проверьте соединение.";
        statusBox.className = "location-status is-error";
        button.disabled = false;
        button.innerHTML = 'Проверить место и отметить приход <span>→</span>';
      }
    }, (error) => {
      const messages = {
        1: "Доступ к геолокации запрещён. Разрешите его в настройках браузера.",
        2: "Местоположение недоступно. Включите геолокацию и попробуйте ещё раз.",
        3: "Истекло время ожидания геолокации. Попробуйте ещё раз."
      };
      statusBox.textContent = messages[error.code] || "Не удалось получить местоположение.";
      statusBox.className = "location-status is-error";
      button.disabled = false;
      button.innerHTML = 'Проверить место и отметить приход <span>→</span>';
    }, { enableHighAccuracy: true, timeout: 15000, maximumAge: 0 });
  });
})();
