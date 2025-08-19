import os
import logging
import requests
from bs4 import BeautifulSoup

# 📦 Константы
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:114.0) Gecko/20100101 Firefox/114.0",
    "Accept-Language": "ru,en;q=0.9",
}

logging.basicConfig(level=logging.INFO)


# 📁 Категории WB
def fetch_categories():
    categories = {}
    try:
        resp = requests.get(WB_MAIN_CATALOG, headers=HEADERS, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        for a in soup.select("a.menu-burger__main-list-link"):
            name = a.get_text(strip=True)
            href = a.get("href")
            if name and href and href.startswith("/catalog"):
                categories[name] = WB_BASE_URL + href

        logging.info(f"[fetch_categories] Найдено категорий: {len(categories)}")

    except Exception as e:
        logging.error("[fetch_categories] Ошибка: %s", repr(e))

    return categories


# 📦 Парсинг товаров в категории (упрощённо)
def fetch_products_for_category(category_url, max_pages=1):
    products = []

    try:
        for page in range(1, max_pages + 1):
            url = f"{category_url}?page={page}"
            resp = requests.get(url, headers=HEADERS, timeout=20)
            resp.raise_for_status()

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
                            bonus_text = bonus_tag.get_text(strip=True)
                            bonus = int("".join(filter(str.isdigit, bonus_text)))
                        except:
                            pass

                    link = WB_BASE_URL + link_tag.get("href")

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
