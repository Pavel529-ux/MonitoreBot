import os
import re
import json
import logging

from playwright.async_api import async_playwright
from bs4 import BeautifulSoup

# 📦 Настройки из окружения
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"
PROXY_URL = os.getenv("PROXY_URL", "").strip()
HEADLESS = os.getenv("HEADLESS", "1") != "0"

logging.basicConfig(level=logging.INFO)


# 🔧 Прокси парсер
def parse_proxy(url):
    if not url or not url.startswith("http"):
        return None
    try:
        body = url.split("://", 1)[1]
        if "@" in body:
            creds, host = body.split("@", 1)
            if ":" in creds:
                user, pwd = creds.split(":", 1)
                server = url.split("://")[0] + "://" + host
                return {"server": server, "username": user, "password": pwd}
        return {"server": url}
    except Exception:
        return {"server": url}


# 📁 Парсинг категорий Wildberries
async def fetch_categories():
    categories = {}
    try:
        async with async_playwright() as pw:
            proxy = parse_proxy(PROXY_URL) if PROXY_URL else None
            browser = await pw.chromium.launch(headless=HEADLESS, proxy=proxy)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:114.0) Gecko/20100101 Firefox/114.0",
                locale="ru-RU"
            )
            page = await context.new_page()
            await page.goto(WB_MAIN_CATALOG, timeout=60000, wait_until="domcontentloaded")
            html = await page.content()
            await browser.close()

        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("a.menu-burger__main-list-link"):
            name = a.get_text(strip=True)
            href = a.get("href")
            if name and href and href.startswith("/catalog"):
                categories[name] = WB_BASE_URL + href

        logging.info(f"[fetch_categories] Загружено категорий: {len(categories)}")

    except Exception as e:
        logging.error("[fetch_categories] Ошибка: %s", repr(e))

    return categories


# 📦 Парсинг товаров по ссылке категории
async def fetch_products_for_category(category_url, max_pages=3):
    """
    Парсит список товаров из категории WB.
    Возвращает список словарей с товарами.
    """
    products = []

    try:
        async with async_playwright() as pw:
            proxy = parse_proxy(PROXY_URL) if PROXY_URL else None
            browser = await pw.chromium.launch(headless=HEADLESS, proxy=proxy)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:114.0) Gecko/20100101 Firefox/114.0",
                locale="ru-RU"
            )
            page = await context.new_page()

            for page_num in range(1, max_pages + 1):
                url = f"{category_url}?page={page_num}"
                await page.goto(url, timeout=45000)
                html = await page.content()

                match = re.search(r"window\.__WIDGET__ = (\{.*?\});", html)
                if not match:
                    continue

                data_raw = match.group(1)
                data = json.loads(data_raw)

                widgets = data.get("widgets", {})
                for widget in widgets.values():
                    for good in widget.get("items", []):
                        name = good.get("name")
                        price = good.get("priceU", 0) // 100
                        bonus = good.get("rewardAmount", 0) // 100
                        url = f"https://www.wildberries.ru/catalog/{good.get('id')}/detail.aspx"
                        if name and price:
                            products.append({
                                "name": name,
                                "price": price,
                                "bonus": bonus,
                                "url": url
                            })

            await browser.close()

        logging.info(f"[fetch_products_for_category] Найдено товаров: {len(products)}")

    except Exception as e:
        logging.error("[fetch_products_for_category] Ошибка: %s", repr(e))

    return products
