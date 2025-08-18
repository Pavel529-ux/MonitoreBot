# requirements (pin these in your requirements.txt)
# aiogram==3.7.0
# playwright==1.46.0
# redis==5.0.7
# requests==2.32.3
# (after deploy, remember to run: python -m playwright install chromium)

import os
import re
import json
import time
import random
import asyncio
from dataclasses import dataclass
from typing import List, Dict, Optional, Tuple
from contextlib import suppress

import requests
import redis
from aiogram import Bot, Dispatcher, Router, F, html
from aiogram.filters import CommandStart
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums.parse_mode import ParseMode
from aiogram.client.default import DefaultBotProperties
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.state import StatesGroup, State
from aiogram.fsm.context import FSMContext

from playwright.sync_api import sync_playwright

# =============================================================
# ENV & defaults
# =============================================================

def envf(name: str, default: str) -> str:
    v = os.getenv(name)
    return v if v not in (None, "") else default

TELEGRAM_BOT_TOKEN = envf("TELEGRAM_BOT_TOKEN", "")
REDIS_URL          = envf("REDIS_URL", "")
PROXY_URL          = envf("PROXY_URL", "").strip()
HEADLESS           = envf("HEADLESS", "1")
DEBUG              = envf("DEBUG", "0") == "1"

# Search settings
WB_MAX_PAGES       = int(envf("WB_MAX_PAGES", "3"))
DETAIL_CHECK_LIMIT_PER_PAGE = int(envf("DETAIL_CHECK_LIMIT_PER_PAGE", "6"))
WB_NAV_TIMEOUT     = int(envf("WB_NAV_TIMEOUT", "45000"))  # ms
WB_MAX_RETRIES     = int(envf("WB_MAX_RETRIES", "3"))
WB_BACKOFF_BASE    = float(envf("WB_BACKOFF_BASE", "1.2"))
SCROLL_STEPS       = int(envf("SCROLL_STEPS", "8"))
WB_WAIT_SELECTOR   = envf("WB_WAIT_SELECTOR", "div.product-card")

# Optional admin-provided fallbacks (pipe-separated full WB listing links)
WB_CATEGORY_URLS   = envf("WB_CATEGORY_URLS", "")

# Result size when user asks to show products now
USER_RESULTS_LIMIT = int(envf("USER_RESULTS_LIMIT", "15"))

# =============================================================
# Telegram state & session
# =============================================================

class Flow(StatesGroup):
    CHOOSING_MODE = State()
    CHOOSING_CAT  = State()
    FILTER_PRICE  = State()
    FILTER_KIND   = State()
    FILTER_VALUE  = State()

@dataclass
class Session:
    cat_title: Optional[str] = None
    cat_url: Optional[str] = None
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    filter_kind: Optional[str] = None   # "pct" | "rub"
    filter_value: Optional[int] = None  # numeric value

    def summary(self) -> str:
        parts = [f"Категория: {self.cat_title or 'вся витрина'}"]
        if self.price_min or self.price_max:
            parts.append(f"Цена: {self.price_min or 0}–{self.price_max or '∞'} ₽")
        if self.filter_kind == 'pct' and self.filter_value:
            parts.append(f"Фильтр: ≥ {self.filter_value}% от цены")
        elif self.filter_kind == 'rub' and self.filter_value:
            parts.append(f"Фильтр: ≥ {self.filter_value} ₽ за отзыв")
        else:
            parts.append("Фильтр: не задан (покажем всё с плашкой)")
        return "\n".join(parts)

# In-memory session store (можно заменить на Redis при желании)
SESS: Dict[int, Session] = {}

# =============================================================
# Redis helpers (optional caching + anti-dup for search)
# =============================================================

RDS: Optional[redis.Redis] = None
if REDIS_URL:
    try:
        RDS = redis.from_url(REDIS_URL, decode_responses=True)
    except Exception as e:
        print("[redis] connect error:", e)
        RDS = None

def cache_get(key: str) -> Optional[str]:
    if not RDS:
        return None
    try:
        return RDS.get(key)
    except Exception:
        return None

def cache_setex(key: str, ttl: int, val: str) -> None:
    if not RDS:
        return
    try:
        RDS.setex(key, ttl, val)
    except Exception:
        pass

# =============================================================
# Playwright helpers
# =============================================================

def parse_proxy(url: str) -> Optional[dict]:
    if not url or not url.startswith("http"):
        return None
    user = pwd = None
    try:
        body = url.split("://", 1)[1]
        if "@" in body:
            creds, host = body.split("@", 1)
            if ":" in creds:
                user, pwd = creds.split(":", 1)
            server = url.split("://")[0] + "://" + host
        else:
            server = url
        d = {"server": server}
        if user:
            d["username"], d["password"] = user, (pwd or "")
        return d
    except Exception:
        return {"server": url}

def new_browser(pw):
    kw = {
        "headless": (HEADLESS != "0"),
        "args": [
            "--disable-dev-shm-usage",
            "--no-sandbox",
            "--disable-extensions",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-default-apps",
            "--disable-hang-monitor",
            "--disable-popup-blocking",
            "--disk-cache-dir=/tmp/pw-cache",
        ],
    }
    if PROXY_URL:
        p = parse_proxy(PROXY_URL)
        if p:
            kw["proxy"] = p
    return pw.chromium.launch(**kw)

def new_context(pw):
    browser = new_browser(pw)
    ctx = browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127 Safari/537.36"),
        locale="ru-RU",
    )
    ctx.add_init_script("""Object.defineProperty(navigator,'webdriver',{get:()=>undefined});""")
    # block heavy resources
    blocked = (
        "**/*.{png,jpg,jpeg,gif,webp,svg,mp4,webm,avi,mp3,woff,woff2,ttf,otf}",
        "*://mc.yandex.*/*",
        "*://www.googletagmanager.com/*",
        "*://www.google-analytics.com/*",
        "*://vk.com/*",
        "*://staticxx.facebook.com/*",
    )
    for pat in blocked:
        ctx.route(pat, lambda r: r.abort())
    return browser, ctx

def page_open_with_wait(page, url: str) -> bool:
    for attempt in range(1, WB_MAX_RETRIES + 1):
        try:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=WB_NAV_TIMEOUT)
            except Exception:
                page.goto(url, wait_until="load", timeout=WB_NAV_TIMEOUT)
            page.wait_for_selector(WB_WAIT_SELECTOR, timeout=WB_NAV_TIMEOUT // 2, state="visible")
            return True
        except Exception as e:
            print(f"[open warn] attempt {attempt}/{WB_MAX_RETRIES}: {e}")
            time.sleep(WB_BACKOFF_BASE * attempt)
    print("[open] give up:", url)
    return False

def wait_random(a=0.2, b=0.5):
    time.sleep(random.uniform(a, b))

def norm_int(txt: Optional[str]) -> Optional[int]:
    if txt is None:
        return None
    m = re.findall(r"\d+", str(txt))
    if not m:
        return None
    return int("".join(m))

def scan_catalog_page(page, url: str) -> Tuple[List[str], Dict[str, int], Dict[str, int]]:
    """Return (nm_list, badge_bonus_rub, approx_price_from_tile)"""
    ok = page_open_with_wait(page, url)
    if not ok:
        return [], {}, {}

    for _ in range(SCROLL_STEPS):
        with suppress(Exception):
            page.evaluate("() => window.scrollTo(0, document.body.scrollHeight)")
        wait_random(0.1, 0.25)

    tiles = page.evaluate(
        """
        () => {
          const out=[];
          document.querySelectorAll('[data-nm-id]').forEach(el=>{
            const nm = el.getAttribute('data-nm-id');
            const txt = (el.textContent||'').toLowerCase().replace(/\s+/g,' ');
            let bonusRub=null, priceRub=null;
            const bm = txt.match(/(\d{2,6})\s*(?:₽|р)\s*за\s*отзыв/);
            if (bm) bonusRub = parseInt(bm[1],10);
            const pm = txt.match(/([\d\s]{2,})\s*₽/);
            if (pm) {
               const s = pm[1].replace(/\s+/g, '');
               if (/^\d{2,}$/.test(s)) priceRub = parseInt(s,10);
            }
            out.push({nm, bonusRub, priceRub});
          });
          return out;
        }
        """
    )
    nm_list: List[str] = []
    badge: Dict[str, int] = {}
    price_tile: Dict[str, int] = {}
    for t in tiles:
        nm = str(t.get("nm") or "").strip()
        if not nm:
            continue
        nm_list.append(nm)
        br = t.get("bonusRub")
        pr = t.get("priceRub")
        if isinstance(br, int):
            badge[nm] = br
        if isinstance(pr, int):
            price_tile[nm] = pr

    if DEBUG:
        print(f"[debug] tiles={len(nm_list)} with_badge={len(badge)} with_price={len(price_tile)}")
    return nm_list, badge, price_tile

def fetch_detail_bonus_and_price(page, nm: str) -> Tuple[int, int, str, str]:
    """Return (price, bonus, url, title) from product detail page."""
    url = f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"
    title = ""
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=WB_NAV_TIMEOUT)
    except Exception:
        page.goto(url, wait_until="load", timeout=WB_NAV_TIMEOUT)

    wait_random(0.15, 0.35)
    with suppress(Exception):
        title = (page.title() or "").strip()

    with suppress(Exception):
        txt = page.inner_text("body")
    if not txt:
        txt = ""

    price = 0
    with suppress(Exception):
        m = re.search(r"([\d\s]{2,})\s*₽", txt)
        if m:
            price = norm_int(m.group(1)) or 0

    bonus = 0
    with suppress(Exception):
        m = re.search(r"(\d{2,6})\s*(?:₽|р)\s*за\s*отзыв", txt.lower())
        if m:
            bonus = int(m.group(1))

    if bonus == 0:
        with suppress(Exception):
            btn = page.query_selector("text=/за отзыв/i")
            if btn:
                around = btn.text_content()
                m2 = re.search(r"(\d{2,6})", around or "")
                if m2:
                    bonus = int(m2.group(1))

    return price or 0, bonus or 0, url, title

# =============================================================
# Category discovery
# =============================================================

CANDIDATE_SUBJECT_URLS = [
    "https://static-basket-01.wb.ru/vol0/data/subject-tree/0.json",
    "https://static-basket-01.wb.ru/vol0/data/subject-tree/1.json",
    "https://static-basket-01.wb.ru/vol0/data/subject-v3.json",
]

class CatNode:
    __slots__ = ("title", "url", "children")
    def __init__(self, title: str, url: str, children: Optional[List['CatNode']] = None):
        self.title = title
        self.url = url
        self.children = children or []
    def to_dict(self):
        return {"title": self.title, "url": self.url, "children": [c.to_dict() for c in self.children]}

def _fetch_subject_tree_via_http() -> Optional[List[CatNode]]:
    s = requests.Session()
    s.headers.update({"User-Agent": "WBMonitorBot/1.0"})
    for u in CANDIDATE_SUBJECT_URLS:
        try:
            r = s.get(u, timeout=10)
            if r.status_code == 200 and r.text and r.text.startswith("["):
                data = r.json()
                def to_nodes(items):
                    out = []
                    for it in items:
                        name = it.get("name") or it.get("title") or "Категория"
                        href = it.get("url") or it.get("urlPath") or "https://www.wildberries.ru/"
                        ch = to_nodes(it.get("children", []))
                        out.append(CatNode(name, href, ch))
                    return out
                return to_nodes(data)
        except Exception as e:
            print("[cat http] fail:", u, e)
    return None

def _scrape_top_menu_with_playwright() -> List[CatNode]:
    print("[cat] scrape via Playwright fallback")
    nodes: List[CatNode] = []
    with sync_playwright() as pw:
        browser, ctx = new_context(pw)
        page = ctx.new_page()
        try:
            page.goto("https://www.wildberries.ru/", wait_until="load", timeout=60000)
            with suppress(Exception):
                page.wait_for_timeout(1000)
            data = page.evaluate(
                """
                () => {
                  const pickText = el => (el && el.textContent ? el.textContent.trim() : '');
                  const res = [];
                  document.querySelectorAll('nav a, .menu-categories__item a, .menu__item a').forEach(a=>{
                    const title = pickText(a);
                    const href = a.getAttribute('href') || '';
                    if (title && href && href.startsWith('http')) {
                       res.push({title, url: href});
                    }
                  });
                  return res;
                }
                """
            )
            seen = set()
            for it in data or []:
                t = it.get("title")
                u = it.get("url")
                if not t or not u or (t, u) in seen:
                    continue
                seen.add((t, u))
                nodes.append(CatNode(t, u, []))
        finally:
            with suppress(Exception): page.close()
            with suppress(Exception): ctx.close()
            with suppress(Exception): browser.close()
    return nodes

def get_category_tree() -> List[CatNode]:
    cached = cache_get("wb:cats:v1")
    if cached:
        try:
            raw = json.loads(cached)
            def to_node(d):
                return CatNode(d["title"], d["url"], [to_node(x) for x in d.get("children", [])])
            return [to_node(x) for x in raw]
        except Exception:
            pass

    nodes = _fetch_subject_tree_via_http()
    if not nodes:
        nodes = _scrape_top_menu_with_playwright()
    if not nodes and WB_CATEGORY_URLS:
        tmp = []
        for i, u in enumerate([x.strip() for x in WB_CATEGORY_URLS.split('|') if x.strip()]):
            tmp.append(CatNode(f"Категория {i+1}", u, []))
        nodes = tmp

    if nodes:
        cache_setex("wb:cats:v1", 6*3600, json.dumps([n.to_dict() for n in nodes], ensure_ascii=False))
    return nodes

# =============================================================
# Search core
# =============================================================

def ensure_bonus_filter(url: str) -> str:
    return url if "ffeedbackpoints=1" in url else (url + ("&" if "?" in url else "?") + "ffeedbackpoints=1")

def need_bonus(price: int, filter_kind: Optional[str], filter_value: Optional[int]) -> int:
    if filter_kind == "pct" and filter_value:
        return max(int(price * filter_value / 100), 0)
    if filter_kind == "rub" and filter_value:
        return int(filter_value)
    return 1  # если фильтр не задан — любая плашка

def run_search(cat_url: Optional[str], price_min: Optional[int], price_max: Optional[int],
               filter_kind: Optional[str], filter_value: Optional[int],
               limit: int = 15) -> List[dict]:
    if cat_url:
        urls = [ensure_bonus_filter(cat_url)]
    elif WB_CATEGORY_URLS:
        urls = [ensure_bonus_filter(x.strip()) for x in WB_CATEGORY_URLS.split('|') if x.strip()]
    else:
        urls = [ensure_bonus_filter("https://www.wildberries.ru/catalog/0/search.aspx")]

    results = []
    with sync_playwright() as pw:
        browser, ctx = new_context(pw)
        page = ctx.new_page()
        try:
            for base in urls:
                for p in range(1, WB_MAX_PAGES + 1):
                    if len(results) >= limit:
                        break
                    u = re.sub(r"[?&]page=\d+", "", base)
                    u = u + ("&" if "?" in u else "?") + f"page={p}"
                    nm_list, badges, price_tile = scan_catalog_page(page, u)
                    if not nm_list:
                        continue

                    # Быстрый проход по тем, у кого уже видны плашка и цена
                    for nm in nm_list:
                        if len(results) >= limit:
                            break
                        br = badges.get(nm)
                        pr = price_tile.get(nm)
                        if br and pr:
                            if (price_min and pr < price_min) or (price_max and pr > price_max):
                                continue
                            need = need_bonus(pr, filter_kind, filter_value)
                            if br >= need:
                                results.append({
                                    "nm": nm,
                                    "price": pr,
                                    "bonus": br,
                                    "url": f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
                                    "title": "",
                                })

                    # Детальные проверки (лимитированы)
                    detail_checked = 0
                    for nm in nm_list:
                        if len(results) >= limit or detail_checked >= DETAIL_CHECK_LIMIT_PER_PAGE:
                            break
                        if any(r["nm"] == nm for r in results):
                            continue
                        price, bonus, durl, title = fetch_detail_bonus_and_price(page, nm)
                        detail_checked += 1
                        if not price:
                            continue
                        if (price_min and price < price_min) or (price_max and price > price_max):
                            continue
                        need = need_bonus(price, filter_kind, filter_value)
                        if bonus >= need and bonus > 0:
                            results.append({"nm": nm, "price": price, "bonus": bonus, "url": durl, "title": title})
                if len(results) >= limit:
                    break
        finally:
            with suppress(Exception): page.close()
            with suppress(Exception): ctx.close()
            with suppress(Exception): browser.close()

    def ratio(x):
        pr = x.get("price") or 1
        return (x.get("bonus") or 0) / max(pr, 1)

    if filter_kind == "pct":
        results.sort(key=lambda x: (ratio(x), x.get("bonus", 0)), reverse=True)
    else:
        results.sort(key=lambda x: (x.get("bonus", 0), ratio(x)), reverse=True)

    uniq = {}
    for r in results:
        uniq.setdefault(r["nm"], r)
    return list(uniq.values())[:limit]

# =============================================================
# Telegram UI & handlers
# =============================================================

router = Router()

@router.message(CommandStart())
async def on_start(m: Message, bot: Bot):
    SESS.setdefault(m.from_user.id, Session())
    kb = InlineKeyboardBuilder()
    kb.button(text="Выбрать категорию", callback_data="choose_cat")
    kb.button(text="Пропустить к фильтрам", callback_data="skip_to_filters")
    kb.adjust(1)
    await m.answer(
        "Привет! Я помогу найти самые выгодные товары по акции «Рубли за отзыв».\n\nКак начнём?",
        reply_markup=kb.as_markup(),
    )

def paginate(items: List[Tuple[str, str]], page: int, per_page: int = 10):
    total = len(items)
    start = page * per_page
    end = min(total, start + per_page)
    return items[start:end], total

def build_cat_kb(nodes: List['CatNode'], page: int = 0) -> InlineKeyboardBuilder:
    pairs = [(n.title[:64], n.url) for n in nodes]
    kb = InlineKeyboardBuilder()
    page_items, total = paginate(pairs, page)
    for title, url in page_items:
        kb.button(text=title, callback_data=f"cat:{page}:{url}")
    nav = []
    if page > 0:
        nav.append(("◀️ Назад", f"catpage:{page-1}"))
    if (page + 1) * 10 < total:
        nav.append(("Вперёд ▶️", f"catpage:{page+1}"))
    for t, d in nav:
        kb.button(text=t, callback_data=d)
    kb.button(text="Пропустить к фильтрам", callback_data="skip_to_filters")
    kb.adjust(1)
    return kb

@router.callback_query(F.data == "choose_cat")
async def on_choose_cat(cb: CallbackQuery):
    nodes = await asyncio.to_thread(get_category_tree)
    if not nodes:
        await cb.message.edit_text(
            "Не удалось загрузить категории сейчас. Попробуйте ещё раз или используйте пропуск к фильтрам."
        )
        return
    kb = build_cat_kb(nodes, page=0)
    await cb.message.edit_text("Выберите категорию:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("catpage:"))
async def on_cat_page(cb: CallbackQuery):
    page = int(cb.data.split(":")[1])
    nodes = await asyncio.to_thread(get_category_tree)
    kb = build_cat_kb(nodes, page=page)
    await cb.message.edit_text("Выберите категорию:", reply_markup=kb.as_markup())

@router.callback_query(F.data.startswith("cat:"))
async def on_cat_pick(cb: CallbackQuery):
    _tag, _page_str, url = cb.data.split(":", 2)
    s = SESS.setdefault(cb.from_user.id, Session())
    s.cat_url = url
    s.cat_title = "Выбранная категория"
    kb = InlineKeyboardBuilder()
    kb.button(text="Задать диапазон цены", callback_data="price:set")
    kb.button(text="Пропустить цену", callback_data="price:skip")
    kb.adjust(1)
    await cb.message.edit_text("Категория выбрана. Хотите сузить по цене?", reply_markup=kb.as_markup())

@router.callback_query(F.data == "skip_to_filters")
async def on_skip_to_filters(cb: CallbackQuery):
    s = SESS.setdefault(cb.from_user.id, Session())
    s.cat_url = None
    s.cat_title = None
    kb = InlineKeyboardBuilder()
    kb.button(text="Задать диапазон цены", callback_data="price:set")
    kb.button(text="Пропустить цену", callback_data="price:skip")
    kb.adjust(1)
    await cb.message.edit_text("Хорошо, пропускаем выбор категорий. Зададим цену?", reply_markup=kb.as_markup())

@router.callback_query(F.data == "price:set")
async def on_price_set(cb: CallbackQuery, state: FSMContext):
    await cb.message.edit_text(
        "Отправьте диапазон цены в сообщении в формате:  MIN-MAX  (например 500-2500).\n"
        "Можно одну границу: 0-2000 или 1500-"
    )
    await state.set_state(Flow.FILTER_PRICE)

@router.message(Flow.FILTER_PRICE)
async def on_price_received(m: Message, state: FSMContext):
    s = SESS.setdefault(m.from_user.id, Session())
    txt = (m.text or "").replace(" ", "")
    m1, m2 = None, None
    if "-" in txt:
        a, b = txt.split("-", 1)
        if a:
            m1 = int(re.sub("[^0-9]", "", a) or 0)
        if b:
            nums = re.sub("[^0-9]", "", b)
            m2 = int(nums) if nums else None
    else:
        m2 = int(re.sub("[^0-9]", "", txt) or 0)
    s.price_min, s.price_max = m1, m2

    kb = InlineKeyboardBuilder()
    kb.button(text="Процент от цены", callback_data="fkind:pct")
    kb.button(text="Рубли за отзыв", callback_data="fkind:rub")
    kb.button(text="Пропустить фильтр", callback_data="fkind:skip")
    kb.adjust(1)
    await m.answer("Как фильтровать по бонусу?", reply_markup=kb.as_markup())
    await state.set_state(Flow.FILTER_KIND)

@router.callback_query(F.data == "price:skip")
async def on_price_skip(cb: CallbackQuery, state: FSMContext):
    s = SESS.setdefault(cb.from_user.id, Session())
    s.price_min = s.price_max = None
    kb = InlineKeyboardBuilder()
    kb.button(text="Процент от цены", callback_data="fkind:pct")
    kb.button(text="Рубли за отзыв", callback_data="fkind:rub")
    kb.button(text="Пропустить фильтр", callback_data="fkind:skip")
    kb.adjust(1)
    await cb.message.edit_text("Как фильтровать по бонусу?", reply_markup=kb.as_markup())
    await state.set_state(Flow.FILTER_KIND)

@router.callback_query(F.data.startswith("fkind:"))
async def on_filter_kind(cb: CallbackQuery, state: FSMContext):
    kind = cb.data.split(":", 1)[1]
    s = SESS.setdefault(cb.from_user.id, Session())
    if kind == "skip":
        s.filter_kind, s.filter_value = None, None
        kb = InlineKeyboardBuilder()
        kb.button(text="Показать товары", callback_data="show:now")
        kb.adjust(1)
        await cb.message.edit_text("Отлично! Вот сводка параметров:\n\n" + s.summary(), reply_markup=kb.as_markup())
        await state.clear()
        return
    s.filter_kind = kind
    await cb.message.edit_text(
        "Введите число:\n"
        "• если выбрали процент — например 20 (это ≥20% от цены)\n"
        "• если выбрали рубли — например 500 (это ≥500 ₽ за отзыв)"
    )
    await state.set_state(Flow.FILTER_VALUE)

@router.message(Flow.FILTER_VALUE)
async def on_filter_value(m: Message, state: FSMContext):
    s = SESS.setdefault(m.from_user.id, Session())
    s.filter_value = int(re.sub("[^0-9]", "", m.text or "0") or 0)
    kb = InlineKeyboardBuilder()
    kb.button(text="Показать товары", callback_data="show:now")
    kb.adjust(1)
    await m.answer("Параметры заданы.\n\n" + s.summary(), reply_markup=kb.as_markup())
    await state.clear()

def fmt_item(i: dict) -> str:
    price = i.get("price")
    bonus = i.get("bonus")
    title = i.get("title") or "Товар"
    url = i.get("url") or ""
    ratio = int(100 * (bonus or 0) / max(price or 1, 1))
    return (
        f"<b>{html.quote(title)}</b>\n"
        f"Цена: <b>{price or '?'} ₽</b>\n"
        f"Бонус: <b>{bonus or 0} ₽</b> (≈ {ratio}%)\n"
        f"{html.link('Открыть', url)}"
    )

@router.callback_query(F.data == "show:now")
async def on_show_now(cb: CallbackQuery):
    s = SESS.setdefault(cb.from_user.id, Session())
    await cb.message.edit_text("Ищу подходящие товары… Это может занять 10–25 секунд.")
    try:
        res = await asyncio.to_thread(
            run_search,
            s.cat_url,
            s.price_min,
            s.price_max,
            s.filter_kind,
            s.filter_value,
            USER_RESULTS_LIMIT,
        )
    except Exception as e:
        await cb.message.answer("Не удалось выполнить поиск сейчас. Попробуйте ещё раз.")
        print("[search error]", repr(e))
        return

    if not res:
        await cb.message.answer(
            "Подходящих товаров не нашлось. Попробуйте ослабить фильтры или выбрать другую категорию."
        )
        return

    chunk, total_sent = [], 0
    for i, item in enumerate(res, 1):
        chunk.append(fmt_item(item))
        if len(chunk) == 5 or i == len(res):
            await cb.message.answer("\n\n".join(chunk), parse_mode=ParseMode.HTML, disable_web_page_preview=False)
            total_sent += len(chunk)
            chunk = []
    await cb.message.answer(f"Готово! Показано товаров: {total_sent}.")

# =============================================================
# App bootstrap
# =============================================================

def main():
    if not TELEGRAM_BOT_TOKEN:
        raise SystemExit("Set TELEGRAM_BOT_TOKEN env var")

    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    bot = Bot(TELEGRAM_BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    print("WB Deals Bot started")
    asyncio.run(dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types()))

if __name__ == "__main__":
    main()

