import os
import time
import requests
import telebot

# --- Конфигурация ---
TOKEN = os.getenv("TELEGRAM_TOKEN")       # Токен бота из BotFather
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")   # Твой ID (кому слать уведомления)
FEED_URL = os.getenv("WB_FEED_URL")       # Ссылка WB (категория / поиск)
CHECK_INTERVAL = 60                       # Проверка раз в 60 секунд

# Создаём телеграм-бота
bot = telebot.TeleBot(TOKEN)

# Храним уже увиденные товары, чтобы не дублировать
seen_items = set()

def fetch_items():
    """Получаем список товаров с WB"""
    try:
        resp = requests.get(FEED_URL, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return data.get("data", {}).get("products", [])
    except Exception as e:
        print("Ошибка при получении данных:", e)
        return []

def check_promos():
    """Проверяем товары на акцию Баллы за отзывы"""
    items = fetch_items()
    for item in items:
        name = item.get("name")
        price = item.get("priceU", 0) // 100   # Цена в рублях
        promo_texts = item.get("promoTextCard", [])
        url = f"https://www.wildberries.ru/catalog/{item.get('id')}/detail.aspx"

        # Проверяем промо
        for promo in promo_texts:
            if "Баллы за отзывы" in promo and item["id"] not in seen_items:
                seen_items.add(item["id"])
                message = f"🔥 Найден товар с акцией!\n\n{name}\nЦена: {price} руб.\n{url}\nАкция: {promo}"
                print(message)
                bot.send_message(CHAT_ID, message)

def main():
    print("WB мониторинг запущен (только Баллы за отзывы)...")
    while True:
        check_promos()
        time.sleep(CHECK_INTERVAL)

if __name__ == "__main__":
    main()
