# -*- coding: utf-8 -*-
import os, re, time, random, json, traceback
from pathlib import Path
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import requests

# ===================== ПАРАМЕТРЫ ПО УМОЛЧАНИЮ =====================
# Лимит отправок за один цикл и пауза между циклами
MAX_SEND_PER_CYCLE          = int(os.getenv("MAX_SEND_PER_CYCLE", "5"))    # <= 5 карточек за цикл
CHECK_INTERVAL              = int(os.getenv("CHECK_INTERVAL", "300"))      # 300 сек = 5 минут
DETAIL_CHECK_LIMIT_PER_PAGE = int(os.getenv("DETAIL_CHECK_LIMIT_PER_PAGE", "30"))
SCROLL_STEPS                = int(os.getenv("SCROLL_STEPS", "8"))
HEADLESS                    = os.getenv("HEADLESS", "0") == "1"            # 0 = видимое окно (удобно для отладки)
DEBUG                       = os.getenv("DEBUG", "1") == "1"

# Бонусный фильтр: бонус ≥ P% от цены (по умолчанию 50%)
BONUS_MIN_PCT = float(os.getenv("BONUS_MIN_PCT", "0.5"))  # 0.5 = 50%

# Требуемые переменные окружения
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
urls_raw = (os.getenv("WB_CATEGORY_URLS") or "").strip()
CATEGORY_URLS = [u for u in re.split(r"[|\s]+", urls_raw) if u.startswith("http")]

if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID):
    raise SystemExit("Нужно задать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
if not CATEGORY_URLS:
    raise SystemExit("Нужно задать WB_CATEGORY_URLS (обычные ссылки разделов WB, через |)")

# (не обязательно) прокси/редис — оставлены на случай, если были настроены
PROXY_URL = os.getenv("PROXY_URL")
REDIS_URL = os.getenv("REDIS_URL")

# ===================== КОНСТАНТЫ/РЕГУЛЯРКИ =====================
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
BONUS_RE = re.compile(r'(\d{1,6})\s*(?:₽|руб\w*|балл\w*)\s+за\s+отзыв', re.I)
PRICE_RE = re.compile(r'(\d{2,6}(?:[ \u00A0]\d{3})*)\s*(?:₽|руб\w*)', re.I)  # числа, рядом с ₽/руб
SS_DIR = Path("screens"); SS_DIR.mkdir(exist_ok=True)

def debug(*args):
    if DEBUG: print(*args)

def parse_amount(s: str) -> int:
    return int(s.replace(" ", "").replace("\u00A0", ""))

def parse_proxy(url: str | None):
    if not url: return None
    u = urlparse(url)
    if not (u.scheme and u.hostname and u.port): return None
    proxy = {"server": f"{u.scheme}://{u.hostname}:{u.port}"}
    if u.username: proxy["username"] = u.username
    if u.password: proxy["password"] = u.password
    return proxy

# ===================== Антидубли =====================
rds = None
if REDIS_URL:
    try:
        import redis
        rds = redis.from_url(REDIS_URL, decode_responses=True, socket_timeout=5)
        rds.ping(); print("[init] Redis OK")
    except Exception as e:
        print("[init] Redis error:", e); rds = None

local_sent = set()
def already_sent(nm: str) -> bool:
    key = f"sent:{nm}"
    if rds:
        try: return rds.exists(key) == 1
        except Exception: pass
    return nm in local_sent
def mark_sent(nm: str):
    key = f"sent:{nm}"
    if rds:
        try: rds.set(key, "1", ex=7*24*3600)
        except Exception: pass
    local_sent.add(nm)

# ===================== Telegram =====================
def send_telegram(text: str):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID, "text": text, "disable_web_page_preview": True},
            timeout=20
        )
        j = r.json()
        if not j.get("ok"):
            print("[telegram] not ok:", j)
    except Exception as e:
        print("[telegram] error:", e)

# ===================== Playwright helpers =====================
def close_popups(page):
    texts = ["Да, верно", "Понятно", "Хорошо", "Ок", "OK", "Согласен", "Не сейчас", "Продолжить", "Закрыть"]
    for _ in range(2):
        clicked = False
        for t in texts:
            for fn in (
                lambda: page.get_by_role("button", name=t).click(timeout=800),
                lambda: page.get_by_text(t).first.click(timeout=800),
            ):
                try:
                    fn(); time.sleep(0.2); clicked = True
                except Exception:
                    pass
        if not clicked: break

def prime_session(page):
    try:
        page.goto("https://www.wildberries.ru/", wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(0.5,1.0)); close_popups(page)
        page.goto("https://www.wildberries.ru/catalog/0/search.aspx?sort=popular",
                  wait_until="domcontentloaded", timeout=45000)
        time.sleep(random.uniform(0.4,0.8)); close_popups(page)
    except PlaywrightTimeoutError:
        pass

def wait_products(page):
    try:
        page.wait_for_selector('[data-nm-id], a[href*="/catalog/"][href*="/detail.aspx"]', timeout=25000)
        return True
    except PlaywrightTimeoutError:
        fname = SS_DIR / f"no_tiles_{int(time.time())}.png"
        try: page.screenshot(path=str(fname), full_page=True); print(f"[debug] saved screenshot: {fname}")
        except Exception: pass
        return False

def cards_with_nm(page):
    """Возвращает список (nm, element_handle) для видимых карточек."""
    out = []
    for el in page.query_selector_all("[data-nm-id]"):
        try:
            nm = el.get_attribute("data-nm-id")
            if nm and nm.isdigit(): out.append((nm, el))
        except Exception:
            pass
    if not out:
        for a in page.query_selector_all("a[href*='/catalog/'][href*='/detail.aspx']"):
            try:
                href = a.get_attribute("href") or ""
                m = re.search(r"/catalog/(\d+)/detail\.aspx", href)
                if m: out.append((m.group(1), a))
            except Exception:
                pass
    return out

# Универсальный сбор products из любых json-ответов WB
def extract_products_from_any(payload) -> list:
    products = []
    def walk(obj):
        if isinstance(obj, dict):
            if "products" in obj and isinstance(obj["products"], list):
                products.extend(obj["products"])
            for v in obj.values(): walk(v)
        elif isinstance(obj, list):
            for x in obj: walk(x)
    walk(payload)
    return products

def pick_price_from_text(text: str) -> int | None:
    # Берём МАКСИМАЛЬНУЮ сумму, помеченную ₽/руб (обычно это текущая цена)
    nums = []
    for m in PRICE_RE.finditer(text):
        try:
            nums.append(parse_amount(m.group(1)))
        except Exception:
            pass
    if not nums: return None
    price = max(nums)
    if price < 50 or price > 200000:
        return None
    return price

def pass_bonus_rule(bonus: int, price: int) -> bool:
    if not price or price <= 0: return False
    need = int(price * BONUS_MIN_PCT)
    return bonus >= need

# ===================== Основная логика =====================
def scan_once():
    sent_this_cycle = 0
    seen_this_run = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            locale="ru-RU",
            user_agent=UA,
            proxy=parse_proxy(PROXY_URL),
        )
        page = context.new_page()
        prime_session(page)
        detail_page = context.new_page()

        for url in CATEGORY_URLS:
            if sent_this_cycle >= MAX_SEND_PER_CYCLE: break
            print("[open]", url)

            captured = []
            def on_response(resp):
                try:
                    u = resp.url; rtype = resp.request.resource_type
                    if ("catalog/" in u or "search" in u) and rtype in ("xhr","fetch"):
                        j = resp.json(); captured.append(j)
                except Exception:
                    pass
            page.on("response", on_response)

            try:
                page.goto(url, wait_until="domcontentloaded", timeout=60000)
                time.sleep(random.uniform(0.8,1.5)); close_popups(page)
                ok = wait_products(page)
                last = 0
                for _ in range(max(1, SCROLL_STEPS)):
                    page.mouse.wheel(0, 5000)
                    time.sleep(random.uniform(1.0,1.6)); close_popups(page)
                    cnt = len(page.query_selector_all("[data-nm-id], a[href*='/catalog/'][href*='/detail.aspx']"))
                    if cnt == last: break
                    last = cnt
            except PlaywrightTimeoutError:
                continue

            # 1) XHR JSON
            total_products = 0; found_json = 0
            for ev in captured:
                products = extract_products_from_any(ev)
                total_products += len(products)
                for it in products:
                    nm = str(it.get("id") or it.get("nmId") or it.get("nm") or "")
                    if not nm or nm in seen_this_run or already_sent(nm): continue

                    # Бонус
                    bonus = None
                    for key in ("promoTextCard","promoTextCat","description","extended","badges"):
                        if key in it:
                            try:
                                m = BONUS_RE.search(json.dumps(it[key], ensure_ascii=False))
                                if m: bonus = int(m.group(1)); break
                            except Exception:
                                pass
                    if not bonus: continue

                    # Цена
                    price_u = it.get("salePriceU") or it.get("priceU") or 0
                    price = int(price_u)//100 if price_u else 0
                    if not pass_bonus_rule(bonus, price):  # фильтр 50%
                        continue

                    name = it.get("name") or "Без названия"
                    link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
                    need = int(price * BONUS_MIN_PCT)

                    send_telegram(f"🎯 Баллы за отзыв\n{name}\nЦена: {price} ₽ | Бонус: {bonus} ₽ (порог {need} ₽)\n{link}")
                    mark_sent(nm); seen_this_run.add(nm)
                    sent_this_cycle += 1; found_json += 1
                    time.sleep(random.uniform(0.2,0.5))
                    if sent_this_cycle >= MAX_SEND_PER_CYCLE: break
                if sent_this_cycle >= MAX_SEND_PER_CYCLE: break

            debug(f"[debug] captured={len(captured)} xhr_products={total_products} json_hits={found_json}")

            # 2) Проверяем видимый текст карточек (если лимит не выбран)
            if sent_this_cycle < MAX_SEND_PER_CYCLE:
                cards = cards_with_nm(page)
                debug(f"[debug] cards_on_page={len(cards)}")
                card_hits = 0
                for nm, el in cards[:DETAIL_CHECK_LIMIT_PER_PAGE]:
                    if sent_this_cycle >= MAX_SEND_PER_CYCLE: break
                    if nm in seen_this_run or already_sent(nm): continue
                    try:
                        txt = el.inner_text(timeout=2000)
                    except Exception:
                        txt = ""
                    m = BONUS_RE.search(txt)
                    if not m: continue
                    bonus = int(m.group(1))
                    price = pick_price_from_text(txt)
                    # если цену на карточке не распознали — проверим детальную
                    if price is None:
                        detail_url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
                        try:
                            detail_page.goto(detail_url, wait_until="domcontentloaded", timeout=45000)
                            time.sleep(random.uniform(0.6,1.1)); close_popups(detail_page)
                            body_text = detail_page.inner_text("body", timeout=4000)
                            price = pick_price_from_text(body_text)
                        except Exception:
                            price = None
                    if price is None or not pass_bonus_rule(bonus, price):
                        continue

                    link = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
                    need = int(price * BONUS_MIN_PCT)
                    send_telegram(f"🎯 Баллы за отзыв (карточка)\nNM {nm}\nЦена: {price} ₽ | Бонус: {bonus} ₽ (порог {need} ₽)\n{link}")
                    mark_sent(nm); seen_this_run.add(nm)
                    sent_this_cycle += 1; card_hits += 1
                    time.sleep(random.uniform(0.2,0.5))
                debug(f"[debug] card_text_hits={card_hits}")

            # 3) (опционально) детальные страницы для оставшихся — уже учли выше при необходимости

        context.close(); browser.close()
    return sent_this_cycle

# ===================== Запуск =====================
if __name__ == "__main__":
    print("[start] Playwright monitor — categories mode (лимит 5 шт/цикл, пауза 5 минут, бонус ≥ 50% цены)")
    send_telegram("✅ Браузерный монитор WB запущен (лимит 5/цикл, 5 мин, бонус ≥ 50% цены)")
    while True:
        try:
            n = scan_once()
            print(f"[cycle] Done. Sent: {n}")
        except Exception as e:
            print("[error]", e)
            traceback.print_exc()
        time.sleep(CHECK_INTERVAL)

