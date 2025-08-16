import os, re, time, signal, traceback
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests

TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WB_FEED_URLS       = [u.strip() for u in os.getenv("WB_FEED_URLS","").split(",") if u.strip()]

CHECK_INTERVAL  = int(os.getenv("CHECK_INTERVAL", "600"))  # сек между циклами
MAX_PAGES       = int(os.getenv("MAX_PAGES", "5"))
REDIS_URL       = os.getenv("REDIS_URL")
DEBUG           = os.getenv("DEBUG", "1") == "1"

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_FEED_URLS):
    raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_FEED_URLS")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# 1–6 цифр: покрываем от 10 до 100000 и т.п.
BONUS_RE = re.compile(r'(\d{1,6})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)

# ===== Redis (для анти-дублей) =====
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

local_seen = set()
def seen_before(nm_id: int) -> bool:
    key = f"seen:{nm_id}"
    if rds:
        try:
            if rds.get(key): 
                if DEBUG: print(f"[debug] skip duplicate nm={nm_id}")
                return True
            rds.set(key, "1", ex=7*24*3600)
            return False
        except Exception:
            pass
    if nm_id in local_seen:
        if DEBUG: print(f"[debug] skip duplicate (local) nm={nm_id}")
        return True
    local_seen.add(nm_id)
    return False

def set_param(url: str, key: str, value) -> str:
    u = urlparse(url); q = parse_qs(u.query); q[key] = [str(value)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))

def fetch_products(feed_url: str, max_pages: int):
    total = 0
    for p in range(1, max_pages + 1):
        url = set_param(feed_url, "page", p)
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        r.raise_for_status()
        data = r.json()
        products = (data.get("data") or {}).get("products") or []
        if DEBUG: print(f"[debug] feed page={p} products={len(products)} url_host={urlparse(url).netloc}")
        if not products: break
        total += len(products)
        for item in products:
            yield item
    if DEBUG and total == 0:
        print("[debug] products total=0 — проверь WB_FEED_URLS и наличие ffeedbackpoints=1")

def extract_bonus_from_any(obj):
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
    url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
        if r.ok:
            m = BONUS_RE.search(r.text)
            if m: return int(m.group(1))
    except Exception:
        pass
    return None

def send_telegram(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    requests.post(api, json=payload, timeout=20)

def one_scan() -> int:
    sent = 0
    for feed in WB_FEED_URLS:
        if DEBUG: print(f"[debug] scan feed: {feed[:120]}...")
        try:
            for item in fetch_products(feed, MAX_PAGES):
                nm = item.get("id") or item.get("nmId") or item.get("nm")
                if not nm:
                    if DEBUG: print("[debug] skip: no nm/id in item")
                    continue
                nm = int(nm)
                if seen_before(nm):
                    continue

                name = item.get("name") or "Без названия"
                price_u = item.get("salePriceU") or item.get("priceU") or 0
                price = int(price_u) // 100 if price_u else 0
                link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

                # Извлекаем конкретный размер бонуса (если WB его отдаёт)
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
                    # даже если число не нашли, фид уже отфильтрован ffeedbackpoints=1 — шлём уведомление
                    msg = f"🎯 Баллы за отзыв\n{name}\nЦена: {price} ₽\n{link}"

                try:
                    send_telegram(msg)
                    sent += 1
                    if DEBUG: print(f"[debug] sent nm={nm}, bonus={bonus}, price={price}")
                    time.sleep(0.4)
                except Exception as e:
                    print("[warn] telegram error:", e)
        except Exception as e:
            print("[warn] feed failed:", e)
            traceback.print_exc()
            time.sleep(1.0)
    return sent

stop_flag = False
def handle_stop(sig, frame):
    global stop_flag
    stop_flag = True
    print(f"[signal] Got {sig}, stopping loop...")

signal.signal(signal.SIGTERM, handle_stop)
signal.signal(signal.SIGINT, handle_stop)

def main_loop():
    print(f"[start] WB monitor (ONLY 'Баллы за отзывы'). Interval={CHECK_INTERVAL}s pages={MAX_PAGES}")
    send_telegram("✅ Монитор запущен и работает")
    while not stop_flag:
        n = one_scan()
        print(f"[cycle] Done. Sent: {n}")
        for _ in range(CHECK_INTERVAL):
            if stop_flag: break
            time.sleep(1)
    print("[stop] Exit gracefully")

if __name__ == "__main__":
    main_loop()
