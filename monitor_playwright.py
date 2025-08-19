# monitor_playwright.py
import os
import re
import requests
from contextlib import suppress
from playwright.sync_api import sync_playwright, TimeoutError
from bs4 import BeautifulSoup

# Настройки
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"

PROXY_URL = os.getenv("PROXY_URL", "").strip()
HEADLESS = os.getenv("HEADLESS", "1") != "0"


def parse_proxy(url):
    # Для playwright proxy
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


def fetch_categories():
    """
    Загружает категории с главной страницы WB с помощью Playwright.
    Возвращает словарь {название: ссылка}
    """
    categories = {}
    try:
        with sync_playwright() as pw:
            proxy = parse_proxy(PROXY_URL) if PROXY_URL else None
            browser = pw.chromium.launch(headless=HEADLESS, proxy=proxy)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                locale="ru-RU"
            )
            page = context.new_page()
            page.goto(WB_MAIN_CATALOG, timeout=30000)
            html = page.content()
            browser.close()

        soup = BeautifulSoup(html, "html.parser")

        for a in soup.select("a.menu-burger__main-list-link"):
            name = a.get_text(strip=True)
            href = a.get("href")
            if name and href and href.startswith("/catalog"):
                categories[name] = WB_BASE_URL + href

        print(f"[fetch_categories] Загружено категорий: {len(categories)}")

    except Exception as e:
        print("[fetch_categories] Ошибка:", repr(e))

    return categories


# Пример вызова
if __name__ == "__main__":
    cats = fetch_categories()
    for name, url in cats.items():
        print(f"- {name}: {url}")
