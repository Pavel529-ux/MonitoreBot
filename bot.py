import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from redis.asyncio import Redis

from monitor_playwright import fetch_categories  # импорт из твоего файла

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
BONUS_MIN_PCT = int(os.getenv("BONUS_MIN_PCT", 20))
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", 200))

redis = Redis.from_url(REDIS_URL, decode_responses=True)
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 🔄 Генерация актуальных кнопок
async def get_keyboard():
    categories = fetch_categories()
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"category:{url}")]
        for name, url in categories.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = await get_keyboard()
    await message.answer("👋 Выберите категорию для поиска товаров с бонусами за отзыв:", reply_markup=kb)

@dp.callback_query(F.data.startswith("category:"))
async def category_handler(callback: CallbackQuery):
    url = callback.data.split(":", 1)[1]
    await callback.message.edit_text("🔎 Поиск товаров...")

    # Заглушка: фейковые данные (реальное подключение к парсингу будет позже)
    import random
    items = [
        {
            "name": f"Товар {i+1}",
            "price": random.randint(500, 1500),
            "bonus": random.randint(50, 600),
            "url": f"{url}?fake_id={random.randint(100000,999999)}"
        }
        for i in range(8)
    ]

    filtered = [
        item for item in items
        if item["bonus"] >= BONUS_MIN_RUB and (item["bonus"] / item["price"]) * 100 >= BONUS_MIN_PCT
    ]

    if not filtered:
        await callback.message.edit_text("❌ Подходящих товаров не найдено.")
    else:
        text = "🎯 Найденные товары:\n\n"
        for item in filtered:
            text += (
                f"🛍 <b>{item['name']}</b>\n"
                f"💰 Цена: {item['price']} ₽\n"
                f"🎁 Бонус: {item['bonus']} ₽\n"
                f"🔗 <a href='{item['url']}'>Смотреть</a>\n\n"
            )
        await callback.message.edit_text(text, parse_mode="HTML")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
