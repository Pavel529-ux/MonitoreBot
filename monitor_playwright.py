import os, re, time, random, json, math
from contextlib import contextmanager
from urllib.parse import urlparse, urlunparse, parse_qs, urlencode

import requests
import redis
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# -------------------- ENV --------------------
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WB_CATEGORY_URLS   = os.getenv("WB_CATEGORY_URLS", "").strip()

# Лимиты и пороги
MAX_SEND_PER_CYCLE          = int(os.getenv("MAX_SEND_PER_CYCLE", "5"))
CHECK_INTERVAL              = int(os.getenv("CHECK_INTERVAL", "300"))  # секунд между циклами
BONUS_MIN_PCT               = float(os.getenv("BONUS_MIN_PCT", "0"))   # 0.5 = 50%
BONUS_MIN_RUB               = int(os.getenv("BONUS_MIN_RUB", "0"))     # абсолютный минимум, руб
DETAIL_CHECK_LIMIT_PER_PAGE = int(os.getenv("DETAIL_CHECK_LIMIT_PER_PAGE", "40"))  # сколько детальных карточек открывать с одной страницы
MAX_PAGES                   = int(os.getenv("MAX_PAGES", "5"))         # страниц каталога на URL

# Поведение Playwright
HEADLESS         = os.getenv("HEADLESS", "1") not in ("0", "false", "False")
SCROLL_STEPS     = int(os.getenv("SCROLL_STEPS", "6"))
WB_MAX_RETRIES   = int(os.getenv("WB_MAX_RETRIES", "2"))
WB_PAGE_DELAY_MIN= float(os.getenv("WB_PAGE_DELAY_MIN", "0.9"))
WB_PAGE_DELAY_MAX= float(os.getenv("WB_PAGE_DELAY_MAX", "1.6"))

# Прокси (HTTP для браузера)
PROXY_URL = (os.getenv("PROXY_URL") or "").strip()  # пример: http://user:pass@host:port

# Redis для дедупликации
REDIS_URL = os.getenv("REDIS_URL", "redis://localhost:6379/0")
rdb = redis.Redis.from_url(REDIS_URL)

DEBUG = os.getenv("DEBUG", "0") in ("1", "true", "True")

def dprint(*a):
    if DEBUG:
        print(*a)

# -------------------- TG helpers --------------------
def tg_send(text, preview=True):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("Нужно задать TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "disable_web_page_preview": not preview,
        "parse_mode": "HTML",
    }
    try:
        requests.post(url, json=payload, timeout=20)
    except Exception as e:
        print("[telegram] error:", e)

def fmt_banner():
    pct = f"{int(BONUS_MIN_PCT*100)}%" if BONUS_MIN_PCT>0 else "0%"
    rub = f"{BONUS_MIN_RUB}₽" if BONUS_MIN_RUB>0 else "0₽"
    return f"✅ Монитор запущен (лимит {MAX_SEND_PER_CYCLE}/цикл, пауза {CHECK_INTERVAL//60} мин, бонус ≥ {pct}, или ≥ {rub})"

def fmt_item(name, price, bonus, url):
    price_s = f"{price:,}".replace(",", " ")
    bonus_s = f"{bonus:,}".replace(",", " ")
    return (f"🍒 <b>Балл(ы) за отзыв</b>\n"
            f"{name}\n"
            f"Цена: <b>{price_s} ₽</b>\n"
            f"Бонус: <b>{bonus_s} ₽</b>\n"
            f"{url}")

# -------------------- utils --------------------
def normalize_url(u: str) -> str:
    """Добавляем ffeedbackpoints=1, если нет; нормализуем параметр page."""
    try:
        pr = urlparse(u)
        q = parse_qs(pr.query)
        if "ffeedbackpoints" not in q:
            q["ffeedbackpoints"] = ["1"]
        if "page" not in q:
            q["page"] = ["1"]
        new_q = urlencode({k:v[0] for k,v in q.items()})
        return urlunparse(pr._replace(query=new_q))
    except:
        return u

def need_send(price: int, bonus: int) -> bool:
    need_rub = max(int(math.ceil(price * BONUS_MIN_PCT)), BONUS_MIN_RUB)
    return bonus >= need_rub

@contextmanager
def browser_ctx(pw):
    args = {}
    if PROXY_URL:
        # playwright ждёт dict proxy={"server":"http://host:port","username":"u","password":"p"}
        # разберём вручную
        try:
            pr = urlparse(PROXY_URL)
            server = f"{pr.scheme}://{pr.hostname}:{pr.port}"
            proxy = {"server": server}
            if pr.username or pr.password:
                if pr.username: proxy["username"] = pr.username
                if pr.password: proxy["password"] = pr.password
            args["proxy"] = proxy
            print(f"[proxy] using {server}")
        except Exception as e:
            print("[proxy] parse error:", e)

    browser = pw.chromium.launch(headless=HEADLESS)
    ctx = browser.new_context(**args)
    try:
        yield ctx
    finally:
        ctx.close()
        browser.close()

def wait_random():
    time.sleep(random.uniform(WB_PAGE_DELAY_MIN, WB_PAGE_DELAY_MAX))

# -------------------- page parsers --------------------
TILE_JS = """
() => {
  const res = [];
  // карточки имеют data-nm-id либо data-popup-nm-id
  const cards = document.querySelectorAll('[data-nm-id], [data-popup-nm-id]');
  for (const c of cards) {
    const nm = c.getAttribute('data-nm-id') || c.getAttribute('data-popup-nm-id');
    if (!nm) continue;

    // ищем любой элемент с текстом «₽ за отзыв»
    let bonusRub = null;
    const walker = document.createTreeWalker(c, NodeFilter.SHOW_TEXT);
    let node;
    while (node = walker.nextNode()) {
      const t = (node.textContent || "").replace(/\\s+/g,' ').trim();
      if (!t) continue;
      // Матчим «500 ₽ за отзыв», «500₽ за отзыв»
      const m = t.match(/(\\d[\\d\\s]{1,6})\\s*₽\\s*за\\s*отзыв/i);
      if (m) {
        bonusRub = parseInt(m[1].replace(/\\s/g,''), 10);
        break;
      }
    }
    res.push({nm, bonusRub});
  }
  return res;
}
"""

def scan_catalog_page(page, url: str):
    """Открывает страницу каталога, собирает nm-ID + бонус на карточках (если есть)."""
    ok = False
    for attempt in range(1, WB_MAX_RETRIES+1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=12000)
            ok = True
            break
        except Exception as e:
            print(f"[warn] open error (attempt {attempt}/{WB_MAX_RETRIES}): {e}")
    if not ok:
        print(f"[warn] skip url after retries: {url}")
        return [], {}

    # плавная прогрузка витрины
    for _ in range(SCROLL_STEPS):
        try:
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            pass
        wait_random()

    # собираем плитки
    tiles = page.evaluate(TILE_JS)
    # tiles: [{nm:"123", bonusRub: 250}, ...]
    nm_list = []
    tile_bonus = {}
    for t in tiles:
        nm = str(t.get("nm") or "").strip()
        if not nm: continue
        nm_list.append(nm)
        br = t.get("bonusRub")
        if isinstance(br, int):
            tile_bonus[nm] = br

    print(f"[debug] tiles_nm={len(nm_list)} with_badge={sum(1 for v in tile_bonus.values() if v is not None)}")
    return nm_list, tile_bonus

DETAIL_JS = """
() => {
  // цена
  function parseIntSafe(s){
    s = (s||'').replace(/[^0-9]/g,'');
    return s ? parseInt(s,10) : null;
  }
  let price = null;
  // WB кладёт цену в нескольких местах, возьмём первое пригодное
  const priceCandidates = [
    '[data-link="text{:product^price}"]',
    'ins[itemprop="price"]',
    '.price-block__final-price',
    '.price__lower-price',
    '.price-block__price'
  ];
  for (const sel of priceCandidates) {
    const el = document.querySelector(sel);
    if (el && el.textContent) {
      const p = parseIntSafe(el.textContent);
      if (p) { price = p; break; }
    }
  }

  // бонус «₽ за отзыв» — ищем по тексту
  let bonus = null;
  const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  let n;
  while (n = walker.nextNode()) {
    const t = (n.textContent||'').replace(/\\s+/g,' ').trim();
    if (!t) continue;
    const m = t.match(/(\\d[\\d\\s]{1,6})\\s*₽\\s*за\\s*отзыв/i);
    if (m) { bonus = parseInt(m[1].replace(/\\s/g,''),10); break; }
  }
  // заголовок
  let name = document.querySelector('h1')?.textContent?.trim() || '';
  return {price, bonus, name};
}
"""

def fetch_detail(page, nm: str):
    url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
    for attempt in range(1, WB_MAX_RETRIES+1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=9000)
            wait_random()
            data = page.evaluate(DETAIL_JS)
            price = int(data.get("price") or 0)
            bonus = int(data.get("bonus") or 0)
            name  = (data.get("name") or "").strip()
            dprint("[detail]", "nm=", nm, "price=", price, "bonus=", bonus, "name=", name[:30])
            return price, bonus, name, url
        except PWTimeout:
            print(f"[detail] timeout nm={nm} (attempt {attempt}/{WB_MAX_RETRIES})")
        except Exception as e:
            print(f"[detail] error: {nm}", e)
    return 0, 0, "", url

# -------------------- main scan --------------------
def scan_once():
    if not (TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID and WB_CATEGORY_URLS):
        print("Нужно задать TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, WB_CATEGORY_URLS")
        return 0

    urls = [normalize_url(u.strip()) for u in WB_CATEGORY_URLS.split("|") if u.strip()]
    total_sent = 0

    with sync_playwright() as pw, browser_ctx(pw) as ctx:
        page = ctx.new_page()

        for base_url in urls:
            # прогон по нескольким страницам
            for page_idx in range(1, MAX_PAGES+1):
                if total_sent >= MAX_SEND_PER_CYCLE:
                    return total_sent

                # заменим параметр page=
                pr = urlparse(base_url)
                q = parse_qs(pr.query)
                q["page"] = [str(page_idx)]
                new_q = urlencode({k:v[0] for k,v in q.items()})
                url = urlunparse(pr._replace(query=new_q))

                print("[open]", url)
                nm_list, tile_bonus = scan_catalog_page(page, url)
                if not nm_list:
                    continue

                # Предфильтр по «бонус на плитке»
                prefiltered = nm_list
                if BONUS_MIN_RUB > 0:
                    prefiltered = [nm for nm in nm_list if tile_bonus.get(nm, 0) >= BONUS_MIN_RUB]
                    dprint(f"[prefilter] by tile >= {BONUS_MIN_RUB}₽ => {len(prefiltered)}")

                # ограничим деталку
                to_check = prefiltered[:DETAIL_CHECK_LIMIT_PER_PAGE] if prefiltered else nm_list[:DETAIL_CHECK_LIMIT_PER_PAGE]

                # открываем детали и отправляем подходящее
                for nm in to_check:
                    if total_sent >= MAX_SEND_PER_CYCLE:
                        break

                    price, bonus, name, detail_url = fetch_detail(page, nm)
                    if price <= 0:
                        continue

                    ok = need_send(price, bonus)
                    print(f"[rule] price={price} bonus={bonus} need={max(int(math.ceil(price*BONUS_MIN_PCT)), BONUS_MIN_RUB)} "
                          f"(pct={int(BONUS_MIN_PCT*100)}, min_rub={BONUS_MIN_RUB}) -> {ok}")

                    if ok:
                        # дедуп по nm+bonus (на случай, если бонус не менялся)
                        key = f"sent:{nm}:{bonus}"
                        if rdb.get(key):
                            continue
                        msg = fmt_item(name, price, bonus, detail_url)
                        tg_send(msg, preview=True)
                        rdb.setex(key, 24*3600, "1")
                        total_sent += 1

                if total_sent >= MAX_SEND_PER_CYCLE:
                    break

    return total_sent

# -------------------- entry --------------------
if __name__ == "__main__":
    print("[init] Redis OK")
    banner = fmt_banner()
    tg_send(banner, preview=False)
    print("[start]", banner.replace("✅ ", ""))

    # один цикл (Railway cron/worker перезапускает согласно CHECK_INTERVAL)
    sent = scan_once()
    print(f"[cycle] Done. Sent: {sent}")
