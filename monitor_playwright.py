# monitor_playwright.py
import os, re, json, time, random, sys
from contextlib import suppress

import redis
import requests

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# =========================
# Env / настройки по умолчанию
# =========================
def envf(name, default):
    v = os.getenv(name)
    return v if v is not None and v != "" else default

TELEGRAM_BOT_TOKEN = envf("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = envf("TELEGRAM_CHAT_ID", "")
REDIS_URL          = envf("REDIS_URL", "redis://localhost:6379/0")

# Категории WB (обычные ссылки, ‘|’ между ссылками)
WB_CATEGORY_URLS   = envf("WB_CATEGORY_URLS", "")
if not WB_CATEGORY_URLS:
    print("Нужно задать WB_CATEGORY_URLS (обычные ссылки разделов WB, через |)")
    sys.exit(1)

HEADLESS           = envf("HEADLESS", "1")
DEBUG              = envf("DEBUG", "0") == "1"

# Порог бонуса: процент от цены и минимум в рублях
BONUS_MIN_PCT      = float(envf("BONUS_MIN_PCT", "0.5"))   # 0.5 → 50%
BONUS_MIN_RUB      = int(envf("BONUS_MIN_RUB", "0"))       # 200, 500 и т.п.

# Сколько отправлять за 1 цикл и как часто повторять (сек)
MAX_SEND_PER_CYCLE = int(envf("MAX_SEND_PER_CYCLE", "5"))
CHECK_INTERVAL     = int(envf("CHECK_INTERVAL", "300"))

# Сколько страниц листинга пытаться открыть (на каждую ссылку в WB_CATEGORY_URLS)
WB_MAX_PAGES       = int(envf("WB_MAX_PAGES", "3"))

# Сколько карточек товара открывать «вглубь» на каждой странице (чтобы добрать точный бонус/цену)
DETAIL_CHECK_LIMIT_PER_PAGE = int(envf("DETAIL_CHECK_LIMIT_PER_PAGE", "10"))

# Навигационные таймауты / повторы
WB_NAV_TIMEOUT     = int(envf("WB_NAV_TIMEOUT", "30000"))  # мс
WB_MAX_RETRIES     = int(envf("WB_MAX_RETRIES", "3"))
WB_BACKOFF_BASE    = float(envf("WB_BACKOFF_BASE", "1.0")) # секунд

# Скролл листинга
SCROLL_STEPS       = int(envf("SCROLL_STEPS", "8"))

# Прокси для Playwright (только HTTP). Пример: http://host:port или http://user:pass@host:port
PROXY_URL          = envf("PROXY_URL", "").strip()

# Прокси для Telegram (можно тот же http://user:pass@host:port)
TG_PROXY           = PROXY_URL

# Селектор карточек на листинге
WB_WAIT_SELECTOR   = envf("WB_WAIT_SELECTOR", "div.product-card")

# =========================
# Вспомогательные функции
# =========================
def wait_random(a=0.2, b=0.6):
    time.sleep(random.uniform(a, b))

def norm_int(txt):
    if txt is None:
        return None
    m = re.findall(r"\d+", str(txt))
    if not m: 
        return None
    return int("".join(m))

def build_need_bonus(price):
    need_pct = int(price * BONUS_MIN_PCT)
    return max(need_pct, BONUS_MIN_RUB)

def tg_session():
    sess = requests.Session()
    if TG_PROXY and TG_PROXY.startswith("http"):
        sess.proxies.update({"http": TG_PROXY, "https": TG_PROXY})
    sess.headers.update({"User-Agent": "WBMonitor/1.0"})
    return sess

def send_telegram(text, parse_mode="HTML", preview=True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID")
        return False
    api = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": not preview
    }
    s = tg_session()
    try:
        r = s.post(api, json=payload, timeout=20)
        if r.status_code != 200:
            print("[telegram] status/text:", r.status_code, r.text[:200])
        return (r.status_code == 200)
    except Exception as e:
        print("[telegram] request failed:", e)
        return False

def redis_client():
    try:
        return redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print("[redis] connect error:", e)
        return None

def already_sent(rds, key):
    if not rds:
        return False
    try:
        return rds.sismember("sent_items", key)
    except Exception:
        return False

def mark_sent(rds, key, ttl_days=7):
    if not rds:
        return
    try:
        rds.sadd("sent_items", key)
        rds.expire("sent_items", ttl_days * 86400)
    except Exception:
        pass

def parse_proxy(url):
    # Возвращает словарь proxy аргумента для playwright
    if not url:
        return None
    if not url.startswith("http"):
        # Playwright умеет аутентификацию только для http/https в proxy
        print("[proxy] only HTTP proxy is supported for Playwright; ignore:", url)
        return None
    # Обрежем креды и хост
    # допускаем http://user:pass@host:port или http://host:port
    user, pwd = None, None
    try:
        # вручную распарсим
        # http://user:pass@host:port
        body = url.split("://", 1)[1]
        if "@" in body:
            creds, host = body.split("@", 1)
            if ":" in creds:
                user, pwd = creds.split(":", 1)
            server = url.split("://")[0] + "://" + host
        else:
            server = url
        proxy = {"server": server}
        if user:
            proxy["username"] = user
            proxy["password"] = pwd or ""
        return proxy
    except Exception:
        return {"server": url}

# =========================
# Playwright helpers
# =========================
def new_browser(pw):
    launch_kwargs = {
        "headless": (HEADLESS != "0"),
        "args": [
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-breakpad",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disk-cache-dir=/tmp/pw-cache",
        ],
    }
    if PROXY_URL:
        proxy = parse_proxy(PROXY_URL)
        if proxy:
            launch_kwargs["proxy"] = proxy
            print(f"[proxy] using {proxy.get('server')}")
    return pw.chromium.launch(**launch_kwargs)

def new_context(pw):
    browser = new_browser(pw)
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36",
        locale="ru-RU",
    )
    # Скрыть webdriver
    ctx.add_init_script("""Object.defineProperty(navigator,'webdriver',{get:()=>undefined});""")

    # Заблокировать тяжёлые ресурсы
    blocked = (
        "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,webm,avi,mp3,woff,woff2,ttf,otf}",
        "*://mc.yandex.*/*",
        "*://www.googletagmanager.com/*",
        "*://www.google-analytics.com/*",
        "*://vk.com/*",
        "*://staticxx.facebook.com/*",
    )
    for pattern in blocked:
        ctx.route(pattern, lambda r: r.abort())

    return browser, ctx

def page_open_with_wait(page, url: str):
    # пробуем domcontentloaded → load → ждём карточки
    for attempt in range(1, WB_MAX_RETRIES + 1):
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=WB_NAV_TIMEOUT)
            except Exception:
                page.goto(url, wait_until="load", timeout=WB_NAV_TIMEOUT)
            page.wait_for_selector(WB_WAIT_SELECTOR, timeout=WB_NAV_TIMEOUT//2, state="visible")
            return True
        except Exception as e:
            print(f"[warn] open error (attempt {attempt}/{WB_MAX_RETRIES}): {e}")
            time.sleep(WB_BACKOFF_BASE * attempt)
    print(f"[warn] skip url after retries: {url}")
    return False

def scan_catalog_page(page, url: str):
    ok = page_open_with_wait(page, url)
    if not ok:
        return [], {}

    # мягко прокрутим страницу
    for _ in range(SCROLL_STEPS):
        with suppress(Exception):
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        wait_random(0.15, 0.35)

    # Соберём nm и предполагаемые бонусы с плашек
    tiles = page.evaluate("""
        () => {
          const out=[];
          document.querySelectorAll('[data-nm-id]').forEach(el=>{
            const nm = el.getAttribute('data-nm-id');
            let bonusRub=null;

            // ищем плашки/текст про бонус
            const txt = el.textContent.toLowerCase().replace(/\\s+/g,' ');
            // примеры: "80 р за отзыв", "250₽ за отзыв", "баллы за отзыв 500 р"
            const m = txt.match(/(\\d{2,6})\\s*(?:₽|р)\\s*за\\s*отзыв/);
            if (m) bonusRub = parseInt(m[1],10);

            out.push({nm, bonusRub});
          });
          return out;
        }
    """)
    nm_list, tile_bonus = [], {}
    for t in tiles:
        nm = str(t.get("nm") or "").strip()
        if not nm:
            continue
        nm_list.append(nm)
        br = t.get("bonusRub")
        if isinstance(br, int):
            tile_bonus[nm] = br

    if DEBUG:
        print(f"[debug] tiles_nm={len(nm_list)} with_badge={sum(1 for v in tile_bonus.values() if v is not None)}")
    return nm_list, tile_bonus

def fetch_detail_bonus_and_price(page, nm: str):
    url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=WB_NAV_TIMEOUT)
    except Exception:
        page.goto(url, wait_until="load", timeout=WB_NAV_TIMEOUT)

    wait_random(0.2, 0.4)
    html = page.content()

    # Цена: возьмём финальную цену (например "1 399 ₽")
    price = None
    with suppress(Exception):
        txt = page.inner_text("body")
        # поищем паттерны вида "Цена: 1 399 ₽" или просто "1399 ₽"
        m = re.search(r"([\d\s]{2,})\s*₽", txt)
        if m:
            price = norm_int(m.group(1))

    # Бонус за отзыв
    bonus = 0
    # ищем “за отзыв”
    with suppress(Exception):
        txt = (txt or page.inner_text("body")).lower()
        m = re.search(r"(\d{2,6})\s*(?:₽|р)\s*за\s*отзыв", txt)
        if m:
            bonus = int(m.group(1))

    # иногда бонус рисуется значком — попробуем ещё
    if bonus == 0:
        with suppress(Exception):
            btn = page.query_selector("text=/за отзыв/i")
            if btn:
                around = btn.text_content()
                m2 = re.search(r"(\d{2,6})", around or "")
                if m2:
                    bonus = int(m2.group(1))

    return price or 0, bonus or 0, url

# =========================
# Основной цикл
# =========================
def main_loop():
    rds = redis_client()

    # человеко-читаемое описание порога
    pct_text = int(BONUS_MIN_PCT*100)
    start_text = f"[start] Монитор запущен (лимит {MAX_SEND_PER_CYCLE}/цикл, пауза {CHECK_INTERVAL//60} мин, бонус ≥ {pct_text}%, или ≥ {BONUS_MIN_RUB}₽)"
    print(start_text)
    send_telegram("✅ " + start_text.replace("[start] ", ""), preview=False)

    urls = [u.strip() for u in WB_CATEGORY_URLS.split("|") if u.strip()]
    # гарантируем фильтр WB (ffeedbackpoints=1) в урле
    def ensure_bonus_filter(u):
        return u if "ffeedbackpoints=1" in u else (u + ("&" if "?" in u else "?") + "ffeedbackpoints=1")
    urls = [ensure_bonus_filter(u) for u in urls]

    sent_this_cycle = 0

    with sync_playwright() as pw:
        browser, ctx = new_context(pw)
        page = ctx.new_page()

        try:
            for base in urls:
                # пробегаем страницы
                for p in range(1, WB_MAX_PAGES+1):
                    if sent_this_cycle >= MAX_SEND_PER_CYCLE:
                        break
                    url = re.sub(r"[?&]page=\d+", "", base)
                    url = url + ("&" if "?" in url else "?") + f"page={p}"
                    # открываем листинг
                    nm_list, tile_bonus = scan_catalog_page(page, url)
                    if not nm_list:
                        continue

                    # те, у кого уже есть плашка, проверим сразу — без detail
                    for nm in nm_list:
                        if sent_this_cycle >= MAX_SEND_PER_CYCLE:
                            break
                        if already_sent(rds, nm):
                            continue

                        bonus_from_tile = tile_bonus.get(nm, None)
                        price, bonus = 0, 0
                        price_url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"

                        need = 0
                        if bonus_from_tile is not None and bonus_from_tile > 0:
                            # Нужно минимум цену; попробуем прикинуть её с листинга (текст)
                            # Если не нашли — сходим в деталь позже
                            with suppress(Exception):
                                # грубая оценка цены: первая ₽ рядом с карточкой
                                price_txt = page.query_selector(f'[data-nm-id="{nm}"]')
                                if price_txt:
                                    m = re.search(r"([\d\s]{2,})\s*₽", price_txt.text_content())
                                    if m:
                                        price = norm_int(m.group(1))
                            bonus = int(bonus_from_tile)

                            if price:
                                need = build_need_bonus(price)
                                if bonus >= need:
                                    text = f"💗 <b>Баллы за отзыв</b>\n" \
                                           f"Бонус: <b>{bonus} ₽</b>\n" \
                                           f"Товар: https://www.wildberries.ru/catalog/{nm}/detail.aspx"
                                    if send_telegram(text, preview=True):
                                        mark_sent(rds, nm)
                                        sent_this_cycle += 1
                                    continue
                            # если цену не смогли оценить — оставим товар на детальную проверку

                    # Детальные проверки (чтоб не «прожигать» трафик — лимит на страницу)
                    detail_checked = 0
                    for nm in nm_list:
                        if sent_this_cycle >= MAX_SEND_PER_CYCLE:
                            break
                        if already_sent(rds, nm):
                            continue
                        if detail_checked >= DETAIL_CHECK_LIMIT_PER_PAGE:
                            break

                        price, bonus, durl = fetch_detail_bonus_and_price(page, nm)
                        if DEBUG:
                            print(f"[detail] nm={nm} price={price} bonus={bonus} url={durl}")
                        need = build_need_bonus(price)
                        ok = bonus >= need

                        print(f"[rule] price={price} bonus={bonus} need={need} (pct={int(BONUS_MIN_PCT*100)}, min_rub={BONUS_MIN_RUB}) -> {ok}")

                        if ok:
                            text = (
                                f"💗 <b>Баллы за отзыв</b>\n"
                                f"Цена: <b>{price} ₽</b>\n"
                                f"Бонус: <b>{bonus} ₽</b>\n"
                                f"{durl}"
                            )
                            if send_telegram(text, preview=True):
                                mark_sent(rds, nm)
                                sent_this_cycle += 1
                        detail_checked += 1

                if sent_this_cycle >= MAX_SEND_PER_CYCLE:
                    break

        finally:
            # Тихое закрытие без шума в логах
            with suppress(Exception): page.close()
            with suppress(Exception): ctx.close()
            with suppress(Exception): browser.close()

    print(f"[cycle] Done. Sent: {sent_this_cycle}")

if __name__ == "__main__":
    rds = redis_client()
    try:
        main_loop()
    except Exception as e:
        print("[error] cycle:", repr(e))
    finally:
        with suppress(Exception):
            if rds: rds.close()

# =========================
# Автообновление категорий WB
# =========================
def fetch_categories():
    import requests
    from bs4 import BeautifulSoup

    url = "https://www.wildberries.ru/"
    response = requests.get(url, timeout=15)
    soup = BeautifulSoup(response.content, "html.parser")
    categories = {}

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith("/catalog/") and len(href.strip("/").split("/")) == 2:
            name = a.get_text(strip=True)
            full_url = f"https://www.wildberries.ru{href}"
            if name and full_url not in categories.values():
                categories[name] = full_url
    return categories
