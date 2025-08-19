# monitor_playwright.py
import os
import logging
from contextlib import suppress
from playwright.async_api import async_playwright, TimeoutError
from bs4 import BeautifulSoup

# Настройки
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"
PROXY_URL = os.getenv("PROXY_URL", "").strip()
HEADLESS = os.getenv("HEADLESS", "1") != "0"

logging.basicConfig(level=logging.INFO)

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


async def fetch_categories():
    categories = {}
    try:
        async with async_playwright() as pw:
            proxy = parse_proxy(PROXY_URL) if PROXY_URL else None
            browser = await pw.chromium.launch(headless=HEADLESS, proxy=proxy)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:114.0) Gecko/20100101 Firefox/114.0",
                locale="ru-RU",
                extra_http_headers={
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                }
            )
            page = await context.new_page()
            await page.goto(WB_MAIN_CATALOG, timeout=30000)
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
