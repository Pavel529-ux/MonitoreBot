import os, re, time, signal, traceback, random
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse
import requests

# ===== Конфиг =====
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")

# ВАЖНО: WB_FEED_URLS разделяй ИЛИ символом |, ИЛИ пробелом/переносом строки (НЕ запятой!)
urls_raw = os.getenv("WB_FEED_URLS", "").strip()
WB_FEED_URLS = [u for u in re.split(r"[|\s]+", urls_raw) if u and u.startswith("http")]

CHECK_INTERVAL     = int(os.getenv("CHECK_INTERVAL", "600"))
MAX_PAGES          = int(os.getenv("MAX_PAGES", "8"))       # стало скромнее по умолчанию
REDIS_URL          = os.getenv("REDIS_URL")

# Прокси только для WB. Telegram идёт без прокси по умолчанию.
PROXY_URL          = os.getenv("PROXY_URL")                 # http://user:pass@host:port ИЛИ socks5h://user:pass@host:port
PROXY_TG_URL       = os.getenv("PROXY_TG_URL")              # опционально — отдельный прокси для Telegram
DEBUG              = os.getenv("DEBUG", "1") == "1"

# Анти-429 настройки (можно править через переменные)
WB_PAGE_DELAY_MIN  = float(os.getenv("WB_PAGE_DELAY_MIN", "0.9"))   # сек между запросами к WB (минимум)
WB_PAGE_DELAY_MAX  = float(os.getenv("WB_PAGE_DELAY_MAX", "1.7"))   # сек (максимум)
WB_HTML_PROBE_LIMIT= int(os.getenv("WB_HTML_PROBE_LIMIT","6"))       # максимум HTML-проверок на ОДНУ страницу каталога
WB_MAX_RETRIES     = int(os.getenv("WB_MAX_RETRIES", "5"))          # ретраи при 429/403/503
WB_BACKOFF_BASE    = float(os.getenv("WB_BACKOFF_BASE", "2.0"))     # экспоненциальная задержка: base * 2^attempt (+джиттер)

if DEBUG:
    print(f"[debug] feeds parsed: {len(WB_FEED_URLS)}")
    for i, u in enumerate(WB_FEED_URLS, 1):
        print(f"[debug] feed[{i}]: {u[:160]}...")

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_FEED_URLS):
    raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_FEED_URLS")

# Заголовки под реальный браузер (RU-локаль)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
    "Connection": "keep-alive",
}

# Плашка вида "80 ₽ за отзыв" (1–6 цифр: 10..100000)
BONUS_RE = re.compile(r'(\d{1,6})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)

def _valid_proxy(url: str) -> bool:
    try:
        u = urlparse(url)
        return u.scheme in ("http","https","socks5","socks5h") and bool(u.netloc) and (":" in u.netloc)
    except Exception:
        return False

def _wb_delay():
    return random.uniform(WB_PAGE_DELAY_MIN, WB_PAGE_DELAY_MAX)

# ===== HTTP-сессии =====
wb_session = requests.Session()
wb_session.headers.update(HEADERS)
if PROXY_URL:
    if _valid_proxy(PROXY_URL):
        wb_session.proxies.update({"http": PROXY_URL, "https": PROXY_URL})
        print("[init] WB proxy enabled")
    else:
        print("[init] WB proxy ignored: invalid PROXY_URL")

tg_session = requests.Session()
tg_session.headers.update({"User-Agent": HEADERS["User-Agent"]})
if PROXY_TG_URL:
    if _valid_proxy(PROXY_TG_URL):
        tg_session.proxies.update({"http": PROXY_TG_URL, "https": PROXY_TG_URL})
        print("[init] TG proxy enabled")
    else:
        print("[init] TG proxy ignored: invalid PROXY_TG_URL")

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

# ===== HTTP JSON c ретраями и экспоненциальным backoff =====
def http_json(url: str):
    attempt = 0
    while True:
        r = wb_session.get(url, timeout=25)
        if DEBUG and (r.status_code != 200 or ("captcha" in r.text.lower() or "access denied" in r.text.lower())):
            print(f"[debug] http {r.status_code} {urlparse(url).netloc} len={len(r.text)}")
            head = r.text[:160].replace("\n"," ")
            if head: print(f"[debug] head sample: {head}")
        if r.status_code in (429, 403, 503):
            if attempt >= WB_MAX_RETRIES:
                r.raise_for_status()
            wait = min(90.0, WB_BACKOFF_BASE * (2 ** attempt)) + random.uniform(0.0, 1.5)
            print(f"[rate] {r.status_code} on {urlparse(url).path} — wait {wait:.1f}s (attempt {attempt+1}/{WB_MAX_RETRIES})")
            time.sleep(wait)
            attempt += 1
            continue
        r.raise_for_status()
        # темпуем даже при успехе
        time.sleep(_wb_delay())
        return r.json()

def iter_products(feed_url: str, max_pages: int, label: str):
    total = 0
    for p in range(1, max_pages + 1):
        url = set_param(feed_url, "page", p)
        data = http_json(url)
        products = (data.get("data") or {}).get("products") or []
        host = urlparse(url).netloc
        if DEBUG: print(f"[debug] {label} page={p} products={len(products)} host={host}")
        if not products:
            break
        total += len(products)
        for item in products:
            yield p, item
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
    for key in ("promoTextCard", "promoTextCat", "description", "extended", "badges"):
        if key in item:
            b = extract_bonus_from_any(item[key])
            if b: return b
    return None

def bonus_from_card_html(nm_id: int) -> int | None:
    url = f"https://www.wildberries.ru/catalog/{nm_id}/detail.aspx"
    try:
        r = wb_session.get(url, timeout=25)
        if r.ok:
            m = BONUS_RE.search(r.text)
            if m: return int(m.group(1))
    except Exception:
        pass
    return None

def send_telegram(text: str):
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True}
    r = None
    try:
        r = tg_session.post(api, json=payload, timeout=20)
        j = r.json()
        if not j.get("ok"):
            print(f"[telegram] not ok: {j}")
    except Exception as e:
        print("[telegram] request failed:", e)
        if r is not None:
            print("[telegram] status/text:", r.status_code, r.text[:200])

def one_scan() -> int:
    sent = 0
    for feed in WB_FEED_URLS:
        # клиент-сайд фильтр: убираем ffeedbackpoints
        feed_nf = del_param(feed, "ffeedbackpoints")
        print("[debug] client-side filter mode")
        try:
            current_page = None
            html_probed_on_page = 0
            for page, item in iter_products(feed_nf, MAX_PAGES, label="scan"):
                if page != current_page:
                    current_page = page
                    html_probed_on_page = 0

                nm = item.get("id") or item.get("nmId") or item.get("nm")
                if not nm:
                    continue
                nm = int(nm)

                bonus = bonus_from_json(item)
                if bonus is None and html_probed_on_page < WB_HTML_PROBE_LIMIT:
                    bonus = bonus_from_card_html(nm)
                    html_probed_on_page += 1
                    time.sleep(_wb_delay())

                if bonus is None:
                    continue

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
                time.sleep(0.35)  # не спамим в TG
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

