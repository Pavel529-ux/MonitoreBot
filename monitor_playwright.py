import os
import re
import requests
import random
import time
from bs4 import BeautifulSoup

# Настройки
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"
PROXY_URL = os.getenv("WB_PROXY_URL")

# Список возможных User-Agent для обхода 498
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:102.0) Gecko/20100101 Firefox/102.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 12_5) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/15.6 Safari/605.1.15",
]

def fetch_categories():
    """
    Загружает основные категории с главной страницы WB
    Возвращает словарь {название: ссылка}
    """
    try:
        headers = {
            "User-Agent": random.choice(USER_AGENTS),
            "Referer": "https://www.wildberries.ru/",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ru,en;q=0.9",
            "Connection": "keep-alive"
        }

        proxies = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None
        time.sleep(random.uniform(1.5, 3.0))  # Антиспам задержка

        resp = requests.get(WB_MAIN_CATALOG, timeout=10, headers=headers, proxies=proxies)

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

# Пример ручного вызова
if __name__ == "__main__":
    categories = fetch_categories()
    for name, url in categories.items():
        print(f"- {name}: {url}")
