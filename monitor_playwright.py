# monitor_playwright.py
import os, re, time, json, random
from urllib.parse import urlparse, parse_qs
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

CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL", "300"))
MAX_SEND_PER_CYCLE = int(os.getenv("MAX_SEND_PER_CYCLE", "5"))

SCROLL_STEPS = int(os.getenv("SCROLL_STEPS", "6"))
DETAIL_CHECK_LIMIT_PER_PAGE = int(os.getenv("DETAIL_CHECK_LIMIT_PER_PAGE", "60"))

BONUS_MIN_PCT = float(os.getenv("BONUS_MIN_PCT", "0.5"))     # 0.5 = 50% цены
BONUS_MIN_RUB = int(os.getenv("BONUS_MIN_RUB", "0") or "0")  # фикс минимум в ₽ (0 = не использовать)

DEBUG = os.getenv("DEBUG", "0") == "1"

PROXY_URL = os.getenv("PROXY_URL", "").strip()  # http://user:pass@host:port
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

SEEN_TTL = 60 * 60 * 24 * 14  # 14 дней

def seen_before(nm: int) -> bool:
    key = f"wb:sent:{nm}"
    try:
        if r:
            if r.get(key):
                return True
            r.setex(key, SEEN_TTL, "1")
            return False
    except Exception:
        pass
    # fallback in-memory per run
    _mem.add(nm)
    return False

_mem = set()

# ========= HELPERS =========
def digits(s: str) -> int:
    m = re.findall(r"\d+", s.replace("\u00a0"," "))
    return int("".join(m)) if m else 0

def product_link(nm: int) -> str:
    return f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

def tg_send(text: str):
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
        print("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": False
    }
    try:
        r = requests.post(api, json=payload, timeout=25)
        if r.status_code != 200 and DEBUG:
            print("[telegram]", r.status_code, r.text[:200])
    except Exception as e:
        print("[telegram] error:", e)

def pass_bonus_rule(bonus: int, price: int) -> bool:
    need_pct = int(price * BONUS_MIN_PCT) if price and price > 0 else 0
    need = max(BONUS_MIN_RUB, need_pct)
    if DEBUG:
        print(f"[rule] price={price} bonus={bonus} need={need} (pct={need_pct}, min_rub={BONUS_MIN_RUB}) -> {bonus >= need}")
    return bonus >= need

def parse_products_from_json_payload(payload) -> list:
    """
    Собираем из различных вариантов WB: либо {"data":{"products":[...]}} либо списки.
    Возвращаем список словарей с ключами nm, price, name (если есть).
    """
    out = []
    try:
        # варианты структур
        candidates = []
        if isinstance(payload, dict):
            if "data" in payload and isinstance(payload["data"], dict):
                if "products" in payload["data"]:
                    candidates = payload["data"]["products"]
                elif isinstance(payload["data"].get("products"), list):
                    candidates = payload["data"]["products"]
            if not candidates and "products" in payload and isinstance(payload["products"], list):
                candidates = payload["products"]
        elif isinstance(payload, list):
            # иногда весь ответ - список
            for el in payload:
                if isinstance(el, dict) and "data" in el:
                    d = el.get("data") or {}
                    if isinstance(d, dict) and "products" in d:
                        candidates.extend(d.get("products") or [])
        for p in candidates:
            nm = p.get("id") or p.get("nm") or p.get("nm_id") or p.get("nmId") or 0
            if not nm:
                continue
            price = 0
            # цена бывает в разных полях (rubPrice, priceU/100 и пр.)
            if isinstance(p.get("priceU"), int):
                price = int(p.get("priceU")) // 100
            elif isinstance(p.get("salePriceU"), int):
                price = int(p.get("salePriceU")) // 100
            elif p.get("price"):
                price = digits(str(p.get("price")))
            name = p.get("name") or p.get("brand") or ""
            out.append({"nm": int(nm), "price": int(price), "name": name})
    except Exception as e:
        if DEBUG: print("[json-parse] err:", e)
    return out

def try_close_popups(page):
    # закрываем возможные попапы куки/региона
    selectors = [
        "button:has-text('Понятно')",
        "button:has-text('Хорошо')",
        "button:has-text('Согласен')",
        "button:has-text('Сохранить')",
        "button:has-text('Да')",
        "button:has-text('Ок')",
        "button:has-text('Принять')"
    ]
    for sel in selectors:
        try:
            el = page.locator(sel)
            if el.first.is_visible(timeout=500):
                el.first.click(timeout=500)
                time.sleep(0.2)
        except Exception:
            pass

def extract_bonus_from_text(text: str) -> int:
    # варианты: "80 ₽ за отзыв", "80Р за отзыв"
    m = re.search(r"(\d{2,6})\s*[₽Р]\s*за\s*отзыв", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    # Иногда пишут "баллы за отзыв" — укажем ₽ в сообщении, но тут всё равно число
    m = re.search(r"балл[а-я]*\s+за\s+отзыв[^0-9]*(\d{2,6})", text, re.IGNORECASE)
    if m:
        return int(m.group(1))
    return 0

def capture_products_on_page(page) -> list:
    """
    Собираем товары из XHR ответов + по плиткам (id).
    """
    captured_json = []
    products = []

    def on_response(res):
        try:
            url = res.url
            ct = res.headers.get("content-type", "")
            if ("application/json" in ct) and ("/catalog" in url or "/search" in url):
                data = res.json()
                captured_json.extend(parse_products_from_json_payload(data))
        except Exception:
            pass

    page.on("response", on_response)
    # ждём базовую загрузку
    try_close_popups(page)
    time.sleep(0.8)

    # прокрутка
    for _ in range(max(1, SCROLL_STEPS)):
        page.evaluate("window.scrollBy(0, document.body.scrollHeight);")
        time.sleep(random.uniform(0.5, 1.1))
        try_close_popups(page)

    # плитки на странице (fallback на случай, если XHR не поймался)
    tiles = []
    try:
        tiles = page.locator("[data-nm-id]").all()
    except Exception:
        tiles = []
    nm_from_tiles = []
    for t in tiles[:500]:
        try:
            nm = int(t.get_attribute("data-nm-id") or "0")
            if nm:
                nm_from_tiles.append(nm)
        except Exception:
            pass

    # склейка
    by_nm = {}
    for p in captured_json:
        by_nm[p["nm"]] = {"nm": p["nm"], "price": p.get("price", 0), "name": p.get("name","")}
    for nm in nm_from_tiles:
        if nm not in by_nm:
            by_nm[nm] = {"nm": nm, "price": 0, "name": ""}

    if DEBUG:
        print(f"[debug] captured={len(captured_json)} tiles_nm={len(nm_from_tiles)} merged={len(by_nm)}")

    return list(by_nm.values())

def probe_detail(context, nm: int) -> dict:
    """
    Открываем карточку и пытаемся вытащить цену и бонус.
    """
    url = product_link(nm)
    page = context.new_page()
    page.set_default_timeout(8000)
    price = 0
    bonus = 0
    name  = ""
    try:
        page.goto(url, wait_until="domcontentloaded")
        try_close_popups(page)
        time.sleep(0.6)

        # имя / заголовок
        try:
            name = page.locator("h1").first.inner_text(timeout=2000).strip()
        except Exception:
            name = ""

        # цена: чаще всего "final-price" или aria-label
        texts = []
        try:
            txt = page.locator('[data-link="text{:product_card_price}"]').first.inner_text(timeout=1500)
            texts.append(txt)
        except Exception:
            pass
        try:
            txt = page.locator(".price-block__final-price").first.inner_text(timeout=1500)
            texts.append(txt)
        except Exception:
            pass
        try:
            txt = page.locator("body").inner_text(timeout=2000)
            texts.append(txt)
        except Exception:
            pass

        for t in texts:
            if not price:
                price = digits(t)

        # бонус по тексту страницы
        bigtxt = ""
        try:
            bigtxt = page.locator("body").inner_text(timeout=2000)
        except Exception:
            pass
        bonus = extract_bonus_from_text(bigtxt)

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

def scan_once() -> int:
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_CATEGORY_URLS):
        print("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID и WB_CATEGORY_URLS")
        return 0

    pct_info = int(BONUS_MIN_PCT * 100)
    rub_info = f", или ≥ {BONUS_MIN_RUB}₽" if BONUS_MIN_RUB > 0 else ""
    if DEBUG:
        print(f"[start] Playwright monitor — categories mode (лимит {MAX_SEND_PER_CYCLE} шт/цикл, пауза {CHECK_INTERVAL//60} минут, бонус ≥ {pct_info}% цены{rub_info})")

    sent = 0

    with sync_playwright() as p:
        launch_kwargs = dict(headless=HEADLESS, args=["--disable-dev-shm-usage"])
        browser = p.chromium.launch(**launch_kwargs)

        context_kwargs = {}
        if PROXY_URL:
            context_kwargs["proxy"] = {"server": PROXY_URL}
        context = browser.new_context(**context_kwargs)

        for url in WB_CATEGORY_URLS:
            if sent >= MAX_SEND_PER_CYCLE:
                break

            page = context.new_page()
            page.set_default_timeout(10000)
            try:
                print("[open]", url)
                page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                if DEBUG: print("[warn] timeout on open")
            except Exception as e:
                if DEBUG: print("[warn] open error:", e)

            try_close_popups(page)

            products_basic = capture_products_on_page(page)

            # ограничим количество детальных проверок на страницу
            to_probe = products_basic[:DETAIL_CHECK_LIMIT_PER_PAGE]

            for pr in to_probe:
                if sent >= MAX_SEND_PER_CYCLE:
                    break

                nm = pr["nm"]
                if nm in _mem or (r and r.get(f"wb:sent:{nm}")):
                    continue

                # если уже есть цена и бонус — ок; иначе идём в деталь
                price = pr.get("price", 0)
                bonus = 0

                # деталь
                detail = probe_detail(context, nm)
                if not price:
                    price = detail.get("price", 0)
                bonus = max(bonus, detail.get("bonus", 0))
                name  = detail.get("name") or pr.get("name") or ""

                if bonus and price and pass_bonus_rule(bonus, price):
                    # антидубль
                    if r:
                        if r.get(f"wb:sent:{nm}"):
                            continue
                        r.setex(f"wb:sent:{nm}", SEEN_TTL, "1")
                    elif nm in _mem:
                        continue
                    else:
                        _mem.add(nm)

                    msg = (
                        f"🍒 <b>Баллы за отзыв</b>\n"
                        f"{name.strip()}\n"
                        f"<b>Цена:</b> {price} ₽\n"
                        f"<b>Бонус:</b> {bonus} ₽\n"
                        f"{product_link(nm)}"
                    )
                    tg_send(msg)
                    sent += 1
                    time.sleep(0.7)  # чуть разгрузим Telegram

            try:
                page.close()
            except Exception:
                pass

        try:
            context.close()
            browser.close()
        except Exception:
            pass

    return sent


if __name__ == "__main__":
    # приветствие один раз при старте (не спамим в цикле)
    pct_info = int(BONUS_MIN_PCT * 100)
    rub_info = f", или ≥ {BONUS_MIN_RUB}₽" if BONUS_MIN_RUB > 0 else ""
    tg_send(f"✅ Монитор запущен (лимит {MAX_SEND_PER_CYCLE}/цикл, пауза {CHECK_INTERVAL//60} мин, бонус ≥ {pct_info}%{rub_info})")

    while True:
        try:
            n = scan_once()
            print(f"[cycle] Done. Sent: {n}")
        except KeyboardInterrupt:
            print("[stop] Exit by user")
            break
        except Exception as e:
            print("[error] cycle:", e)
        # Пауза между циклами
        time.sleep(max(5, CHECK_INTERVAL))

