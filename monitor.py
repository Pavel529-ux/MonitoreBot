import os, re, time, signal, traceback
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests

# ===== Конфиг =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WB_FEED_URLS       = [u.strip() for u in os.getenv("WB_FEED_URLS","").split(",") if u.strip()]
CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "600"))  # сек между циклами
MAX_PAGES          = int(os.getenv("MAX_PAGES", "10"))        # разумная глубина
REDIS_URL          = os.getenv("REDIS_URL")                   # опционально
DEBUG              = os.getenv("DEBUG", "1") == "1"

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_FEED_URLS):
    raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_FEED_URLS")

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"

# Плашка вида "80 ₽ за отзыв" (1–6 цифр: 10..100000)
BONUS_RE = re.compile(r'(\d{1,6})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)

# ===== Redis (анти-дубли) =====
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

local_sent = set()

def already_sent(nm_id: int) -> bool:
    key = f"sent:{nm_id}"
    if rds:
        try:
            return rds.exists(key) == 1
        except Exception:
            pass
    return nm_id in local_sent

def mark_sent(nm_id: int):
    key = f"sent:{nm_id}"
    if rds:
        try:
            rds.set(key, "1", ex=7*24*3600)
        except Exception:
            pass
    local_sent.add(nm_id)

def set_param(url: str, key: str, value) -> str:
    u = urlparse(url); q = parse_qs(u.query); q[key] = [str(value)]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))

def del_param(url: str, key: str) -> str:
    u = urlparse(url); q = parse_qs(u.query)
    if key in q: del q[key]
    return urlunparse((u.scheme, u.netloc, u.path, u.params, urlencode(q, doseq=True), u.fragment))

def http_json(url: str):
    r = requests.get(url, headers={"User-Agent": UA}, timeout=25)
    r.raise_for_status()
    return r.json()

def iter_products(feed_url: str, max_pages: int, label: str):
    total = 0
    for p in range(1, max_pages + 1):
        url = set_param(feed_url, "page", p)
        data = http_json(url)
        products = (data.get("data") or {}).get("products") or []
        host = urlparse(url).netloc
        if DEBUG: print(f"[debug] {label} page={p} products={len(products)} host={host}")
        if not products: break
        total += len(products)
        for item in products:
            yield item
    if DEBUG: print(f"[debug] {label} total={total}")

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

def bonus_from_json(item) -> int | None:
    # пробуем типичные поля, где WB отдаёт текст плашек
    for key in ("promoTextCard", "promoTextCat", "description", "extended", "badges"):
        if key in item:
            b = extract_bonus_from_any(item[key])
            if b: return b
    return None

def bonus_from_card_html(nm_id: int) -> int | None:
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
    try:
        r = requests.post(api, json=payload, timeout=20)
        j = r.json()
        if not j.get("ok"):
            print(f"[telegram] not ok: {j}")
    except Exception as e:
        print("[telegram] request failed:", e)
        print("[telegram] status/text:", getattr(r, "status_code", "?"), getattr(r, "text", "")[:200])

def one_scan() -> int:
    sent = 0
    for feed in WB_FEED_URLS:
        # снимаем серверный фильтр, если он есть — фильтруем сами
        feed_nf = del_param(feed, "ffeedbackpoints")
        if DEBUG and feed != feed_nf:
            print("[debug] removed ffeedbackpoints from feed (client-side filter mode)")
        else:
            print("[debug] scanning feed as-is (client-side filter mode)")

        try:
            for item in iter_products(feed_nf, MAX_PAGES, label="scan"):
                nm = item.get("id") or item.get("nmId") or item.get("nm")
                if not nm:
                    continue
                nm = int(nm)

                # ищем плашку «... за отзыв» (сумма любая)
                bonus = bonus_from_json(item)
                if bonus is None:
                    bonus = bonus_from_card_html(nm)
                    # чуть притормаживаем при HTML-фолбэке
                    time.sleep(0.25)

                if bonus is None:
                    continue  # нет плашки — пропускаем

                if already_sent(nm):
                    if DEBUG: print(f"[debug] skip duplicate nm={nm}")
                    continue

                name = item.get("name") or "Без названия"
                price_u = item.get("salePriceU") or item.get("priceU") or 0
                price = int(price_u) // 100 if price_u else 0
                link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

                msg = f"🎯 Баллы за отзыв\n{name}\nБонус: {bonus} ₽ | Цена: {price} ₽\n{link}"
                send_telegram(msg)
                mark_sent(nm)
                sent += 1
                if DEBUG: print(f"[debug] sent nm={nm}, bonus={bonus}, price={price}")
                time.sleep(0.35)  # бережём API
        except Exception as e:
            print("[warn] feed failed:", e)
            traceback.print_exc()
            time.sleep(1.0)
    return sent

# ===== 24/7 цикл =====
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
