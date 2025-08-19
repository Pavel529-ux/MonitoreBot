import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from redis.asyncio import Redis

from monitor_playwright import fetch_categories, fetch_products_for_category  # ✅ Асинхронный парсинг

# 📦 Переменные окружения
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
BONUS_MIN_PCT = int(os.getenv("BONUS_MIN_PCT", 20))
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", 200))

# 🔌 Redis
redis = Redis.from_url(REDIS_URL, decode_responses=True)

# 🤖 Инициализация бота
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()


# 🔘 Генерация клавиатуры категорий
async def get_keyboard():
    categories = await fetch_categories()
    if not categories:
        return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Категории не найдены", callback_data="none")]])
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=f"category:{url}")]
        for name, url in categories.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# 🟢 Обработка команды /start
@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = await get_keyboard()
    await message.answer("👋 Выберите категорию для поиска товаров с бонусами за отзыв:", reply_markup=kb)


# 🟡 Обработка выбора категории
@dp.callback_query(F.data.startswith("category:"))
async def category_handler(callback: CallbackQuery):
    url = callback.data.split(":", 1)[1]
    await callback.message.edit_text("🔎 Ищу товары, подождите...")

    try:
        items = await fetch_products_for_category(url)

        # 🧮 Фильтрация
        filtered = [
            item for item in items
            if item["bonus"] >= BONUS_MIN_RUB and (item["bonus"] / item["price"]) * 100 >= BONUS_MIN_PCT
        ]

        if not filtered:
            await callback.message.edit_text("❌ Подходящих товаров не найдено.")
        else:
            text = "🎯 Найденные товары:\n\n"
            for item in filtered[:10]:  # Ограничим до 10 товаров
                text += (
                    f"🛍 <b>{item['name']}</b>\n"
                    f"💰 Цена: {item['price']} ₽\n"
                    f"🎁 Бонус: {item['bonus']} ₽\n"
                    f"🔗 <a href='{item['url']}'>Смотреть</a>\n\n"
                )
            await callback.message.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logging.exception("Ошибка при обработке категории")
        await callback.message.edit_text("⚠️ Произошла ошибка при поиске. Попробуйте позже.")


# 🚀 Точка входа
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
