import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.error import BadRequest
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ================== PATHS ==================

BASE_DIR = Path(__file__).resolve().parent
CATALOG_PATH = BASE_DIR / "catalog.json"
STOCK_PATH = BASE_DIR / "stock.json"
ORDERS_PATH = BASE_DIR / "orders.json"

# ================== LOGGING ==================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# ================== CONFIG ==================

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is not set")


def parse_admin_ids() -> List[int]:
    raw = os.getenv("ADMIN_IDS", "")
    ids: List[int] = []

    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))

    if not ids:
        # fallback, щоб не ламалося, якщо Railway variable ще не налаштована
        ids = [7406405860, 721379009]

    return ids


ADMIN_IDS = parse_admin_ids()

CITY_CONFIG = {
    "Berlin": {
        "title": "Берлін",
        "courier_chat_id": 8449852526,
        "courier_username": "@courier_fox",
    },
    "Leipzig": {
        "title": "Лейпциг",
        "courier_chat_id": 8401636475,
        "courier_username": "@leipzig_foxs",
    },
    "Dresden": {
        "title": "Дрезден",
        "courier_chat_id": 8501964969,
        "courier_username": "@dresden_fox",
    },
    "Other": {
        "title": "Інше",
        "courier_chat_id": 8449852526,
        "courier_username": "@courier_fox",
    },
}

NO_ITEM_PHOTO_CATS = {"pods", "liquids"}

# ================== GLOBAL STATE ==================

USER_LOCKS: Dict[int, asyncio.Lock] = {}
STOCK_CACHE: Dict[str, Dict[str, Any]] = {}
STOCK_LOCK = asyncio.Lock()

# ================== FILE HELPERS ==================


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default

        data = json.loads(path.read_text(encoding="utf-8"))
        return data if data is not None else default

    except Exception as e:
        logging.exception("Failed to read JSON %s: %s", path, e)
        return default


def write_json(path: Path, data: Any) -> None:
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logging.exception("Failed to write JSON %s: %s", path, e)


CATALOG: Dict[str, Any] = read_json(CATALOG_PATH, {})
CURRENCY = CATALOG.get("currency", "EUR")


def load_stock() -> None:
    global STOCK_CACHE
    STOCK_CACHE = read_json(STOCK_PATH, {})
    if not isinstance(STOCK_CACHE, dict):
        STOCK_CACHE = {}


async def save_stock() -> None:
    async with STOCK_LOCK:
        write_json(STOCK_PATH, STOCK_CACHE)


def save_order(order: Dict[str, Any]) -> None:
    orders = read_json(ORDERS_PATH, [])
    if not isinstance(orders, list):
        orders = []

    orders.append(order)
    write_json(ORDERS_PATH, orders[-500:])

# ================== BASIC HELPERS ==================


def get_user_lock(user_id: int) -> asyncio.Lock:
    if user_id not in USER_LOCKS:
        USER_LOCKS[user_id] = asyncio.Lock()
    return USER_LOCKS[user_id]


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_price(value: Any) -> str:
    try:
        return f"{float(value):g} {CURRENCY}"
    except Exception:
        return f"{value} {CURRENCY}"


def get_username(user) -> str:
    return f"@{user.username}" if user.username else f"id:{user.id}"


def item_key(*parts: Any) -> str:
    return ":".join(str(p) for p in parts)


def stock_get(key: str) -> Dict[str, Any]:
    return STOCK_CACHE.get(key, {"in_stock": True, "eta": None})


def stock_set(key: str, in_stock: bool, eta: Optional[str] = None) -> None:
    STOCK_CACHE[key] = {
        "in_stock": in_stock,
        "eta": eta,
    }


def cart_get(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
    return context.user_data.setdefault("cart", [])


def cart_total(cart: List[Dict[str, Any]]) -> float:
    return round(sum(float(item["price"]) for item in cart), 2)


def extract_flavor_name(flavor: Any) -> str:
    if isinstance(flavor, str):
        return flavor

    if isinstance(flavor, dict):
        return str(
            flavor.get("name")
            or flavor.get("title")
            or flavor.get("flavor")
            or flavor
        )

    return str(flavor)


def get_city_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("city_key", "Other")


def get_city_title(context: ContextTypes.DEFAULT_TYPE) -> str:
    city_key = get_city_key(context)
    custom_city = context.user_data.get("custom_city")

    if city_key == "Other" and custom_city:
        return custom_city

    return CITY_CONFIG.get(city_key, CITY_CONFIG["Other"])["title"]


def get_city_config(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    city_key = get_city_key(context)
    return CITY_CONFIG.get(city_key, CITY_CONFIG["Other"])

# ================== CLEAN CHAT ==================


async def delete_message_safely(bot, chat_id: int, message_id: Optional[int]) -> None:
    if not message_id:
        return

    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except BadRequest:
        pass
    except Exception as e:
        logging.warning("Could not delete message %s: %s", message_id, e)


async def clean_previous_bot_message(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
) -> None:
    chat_id = update.effective_chat.id
    last_id = context.user_data.get("last_bot_message_id")

    await delete_message_safely(context.bot, chat_id, last_id)
    context.user_data.pop("last_bot_message_id", None)


async def clean_callback_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    if not q or not q.message:
        return

    await delete_message_safely(
        context.bot,
        q.message.chat_id,
        q.message.message_id,
    )


async def send_clean_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    chat_id = update.effective_chat.id

    await clean_previous_bot_message(update, context)

    msg = await context.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
    )

    context.user_data["last_bot_message_id"] = msg.message_id


async def send_clean_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: str,
    caption: str = "",
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    chat_id = update.effective_chat.id

    await clean_previous_bot_message(update, context)

    try:
        p = photo.strip()

        if p.startswith("http://") or p.startswith("https://"):
            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=p,
                caption=caption,
                reply_markup=reply_markup,
            )
        else:
            abs_path = (BASE_DIR / p).resolve()
            if not abs_path.exists():
                return False

            msg = await context.bot.send_photo(
                chat_id=chat_id,
                photo=InputFile(abs_path),
                caption=caption,
                reply_markup=reply_markup,
            )

        context.user_data["last_bot_message_id"] = msg.message_id
        return True

    except Exception as e:
        logging.exception("Failed to send photo: %s", e)
        return False

# ================== KEYBOARDS ==================


def kb_city() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:Other")],
    ])


def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
    ])


def kb_after_add() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ])

# ================== ITEM RESOLVER ==================


def resolve_item(key: str) -> Optional[Dict[str, Any]]:
    try:
        parts = key.split(":")
        kind = parts[0]

        if kind == "cat":
            _, cat_key, idx = parts
            item = CATALOG["categories"][cat_key]["items"][int(idx)]
            return {
                "key": key,
                "name": item["name"],
                "price": float(item["price"]),
                "photo": item.get("photo"),
                "cat_key": cat_key,
            }

        if kind == "brand":
            _, cat_key, brand_key, idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            item = brand["items"][int(idx)]
            return {
                "key": key,
                "name": item["name"],
                "price": float(item["price"]),
                "photo": item.get("photo") or brand.get("photo"),
                "cat_key": cat_key,
            }

        if kind == "nic":
            _, cat_key, brand_key, block_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            block = brand["items"][int(block_idx)]
            flavor = block["items"][int(flavor_idx)]
            name = f"{brand.get('title', '')} {block.get('nicotine', '')} — {flavor}".strip()

            return {
                "key": key,
                "name": name,
                "price": float(block["price"]),
                "photo": brand.get("photo"),
                "cat_key": cat_key,
            }

        if kind == "flv":
            _, cat_key, brand_key, parent_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            parent = brand["items"][int(parent_idx)]
            flavor = parent["items"][int(flavor_idx)]
            flavor_name = extract_flavor_name(flavor)
            base_name = parent.get("name", brand.get("title", ""))
            name = f"{base_name} — {flavor_name}".strip()

            return {
                "key": key,
                "name": name,
                "price": float(parent["price"]),
                "photo": parent.get("photo") or brand.get("photo"),
                "cat_key": cat_key,
            }

    except Exception as e:
        logging.exception("resolve_item failed for key=%s: %s", key, e)

    return None


def stock_button_label(name: str, price: Any, key: str) -> Tuple[str, str]:
    st = stock_get(key)

    if st.get("in_stock", True):
        return f"{name} — {fmt_price(price)} ✅", f"add:{key}"

    eta = st.get("eta")
    eta_txt = f" (з {eta})" if eta else ""
    return f"{name} — {fmt_price(price)} ❌{eta_txt}", f"reserve:{key}"

# ================== START / CITY ==================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()

    await send_clean_text(
        update,
        context,
        "📍 Звідки ви?\nОберіть місто:",
        kb_city(),
    )


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    city_key = q.data.split(":", 1)[1]

    if city_key == "Other":
        context.user_data["awaiting_city"] = True
        context.user_data["city_key"] = "Other"

        await send_clean_text(
            update,
            context,
            "✍️ Напишіть назву вашого міста:",
        )
        return

    context.user_data["city_key"] = city_key
    context.user_data.pop("custom_city", None)

    await show_main_menu(update, context)

# ================== TEXT ROUTER ==================


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if context.user_data.get("awaiting_city"):
        context.user_data["custom_city"] = text
        context.user_data["city_key"] = "Other"
        context.user_data.pop("awaiting_city", None)

        await delete_message_safely(
            context.bot,
            update.effective_chat.id,
            update.message.message_id,
        )

        await show_main_menu(update, context)
        return

    if context.user_data.get("awaiting_eta_key"):
        if not is_admin(user_id):
            context.user_data.pop("awaiting_eta_key", None)
            return

        eta = text

        try:
            datetime.strptime(eta, "%Y-%m-%d")
        except ValueError:
            await send_clean_text(
                update,
                context,
                "❌ Невірний формат.\nТреба YYYY-MM-DD, наприклад 2026-01-20.",
            )
            return

        key = context.user_data.pop("awaiting_eta_key")
        item = resolve_item(key)

        stock_set(key, in_stock=False, eta=eta)
        await save_stock()

        title = item["name"] if item else key
        price = fmt_price(item["price"]) if item else "—"

        await delete_message_safely(
            context.bot,
            update.effective_chat.id,
            update.message.message_id,
        )

        await send_clean_text(
            update,
            context,
            f"✅ Позначено як ❌ Нема в наявності.\n\n"
            f"🧾 {title}\n"
            f"💶 {price}\n"
            f"🗓 Очікується з: {eta}",
        )
        return

    if context.user_data.get("reserve_key"):
        key = context.user_data.pop("reserve_key")
        item = resolve_item(key)
        st = stock_get(key)
        eta = st.get("eta")

        title = item["name"] if item else key
        price = fmt_price(item["price"]) if item else "—"
        city = get_city_title(context)

        reservation_text = (
            "📌 НОВЕ БРОНЮВАННЯ\n\n"
            f"👤 Клієнт: {get_username(update.effective_user)}\n"
            f"ID: {update.effective_user.id}\n"
            f"📍 Місто: {city}\n\n"
            f"🧾 Товар: {title}\n"
            f"💶 Ціна: {price}\n"
            f"🗓 Очікується з: {eta or 'не вказано'}\n\n"
            f"💬 Контакт/коментар: {text}"
        )

        for admin_id in ADMIN_IDS:
            await send_message_safely(context, admin_id, reservation_text)

        await delete_message_safely(
            context.bot,
            update.effective_chat.id,
            update.message.message_id,
        )

        await send_clean_text(
            update,
            context,
            "✅ Дякую! Бронювання передано адміну.\n"
            f"{'Очікується з ' + eta if eta else 'Дата надходження ще не вказана.'}",
            kb_main(),
        )
        return

# ================== MENU ==================


async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    city = get_city_title(context)

    await send_clean_text(
        update,
        context,
        f"Вітаю 👋\nВаше місто: {city}\n\nОберіть дію:",
        kb_main(),
    )


async def main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)
    await show_main_menu(update, context)

# ================== CATALOG ==================


async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    if not context.user_data.get("city_key"):
        await start(update, context)
        return

    keyboard = []

    for cat_key, cat in CATALOG.get("categories", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                cat.get("title", cat_key),
                callback_data=f"category:{cat_key}",
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅ На головну", callback_data="main")])

    await send_clean_text(
        update,
        context,
        "🛍 Каталог\nОберіть категорію:",
        InlineKeyboardMarkup(keyboard),
    )


async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG.get("categories", {}).get(cat_key)

    if not cat:
        await send_clean_text(update, context, "❌ Категорію не знайдено.", kb_main())
        return

    keyboard = []

    if "brands" in cat:
        for brand_key, brand in cat["brands"].items():
            keyboard.append([
                InlineKeyboardButton(
                    brand.get("title", brand_key),
                    callback_data=f"brand:{cat_key}:{brand_key}",
                )
            ])

    else:
        for idx, item in enumerate(cat.get("items", [])):
            key = item_key("cat", cat_key, idx)
            label, callback = stock_button_label(item["name"], item["price"], key)
            keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    text = f"🛍 {cat.get('title', 'Категорія')}\nОберіть:"
    markup = InlineKeyboardMarkup(keyboard)

    photo = cat.get("photo")
    if photo:
        sent = await send_clean_photo(update, context, photo, text, markup)
        if sent:
            return

    await send_clean_text(update, context, text, markup)


async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    _, cat_key, brand_key = q.data.split(":", 2)

    try:
        cat = CATALOG["categories"][cat_key]
        brand = cat["brands"][brand_key]
    except Exception:
        await send_clean_text(update, context, "❌ Бренд не знайдено.", kb_main())
        return

    keyboard = []
    items = brand.get("items", [])

    if items:
        first = items[0]

        if isinstance(first, dict) and "nicotine" in first and "items" in first:
            for idx, block in enumerate(items):
                keyboard.append([
                    InlineKeyboardButton(
                        f"{block.get('nicotine')} — {fmt_price(block.get('price'))}",
                        callback_data=f"nic:{cat_key}:{brand_key}:{idx}",
                    )
                ])

        elif isinstance(first, dict) and "name" in first:
            for idx, item in enumerate(items):
                has_flavors = isinstance(item.get("items"), list) and bool(item.get("items"))

                if has_flavors:
                    keyboard.append([
                        InlineKeyboardButton(
                            f"{item['name']} — {fmt_price(item['price'])}",
                            callback_data=f"flavors:{cat_key}:{brand_key}:{idx}",
                        )
                    ])
                else:
                    key = item_key("brand", cat_key, brand_key, idx)
                    label, callback = stock_button_label(item["name"], item["price"], key)
                    keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

        else:
            for idx, name in enumerate(items):
                key = item_key("brand", cat_key, brand_key, idx)
                label, callback = stock_button_label(str(name), brand.get("price", 0), key)
                keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{cat_key}")])

    caption = brand.get("title", brand_key)
    if brand.get("price_range"):
        caption += f"\n💶 {brand['price_range']}"

    markup = InlineKeyboardMarkup(keyboard)
    photo = brand.get("photo")

    if photo:
        sent = await send_clean_photo(update, context, photo, caption, markup)
        if sent:
            return

    await send_clean_text(update, context, caption, markup)


async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        block = brand["items"][int(block_idx)]
    except Exception:
        await send_clean_text(update, context, "❌ Позицію не знайдено.", kb_main())
        return

    keyboard = []

    for idx, flavor in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, block_idx, idx)
        item = resolve_item(key)
        name = item["name"] if item else str(flavor)
        label, callback = stock_button_label(name, block["price"], key)

        # коротше для кнопки
        if stock_get(key).get("in_stock", True):
            label = f"{flavor} ✅"
        else:
            eta = stock_get(key).get("eta")
            label = f"{flavor} ❌" + (f" (з {eta})" if eta else "")

        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])

    await send_clean_text(
        update,
        context,
        "Оберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def flavors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    _, cat_key, brand_key, parent_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        parent = brand["items"][int(parent_idx)]
        flavors = parent.get("items", [])
    except Exception:
        await send_clean_text(update, context, "❌ Смаки не знайдено.", kb_main())
        return

    keyboard = []

    for idx, flavor in enumerate(flavors):
        flavor_name = extract_flavor_name(flavor)
        key = item_key("flv", cat_key, brand_key, parent_idx, idx)
        st = stock_get(key)

        if st.get("in_stock", True):
            label = f"{flavor_name} ✅"
            callback = f"add:{key}"
        else:
            eta = st.get("eta")
            label = f"{flavor_name} ❌" + (f" (з {eta})" if eta else "")
            callback = f"reserve:{key}"

        keyboard.append([InlineKeyboardButton(label, callback_data=callback)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])

    await send_clean_text(
        update,
        context,
        f"{parent.get('name', 'Товар')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )

# ================== ADD / RESERVE ==================


async def add_to_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    lock = get_user_lock(user_id)

    async with lock:
        await clean_callback_message(update, context)

        key = q.data.split(":", 1)[1]
        item = resolve_item(key)

        if not item:
            await send_clean_text(update, context, "❌ Товар не знайдено.", kb_main())
            return

        st = stock_get(key)
        if not st.get("in_stock", True):
            await send_clean_text(
                update,
                context,
                "❌ Цей товар уже не в наявності.",
                kb_main(),
            )
            return

        cart = cart_get(context)
        cart.append({
            "key": item["key"],
            "name": item["name"],
            "price": item["price"],
        })

        text = (
            "✅ Додано в кошик\n\n"
            f"🧾 {item['name']}\n"
            f"💶 {fmt_price(item['price'])}"
        )

        force_no_photo = item["cat_key"] in NO_ITEM_PHOTO_CATS

        if item.get("photo") and not force_no_photo:
            sent = await send_clean_photo(
                update,
                context,
                item["photo"],
                text,
                kb_after_add(),
            )
            if sent:
                return

        await send_clean_text(update, context, text, kb_after_add())


async def reserve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    key = q.data.split(":", 1)[1]
    item = resolve_item(key)

    if not item:
        await send_clean_text(update, context, "❌ Товар не знайдено.", kb_main())
        return

    st = stock_get(key)
    eta = st.get("eta")

    context.user_data["reserve_key"] = key

    await send_clean_text(
        update,
        context,
        "📌 Бронювання\n\n"
        f"🧾 {item['name']}\n"
        f"💶 {fmt_price(item['price'])}\n"
        f"🗓 Очікується: {eta or 'дату уточнюйте'}\n\n"
        "✍️ Напишіть контакт або коментар:",
    )

# ================== CART ==================


async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    cart = cart_get(context)

    if not cart:
        await send_clean_text(
            update,
            context,
            "🛒 Кошик порожній.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
                [InlineKeyboardButton("⬅ На головну", callback_data="main")],
            ]),
        )
        return

    lines = [
        f"{idx + 1}. {item['name']} — {fmt_price(item['price'])}"
        for idx, item in enumerate(cart)
    ]

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("➖ Прибрати останній товар", callback_data="remove_last")],
        [InlineKeyboardButton("🗑 Очистити кошик", callback_data="clear_cart")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ]

    await send_clean_text(
        update,
        context,
        "🛒 Ваше замовлення:\n\n"
        + "\n".join(lines)
        + f"\n\n💰 Разом: {fmt_price(cart_total(cart))}",
        InlineKeyboardMarkup(keyboard),
    )


async def remove_last_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    cart = cart_get(context)

    if cart:
        cart.pop()

    await cart_handler(update, context)


async def clear_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    context.user_data["cart"] = []

    await clean_callback_message(update, context)
    await send_clean_text(update, context, "🗑 Кошик очищено.", kb_main())

# ================== CHECKOUT ==================


async def send_message_safely(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
) -> None:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except Exception as e:
        logging.exception("Failed to send message to %s: %s", chat_id, e)


async def checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    await clean_callback_message(update, context)

    cart = cart_get(context)

    if not cart:
        await send_clean_text(update, context, "🛒 Кошик порожній.", kb_main())
        return

    unavailable = []

    for item in cart:
        st = stock_get(item["key"])
        if not st.get("in_stock", True):
            unavailable.append(item["name"])

    if unavailable:
        await send_clean_text(
            update,
            context,
            "❌ Деякі товари вже не в наявності:\n\n"
            + "\n".join(f"• {name}" for name in unavailable)
            + "\n\nБудь ласка, оновіть кошик.",
            kb_main(),
        )
        return

    user = q.from_user
    city = get_city_title(context)
    city_cfg = get_city_config(context)
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    order_id = f"{user.id}-{int(datetime.now().timestamp())}"

    items_text = "\n".join(
        f"• {item['name']} — {fmt_price(item['price'])}"
        for item in cart
    )

    order_text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"🆔 Order ID: {order_id}\n"
        f"👤 Клієнт: {get_username(user)}\n"
        f"ID: {user.id}\n"
        f"📍 Місто: {city}\n\n"
        f"🛒 Товари:\n{items_text}\n\n"
        f"💰 Разом: {fmt_price(cart_total(cart))}\n"
        f"🕒 {timestamp}"
    )

    # тільки адміни
    for admin_id in ADMIN_IDS:
        await send_message_safely(context, admin_id, order_text)

    # тільки курʼєр конкретного міста
    await send_message_safely(
        context,
        city_cfg["courier_chat_id"],
        order_text,
    )

    save_order({
        "order_id": order_id,
        "user_id": user.id,
        "username": user.username,
        "city": city,
        "items": cart,
        "total": cart_total(cart),
        "created_at": timestamp,
        "courier_chat_id": city_cfg["courier_chat_id"],
        "courier_username": city_cfg["courier_username"],
    })

    context.user_data["cart"] = []

    await send_clean_text(
        update,
        context,
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{city_cfg['courier_username']}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
            [InlineKeyboardButton("⬅ На головну", callback_data="main")],
        ]),
    )

# ================== ADMIN ==================


async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_admin(update.effective_user.id):
        return

    keyboard = []

    for cat_key, cat in CATALOG.get("categories", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                f"⚙️ {cat.get('title', cat_key)}",
                callback_data=f"admin_cat:{cat_key}",
            )
        ])

    await send_clean_text(
        update,
        context,
        "🛠 Адмін-панель\nКерування наявністю:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_cat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG.get("categories", {}).get(cat_key)

    if not cat:
        await send_clean_text(update, context, "❌ Категорію не знайдено.")
        return

    keyboard = []

    if "brands" in cat:
        for brand_key, brand in cat["brands"].items():
            keyboard.append([
                InlineKeyboardButton(
                    brand.get("title", brand_key),
                    callback_data=f"admin_brand:{cat_key}:{brand_key}",
                )
            ])

        keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_home")])

        await send_clean_text(
            update,
            context,
            f"⚙️ {cat.get('title', cat_key)}\nОберіть бренд:",
            InlineKeyboardMarkup(keyboard),
        )
        return

    for idx, item in enumerate(cat.get("items", [])):
        key = item_key("cat", cat_key, idx)
        keyboard.append([admin_item_button(key)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_home")])

    await send_clean_text(
        update,
        context,
        f"⚙️ {cat.get('title', cat_key)}\nОберіть товар:",
        InlineKeyboardMarkup(keyboard),
    )


def admin_item_button(key: str) -> InlineKeyboardButton:
    item = resolve_item(key)
    st = stock_get(key)

    name = item["name"] if item else key
    mark = "✅" if st.get("in_stock", True) else "❌"
    eta = st.get("eta")

    label = f"{mark} {name}"
    if not st.get("in_stock", True) and eta:
        label += f" з {eta}"

    if len(label) > 60:
        label = label[:57] + "..."

    return InlineKeyboardButton(label, callback_data=f"admin_toggle:{key}")


async def admin_brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    _, cat_key, brand_key = q.data.split(":", 2)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    except Exception:
        await send_clean_text(update, context, "❌ Бренд не знайдено.")
        return

    keyboard = []
    items = brand.get("items", [])

    if items:
        first = items[0]

        if isinstance(first, dict) and "nicotine" in first:
            for idx, block in enumerate(items):
                keyboard.append([
                    InlineKeyboardButton(
                        f"{block.get('nicotine')} — {fmt_price(block.get('price'))}",
                        callback_data=f"admin_nic:{cat_key}:{brand_key}:{idx}",
                    )
                ])

        elif isinstance(first, dict) and "name" in first:
            for idx, item in enumerate(items):
                if isinstance(item.get("items"), list) and item.get("items"):
                    keyboard.append([
                        InlineKeyboardButton(
                            f"🍓 {item['name']}",
                            callback_data=f"admin_flavors:{cat_key}:{brand_key}:{idx}",
                        )
                    ])
                else:
                    key = item_key("brand", cat_key, brand_key, idx)
                    keyboard.append([admin_item_button(key)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])

    await send_clean_text(
        update,
        context,
        f"⚙️ {brand.get('title', brand_key)}\nОберіть позицію:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_nic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        block = brand["items"][int(block_idx)]
    except Exception:
        await send_clean_text(update, context, "❌ Блок не знайдено.")
        return

    keyboard = []

    for idx, _ in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, block_idx, idx)
        keyboard.append([admin_item_button(key)])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")
    ])

    await send_clean_text(
        update,
        context,
        f"⚙️ {brand.get('title', brand_key)} {block.get('nicotine', '')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_flavors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    _, cat_key, brand_key, parent_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        parent = brand["items"][int(parent_idx)]
        flavors = parent.get("items", [])
    except Exception:
        await send_clean_text(update, context, "❌ Смаки не знайдено.")
        return

    keyboard = []

    for idx, _ in enumerate(flavors):
        key = item_key("flv", cat_key, brand_key, parent_idx, idx)
        keyboard.append([admin_item_button(key)])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")
    ])

    await send_clean_text(
        update,
        context,
        f"⚙️ {parent.get('name', 'Товар')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    key = q.data.split(":", 1)[1]
    item = resolve_item(key)

    if not item:
        await send_clean_text(update, context, "❌ Товар не знайдено.")
        return

    st = stock_get(key)

    if st.get("in_stock", True):
        context.user_data["awaiting_eta_key"] = key

        await send_clean_text(
            update,
            context,
            "❌ Ставимо товар як «нема в наявності»\n\n"
            f"🧾 {item['name']}\n"
            f"💶 {fmt_price(item['price'])}\n\n"
            "Вкажіть дату надходження у форматі YYYY-MM-DD:",
        )
        return

    stock_set(key, in_stock=True, eta=None)
    await save_stock()

    await send_clean_text(
        update,
        context,
        "✅ Тепер товар в наявності:\n\n"
        f"🧾 {item['name']}\n"
        f"💶 {fmt_price(item['price'])}",
    )


async def admin_home_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    await clean_callback_message(update, context)

    keyboard = []

    for cat_key, cat in CATALOG.get("categories", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                f"⚙️ {cat.get('title', cat_key)}",
                callback_data=f"admin_cat:{cat_key}",
            )
        ])

    await send_clean_text(
        update,
        context,
        "🛠 Адмін-панель\nКерування наявністю:",
        InlineKeyboardMarkup(keyboard),
    )

# ================== ERROR ==================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception in handler", exc_info=context.error)

# ================== MAIN ==================


def main() -> None:
    load_stock()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.add_handler(CallbackQueryHandler(city_handler, pattern=r"^city:"))
    app.add_handler(CallbackQueryHandler(main_menu_handler, pattern=r"^main$"))

    app.add_handler(CallbackQueryHandler(catalog_menu, pattern=r"^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern=r"^category:"))
    app.add_handler(CallbackQueryHandler(brand_handler, pattern=r"^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern=r"^nic:"))
    app.add_handler(CallbackQueryHandler(flavors_handler, pattern=r"^flavors:"))

    app.add_handler(CallbackQueryHandler(add_to_cart_handler, pattern=r"^add:"))
    app.add_handler(CallbackQueryHandler(reserve_handler, pattern=r"^reserve:"))

    app.add_handler(CallbackQueryHandler(cart_handler, pattern=r"^cart$"))
    app.add_handler(CallbackQueryHandler(remove_last_handler, pattern=r"^remove_last$"))
    app.add_handler(CallbackQueryHandler(clear_cart_handler, pattern=r"^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout_handler, pattern=r"^checkout$"))

    app.add_handler(CallbackQueryHandler(admin_home_handler, pattern=r"^admin_home$"))
    app.add_handler(CallbackQueryHandler(admin_cat_handler, pattern=r"^admin_cat:"))
    app.add_handler(CallbackQueryHandler(admin_brand_handler, pattern=r"^admin_brand:"))
    app.add_handler(CallbackQueryHandler(admin_nic_handler, pattern=r"^admin_nic:"))
    app.add_handler(CallbackQueryHandler(admin_flavors_handler, pattern=r"^admin_flavors:"))
    app.add_handler(CallbackQueryHandler(admin_toggle_handler, pattern=r"^admin_toggle:"))

    app.add_error_handler(error_handler)

    logging.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
