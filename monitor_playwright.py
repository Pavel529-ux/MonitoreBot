import os
import re
import requests
from bs4 import BeautifulSoup

# Настройки
WB_BASE_URL = "https://www.wildberries.ru"
WB_MAIN_CATALOG = f"{WB_BASE_URL}/catalog"

# Получаем переменную окружения, если есть
def envf(name, default=None):
    val = os.getenv(name)
    return val if val else default

# Твоя переменная прокси
PROXY_URL = envf("PROXY_URL", "")

def fetch_categories():
    """
    Загружает основные категории с главной страницы WB
    Возвращает словарь {название: ссылка}
    """
    try:
        # Важно: отключаем прокси для requests!
        resp = requests.get(
            WB_MAIN_CATALOG,
            timeout=10,
            headers={"User-Agent": "Mozilla/5.0"},
            proxies={}  # отключаем использование прокси
        )
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
