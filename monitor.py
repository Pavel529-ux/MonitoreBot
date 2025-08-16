import os, re, time, signal, traceback
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests

# ====== Конфиг через переменные окружения ======
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")  # В Railway уже есть
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")    # Ваш chat_id
WB_FEED_URLS       = [u.strip() for u in os.getenv("WB_FEED_URLS","").split(",") if u.strip()]

CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "600"))  # сек между циклами (по умолчанию 10 мин)
MAX_PAGES       = int(os.getenv("MAX_PAGES", "3"))
REDIS_URL       = os.getenv("REDIS_URL")                   # можно не задавать

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_FEED_URLS):
    raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_FEED_URLS")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# Плашка/текст акции «Баллы за отзыв» (иногда пишут «рубли за отзыв»)
BONUS_RE = re.compile(r'(\d{2,5})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)

# ====== Redis для анти-дублей (не обязателен) ======
rds = None
if REDIS_URL:
    try:
        import redis
        rds = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
        rds.ping()
        print("[init] Redis OK")
    except Exception as e:
        print("[init] Redis connect error:", e)
        rds = None

# На случай, если Redis не задан — держим локальный кэш (сбрасывается при рестарте контейнера)
local_seen = set()

def dedup_key(nm_id: int) -> str:
    return f"seen:{nm_id}"

def seen_before(nm_id: int) -> bool:
    """True если уже слали уведомление по этому товару."""
    key = dedup_key(nm_id)
    if rds:
        try:
            if rds.get(key):
                return True
            rds.set(key, "1", ex=7*24*3600)  # помним 7 дней
            return False
        except Exception:
            pass
    # локально
    if nm_id in local_seen:
        return True
    local_seen.add(nm_id)
    return False

def set_param(url: str, key: str, value) -> str:
    u = urlparse(url)
    q = parse_qs(u.query)
    q[key] = [str(value)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))

def fetch_products(feed_url: str, max_pages: int):
    """Читает публичный JSON WB. Предполагаем, что фид уже с фильтром ffeedbackpoints=1."""
    for p in range(1, max_pages + 1):
        url = set_param(feed_url, "page", p)
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        data = r.json()
        products = (data.get("data") or {}).get("products") or []
        if not products:
            break
        for item in products:
            yield item

def extract_bonus_from_any(obj):
    """Достаём число бонуса из любого текстового поля, если WB прислал текст плашки."""
    if isinstance(obj, str):
        m = BONUS_RE.search(obj)
        if m: return int(m.group(1))
    elif isinstance(obj, dict):
        for v in obj.values():
            b = extract_bonus_from_any(v)
            if b: return b
    elif isinstance(obj, list):
        for v in obj:
            b = extract_bonus_from_any(v)
            if b: return b
    return None

def fallback_bonus_from_card(nm_id: int):
    """Если в JSON нет текста плашки — пробуем вытащить из HTML карточки."""
    url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        if r.ok:
            m = BONUS_RE.search(r.text)
            if m:
                return int(m.group(1))
    except Exception:
        pass
    return None

def send_telegram(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    requests.post(api, json=payload, timeout=20)

def one_scan() -> int:
    """Шлём ТОЛЬКО товары с акцией «Баллы за отзывы». Фид уже отфильтрован ffeedbackpoints=1."""
    sent = 0
    for feed in WB_FEED_URLS:
        try:
            for item in fetch_products(feed, MAX_PAGES):
                nm = item.get("id") or item.get("nmId") or item.get("nm")
                if not nm:
                    continue
                nm = int(nm)
                if seen_before(nm):
                    continue

                name = item.get("name") or "Без названия"
                price_u = item.get("salePriceU") or item.get("priceU") or 0
                price = int(price_u) // 100 if price_u else 0
                link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

                # Пробуем достать конкретный размер бонуса (это не обязательно, но красиво)
                bonus = None
                for key in ("promoTextCard", "promoTextCat", "description", "extended"):
                    if key in item:
                        bonus = extract_bonus_from_any(item[key])
                        if bonus: break
                if not bonus:
                    bonus = fallback_bonus_from_card(nm)

                if bonus:
                    msg = f"🎯 Баллы за отзыв\n{name}\nБонус: {bonus} ₽ | Цена: {price} ₽\n{link}"
                else:
                    # если число не нашли — всё равно шлём (фид уже с ffeedbackpoints=1)
                    msg = f"🎯 Баллы за отзыв\n{name}\nЦена: {price} ₽\n{link}"

                try:
                    send_telegram(msg)
                    sent += 1
                    time.sleep(0.4)  # бережём API
                except Exception as e:
                    print("[warn] telegram error:", e)
        except Exception as e:
            print("[warn] feed failed:", e)
            traceback.print_exc()
            time.sleep(1.0)
    return sent

# 24/7 цикл с корректным завершением
stop_flag = False
def handle_stop(sig, frame):
    global stop_flag
    stop_flag = True
    print(f"[signal] Got {sig}, stopping loop...")

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)

def main_loop():
    print(f"[start] WB monitor (ONLY 'Баллы за отзывы'). Interval={CHECK_INTERVAL}s pages={MAX_PAGES}")
    while not stop_flag:
        n = one_scan()
        print(f"[cycle] Done. Sent: {n}")
        for _ in range(CHECK_INTERVAL):
            if stop_flag: break
            time.sleep(1)
    print("[stop] Exit gracefully")

if __name__ == "__main__":
    main_loop()
