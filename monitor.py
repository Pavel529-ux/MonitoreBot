import os, re, time, json
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests

# === Настройки из переменных окружения ===
BONUS_THRESHOLD = float(os.getenv("BONUS_THRESHOLD", "0.9"))  # 0.9 = 90%
MAX_PAGES = int(os.getenv("MAX_PAGES", "3"))
MIN_PRICE = float(os.getenv("MIN_PRICE", "0"))
MAX_PRICE = float(os.getenv("MAX_PRICE", "0"))

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WB_FEED_URLS = [u.strip() for u in os.getenv("WB_FEED_URLS", "").split(",") if u.strip()]

REDIS_URL = os.getenv("REDIS_URL")  # добавим Redis для анти-дублей
rds = None
if REDIS_URL:
    try:
        import redis
        rds = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception:
        rds = None

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_FEED_URLS):
    raise SystemExit("Нужны TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_FEED_URLS")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
BONUS_RE = re.compile(r'(\d{2,5})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)

def set_param(url: str, key: str, value) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q[key] = [str(value)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))

def extract_bonus_from_any(obj):
    if isinstance(obj, str):
        m = BONUS_RE.search(obj)
        return int(m.group(1)) if m else None
    if isinstance(obj, dict):
        for v in obj.values():
            b = extract_bonus_from_any(v)
            if b: return b
    if isinstance(obj, list):
        for v in obj:
            b = extract_bonus_from_any(v)
            if b: return b
    return None

def fetch_products(feed_url: str, max_pages: int):
    for p in range(1, max_pages + 1):
        url = set_param(feed_url, "page", p)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
            r.raise_for_status()
            data = r.json()
        except Exception:
            break
        products = (data.get("data") or {}).get("products") or []
        if not products:
            break
        for item in products:
            yield item

def fallback_bonus_from_card(nm_id: int):
    url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        if r.ok:
            m = BONUS_RE.search(r.text)
            if m: return int(m.group(1))
    except Exception:
        pass
    return None

def get_price(item):
    price_u = item.get("salePriceU") or item.get("priceU") or 0
    try:
        return float(price_u) / 100.0
    except Exception:
        return 0.0

def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    requests.post(url, json=payload, timeout=20)

def is_notified(nm_id: int, bonus: int) -> bool:
    """Проверка через Redis, чтобы не слать одно и то же. TTL 7 дней."""
    if not rds:
        return False
    key = f"notified:{nm_id}"
    prev = rds.get(key)
    if not prev or int(bonus) > int(prev):
        rds.set(key, int(bonus), ex=7*24*3600)
        return False
    return True

found = 0
for feed in WB_FEED_URLS:
    for item in fetch_products(feed, MAX_PAGES):
        nm = item.get("id") or item.get("nmId") or item.get("nm")
        if not nm:
            continue
        name = item.get("name") or "Без названия"
        price = get_price(item)
        if price <= 0:
            continue
        if MIN_PRICE and price < MIN_PRICE:
            continue
        if MAX_PRICE and price > MAX_PRICE:
            continue

        bonus = None
        for key in ("promoTextCard", "promoTextCat", "description", "extended"):
            if key in item:
                bonus = extract_bonus_from_any(item[key])
                if bonus:
                    break
        if not bonus:
            bonus = fallback_bonus_from_card(int(nm))
            time.sleep(0.3)

        if not bonus:
            continue

        ratio = bonus / price if price else 0
        if ratio < BONUS_THRESHOLD:
            continue

        if is_notified(int(nm), int(bonus)):
            continue

        link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
        msg = (f"🔥 Высокий бонус за отзыв\n"
               f"{name}\n"
               f"Бонус: {bonus} ₽ | Цена: {int(price)} ₽ | Коэффициент: {ratio:.2f}\n"
               f"{link}")
        try:
            send_telegram(msg)
            found += 1
            time.sleep(0.4)
        except Exception:
            pass

print(f"Done. New alerts: {found}")
