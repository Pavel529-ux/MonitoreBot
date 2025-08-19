# ✅ Используем официальный Playwright образ с уже установленными браузерами и зависимостями
FROM mcr.microsoft.com/playwright/python:v1.46.0-jammy

# Устанавливаем рабочую директорию
WORKDIR /app

# Копируем зависимости
COPY requirements.txt .

# Устанавливаем Python-библиотеки
RUN pip install --no-cache-dir -r requirements.txt

# Копируем весь исходный код в контейнер
COPY . .

# Устанавливаем Playwright браузеры
RUN playwright install

# 🚀 Команда запуска
CMD ["python", "bot.py"]
