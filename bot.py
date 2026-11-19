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
import certifi
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

DB_PATH = os.getenv("DB_PATH", "data/shop.db")

TZ_YANAO = timezone(timedelta(hours=5))  # ЯНАО = UTC+5

STATUS_LABELS = {
    "new": "🆕 Новый",
    "cooking": "👨‍🍳 Готовится",
    "delivering": "🛵 В доставке",
    "done": "✅ Доставлен",
    "cancelled": "❌ Отменён",
}

OFFER_TEXT = (
    "📜 **Публичная оферта**\n\n"
    "Для оформления заказа необходимо согласиться с условиями публичной оферты "
    "и обработкой персональных данных (имя, телефон, адрес).\n\n"
    "Кратко о главном:\n"
    "• Оплата — СБП, переводом по реквизитам или наличными при получении.\n"
    "• Отмена заказа — бесплатно до передачи товара курьеру.\n"
    "• Возврат за брак — в течение 10 дней, деньги тем же способом.\n"
    "• Дистанционная покупка — отказ в течение 7 дней после получения.\n"
    "• Персональные данные используются только для доставки (152-ФЗ), "
    "отзыв согласия — ZEVSNAB@yandex.ru.\n\n"
    "Полный текст оферты — по кнопке ниже или командой /offer в чате."
)

OFFER_FULL_TEXT = (
    "📜 **Публичная оферта — Котлеты и Стейк 89**\n\n"
    "**1. Общие положения.** Настоящая оферта является публичным договором "
    "(ст. 435, 437 ГК РФ). Акцепт — оформление заказа у бота.\n\n"
    "**2. Исполнитель.** ИП Зиньковский Евгений Викторович, ИНН 890603958650, "
    "ОГРНИП 325890000000641, ЯНАО, г. Муравленко. "
    "Тел: +7 996 655-26-90, e-mail: ZEVSNAB@yandex.ru.\n\n"
    "**3. Товар и цена.** Блюда по меню бота (Приложение № 1 к оферте). "
    "Цена указана за порцию и подтверждается при подтверждении заказа.\n\n"
    "**4. Заказ.** Заказ оформляется через бота: состав корзины + данные "
    "доставки (имя, телефон, адрес).\n\n"
    "**5. Доставка.** Доставка по г. Муравленко. Способ и время согласуются "
    "с кассиром.\n\n"
    "**6. Оплата.** СБП на номер +7 996 655-26-90, перевод по реквизитам "
    "(ООО «ОЗОН Банк», р/с 40802810600000705166) или наличными при получении. "
    "К каждому заказу прилагается кассовый чек.\n\n"
    "**7. Отмена заказа и возвраты.**\n"
    "7.1. Клиент может отменить заказ бесплатно до передачи товара курьеру — "
    "написав в чат бота или позвонив +7 996 655-26-90.\n"
    "7.2. При дистанционной покупке (ст. 26.1 Закона «О защите прав "
    "потребителей») клиент вправе отказаться от товара в течение 7 дней "
    "после получения, если сохранены потребительские свойства и товарный вид.\n"
    "7.3. При претензии к качеству (брак, несоответствие заказу) — замена "
    "товара, уменьшение цены или полный возврат средств в течение 10 дней.\n"
    "7.4. Деньги возвращаются тем же способом, которым была произведена "
    "оплата (СБП — на карту отправителя, наличные — наличными).\n"
    "7.5. Если клиент не принял заказ по своей вине (не отвечает, нет "
    "адреса), расходы исполнителя на доставку не возвращаются.\n\n"
    "**8. Претензии.** Телефон +7 996 655-26-90, ZEVSNAB@yandex.ru, "
    "личные сообщения канала или бота.\n\n"
    "**9. Персональные данные.** Данные (имя, телефон, адрес) обрабатываются "
    "только для исполнения заказа (152-ФЗ). Отзыв согласия: "
    "ZEVSNAB@yandex.ru. Данные хранятся не дольше, чем это необходимо.\n\n"
    "**10. Защита прав.** Клиент вправе обратиться в Роспотребнадзор или в суд.\n"
)

WELCOME_MENU_TEXT = "Добро пожаловать! Выберите категорию:"

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
                agreed_offer INTEGER DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS categories (
                key TEXT PRIMARY KEY, name TEXT NOT NULL,
                sort_order INTEGER DEFAULT 0
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS menu (
                key TEXT PRIMARY KEY, name TEXT NOT NULL,
                description TEXT DEFAULT '', price TEXT NOT NULL,
                photo_token TEXT, category_key TEXT DEFAULT 'default',
                active INTEGER DEFAULT 1
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS carts (
                user_id INTEGER, item_key TEXT, quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, item_key)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                username TEXT, name TEXT, phone TEXT, address TEXT,
                comment TEXT, items TEXT, total TEXT,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            INSERT OR IGNORE INTO categories (key, name, sort_order)
            VALUES ('default', 'Меню', 0)""")
        await db.commit()

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

async def categories_all():
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM categories ORDER BY sort_order, name") as cur:
            return await cur.fetchall()

async def category_add(key, name, sort_order=0):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT OR IGNORE INTO categories (key, name, sort_order) VALUES (?, ?, ?)",
            (key, name, sort_order))
        await db.commit()

async def menu_all(active_only=True, category_key=None):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        query, params = "SELECT * FROM menu WHERE 1=1", []
        if active_only:
            query += " AND active = 1"
        if category_key:
            query += " AND category_key = ?"
            params.append(category_key)
        query += " ORDER BY name"
        async with db.execute(query, params) as cur:
            return await cur.fetchall()

async def menu_get(key: str):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM menu WHERE key = ?", (key,)) as cur:
            return await cur.fetchone()

async def menu_add(key, name, description, price, category_key="default", photo_token=None):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO menu (key, name, description, price, category_key, photo_token) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (key, name, description, str(price), category_key, photo_token))
        await db.commit()

async def menu_update(key, field, value):
    allowed = {"name", "description", "price", "photo_token", "active", "category_key"}
    if field not in allowed:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(f"UPDATE menu SET {field} = ? WHERE key = ?", (value, key))
        await db.commit()

async def menu_delete(key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM menu WHERE key = ?", (key,))
        await db.commit()

async def cart_add(user_id, item_key):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO carts (user_id, item_key, quantity) VALUES (?, ?, 1)
            ON CONFLICT(user_id, item_key) DO UPDATE SET quantity = quantity + 1
        """, (user_id, item_key))
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

async def order_save(user_id, username, data, items_text, total):
    now = datetime.now(TZ_YANAO).strftime("%Y-%m-%d %H:%M")
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("""
            INSERT INTO orders (user_id, username, name, phone, address, comment,
                                items, total, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (user_id, username, data.get("name"), data.get("phone"),
              data.get("address"), data.get("comment"), items_text, str(total), now))
        await db.commit()
        return cur.lastrowid

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

# ================= MAX API КЛИЕНТ =================
class MaxApi:
    def __init__(self, token: str):
        self.token = token
        self.session = None
        self.send_locks = {}

    async def start(self):
        ctx = ssl.create_default_context(cafile=certifi.where())
        log.info("SSL: использую сертификаты certifi (%s)", certifi.where())
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
                log.warning("Сеть при %s %s (попытка %s): %s", method, path, attempt + 1, e)
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
        row.append(cb_btn(c["name"], f"cat:{c['key']}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    buttons.append([cb_btn("🛒 Корзина", "cart")])
    return kb(buttons)

def menu_keyboard(items, cat_key):
    buttons = [[cb_btn(f"{i['name']} — {i['price']} ₽", f"view:{i['key']}")]
               for i in items]
    buttons.append([cb_btn("⬅️ Категории", "back_categories")])
    buttons.append([cb_btn("🛒 Корзина", "cart")])
    return kb(buttons)

def item_card_keyboard(item_key, cat_key):
    return kb([
        [cb_btn("🛒 Добавить в корзину", f"add:{item_key}")],
        [cb_btn("⬅️ Назад", f"cat:{cat_key}")],
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
        [cb_btn("➕ Добавить блюдо", "admin:add")],
        [cb_btn("📋 Все блюда", "admin:list")],
        [cb_btn("🗂 Категории", "admin:cats")],
        [cb_btn("➕ Добавить категорию", "admin:cat_add")],
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
        return "Корзина пуста.", kb([[cb_btn("⬅️ В меню", "back_categories")]])

    lines, kb_buttons = [], []
    total = Decimal("0")
    for key, qty in rows:
        item = await menu_get(key)
        if not item:
            continue
        subtotal = Decimal(item["price"]) * qty
        total += subtotal
        lines.append(f"• {item['name']} ({qty} шт.) — {subtotal} ₽")
        kb_buttons.append([
            cb_btn(f"➖ {item['name'][:15]}", f"cartsub:{key}"),
            cb_btn("➕", f"cartadd:{key}"),
            cb_btn("❌", f"cartdel:{key}"),
        ])

    text = "🛒 Ваша корзина:\n" + "\n".join(lines) + f"\n\nИтого: {total} ₽"
    kb_buttons.append([cb_btn("✅ Оформить заказ", "order")])
    kb_buttons.append([cb_btn("⬅️ В меню", "back_categories")])
    return text, kb(kb_buttons)

# ================= ОБЩИЕ ЭКРАНЫ =================
async def send_menu(chat_id):
    cats = await categories_all()
    if not cats:
        await api.send(chat_id, "Меню пока пустое.")
        return
    await api.send(chat_id, "Выберите категорию:",
                   attachments=[categories_keyboard(cats)])

async def send_offer_screen(chat_id):
    buttons = [
        [link_btn("📄 Прочитать оферту", OFFER_PDF_URL)],
        [cb_btn("📜 Оферта в чате", "offer_full")],
        [cb_btn("✅ Согласен", "agree_offer")],
    ]
    await api.send(chat_id, OFFER_TEXT, attachments=[kb(buttons)])

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

    state = st_get(user_id)
    sname = state.get("state")

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
        buttons = [[cb_btn(c["name"], f"pickcat:{c['key']}")] for c in cats]
        buttons.append([cb_btn("Отмена", "admin_cancel")])
        st_set(user_id, "admin_add_category", **data)
        await api.send(chat_id, "Выберите категорию:", attachments=[kb(buttons)])
        return

    if sname == "admin_add_photo":
        photo_token = None
        if photos:
            photo_token = (photos[-1].get("payload") or {}).get("token")
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

    if sname == "admin_edit_value":
        data = st_data(user_id)
        key, field = data.get("key"), data.get("field")
        if field == "photo_token":
            if photos:
                value = (photos[-1].get("payload") or {}).get("token")
            else:
                await api.send(chat_id, "Нужно фото:")
                return
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

    if sname == "admin_cat_key" and text:
        key = text.lower().replace(" ", "_")
        if not key or not key.isascii():
            await api.send(chat_id, "Только латиница без пробелов:")
            return
        st_set(user_id, "admin_cat_name", key=key)
        await api.send(chat_id, "Название категории:")
        return

    if sname == "admin_cat_name" and text:
        if len(text) < 2:
            await api.send(chat_id, "Слишком короткое:")
            return
        data = st_data(user_id)
        await category_add(data["key"], text)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Категория «{text}» добавлена.",
                       attachments=[admin_panel_keyboard()])
        return

    if sname == "waiting_name":
        if not text or len(text) < 2:
            await api.send(chat_id, "Укажите имя (мин. 2 символа):")
            return
        data = st_data(user_id)
        data["name"] = text
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
        st_set(user_id, "waiting_address", **data)
        await api.send(chat_id, "Укажите адрес доставки:")
        return

    if sname == "waiting_address":
        if not text or len(text) < 5:
            await api.send(chat_id, "Адрес слишком короткий:")
            return
        data = st_data(user_id)
        data["address"] = text
        st_set(user_id, "waiting_comment", **data)
        buttons = [[cb_btn("Пропустить", "skip_comment")]]
        await api.send(chat_id, "Комментарий к заказу (или «Пропустить»):",
                       attachments=[kb(buttons)])
        return

    if sname == "waiting_comment":
        data = st_data(user_id)
        data["comment"] = text
        st_set(user_id, "waiting_comment", **data)
        await finalize_order(chat_id, user_id, username)
        return

    if text.startswith("/offer"):
        await api.send(chat_id, OFFER_FULL_TEXT,
                       attachments=[kb([[link_btn("📄 Открыть PDF", OFFER_PDF_URL)]])])
        return

    if text.startswith("/start"):
        user = await get_user(user_id)
        if not user or user["agreed_offer"] == 0:
            await send_offer_screen(chat_id)
            st_set(user_id, "waiting_offer")
            return
        st_clear(user_id)
        await send_menu(chat_id)
        return

    if text.startswith("/menu"):
        user = await get_user(user_id)
        if user and user["agreed_offer"]:
            st_clear(user_id)
            await send_menu(chat_id)
        return

    if not sname:
        user = await get_user(user_id)
        if user and user["agreed_offer"]:
            await api.send(chat_id, "Выберите категорию через /menu или нажмите "
                                    "кнопку под сообщением.")
        else:
            await send_offer_screen(chat_id)
            st_set(user_id, "waiting_offer")

# ================= ЗАКАЗ =================
async def finalize_order(chat_id, user_id, username):
    data = st_data(user_id)
    rows = await cart_get(user_id)
    if not rows:
        await api.send(chat_id, "Корзина пуста. Начните заново: /start")
        st_clear(user_id)
        return

    await save_user(user_id, data.get("name", "—"),
                    data.get("phone", "—"), data.get("address", "—"))

    lines, total = [], Decimal("0")
    missing = []
    for key, qty in rows:
        item = await menu_get(key)
        if not item or not item["active"]:
            missing.append(key)
            continue
        subtotal = Decimal(item["price"]) * qty
        total += subtotal
        lines.append(f"• {item['name']} ({qty} шт.) — {subtotal} ₽")
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
    order_id = await order_save(user_id, username or "—", data, items_text, total)

    manager_text = (
        f"🆕 Заказ #{order_id}\n"
        f"Клиент: @{username or '—'} (ID: {user_id})\n"
        f"Имя: {data.get('name')}\n"
        f"Тел: {data.get('phone')}\n"
        f"Адрес: {data.get('address')}\n"
        f"Коммент: {data.get('comment') or '—'}\n\n"
        f"{items_text}\n\nИтого: {total} ₽"
    )

    manager_sent = True
    sent = await api.send_to_user(MANAGER_CHAT_ID, manager_text,
                                  attachments=[manager_keyboard(order_id, "new")])
    if sent is None:
        manager_sent = False

    user_text = (
        f"✅ Ваш заказ **#{order_id}** успешно оформлен!\n\n"
        f"Имя: {data.get('name')}\n"
        f"Адрес: {data.get('address')}\n"
        f"{items_text}\n\n"
        f"💰 **Итого: {total} ₽**\n\n"
        f"📌 **Для оплаты:**\n"
        f"Напишите кассиру номер заказа (**#{order_id}**) и оплатите по СБП "
        f"на +7 996 655-26-90. Кнопка ниже — чат с кассиром."
    )
    buttons = [[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]]
    await api.send(chat_id, user_text, attachments=[kb(buttons)])

    if not manager_sent:
        await api.send_to_user(
            MANAGER_CHAT_ID,
            "⚠️ Внимание: заказ не ушёл автоматически. Чек от клиента:\n\n"
            + manager_text)
    await cart_clear(user_id)
    st_clear(user_id)

# ================= ОБРАБОТКА CALLBACK =================
async def handle_callback(update):
    cb = (update.get("callback") or {})
    callback_id = cb.get("callback_id")
    payload = cb.get("payload", "")
    user = (cb.get("user") or {})
    user_id = user.get("user_id")
    msg = (cb.get("message") or {})
    chat_id = ((msg.get("recipient") or {}).get("chat_id")) or user_id

    async def ack(notification=None):
        if callback_id:
            await api.answer_callback(callback_id, notification)

    if not user_id or chat_id is None:
        return

    if payload == "offer_full":
        await ack()
        await api.send(chat_id, OFFER_FULL_TEXT,
                       attachments=[kb([[link_btn("📄 Открыть PDF", OFFER_PDF_URL)],
                                        [cb_btn("✅ Согласен", "agree_offer")]])])
        return

    if payload == "agree_offer":
        st = st_get(user_id)
        if st.get("state") == "waiting_offer":
            await set_agreed(user_id)
            st_clear(user_id)
            await ack()
            await api.send(chat_id, WELCOME_MENU_TEXT)
            await send_menu(chat_id)
        else:
            await ack("Сначала прочитайте оферту: /start")
        return

    user = await get_user(user_id)
    if not user or user["agreed_offer"] == 0:
        await ack("Сначала согласитесь с офертой: /start")
        return

    if payload.startswith("cat:"):
        cat_key = payload.split(":")[1]
        items = await menu_all(category_key=cat_key)
        if not items:
            await ack("Пусто")
            return
        await ack()
        await api.send(chat_id, "Выберите блюдо:",
                       attachments=[menu_keyboard(items, cat_key)])
        return

    if payload == "back_categories":
        await ack()
        await send_menu(chat_id)
        return

    if payload.startswith("view:"):
        key = payload.split(":")[1]
        item = await menu_get(key)
        if not item or not item["active"]:
            await ack("Блюдо недоступно")
            return
        desc = item["description"] if item["description"] else "Без описания"
        text = f"**{item['name']}**\n\n{desc}\n\n💰 **Цена: {item['price']} ₽**"
        attachments = [item_card_keyboard(key, item["category_key"])]
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
        await cart_add(user_id, key)
        await ack(f"✅ {item['name']} добавлен в корзину!")
        return

    if payload == "cart":
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartsub:"):
        await cart_remove_one(user_id, payload.split(":")[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartadd:"):
        await cart_add(user_id, payload.split(":")[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload.startswith("cartdel:"):
        await cart_delete_item(user_id, payload.split(":")[1])
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload == "order":
        rows = await cart_get(user_id)
        if not rows:
            await ack("Корзина пуста")
            return
        if user["name"] and user["phone"] and user["address"]:
            text = (
                "Ваши данные для доставки:\n\n"
                f"👤 Имя: {user['name']}\n"
                f"📞 Тел: {user['phone']}\n"
                f"📍 Адрес: {user['address']}\n\n"
                "Все верно?"
            )
            buttons = [
                [cb_btn("✅ Да, оформить", "confirm_yes")],
                [cb_btn("✏️ Изменить данные", "confirm_no")],
            ]
            st_set(user_id, "confirm_data")
            await ack()
            await api.send(chat_id, text, attachments=[kb(buttons)])
        else:
            st_set(user_id, "waiting_name")
            await ack()
            await api.send(chat_id, "Как вас зовут?")
        return

    if payload == "confirm_yes":
        data = st_data(user_id)
        data["name"], data["phone"], data["address"] = (
            user["name"], user["phone"], user["address"])
        st_set(user_id, "waiting_comment", **data)
        await ack()
        await api.send(chat_id, "Комментарий к заказу (или «Пропустить»):",
                       attachments=[kb([[cb_btn("Пропустить", "skip_comment")]])])
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
            text = (f"🔑 {item['key']}\n📛 {item['name']}\n"
                    f"📝 {item['description'] or '—'}\n"
                    f"💰 {item['price']} ₽\n🗂 {item['category_key']}\n"
                    f"Статус: {'активно' if item['active'] else 'скрыто'}")
            attachments = [admin_item_keyboard(item["key"], item["active"])]
            if item["photo_token"]:
                attachments.insert(0, photo_attachment(item["photo_token"]))
            await api.send(chat_id, text, attachments=attachments)
        return

    if payload == "admin:cats" and is_admin(user_id):
        cats = await categories_all()
        text = "Категории:\n" + "\n".join(f"• {c['key']} — {c['name']}" for c in cats)
        await ack()
        await api.send(chat_id, text)
        return

    if payload == "admin:cat_add" and is_admin(user_id):
        st_set(user_id, "admin_cat_key")
        await ack()
        await api.send(chat_id, "Ключ категории (латиницей):")
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
            buttons = [[cb_btn(c["name"], f"setcat:{key}:{c['key']}")] for c in cats]
            buttons.append([cb_btn("Отмена", "admin_cancel")])
            await ack()
            await api.send(chat_id, "Выберите категорию:", attachments=[kb(buttons)])
            return
        st_set(user_id, "admin_edit_value", key=key, field=field)
        prompts = {"name": "Новое название:", "price": "Новая цена:",
                   "description": "Новое описание:", "photo_token": "Пришлите новое фото:"}
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

    if payload.startswith("mgr:") and is_manager(user_id):
        status = payload.split(":")[1]
        orders = await orders_by_status(status, limit=10)
        if not orders:
            await ack(f"{STATUS_LABELS.get(status, status)}: заказов нет.")
            return
        await ack()
        for o in orders:
            text = (f"Заказ #{o['id']} — {STATUS_LABELS.get(o['status'], o['status'])}\n"
                    f"Клиент: {o['name']} (@{o['username']}, ID: {o['user_id']})\n"
                    f"Тел: {o['phone']}\nАдрес: {o['address']}\n"
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
        await order_set_status(order_id, new_status)
        new_text = (f"Заказ #{order['id']} — "
                    f"{STATUS_LABELS.get(new_status, new_status)}\n"
                    f"Клиент: {order['name']} (@{order['username']}, "
                    f"ID: {order['user_id']})\n"
                    f"Тел: {order['phone']}\nАдрес: {order['address']}\n"
                    f"Коммент: {order['comment']}\n\n{order['items']}\n\n"
                    f"Итого: {order['total']} ₽\nСоздан: {order['created_at']}")
        await ack("Статус обновлён")
        await api.send(chat_id, new_text,
                       attachments=[manager_keyboard(order_id, new_status)])
        try:
            await api.send_to_user(
                order["user_id"],
                f"Статус заказа #{order_id} изменён: "
                f"{STATUS_LABELS.get(new_status, new_status)}")
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
                    user_id = ((update.get("user") or {}).get("user_id"))
                    if chat_id is None:
                        chat_id = user_id
                    if chat_id is not None:
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
