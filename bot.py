# -*- coding: utf-8 -*-
"""
КОТЛЕТЫ И СТЕЙК 89 — бот для мессенджера MAX
Long polling на aiohttp, база SQLite (aiosqlite).
Переменные окружения: MAX_BOT_TOKEN (или MAX_TOKEN) — токен бота.
"""
import asyncio
import logging
import os
import ssl
import sqlite3
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from decimal import Decimal, InvalidOperation

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kotleta89")

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN") or os.getenv("MAX_TOKEN") or "ВСТАВЬ_ТОКЕН"
API_BASE = "https://platform-api2.max.ru"
MANAGER_USERNAME = "https://max.ru/u/f9LHodD0cOIIewxP4aChMb6ZLuuMWIITz-zSiBLo97JIL3CrV_li1dpy81g"
MANAGER_CHAT_ID = 313053726
ADMIN_IDS = {313053726}

OFFER_PDF_URL = "https://disk.yandex.ru/i/YWhP_Tsg3c2Mxw"

DISCOUNT_EVERY_N_ORDERS = 5
DISCOUNT_PERCENT = Decimal("50")
MIN_ORDER_AMOUNT = Decimal("900")
FREE_DELIVERY_FROM = Decimal("1000")
DELIVERY_COST = Decimal("200")

DB_PATH = os.getenv("DB_PATH", "data/shop.db")

TZ_YANAO = timezone(timedelta(hours=5))  # ЯНАО = UTC+5

STATUS_LABELS = {
    "new": "🆕 Новый",
    "cooking": "👨‍🍳 Готовится",
    "delivering": "🛵 В доставке",
    "done": "✅ Доставлен",
    "cancelled": "❌ Отменён",
}

GARNISH_CATEGORIES = {"hot_dishes", "schnitzels", "steaks"}
GARNISH_LABELS = {"fries": "🍟 Картофель фри", "rice": "🍚 Отварной рис"}

# ================= СОСТОЯНИЯ (FSM в памяти) =================
user_states = {}

def st_get(user_id):
    return user_states.get(user_id, {"state": None, "data": {}})

def st_set(user_id, state, **data):
    user_states[user_id] = {"state": state, "data": data}

def st_data(user_id):
    return st_get(user_id).get("data", {})

def st_clear(user_id):
    user_states.pop(user_id, None)

# ================= БАЗА ДАННЫХ =================
async def init_db():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                name TEXT, phone TEXT, address TEXT,
                agreed_offer INTEGER DEFAULT 0,
                order_count INTEGER DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                key TEXT PRIMARY KEY, name TEXT NOT NULL,
                display_name TEXT, sort_order INTEGER DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS menu (
                key TEXT PRIMARY KEY, name TEXT NOT NULL,
                description TEXT DEFAULT '', price TEXT NOT NULL,
                photo_token TEXT, category_key TEXT DEFAULT 'default',
                active INTEGER DEFAULT 1, stock INTEGER
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER, item_key TEXT, quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, item_key)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS cart_garnish (
                user_id INTEGER, item_key TEXT, garnish TEXT,
                PRIMARY KEY (user_id, item_key)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                username TEXT, name TEXT, phone TEXT, address TEXT,
                comment TEXT, delivery_time TEXT, items TEXT, total TEXT,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS order_items (
                order_id INTEGER, item_key TEXT, garnish TEXT,
                quantity INTEGER, price TEXT,
                PRIMARY KEY (order_id, item_key, garnish)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS favorites (
                user_id INTEGER, item_key TEXT,
                PRIMARY KEY (user_id, item_key)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY, value TEXT
            )""")

        # Миграции (добавляем недостающие колонки в старые базы)
        async with db.execute("PRAGMA table_info(categories)") as cur:
            cols = [r[1] for r in await cur.fetchall()]
        if "display_name" not in cols:
            await db.execute("ALTER TABLE categories ADD COLUMN display_name TEXT")
            await db.execute("UPDATE categories SET display_name = name")

        async with db.execute("PRAGMA table_info(users)") as cur:
            ucols = [r[1] for r in await cur.fetchall()]
        if "order_count" not in ucols:
            await db.execute("ALTER TABLE users ADD COLUMN order_count INTEGER DEFAULT 0")

        async with db.execute("PRAGMA table_info(menu)") as cur:
            mcols = [r[1] for r in await cur.fetchall()]
        if "stock" not in mcols:
            await db.execute("ALTER TABLE menu ADD COLUMN stock INTEGER")

        async with db.execute("PRAGMA table_info(orders)") as cur:
            ocols = [r[1] for r in await cur.fetchall()]
        for col, typ in [("comment", "TEXT DEFAULT ''"),
                         ("status", "TEXT DEFAULT 'new'"),
                         ("created_at", "TIMESTAMP"),
                         ("username", "TEXT"),
                         ("delivery_time", "TEXT")]:
            if col not in ocols:
                await db.execute(f"ALTER TABLE orders ADD COLUMN {col} {typ}")

        await db.execute("""
            INSERT OR IGNORE INTO categories (key, name, display_name, sort_order)
            VALUES ('default', 'Меню', 'Меню', 0)
        """)
        await db.commit()

# ================= ПОЛЬЗОВАТЕЛИ И НАСТРОЙКИ =================
async def get_user(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def save_user(user_id, name, phone, address):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO users (user_id, name, phone, address, agreed_offer)
            VALUES (?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                name=excluded.name, phone=excluded.phone, address=excluded.address
        """, (user_id, name, phone, address))
        await db.commit()

async def set_agreed(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute("UPDATE users SET agreed_offer = 1 WHERE user_id = ?", (user_id,))
        await db.commit()

async def get_order_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT order_count FROM users WHERE user_id = ?",
                              (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0

async def increment_order_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO users (user_id) VALUES (?)", (user_id,))
        await db.execute(
            "UPDATE users SET order_count = COALESCE(order_count, 0) + 1 WHERE user_id = ?",
            (user_id,))
        await db.commit()
        async with db.execute("SELECT order_count FROM users WHERE user_id = ?",
                              (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0

async def decrement_order_count(user_id: int) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET order_count = MAX(COALESCE(order_count, 0) - 1, 0) "
            "WHERE user_id = ?", (user_id,))
        await db.commit()
        async with db.execute("SELECT order_count FROM users WHERE user_id = ?",
                              (user_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row and row[0] is not None else 0

async def get_setting(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT value FROM settings WHERE key = ?", (key,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (key, value))
        await db.commit()

# ================= КАТЕГОРИИ И МЕНЮ =================
async def categories_all():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories ORDER BY sort_order, name") as cur:
            return await cur.fetchall()

async def categories_visible():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM categories WHERE key != 'default' ORDER BY sort_order, name"
        ) as cur:
            return await cur.fetchall()

async def category_add(key, name, display_name=None, sort_order=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO categories (key, name, display_name, sort_order) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                display_name = COALESCE(excluded.display_name, categories.display_name)
        """, (key, name, display_name, sort_order))
        await db.commit()

async def category_delete(key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE menu SET category_key = 'default' WHERE category_key = ?",
                         (key,))
        await db.execute("DELETE FROM categories WHERE key = ?", (key,))
        await db.commit()

async def menu_all(active_only=True, category_key=None, in_stock_only=False):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query, params = "SELECT * FROM menu WHERE 1=1", []
        if active_only:
            query += " AND active = 1"
        if category_key:
            query += " AND category_key = ?"
            params.append(category_key)
        if in_stock_only:
            query += " AND (stock IS NULL OR stock > 0)"
        query += " ORDER BY name"
        async with db.execute(query, params) as cur:
            return await cur.fetchall()

async def menu_search(query_str: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        like = f"%{query_str}%"
        async with db.execute(
            "SELECT * FROM menu WHERE active = 1 AND (stock IS NULL OR stock > 0) "
            "AND (name LIKE ? OR description LIKE ?) ORDER BY name LIMIT 10",
            (like, like)) as cur:
            return await cur.fetchall()

async def menu_get(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM menu WHERE key = ?", (key,)) as cur:
            return await cur.fetchone()

async def menu_add(key, name, description, price, category_key="default", photo_token=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO menu (key, name, description, price, category_key, photo_token) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, name, description, str(price), category_key, photo_token))
        await db.commit()

async def menu_update(key, field, value):
    allowed = {"name", "description", "price", "photo_token", "active",
               "category_key", "stock"}
    if field not in allowed:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE menu SET {field} = ? WHERE key = ?", (value, key))
        await db.commit()

async def menu_delete(key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM menu WHERE key = ?", (key,))
        await db.commit()

# ================= КОРЗИНА И ГАРНИРЫ =================
def parse_cart_key(cart_key: str):
    item_key, _, garnish = cart_key.partition("#")
    return item_key, (garnish or None)

async def cart_add(user_id, item_key, garnish=None):
    cart_key = f"{item_key}#{garnish}" if garnish else item_key
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO carts (user_id, item_key, quantity) VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_key) DO UPDATE SET quantity = quantity + 1
        """, (user_id, cart_key))
        await db.commit()

async def cart_remove_one(user_id, item_key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE carts SET quantity = quantity - 1 WHERE user_id = ? AND item_key = ?",
            (user_id, item_key))
        await db.execute(
            "DELETE FROM carts WHERE user_id = ? AND item_key = ? AND quantity <= 0",
            (user_id, item_key))
        await db.commit()

async def cart_delete_item(user_id, item_key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM carts WHERE user_id = ? AND item_key = ?",
                         (user_id, item_key))
        await db.commit()

async def cart_get(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT item_key, quantity FROM carts WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchall()

async def cart_clear(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM carts WHERE user_id = ?", (user_id,))
        await db.commit()

async def set_garnish(user_id, item_key, garnish):
    cart_key = f"{item_key}#{garnish}"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR REPLACE INTO cart_garnish (user_id, item_key, garnish) "
            "VALUES (?, ?, ?)", (user_id, cart_key, garnish))
        await db.commit()

async def get_all_garnishes(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT item_key, garnish FROM cart_garnish WHERE user_id = ?",
            (user_id,)) as cur:
            return {k: g for k, g in await cur.fetchall()}

async def clear_garnishes(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM cart_garnish WHERE user_id = ?", (user_id,))
        await db.commit()

# ================= ИЗБРАННОЕ =================
async def favorites_add(user_id, item_key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO favorites (user_id, item_key) VALUES (?, ?)",
                         (user_id, item_key))
        await db.commit()

async def favorites_remove(user_id, item_key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM favorites WHERE user_id = ? AND item_key = ?",
                         (user_id, item_key))
        await db.commit()

async def favorites_all(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT item_key FROM favorites WHERE user_id = ?",
                              (user_id,)) as cur:
            return [r[0] for r in await cur.fetchall()]

async def is_favorite(user_id, item_key) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM favorites WHERE user_id = ? AND item_key = ?",
                              (user_id, item_key)) as cur:
            return await cur.fetchone() is not None

# ================= ЗАКАЗЫ И СТАТИСТИКА =================
async def order_save(user_id, username, data, items_text, total, delivery_time=None):
    now = datetime.now(TZ_YANAO).strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders (user_id, username, name, phone, address, comment,
                                delivery_time, items, total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, username, data.get("name"), data.get("phone"),
              data.get("address"), data.get("comment", "—"),
              delivery_time or "—", items_text, str(total), now))
        await db.commit()
        return cur.lastrowid

async def order_items_save(order_id, rows, garnishes: dict):
    saved = []
    for cart_key, qty in rows:
        item_key, garnish = parse_cart_key(cart_key)
        item = await menu_get(item_key)
        if not item:
            continue
        g = garnish or garnishes.get(cart_key)
        saved.append((item_key, g or "", qty, str(item["price"])))
    async with aiosqlite.connect(DB_PATH) as db:
        for item_key, g, qty, price in saved:
            await db.execute("""
                INSERT OR REPLACE INTO order_items (order_id, item_key, garnish,
                                                    quantity, price)
                VALUES (?, ?, ?, ?, ?)
            """, (order_id, item_key, g, qty, price))
        await db.commit()

async def order_items_get(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM order_items WHERE order_id = ?",
                              (order_id,)) as cur:
            return await cur.fetchall()

async def order_get(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE id = ?", (order_id,)) as cur:
            return await cur.fetchone()

async def order_set_status(order_id, status):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET status = ? WHERE id = ?", (status, order_id))
        await db.commit()

async def orders_by_status(status, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE status = ? ORDER BY id DESC LIMIT ?",
            (status, limit)) as cur:
            return await cur.fetchall()

async def orders_of_user(user_id, limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM orders WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit)) as cur:
            return await cur.fetchall()

async def orders_items_of_user(user_id, order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT oi.* FROM order_items oi JOIN orders o ON o.id = oi.order_id "
            "WHERE o.user_id = ? AND oi.order_id = ?",
            (user_id, order_id)) as cur:
            return await cur.fetchall()

async def sales_top(limit=10):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("""
            SELECT oi.item_key, SUM(oi.quantity) AS total_qty,
                   COUNT(DISTINCT oi.order_id) AS orders_cnt
            FROM order_items oi
            JOIN orders o ON o.id = oi.order_id
            WHERE o.status != 'cancelled'
            GROUP BY oi.item_key
            ORDER BY total_qty DESC
            LIMIT ?
        """, (limit,)) as cur:
            return await cur.fetchall()

async def stats_summary():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(CAST(total AS REAL)), 0) AS revenue "
            "FROM orders WHERE status != 'cancelled'") as cur:
            all_time = await cur.fetchone()
        async with db.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(CAST(total AS REAL)), 0) AS revenue "
            "FROM orders WHERE status != 'cancelled' "
            "AND DATE(created_at) = DATE('now')") as cur:
            today = await cur.fetchone()
        return all_time, today

# ================= MAX API КЛИЕНТ =================
class MaxApi:
    def __init__(self, token: str):
        self.token = token
        self.session = None
        self.send_locks = {}

    async def start(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.info("SSL: строгая проверка сертификата отключена")
        self.session = aiohttp.ClientSession(
            headers={"Authorization": self.token},
            timeout=aiohttp.ClientTimeout(total=120),
            connector=aiohttp.TCPConnector(ssl=ctx))

    async def stop(self):
        if self.session:
            await self.session.close()

    async def _request(self, method, path, params=None, json_body=None, retries=3):
        url = f"{API_BASE}{path}"
        for attempt in range(retries):
            try:
                async with self.session.request(method, url, params=params,
                                                json=json_body) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2 * (attempt + 1))
                        continue
                    data = None
                    try:
                        data = await resp.json(content_type=None)
                    except Exception:
                        data = None
                    if resp.status >= 400:
                        code = (data or {}).get("code", "")
                        message = (data or {}).get("message", "")
                        if code in ("attachment.not.ready", "upload.error"):
                            await asyncio.sleep(2)
                            continue
                        log.error("API %s %s -> %s %s: %s", method, path,
                                  resp.status, code, message)
                        return None
                    return data
            except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                log.warning("Сеть при %s %s (попытка %s): %s", method, path,
                            attempt + 1, e)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def get_updates(self, marker=None, timeout=30):
        params = {"timeout": timeout,
                  "types": "message_created,message_callback,bot_started"}
        if marker is not None:
            params["marker"] = marker
        return await self._request("GET", "/updates", params=params)

    async def answer_callback(self, callback_id, notification=None):
        body = {}
        if notification:
            body = {"notification": notification}
        await self._request("POST", "/answers", params={"callback_id": callback_id},
                            json_body=body)

    async def _lock(self, chat_id):
        if chat_id not in self.send_locks:
            self.send_locks[chat_id] = asyncio.Lock()
        return self.send_locks[chat_id]

    async def send(self, chat_id, text, attachments=None):
        body = {"text": text or " ", "format": "markdown"}
        if attachments:
            body["attachments"] = attachments
        lock = await self._lock(chat_id)
        async with lock:
            data = await self._request("POST", "/messages", params={"chat_id": chat_id},
                                       json_body=body)
        if data is None:
            log.error("Не удалось отправить сообщение в чат %s", chat_id)
        return data

    async def send_to_user(self, user_id, text, attachments=None):
        return await self.send(user_id, text, attachments)

    async def edit(self, chat_id, message_id, text, attachments=None):
        body = {"text": text or " ", "format": "markdown"}
        if attachments is not None:
            body["attachments"] = attachments
        data = await self._request("PUT", f"/messages/{message_id}",
                                   params={"chat_id": chat_id}, json_body=body)
        return data is not None

    async def get_upload_url(self, token_type: str):
        return await self._request("GET", "/uploads",
                                   params={"type": token_type})

    async def upload_bytes(self, data: bytes, token_type: str = "image"):
        info = await self.get_upload_url(token_type)
        if not info or not info.get("url"):
            return None
        try:
            async with self.session.post(info["url"], data=data,
                                         ssl=False) as resp:
                if resp.status >= 400:
                    return None
                res = await resp.json(content_type=None)
                token = (res or {}).get("token")
                return token
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Загрузка файла не удалась: %s", e)
            return None

api = MaxApi(BOT_TOKEN)

def kb(buttons):
    return {"type": "inline_keyboard",
            "payload": {"buttons": buttons}}

def cb_btn(text, payload):
    return {"type": "callback", "text": text, "payload": payload}

def link_btn(text, url):
    return {"type": "link", "text": text, "url": url}

def photo_attachment(token):
    return {"type": "image", "payload": {"token": token}}
# ================= КЛАВИАТУРЫ =================
def categories_keyboard(categories):
    buttons, row = [], []
    for c in categories:
        label = c["display_name"] if c["display_name"] else c["name"]
        row.append(cb_btn(label, f"cat:{c['key']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([cb_btn("🛒 Корзина", "cart")])
    buttons.append([cb_btn("❤️ Избранное", "favs"),
                    cb_btn("📜 История", "history")])
    buttons.append([cb_btn("🔍 Поиск по меню", "search_hint")])
    return kb(buttons)

def item_label(item):
    sold_out = (item["stock"] is not None and item["stock"] <= 0)
    if sold_out:
        return f"🚫 {item['name']} — нет в наличии"
    return f"{item['name']} — {item['price']} ₽"

def menu_keyboard(items, cat_key):
    buttons = [[cb_btn(item_label(i), f"view:{i['key']}")] for i in items]
    buttons.append([cb_btn("⬅️ Назад", "back_categories")])
    buttons.append([cb_btn("🗑 Очистить корзину", "main_menu")])
    buttons.append([cb_btn("🛒 Корзина", "cart")])
    return kb(buttons)

async def item_card_keyboard(user_id, item_key, cat_key):
    fav = await is_favorite(user_id, item_key)
    fav_label = "❤️ Убрать из избранного" if fav else "🤍 В избранное"
    return kb([
        [cb_btn("🛒 Добавить в корзину", f"add:{item_key}")],
        [cb_btn(fav_label, f"fav:{item_key}")],
        [cb_btn("⬅️ Назад", f"cat:{cat_key}")],
        [cb_btn("🗑 Очистить корзину", "main_menu")],
        [cb_btn("🛒 Корзина", "cart")],
    ])

def garnish_keyboard(item_key):
    return kb([
        [cb_btn("🍟 Картофель фри", f"garnish:{item_key}:fries")],
        [cb_btn("🍚 Отварной рис", f"garnish:{item_key}:rice")],
        [cb_btn("⬅️ Назад", f"cat_back:{item_key}")],
        [cb_btn("🗑 Очистить корзину", "main_menu")],
        [cb_btn("🛒 Корзина", "cart")],
    ])

def manager_keyboard(order_id, status):
    transitions = {
        "new": [("👨‍🍳 Готовится", "cooking"), ("❌ Отменить", "cancelled")],
        "cooking": [("🛵 В доставке", "delivering"), ("❌ Отменить", "cancelled")],
        "delivering": [("✅ Доставлен", "done"), ("❌ Отменить", "cancelled")],
        "done": [], "cancelled": [],
    }
    buttons = [[cb_btn(label, f"setstatus:{order_id}:{next_status}")]
               for label, next_status in transitions.get(status, [])]
    return kb(buttons)

def manager_panel_keyboard():
    return kb([
        [cb_btn("🆕 Новые заказы", "mgr:new")],
        [cb_btn("👨‍🍳 Готовятся", "mgr:cooking")],
        [cb_btn("🛵 В доставке", "mgr:delivering")],
        [cb_btn("✅ Доставленные", "mgr:done")],
    ])

def admin_panel_keyboard():
    return kb([
        [cb_btn("➕ Добавить блюдо", "admin:add"),
         cb_btn("📋 Все блюда", "admin:list")],
        [cb_btn("🗂 Категории", "admin:cats"),
         cb_btn("🗑 Удалить категорию", "admin:cat_del")],
        [cb_btn("📦 Остатки / стоп-лист", "admin:stock")],
        [cb_btn("📈 Топ продаж", "admin:stats")],
        [cb_btn("🖼 Загрузить QR-код", "admin:qr"),
         cb_btn("📊 Загрузить меню списком", "admin:excel")],
        [cb_btn("💾 Резервная копия БД", "admin:backup")],
    ])

def admin_item_keyboard(key, active):
    toggle_label = "🙈 Скрыть" if active else "👁 Показать"
    toggle_action = "hide" if active else "show"
    return kb([
        [cb_btn("✏️ Название", f"aedit:{key}:name"),
         cb_btn("💰 Цена", f"aedit:{key}:price")],
        [cb_btn("📝 Описание", f"aedit:{key}:description"),
         cb_btn("🖼 Фото", f"aedit:{key}:photo_token")],
        [cb_btn("🗂 Категория", f"aedit:{key}:category_key"),
         cb_btn("🗑 Удалить", f"adel:{key}")],
        [cb_btn(toggle_label, f"atoggle:{key}:{toggle_action}")],
    ])

async def cart_text_and_keyboard(user_id):
    rows = await cart_get(user_id)
    if not rows:
        return "Корзина пуста.", kb([[cb_btn("🗑 Очистить корзину", "main_menu")]])

    lines, kb_buttons = [], []
    total = Decimal("0")
    garnishes = await get_all_garnishes(user_id)

    for key, qty in rows:
        item_key, _ = parse_cart_key(key)
        item = await menu_get(item_key)
        if not item:
            continue
        subtotal = Decimal(item["price"]) * qty
        total += subtotal
        lines.append(f"• {item['name']} ({qty} шт.) — {subtotal} ₽")
        garnish = garnishes.get(key)
        if garnish:
            lines.append(f"   Гарнир: {GARNISH_LABELS.get(garnish, garnish)}")
        kb_buttons.append([
            cb_btn(f"➖ {item['name'][:15]}", f"cartsub:{key}"),
            cb_btn("➕", f"cartadd:{key}"),
            cb_btn("❌", f"cartdel:{key}"),
        ])

    text = "🛒 Ваша корзина:\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽"

    if total < MIN_ORDER_AMOUNT:
        missing = MIN_ORDER_AMOUNT - total
        text += f"\n⚠️ Минимальный заказ: {MIN_ORDER_AMOUNT} ₽. Добавьте ещё на {missing} ₽"
    elif total < FREE_DELIVERY_FROM:
        missing = FREE_DELIVERY_FROM - total
        text += (f"\n🚚 Доставка {DELIVERY_COST} ₽. Добавьте ещё на {missing} ₽ — "
                 f"и доставка бесплатная!")
    else:
        text += "\n🚚 Доставка бесплатная!"

    kb_buttons.append([cb_btn("📞 Мой телефон", "edit_phone"),
                       cb_btn("📍 Мой адрес", "edit_address")])
    kb_buttons.append([cb_btn("✅ Оформить заказ", "order")])
    kb_buttons.append([cb_btn("🗑 Очистить корзину", "main_menu")])
    return text, kb(kb_buttons)

# ================= ОБЩИЕ ЭКРАНЫ =================
async def send_menu(chat_id):
    cats = await categories_visible()
    if not cats:
        await api.send(chat_id, "МЕНЮ (пока пустое). Зайдите в /admin и добавьте блюда.")
        return
    await api.send(chat_id, "МЕНЮ", attachments=[categories_keyboard(cats)])

def offer_keyboard():
    return kb([
        [link_btn("📄 Прочитать оферту", OFFER_PDF_URL)],
        [cb_btn("✅ Согласен", "agree_offer"),
         cb_btn("❌ Не согласен", "disagree_offer")],
    ])


async def send_offer_screen(chat_id):
    await api.send(
        chat_id,
        "Для продолжения работы с ботом ознакомьтесь с публичной офертой "
        "по кнопке «Прочитать оферту» и выберите «Согласен».",
        attachments=[offer_keyboard()]
    )


# ================= ОБРАБОТКА СООБЩЕНИЙ =================
async def handle_message(message):
    sender = (message.get("sender") or {})
    recipient = (message.get("recipient") or {})
    user_id = sender.get("user_id")
    chat_id = recipient.get("chat_id") or user_id
    if not user_id or chat_id is None:
        return
    username = sender.get("username")
    body = (message.get("body") or {})
    text = (body.get("text") or "").strip()
    photos = [a for a in (body.get("attachments") or [])
              if a.get("type") == "image"]
    photo_token = None
    if photos:
        photo_token = (photos[-1].get("payload") or {}).get("token")

    state = st_get(user_id)
    sname = state.get("state")

    if text.startswith("/start"):
        user = await get_user(user_id)
        if not user or not user["agreed_offer"]:
            await send_offer_screen(chat_id)
            st_set(user_id, "waiting_offer")
            return
        st_clear(user_id)
        await send_menu(chat_id)
        return

    if text.startswith("/offer"):
        await api.send(
            chat_id,
            "Полный текст публичной оферты доступен по ссылке:",
            attachments=[kb([[link_btn("📄 Прочитать оферту", OFFER_PDF_URL)]])]
        )
        return

    if text.startswith("/manager"):
        if is_manager(user_id):
            await api.send(chat_id, "Панель менеджера:",
                           attachments=[manager_panel_keyboard()])
        else:
            await api.send(chat_id, "Нет доступа.")
        st_clear(user_id)
        return

    if text.startswith("/admin"):
        if is_admin(user_id):
            await api.send(chat_id, "Админ-панель:",
                           attachments=[admin_panel_keyboard()])
        else:
            await api.send(chat_id, "Нет доступа.")
        st_clear(user_id)
        return

    if text.startswith("/menu"):
        user = await get_user(user_id)
        if user and user["agreed_offer"]:
            st_clear(user_id)
            await send_menu(chat_id)
        return

    # ===== Состояния админа: добавление блюда =====
    if sname == "admin_add_key" and text:
        key = text.lower().replace(" ", "_")
        if not key or not key.isascii():
            await api.send(chat_id, "Только латиница без пробелов:")
            return
        if await menu_get(key):
            await api.send(chat_id, "Такой ключ уже есть, придумайте другой:")
            return
        data = st_data(user_id)
        data["key"] = key
        st_set(user_id, "admin_add_name", **data)
        await api.send(chat_id, "Название блюда:")
        return

    if sname == "admin_add_name" and text:
        if len(text) < 2:
            await api.send(chat_id, "Слишком короткое:")
            return
        data = st_data(user_id)
        data["name"] = text
        st_set(user_id, "admin_add_description", **data)
        await api.send(chat_id, "Описание (вес, острота):")
        return

    if sname == "admin_add_description" and text:
        data = st_data(user_id)
        data["description"] = text
        st_set(user_id, "admin_add_price", **data)
        await api.send(chat_id, "Цена в рублях (только число):")
        return

    if sname == "admin_add_price" and text:
        try:
            price = Decimal(text.replace(",", "."))
            if price <= 0:
                raise InvalidOperation
        except Exception:
            await api.send(chat_id, "Положительное число:")
            return
        data = st_data(user_id)
        data["price"] = str(price)
        cats = await categories_all()
        buttons = [[cb_btn(c["display_name"] or c["name"], f"pickcat:{c['key']}")]
                   for c in cats]
        buttons.append([cb_btn("Отмена", "admin_cancel")])
        st_set(user_id, "admin_add_category", **data)
        await api.send(chat_id, "Выберите категорию:", attachments=[kb(buttons)])
        return

    if sname == "admin_add_photo":
        if photo_token:
            pass
        elif text.lower() != "пропустить":
            await api.send(chat_id, "Фото или слово «пропустить»:")
            return
        data = st_data(user_id)
        await menu_add(data["key"], data["name"], data.get("description", ""),
                       data["price"], data.get("category_key", "default"), photo_token)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Блюдо «{data['name']}» добавлено.",
                       attachments=[admin_panel_keyboard()])
        return

    # ===== Состояния админа: правка блюда =====
    if sname == "admin_edit_value":
        data = st_data(user_id)
        key, field = data.get("key"), data.get("field")
        if field == "photo_token":
            if not photo_token:
                await api.send(chat_id, "Нужно фото:")
                return
            value = photo_token
        elif field == "price":
            try:
                value = str(Decimal(text.replace(",", ".")))
            except Exception:
                await api.send(chat_id, "Число:")
                return
        else:
            value = text
        await menu_update(key, field, value)
        st_clear(user_id)
        await api.send(chat_id, "✅ Обновлено.", attachments=[admin_panel_keyboard()])
        return

    # ===== Состояния админа: категории =====
    if sname == "admin_cat_key" and text:
        key = text.lower().replace(" ", "_")
        if not key or not key.isascii():
            await api.send(chat_id, "Только латиница без пробелов:")
            return
        st_set(user_id, "admin_cat_name", key=key)
        await api.send(chat_id, "Название категории (его увидят клиенты, "
                                "например: Основные блюда):")
        return

    if sname == "admin_cat_name" and text:
        if len(text) < 2:
            await api.send(chat_id, "Слишком короткое:")
            return
        data = st_data(user_id)
        await category_add(data["key"], data["key"], text)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Категория «{text}» добавлена.",
                       attachments=[admin_panel_keyboard()])
        return

    # ===== Состояния админа: QR-код =====
    if sname == "admin_qr":
        if photo_token:
            await set_setting("payment_qr", photo_token)
            st_clear(user_id)
            await api.send(chat_id, "✅ QR-код для оплаты успешно загружен! "
                                    "Теперь он будет приходить клиентам после "
                                    "оформления заказа.",
                           attachments=[admin_panel_keyboard()])
        else:
            await api.send(chat_id, "Отправьте фото QR-кода:")
        return

    # ===== Состояния админа: импорт меню списком =====
    if sname == "admin_excel" and text:
        if text.lower() in ("отмена", "cancel"):
            st_clear(user_id)
            await api.send(chat_id, "Отменено.",
                           attachments=[admin_panel_keyboard()])
            return
        count = 0
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = [p.strip() for p in line.split(";")]
            if len(parts) < 5 or not parts[0] or not parts[1]:
                continue
            key = parts[0].lower().replace(" ", "_")
            name, desc, price, cat = parts[1], parts[2], parts[3], parts[4]
            cat_display = parts[5] if len(parts) > 5 and parts[5] else None
            await category_add(cat, cat, cat_display)
            await menu_add(key, name, desc or "", price, cat, None)
            count += 1
        st_clear(user_id)
        await api.send(chat_id, f"✅ Успешно загружено {count} блюд из списка!",
                       attachments=[admin_panel_keyboard()])
        return

    # ===== Оформление заказа: имя/телефон/адрес/время/комментарий =====
    if sname == "edit_phone":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 10:
            await api.send(chat_id, "Некорректный номер. Введите ещё раз:")
            return
        user = await get_user(user_id)
        name = user["name"] if user and user["name"] else "Гость"
        address = user["address"] if user and user["address"] else "—"
        await save_user(user_id, name, text, address)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Телефон сохранён: {text}")
        return

    if sname == "edit_address":
        if len(text) < 5:
            await api.send(chat_id, "Адрес слишком короткий, напишите подробнее:")
            return
        user = await get_user(user_id)
        name = user["name"] if user and user["name"] else "Гость"
        phone = user["phone"] if user and user["phone"] else "—"
        await save_user(user_id, name, phone, text)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Адрес сохранён: {text}")
        return

    if sname == "waiting_name":
        if not text or len(text) < 2:
            await api.send(chat_id, "Укажите имя (мин. 2 символа):")
            return
        data = st_data(user_id)
        data["name"] = text
        user = await get_user(user_id)
        phone = user["phone"] if user and user["phone"] else "—"
        address = user["address"] if user and user["address"] else "—"
        await save_user(user_id, text, phone, address)
        st_set(user_id, "waiting_phone", **data)
        await api.send(chat_id, "Укажите номер телефона:")
        return

    if sname == "waiting_phone":
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) < 10:
            await api.send(chat_id, "Некорректный номер. Введите ещё раз:")
            return
        data = st_data(user_id)
        data["phone"] = text
        user = await get_user(user_id)
        address = user["address"] if user and user["address"] else "—"
        await save_user(user_id, data.get("name", "Гость"), text, address)
        st_set(user_id, "waiting_address", **data)
        await api.send(chat_id, "Укажите адрес доставки:")
        return

    if sname == "waiting_address":
        if not text or len(text) < 5:
            await api.send(chat_id, "Адрес слишком короткий:")
            return
        data = st_data(user_id)
        data["address"] = text
        await save_user(user_id, data.get("name", "Гость"),
                        data.get("phone", "—"), text)
        st_set(user_id, "waiting_time", **data)
        buttons = [[cb_btn("🚀 Как можно скорее", "time:now")]]
        await api.send(chat_id,
                       "⏰ Когда доставить заказ?\n\n"
                       "• Нажмите «Как можно скорее»\n"
                       "• Или напишите время, например: «к 19:00»",
                       attachments=[kb(buttons)])
        return

    if sname == "waiting_time":
        data = st_data(user_id)
        data["delivery_time"] = text
        st_set(user_id, "waiting_comment", **data)
        buttons = [[cb_btn("Пропустить и оплатить", "skip_comment")]]
        await api.send(chat_id, "Комментарий к заказу (или «Пропустить и оплатить»):",
                       attachments=[kb(buttons)])
        return

    if sname == "waiting_comment":
        data = st_data(user_id)
        data["comment"] = text or "—"
        st_set(user_id, "waiting_comment", **data)
        await finalize_order(chat_id, user_id, username)
        return

    # ===== Поиск: любой текст вне диалогов =====
    if len(text) >= 2:
        user = await get_user(user_id)
        if not user or not user["agreed_offer"]:
            await send_offer_screen(chat_id)
            st_set(user_id, "waiting_offer")
            return
        items = await menu_search(text)
        if not items:
            await api.send(chat_id, "Ничего не найдено. Попробуйте другое слово.")
            return
        buttons = [[cb_btn(item_label(i), f"view:{i['key']}")] for i in items]
        buttons.append([cb_btn("⬅️ Назад", "back_categories")])
        await api.send(chat_id, f"🔍 Результаты по запросу «{text}»:",
                       attachments=[kb(buttons)])
        return

    user = await get_user(user_id)
    if user and user["agreed_offer"]:
        await send_menu(chat_id)
    else:
        await send_offer_screen(chat_id)
        st_set(user_id, "waiting_offer")
# ================= ЗАКАЗ: ФИНАЛИЗАЦИЯ =================
async def finalize_order(chat_id, user_id, username):
    data = st_data(user_id)
    rows = await cart_get(user_id)
    if not rows:
        await api.send(chat_id, "Корзина пуста. Начните заново: /start")
        st_clear(user_id)
        return

    await save_user(user_id, data.get("name", "—"),
                    data.get("phone", "—"), data.get("address", "—"))

    garnishes = await get_all_garnishes(user_id)
    lines, total = [], Decimal("0")
    missing = []
    for key, qty in rows:
        item_key, _ = parse_cart_key(key)
        item = await menu_get(item_key)
        if not item or not item["active"] or (item["stock"] is not None and item["stock"] <= 0):
            missing.append(key)
            continue
        pos_sum = Decimal(item["price"]) * qty
        total += pos_sum
        lines.append(f"• {item['name']} ({qty} шт.) — {pos_sum} ₽")
        garnish = garnishes.get(key)
        if garnish:
            lines.append(f"   Гарнир: {GARNISH_LABELS.get(garnish, garnish)}")
    if missing:
        for key in missing:
            await cart_delete_item(user_id, key)
    if not lines:
        await api.send(chat_id, "Все позиции из корзины стали недоступны. "
                                "Посмотрите меню заново: /menu")
        st_clear(user_id)
        return
    if missing:
        await api.send(chat_id, "⚠️ Часть позиций больше недоступна — заказ "
                                "пересчитан без них.")

    items_text = "\n".join(lines)

    count_before = await get_order_count(user_id)
    new_count = count_before + 1
    is_discount_order = (new_count % DISCOUNT_EVERY_N_ORDERS == 0)
    if is_discount_order:
        discount_sum = total * DISCOUNT_PERCENT / Decimal("100")
        discount_note = (f"🎉 Скидка 50% (каждый {DISCOUNT_EVERY_N_ORDERS}-й заказ): "
                         f"−{discount_sum} ₽")
        bonus_text = (f"🎉 Поздравляем! Это ваш {new_count}-й заказ — "
                      f"скидка 50% применена!\nСпасибо, что вы с нами!")
    else:
        discount_sum = Decimal("0")
        discount_note = None
        remaining = DISCOUNT_EVERY_N_ORDERS - (new_count % DISCOUNT_EVERY_N_ORDERS)
        bonus_text = (f"🎁 До скидки 50% осталось {remaining} заказ(ов).\n"
                      f"Спасибо, что вы с нами!")

    base = total - discount_sum
    if base >= FREE_DELIVERY_FROM:
        delivery_cost = Decimal("0")
        delivery_note = "🚚 Доставка: бесплатно"
    else:
        delivery_cost = DELIVERY_COST
        delivery_note = f"🚚 Доставка: {delivery_cost} ₽"
    final_total = base + delivery_cost

    order_id = await order_save(user_id, username or "—", data, items_text,
                                final_total,
                                delivery_time=data.get("delivery_time", "—"))
    await order_items_save(order_id, rows, garnishes)
    await increment_order_count(user_id)

    manager_text = (
        f"🆕 Заказ #{order_id}\n"
        f"Клиент: @{username or '—'} (ID: {user_id})\n"
        f"Имя: {data.get('name')}\n"
        f"Тел: {data.get('phone')}\n"
        f"Адрес: {data.get('address')}\n"
        f"⏰ Время: {data.get('delivery_time', '—')}\n"
        f"Коммент: {data.get('comment') or '—'}\n"
        f"Заказ клиента №{new_count}\n\n"
        f"{items_text}\n\n"
    )
    if discount_note:
        manager_text += f"{discount_note}\n"
    manager_text += f"{delivery_note}\nИтого: {final_total} ₽"

    sent = await api.send_to_user(MANAGER_CHAT_ID, manager_text,
                                  attachments=[manager_keyboard(order_id, "new")])
    manager_sent = sent is not None

    user_text = (
        f"✅ Ваш заказ **#{order_id}** успешно оформлен!\n\n"
        f"Имя: {data.get('name')}\n"
        f"Адрес: {data.get('address')}\n"
        f"⏰ Время доставки: {data.get('delivery_time', '—')}\n"
        f"{items_text}\n\n"
    )
    if discount_note:
        user_text += f"{discount_note}\n"
    user_text += (
        f"{delivery_note}\n"
        f"💰 **Итого: {final_total} ₽**\n\n"
        f"{bonus_text}\n\n"
        f"📌 **Для оплаты:**\n"
        f"Отсканируйте QR-код ниже и оплатите заказ."
    )
    buttons = [[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]]
    await api.send(chat_id, user_text, attachments=[kb(buttons)])

    qr_token = await get_setting("payment_qr")
    if qr_token:
        try:
            await api.send(chat_id,
                           f"💳 **Оплата заказа #{order_id}**\n"
                           f"Сумма: **{final_total} ₽**\n\n"
                           f"Отсканируйте QR-код для оплаты.",
                           attachments=[photo_attachment(qr_token)])
        except Exception:
            pass

    if not manager_sent:
        await api.send_to_user(
            MANAGER_CHAT_ID,
            "⚠️ Внимание: автоматическая отправка не удалась. Чек от клиента:\n\n"
            + manager_text)

    await cart_clear(user_id)
    await clear_garnishes(user_id)
    st_clear(user_id)

# ================= ОБРАБОТКА CALLBACK =================
async def handle_callback(update):
    cb = update.get("callback") or {}
    callback_id = cb.get("callback_id") or update.get("callback_id")

    # Поддерживаем варианты структуры callback-событий MAX.
    payload = cb.get("payload")
    if payload is None:
        payload = cb.get("data", update.get("payload", ""))
    if not isinstance(payload, str):
        payload = str(payload)

    user = cb.get("user") or update.get("user") or {}
    user_id = user.get("user_id")
    msg = cb.get("message") or update.get("message") or {}
    recipient = msg.get("recipient") or {}
    chat_id = recipient.get("chat_id") or update.get("chat_id") or user_id

    log.info("Callback: payload=%r user=%s chat=%s", payload, user_id, chat_id)

    async def ack(notification=None):
        if callback_id:
            await api.answer_callback(callback_id, notification)

    if not user_id or chat_id is None:
        log.warning("Callback без user_id/chat_id: %r", update)
        await ack("Не удалось определить пользователя. Напишите /start.")
        return

    if payload == "agree_offer":
        await set_agreed(user_id)
        st_clear(user_id)
        await ack("Спасибо! Согласие принято.")
        log.info("Пользователь %s согласился с офертой", user_id)
        await send_menu(chat_id)
        return

    if payload == "disagree_offer":
        st_clear(user_id)
        await ack("Вы отказались от условий оферты.")
        await api.send(
            chat_id,
            "Без согласия с офертой оформление заказов недоступно. "
            "Если передумаете, нажмите /start."
        )
        return

    user = await get_user(user_id)
    if not user or not user["agreed_offer"]:
        await ack("Сначала согласитесь с офертой: /start")
        return

    if payload == "main_menu":
        st_clear(user_id)
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        await ack()
        await send_menu(chat_id)
        return

    if payload == "menu":
        st_clear(user_id)
        await ack()
        await send_menu(chat_id)
        return

    if payload.startswith("cat:"):
        cat_key = payload.split(":")[1]
        items = await menu_all(category_key=cat_key, in_stock_only=True)
        if not items:
            await ack("Пусто")
            return
        await ack()
        await api.send(chat_id, "МЕНЮ",
                       attachments=[menu_keyboard(items, cat_key)])
        return

    if payload == "back_categories":
        await ack()
        await send_menu(chat_id)
        return

    if payload.startswith("cat_back:"):
        key = payload.split(":")[1]
        item = await menu_get(key)
        if item:
            items = await menu_all(category_key=item["category_key"],
                                   in_stock_only=True)
            if items:
                await ack()
                await api.send(chat_id, "МЕНЮ",
                               attachments=[menu_keyboard(items,
                                                          item["category_key"])])
                return
        await ack("Пусто")
        return

    if payload.startswith("view:"):
        key = payload.split(":")[1]
        item = await menu_get(key)
        if not item or not item["active"]:
            await ack("Блюдо недоступно")
            return
        sold_out = (item["stock"] is not None and item["stock"] <= 0)
        desc = item["description"] if item["description"] else "Без описания"
        text = f"**{item['name']}**\n\n{desc}\n\n💰 **Цена: {item['price']} ₽**"
        if sold_out:
            text += "\n\n🚫 Сейчас нет в наличии"
        attachments = [await item_card_keyboard(user_id, key, item["category_key"])]
        if item["photo_token"]:
            attachments.insert(0, photo_attachment(item["photo_token"]))
        await ack()
        await api.send(chat_id, text, attachments=attachments)
        return

    if payload.startswith("add:"):
        key = payload.split(":")[1]
        item = await menu_get(key)
        if not item or not item["active"]:
            await ack("Недоступно")
            return
        if item["stock"] is not None and item["stock"] <= 0:
            await ack("Нет в наличии")
            return
        if item["category_key"] in GARNISH_CATEGORIES:
            await ack()
            await api.send(chat_id, f"{item['name']} — выберите гарнир:",
                           attachments=[garnish_keyboard(key)])
        else:
            await cart_add(user_id, key)
            await ack(f"✅ {item['name']} добавлен в корзину!")
        return

    if payload.startswith("garnish:"):
        _, item_key, garnish = payload.split(":")
        await set_garnish(user_id, item_key, garnish)
        await cart_add(user_id, item_key, garnish)
        item = await menu_get(item_key)
        await ack(f"✅ {item['name'] if item else 'Блюдо'} добавлено. "
                  f"Гарнир: {GARNISH_LABELS[garnish]}")
        return

    if payload == "cart":
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartsub:"):
        await cart_remove_one(user_id, payload.split(":", 1)[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartadd:"):
        await cart_add(user_id, payload.split(":", 1)[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartdel:"):
        await cart_delete_item(user_id, payload.split(":", 1)[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    # ===== Избранное =====
    if payload.startswith("fav:"):
        key = payload.split(":")[1]
        if await is_favorite(user_id, key):
            await favorites_remove(user_id, key)
            await ack("Убрано из избранного")
        else:
            await favorites_add(user_id, key)
            await ack("Добавлено в избранное ❤️")
        item = await menu_get(key)
        if item:
            await api.send(chat_id,
                           f"**{item['name']}**",
                           attachments=[await item_card_keyboard(user_id, key,
                                                                 item["category_key"])])
        return

    if payload == "favs":
        fav_keys = await favorites_all(user_id)
        if not fav_keys:
            await ack()
            await api.send(chat_id,
                           "❤️ Избранное пусто.\n\nНажмите «🤍 В избранное» "
                           "на карточке любого блюда — и оно появится здесь.")
            return
        buttons = []
        for key in fav_keys:
            item = await menu_get(key)
            if not item or not item["active"]:
                continue
            buttons.append([cb_btn(item_label(item), f"view:{key}")])
        if not buttons:
            await ack()
            await api.send(chat_id, "❤️ Избранное пусто.")
            return
        buttons.append([cb_btn("⬅️ Назад", "back_categories")])
        await ack()
        await api.send(chat_id, "❤️ Избранное:", attachments=[kb(buttons)])
        return

    # ===== История и повтор заказа =====
    if payload == "history":
        orders = await orders_of_user(user_id, limit=10)
        if not orders:
            await ack()
            await api.send(chat_id,
                           "📜 История заказов пуста. Оформите первый заказ!")
            return
        await ack()
        for o in orders:
            text = (f"📜 Заказ #{o['id']} — "
                    f"{STATUS_LABELS.get(o['status'], o['status'])}\n"
                    f"Создан: {o['created_at']}\n"
                    f"{o['items']}\n\n💰 Итого: {o['total']} ₽")
            if o["delivery_time"] and o["delivery_time"] != "—":
                text += f"\n⏰ К времени: {o['delivery_time']}"
            await api.send(chat_id, text,
                           attachments=[kb([[cb_btn("🔁 Повторить заказ",
                                                    f"reorder:{o['id']}")]])])
        return

    if payload.startswith("reorder:"):
        order_id = int(payload.split(":")[1])
        items = await orders_items_of_user(user_id, order_id)
        if not items:
            await ack("Не удалось найти состав заказа")
            return
        st_clear(user_id)
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        added = 0
        for row in items:
            item = await menu_get(row["item_key"])
            if not item or not item["active"]:
                continue
            if item["stock"] is not None and item["stock"] <= 0:
                continue
            garnish = row["garnish"] or None
            for _ in range(row["quantity"]):
                await cart_add(user_id, row["item_key"], garnish)
            if garnish:
                await set_garnish(user_id, row["item_key"], garnish)
            added += 1
        if added == 0:
            await ack("Ни одна позиция недоступна — меню изменилось")
            return
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await ack()
        await api.send(chat_id,
                       f"🔁 Заказ #{order_id} добавлен в корзину ({added} поз.):\n\n{text}",
                       attachments=[kbd_])
        return

    # ===== Быстрая правка телефона и адреса =====
    if payload == "edit_phone":
        u = await get_user(user_id)
        current = u["phone"] if u and u["phone"] else "не указан"
        st_set(user_id, "edit_phone")
        await ack()
        await api.send(chat_id, f"Текущий телефон: {current}\n\nВведите новый номер:")
        return

    if payload == "edit_address":
        u = await get_user(user_id)
        current = u["address"] if u and u["address"] else "не указан"
        st_set(user_id, "edit_address")
        await ack()
        await api.send(chat_id, f"Текущий адрес: {current}\n\nВведите новый адрес:")
        return

    # ===== Оформление заказа =====
    if payload == "order":
        rows = await cart_get(user_id)
        if not rows:
            await ack("Корзина пуста")
            return
        total = Decimal("0")
        for key, qty in rows:
            item_key, _ = parse_cart_key(key)
            item = await menu_get(item_key)
            if item:
                total += Decimal(item["price"]) * qty
        if total < MIN_ORDER_AMOUNT:
            missing = MIN_ORDER_AMOUNT - total
            await ack(f"Минимальный заказ {MIN_ORDER_AMOUNT} ₽. "
                      f"Добавьте ещё на {missing} ₽")
            return
        if user["name"] and user["phone"] and user["address"]:
            text = ("Ваши данные для доставки:\n\n"
                    f"👤 Имя: {user['name']}\n"
                    f"📞 Тел: {user['phone']}\n"
                    f"📍 Адрес: {user['address']}\n\n"
                    "Все верно?")
            st_set(user_id, "confirm_data")
            await ack()
            await api.send(chat_id, text,
                           attachments=[kb([
                               [cb_btn("✅ Да, оформить", "confirm_yes")],
                               [cb_btn("✏️ Изменить данные", "confirm_no")],
                           ])])
        else:
            st_set(user_id, "waiting_name")
            await ack()
            await api.send(chat_id, "Как вас зовут?")
        return

    if payload == "confirm_yes":
        data = st_data(user_id)
        data["name"], data["phone"], data["address"] = (
            user["name"], user["phone"], user["address"])
        st_set(user_id, "waiting_time", **data)
        await ack()
        await api.send(chat_id,
                       "⏰ Когда доставить заказ?\n\n"
                       "• Нажмите «Как можно скорее»\n"
                       "• Или напишите время, например: «к 19:00»",
                       attachments=[kb([[cb_btn("🚀 Как можно скорее", "time:now")]])])
        return

    if payload == "time:now":
        data = st_data(user_id)
        data["delivery_time"] = "Как можно скорее"
        st_set(user_id, "waiting_comment", **data)
        await ack()
        await api.send(chat_id,
                       "Комментарий к заказу (или «Пропустить и оплатить»):",
                       attachments=[kb([[cb_btn("Пропустить и оплатить",
                                                "skip_comment")]])])
        return

    if payload == "confirm_no":
        st_set(user_id, "waiting_name")
        await ack()
        await api.send(chat_id, "Введите ваше имя:")
        return

    if payload == "skip_comment":
        data = st_data(user_id)
        data["comment"] = "—"
        st_set(user_id, "waiting_comment", **data)
        await ack()
        await finalize_order(chat_id, user_id, user.get("username"))
        return

    # ===== Админ-панель =====
    if payload == "admin_cancel":
        st_clear(user_id)
        await ack()
        await api.send(chat_id, "Отменено.", attachments=[admin_panel_keyboard()])
        return

    if payload == "admin:add" and is_admin(user_id):
        st_set(user_id, "admin_add_key")
        await ack()
        await api.send(chat_id, "Ключ блюда (латиницей):")
        return

    if payload == "admin:list" and is_admin(user_id):
        items = await menu_all(active_only=False)
        if not items:
            await ack("Меню пустое.")
            return
        await ack()
        for item in items:
            stock = item["stock"]
            stock_str = "без учёта" if stock is None else str(stock)
            text = (f"🔑 {item['key']}\n📛 {item['name']}\n"
                    f"📝 {item['description'] or '—'}\n"
                    f"💰 {item['price']} ₽\n🗂 {item['category_key']}\n"
                    f"📦 Остаток: {stock_str}\n"
                    f"Статус: {'активно' if item['active'] else 'скрыто'}")
            attachments = [admin_item_keyboard(item["key"], item["active"])]
            if item["photo_token"]:
                attachments.insert(0, photo_attachment(item["photo_token"]))
            await api.send(chat_id, text, attachments=attachments)
        return

    if payload == "admin:cats" and is_admin(user_id):
        cats = await categories_all()
        text = "Категории:\n" + "\n".join(
            f"• {c['key']} — {c['display_name'] or c['name']}" for c in cats)
        await ack()
        await api.send(chat_id, text)
        return

    if payload == "admin:cat_add" and is_admin(user_id):
        st_set(user_id, "admin_cat_key")
        await ack()
        await api.send(chat_id, "Ключ категории (латиницей):")
        return

    if payload == "admin:cat_del" and is_admin(user_id):
        cats = await categories_all()
        deletable = [c for c in cats if c["key"] != "default"]
        if not deletable:
            await ack("Удалять нечего: кроме служебной категории «Меню» "
                      "других категорий нет.")
            return
        buttons = [[cb_btn(f"🗑 {c['display_name'] or c['name']} ({c['key']})",
                           f"catdel:{c['key']}")] for c in deletable]
        buttons.append([cb_btn("⬅️ Назад", "admin:back")])
        await ack()
        await api.send(chat_id,
                       "Выберите категорию для удаления.\n"
                       "Блюда из неё автоматически переедут в категорию «Меню».",
                       attachments=[kb(buttons)])
        return

    if payload.startswith("catdel:") and is_admin(user_id):
        key = payload.split(":")[1]
        if key == "default":
            await ack("Служебную категорию удалить нельзя")
            return
        await category_delete(key)
        await ack()
        await api.send(chat_id,
                       f"🗑 Категория `{key}` удалена. "
                       f"Её блюда переедут в категорию «Меню».")
        return

    if payload == "admin:stock" and is_admin(user_id):
        items = await menu_all(active_only=False)
        if not items:
            await ack("Меню пустое.")
            return
        lines = ["📦 **Остатки и стоп-лист**\n"]
        buttons = []
        for item in items:
            stock = item["stock"]
            stock_str = "без учёта" if stock is None else str(stock)
            status = "🚫 СТОП" if stock == 0 else "✅ в наличии"
            lines.append(f"• {item['name']} — остаток: {stock_str} ({status})")
            if stock == 0:
                buttons.append([cb_btn(f"✅ Вернуть в продажу: {item['name']}",
                                       f"stock:{item['key']}:on")])
            else:
                buttons.append([cb_btn(f"🚫 В стоп-лист: {item['name']}",
                                       f"stock:{item['key']}:off")])
        buttons.append([cb_btn("⬅️ Назад", "admin:back")])
        await ack()
        await api.send(chat_id, "\n".join(lines), attachments=[kb(buttons)])
        return

    if payload.startswith("stock:") and is_admin(user_id):
        _, key, action = payload.split(":")
        new_stock = 0 if action == "off" else None
        await menu_update(key, "stock", new_stock)
        label = "🚫 в стоп-листе" if action == "off" else "✅ вернулся в продажу"
        await ack("Готово")
        await api.send(chat_id, f"Блюдо `{key}` {label}.")
        return

    if payload == "admin:stats" and is_admin(user_id):
        all_time, today = await stats_summary()
        lines = ["📈 **Статистика**",
                 f"Заказов всего: {all_time['cnt']} | Выручка: {all_time['revenue']} ₽",
                 f"Заказов сегодня: {today['cnt']} | Выручка: {today['revenue']} ₽",
                 "",
                 "🏆 **Топ-10 блюд по продажам:**"]
        top = await sales_top(10)
        if not top:
            lines.append("Пока нет продаж (учитываются заказы после этого обновления).")
        else:
            for i, row in enumerate(top, start=1):
                item = await menu_get(row["item_key"])
                name = item["name"] if item else row["item_key"]
                lines.append(f"{i}. {name} — {row['total_qty']} шт. "
                             f"({row['orders_cnt']} заказов)")
        await ack()
        await api.send(chat_id, "\n".join(lines))
        return

    if payload == "admin:qr" and is_admin(user_id):
        st_set(user_id, "admin_qr")
        await ack()
        await api.send(chat_id,
                       "Отправьте фото QR-кода для оплаты (оно будет показываться "
                       "клиентам после оформления заказа):")
        return

    if payload == "admin:excel" and is_admin(user_id):
        st_set(user_id, "admin_excel")
        await ack()
        await api.send(chat_id,
                       "Отправьте список блюд текстом — по одному блюду на строку, "
                       "поля через точку с запятой:\n\n"
                       "ключ; Название; Описание; Цена; категория; Название категории\n\n"
                       "Пример строки:\n"
                       "kotleta; Котлета по-киевски; 250 г, курица; 350; hot_dishes; "
                       "Горячие блюда\n\n"
                       "Категория и название категории создадутся автоматически.\n"
                       "Для отмены напишите: отмена")
        return

    if payload == "admin:backup" and is_admin(user_id):
        await ack()
        try:
            src = sqlite3.connect(DB_PATH)
            dump = "\n".join(src.iterdump())
            src.close()
            chunk_size = 3500
            parts = [dump[i:i + chunk_size]
                     for i in range(0, len(dump), chunk_size)]
            await api.send(chat_id,
                           f"💾 Резервная копия базы (SQL-дамп), "
                           f"частей: {len(parts)}. Сохраните их все.")
            for i, part in enumerate(parts, start=1):
                await api.send(chat_id, f"— Часть {i}/{len(parts)} —\n```\n{part}\n```")
        except Exception as e:
            await api.send(chat_id, f"❌ Не удалось сделать копию: {e}")
        return

    if payload == "admin:back" and is_admin(user_id):
        await ack()
        await api.send(chat_id, "Админ-панель:", attachments=[admin_panel_keyboard()])
        return

    if payload.startswith("pickcat:") and is_admin(user_id):
        cat_key = payload.split(":")[1]
        data = st_data(user_id)
        data["category_key"] = cat_key
        st_set(user_id, "admin_add_photo", **data)
        await ack()
        await api.send(chat_id, "Фото блюда (или «пропустить»):")
        return

    if payload.startswith("aedit:") and is_admin(user_id):
        _, key, field = payload.split(":", 2)
        item = await menu_get(key)
        if not item:
            await ack("Не найдено")
            return
        if field == "category_key":
            cats = await categories_all()
            buttons = [[cb_btn(c["display_name"] or c["name"],
                               f"setcat:{key}:{c['key']}")] for c in cats]
            buttons.append([cb_btn("Отмена", "admin_cancel")])
            await ack()
            await api.send(chat_id, "Выберите категорию:", attachments=[kb(buttons)])
            return
        st_set(user_id, "admin_edit_value", key=key, field=field)
        prompts = {"name": "Новое название:", "price": "Новая цена:",
                   "description": "Новое описание:",
                   "photo_token": "Пришлите новое фото:"}
        await ack()
        await api.send(chat_id, prompts.get(field, "Новое значение:"))
        return

    if payload.startswith("setcat:") and is_admin(user_id):
        _, key, cat_key = payload.split(":", 2)
        await menu_update(key, "category_key", cat_key)
        await ack()
        await api.send(chat_id, f"✅ Категория блюда {key} изменена на {cat_key}.")
        return

    if payload.startswith("atoggle:") and is_admin(user_id):
        _, key, action = payload.split(":")
        new_active = 1 if action == "show" else 0
        await menu_update(key, "active", new_active)
        await ack(f"Блюдо {'показано' if new_active else 'скрыто'}")
        await api.send(chat_id,
                       f"✅ Блюдо {key} {'показано' if new_active else 'скрыто'}.")
        return

    if payload.startswith("adel:") and is_admin(user_id):
        key = payload.split(":")[1]
        await menu_delete(key)
        await ack()
        await api.send(chat_id, f"🗑 Блюдо {key} удалено.")
        return

    # ===== Менеджер =====
    if payload.startswith("mgr:") and is_manager(user_id):
        status = payload.split(":")[1]
        orders = await orders_by_status(status, limit=10)
        if not orders:
            await ack(f"{STATUS_LABELS.get(status, status)}: заказов нет.")
            return
        await ack()
        for o in orders:
            text = (f"Заказ #{o['id']} — "
                    f"{STATUS_LABELS.get(o['status'], o['status'])}\n"
                    f"Клиент: {o['name']} (@{o['username']}, ID: {o['user_id']})\n"
                    f"Тел: {o['phone']}\nАдрес: {o['address']}\n"
                    f"⏰ Время: {o['delivery_time'] or '—'}\n"
                    f"Коммент: {o['comment']}\n\n{o['items']}\n\n"
                    f"Итого: {o['total']} ₽\nСоздан: {o['created_at']}")
            await api.send(chat_id, text,
                           attachments=[manager_keyboard(o["id"], o["status"])])
        return

    if payload.startswith("setstatus:") and is_manager(user_id):
        _, order_id, new_status = payload.split(":")
        order_id = int(order_id)
        order = await order_get(order_id)
        if not order:
            await ack("Заказ не найден")
            return
        previous_status = order["status"]
        await order_set_status(order_id, new_status)
        if new_status == "cancelled" and previous_status != "cancelled":
            await decrement_order_count(order["user_id"])
        new_text = (f"Заказ #{order['id']} — "
                    f"{STATUS_LABELS.get(new_status, new_status)}\n"
                    f"Клиент: {order['name']} (@{order['username']}, "
                    f"ID: {order['user_id']})\n"
                    f"Тел: {order['phone']}\nАдрес: {order['address']}\n"
                    f"⏰ Время: {order['delivery_time'] or '—'}\n"
                    f"Коммент: {order['comment']}\n\n{order['items']}\n\n"
                    f"Итого: {order['total']} ₽\nСоздан: {order['created_at']}")
        await ack("Статус обновлён")
        await api.send(chat_id, new_text,
                       attachments=[manager_keyboard(order_id, new_status)])
        notify_texts = {
            "cooking": "👨‍🍳 Ваш заказ #{} уже готовится!",
            "delivering": "🛵 Ваш заказ #{} уже везёт курьер!",
            "done": "✅ Заказ #{} доставлен. Приятного аппетита!",
            "cancelled": "❌ Заказ #{} отменён. Свяжитесь с кассиром, "
                         "если это ошибка.",
        }
        if new_status in notify_texts and new_status != previous_status:
            try:
                await api.send_to_user(order["user_id"],
                                       notify_texts[new_status].format(order_id))
            except Exception:
                pass
        return

    await ack()

# ================= РОЛИ =================
def is_manager(user_id):
    return user_id == MANAGER_CHAT_ID or user_id in ADMIN_IDS

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ================= ГЛАВНЫЙ ЦИКЛ =================
async def poll_loop():
    marker = None
    log.info("Бот запущен. Long polling активен.")
    while True:
        resp = await api.get_updates(marker=marker, timeout=30)
        if resp is None:
            await asyncio.sleep(5)
            continue
        marker = resp.get("marker", marker)
        for update in resp.get("updates", []):
            ut = update.get("update_type")
            try:
                if ut == "message_created":
                    await handle_message(update.get("message") or {})
                elif ut == "message_callback":
                    await handle_callback(update)
                elif ut == "bot_started":
                    chat_id = update.get("chat_id")
                    user_id = (update.get("user") or {}).get("user_id")
                    if chat_id is None:
                        chat_id = user_id
                    if chat_id is not None:
                        existing_user = await get_user(user_id) if user_id else None
                        if existing_user and existing_user["agreed_offer"]:
                            st_clear(user_id)
                            await send_menu(chat_id)
                        else:
                            await send_offer_screen(chat_id)
                            if user_id:
                                st_set(user_id, "waiting_offer")
            except Exception:
                log.exception("Ошибка обработки обновления: %s", ut)

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН":
        raise SystemExit("Не задан MAX_BOT_TOKEN — добавьте переменную окружения "
                         "на bothost.ru (MAX_TOKEN / MAX_BOT_TOKEN).")
    await init_db()
    await api.start()
    me = await api._request("GET", "/me")
    if me:
        log.info("Токен подтверждён: бот %s", me.get("name", me))
    else:
        log.warning("Не удалось получить /me — проверьте токен, но продолжаем.")
    try:
        await poll_loop()
    finally:
        await api.stop()

if __name__ == "__main__":
    asyncio.run(main())
