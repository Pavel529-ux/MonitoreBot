# monitor_playwright.py

import os
import re
import requests
from bs4 import BeautifulSoup

# Настройки
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"

def fetch_categories():
    """
    Загружает основные категории с главной страницы WB
    Возвращает словарь {название: ссылка}
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36",
            "Referer": "https://www.wildberries.ru/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
            "Connection": "keep-alive"
        }

        resp = requests.get(WB_MAIN_CATALOG, timeout=10, headers=headers)
        if resp.status_code != 200:
            print("[fetch_categories] Ошибка загрузки:", resp.status_code)
            return {}

        soup = BeautifulSoup(resp.text, "html.parser")
        cats = {}

        for a in soup.select("a.menu-burger__main-list-link"):
            name = a.get_text(strip=True)
            href = a.get("href")
            if name and href and href.startswith("/catalog"):
                full_url = WB_BASE_URL + href
                cats[name] = full_url

        print(f"[fetch_categories] Загружено категорий: {len(cats)}")
        return cats

    except Exception as e:
        print("[fetch_categories] Ошибка:", repr(e))
        return {}

# Пример вызова при отладке
if __name__ == "__main__":
    categories = fetch_categories()
    for name, url in categories.items():
        print(f"- {name}: {url}")
