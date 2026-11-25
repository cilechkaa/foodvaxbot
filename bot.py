# -*- coding: utf-8 -*-
"""
КОТЛЕТЫ И СТЕЙК 89 — бот для мессенджера MAX
Long polling на aiohttp, база SQLite (aiosqlite).
Переменные окружения: MAX_BOT_TOKEN (или MAX_TOKEN) — токен бота;
MANAGER_CHAT_ID — числовой ID кассира (можно задать в админ-панели);
ADMIN_IDS — ID администраторов через запятую.
"""
import asyncio
import logging
import os
import ssl
import sqlite3
import io
import re
import json
import uuid
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from openpyxl import load_workbook
from decimal import Decimal, InvalidOperation

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("kotleta89")

# ================= НАСТРОЙКИ =================
BOT_TOKEN = os.getenv("MAX_BOT_TOKEN") or os.getenv("MAX_TOKEN") or "ВСТАВЬ_ТОКЕН"
API_BASE = "https://platform-api2.max.ru"
MANAGER_USERNAME = "https://max.ru/u/f9LHodD0cOIIewxP4aChMb6ZLuuMWIITz-zSiBLo97JIL3CrV_li1dpy81g"
MANAGER_CHAT_ID = int(os.getenv("MANAGER_CHAT_ID", "313053726"))

def _parse_ids(raw):
    result = set()
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            result.add(int(part))
    return result

ADMIN_IDS = _parse_ids(os.getenv("ADMIN_IDS", "313053726")) or {313053726}
ADMIN_IDS.add(313053726)

OFFER_PDF_URL = "https://disk.yandex.ru/i/YWhP_Tsg3c2Mxw"

DISCOUNT_EVERY_N_ORDERS = 5
DISCOUNT_PERCENT = Decimal("50")
MIN_ORDER_AMOUNT = Decimal("900")
FREE_DELIVERY_FROM = Decimal("1000")
DELIVERY_COST = Decimal("200")

DB_PATH = os.getenv("DB_PATH", "data/shop.db")

# ================= ЮKASSA: НАСТРОЙКИ =================
# Задаются переменными окружения на bothost.ru:
#   YOOKASSA_SHOP_ID    — идентификатор магазина (shopId) из личного кабинета ЮKassa
#   YOOKASSA_SECRET_KEY — секретный ключ из кабинета ЮKassa (Настройки → Ключи API)
# Пока переменные не заданы, бот работает в прежнем режиме (QR-код / кассир).
YOOKASSA_SHOP_ID = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_API_URL = "https://api.yookassa.ru/v3"
YOOKASSA_POLL_INTERVAL = 20      # секунд между проверками статуса платежа
YOOKASSA_WATCH_HOURS = 12        # сколько часов бот следит за платежом
YOOKASSA_NAME = "СВОЯ КУХНЯ 89"

def yookassa_configured():
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY)

TZ_YANAO = timezone(timedelta(hours=5))  # ЯНАО = UTC+5

STATUS_LABELS = {
    "new": "🆕 Ожидает оплаты",
    "paid": "💳 Оплачен (ЮKassa)",
    "cooking": "👨‍🍳 Готовится",
    "delivering": "🛵 В доставке",
    "done": "✅ Доставлен",
    "cancelled": "❌ Отменён",
}

GARNISH_CATEGORIES = {"hot_dishes", "schnitzels", "steaks"}
GARNISH_LABELS = {"fries": "🍟 Картофель фри", "rice": "🍚 Отварной рис"}

# ================= СОСТОЯНИЯ (FSM в SQLite) =================
# Кэш сохраняет прежний синхронный интерфейс st_get/st_set/st_clear,
# а SQLite обеспечивает восстановление незавершённых действий после перезапуска.
user_states = {}

def _state_user_key(user_id):
    return str(user_id)

def _state_db_write(user_id, state_data):
    """Синхронно сохраняет одно состояние; вызывается из прежнего FSM-интерфейса."""
    try:
        os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute(
                """INSERT INTO user_states (user_id, state, data, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(user_id) DO UPDATE SET
                       state=excluded.state,
                       data=excluded.data,
                       updated_at=CURRENT_TIMESTAMP""",
                (_state_user_key(user_id), state_data.get("state"),
                 json.dumps(state_data.get("data") or {}, ensure_ascii=False))
            )
    except Exception:
        # Не ломаем обработку сообщения, если временно недоступна запись состояния.
        log.exception("Не удалось сохранить FSM-состояние пользователя %s", user_id)

def _state_db_delete(user_id):
    try:
        with sqlite3.connect(DB_PATH, timeout=10) as db:
            db.execute("DELETE FROM user_states WHERE user_id = ?",
                       (_state_user_key(user_id),))
    except Exception:
        log.exception("Не удалось удалить FSM-состояние пользователя %s", user_id)

def st_get(user_id):
    return user_states.get(user_id, {"state": None, "data": {}})

def st_set(user_id, state, **data):
    state_data = {"state": state, "data": data}
    user_states[user_id] = state_data
    _state_db_write(user_id, state_data)

def st_data(user_id):
    return st_get(user_id).get("data", {})

def st_clear(user_id):
    user_states.pop(user_id, None)
    _state_db_delete(user_id)

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
            CREATE TABLE IF NOT EXISTS garnish_choices (
                user_id INTEGER NOT NULL, prompt_id TEXT NOT NULL,
                item_key TEXT NOT NULL, garnish TEXT NOT NULL,
                PRIMARY KEY (user_id, prompt_id)
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS orders (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
                username TEXT, name TEXT, phone TEXT, address TEXT,
                comment TEXT, delivery_time TEXT, items TEXT, total TEXT,
                status TEXT DEFAULT 'new',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        try:
            # ID платежа ЮKassa (для обновления со старой базой)
            await db.execute("ALTER TABLE orders ADD COLUMN payment_id TEXT")
            await db.commit()
        except Exception:
            await db.rollback()
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
        await db.execute("""
            CREATE TABLE IF NOT EXISTS staff_chats (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_chats (
                user_id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_addresses (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                address TEXT NOT NULL, is_default INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_states (
                user_id TEXT PRIMARY KEY,
                state TEXT,
                data TEXT NOT NULL DEFAULT '{}',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS dish_ratings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                order_id INTEGER NOT NULL,
                item_key TEXT NOT NULL,
                rating INTEGER NOT NULL CHECK(rating BETWEEN 1 AND 5),
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, order_id, item_key)
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
        await db.execute("""
            INSERT INTO user_addresses (user_id, address, is_default)
            SELECT u.user_id, u.address, 1 FROM users u
            WHERE u.address IS NOT NULL AND TRIM(u.address) != '' AND TRIM(u.address) != '—'
              AND NOT EXISTS (SELECT 1 FROM user_addresses a WHERE a.user_id=u.user_id)
        """)
        await db.commit()

        # Восстанавливаем незавершённые действия после перезапуска процесса.
        user_states.clear()
        async with db.execute("SELECT user_id, state, data FROM user_states") as cur:
            saved_states = await cur.fetchall()
        for saved_user_id, saved_state, saved_data in saved_states:
            try:
                cache_user_id = (int(saved_user_id)
                                 if str(saved_user_id).isdigit()
                                 else saved_user_id)
                parsed_data = json.loads(saved_data or "{}")
                if not isinstance(parsed_data, dict):
                    parsed_data = {}
                user_states[cache_user_id] = {
                    "state": saved_state,
                    "data": parsed_data,
                }
            except (TypeError, ValueError, json.JSONDecodeError):
                log.warning("Пропущено повреждённое FSM-состояние user_id=%s",
                            saved_user_id)

        async with db.execute("SELECT value FROM settings WHERE key = 'manager_chat_id'") as cur:
            manager_row = await cur.fetchone()
        if manager_row and str(manager_row[0]).isdigit():
            global MANAGER_CHAT_ID
            MANAGER_CHAT_ID = int(manager_row[0])

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
        # Поддерживаем адресную книгу синхронно с основным адресом профиля.
        clean_address = str(address or "").strip()
        if clean_address and clean_address != "—":
            async with db.execute(
                "SELECT id FROM user_addresses WHERE user_id=? AND address=?",
                (user_id, clean_address)
            ) as cur:
                found = await cur.fetchone()
            if found:
                await db.execute("UPDATE user_addresses SET is_default=0 WHERE user_id=?", (user_id,))
                await db.execute("UPDATE user_addresses SET is_default=1 WHERE id=?", (found[0],))
            else:
                async with db.execute("SELECT COUNT(*) FROM user_addresses WHERE user_id=?", (user_id,)) as cur:
                    count = (await cur.fetchone())[0]
                await db.execute(
                    "INSERT INTO user_addresses (user_id,address,is_default) VALUES (?,?,1)",
                    (user_id, clean_address)
                )
                await db.execute("UPDATE user_addresses SET is_default=0 WHERE user_id=? AND id != last_insert_rowid()", (user_id,))
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


async def remember_user_chat(user_id: int, chat_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO user_chats (user_id, chat_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET
            chat_id=excluded.chat_id, updated_at=CURRENT_TIMESTAMP""",
            (int(user_id), int(chat_id)))
        await db.commit()
    await remember_staff_chat(user_id, chat_id)


async def remember_staff_chat(user_id: int, chat_id: int):
    if user_id not in ADMIN_IDS and user_id != MANAGER_CHAT_ID:
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""INSERT INTO staff_chats (user_id, chat_id, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP) ON CONFLICT(user_id) DO UPDATE SET
            chat_id=excluded.chat_id, updated_at=CURRENT_TIMESTAMP""",
            (int(user_id), int(chat_id)))
        await db.commit()
    log.info("Сохранён личный чат сотрудника user_id=%s chat_id=%s", user_id, chat_id)

async def staff_chat_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM staff_chats WHERE user_id=?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None

async def user_chat_id(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT chat_id FROM user_chats WHERE user_id=?", (int(user_id),)) as cur:
            row = await cur.fetchone()
            return int(row[0]) if row else None

async def addresses_get(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM user_addresses WHERE user_id=? ORDER BY is_default DESC, id DESC", (user_id,)) as cur:
            return await cur.fetchall()

async def address_add(user_id: int, address: str):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT COUNT(*) FROM user_addresses WHERE user_id=?", (user_id,)) as cur:
            count=(await cur.fetchone())[0]
        await db.execute("INSERT INTO user_addresses (user_id,address,is_default) VALUES (?,?,?)", (user_id,address,1 if count==0 else 0))
        await db.commit()

async def address_update(user_id: int, address_id: int, address: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_addresses SET address=? WHERE id=? AND user_id=?", (address,address_id,user_id))
        async with db.execute("SELECT address,is_default FROM user_addresses WHERE id=? AND user_id=?", (address_id,user_id)) as cur:
            row=await cur.fetchone()
        if row and row[1]:
            await db.execute("UPDATE users SET address=? WHERE user_id=?", (row[0],user_id))
        await db.commit()

async def address_delete(user_id: int, address_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM user_addresses WHERE id=? AND user_id=?", (address_id,user_id))
        async with db.execute("SELECT id,address FROM user_addresses WHERE user_id=? AND is_default=1 LIMIT 1", (user_id,)) as cur:
            row=await cur.fetchone()
        if not row:
            async with db.execute("SELECT id,address FROM user_addresses WHERE user_id=? ORDER BY id DESC LIMIT 1", (user_id,)) as cur:
                row=await cur.fetchone()
            if row:
                await db.execute("UPDATE user_addresses SET is_default=0 WHERE user_id=?", (user_id,))
                await db.execute("UPDATE user_addresses SET is_default=1 WHERE id=?", (row[0],))
        if row:
            await db.execute("UPDATE users SET address=? WHERE user_id=?", (row[1],user_id))
        else:
            await db.execute("UPDATE users SET address=NULL WHERE user_id=?", (user_id,))
        await db.commit()

async def address_set_default(user_id: int, address_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE user_addresses SET is_default=0 WHERE user_id=?", (user_id,))
        await db.execute("UPDATE user_addresses SET is_default=1 WHERE id=? AND user_id=?", (address_id,user_id))
        async with db.execute("SELECT address FROM user_addresses WHERE id=? AND user_id=?", (address_id,user_id)) as cur:
            row=await cur.fetchone()
        if row:
            await db.execute("UPDATE users SET address=? WHERE user_id=?", (row[0],user_id))
        await db.commit()

async def set_setting(key: str, value: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                         (key, value))
        await db.commit()


# ================= ЮKASSA: API =================
_yk_session = None

async def _yk_http():
    global _yk_session
    if _yk_session is None or _yk_session.closed:
        _yk_session = aiohttp.ClientSession()
    return _yk_session

def _yk_phone(raw):
    """Приводит телефон к виду +7XXXXXXXXXX или возвращает None."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] == "8":
        digits = "7" + digits[1:]
    if len(digits) == 10:
        digits = "7" + digits
    if len(digits) == 11 and digits[0] == "7":
        return "+" + digits
    return None

def _money(value):
    return str(Decimal(value).quantize(Decimal("0.01")))

def _yk_build_receipt(receipt_lines, discount_rate, delivery_cost, final_total):
    """Собирает позиции чека 54-ФЗ: каждое блюдо (со скидкой) + доставка.
    Сумма позиций чека обязана совпадать с суммой платежа, поэтому последняя
    крупная позиция корректируется на копейки округления."""
    if not receipt_lines:
        return None
    sums = []
    for name, qty, price in receipt_lines:
        sums.append((price * qty * (Decimal("1") - discount_rate))
                    .quantize(Decimal("0.01")))
    expected_goods = final_total - delivery_cost
    diff = (expected_goods - sum(sums)).quantize(Decimal("0.01"))
    if diff != 0 and max(sums) + diff > 0:
        sums[sums.index(max(sums))] += diff
    items = []
    for (name, qty, price), value in zip(receipt_lines, sums):
        if value <= 0:
            # Не удалось корректно распределить — отдаём заказ одной позицией.
            return [{
                "description": f"Заказ — {YOOKASSA_NAME}",
                "quantity": "1.00",
                "amount": {"value": _money(final_total), "currency": "RUB"},
                "payment_mode": "full_prepayment",
                "payment_subject": "commodity",
                "vat_code": 1,
            }]
        items.append({
            "description": str(name)[:128],
            "quantity": f"{qty:.2f}",
            "amount": {"value": _money(value), "currency": "RUB"},
            "payment_mode": "full_prepayment",
            "payment_subject": "commodity",
            "vat_code": 1,  # без НДС
        })
    if delivery_cost > 0:
        items.append({
            "description": "Доставка заказа",
            "quantity": "1.00",
            "amount": {"value": _money(delivery_cost), "currency": "RUB"},
            "payment_mode": "full_prepayment",
            "payment_subject": "service",
            "vat_code": 1,
        })
    return items

async def yookassa_create_payment(order_id, amount, description, receipt_items, phone=None):
    """Создаёт платёж в ЮKassa. Возвращает dict платежа либо None при ошибке."""
    if not yookassa_configured():
        return None
    body = {
        "amount": {"value": _money(amount), "currency": "RUB"},
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": "https://max.ru"},
        "description": description[:128],
        "metadata": {"order_id": str(order_id)},
    }
    if receipt_items:
        receipt = {"items": receipt_items, "taxation_system_code": 6}  # 6 = АУСН
        ph = _yk_phone(phone)
        if ph:
            receipt["customer"] = {"phone": ph}
        body["receipt"] = receipt
    headers = {"Idempotence-Key": uuid.uuid4().hex, "Content-Type": "application/json"}
    try:
        session = await _yk_http()
        async with session.post(
            f"{YOOKASSA_API_URL}/payments",
            json=body,
            headers=headers,
            auth=aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            timeout=aiohttp.ClientTimeout(total=30),
        ) as resp:
            data = await resp.json(content_type=None)
        if resp.status in (200, 201) and data.get("id"):
            return data
        log.error("ЮKassa: не удалось создать платёж для заказа #%s: %s %s",
                  order_id, resp.status, data)
        return None
    except Exception:
        log.exception("ЮKassa: ошибка создания платежа для заказа #%s", order_id)
        return None

async def yookassa_get_payment(payment_id):
    """Возвращает актуальный статус платежа из ЮKassa либо None."""
    try:
        session = await _yk_http()
        async with session.get(
            f"{YOOKASSA_API_URL}/payments/{payment_id}",
            auth=aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
        return data if resp.status == 200 else None
    except Exception:
        log.exception("ЮKassa: ошибка запроса платежа %s", payment_id)
        return None

async def yookassa_cancel_payment(payment_id):
    try:
        session = await _yk_http()
        async with session.post(
            f"{YOOKASSA_API_URL}/payments/{payment_id}/cancel",
            headers={"Idempotence-Key": uuid.uuid4().hex},
            auth=aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY),
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            return resp.status == 200
    except Exception:
        log.exception("ЮKassa: не удалось отменить платёж %s", payment_id)
        return False

async def _yk_cancel_after_cancel_order(payment_id, order_id):
    """Если клиент отменил заказ до оплаты — отменяем и платёж в ЮKassa."""
    payment = await yookassa_get_payment(payment_id)
    if payment and payment.get("status") == "pending":
        ok = await yookassa_cancel_payment(payment_id)
        log.info("ЮKassa: платёж %s отменённого заказа #%s — %s",
                 payment_id, order_id, "отменён" if ok else "не отменён")

# ================= ЮKASSA: ХРАНЕНИЕ ПЛАТЕЖА В БАЗЕ =================
async def order_set_payment_id(order_id, payment_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE orders SET payment_id = ? WHERE id = ?",
                         (payment_id, order_id))
        await db.commit()

async def orders_pending_yookassa():
    """Заказы, по которым есть платёж ЮKassa и оплата ещё не подтверждена."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT id, payment_id FROM orders "
            "WHERE status = 'new' AND payment_id IS NOT NULL"
        ) as cur:
            return await cur.fetchall()

# ================= ЮKASSA: ФОНОВЫЙ КОНТРОЛЬ ОПЛАТЫ =================
_yk_watching = set()

async def _yk_notify_paid(order_id):
    order = await order_get(order_id)
    if not order:
        return
    await api.notify_staff(
        f"💳 Заказ #{order_id} оплачен через ЮKassa!\n"
        f"Клиент: {order['name']} (@{order['username']}, ID: {order['user_id']})\n"
        f"Сумма: {order['total']} ₽\n\n"
        "Заказ можно передавать на кухню.",
        attachments=[manager_keyboard(order_id, "paid")])
    try:
        await api.send_to_user(
            order["user_id"],
            f"💳 Оплата заказа #{order_id} подтверждена!\n"
            "Кассир получил уведомление — начнём готовить. Спасибо!")
    except Exception:
        log.exception("Не удалось уведомить клиента об оплате заказа #%s", order_id)

async def _yk_watch(order_id, payment_id):
    """Раз в YOOKASSA_POLL_INTERVAL секунд проверяет статус платежа в ЮKassa.
    При успехе сам переводит заказ в статус «оплачен» и уведомляет клиента и кассира.
    Переживает перезапуск бота: незавершённые платежи подхватываются в main()."""
    if order_id in _yk_watching:
        return
    _yk_watching.add(order_id)
    try:
        deadline = asyncio.get_event_loop().time() + YOOKASSA_WATCH_HOURS * 3600
        while asyncio.get_event_loop().time() < deadline:
            await asyncio.sleep(YOOKASSA_POLL_INTERVAL)
            payment = await yookassa_get_payment(payment_id)
            if not payment:
                continue
            status = payment.get("status")
            if status == "succeeded":
                order = await order_get(order_id)
                if order and order["status"] == "new":
                    await order_set_status(order_id, "paid")
                    log.info("ЮKassa: заказ #%s оплачен (платёж %s)", order_id, payment_id)
                    await _yk_notify_paid(order_id)
                return
            if status == "canceled":
                order = await order_get(order_id)
                if order and order["status"] == "new":
                    try:
                        await api.send_to_user(
                            order["user_id"],
                            f"❌ Платёж по заказу #{order_id} не завершён или отменён.\n"
                            "Оформите заказ заново или напишите кассиру.",
                            attachments=[kb([[cb_btn("💬 Написать кассиру", MANAGER_USERNAME)]])])
                    except Exception:
                        log.exception("Не удалось уведомить об отмене платежа #%s", order_id)
                return
        log.info("ЮKassa: наблюдение за платежом %s заказа #%s истекло", payment_id, order_id)
    finally:
        _yk_watching.discard(order_id)

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
    """Создаёт категорию или обновляет её отображаемое название."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT INTO categories (key, name, display_name, sort_order) VALUES (?, ?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                name = excluded.name,
                display_name = COALESCE(NULLIF(excluded.display_name, ''),
                                        NULLIF(categories.display_name, ''), excluded.name)
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


async def category_requires_garnish(category_key: str) -> bool:
    """Гарнир нужен только для категорий из GARNISH_CATEGORIES."""
    return category_key in GARNISH_CATEGORIES

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

async def menu_delete_all():
    """Удаляет блюда и связанные пользовательские корзины, сохраняя историю."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM menu")
        await db.execute("DELETE FROM carts")
        await db.execute("DELETE FROM cart_garnish")
        await db.execute("DELETE FROM favorites")
        await db.execute("DELETE FROM garnish_choices")
        await db.commit()

async def register_garnish_choice(user_id, prompt_id, item_key, garnish):
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "INSERT OR IGNORE INTO garnish_choices(user_id, prompt_id, item_key, garnish) "
            "VALUES (?, ?, ?, ?)", (user_id, str(prompt_id), item_key, garnish))
        await db.commit()
        return cur.rowcount == 1

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

async def order_cancel_if_allowed(order_id, user_id):
    """Атомарно отменяет заказ клиента только до начала приготовления."""
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE orders SET status = 'cancelled' "
            "WHERE id = ? AND user_id = ? AND status IN ('new', 'paid')",
            (order_id, user_id))
        await db.commit()
        return cur.rowcount == 1

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

async def dish_rating_summary(item_key):
    """Средняя оценка и число оценок блюда."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT AVG(rating), COUNT(*) FROM dish_ratings WHERE item_key=?",
            (item_key,)) as cur:
            row = await cur.fetchone()
    return (float(row[0]) if row and row[0] is not None else None,
            int(row[1]) if row else 0)


async def order_rating_items(order_id):
    """Уникальные блюда из заказа, для которых можно запросить оценку."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT oi.item_key, MAX(m.name) AS name "
            "FROM order_items oi LEFT JOIN menu m ON m.key=oi.item_key "
            "WHERE oi.order_id=? GROUP BY oi.item_key ORDER BY name",
            (order_id,)) as cur:
            return await cur.fetchall()


async def dish_user_rating(user_id, order_id, item_key):
    """Возвращает ранее сохранённую оценку пользователя за блюдо в заказе."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT rating FROM dish_ratings WHERE user_id=? AND order_id=? AND item_key=?",
            (user_id, order_id, item_key)) as cur:
            row = await cur.fetchone()
    return int(row[0]) if row else None


async def save_dish_rating(user_id, order_id, item_key, rating):
    """Сохраняет/обновляет оценку только за блюдо из доставленного заказа."""
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute(
            "SELECT 1 FROM orders WHERE id=? AND user_id=? AND status='done'",
            (order_id, user_id)) as cur:
            order = await cur.fetchone()
        if not order:
            return False
        async with db.execute(
            "SELECT 1 FROM order_items WHERE order_id=? AND item_key=? LIMIT 1",
            (order_id, item_key)) as cur:
            purchased = await cur.fetchone()
        if not purchased:
            return False
        await db.execute(
            "INSERT INTO dish_ratings(user_id,order_id,item_key,rating) VALUES(?,?,?,?) "
            "ON CONFLICT(user_id,order_id,item_key) DO UPDATE SET "
            "rating=excluded.rating, created_at=CURRENT_TIMESTAMP",
            (user_id, order_id, item_key, rating))
        await db.commit()
        return True


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
        result = await self._request("GET", "/updates", params=params)
        if result is not None and not isinstance(result, dict):
            log.error("MAX /updates вернул неожиданный формат: %r", result)
        return result

    async def answer_callback(self, callback_id, notification=None):
        # MAX требует непустое поле message или notification даже для тихого ack.
        # Пробел используется как нейтральное уведомление, чтобы не показывать
        # пользователю лишний текст при обычном нажатии кнопки.
        body = {"notification": str(notification) if notification else " "}
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
        # В MAX user_id не равен chat_id личного диалога.
        target_chat_id = await user_chat_id(int(user_id))
        if target_chat_id is None:
            log.error("Нет сохранённого личного chat_id для user_id=%s. Пользователь должен открыть бота и отправить /start.", user_id)
            return None
        return await self.send(target_chat_id, text, attachments)

    async def notify_staff(self, text, attachments=None):
        recipients = set(ADMIN_IDS) | {313053726}
        if MANAGER_CHAT_ID:
            recipients.add(int(MANAGER_CHAT_ID))
        results = {}
        for recipient_id in sorted(recipients):
            try:
                result = await self.send_to_user(recipient_id, text, attachments)
                results[recipient_id] = result is not None
            except Exception:
                log.exception("Ошибка уведомления сотрудника user_id=%s", recipient_id)
                results[recipient_id] = False
        if not any(results.values()):
            log.critical("Заказ не доставлен ни одному сотруднику. Кассир %s должен открыть бота и отправить /start.", MANAGER_CHAT_ID)
        return any(results.values())

    async def edit(self, chat_id, message_id, text, attachments=None):
        # MAX редактирует сообщение через PUT /messages?message_id=..., а не
        # через PUT /messages/{message_id}. chat_id для этого метода не нужен.
        body = {"text": text or " ", "format": "markdown"}
        if attachments is not None:
            body["attachments"] = attachments
        data = await self._request("PUT", "/messages",
                                   params={"message_id": message_id},
                                   json_body=body)
        return data is not None

    async def get_upload_url(self, token_type: str):
        # По актуальному Bot API MAX URL для загрузки получается через POST /uploads.
        return await self._request("POST", "/uploads",
                                   params={"type": token_type})

    async def download_bytes(self, url):
        """Скачивает вложение MAX по URL и возвращает его содержимое."""
        if not url:
            return None
        try:
            async with self.session.get(
                    url, timeout=aiohttp.ClientTimeout(total=30),
                    ssl=False) as resp:
                if resp.status != 200:
                    log.error("Скачивание файла не удалось: %s -> %s",
                              url, resp.status)
                    return None
                return await resp.read()
        except (aiohttp.ClientError, asyncio.TimeoutError):
            log.exception("Ошибка скачивания файла %s", url)
            return None

    async def upload_bytes(self, data: bytes, token_type: str = "image",
                           filename: str = "photo.jpg"):
        """Загружает файл в MAX через multipart/form-data и возвращает токен."""
        info = await self.get_upload_url(token_type)
        if not info or not info.get("url"):
            log.error("MAX не выдал URL загрузки для типа %s: %r", token_type, info)
            return None
        try:
            form = aiohttp.FormData()
            form.add_field("data", data, filename=filename,
                           content_type="application/octet-stream")
            async with self.session.post(info["url"], data=form, ssl=False) as resp:
                response_text = await resp.text()
                if resp.status >= 400:
                    log.error("MAX upload вернул %s: %s", resp.status,
                              response_text[:1000])
                    return None
                try:
                    result = await resp.json(content_type=None)
                except Exception:
                    result = {}
                token = (result or {}).get("token")
                if not token:
                    # Для изображений MAX в некоторых ответах возвращает
                    # объект photos с токеном внутри.
                    photos = (result or {}).get("photos") or {}
                    if isinstance(photos, dict):
                        for photo in photos.values():
                            if isinstance(photo, dict) and photo.get("token"):
                                token = photo["token"]
                                break
                if not token:
                    log.error("MAX загрузил файл, но токен не найден: %r", result)
                return token
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            log.warning("Загрузка файла не удалась: %s", e)
            return None

    async def image_token_from_attachment(self, attachment):
        """Получает токен изображения из входящего вложения MAX.

        Если MAX прислал готовый image-token, используем его. Если пришёл
        документ/изображение со ссылкой, скачиваем файл и загружаем как image.
        """
        if not isinstance(attachment, dict):
            return None
        payload = attachment.get("payload") or {}
        attachment_type = (attachment.get("type") or "").lower()

        # Токен вложения типа image уже является токеном фотографии MAX.
        # Токен типа file нельзя записывать в photo_token: это другой тип медиа.
        if attachment_type == "image" and payload.get("token"):
            return payload["token"]

        # Документ (type=file) сначала скачиваем по URL, затем загружаем заново
        # именно как image, чтобы полученный токен можно было показать в меню.
        url = payload.get("url") or attachment.get("url")
        if not url:
            return None
        image_bytes = await self.download_bytes(url)
        if not image_bytes:
            return None
        filename = (attachment.get("filename") or attachment.get("name") or
                    payload.get("filename") or payload.get("name") or "photo.jpg")
        return await self.upload_bytes(image_bytes, "image", filename=filename)

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
    buttons.append([cb_btn("👤 Личный кабинет", "profile")])
    return kb(buttons)

def item_label(item):
    sold_out = (item["stock"] is not None and item["stock"] <= 0)
    if sold_out:
        return f"🚫 {item['name']} — нет в наличии"
    return f"{item['name']} — {item['price']} ₽"

def menu_keyboard(items, cat_key):
    buttons = [[cb_btn(item_label(i), f"view:{i['key']}")] for i in items]
    buttons.append([cb_btn("🛒 Корзина", "cart")])
    buttons.append([cb_btn("🏠 Главное меню", "main_menu")])
    return kb(buttons)

async def item_card_keyboard(user_id, item_key, cat_key):
    fav = await is_favorite(user_id, item_key)
    fav_label = "❤️ Убрать из избранного" if fav else "🤍 В избранное"
    return kb([
        [cb_btn("🛒 Добавить в корзину", f"add:{item_key}")],
        [cb_btn(fav_label, f"fav:{item_key}")],
        [cb_btn("⬅️ Назад", f"cat:{cat_key}")],
        [cb_btn("🏠 Главное меню", "main_menu")],
        [cb_btn("🛒 Корзина", "cart")],
    ])

def garnish_keyboard(item_key):
    return kb([
        [cb_btn("🍟 Картофель фри", f"garnish:{item_key}:fries")],
        [cb_btn("🍚 Отварной рис", f"garnish:{item_key}:rice")],
    ])

def manager_keyboard(order_id, status):
    transitions = {
        "new": [("👨‍🍳 Готовится", "cooking")],
        "paid": [("👨‍🍳 Готовится", "cooking")],
        "cooking": [("🛵 В доставке", "delivering")],
        "delivering": [("✅ Доставлен", "done")],
        "done": [], "cancelled": [],
    }
    buttons = [[cb_btn(label, f"setstatus:{order_id}:{next_status}")]
               for label, next_status in transitions.get(status, [])]
    return kb(buttons)

def manager_panel_keyboard():
    return kb([
        [cb_btn("🆕 Ожидают оплаты", "mgr:new")],
        [cb_btn("💳 Клиент сообщил об оплате", "mgr:paid")],
        [cb_btn("👨‍🍳 Готовятся", "mgr:cooking")],
        [cb_btn("🛵 В доставке", "mgr:delivering")],
        [cb_btn("✅ Доставленные", "mgr:done")],
    ])

def admin_panel_keyboard():
    return kb([
        [cb_btn("➕ Добавить блюдо", "admin:add"),
         cb_btn("📋 Все блюда", "admin:list")],
        [cb_btn("🗂 Список категорий", "admin:cats"),
         cb_btn("➕ Добавить категорию", "admin:cat_add")],
        [cb_btn("🗑 Удалить категорию", "admin:cat_del")],
        [cb_btn("👤 Настроить ID кассира", "admin:manager_id"),
         cb_btn("📋 Активные заказы", "admin:orders")],
        [cb_btn("🧹 Сбросить все заказы", "admin:orders_clear_confirm")],
        [cb_btn("📦 Остатки / стоп-лист", "admin:stock")],
        [cb_btn("📈 Топ продаж", "admin:stats")],
        [cb_btn("💳 Оплата (ЮKassa)", "admin:yk"),
         cb_btn("📊 Загрузить меню Excel", "admin:excel")],
        [cb_btn("📸 Массовая загрузка фото блюд", "admin:bulk_photos")],
        [cb_btn("💾 Резервная копия БД", "admin:backup")],
        [cb_btn("🗑 Удалить всё меню", "admin:menu_clear_confirm")],
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
        return "Корзина пуста.", kb([[cb_btn("🏠 Главное меню", "main_menu")]])

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
    kb_buttons.append([cb_btn("🗑 Очистить корзину", "cart_clear")])
    kb_buttons.append([cb_btn("🏠 Главное меню", "main_menu")])
    return text, kb(kb_buttons)

# ================= ИМПОРТ EXCEL =================
def _excel_key(value, fallback):
    raw = str(value or fallback).strip().lower()
    raw = re.sub(r"[^a-z0-9_]+", "_", raw.replace(" ", "_"))
    raw = re.sub(r"_+", "_", raw).strip("_")
    return raw or fallback

async def import_menu_excel(file_bytes):
    """Импорт XLSX. Колонки: key, name, description, price, category_key, category_name.

    category_key — стабильный латинский код категории (например, hot_dishes).
    category_name — название, которое увидят клиенты (например, Горячие блюда).
    Если category_name не заполнен, используем уже существующее название категории,
    а если его нет — аккуратно формируем название из category_key.
    """
    workbook = load_workbook(io.BytesIO(file_bytes), read_only=True, data_only=True)
    try:
        sheet = workbook.active
        rows = sheet.iter_rows(values_only=True)
        first = next(rows, None)
        if first is None:
            raise ValueError("В Excel нет строк")

        aliases = {
            "key": {"key", "ключ", "код", "артикул"},
            "name": {"name", "название", "блюдо", "наименование"},
            "description": {"description", "описание", "состав"},
            "price": {"price", "цена", "стоимость"},
            "category_key": {"category_key", "category key", "ключ категории", "категория ключ", "код категории"},
            "category_name": {"category_name", "category name", "название категории", "категория название"},
        }
        normalized = [str(v or "").replace("\ufeff", "").strip().lower() for v in first]
        header_map = {}
        for idx, value in enumerate(normalized):
            for field, names in aliases.items():
                if value in names:
                    header_map[field] = idx
        has_header = "name" in header_map and "price" in header_map
        if not has_header:
            raise ValueError(
                "Не найдены заголовки. Нужны столбцы: key, name, description, price, category_key, category_name"
            )

        imported = skipped = 0
        errors = []
        for row_num, row in enumerate(rows, start=2):
            if not row or not any(v is not None and str(v).strip() for v in row):
                continue
            try:
                def val(field, default=""):
                    idx = header_map.get(field)
                    if idx is None or idx >= len(row) or row[idx] is None:
                        return default
                    return row[idx]

                name = str(val("name")).strip()
                desc = str(val("description")).strip()
                price_raw = val("price")
                cat_raw = str(val("category_key", "other")).strip() or "other"
                cat_name_raw = str(val("category_name", "")).strip()
                key_raw = val("key", "")

                if not name or price_raw == "":
                    raise ValueError("нужно заполнить название и цену")
                price = Decimal(str(price_raw).replace(" ", "").replace(",", "."))
                if price <= 0:
                    raise ValueError("цена должна быть больше нуля")

                key = _excel_key(key_raw, _excel_key(name, f"dish_{row_num}"))
                cat_key = _excel_key(cat_raw, "other")
                if cat_key == "default":
                    cat_key = "other"

                if cat_name_raw:
                    cat_name = cat_name_raw
                else:
                    # Не затираем уже заведённое вручную название категории.
                    async with aiosqlite.connect(DB_PATH) as db:
                        async with db.execute(
                            "SELECT display_name, name FROM categories WHERE key = ?", (cat_key,)
                        ) as cur:
                            existing = await cur.fetchone()
                    existing_name = (existing[0] or existing[1]) if existing else None
                    cat_name = existing_name or cat_key.replace("_", " ").capitalize()

                await category_add(cat_key, cat_name, cat_name)
                await menu_add(key, name, desc, str(price), cat_key, None)
                imported += 1
            except Exception as exc:
                skipped += 1
                errors.append(f"Строка {row_num}: {exc}")
        return imported, skipped, errors
    finally:
        workbook.close()

async def send_profile(chat_id, user_id):
    user = await get_user(user_id)
    count = await get_order_count(user_id)
    remaining = DISCOUNT_EVERY_N_ORDERS - (count % DISCOUNT_EVERY_N_ORDERS)
    bonus = ("🎉 Следующий заказ будет со скидкой 50%!" if remaining == 1
             else f"До скидки 50% осталось заказов: {remaining}")
    text = ("👤 **Личный кабинет**\n\n"
            f"Имя: {user['name'] if user and user['name'] else 'не указано'}\n"
            f"Телефон: {user['phone'] if user and user['phone'] else 'не указан'}\n"
            f"Всего заказов: **{count}**\n{bonus}\n\n"
            "Управляйте адресами и просматривайте историю заказов.")
    await api.send(chat_id, text, attachments=[kb([
        [cb_btn("📦 Мои заказы", "history")],
        [cb_btn("📍 Мои адреса", "addresses")],
        [cb_btn("✏️ Изменить телефон", "edit_phone")],
        [cb_btn("⬅️ В меню", "main_menu")]
    ])])

async def send_addresses(chat_id, user_id):
    addresses = await addresses_get(user_id)
    buttons=[]
    for a in addresses:
        prefix="⭐ " if a["is_default"] else ""
        buttons.append([cb_btn(prefix + a["address"][:48], f"address_default:{a['id']}")])
        buttons.append([cb_btn("✏️ Изменить", f"address_edit:{a['id']}"), cb_btn("🗑 Удалить", f"address_delete:{a['id']}")])
    buttons += [[cb_btn("➕ Добавить адрес", "address_add")], [cb_btn("⬅️ В личный кабинет", "profile")]]
    message="📍 **Мои адреса доставки**\nНажмите на адрес, чтобы выбрать его основным."
    if not addresses:
        message += "\n\nАдресов пока нет. Добавьте первый адрес."
    await api.send(chat_id, message, attachments=[kb(buttons)])


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
    user_id = sender.get("user_id") or sender.get("id")
    chat_id = (recipient.get("chat_id") or message.get("chat_id")
               or recipient.get("user_id") or user_id)
    try:
        user_id = int(user_id) if user_id is not None else None
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        log.warning("Некорректные ID в message_created: sender=%r recipient=%r",
                    sender, recipient)
        return
    if not user_id or chat_id is None:
        return
    username = sender.get("username")
    await remember_user_chat(user_id, chat_id)
    body = (message.get("body") or {})
    text = (body.get("text") or "").strip()
    state = st_get(user_id)
    sname = state.get("state")
    incoming_images = [a for a in (body.get("attachments") or [])
                       if a.get("type") in ("image", "file")]
    photo_token = None
    # Не скачиваем и не загружаем вложения обычных сообщений клиентов.
    # Общий обработчик используется только в состояниях ручной загрузки фото.
    wants_photo = (sname == "admin_add_photo" or
                   (sname == "admin_edit_value" and
                    st_data(user_id).get("field") == "photo_token"))
    if wants_photo and incoming_images:
        for incoming_image in reversed(incoming_images):
            photo_token = await api.image_token_from_attachment(incoming_image)
            if photo_token:
                break

    if text.startswith("/start"):
        user = await get_user(user_id)
        if not user or not user["agreed_offer"]:
            await send_offer_screen(chat_id)
            st_set(user_id, "waiting_offer")
            return
        st_clear(user_id)
        await send_menu(chat_id)
        return

    if text.startswith("/myid"):
        await api.send(chat_id, f"Ваш ID в MAX: `{user_id}`\n"
                                "Передайте этот номер администратору для настройки уведомлений.")
        return
    if text.startswith("/stafftest") and is_manager(user_id):
        await remember_staff_chat(user_id, chat_id)
        await api.send(chat_id, "✅ Личный чат кассира зарегистрирован. Этот чат будет использоваться для новых заказов.")
        return

    if text.startswith("/offer"):
        await api.send(
            chat_id,
            "Полный текст публичной оферты доступен по ссылке:",
            attachments=[kb([[link_btn("📄 Прочитать оферту", OFFER_PDF_URL)]])]
        )
        return

    # До акцепта оферты обычные действия и просмотр меню недоступны.
    if not is_admin(user_id):
        current_user = await get_user(user_id)
        if not current_user or not current_user["agreed_offer"]:
            st_set(user_id, "waiting_offer")
            await send_offer_screen(chat_id)
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

    # ===== Состояния админа: кассир и переименование категории =====
    if sname == "admin_manager_id" and text:
        raw_id = text.strip()
        if not raw_id.isdigit():
            await api.send(chat_id, "Нужен числовой ID из команды /myid. Попробуйте ещё раз:")
            return
        manager_id = int(raw_id)
        if manager_id <= 0:
            await api.send(chat_id, "Некорректный ID. Введите положительное число:")
            return
        await set_setting("manager_chat_id", str(manager_id))
        global MANAGER_CHAT_ID
        MANAGER_CHAT_ID = manager_id
        st_clear(user_id)
        await api.send(chat_id,
                       f"✅ ID кассира сохранён: {manager_id}.\n"
                       "Убедитесь, что кассир уже отправил боту /start — иначе MAX "
                       "не разрешит боту написать ему первым.",
                       attachments=[admin_panel_keyboard()])
        return

    if sname == "admin_cat_rename" and text:
        if len(text.strip()) < 2:
            await api.send(chat_id, "Название слишком короткое. Введите минимум 2 символа:")
            return
        data = st_data(user_id)
        key = data.get("key")
        cats = await categories_all()
        if not any(c["key"] == key for c in cats):
            st_clear(user_id)
            await api.send(chat_id, "Категория больше не существует.",
                           attachments=[admin_panel_keyboard()])
            return
        new_name = text.strip()
        await category_add(key, new_name, new_name)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Название категории изменено на «{new_name}».",
                       attachments=[kb([[cb_btn("🗂 К списку категорий", "admin:cats")],
                                        [cb_btn("⬅️ В админ-панель", "admin:back")]])])
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

    # ===== Массовая загрузка: сопоставление фото без имени =====
    if sname == "admin_bulk_photo_names":
        if text.lower() in ("отмена", "cancel"):
            st_clear(user_id)
            await api.send(chat_id, "Массовая загрузка фото отменена.",
                           attachments=[admin_panel_keyboard()])
            return
        pending_tokens = st_data(user_id).get("pending_tokens") or []
        names = [line.strip() for line in text.splitlines() if line.strip()]
        if len(names) != len(pending_tokens):
            await api.send(
                chat_id,
                f"Получено названий: {len(names)}, а фотографий без имени: "
                f"{len(pending_tokens)}. Пришлите ровно {len(pending_tokens)} "
                "названий — каждое с новой строки, в том же порядке, что и фотографии.")
            return

        def _photo_name_key(value):
            value = os.path.basename(str(value or "")).strip()
            value = re.sub(r"\.(jpg|jpeg|png|webp|gif|bmp|tif|tiff|heic)$", "", value,
                           flags=re.IGNORECASE)
            value = re.sub(r"\s*[-–—]\s*\d.*$", "", value)
            return re.sub(r"\s+", " ", value).strip().casefold()

        menu_items = await menu_all(active_only=False)
        menu_by_name = {_photo_name_key(item["name"]): item for item in menu_items}
        counts = {}
        for item in menu_items:
            k = _photo_name_key(item["name"])
            counts[k] = counts.get(k, 0) + 1

        uploaded, skipped = 0, []
        for token, filename in zip(pending_tokens, names):
            key = _photo_name_key(filename)
            item = menu_by_name.get(key)
            if not key or counts.get(key, 0) != 1 or not item:
                skipped.append(f"{filename} — блюдо не найдено или название неоднозначно")
                continue
            await menu_update(item["key"], "photo_token", token)
            uploaded += 1

        st_set(user_id, "admin_bulk_photos")
        result = f"✅ По названиям привязано фотографий: {uploaded}."
        if skipped:
            result += "\n\nНе обработаны:\n" + "\n".join(skipped[:20])
        result += "\n\nМожете отправить следующую партию. Для завершения напишите «готово», для отмены — «отмена»."
        await api.send(chat_id, result)
        return

    # ===== Массовая загрузка фотографий блюд =====
    if sname == "admin_bulk_photos":
        if text.lower() in ("отмена", "cancel"):
            st_clear(user_id)
            await api.send(chat_id, "Массовая загрузка фото отменена.",
                           attachments=[admin_panel_keyboard()])
            return
        if text.lower() in ("готово", "завершить", "закончить", "done"):
            st_clear(user_id)
            await api.send(chat_id, "✅ Массовая загрузка завершена.",
                           attachments=[admin_panel_keyboard()])
            return

        file_attachments = [
            a for a in (body.get("attachments") or [])
            if isinstance(a, dict) and a.get("type") in ("file", "image")
        ]
        if not file_attachments:
            await api.send(
                chat_id,
                "Пришлите изображения как фото или как файлы-документы. "
                "Если MAX не передаст имена файлов, я попрошу перечислить названия блюд в том же порядке.")
            return

        def _photo_name_key(value):
            value = os.path.basename(str(value or "")).strip()
            value = re.sub(r"\.(jpg|jpeg|png|webp|gif|bmp|tif|tiff|heic)$", "", value,
                           flags=re.IGNORECASE)
            value = re.sub(r"\s*[-–—]\s*\d.*$", "", value)
            return re.sub(r"\s+", " ", value).strip().casefold()

        menu_items = await menu_all(active_only=False)
        menu_by_name = {}
        name_counts = {}
        for menu_item in menu_items:
            name_key = _photo_name_key(menu_item["name"])
            if name_key:
                menu_by_name[name_key] = menu_item
                name_counts[name_key] = name_counts.get(name_key, 0) + 1

        uploaded, skipped, unnamed_tokens = 0, [], []
        allowed_extensions = (".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tif", ".tiff", ".heic")
        for attachment in file_attachments:
            payload_file = attachment.get("payload") or {}
            filename = (attachment.get("filename") or attachment.get("name") or
                        payload_file.get("filename") or payload_file.get("name") or "").strip()
            token = await api.image_token_from_attachment(attachment)
            if not token:
                skipped.append(f"{filename or 'Изображение'} — не удалось получить image-токен MAX")
                continue

            if not filename:
                # Фото, отправленное в MAX как изображение, не содержит исходного
                # имени файла в API. Сохраняем токен и сопоставим по списку названий.
                if attachment.get("type") == "image":
                    unnamed_tokens.append(token)
                else:
                    skipped.append("Файл без имени — MAX не передал filename")
                continue

            if not filename.lower().endswith(allowed_extensions):
                skipped.append(f"{filename} — неподдерживаемый формат изображения")
                continue
            name_key = _photo_name_key(filename)
            if name_counts.get(name_key, 0) > 1:
                skipped.append(f"{filename} — название блюда неоднозначно в меню")
                continue
            menu_item = menu_by_name.get(name_key)
            if not menu_item:
                skipped.append(f"{filename} — блюдо с таким названием не найдено")
                continue
            await menu_update(menu_item["key"], "photo_token", token)
            uploaded += 1

        result = f"✅ По именам файлов привязано фотографий: {uploaded}."
        if unnamed_tokens:
            st_set(user_id, "admin_bulk_photo_names", pending_tokens=unnamed_tokens)
            result += (
                f"\n\nMAX прислал ещё {len(unnamed_tokens)} фото без исходных имён. "
                "Чтобы привязать их правильно, отправьте названия соответствующих "
                "блюд каждое с новой строки, строго в том же порядке, что фотографии. "
                "Можно писать названия без расширения файла.")
        else:
            result += "\n\nМожете отправить следующую партию. Когда закончите, напишите «готово». Для отмены — «отмена»."
        if skipped:
            result += "\n\nНе обработаны:\n" + "\n".join(skipped[:20])
            if len(skipped) > 20:
                result += f"\n…и ещё {len(skipped) - 20} файлов."
        await api.send(chat_id, result)
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

    # ===== Импорт меню из Excel =====
    if sname == "admin_excel":
        if text.lower() in ("отмена", "cancel"):
            st_clear(user_id)
            await api.send(chat_id, "Импорт отменён.",
                           attachments=[admin_panel_keyboard()])
            return
        file_attachments = [a for a in (body.get("attachments") or [])
                            if a.get("type") == "file"]
        if not file_attachments:
            await api.send(chat_id,
                           "Пришлите файл Excel формата .xlsx как документ. "
                           "Для отмены напишите «отмена».")
            return
        payload_file = file_attachments[-1].get("payload") or {}
        filename = (payload_file.get("filename") or "").lower()
        file_url = payload_file.get("url")
        if filename and not filename.endswith(".xlsx"):
            await api.send(chat_id, "Нужен файл Excel именно в формате .xlsx.")
            return
        if not file_url:
            await api.send(chat_id,
                           "MAX не передал ссылку на файл. Попробуйте отправить "
                           "Excel ещё раз как документ.")
            return
        try:
            async with api.session.get(file_url, ssl=False) as response:
                if response.status != 200:
                    raise ValueError(f"HTTP {response.status}")
                excel_bytes = await response.read()
            if len(excel_bytes) > 15 * 1024 * 1024:
                raise ValueError("Файл больше 15 МБ")
            imported, skipped, errors = await import_menu_excel(excel_bytes)
            st_clear(user_id)
            result = (f"✅ Импорт завершён.\nДобавлено/обновлено блюд: {imported}.\n"
                      f"Пропущено строк: {skipped}.")
            if errors:
                result += "\n\nПервые ошибки:\n" + "\n".join(errors[:8])
            await api.send(chat_id, result, attachments=[admin_panel_keyboard()])
        except Exception as exc:
            log.exception("Ошибка импорта Excel")
            await api.send(chat_id,
                           f"Не удалось прочитать Excel: {exc}. Проверьте формат .xlsx "
                           "и попробуйте снова.")
        return

    # ===== Управление адресами в личном кабинете =====
    if sname in ("profile_address_add", "profile_address_edit"):
        if len(text) < 5:
            await api.send(chat_id, "Адрес слишком короткий. Укажите улицу, дом и квартиру:")
            return
        if sname == "profile_address_add":
            await address_add(user_id, text)
            saved=await addresses_get(user_id)
            default=next((a for a in saved if a["is_default"]), None)
            user=await get_user(user_id)
            if default and user:
                await save_user(user_id, user["name"] or "Гость", user["phone"] or "—", default["address"])
            response="✅ Новый адрес сохранён."
        else:
            address_id=int(st_data(user_id).get("address_id"))
            await address_update(user_id, address_id, text)
            response="✅ Адрес изменён."
        st_clear(user_id)
        await api.send(chat_id, response, attachments=[kb([
            [cb_btn("📍 К адресам", "addresses")],
            [cb_btn("👤 В личный кабинет", "profile")]
        ])])
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
    lines, receipt_lines, total = [], [], Decimal("0")
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
        receipt_lines.append((item["name"], Decimal(qty), Decimal(item["price"])))
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
    discount_rate = (discount_sum / total) if total > 0 else Decimal("0")

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

    manager_sent = await api.notify_staff(
        manager_text, attachments=[manager_keyboard(order_id, "new")])
    if not manager_sent:
        log.error("Новый заказ #%s сохранён, но уведомление не доставлено ни одному "
                  "сотруднику. Проверьте ID кассира/админа и запуск бота в их чате.",
                  order_id)

    # ===== Создание платежа ЮKassa =====
    payment = None
    if yookassa_configured():
        receipt = _yk_build_receipt(receipt_lines, discount_rate, delivery_cost, final_total)
        payment = await yookassa_create_payment(
            order_id, final_total,
            f"Заказ №{order_id} — {YOOKASSA_NAME}",
            receipt, phone=data.get("phone"))
        if payment:
            await order_set_payment_id(order_id, payment["id"])
            log.info("ЮKassa: платёж %s создан для заказа #%s", payment["id"], order_id)
        else:
            log.error("ЮKassa: платёж для заказа #%s не создан — заказ идёт "
                      "по старому сценарию (QR/кассир)", order_id)

    user_text = (
        "✅ Ваш заказ успешно оформлен!\n\n"
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
    )
    if payment:
        user_text += ("📌 **Для оплаты:**\n"
                      "Нажмите кнопку **«Оплатить онлайн»** ниже — карта или СБП.")
    elif await get_setting("payment_qr"):
        user_text += "📌 **Для оплаты:**\nОтсканируйте QR-код в следующем сообщении."
    else:
        user_text += ("⚠️ Оплата онлайн не настроена — напишите кассиру, "
                      "он пришлёт реквизиты.")

    buttons = []
    if payment:
        buttons.append([link_btn("💳 Оплатить онлайн",
                                 payment["confirmation"]["confirmation_url"])])
    buttons.append([link_btn("💬 Написать кассиру", MANAGER_USERNAME)])
    buttons.append([cb_btn("❌ Отменить заказ", f"cancelorder:{order_id}")])
    customer_result = await api.send(chat_id, user_text, attachments=[kb(buttons)])
    if customer_result is None:
        log.error("Заказ #%s сохранён, но клиентское подтверждение не доставлено chat_id=%s", order_id, chat_id)
    else:
        log.info("Подтверждение заказа #%s доставлено user_id=%s chat_id=%s", order_id, user_id, chat_id)

    if payment:
        pay_msg = await api.send(
            chat_id,
            "💳 **Оплата заказа**\n"
            f"Сумма: **{final_total} ₽**\n\n"
            "Нажмите **«Оплатить онлайн»** выше — карта или СБП.\n"
            "Бот сам увидит оплату и передаст заказ на кухню.",
            attachments=[kb([[cb_btn("🔄 Проверить оплату", f"paid:{order_id}")],
                             [cb_btn("❌ Отменить заказ", f"cancelorder:{order_id}")]])])
        if pay_msg is None:
            log.error("Не удалось отправить платёжное сообщение для заказа #%s", order_id)
        asyncio.create_task(_yk_watch(order_id, payment["id"]))
    else:
        qr_token = await get_setting("payment_qr")
        if qr_token:
            qr_sent = await api.send(
                chat_id,
                "💳 **Оплата заказа**\n"
                f"Сумма: **{final_total} ₽**\n\n"
                "Отсканируйте QR-код и оплатите заказ. После оплаты нажмите «Я оплатил».",
                attachments=[photo_attachment(qr_token),
                             kb([[cb_btn("✅ Я оплатил", f"paid:{order_id}")],
                                 [cb_btn("❌ Отменить заказ", f"cancelorder:{order_id}")]])])
            if qr_sent is None:
                log.error("Не удалось отправить QR-код клиенту для заказа #%s", order_id)
        else:
            log.error("ЮKassa недоступна и QR-код не задан: заказ #%s без способа оплаты", order_id)
            await api.send(chat_id,
                           "⚠️ Оплата пока не настроена. Напишите кассиру, "
                           "чтобы получить реквизиты для оплаты.",
                           attachments=[kb([[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]])])

    if not manager_sent:
        log.critical("Не удалось доставить уведомление о заказе #%s. Текст заказа:\n%s",
                     order_id, manager_text)

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

    callback_user = cb.get("user") or update.get("user") or {}
    user_id = callback_user.get("user_id") or callback_user.get("id")
    callback_username = callback_user.get("username")
    msg = cb.get("message") or update.get("message") or {}
    recipient = msg.get("recipient") or {}
    chat_id = (recipient.get("chat_id") or msg.get("chat_id")
               or update.get("chat_id") or user_id)
    try:
        user_id = int(user_id) if user_id is not None else None
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        log.warning("Некорректные ID callback: %r", update)
        return

    log.info("Callback: payload=%r user=%s chat=%s", payload, user_id, chat_id)
    await remember_user_chat(user_id, chat_id)

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
        # Переход в меню не должен удалять уже выбранные товары.
        st_clear(user_id)
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
        avg_rating, rating_count = await dish_rating_summary(key)
        if rating_count:
            text += f"\n⭐ **Оценка: {avg_rating:.1f}/5** ({rating_count})"
        else:
            text += "\n⭐ Пока нет оценок"
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
        if await category_requires_garnish(item["category_key"]):
            await ack()
            await api.send(chat_id, f"{item['name']} — выберите гарнир:",
                           attachments=[garnish_keyboard(key)])
        else:
            await cart_add(user_id, key)
            await ack(f"✅ {item['name']} добавлен в корзину!")
            # После добавления блюда без гарнира остаёмся в его категории.
            items = await menu_all(category_key=item["category_key"],
                                   in_stock_only=True)
            if items:
                await api.send(
                    chat_id, "МЕНЮ",
                    attachments=[menu_keyboard(items, item["category_key"])]
                )
            else:
                await send_menu(chat_id)
        return

    if payload.startswith("garnish:"):
        try:
            _, item_key, garnish = payload.split(":", 2)
        except ValueError:
            await ack("Некорректный выбор гарнира")
            return
        if garnish not in GARNISH_LABELS:
            await ack("Неизвестный гарнир")
            return
        item = await menu_get(item_key)
        if not item or not item["active"]:
            await ack("Блюдо больше недоступно")
            return
        message_id = (msg.get("message_id") or msg.get("id")
                      or (msg.get("body") or {}).get("mid") or callback_id)
        if not message_id:
            await ack("Не удалось подтвердить выбор. Откройте блюдо и попробуйте ещё раз.")
            return
        first_choice = await register_garnish_choice(
            user_id, message_id, item_key, garnish)
        if not first_choice:
            await ack("Для этого блюда гарнир уже выбран")
            return
        await set_garnish(user_id, item_key, garnish)
        await cart_add(user_id, item_key, garnish)
        await ack("Гарнир выбран")
        message_text = (f"✅ К блюду «{item['name']}» добавлен гарнир: "
                        f"{GARNISH_LABELS[garnish]}.")
        original_message_id = (msg.get("message_id") or msg.get("id")
                               or (msg.get("body") or {}).get("mid"))
        edited = False
        if original_message_id:
            edited = await api.edit(chat_id, original_message_id, message_text,
                                    attachments=[])
        if not edited:
            await api.send(chat_id, message_text)
        items = await menu_all(category_key=item["category_key"], in_stock_only=True)
        if items:
            await api.send(chat_id, "МЕНЮ",
                           attachments=[menu_keyboard(items, item["category_key"])])
        else:
            await send_menu(chat_id)
        return

    if payload == "cart":
        await ack()
        text, kbd_ = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[kbd_])
        return

    if payload == "cart_clear":
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        st_clear(user_id)
        await ack("Корзина очищена")
        await send_menu(chat_id)
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

    # ===== Личный кабинет и адресная книга =====
    if payload == "profile":
        st_clear(user_id)
        await ack()
        await send_profile(chat_id, user_id)
        return
    if payload == "addresses":
        await ack()
        await send_addresses(chat_id, user_id)
        return
    if payload == "address_add":
        st_set(user_id, "profile_address_add")
        await ack()
        await api.send(chat_id, "Введите новый адрес доставки:")
        return
    if payload.startswith("address_edit:"):
        try: address_id=int(payload.split(":",1)[1])
        except ValueError:
            await ack("Некорректный адрес"); return
        if address_id not in {int(a["id"]) for a in await addresses_get(user_id)}:
            await ack("Адрес не найден"); return
        st_set(user_id, "profile_address_edit", address_id=address_id)
        await ack()
        await api.send(chat_id, "Введите новый адрес:")
        return
    if payload.startswith("address_delete:"):
        try: address_id=int(payload.split(":",1)[1])
        except ValueError:
            await ack("Некорректный адрес"); return
        await address_delete(user_id, address_id)
        await ack("Адрес удалён")
        await send_addresses(chat_id, user_id)
        return
    if payload.startswith("address_default:"):
        try: address_id=int(payload.split(":",1)[1])
        except ValueError:
            await ack("Некорректный адрес"); return
        await address_set_default(user_id, address_id)
        await ack("Основной адрес выбран")
        await send_addresses(chat_id, user_id)
        return

    # ===== Оценка блюд после доставки: выбор блюда -> выбор звёзд =====
    if payload.startswith("ratelist:"):
        try:
            rated_order_id = int(payload.split(":", 1)[1])
        except (ValueError, TypeError):
            await ack("Некорректный номер заказа")
            return
        order = await order_get(rated_order_id)
        if not order or int(order["user_id"]) != user_id or order["status"] != "done":
            await ack("Оценивать можно только свой доставленный заказ")
            return
        rated_items = await order_rating_items(rated_order_id)
        if not rated_items:
            await ack("В заказе нет блюд для оценки")
            return
        buttons = []
        for rated_item in rated_items:
            item_key = rated_item["item_key"]
            item_name = rated_item["name"] or item_key
            current_rating = await dish_user_rating(user_id, rated_order_id, item_key)
            label = f"⭐ {item_name[:30]}"
            if current_rating:
                label += f" — {current_rating}/5"
            buttons.append([cb_btn(label, f"rateitem:{rated_order_id}:{item_key}")])
        buttons.append([cb_btn("🛍 Заказать снова", "main_menu")])
        await ack()
        await api.send(chat_id,
                       f"🍽 **Оценка заказа #{rated_order_id}**\n"
                       "Выберите блюдо, которое хотите оценить:",
                       attachments=[kb(buttons)])
        return

    if payload.startswith("rateitem:"):
        try:
            _, raw_order_id, item_key = payload.split(":", 2)
            rated_order_id = int(raw_order_id)
        except (ValueError, TypeError):
            await ack("Некорректный выбор блюда")
            return
        order = await order_get(rated_order_id)
        if not order or int(order["user_id"]) != user_id or order["status"] != "done":
            await ack("Оценивать можно только свой доставленный заказ")
            return
        rated_items = await order_rating_items(rated_order_id)
        selected = next((r for r in rated_items if r["item_key"] == item_key), None)
        if not selected:
            await ack("Это блюдо не найдено в вашем заказе")
            return
        item_name = selected["name"] or item_key
        current_rating = await dish_user_rating(user_id, rated_order_id, item_key)
        buttons = [[cb_btn(
            (("⭐ " if stars <= (current_rating or 0) else "") + f"{stars} ⭐"),
            f"rate:{rated_order_id}:{item_key}:{stars}")
            for stars in range(1, 6)]]
        buttons.append([cb_btn("⬅️ К списку блюд", f"ratelist:{rated_order_id}")])
        await ack()
        await api.send(chat_id,
                       f"🍽 **{item_name}**\n"
                       + (f"Ваша текущая оценка: {current_rating}/5. Её можно изменить.\n"
                          if current_rating else "Поставьте оценку от 1 до 5 звёзд:\n")
                       + "Выберите количество звёзд:",
                       attachments=[kb(buttons)])
        return

    if payload.startswith("rate:"):
        try:
            _, raw_order_id, item_key, raw_rating = payload.split(":", 3)
            rated_order_id = int(raw_order_id)
            rating = int(raw_rating)
        except (ValueError, TypeError):
            await ack("Некорректная оценка")
            return
        if rating < 1 or rating > 5:
            await ack("Оценка должна быть от 1 до 5")
            return
        saved = await save_dish_rating(user_id, rated_order_id, item_key, rating)
        if not saved:
            await ack("Оценить можно только блюдо из вашего доставленного заказа")
            return
        avg_rating, rating_count = await dish_rating_summary(item_key)
        item = await menu_get(item_key)
        dish_name = item["name"] if item else item_key
        await ack(f"Спасибо! {rating} из 5 ⭐")
        # Обновляем сообщение со звёздами сразу после нажатия, чтобы пользователь
        # увидел сохранённую оценку без дополнительного сообщения.
        message_id = (msg.get("message_id") or msg.get("id")
                      or (msg.get("body") or {}).get("mid"))
        selected_stars = "⭐" * rating + "☆" * (5 - rating)
        updated_text = (
            f"✅ **Оценка сохранена!**\n"
            f"{dish_name}: {selected_stars} ({rating}/5)\n"
            f"Средняя оценка блюда: {avg_rating:.1f}/5 "
            f"({rating_count} оценок).\n\n"
            "Спасибо за обратную связь! Вы можете изменить оценку или выбрать другое блюдо."
        )
        buttons = [[cb_btn(
            (("⭐" if stars <= rating else "☆") + f" {stars}"),
            f"rate:{rated_order_id}:{item_key}:{stars}")
            for stars in range(1, 6)]]
        buttons.append([cb_btn("⬅️ Оценить другое блюдо", f"ratelist:{rated_order_id}")])
        edited = False
        if message_id:
            edited = await api.edit(chat_id, message_id, updated_text,
                                    attachments=[kb(buttons)])
        if not edited:
            await api.send(chat_id, updated_text,
                           attachments=[kb(buttons)])
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
            text = (f"📜 {STATUS_LABELS.get(o['status'], o['status'])}\n"
                    f"Создан: {o['created_at']}\n"
                    f"{o['items']}\n\n💰 Итого: {o['total']} ₽")
            if o["delivery_time"] and o["delivery_time"] != "—":
                text += f"\n⏰ К времени: {o['delivery_time']}"
            history_buttons = [[cb_btn("🔁 Повторить заказ", f"reorder:{o['id']}")]]
            if o["status"] in ("new", "paid"):
                history_buttons.append([cb_btn("❌ Отменить заказ", f"cancelorder:{o['id']}")])
            await api.send(chat_id, text, attachments=[kb(history_buttons)])
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
                       f"🔁 Предыдущий заказ добавлен в корзину ({added} поз.):\n\n{text}",
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
        # Сначала подтверждаем нажатие MAX, не дожидаясь медленных запросов к БД.
        # Иначе клиент повторно нажимает кнопку, а MAX присылает callback ещё раз.
        await ack()

        # Не запускаем оформление повторно, если пользователь уже находится
        # внутри этого же процесса оформления.
        current_state = st_get(user_id).get("state")
        checkout_states = {
            "waiting_name", "waiting_phone", "waiting_address",
            "confirm_data", "waiting_time", "waiting_comment",
            "admin_manager_id", "edit_phone", "edit_address"
        }
        if current_state in checkout_states:
            await api.send(chat_id,
                           "Оформление уже начато. Продолжите отвечать на вопросы выше.")
            return

        rows = await cart_get(user_id)
        if not rows:
            await api.send(chat_id, "Корзина пуста. Добавьте блюда перед оформлением.")
            return

        total = Decimal("0")
        for key, qty in rows:
            item_key, _ = parse_cart_key(key)
            item = await menu_get(item_key)
            if item and item["active"] and (item["stock"] is None or item["stock"] > 0):
                total += Decimal(item["price"]) * qty
        if total < MIN_ORDER_AMOUNT:
            missing = MIN_ORDER_AMOUNT - total
            await api.send(chat_id,
                           f"Минимальный заказ {MIN_ORDER_AMOUNT} ₽. "
                           f"Добавьте ещё на {missing} ₽.")
            return

        if user["name"] and user["phone"] and user["address"]:
            text = ("Ваши данные для доставки:\n\n"
                    f"👤 Имя: {user['name']}\n"
                    f"📞 Тел: {user['phone']}\n"
                    f"📍 Адрес: {user['address']}\n\n"
                    "Все верно?")
            st_set(user_id, "confirm_data")
            await api.send(chat_id, text,
                           attachments=[kb([
                               [cb_btn("✅ Да, оформить", "confirm_yes")],
                               [cb_btn("✏️ Изменить данные", "confirm_no")],
                           ])])
        else:
            st_set(user_id, "waiting_name")
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
        await finalize_order(chat_id, user_id, callback_username)
        return

    if payload == "contact_manager":
        await ack("Открываю контакт кассира")
        await api.send(chat_id, "Свяжитесь с кассиром:",
                       attachments=[kb([[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]])])
        return

    if payload.startswith("cancelorder:"):
        try:
            order_id = int(payload.split(":", 1)[1])
        except (ValueError, IndexError):
            await ack("Некорректный заказ")
            return
        order = await order_get(order_id)
        if not order or order["user_id"] != user_id:
            await ack("Заказ не найден")
            return
        if order["status"] not in ("new", "paid"):
            await ack("Отмена доступна только до начала приготовления")
            return
        if not await order_cancel_if_allowed(order_id, user_id):
            await ack("Заказ уже перешёл в приготовление или был изменён")
            return
        await decrement_order_count(user_id)
        if yookassa_configured() and order["payment_id"]:
            asyncio.create_task(_yk_cancel_after_cancel_order(
                order["payment_id"], order_id))
        await ack("Заказ отменён")
        await api.send(chat_id, "❌ Ваш заказ отменён. Если потребуется, вы можете оформить новый.",
                       attachments=[kb([[cb_btn("🏠 Главное меню", "main_menu")]])])
        await api.notify_staff(
            f"❌ Клиент отменил заказ #{order_id}.\n"
            f"Клиент: {order['name']} (ID: {user_id})\n"
            f"Сумма: {order['total']} ₽")
        return

    if payload.startswith("paid:"):
        try:
            order_id = int(payload.split(":", 1)[1])
        except (ValueError, IndexError):
            await ack("Некорректный номер заказа")
            return
        order = await order_get(order_id)
        if not order or order["user_id"] != user_id:
            await ack("Заказ не найден")
            return
        if order["status"] == "cancelled":
            await ack("Заказ отменён. Свяжитесь с кассиром.")
            return
        if order["status"] in ("paid", "cooking", "delivering", "done"):
            await ack("Оплата уже отмечена")
            return
        if yookassa_configured() and order["payment_id"]:
            payment = await yookassa_get_payment(order["payment_id"])
            pay_status = (payment or {}).get("status")
            if pay_status == "succeeded":
                await order_set_status(order_id, "paid")
                await ack("Оплата подтверждена!")
                await api.send(chat_id,
                               "💳 Оплата заказа подтверждена!\n"
                               "Заказ передаётся на кухню. Спасибо!")
                await api.notify_staff(
                    f"💳 Заказ #{order_id} оплачен через ЮKassa (проверено по кнопке).\n"
                    f"Клиент: {order['name']} (@{order['username']}, ID: {order['user_id']})\n"
                    f"Сумма: {order['total']} ₽\n\n"
                    "Заказ можно передавать на кухню.",
                    attachments=[manager_keyboard(order_id, "paid")])
            elif pay_status == "canceled":
                await ack("Платёж не завершён")
                await api.send(chat_id,
                               "❌ Платёж по заказу не завершён или отменён.\n"
                               "Оформите заказ заново или напишите кассиру.",
                               attachments=[kb([[cb_btn("💬 Написать кассиру", "contact_manager")]])])
            else:
                await ack("Оплата пока не поступила")
                await api.send(chat_id,
                               "⏳ Оплата пока не поступила.\n"
                               "Если вы только что оплатили — подождите минуту "
                               "и нажмите «Проверить оплату» ещё раз.")
            return
        # Резервный сценарий (без ЮKassa): клиент подтверждает оплату кассиру.
        await order_set_status(order_id, "paid")
        await ack("Спасибо! Сообщение об оплате отправлено кассиру.")
        await api.send(chat_id,
                       "💳 Вы сообщили об оплате заказа.\n"
                       "Статус: **Оплата ожидает проверки кассиром**.\n"
                       "После проверки кассир обновит статус заказа, и бот сообщит вам.")
        manager_order_text = (
            f"💳 Клиент сообщил об оплате заказа #{order_id}.\n"
            f"Клиент: {order['name']} (@{order['username']}, ID: {order['user_id']})\n"
            f"Сумма: {order['total']} ₽\n\n"
            "Проверьте поступление оплаты и переведите заказ в приготовление."
        )
        await api.notify_staff(
            manager_order_text, attachments=[manager_keyboard(order_id, "paid")])
        return

    # ===== Админ-панель =====
    if payload == "admin_cancel":
        st_clear(user_id)
        await ack()
        await api.send(chat_id, "Отменено.", attachments=[admin_panel_keyboard()])
        return

    if payload == "admin:yk" and is_admin(user_id):
        await ack()
        if yookassa_configured():
            await api.send(chat_id,
                           "💳 **ЮKassa подключена.**\n"
                           f"shopId: `{YOOKASSA_SHOP_ID}`\n"
                           "Способ оплаты: ссылка «Оплатить онлайн» (карта/СБП).\n"
                           "Чеки: «Чеки от ЮKassa» (54-ФЗ, АУСН).\n\n"
                           "Бот сам проверяет оплату и переводит заказ в «Оплачен».")
        else:
            await api.send(chat_id,
                           "⚠️ **ЮKassa не настроена.**\n"
                           "Добавьте переменные окружения на bothost.ru:\n"
                           "`YOOKASSA_SHOP_ID` и `YOOKASSA_SECRET_KEY` "
                           "(значения — из кабинета ЮKassa).\n\n"
                           "Пока их нет, заказы работают по старому сценарию "
                           "(QR-код/кассир).",
                           attachments=[admin_panel_keyboard()])
        return

    if payload == "admin:menu_clear_confirm" and is_admin(user_id):
        await ack()
        await api.send(chat_id,
                       "⚠️ Вы действительно хотите удалить ВСЕ блюда из меню?\n"
                       "Категории, настройки и история заказов сохранятся.\n"
                       "Действие нельзя отменить.",
                       attachments=[kb([
                           [cb_btn("🗑 Да, удалить всё меню", "admin:menu_clear_yes")],
                           [cb_btn("↩️ Нет, вернуться", "admin:back")],
                       ])])
        return

    if payload == "admin:menu_clear_yes" and is_admin(user_id):
        await menu_delete_all()
        await ack("Меню удалено")
        await api.send(chat_id, "✅ Все блюда удалены. Категории и настройки сохранены.",
                       attachments=[admin_panel_keyboard()])
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
        buttons = [[cb_btn(f"✏️ Изменить: {c['display_name'] or c['name']}",
                           f"catrename:{c['key']}")]
                   for c in cats if c["key"] != "default"]
        buttons.extend([
            [cb_btn("➕ Добавить категорию", "admin:cat_add")],
            [cb_btn("🗑 Удалить категорию", "admin:cat_del")],
            [cb_btn("⬅️ В админ-панель", "admin:back")],
        ])
        await ack()
        await api.send(chat_id, text, attachments=[kb(buttons)])
        return

    if payload == "admin:manager_id" and is_admin(user_id):
        await ack()
        await api.send(chat_id,
                       "Чтобы узнать ID кассира, попросите его открыть чат с ботом "
                       "по вашей ссылке и отправить команду /myid.\n\n"
                       "Затем отправьте сюда полученный числовой ID. После этого "
                       "новые заказы и сообщения об оплате будут приходить кассиру "
                       "и администраторам.")
        st_set(user_id, "admin_manager_id")
        return

    if payload.startswith("catrename:") and is_admin(user_id):
        key = payload.split(":", 1)[1]
        cats = await categories_all()
        if not any(c["key"] == key for c in cats):
            await ack("Категория не найдена")
            return
        if key == "default":
            await ack("Служебную категорию переименовать нельзя")
            return
        st_set(user_id, "admin_cat_rename", key=key)
        await ack()
        await api.send(chat_id, f"Введите новое название категории «{key}»:")
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

    if payload == "admin:bulk_photos" and is_admin(user_id):
        st_set(user_id, "admin_bulk_photos")
        await ack()
        await api.send(
            chat_id,
            "📸 Отправьте фотографии блюд как файлы-документы (можно несколько "
            "за раз).\n\n"
            "Название файла должно совпадать с названием блюда в меню, "
            "без тире и цены. Например, для блюда «Котлета по-киевски» "
            "назовите файл «Котлета по-киевски.jpg».\n"
            "Поддерживаются JPG, JPEG, PNG, WEBP, GIF и BMP.\n"
            "Для отмены напишите «отмена».")
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
                       "Пришлите Excel-файл .xlsx как документ.\n\n"
                       "Столбцы в первой строке (заголовки):\n"
                       "key | name | description | price | category_key | category_name\n\n"
                       "Пример: kotleta | Котлета по-киевски | 250 г, курица | "
                       "350 | hot_dishes | Горячие блюда\n\n"
                       "Можно использовать русские заголовки: Ключ, Название, "
                       "Описание, Цена, Категория, Название категории. "
                       "Если категории ещё нет, она создастся автоматически.\n"
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

    if payload == "admin:orders_clear_confirm" and is_admin(user_id):
        # Сначала показываем отдельное подтверждение, чтобы исключить случайное удаление.
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT COUNT(*) FROM orders") as cur:
                row = await cur.fetchone()
                orders_count = row[0] if row else 0
        await ack()
        await api.send(
            chat_id,
            f"⚠️ Вы действительно хотите удалить ВСЕ заказы?\n"
            f"Будет удалено заказов: {orders_count}.\n"
            "Также будут удалены связанные позиции заказов.\n"
            "Меню, клиенты, адреса, настройки и счётчики скидок останутся.\n"
            "Действие нельзя отменить.",
            attachments=[kb([
                [cb_btn("🧹 Да, удалить все заказы", "admin:orders_clear_yes")],
                [cb_btn("↩️ Нет, вернуться", "admin:back")],
            ])]
        )
        return

    if payload == "admin:orders_clear_yes" and is_admin(user_id):
        try:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("BEGIN IMMEDIATE")
                async with db.execute("SELECT COUNT(*) FROM orders") as cur:
                    row = await cur.fetchone()
                    orders_count = row[0] if row else 0
                await db.execute("DELETE FROM dish_ratings")
                await db.execute("DELETE FROM order_items")
                await db.execute("DELETE FROM orders")
                # Сбрасываем последовательность номеров, чтобы новые заказы начинались с №1.
                await db.execute("DELETE FROM sqlite_sequence WHERE name = 'orders'")
                await db.commit()
            await ack("Все заказы удалены")
            await api.send(
                chat_id,
                f"✅ Готово. Удалено заказов: {orders_count}.\n"
                "История заказов очищена. Счётчики скидок клиентов сохранены.",
                attachments=[admin_panel_keyboard()]
            )
        except Exception as e:
            await ack("Не удалось удалить заказы")
            await api.send(chat_id, f"❌ Ошибка при сбросе заказов: {e}")
        return

    if payload == "admin:orders" and is_admin(user_id):
        active_statuses = ("new", "paid", "cooking", "delivering")
        counts = {}
        async with aiosqlite.connect(DB_PATH) as db:
            for status in active_statuses:
                async with db.execute(
                    "SELECT COUNT(*) FROM orders WHERE status = ?", (status,)
                ) as cur:
                    row = await cur.fetchone()
                    counts[status] = row[0] if row else 0
        labels = {
            "new": "🆕 Новые / ожидают оплаты",
            "paid": "💳 Клиент сообщил об оплате",
            "cooking": "👨‍🍳 Готовятся",
            "delivering": "🛵 Доставляются",
        }
        lines = ["📋 **Активные заказы**", ""]
        for status in active_statuses:
            lines.append(f"{labels[status]}: **{counts[status]}**")
        buttons = [[cb_btn(f"{labels[status]} ({counts[status]})",
                           f"admin:orders:{status}")]
                   for status in active_statuses]
        buttons.append([cb_btn("🔄 Обновить", "admin:orders")])
        buttons.append([cb_btn("⬅️ В админ-панель", "admin:back")])
        await ack()
        await api.send(chat_id, "\n".join(lines), attachments=[kb(buttons)])
        return

    if payload.startswith("admin:orders:") and is_admin(user_id):
        status = payload.split(":", 2)[2]
        if status not in {"new", "paid", "cooking", "delivering"}:
            await ack("Неизвестный статус")
            return
        orders = await orders_by_status(status, limit=30)
        if not orders:
            await ack("Заказов в этом статусе нет")
            await api.send(chat_id, "В этой категории пока нет заказов.",
                           attachments=[kb([[cb_btn("⬅️ К активным заказам", "admin:orders")]])])
            return
        buttons = []
        for order in orders:
            buttons.append([cb_btn(
                f"№{order['id']} · {order['name'] or 'Клиент'} · {order['total']} ₽",
                f"admin:order:{order['id']}")])
        buttons.append([cb_btn("🔄 Обновить", f"admin:orders:{status}")])
        buttons.append([cb_btn("⬅️ К активным заказам", "admin:orders")])
        await ack()
        await api.send(chat_id,
                       f"{STATUS_LABELS.get(status, status)}\nЗаказов: {len(orders)}",
                       attachments=[kb(buttons)])
        return

    if payload.startswith("admin:order:") and is_admin(user_id):
        try:
            order_id = int(payload.rsplit(":", 1)[1])
        except ValueError:
            await ack("Некорректный номер заказа")
            return
        order = await order_get(order_id)
        if not order:
            await ack("Заказ не найден")
            return
        detail = (
            f"📦 **Заказ №{order['id']}**\n"
            f"Статус: {STATUS_LABELS.get(order['status'], order['status'])}\n"
            f"Клиент: {order['name']} (@{order['username']}, ID: {order['user_id']})\n"
            f"Телефон: {order['phone']}\nАдрес: {order['address']}\n"
            f"Время: {order['delivery_time'] or '—'}\n"
            f"Комментарий: {order['comment'] or '—'}\n\n"
            f"{order['items']}\n\nИтого: **{order['total']} ₽**\n"
            f"Создан: {order['created_at']}"
        )
        await ack()
        await api.send(chat_id, detail, attachments=[kb([
            [cb_btn("🔄 Обновить заказ", f"admin:order:{order_id}")],
            [cb_btn("⬅️ К активным заказам", "admin:orders")],
            [cb_btn("⬅️ В админ-панель", "admin:back")],
        ])])
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
        allowed_transitions = {
            "new": {"cooking"},
            "paid": {"cooking"},
            "cooking": {"delivering"},
            "delivering": {"done"},
        }
        if new_status not in allowed_transitions.get(previous_status, set()):
            await ack("Это изменение статуса недоступно. Отмену заказа выполняет клиент до начала приготовления.")
            return
        await order_set_status(order_id, new_status)
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
            "paid": "💳 Кассир отметил оплату вашего заказа.",
            "cooking": "👨‍🍳 Ваш заказ уже готовится!",
            "delivering": "🛵 Ваш заказ уже везёт курьер!",
            "done": "✅ Заказ доставлен! Приятного аппетита!",
            "cancelled": "❌ Заказ отменён. Свяжитесь с кассиром, "
                         "если это ошибка.",
        }
        if new_status in notify_texts and new_status != previous_status:
            try:
                done_attachments = (
                    [kb([[cb_btn("🛍 Заказать снова", "main_menu")]])]
                    if new_status == "done" else None
                )
                await api.send_to_user(
                    order["user_id"], notify_texts[new_status],
                    attachments=done_attachments)
                if new_status == "done":
                    rated_items = await order_rating_items(order_id)
                    if rated_items:
                        rating_buttons = []
                        for rated_item in rated_items:
                            item_name = rated_item["name"] or rated_item["item_key"]
                            rating_buttons.append([cb_btn(
                                f"🍽 {item_name[:32]}",
                                f"rateitem:{order_id}:{rated_item['item_key']}")])
                        await api.send_to_user(
                            order["user_id"],
                            f"🍽 **Заказ #{order_id} доставлен!**\n"
                            "Спасибо, что выбрали нас! Выберите блюдо, чтобы поставить ему оценку. "
                            "Сначала выберите блюдо, затем нажмите от 1 до 5 звёзд. "
                            "Оценку можно изменить в любое время.",
                            attachments=[kb(rating_buttons)])
            except Exception:
                log.exception("Не удалось отправить клиенту уведомление о статусе заказа #%s",
                              order_id)
        return

    await ack()

# ================= РОЛИ =================
def is_manager(user_id):
    return user_id == MANAGER_CHAT_ID or user_id in ADMIN_IDS

def is_admin(user_id):
    return user_id in ADMIN_IDS

# ================= ГЛАВНЫЙ ЦИКЛ =================
async def _handle_bot_started(update):
    """Обработка нажатия системной кнопки «Старт» в MAX.
    Форматы bot_started могут отличаться: поддерживаем несколько вариантов полей.
    """
    user_obj = (update.get("user") or update.get("sender")
                or update.get("from") or {})
    if not isinstance(user_obj, dict):
        user_obj = {}
    user_id = (user_obj.get("user_id") or user_obj.get("id")
               or update.get("user_id") or update.get("sender_id"))
    msg = update.get("message") or {}
    recipient = msg.get("recipient") or {}
    chat_obj = update.get("chat") or {}
    chat_id = (update.get("chat_id") or recipient.get("chat_id")
               or msg.get("chat_id") or chat_obj.get("chat_id") or user_id)
    try:
        user_id = int(user_id) if user_id is not None else None
        chat_id = int(chat_id) if chat_id is not None else None
    except (TypeError, ValueError):
        log.warning("bot_started: не удалось разобрать ID: %r", update)
        return

    if chat_id is None:
        log.warning("bot_started без chat_id: %r", update)
        return

    if user_id is not None:
        await remember_user_chat(user_id, chat_id)
        existing_user = await get_user(user_id)
        if existing_user and existing_user["agreed_offer"]:
            st_clear(user_id)
            await send_menu(chat_id)
        else:
            st_set(user_id, "waiting_offer")
            await send_offer_screen(chat_id)
    else:
        # Даже если MAX не прислал user_id, отвечаем в известный чат,
        # чтобы пользователь не видел полностью молчащий запуск.
        await send_offer_screen(chat_id)
    log.info("Обработан bot_started user_id=%s chat_id=%s", user_id, chat_id)


async def poll_loop():
    marker = None
    log.info("Бот запущен. Long polling активен.")
    while True:
        resp = await api.get_updates(marker=marker, timeout=30)
        if resp is None:
            await asyncio.sleep(5)
            continue
        if not isinstance(resp, dict):
            log.error("Некорректный ответ /updates: %r", resp)
            await asyncio.sleep(3)
            continue
        marker = resp.get("marker", marker)
        updates = resp.get("updates") or []
        for update in updates:
            if not isinstance(update, dict):
                continue
            ut = update.get("update_type") or update.get("type")
            try:
                if ut == "message_created":
                    await handle_message(update.get("message") or {})
                elif ut == "message_callback":
                    await handle_callback(update)
                elif ut == "bot_started":
                    await _handle_bot_started(update)
                else:
                    log.info("Пропущено неизвестное обновление type=%r keys=%s",
                             ut, list(update.keys()))
            except Exception:
                log.exception("Ошибка обработки обновления type=%r update=%r",
                              ut, update)

async def main():
    if not BOT_TOKEN or BOT_TOKEN == "ВСТАВЬ_ТОКЕН":
        raise SystemExit("Не задан MAX_BOT_TOKEN — добавьте переменную окружения "
                         "на bothost.ru (MAX_TOKEN / MAX_BOT_TOKEN).")
    await init_db()
    if yookassa_configured():
        pending = await orders_pending_yookassa()
        for row in pending:
            asyncio.create_task(_yk_watch(int(row["id"]), row["payment_id"]))
        if pending:
            log.info("ЮKassa: возобновлён контроль %d незавершённых платежей", len(pending))
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
