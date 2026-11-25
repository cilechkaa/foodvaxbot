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
import base64
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

def is_admin_user(user_id):
    """True для администратора или настроенного кассира."""
    try:
        uid = int(user_id)
        return uid in ADMIN_IDS or uid == int(MANAGER_CHAT_ID)
    except (TypeError, ValueError):
        return False

FREE_DELIVERY_FROM = Decimal("1000")
DELIVERY_COST = Decimal("200")

DB_PATH = os.getenv("DB_PATH", "data/shop.db")

# ================= ЮKASSA: НАСТРОЙКИ =================
# Bothost передаёт эти значения в окружение контейнера.
# В панели Bothost должны быть созданы переменные с ТОЧНЫМИ именами:
#   YOOKASSA_SHOP_ID
#   YOOKASSA_SECRET_KEY
# Секретный ключ никогда не прописываем в исходный код.
YOOKASSA_SHOP_ID = (os.getenv("YOOKASSA_SHOP_ID") or "").strip()
YOOKASSA_SECRET_KEY = (os.getenv("YOOKASSA_SECRET_KEY") or "").strip()
YOOKASSA_API_URL = "https://api.yookassa.ru/v3"
YOOKASSA_POLL_INTERVAL = 20      # секунд между проверками статуса платежа
YOOKASSA_WATCH_HOURS = 12        # сколько часов бот следит за платежом
YOOKASSA_NAME = "СВОЯ КУХНЯ 89"

def yookassa_configured():
    """Проверяет, что обе переменные Bothost реально переданы процессу."""
    return YOOKASSA_SHOP_ID.isdigit() and len(YOOKASSA_SECRET_KEY) >= 10

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
            CREATE TABLE IF NOT EXISTS couriers (
                user_id INTEGER PRIMARY KEY,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""")
        await db.execute("""
            CREATE TABLE IF NOT EXISTS courier_order_messages (
                order_id INTEGER NOT NULL, courier_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, message_id TEXT,
                PRIMARY KEY(order_id, courier_id)
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
                         ("delivery_time", "TEXT"),
                         ("courier_id", "INTEGER")]:
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
            MANAGER_CHAT_ID = int(manager_row[0]) # ================= ПОЛЬЗОВАТЕЛИ И НАСТРОЙКИ =================
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

async def courier_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM couriers ORDER BY added_at") as cur:
            return [int(row[0]) for row in await cur.fetchall()]

async def courier_add(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO couriers(user_id) VALUES(?)", (int(user_id),))
        await db.commit()

async def courier_remove(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM couriers WHERE user_id=?", (int(user_id),))
        await db.commit()

async def courier_is(user_id: int) -> bool:
    if is_manager(user_id):
        return True
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT 1 FROM couriers WHERE user_id=?", (int(user_id),)) as cur:
            return await cur.fetchone() is not None

async def courier_claim_order(order_id: int, courier_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            "UPDATE orders SET courier_id=? WHERE id=? AND courier_id IS NULL AND status='paid'",
            (int(courier_id), int(order_id)))
        await db.commit()
        return cur.rowcount == 1

async def courier_set_order_status(order_id: int, courier_id: int, status: str) -> bool:
    allowed = "status IN ('paid','cooking','delivering')" if status == "delivering" else "status='delivering'"
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute(
            f"UPDATE orders SET status=? WHERE id=? AND courier_id=? AND {allowed}",
            (status, int(order_id), int(courier_id)))
        await db.commit()
        return cur.rowcount == 1

async def courier_store_message(order_id, courier_id, chat_id, message_id):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO courier_order_messages(order_id,courier_id,chat_id,message_id) VALUES(?,?,?,?)",
                         (int(order_id), int(courier_id), int(chat_id), str(message_id) if message_id else None))
        await db.commit()

async def courier_messages(order_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM courier_order_messages WHERE order_id=?", (int(order_id),)) as cur:
            return await cur.fetchall()

async def courier_active_orders(courier_id):
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM orders WHERE courier_id=? AND status IN ('paid','cooking','delivering') ORDER BY id DESC", (int(courier_id),)) as cur:
            return await cur.fetchall()

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

def _yk_auth_header():
    """Заголовок Authorization для API ЮKassa (aiohttp.BasicAuth устарел)."""
    token = base64.b64encode(f"{YOOKASSA_SHOP_ID}:{YOOKASSA_SECRET_KEY}".encode()).decode()
    return "Basic " + token

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
            # amount.value в чеке — цена за единицу; ЮKassa умножает её на
            # quantity. Одна строка корзины = один расчёт на полную сумму.
            "quantity": "1.00",
            "amount": {"value": _money(value), "currency": "RUB"},
            "payment_mode": "full_prepayment",
            "payment_subject": "commodity",
            "vat_code": 1,  # без НДС
        })
    if (sum(sums) + delivery_cost).quantize(Decimal("0.01")) != final_total:
        # Суммы разошлись — отдаём весь заказ одной позицией на итог платежа.
        return [{
            "description": f"Заказ — {YOOKASSA_NAME}",
            "quantity": "1.00",
            "amount": {"value": _money(final_total), "currency": "RUB"},
            "payment_mode": "full_prepayment",
            "payment_subject": "commodity",
            "vat_code": 1,
        }]
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
        if not ph:
            log.error("ЮKassa: для заказа #%s не передан корректный телефон покупателя", order_id)
            return None
        receipt["customer"] = {"phone": ph}
        body["receipt"] = receipt
    headers = {"Idempotence-Key": uuid.uuid4().hex, "Content-Type": "application/json",
               "Authorization": _yk_auth_header()}
    try:
        session = await _yk_http()
        async with session.post(
            f"{YOOKASSA_API_URL}/payments",
            json=body,
            headers=headers,
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
            headers={"Authorization": _yk_auth_header()},
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
            headers={"Idempotence-Key": uuid.uuid4().hex,
                     "Authorization": _yk_auth_header()},
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

async def cancel_pending_user_payments(user_id, chat_id=None):
    """Отменяет неоплаченные платежи ЮKassa пользователя. Старые ссылки остаются
    действительными с прежней суммой, поэтому при любом изменении корзины их
    нужно закрыть — новая ссылка с актуальной суммой создаётся при оформлении.
    Возвращает число отменённых платежей."""
    if not yookassa_configured():
        return 0
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT id, payment_id FROM orders "
            "WHERE user_id = ? AND status = 'new' AND payment_id IS NOT NULL",
            (user_id,))
        rows = await cur.fetchall()
    for row in rows:
        await order_set_status(row["id"], "cancelled")
        asyncio.create_task(_yk_cancel_after_cancel_order(row["payment_id"], row["id"]))
    if rows:
        log.info("ЮKassa: отменено %s неоплаченных платежей пользователя %s (корзина изменилась)",
                 len(rows), user_id)
        if chat_id:
            try:
                await api.send(chat_id,
                               "ℹ️ Корзина изменилась — прежняя ссылка на оплату отменена. "
                               "Оформите заказ заново: придёт новая ссылка с актуальной суммой.")
            except Exception:
                log.exception("Не удалось уведомить клиента об отмене старой ссылки")
    return len(rows)

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

def _extract_message_id(result):
    """Достаёт идентификатор сообщения из ответа MAX API."""
    if not isinstance(result, dict):
        return None
    for key in ("message_id", "mid"):
        if result.get(key) is not None:
            return result[key]
    for key in ("message", "body", "result"):
        value = result.get(key)
        if isinstance(value, dict):
            found = _extract_message_id(value)
            if found is not None:
                return found
    return None

async def courier_notify_order(order_id):
    order = await order_get(order_id)
    if not order or order["status"] != "paid" or order["courier_id"]:
        return
    for courier_id in await courier_ids():
        # Не отправляем дубликаты при повторной проверке платежа.
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("SELECT 1 FROM courier_order_messages WHERE order_id=? AND courier_id=?",
                                  (order_id, courier_id)) as cur:
                if await cur.fetchone():
                    continue
        chat_id = await user_chat_id(courier_id)
        if not chat_id:
            log.warning("Курьер %s ещё не открыл бота командой /start", courier_id)
            continue
        text = (f"🛵 **Новый заказ #{order_id}**\n"
                f"Сумма: {order['total']} ₽\n"
                f"Клиент: {order['name']}\nТелефон: {order['phone']}\n"
                f"Адрес: {order['address']}\nВремя: {order['delivery_time']}\n"
                f"Комментарий: {order['comment'] or '—'}\n\n"
                f"{order['items']}")
        result = await api.send(chat_id, text, attachments=[courier_take_keyboard(order_id)])
        if result is not None:
            await courier_store_message(order_id, courier_id, chat_id, _extract_message_id(result))

async def courier_hide_order_from_others(order_id, assigned_courier_id):
    for row in await courier_messages(order_id):
        if int(row["courier_id"]) == int(assigned_courier_id):
            continue
        if row["message_id"]:
            await api.edit(int(row["chat_id"]), row["message_id"],
                           f"🚫 Заказ #{order_id} уже забрал другой курьер.", attachments=[])

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
    await courier_notify_order(order_id)
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

def courier_take_keyboard(order_id):
    return kb([[cb_btn("🛵 Взять заказ", f"courier:take:{order_id}")]])

def courier_progress_keyboard(order_id):
    return kb([[cb_btn("📦 Забрал заказ", f"courier:picked:{order_id}")]])

def courier_deliver_keyboard(order_id):
    return kb([[cb_btn("✅ Доставил заказ", f"courier:delivered:{order_id}")]])

def courier_panel_keyboard():
    return kb([[cb_btn("📦 Мои активные заказы", "courier:active")]])

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
        [cb_btn("🛵 Управление курьерами", "admin:couriers")],
        [cb_btn("🧹 Сбросить все заказы", "admin:orders_clear_confirm")],
        [cb_btn("📦 Остатки / стоп-лист", "admin:stock")],
        [cb_btn("📈 Топ продаж", "admin:stats")],
        [cb_btn("📊 Загрузить меню Excel", "admin:excel")],
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

def _item_field(item, field, default=None):
    """Безопасно читает поле из sqlite3.Row, dict и других mapping-объектов."""
    if item is None:
        return default
    try:
        return item[field]
    except (KeyError, IndexError, TypeError):
        pass
    # Не вызываем .get(): sqlite3.Row этого метода не имеет.
    try:
        keys = item.keys()
        if field in keys:
            return item[field]
    except (AttributeError, TypeError, KeyError, IndexError):
        pass
    if isinstance(item, dict) and field in item:
        return item[field]
    return default


def is_test_product(item):
    """True только для товара «Тест» стоимостью ровно 1 рубль."""
    if item is None:
        return False
    name = _item_field(item, "name", "")
    price = _item_field(item, "price", "0")
    try:
        return (str(name).strip().casefold() == "тест"
                and Decimal(str(price).strip().replace(",", ".")) == Decimal("1"))
    except (InvalidOperation, TypeError, ValueError):
        return False


async def cart_text_and_keyboard(user_id):
    rows = await cart_get(user_id)
    if not rows:
        return "Корзина пуста.", kb([[cb_btn("🏠 Главное меню", "main_menu")]])

    lines, kb_buttons = [], []
    total = Decimal("0")
    only_test_products = True
    garnishes = await get_all_garnishes(user_id)

    for key, qty in rows:
        item_key, _ = parse_cart_key(key)
        item = await menu_get(item_key)
        if not item:
            continue
        if not is_test_product(item):
            only_test_products = False
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

    if only_test_products:
        text += "\n🚚 Доставка для тестового товара бесплатная!"
    elif total < MIN_ORDER_AMOUNT and not is_admin_user(user_id):
        missing = MIN_ORDER_AMOUNT - total
        text += f"\n⚠️ Минимальный заказ: {MIN_ORDER_AMOUNT} ₽. Добавьте ещё на {missing} ₽"
    elif total < FREE_DELIVERY_FROM and not is_admin_user(user_id):
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

    if text.startswith("/courier") and await courier_is(user_id):
        await api.send(chat_id, "🛵 Панель курьера:", attachments=[courier_panel_keyboard()])
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
    if sname == "admin_courier_id" and text:
        raw_id = text.strip()
        if not raw_id.isdigit() or int(raw_id) <= 0:
            await api.send(chat_id, "Введите положительный числовой MAX ID курьера:")
            return
        courier_id = int(raw_id)
        if is_admin(courier_id):
            await api.send(chat_id, "Этот пользователь уже администратор. Введите ID другого курьера:")
            return
        await courier_add(courier_id)
        st_clear(user_id)
        await api.send(chat_id,
                       f"✅ Курьер {courier_id} добавлен. Попросите его открыть бота и отправить /start, чтобы получать заказы.",
                       attachments=[admin_panel_keyboard()])
        return

    if sname == "admin_courier_remove_id" and text:
        raw_id = text.strip()
        if not raw_id.isdigit() or int(raw_id) <= 0:
            await api.send(chat_id, "Введите положительный числовой MAX ID курьера:")
            return
        courier_id = int(raw_id)
        await courier_remove(courier_id)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Курьер {courier_id} удалён из списка уведомлений.",
                       attachments=[admin_panel_keyboard()])
        return

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
        normalized_phone = _yk_phone(text)
        if not normalized_phone:
            await api.send(chat_id, "Некорректный номер. Введите российский номер, например +79991234567 или 89991234567:")
            return
        user = await get_user(user_id)
        name = user["name"] if user and user["name"] else "Гость"
        address = user["address"] if user and user["address"] else "—"
        await save_user(user_id, name, normalized_phone, address)
        st_clear(user_id)
        await api.send(chat_id, f"✅ Телефон сохранён: {normalized_phone}")
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

    if sname == "waiting_phone_payment":
        normalized_phone = _yk_phone(text)
        if not normalized_phone:
            await api.send(chat_id, "Некорректный номер. Введите российский номер, например +79991234567 или 89991234567:")
            return
        data = st_data(user_id)
        data["phone"] = normalized_phone
        user = await get_user(user_id)
        name = data.get("name") or (user["name"] if user and user["name"] else "Гость")
        address = data.get("address") or (user["address"] if user and user["address"] else "—")
        await save_user(user_id, name, normalized_phone, address)
        st_set(user_id, "waiting_comment", **data)
        await api.send(chat_id, "✅ Телефон сохранён. Продолжаю оформление заказа.")
        await finalize_order(chat_id, user_id, username)
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
        await api.send(chat_id, "Укажите номер телефона (можно с 8 или +7):")
        return

    if sname == "waiting_phone":
        normalized_phone = _yk_phone(text)
        if not normalized_phone:
            await api.send(chat_id, "Некорректный номер. Введите российский номер, например +79991234567 или 89991234567:")
            return
        data = st_data(user_id)
        data["phone"] = normalized_phone
        user = await get_user(user_id)
        address = user["address"] if user and user["address"] else "—"
        await save_user(user_id, data.get("name", "Гость"), normalized_phone, address)
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

    normalized_phone = _yk_phone(data.get("phone"))
    if not normalized_phone:
        user = await get_user(user_id)
        data["phone"] = user["phone"] if user and user["phone"] else ""
        normalized_phone = _yk_phone(data["phone"])
    if not normalized_phone:
        st_set(user_id, "waiting_phone_payment", **data)
        await api.send(
            chat_id,
            "Для фискального чека ЮKassa нужен корректный номер телефона. "
            "Введите номер в формате +7XXXXXXXXXX или 8XXXXXXXXXX:"
        )
        return
    data["phone"] = normalized_phone
    await save_user(user_id, data.get("name", "—"),
                    normalized_phone, data.get("address", "—"))

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

    if not yookassa_configured():
        await api.send(
            chat_id,
            "⚠️ Онлайн-оплата временно недоступна. "
            "Пожалуйста, попробуйте оформить заказ позже или свяжитесь с кассиром.",
            attachments=[kb([[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]])])
        return

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
    only_test_products = bool(receipt_lines) and all(
        is_test_product({"name": name, "price": price})
        for name, qty, price in receipt_lines
    )
    if only_test_products or is_admin_user(user_id) or base >= FREE_DELIVERY_FROM:
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

    # ===== Создание платежа ЮKassa (единственный способ оплаты) =====
    receipt = _yk_build_receipt(receipt_lines, discount_rate, delivery_cost, final_total)
    payment = await yookassa_create_payment(
        order_id, final_total,
        f"Заказ №{order_id} — {YOOKASSA_NAME}",
        receipt, phone=data.get("phone"))

    if not payment:
        await order_set_status(order_id, "cancelled")
        await decrement_order_count(user_id)
        await api.send(
            chat_id,
            "❌ Не удалось создать платёж в ЮKassa. Заказ не принят. "
            "Пожалуйста, попробуйте ещё раз позже.",
            attachments=[kb([[link_btn("💬 Написать кассиру", MANAGER_USERNAME)]])])
        log.error("ЮKassa: платёж для заказа #%s не создан; заказ отменён", order_id)
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        st_clear(user_id)
        return

    await order_set_payment_id(order_id, payment["id"])
    log.info("ЮKassa: платёж %s создан для заказа #%s", payment["id"], order_id)

    pay_url = (payment.get("confirmation") or {}).get("confirmation_url")
    if not pay_url:
        await order_set_status(order_id, "cancelled")
        await decrement_order_count(user_id)
        await api.send(chat_id, "❌ ЮKassa не вернула ссылку для оплаты. Заказ не принят.")
        log.error("ЮKassa: у платежа %s отсутствует confirmation_url", payment.get("id"))
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        st_clear(user_id)
        return

    manager_sent = await api.notify_staff(
        manager_text, attachments=[manager_keyboard(order_id, "new")])
    if not manager_sent:
        log.error("Заказ #%s сохранён, платёж создан, но уведомление не доставлено сотрудникам.",
                  order_id)

    user_text = (
        "✅ Ваш заказ оформлен! Остался последний шаг — оплатить его через ЮKassa.\n\n"
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
        "После успешной оплаты бот автоматически подтвердит платёж."
    )

    buttons = [
        [link_btn("💳 Оплатить через ЮKassa", pay_url)],
        [link_btn("💬 Написать кассиру", MANAGER_USERNAME)],
        [cb_btn("❌ Отменить заказ", f"cancelorder:{order_id}")],
    ]
    customer_result = await api.send(chat_id, user_text, attachments=[kb(buttons)])
    if customer_result is None:
        log.error("Не удалось отправить подтверждение заказа #%s клиенту chat_id=%s",
                  order_id, chat_id)

    pay_msg = await api.send(
        chat_id,
        "💳 **Оплата через ЮKassa**\n"
        f"Сумма: **{final_total} ₽**\n\n"
        "Нажмите кнопку «Оплатить через ЮKassa». Доступные способы оплаты "
        "покажет платёжная страница. После оплаты статус обновится автоматически.",
        attachments=[kb([
            [link_btn("💳 Оплатить через ЮKassa", pay_url)],
            [cb_btn("🔄 Проверить оплату", f"paid:{order_id}")],
            [cb_btn("❌ Отменить заказ", f"cancelorder:{order_id}")],
        ])])
    if pay_msg is None:
        log.error("Не удалось отправить платёжное сообщение для заказа #%s", order_id)

    asyncio.create_task(_yk_watch(order_id, payment["id"]))

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
            await ack("Блюдо недоступно")
            return
        if item["stock"] is not None and item["stock"] <= 0:
            await ack("Сейчас нет в наличии")
            return
        if await category_requires_garnish(item["category_key"]):
            await ack()
            await api.send(chat_id,
                           f"Выберите гарнир к «{item['name']}»:",
                           attachments=[garnish_keyboard(key)])
            return
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_add(user_id, key)
        await ack("Добавлено в корзину")
        return

    if payload.startswith("garnish:"):
        parts = payload.split(":")
        if len(parts) != 3:
            await ack("Ошибка")
            return
        _, key, garnish = parts
        item = await menu_get(key)
        if not item:
            await ack("Блюдо недоступно")
            return
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_add(user_id, key, garnish)
        await set_garnish(user_id, key, garnish)
        await ack("Добавлено в корзину")
        return

    if payload.startswith("fav:"):
        key = payload.split(":")[1]
        if await is_favorite(user_id, key):
            await favorites_remove(user_id, key)
            await ack("Убрано из избранного")
        else:
            await favorites_add(user_id, key)
            await ack("Добавлено в избранное")
        return

    if payload == "favs":
        fav_keys = await favorites_all(user_id)
        buttons = []
        for key in fav_keys:
            item = await menu_get(key)
            if item and item["active"]:
                buttons.append([cb_btn(item_label(item), f"view:{key}")])
        buttons.append([cb_btn("🏠 Главное меню", "main_menu")])
        await ack()
        await api.send(chat_id, "❤️ Избранное:" if fav_keys else "Избранное пусто.",
                       attachments=[kb(buttons)])
        return

    if payload == "search_hint":
        await ack()
        await api.send(chat_id, "🔍 Напишите название блюда для поиска:")
        return

    if payload == "cart":
        text, cart_kb = await cart_text_and_keyboard(user_id)
        await ack()
        await api.send(chat_id, text, attachments=[cart_kb])
        return

    if payload.startswith("cartadd:"):
        key = payload.split(":", 1)[1]
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_add(user_id, key)
        await ack("Добавлено")
        text, cart_kb = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[cart_kb])
        return

    if payload.startswith("cartsub:"):
        key = payload.split(":", 1)[1]
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_remove_one(user_id, key)
        await ack()
        text, cart_kb = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[cart_kb])
        return

    if payload.startswith("cartdel:"):
        key = payload.split(":", 1)[1]
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_delete_item(user_id, key)
        await ack("Удалено")
        text, cart_kb = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[cart_kb])
        return

    if payload == "cart_clear":
        await cancel_pending_user_payments(user_id, chat_id)
        await cart_clear(user_id)
        await clear_garnishes(user_id)
        await ack("Корзина очищена")
        text, cart_kb = await cart_text_and_keyboard(user_id)
        await api.send(chat_id, text, attachments=[cart_kb])
        return

    if payload == "order":
        rows = await cart_get(user_id)
        if not rows:
            await ack("Корзина пуста")
            return
        # Считаем сумму, чтобы проверить минимальный заказ
        total = Decimal("0")
        for key, qty in rows:
            item_key, _ = parse_cart_key(key)
            item = await menu_get(item_key)
            if item:
                total += Decimal(item["price"]) * qty
        if total < MIN_ORDER_AMOUNT and not is_admin_user(user_id):
            await ack(f"Минимальный заказ {MIN_ORDER_AMOUNT} ₽")
            return
        user = await get_user(user_id)
        data = {}
        if not user or not user["name"]:
            st_set(user_id, "waiting_name")
            await ack()
            await api.send(chat_id, "Укажите ваше имя:")
            return
        data["name"] = user["name"]
        if not user["phone"] or user["phone"] == "—":
            st_set(user_id, "waiting_phone", **data)
            await ack()
            await api.send(chat_id, "Укажите номер телефона (можно с 8 или +7):")
            return
        data["phone"] = user["phone"]
        if not user["address"] or user["address"] == "—":
            st_set(user_id, "waiting_address", **data)
            await ack()
            await api.send(chat_id, "Укажите адрес доставки:")
            return
        data["address"] = user["address"]
        st_set(user_id, "waiting_time", **data)
        await ack()
        buttons = [[cb_btn("🚀 Как можно скорее", "time:now")]]
        await api.send(chat_id,
                       "⏰ Когда доставить заказ?\n\n"
                       "• Нажмите «Как можно скорее»\n"
                       "• Или напишите время, например: «к 19:00»",
                       attachments=[kb(buttons)])
        return

    if payload == "time:now":
        data = st_data(user_id)
        data["delivery_time"] = "Как можно скорее"
        st_set(user_id, "waiting_comment", **data)
        await ack()
        buttons = [[cb_btn("Пропустить и оплатить", "skip_comment")]]
        await api.send(chat_id, "Комментарий к заказу (или «Пропустить и оплатить»):",
                       attachments=[kb(buttons)])
        return

    if payload == "skip_comment":
        data = st_data(user_id)
        data["comment"] = "—"
        st_set(user_id, "waiting_comment", **data)
        await ack()
        await finalize_order(chat_id, user_id, callback_username)
        return

    if payload == "edit_phone":
        st_set(user_id, "edit_phone")
        await ack()
        await api.send(chat_id, "Введите новый номер телефона (можно с 8 или +7):")
        return

    if payload == "edit_address":
        st_set(user_id, "edit_address")
        await ack()
        await api.send(chat_id, "Введите новый адрес доставки:")
        return

    if payload == "profile":
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
        await api.send(chat_id, "Введите новый адрес (улица, дом, квартира):")
        return

    if payload.startswith("address_edit:"):
        address_id = payload.split(":")[1]
        st_set(user_id, "profile_address_edit", address_id=address_id)
        await ack()
        await api.send(chat_id, "Введите новый текст адреса:")
        return

    if payload.startswith("address_delete:"):
        address_id = int(payload.split(":")[1])
        await address_delete(user_id, address_id)
        await ack("Адрес удалён")
        await send_addresses(chat_id, user_id)
        return

    if payload.startswith("address_default:"):
        address_id = int(payload.split(":")[1])
        await address_set_default(user_id, address_id)
        await ack("Основной адрес обновлён")
        await send_addresses(chat_id, user_id)
        return

    if payload == "history":
        orders = await orders_of_user(user_id, limit=10)
        if not orders:
            await ack()
            await api.send(chat_id, "У вас пока нет заказов.",
                           attachments=[kb([[cb_btn("🏠 Главное меню", "main_menu")]])])
            return
        lines = []
        for o in orders:
            status = STATUS_LABELS.get(o["status"], o["status"])
            lines.append(f"#{o['id']} — {o['total']} ₽ — {status} — {o['created_at']}")
        await ack()
        await api.send(chat_id, "📜 **Ваши последние заказы:**\n\n" + "\n".join(lines),
                       attachments=[kb([[cb_btn("🏠 Главное меню", "main_menu")]])])
        return

    if payload.startswith("cancelorder:"):
        order_id = int(payload.split(":")[1])
        ok = await order_cancel_if_allowed(order_id, user_id)
        if ok:
            await ack("Заказ отменён")
            order = await order_get(order_id)
            if order and order["payment_id"]:
                asyncio.create_task(_yk_cancel_after_cancel_order(order["payment_id"], order_id))
            await api.send(chat_id, f"❌ Заказ #{order_id} отменён.")
            await api.notify_staff(f"❌ Клиент отменил заказ #{order_id}.")
        else:
            await ack("Отменить уже нельзя")
        return

    if payload.startswith("paid:"):
        order_id = int(payload.split(":")[1])
        order = await order_get(order_id)
        if not order or order["user_id"] != user_id:
            await ack("Заказ не найден")
            return
        if not order["payment_id"]:
            await ack("Платёж для этого заказа не найден")
            return

        payment_info = await yookassa_get_payment(order["payment_id"])
        if not payment_info:
            await ack("Не удалось проверить платёж. Попробуйте позже.")
            return

        payment_status = payment_info.get("status")
        if payment_status == "succeeded":
            if order["status"] == "new":
                await order_set_status(order_id, "paid")
                await _yk_notify_paid(order_id)
            await ack("Оплата подтверждена")
            await api.send(chat_id, f"✅ Оплата заказа #{order_id} подтверждена ЮKassa!")
        elif payment_status == "canceled":
            await ack("Платёж отменён")
            await api.send(chat_id, f"❌ Платёж по заказу #{order_id} отменён.")
        else:
            await ack("Оплата пока не поступила")
            await api.send(
                chat_id,
                f"⏳ Заказ #{order_id}: платёж пока не подтверждён. "
                "Если вы только что оплатили, подождите немного и проверьте снова.")
        return

    if payload.startswith("courier:"):
        parts = payload.split(":")
        action = parts[1] if len(parts) > 1 else ""
        if not await courier_is(user_id):
            await ack("Нет доступа: вы не добавлены как курьер")
            return
        if action == "active":
            orders = await courier_active_orders(user_id)
            await ack()
            if not orders:
                await api.send(chat_id, "Активных заказов у вас нет.", attachments=[courier_panel_keyboard()])
                return
            for o in orders:
                text = (f"📦 **Ваш заказ #{o['id']}**\nСумма: {o['total']} ₽\n"
                        f"Клиент: {o['name']}\nТелефон: {o['phone']}\n"
                        f"Адрес: {o['address']}\nВремя: {o['delivery_time']}\n"
                        f"Статус: {STATUS_LABELS.get(o['status'], o['status'])}")
                keyboard = courier_deliver_keyboard(o['id']) if o['status'] == 'delivering' else courier_progress_keyboard(o['id'])
                await api.send(chat_id, text, attachments=[keyboard])
            return
        if len(parts) != 3 or not parts[2].isdigit():
            await ack("Некорректный заказ")
            return
        order_id = int(parts[2])
        order = await order_get(order_id)
        if not order:
            await ack("Заказ не найден")
            return
        if action == "take":
            claimed = await courier_claim_order(order_id, user_id)
            if not claimed:
                await ack("Заказ уже взят или недоступен")
                failed_msg = cb.get("message") or {}
                failed_mid = _extract_message_id(failed_msg)
                if failed_mid:
                    await api.edit(chat_id, failed_mid,
                                   f"🚫 Заказ #{order_id} уже занят или недоступен.", attachments=[])
                return
            await ack("Заказ закреплён за вами")
            await courier_hide_order_from_others(order_id, user_id)
            current_mid = _extract_message_id(msg)
            if current_mid:
                await api.edit(chat_id, current_mid,
                               f"🛵 **Вы взяли заказ #{order_id}**\nАдрес: {order['address']}\nТелефон: {order['phone']}",
                               attachments=[courier_progress_keyboard(order_id)])
            return
        if int(order["courier_id"] or 0) != int(user_id):
            await ack("Этот заказ закреплён за другим курьером")
            return
        if action == "picked":
            if await courier_set_order_status(order_id, user_id, "delivering"):
                await ack("Отмечено: заказ забран")
                await api.send_to_user(order["user_id"], f"📦 Заказ #{order_id} забран курьером и направляется к вам.")
                current_mid = _extract_message_id(msg)
                if current_mid:
                    await api.edit(chat_id, current_mid,
                                   f"📦 **Заказ #{order_id} забран.**\nАдрес: {order['address']}",
                                   attachments=[courier_deliver_keyboard(order_id)])
            else:
                await ack("Не удалось изменить статус заказа")
            return
        if action == "delivered":
            if await courier_set_order_status(order_id, user_id, "done"):
                await ack("Заказ отмечен доставленным")
                await api.send_to_user(order["user_id"], f"✅ Заказ #{order_id} доставлен. Спасибо за заказ!")
                current_mid = _extract_message_id(msg)
                if current_mid:
                    await api.edit(chat_id, current_mid,
                                   f"✅ **Заказ #{order_id} доставлен.**", attachments=[])
            else:
                await ack("Не удалось изменить статус заказа")
            return
        await ack("Неизвестное действие")
        return

    if payload.startswith("setstatus:"):
        if not is_manager(user_id):
            await ack("Нет доступа")
            return
        parts = payload.split(":")
        if len(parts) != 3:
            await ack("Ошибка")
            return
        _, order_id, new_status = parts
        order_id = int(order_id)
        if new_status not in STATUS_LABELS:
            await ack("Неизвестный статус")
            return
        await order_set_status(order_id, new_status)
        await ack(f"Статус: {STATUS_LABELS[new_status]}")
        order = await order_get(order_id)
        if order:
            try:
                await api.send_to_user(
                    order["user_id"],
                    f"📦 Статус заказа #{order_id}: {STATUS_LABELS[new_status]}")
            except Exception:
                log.exception("Не удалось уведомить клиента о статусе #%s", order_id)
        return

    if payload.startswith("mgr:"):
        if not is_manager(user_id):
            await ack("Нет доступа")
            return
        status = payload.split(":")[1]
        orders = await orders_by_status(status, limit=20)
        if not orders:
            await ack("Нет заказов")
            return
        await ack()
        for o in orders:
            text = (f"#{o['id']} — {o['total']} ₽\n"
                    f"Клиент: {o['name']} ({o['phone']})\n"
                    f"Адрес: {o['address']}\n"
                    f"Время: {o['delivery_time']}\n"
                    f"Коммент: {o['comment'] or '—'}")
            await api.send(chat_id, text,
                           attachments=[manager_keyboard(o["id"], o["status"])])
        return

    if payload.startswith("admin:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        action = payload.split(":", 1)[1]

        if action == "back":
            await ack()
            await api.send(chat_id, "Админ-панель:",
                           attachments=[admin_panel_keyboard()])
            return

        if action == "add":
            st_set(user_id, "admin_add_key")
            await ack()
            await api.send(chat_id, "Латинский ключ блюда (например, burger):")
            return

        if action == "list":
            items = await menu_all(active_only=False)
            if not items:
                await ack("Меню пустое")
                return
            await ack()
            buttons = [[cb_btn(item_label(i), f"aitem:{i['key']}")] for i in items]
            buttons.append([cb_btn("⬅️ В админ-панель", "admin:back")])
            await api.send(chat_id, "📋 Все блюда:", attachments=[kb(buttons)])
            return

        if action == "cats":
            cats = await categories_all()
            await ack()
            buttons = [[cb_btn(f"{c['display_name'] or c['name']} ({c['key']})",
                               f"acat:{c['key']}")] for c in cats]
            buttons.append([cb_btn("⬅️ В админ-панель", "admin:back")])
            await api.send(chat_id, "🗂 Категории:", attachments=[kb(buttons)])
            return

        if action == "cat_add":
            st_set(user_id, "admin_cat_key")
            await ack()
            await api.send(chat_id, "Латинский ключ категории (например, desserts):")
            return

        if action == "cat_del":
            cats = await categories_visible()
            await ack()
            buttons = [[cb_btn(f"🗑 {c['display_name'] or c['name']}",
                               f"acatdel:{c['key']}")] for c in cats]
            buttons.append([cb_btn("⬅️ В админ-панель", "admin:back")])
            await api.send(chat_id, "Выберите категорию для удаления:",
                           attachments=[kb(buttons)])
            return

        if action == "couriers":
            ids = await courier_ids()
            buttons = [[cb_btn("➕ Добавить курьера", "admin:courier_add")],
                       [cb_btn("➖ Удалить курьера", "admin:courier_remove")],
                       [cb_btn("🔄 Обновить список", "admin:couriers")],
                       [cb_btn("⬅️ В админ-панель", "admin:back")]]
            listing = "\n".join(f"• {cid}" for cid in ids) if ids else "Пока курьеров нет."
            await ack()
            await api.send(chat_id, "🛵 **Курьеры**\n" + listing, attachments=[kb(buttons)])
            return

        if action == "courier_add":
            st_set(user_id, "admin_courier_id")
            await ack()
            await api.send(chat_id, "Введите MAX ID курьера (узнать можно командой /myid):")
            return

        if action == "courier_remove":
            st_set(user_id, "admin_courier_remove_id")
            await ack()
            await api.send(chat_id, "Введите MAX ID курьера, которого нужно удалить:")
            return

        if action == "manager_id":
            st_set(user_id, "admin_manager_id")
            await ack()
            await api.send(chat_id, "Введите числовой ID кассира (из /myid):")
            return

        if action == "orders":
            orders = await orders_by_status("new", limit=20)
            await ack()
            if not orders:
                await api.send(chat_id, "Активных заказов нет.",
                               attachments=[kb([[cb_btn("⬅️ В админ-панель", "admin:back")]])])
                return
            for o in orders:
                text = (f"#{o['id']} — {o['total']} ₽\n"
                        f"Клиент: {o['name']} ({o['phone']})\n"
                        f"Адрес: {o['address']}")
                await api.send(chat_id, text,
                               attachments=[manager_keyboard(o["id"], o["status"])])
            return

        if action == "orders_clear_confirm":
            await ack()
            await api.send(chat_id, "⚠️ Удалить все заказы? Это необратимо.",
                           attachments=[kb([
                               [cb_btn("✅ Да, удалить", "admin:orders_clear")],
                               [cb_btn("⬅️ Отмена", "admin:back")]
                           ])])
            return

        if action == "orders_clear":
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM orders")
                await db.execute("DELETE FROM order_items")
                await db.commit()
            await ack("Заказы удалены")
            await api.send(chat_id, "🧹 Все заказы удалены.",
                           attachments=[admin_panel_keyboard()])
            return

        if action == "stats":
            all_time, today = await stats_summary()
            top = await sales_top(limit=5)
            text = (f"📈 **Статистика**\n\n"
                    f"Всего заказов: {all_time['cnt']}\n"
                    f"Выручка: {all_time['revenue']:.2f} ₽\n\n"
                    f"Сегодня заказов: {today['cnt']}\n"
                    f"Выручка сегодня: {today['revenue']:.2f} ₽\n\n"
                    "**Топ блюд:**")
            for t in top:
                item = await menu_get(t["item_key"])
                name = item["name"] if item else t["item_key"]
                text += f"\n• {name} — {t['total_qty']} шт."
            await ack()
            await api.send(chat_id, text,
                           attachments=[kb([[cb_btn("⬅️ В админ-панель", "admin:back")]])])
            return

        if action == "yk":
            if yookassa_configured():
                await ack()
                await api.send(chat_id,
                               f"✅ ЮKassa настроена.\nShop ID: {YOOKASSA_SHOP_ID}",
                               attachments=[admin_panel_keyboard()])
            else:
                await ack()
                await api.send(chat_id,
                               "⚠️ ЮKassa не настроена. Задайте переменные окружения "
                               "YOOKASSA_SHOP_ID и YOOKASSA_SECRET_KEY на хостинге.",
                               attachments=[admin_panel_keyboard()])
            return

        if action == "excel":
            st_set(user_id, "admin_excel")
            await ack()
            await api.send(chat_id, "Пришлите .xlsx файл как документ. Для отмены — «отмена».")
            return

        if action == "bulk_photos":
            st_set(user_id, "admin_bulk_photos")
            await ack()
            await api.send(chat_id,
                           "Пришлите фотографии блюд. Имя файла = название блюда. "
                           "Когда закончите — «готово».")
            return

        if action == "backup":
            await ack()
            if not os.path.exists(DB_PATH):
                await api.send(chat_id, "Файл БД не найден.")
                return
            try:
                with open(DB_PATH, "rb") as f:
                    db_bytes = f.read()
                token = await api.upload_bytes(db_bytes, "file", filename="shop.db")
                if token:
                    await api.send(chat_id, "💾 Резервная копия БД:",
                                   attachments=[{"type": "file",
                                                 "payload": {"token": token}}])
                else:
                    await api.send(chat_id, "Не удалось загрузить файл БД в MAX.")
            except Exception:
                log.exception("Ошибка резервного копирования БД")
                await api.send(chat_id, "Ошибка при создании резервной копии.")
            return

        if action == "menu_clear_confirm":
            await ack()
            await api.send(chat_id, "⚠️ Удалить всё меню? Это необратимо.",
                           attachments=[kb([
                               [cb_btn("✅ Да, удалить", "admin:menu_clear")],
                               [cb_btn("⬅️ Отмена", "admin:back")]
                           ])])
            return

        if action == "menu_clear":
            await menu_delete_all()
            await ack("Меню удалено")
            await api.send(chat_id, "🗑 Всё меню удалено.",
                           attachments=[admin_panel_keyboard()])
            return

        await ack("Неизвестное действие")
        return

    if payload.startswith("aitem:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        key = payload.split(":", 1)[1]
        item = await menu_get(key)
        if not item:
            await ack("Блюдо не найдено")
            return
        await ack()
        text = (f"**{item['name']}** — {item['price']} ₽\n"
                f"{item['description'] or 'Без описания'}\n"
                f"Категория: {item['category_key']}\n"
                f"Активно: {'да' if item['active'] else 'нет'}")
        await api.send(chat_id, text,
                       attachments=[admin_item_keyboard(key, bool(item["active"]))])
        return

    if payload.startswith("aedit:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        parts = payload.split(":")
        if len(parts) != 3:
            await ack("Ошибка")
            return
        _, key, field = parts
        st_set(user_id, "admin_edit_value", key=key, field=field)
        await ack()
        prompts = {
            "name": "Новое название:",
            "price": "Новая цена (число):",
            "description": "Новое описание:",
            "photo_token": "Пришлите новое фото:",
            "category_key": "Новый ключ категории:",
        }
        await api.send(chat_id, prompts.get(field, "Новое значение:"))
        return

    if payload.startswith("atoggle:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        parts = payload.split(":")
        if len(parts) != 3:
            await ack("Ошибка")
            return
        _, key, action = parts
        new_active = 0 if action == "hide" else 1
        await menu_update(key, "active", new_active)
        await ack("Скрыто" if new_active == 0 else "Показано")
        item = await menu_get(key)
        if item:
            await api.send(chat_id, f"Блюдо «{item['name']}» обновлено.",
                           attachments=[admin_item_keyboard(key, bool(new_active))])
        return

    if payload.startswith("adel:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        key = payload.split(":", 1)[1]
        await menu_delete(key)
        await ack("Удалено")
        await api.send(chat_id, "🗑 Блюдо удалено.",
                       attachments=[admin_panel_keyboard()])
        return

    if payload.startswith("acat:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        key = payload.split(":", 1)[1]
        st_set(user_id, "admin_cat_rename", key=key)
        await ack()
        await api.send(chat_id, "Введите новое название категории:")
        return

    if payload.startswith("acatdel:"):
        if not is_admin(user_id):
            await ack("Нет доступа")
            return
        key = payload.split(":", 1)[1]
        await category_delete(key)
        await ack("Категория удалена")
        await api.send(chat_id, "🗑 Категория удалена.",
                       attachments=[admin_panel_keyboard()])
        return

    if payload.startswith("pickcat:"):
        data = st_data(user_id)
        data["category_key"] = payload.split(":", 1)[1]
        st_set(user_id, "admin_add_photo", **data)
        await ack()
        await api.send(chat_id, "Пришлите фото блюда или напишите «пропустить»:")
        return

    if payload == "admin_cancel":
        st_clear(user_id)
        await ack("Отменено")
        await api.send(chat_id, "Действие отменено.",
                       attachments=[admin_panel_keyboard()])
        return

    await ack()


# ================= ВСПОМОГАТЕЛЬНЫЕ ПРОВЕРКИ ДОСТУПА =================
def is_manager(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS or int(user_id) == MANAGER_CHAT_ID
    except (TypeError, ValueError):
        return False


def is_admin(user_id) -> bool:
    try:
        return int(user_id) in ADMIN_IDS
    except (TypeError, ValueError):
        return False


# ================= ЗАПУСК =================
async def main():
    await init_db()
    await api.start()
    try:
        # Подхватываем незавершённые платежи после перезапуска.
        for row in await orders_pending_yookassa():
            asyncio.create_task(_yk_watch(row["id"], row["payment_id"]))
        marker = None
        log.info("Бот запущен. Ожидаю события MAX...")
        while True:
            try:
                data = await api.get_updates(marker=marker, timeout=30)
                if not data:
                    continue
                marker = data.get("marker", marker)
                for update in data.get("updates", []):
                    utype = update.get("update_type") or update.get("type")
                    try:
                        if utype == "message_created":
                            await handle_message(update.get("message") or update)
                        elif utype == "message_callback":
                            await handle_callback(update)
                        elif utype == "bot_started":
                            # bot_started можно обрабатывать как /start
                            msg = update.get("message") or {}
                            if not msg:
                                msg = {"sender": update.get("user") or {},
                                       "recipient": update.get("chat") or {},
                                       "body": {"text": "/start"}}
                            await handle_message(msg)
                    except Exception:
                        log.exception("Ошибка обработки update: %r", update)
            except Exception:
                log.exception("Ошибка в главном цикле long polling")
                await asyncio.sleep(5)
    finally:
        await api.stop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Бот остановлен пользователем")
