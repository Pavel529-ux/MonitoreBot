import os
import logging
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

# 🔧 Настройка логов
logging.basicConfig(level=logging.INFO)

# 🌍 Основные константы
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"

# 📦 Заголовки максимально приближенные к реальному браузеру
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Encoding": "gzip, deflate, br",
    "Accept-Language": "ru,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1"
}

# 🛡 Поддержка прокси из переменных окружения
PROXY_URL = os.getenv("PROXY_URL", "")
PROXIES = {
    "http": PROXY_URL,
    "https": PROXY_URL
} if PROXY_URL else None

if PROXY_URL:
    logging.info(f"🌐 Используется прокси: {PROXIES}")


# 📁 Получение списка категорий
def fetch_categories():
    categories = {}

    try:
        resp = requests.get(WB_MAIN_CATALOG, headers=HEADERS, proxies=PROXIES, timeout=15)

        if resp.status_code != 200:
            logging.error(f"[fetch_categories] Код ответа: {resp.status_code}")
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.select("a.menu-burger__main-list-link"):
            name = a.get_text(strip=True)
            href = a.get("href")

            if name and href and href.startswith("/catalog"):
                categories[name] = urljoin(WB_BASE_URL, href)

        logging.info(f"[fetch_categories] Найдено категорий: {len(categories)}")

    except Exception as e:
        logging.error("[fetch_categories] Ошибка: %s", repr(e))

    return categories


# 📦 Получение товаров из категории
def fetch_products_for_category(category_url, max_pages=1):
    products = []

    try:
        for page in range(1, max_pages + 1):
            url = f"{category_url}?page={page}"
            resp = requests.get(url, headers=HEADERS, proxies=PROXIES, timeout=15)

            if resp.status_code != 200:
                logging.warning(f"[fetch_products_for_category] Код ответа: {resp.status_code} для страницы {url}")
                continue

            soup = BeautifulSoup(resp.text, "html.parser")

            for card in soup.select("div.product-card"):
                name_tag = card.select_one(".product-card__name")
                price_tag = card.select_one(".price__lower-price")
                bonus_tag = card.select_one(".product-card__bonus-percent")
                link_tag = card.select_one("a")

                if name_tag and price_tag and link_tag:
                    name = name_tag.get_text(strip=True)
                    price = int(price_tag.get_text(strip=True).replace("₽", "").replace(" ", ""))
                    bonus = 0

                    if bonus_tag:
                        try:
                            bonus = int("".join(filter(str.isdigit, bonus_tag.get_text(strip=True))))
                        except ValueError:
                            pass

                    link = urljoin(WB_BASE_URL, link_tag.get("href"))

                    products.append({
                        "name": name,
                        "price": price,
                        "bonus": bonus,
                        "url": link
                    })

        logging.info(f"[fetch_products_for_category] Найдено товаров: {len(products)}")

    except Exception as e:
        logging.error("[fetch_products_for_category] Ошибка: %s", repr(e))

    return products
