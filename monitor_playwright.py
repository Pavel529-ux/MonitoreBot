# monitor_playwright.py
import os, re, time, json, random
from urllib.parse import urlparse
import requests

try:
    import redis as redis_lib
except Exception:
    redis_lib = None

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ========= ENV =========
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

WB_CATEGORY_URLS   = [u.strip() for u in (os.getenv("WB_CATEGORY_URLS", "")).split("|") if u.strip()]

HEADLESS = os.getenv("HEADLESS", "1")
HEADLESS = False if HEADLESS in ("0", "false", "False", "no") else True

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))         # сек между циклами
MAX_SEND_PER_CYCLE = int(os.getenv("MAX_SEND_PER_CYCLE", "5"))   # максимум отправок за цикл

SCROLL_STEPS = int(os.getenv("SCROLL_STEPS", "6"))               # сколько «пролистать» страницу
DETAIL_CHECK_LIMIT_PER_PAGE = int(os.getenv("DETAIL_CHECK_LIMIT_PER_PAGE", "60"))

BONUS_MIN_PCT = float(os.getenv("BONUS_MIN_PCT", "0.5"))         # 0.5 = 50%
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", "0") or "0")      # фикс минимум в ₽

DEBUG = os.getenv("DEBUG", "0") == "1"

PROXY_URL = os.getenv("PROXY_URL", "").strip()                   # http://user:pass@host:port  или socks5://...
REDIS_URL = os.getenv("REDIS_URL", "").strip()

# ========= REDIS (anti-dup) =========
r = None
if REDIS_URL and redis_lib:
    try:
        r = redis_lib.Redis.from_url(REDIS_URL, socket_timeout=5, decode_responses=True)
        r.ping()
        if DEBUG: print("[init] Redis OK")
    except Exception as e:
        print("[warn] Redis disabled:", e)
        r = None

_mem = set()
SEEN_TTL = 60*60*24*14  # 14 дней

def already_sent(nm: int) -> bool:
    key = f"wb:sent:{nm}"
    try:
        if r:
            return bool(r.get(key))
    except Exception:
        pass
    return nm in _mem

def mark_sent(nm: int):
    key = f"wb:sent:{nm}"
    try:
        if r:
            r.setex(key, SEEN_TTL, "1")
            return
    except Exception:
        pass
    _mem.add(nm)

# ========= UTILS =========
def product_link(nm: int) -> str:
    return f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

def tg_send(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Нужно задать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML", "disable_web_page_preview": False}
    try:
        r0 = requests.post(api, json=payload, timeout=25)
        if r0.status_code != 200 and DEBUG:
            print("[telegram]", r0.status_code, r0.text[:200])
    except Exception as e:
        print("[telegram] error:", e)

def pass_bonus_rule(bonus: int, price: int) -> bool:
    need_pct = int(price * BONUS_MIN_PCT) if price and price > 0 else 0
    need = max(BONUS_MIN_RUB, need_pct)
    if DEBUG:
        print(f"[rule] price={price} bonus={bonus} need={need} (pct={need_pct}, min_rub={BONUS_MIN_RUB}) -> {bonus >= need}")
    return bonus >= need

def parse_products_from_json_payload(payload) -> list:
    """ Извлекаем товары из разных WB JSON. """
    out = []
    try:
        candidates = []
        if isinstance(payload, dict):
            d = payload.get("data")
            if isinstance(d, dict) and isinstance(d.get("products"), list):
                candidates = d["products"]
            elif isinstance(payload.get("products"), list):
                candidates = payload["products"]
        elif isinstance(payload, list):
            for el in payload:
                if isinstance(el, dict) and isinstance(el.get("data"), dict) and isinstance(el["data"].get("products"), list):
                    candidates.extend(el["data"]["products"])

        for p in candidates:
            nm = p.get("id") or p.get("nm") or p.get("nmId") or p.get("nm_id") or 0
            if not nm:
                continue
            price = 0
            if isinstance(p.get("priceU"), int):
                price = int(p["priceU"]) // 100
            elif isinstance(p.get("salePriceU"), int):
                price = int(p["salePriceU"]) // 100
            elif p.get("price"):
                # тут JSON уже рубли, но всё же аккуратно
                price = extract_price_from_text(str(p["price"]))
            out.append({"nm": int(nm), "price": int(price), "name": p.get("name") or p.get("brand") or ""})
    except Exception as e:
        if DEBUG: print("[json-parse] err:", e)
    return out

def try_close_popups(page):
    selectors = [
        "button:has-text('Понятно')","button:has-text('Хорошо')","button:has-text('Согласен')",
        "button:has-text('Сохранить')","button:has-text('Да')","button:has-text('Ок')","button:has-text('Принять')",
    ]
    for sel in selectors:
        try:
            page.locator(sel).first.click(timeout=400)
            time.sleep(0.1)
        except Exception:
            pass

def open_page_with_retries(context, url: str, max_retries: int = 3):
    """Открыть страницу с ретраями; вернуть page или None."""
    backoff = 3.0
    for i in range(1, max_retries + 1):
        page = context.new_page()
        page.set_default_timeout(35000)
        page.set_default_navigation_timeout(35000)
        try:
            print("[open]", url)
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_load_state("load", timeout=18000)
            except Exception:
                pass
            try_close_popups(page)
            return page
        except PlaywrightTimeoutError:
            if DEBUG: print(f"[warn] timeout on open (attempt {i}/{max_retries})")
        except Exception as e:
            if DEBUG: print(f"[warn] open error (attempt {i}/{max_retries}):", e)
        try:
            page.close()
        except Exception:
            pass
        time.sleep(backoff)
        backoff *= 1.6
    return None

def safe_scroll(page, steps: int):
    """Плавная безопасная прокрутка (без падения на пустом body)."""
    for _ in range(max(1, steps)):
        try:
            page.evaluate(
                """
                () => {
                  const root = document.scrollingElement || document.body || document.documentElement;
                  if (!root) return 0;
                  window.scrollBy(0, root.scrollHeight);
                  return root.scrollHeight || 0;
                }
                """
            )
        except Exception:
            time.sleep(0.6)
        time.sleep(0.6)
        try_close_popups(page)

def capture_products_on_page(page) -> list:
    """ Собираем товары из XHR + data-nm-id на плитках. """
    captured_json = []

    def on_response(res):
        try:
            ct = res.headers.get("content-type", "")
            if "application/json" in ct:
                url = res.url
                if ("/catalog" in url) or ("/search" in url):
                    data = res.json()
                    captured_json.extend(parse_products_from_json_payload(data))
        except Exception:
            pass

    page.on("response", on_response)
    time.sleep(0.8)
    try_close_popups(page)

    safe_scroll(page, SCROLL_STEPS)

    tiles = []
    try:
        tiles = page.locator("[data-nm-id]").all()
    except Exception:
        tiles = []

    nm_from_tiles = []
    for t in tiles[:600]:
        try:
            nm = int(t.get_attribute("data-nm-id") or "0")
            if nm:
                nm_from_tiles.append(nm)
        except Exception:
            pass

    by_nm = {}
    for p in captured_json:
        by_nm[p["nm"]] = {"nm": p["nm"], "price": p.get("price", 0), "name": p.get("name","")}
    for nm in nm_from_tiles:
        if nm not in by_nm:
            by_nm[nm] = {"nm": nm, "price": 0, "name": ""}

    if DEBUG:
        print(f"[debug] captured={len(captured_json)} tiles_nm={len(nm_from_tiles)} merged={len(by_nm)}")

    return list(by_nm.values())

def extract_price_from_text(text: str) -> int:
    """
    Достаём цену только из выражений с ₽ или 'руб'.
    Берём первую правдоподобную (>0 и < 10 млн).
    """
    if not text:
        return 0
    for m in re.finditer(r"(\d[\d\s]{0,8})\s*(?:₽|руб\.?|р\.)", text, flags=re.IGNORECASE):
        num = re.sub(r"\s+", "", m.group(1))
        try:
            val = int(num)
            if 0 < val < 10_000_000:
                return val
        except Exception:
            pass
    return 0

def extract_bonus_from_text(text: str) -> int:
    m = re.search(r"(\d{2,6})\s*[₽Р]\s*за\s*отзыв", text or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    m = re.search(r"балл[а-я]*\s+за\s+отзыв[^0-9]*(\d{2,6})", text or "", re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0

def probe_detail(context, nm: int) -> dict:
    url = product_link(nm)
    page = context.new_page()
    page.set_default_timeout(15000)  # WB может грузиться долго
    price = 0
    bonus = 0
    name  = ""
    try:
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_load_state("load", timeout=12000)
        except Exception:
            pass
        try_close_popups(page)
        time.sleep(0.6)

        # 1) Название
        try:
            name = page.locator("h1").first.inner_text(timeout=2500).strip()
        except Exception:
            name = ""

        # 2) Цена — несколько селекторов + фолбэк body (ищем только ₽/руб)
        texts = []
        for sel in (
            '[data-link="text{:product_card_price}"]',
            ".price-block__final-price",
            ".price-block__price",
            '[data-qa="product-price"]',
        ):
            try:
                txt = page.locator(sel).first.inner_text(timeout=1800)
                if txt:
                    texts.append(txt)
            except Exception:
                pass
        try:
            texts.append(page.locator("body").inner_text(timeout=3000))
        except Exception:
            pass

        for t in texts:
            if not price:
                p = extract_price_from_text(t)
                if p:
                    price = p

        # 3) Бонус — из всего текста страницы (ищем «₽ за отзыв»)
        try:
            bigtxt = page.locator("body").inner_text(timeout=2500)
            bonus = extract_bonus_from_text(bigtxt)
        except Exception:
            pass

        # 4) Фолбэк: aria-label кнопки с ценой
        if not price:
            try:
                aria = page.locator('button[aria-label*="₽"]').first.get_attribute("aria-label")
                price = extract_price_from_text(aria)
            except Exception:
                pass

        if DEBUG:
            print(f"[detail] nm={nm} price={price} bonus={bonus} name={name[:40]}")
    except Exception as e:
        if DEBUG: print("[detail] error:", nm, e)
    finally:
        try:
            page.close()
        except Exception:
            pass
    return {"nm": nm, "price": price, "bonus": bonus, "name": name}

def proxy_selftest(proxy_url: str) -> bool:
    """Проверяем, что через указанный прокси вообще есть интернет."""
    if not proxy_url:
        return True
    try:
        proxies = {"http": proxy_url, "https": proxy_url}
        r0 = requests.get("https://api.ipify.org?format=json", proxies=proxies, timeout=10)
        ok = r0.ok
        ip = ""
        try:
            ip = r0.json().get("ip")
        except Exception:
            pass
        print(f"[proxy] self-test status={r0.status_code} ip={ip}")
        return ok
    except Exception as e:
        print("[proxy] self-test failed:", e)
        return False

def build_pw_proxy(proxy_url: str):
    if not proxy_url:
        return None
    try:
        u = urlparse(proxy_url)
        scheme = (u.scheme or "http").lower()
        # normalize socks5h -> socks5
        if scheme in ("socks5h", "socks5", "socks"):
            scheme = "socks5"
        elif scheme not in ("http", "https"):
            scheme = "http"
        server = f"{scheme}://{u.hostname}:{u.port}"
        out = {"server": server}
        if u.username:
            out["username"] = u.username
        if u.password:
            out["password"] = u.password
        return out
    except Exception:
        return None

def make_context(p, proxy_url: str, headless: bool):
    # реалистичный UA + заголовки + часовой пояс
    UA = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/127.0.0.0 Safari/537.36"
    )
    browser = p.chromium.launch(
        headless=headless,
        args=[
            "--no-sandbox",
            "--disable-gpu",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
        ],
    )
    ctx_kwargs = dict(
        user_agent=UA,
        locale="ru-RU",
        timezone_id="Europe/Moscow",
        viewport={"width": 1280, "height": 900},
        extra_http_headers={"Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7"},
    )
    pw_proxy = build_pw_proxy(proxy_url)
    if pw_proxy:
        ctx_kwargs["proxy"] = pw_proxy
        print("[proxy] using", pw_proxy.get("server"))

    context = browser.new_context(**ctx_kwargs)

    # минимальный «stealth»
    context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        window.chrome = { runtime: {} };
        Object.defineProperty(navigator, 'languages', {get: () => ['ru-RU','ru','en-US','en']});
        Object.defineProperty(navigator, 'platform', {get: () => 'Win32'});
    """)

    # режем тяжёлые ресурсы (ускоряет и уменьшает шум)
    def _route(route):
        r1 = route.request
        if r1.resource_type in ("image", "media", "font"):
            return route.abort()
        return route.continue_()
    context.route("**/*", _route)

    return browser, context

def scan_once() -> int:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_CATEGORY_URLS):
        print("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и WB_CATEGORY_URLS")
        return 0

    if DEBUG:
        pct_info = int(BONUS_MIN_PCT * 100)
        rub_info = f", или ≥ {BONUS_MIN_RUB}₽" if BONUS_MIN_RUB > 0 else ""
        print(f"[start] Playwright monitor — categories mode (лимит {MAX_SEND_PER_CYCLE} шт/цикл, пауза {CHECK_INTERVAL//60} минут, бонус ≥ {pct_info}% цены{rub_info})")

    sent = 0
    with sync_playwright() as p:
        use_proxy = PROXY_URL
        if PROXY_URL and not proxy_selftest(PROXY_URL):
            print("[proxy] disabled for this cycle (self-test failed)")
            use_proxy = ""  # работаем без прокси в этом цикле, чтобы не висеть

        browser, context = make_context(p, use_proxy, HEADLESS)

        for url in WB_CATEGORY_URLS:
            if sent >= MAX_SEND_PER_CYCLE:
                break

            page = open_page_with_retries(context, url, max_retries=3)
            if not page:
                if DEBUG: print("[warn] skip url after retries:", url)
                continue

            products_basic = capture_products_on_page(page)
            to_probe = products_basic[:DETAIL_CHECK_LIMIT_PER_PAGE]

            for pr in to_probe:
                if sent >= MAX_SEND_PER_CYCLE:
                    break
                nm = pr["nm"]
                if already_sent(nm):
                    continue

                price = pr.get("price", 0)
                detail = probe_detail(context, nm)
                if not price:
                    price = detail.get("price", 0)
                bonus = detail.get("bonus", 0)
                name  = detail.get("name") or pr.get("name") or ""

                if bonus and price and pass_bonus_rule(bonus, price):
                    msg = (
                        f"🍒 <b>Баллы за отзыв</b>\n"
                        f"{name.strip()}\n"
                        f"<b>Цена:</b> {price} ₽\n"
                        f"<b>Бонус:</b> {bonus} ₽\n"
                        f"{product_link(nm)}"
                    )
                    tg_send(msg)
                    mark_sent(nm)
                    sent += 1
                    time.sleep(0.7)

            try:
                page.close()
            except Exception:
                pass

        try:
            context.close(); browser.close()
        except Exception:
            pass

    return sent


if __name__ == "__main__":
    pct_info = int(BONUS_MIN_PCT * 100)
    rub_info = f", или ≥ {BONUS_MIN_RUB}₽" if BONUS_MIN_RUB > 0 else ""
    tg_send(f"✅ Монитор запущен (лимит {MAX_SEND_PER_CYCLE}/цикл, пауза {CHECK_INTERVAL//60} мин, бонус ≥ {pct_info}%{rub_info})")

    while True:
        try:
            n = scan_once()
            print(f"[cycle] Done. Sent: {n}")
        except KeyboardInterrupt:
            print("[stop] Exit by user"); break
        except Exception as e:
            print("[error] cycle:", e)
        time.sleep(max(5, CHECK_INTERVAL))
