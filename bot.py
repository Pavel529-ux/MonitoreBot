import asyncio
import logging
import os
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton
from redis.asyncio import Redis

from monitor_playwright import run_monitor  # ✅ Импорт основного парсера

# 🔐 Переменные окружения
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
REDIS_URL = os.getenv("REDIS_URL")
BONUS_MIN_PCT = int(os.getenv("BONUS_MIN_PCT", 20))
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", 200))

# 📦 Redis
redis = Redis.from_url(REDIS_URL, decode_responses=True)

# ⚙️ Логирование и бот
logging.basicConfig(level=logging.INFO)
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# 📁 Категории (можно расширить вручную или автоматически позже)
CATEGORIES = {
    "Одежда": "https://www.wildberries.ru/catalog/zhenshchinam/odezhda",
    "Обувь": "https://www.wildberries.ru/catalog/obuv",
    "Электроника": "https://www.wildberries.ru/catalog/elektronika",
    "Косметика": "https://www.wildberries.ru/catalog/krasota",
}

# 🎛 Главное меню
def main_keyboard():
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=cat, callback_data=f"category:{url}")]
            for cat, url in CATEGORIES.items()
        ]
    )

# 👋 Команда /start
@dp.message(CommandStart())
async def cmd_start(message: Message):
    await message.answer(
        "👋 Привет! Выбери категорию для поиска товаров с отзывными бонусами:",
        reply_markup=main_keyboard()
    )

# 🛒 Обработка категории
@dp.callback_query(F.data.startswith("category:"))
async def process_category(callback: CallbackQuery):
    category_url = callback.data.split(":", 1)[1]
    await callback.message.edit_text("🔍 Ищу товары, это займёт 5–15 секунд...")

    try:
        # ⚙️ Парсим реальные товары через playwright
        items = await run_monitor(
            urls=[category_url],
            min_bonus_pct=BONUS_MIN_PCT,
            min_bonus_rub=BONUS_MIN_RUB
        )

        if not items:
            await callback.message.edit_text("❌ Подходящих товаров не найдено.")
            return

        text = "🎯 Найденные товары:\n\n"
        for item in items[:10]:  # максимум 10 штук
            text += (
                f"🛍 <b>{item['name']}</b>\n"
                f"💰 Цена: {item['price']} ₽\n"
                f"🎁 Бонус: {item['bonus']} ₽\n"
                f"🔗 <a href='{item['url']}'>Смотреть</a>\n\n"
            )

        await callback.message.edit_text(text, parse_mode="HTML")

    except Exception as e:
        logging.exception("Ошибка при поиске товаров")
        await callback.message.edit_text("⚠️ Произошла ошибка при поиске. Попробуй позже.")

# 🚀 Запуск
async def main():
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
