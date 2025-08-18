import asyncio
import logging
import os
import json
import random

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from redis.asyncio import Redis

# 📌 Настройки из переменных среды
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
BONUS_MIN_PCT = int(os.getenv("BONUS_MIN_PCT", 20))
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", 200))

# 🧠 Redis для хранения истории
redis = Redis.from_url(REDIS_URL, decode_responses=True)

# 🤖 Настройка бота и логирования
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 📚 Категории (пока статично, потом добавим автообновление)
CATEGORIES = {
    "Одежда": "https://www.wildberries.ru/catalog/obuv",
    "Электроника": "https://www.wildberries.ru/catalog/elektronika",
    "Косметика": "https://www.wildberries.ru/catalog/krasota",
}

# 📍 Главная клавиатура
def main_keyboard():
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"category:{url}")]
        for name, url in CATEGORIES.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

# 🟢 Стартовая команда
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer("👋 Привет! Выбери категорию для мониторинга товаров:", reply_markup=main_keyboard())

# 📦 Обработка выбора категории
@dp.callback_query(F.data.startswith("category:"))
async def process_category(callback: CallbackQuery):
    category_url = callback.data.split(":", 1)[1]
    await callback.message.edit_text("🔍 Ищу товары... Это займёт 3–5 секунд")

    # 🎯 Симуляция поиска подходящих товаров
    fake_items = [
        {
            "name": f"Товар #{i+1}",
            "bonus": random.randint(100, 500),
            "price": random.randint(300, 1500),
            "url": f"{category_url}/detail.aspx?fake_id={random.randint(100000,999999)}"
        }
        for i in range(10)
    ]

    filtered = [
        item for item in fake_items
        if item["bonus"] >= BONUS_MIN_RUB and item["bonus"] / item["price"] * 100 >= BONUS_MIN_PCT
    ]

    if not filtered:
        await callback.message.edit_text("❌ Подходящих товаров не найдено.")
    else:
        text = "🎁 Найдено подходящих товаров:\n\n"
        for item in filtered:
            text += f"🛍 <b>{item['name']}</b>\n💸 Бонус: {item['bonus']} ₽\n💰 Цена: {item['price']} ₽\n🔗 <a href='{item['url']}'>Смотреть</a>\n\n"
        await callback.message.edit_text(text, parse_mode="HTML")

# 🚀 Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
