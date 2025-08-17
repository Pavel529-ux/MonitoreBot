FROM mcr.microsoft.com/playwright/python:v1.45.0-jammy

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app

# чтобы логи сразу шли в Railway Logs
ENV PYTHONUNBUFFERED=1

# основной процесс
CMD ["python", "-u", "monitor_playwright.py"]
