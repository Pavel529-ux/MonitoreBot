import asyncio
import logging
import os
import json
import random
import aiohttp

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from redis.asyncio import Redis

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
BONUS_MIN_PCT = int(os.getenv("BONUS_MIN_PCT", 20))
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", 200))

redis = Redis.from_url(REDIS_URL, decode_responses=True)
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

CATEGORY_CACHE_KEY = "wb_categories"
CATEGORY_CACHE_TTL = 3600  # 1 час

async def fetch_categories():
    url = "https://static.wbstatic.net/data/main-menu-ru-ru.json"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as response:
                return await response.json()
    except Exception as e:
        logging.error(f"Ошибка загрузки категорий: {e}")
        return []

async def get_categories():
    cached = await redis.get(CATEGORY_CACHE_KEY)
    if cached:
        return json.loads(cached)

    raw = await fetch_categories()
    categories = {}

    def extract_links(items):
        for item in items:
            if "url" in item and item["url"].startswith("/catalog/"):
                full_url = f"https://www.wildberries.ru{item['url']}"
                name = item.get("name", "Категория")
                categories[name] = full_url
            if "childs" in item:
                extract_links(item["childs"])

    extract_links(raw)
    await redis.set(CATEGORY_CACHE_KEY, json.dumps(categories), ex=CATEGORY_CACHE_TTL)
    return categories

def build_keyboard(categories):
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"category:{url}")]
        for name, url in categories.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons[:30])  # максимум 30 кнопок

@dp.message(CommandStart())
async def cmd_start(message: Message):
    categories = await get_categories()
    await message.answer("👋 Привет! Выбери категорию для поиска товаров:", reply_markup=build_keyboard(categories))

@dp.callback_query(F.data.startswith("category:"))
async def process_category(callback: CallbackQuery):
    category_url = callback.data.split(":", 1)[1]
    await callback.message.edit_text("🔍 Ищу товары...")

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
        text = "🎯 Найденные товары:

"
        for item in filtered:
            text += f"🛍 <b>{item['name']}</b>
💸 Бонус: {item['bonus']} ₽
💰 Цена: {item['price']} ₽
🔗 <a href='{item['url']}'>Смотреть</a>

"
        await callback.message.edit_text(text, parse_mode="HTML")

async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())