import os
import json
import logging
from pathlib import Path
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
    InputMediaPhoto,
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

# =========================================================
# PATHS
# =========================================================

BASE_DIR = Path(__file__).resolve().parent

CATALOG_PATH = BASE_DIR / "catalog.json"
STOCK_PATH = BASE_DIR / "stock.json"
ORDERS_PATH = BASE_DIR / "orders.json"

# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")


def parse_admin_ids() -> List[int]:
    raw = os.getenv("ADMIN_IDS", "7406405860,721379009")

    result = []

    for part in raw.replace(";", ",").split(","):
        part = part.strip()

        if part.isdigit():
            result.append(int(part))

    return result


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
        "title": "Інше місто",
        "courier_chat_id": 8449852526,
        "courier_username": "@courier_fox",
    },
}

# =========================================================
# JSON HELPERS
# =========================================================


def read_json(path: Path, default: Any):
    try:
        if not path.exists():
            return default

        return json.loads(path.read_text(encoding="utf-8"))

    except Exception as e:
        logging.exception("Failed reading %s: %s", path, e)
        return default


def write_json(path: Path, data: Any):
    try:
        path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    except Exception as e:
        logging.exception("Failed writing %s: %s", path, e)


CATALOG = read_json(CATALOG_PATH, {})
STOCK = read_json(STOCK_PATH, {})

if not isinstance(STOCK, dict):
    STOCK = {}

CURRENCY = CATALOG.get("currency", "EUR")

# =========================================================
# HELPERS
# =========================================================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def item_key(*parts) -> str:
    return ":".join(str(p) for p in parts)


def fmt_price(value: Any) -> str:
    try:
        return f"{float(value):g} {CURRENCY}"
    except Exception:
        return f"{value} {CURRENCY}"


def get_username(user) -> str:
    return f"@{user.username}" if user.username else f"id:{user.id}"


def cart_get(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.setdefault("cart", [])


def cart_total(cart) -> float:
    return round(sum(float(x["price"]) for x in cart), 2)


def stock_get(key: str):
    return STOCK.get(key, {
        "in_stock": True,
        "eta": None,
    })


def stock_set(key: str, in_stock: bool, eta: Optional[str] = None):
    STOCK[key] = {
        "in_stock": in_stock,
        "eta": eta,
    }

    write_json(STOCK_PATH, STOCK)


def get_city_key(context):
    return context.user_data.get("city_key", "Other")


def get_city_title(context):
    city_key = get_city_key(context)

    if city_key == "Other":
        return context.user_data.get("custom_city", "Інше місто")

    return CITY_CONFIG[city_key]["title"]


def get_city_config(context):
    return CITY_CONFIG.get(
        get_city_key(context),
        CITY_CONFIG["Other"],
    )


def save_order(order: Dict[str, Any]):
    orders = read_json(ORDERS_PATH, [])

    if not isinstance(orders, list):
        orders = []

    orders.append(order)

    write_json(ORDERS_PATH, orders[-500:])


def extract_flavor_name(flavor):
    if isinstance(flavor, str):
        return flavor

    if isinstance(flavor, dict):
        return str(
            flavor.get("name")
            or flavor.get("title")
            or flavor
        )

    return str(flavor)


def categories_get() -> Dict[str, Any]:
    categories = CATALOG.get("categories", {})
    return categories if isinstance(categories, dict) else {}


def category_get(cat_key: str) -> Optional[Dict[str, Any]]:
    return categories_get().get(cat_key)


def brand_get(cat_key: str, brand_key: str) -> Optional[Dict[str, Any]]:
    category = category_get(cat_key)

    if not category:
        return None

    brands = category.get("brands", {})

    if not isinstance(brands, dict):
        return None

    return brands.get(brand_key)


def items_get(container: Dict[str, Any]) -> List[Any]:
    items = container.get("items", [])
    return items if isinstance(items, list) else []


def parse_idx(value: str) -> Optional[int]:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def has_city(context: ContextTypes.DEFAULT_TYPE) -> bool:
    return bool(context.user_data.get("city_key"))

# =========================================================
# UI HELPERS
# =========================================================


async def answer_callback(update: Update):
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


async def show_stale_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str = "⚠️ Це меню застаріло або вже недійсне. Я відкрив актуальне меню.",
):
    if has_city(context):
        await show_text(update, text, kb_main())
        return

    await start(update, context)


def resolve_photo_source(photo: str):
    if not photo:
        return None

    if photo.startswith("http://") or photo.startswith("https://"):
        return photo

    path = (BASE_DIR / photo).resolve()
    if path.exists():
        return InputFile(path)

    return None


async def show_text(
    update: Update,
    text: str,
    reply_markup=None,
):
    if update.callback_query:
        q = update.callback_query

        try:
            if q.message and q.message.photo:
                await q.edit_message_caption(
                    caption=text,
                    reply_markup=reply_markup,
                )
            else:
                await q.edit_message_text(
                    text=text,
                    reply_markup=reply_markup,
                )
            return

        except BadRequest as e:
            if "message is not modified" in str(e).lower():
                return
        except Exception:
            logging.exception("show_text edit failed")

        await q.message.reply_text(
            text=text,
            reply_markup=reply_markup,
        )
        return

    if update.message:
        await update.message.reply_text(
            text=text,
            reply_markup=reply_markup,
        )


async def show_photo(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    photo: str,
    caption: str,
    reply_markup=None,
):
    source = resolve_photo_source(photo)

    if source is None:
        await show_text(update, caption, reply_markup)
        return

    try:
        if update.callback_query and update.callback_query.message:
            await update.callback_query.edit_message_media(
                media=InputMediaPhoto(media=source, caption=caption),
                reply_markup=reply_markup,
            )
            return

    except BadRequest as e:
        if "message is not modified" in str(e).lower():
            return

    except Exception as e:
        logging.exception("Photo send failed: %s", e)

    try:
        await context.bot.send_photo(
            chat_id=update.effective_chat.id,
            photo=source,
            caption=caption,
            reply_markup=reply_markup,
        )
        return
    except Exception as e:
        logging.exception("Photo send fallback failed: %s", e)

    await show_text(update, caption, reply_markup)


async def send_message_safe(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
) -> bool:
    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=text,
        )
        return True

    except Exception as e:
        logging.exception("Failed sending message: %s", e)
        return False


async def notify_targets(
    context: ContextTypes.DEFAULT_TYPE,
    recipients: List[int],
    text: str,
) -> Tuple[List[int], List[int]]:
    delivered = []
    failed = []

    for chat_id in recipients:
        if await send_message_safe(context, chat_id, text):
            delivered.append(chat_id)
        else:
            failed.append(chat_id)

    return delivered, failed

# =========================================================
# KEYBOARDS
# =========================================================


def kb_main():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
    ])


def kb_after_add():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ])

# =========================================================
# ITEM RESOLVER
# =========================================================


def resolve_item(key: str):
    try:
        parts = key.split(":")
        kind = parts[0]

        if kind == "brand":
            if len(parts) != 4:
                return None

            _, cat_key, brand_key, idx = parts

            brand = brand_get(cat_key, brand_key)
            item_idx = parse_idx(idx)

            if not brand or item_idx is None:
                return None

            items = items_get(brand)

            if item_idx < 0 or item_idx >= len(items):
                return None

            item = items[item_idx]

            if not isinstance(item, dict):
                return None

            return {
                "key": key,
                "name": item["name"],
                "price": float(item["price"]),
                "photo": item.get("photo") or brand.get("photo"),
            }

        if kind == "nic":
            if len(parts) != 5:
                return None

            _, cat_key, brand_key, block_idx, flavor_idx = parts

            brand = brand_get(cat_key, brand_key)
            block_index = parse_idx(block_idx)
            flavor_index = parse_idx(flavor_idx)

            if not brand or block_index is None or flavor_index is None:
                return None

            brand_items = items_get(brand)

            if block_index < 0 or block_index >= len(brand_items):
                return None

            block = brand_items[block_index]

            if not isinstance(block, dict):
                return None

            flavors = items_get(block)

            if flavor_index < 0 or flavor_index >= len(flavors):
                return None

            flavor_name = extract_flavor_name(flavors[flavor_index])

            return {
                "key": key,
                "name": f"{brand.get('title')} {block.get('nicotine')} — {flavor_name}",
                "price": float(block["price"]),
                "photo": brand.get("photo"),
            }

        if kind == "flv":
            if len(parts) != 5:
                return None

            _, cat_key, brand_key, parent_idx, flavor_idx = parts

            brand = brand_get(cat_key, brand_key)
            parent_index = parse_idx(parent_idx)
            flavor_index = parse_idx(flavor_idx)

            if not brand or parent_index is None or flavor_index is None:
                return None

            brand_items = items_get(brand)

            if parent_index < 0 or parent_index >= len(brand_items):
                return None

            parent = brand_items[parent_index]

            if not isinstance(parent, dict):
                return None

            flavors = items_get(parent)

            if flavor_index < 0 or flavor_index >= len(flavors):
                return None

            flavor = flavors[flavor_index]

            flavor_name = extract_flavor_name(flavor)

            return {
                "key": key,
                "name": f"{parent.get('name')} — {flavor_name}",
                "price": float(parent["price"]),
                "photo": parent.get("photo") or brand.get("photo"),
            }

    except Exception as e:
        logging.exception("resolve_item failed: %s", e)

    return None

# =========================================================
# START
# =========================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:Other")],
    ])

    await show_text(
        update,
        "👋 Вітаємо у ELF FOX\n\n"
        "📍 Оберіть ваше місто:",
        keyboard,
    )


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    city_key = q.data.split(":")[1]

    context.user_data["city_key"] = city_key

    if city_key == "Other":
        context.user_data["awaiting_city"] = True

        await show_text(
            update,
            "✍️ Напишіть назву вашого міста:",
        )
        return

    await show_main(update, context)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE):
    city = get_city_title(context)

    await show_text(
        update,
        f"🦊 ELF FOX\n\n"
        f"📍 Ваше місто: {city}\n\n"
        f"Оберіть дію:",
        kb_main(),
    )


async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)
    await show_main(update, context)

# =========================================================
# TEXT ROUTER
# =========================================================


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()

    if context.user_data.get("awaiting_city"):
        context.user_data["custom_city"] = text
        context.user_data["awaiting_city"] = False

        await show_main(update, context)
        return

    if context.user_data.get("reserve_key"):
        key = context.user_data["reserve_key"]

        item = resolve_item(key)

        if not item:
            await update.message.reply_text("❌ Товар не знайдено")
            return

        st = stock_get(key)

        reservation_text = (
            "📌 НОВЕ БРОНЮВАННЯ\n\n"
            f"👤 {get_username(update.effective_user)}\n"
            f"📍 Місто: {get_city_title(context)}\n\n"
            f"🧾 {item['name']}\n"
            f"💶 {fmt_price(item['price'])}\n"
            f"🗓 Очікується: {st.get('eta') or 'не вказано'}\n\n"
            f"💬 Контакт:\n{text}"
        )

        delivered, _ = await notify_targets(context, ADMIN_IDS, reservation_text)

        if not delivered:
            await update.message.reply_text(
                "❌ Не вдалося передати бронювання. Спробуйте ще раз трохи пізніше.",
                reply_markup=kb_main(),
            )
            return

        context.user_data.pop("reserve_key", None)

        await update.message.reply_text(
            "✅ Бронювання передано адміну",
            reply_markup=kb_main(),
        )
        return

    if not has_city(context):
        await start(update, context)
        return

    await update.message.reply_text(
        "Я не очікував текст у цей момент. Скористайся кнопками нижче.",
        reply_markup=kb_main(),
    )

# =========================================================
# CATALOG
# =========================================================


async def catalog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    categories = categories_get()

    if not categories:
        await show_text(update, "❌ Каталог тимчасово недоступний", kb_main())
        return

    keyboard = []

    for cat_key, cat in categories.items():
        keyboard.append([
            InlineKeyboardButton(
                cat["title"],
                callback_data=f"cat:{cat_key}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ На головну",
            callback_data="main",
        )
    ])

    await show_text(
        update,
        "🛍 Каталог\n\nОберіть категорію:",
        InlineKeyboardMarkup(keyboard),
    )


async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    parts = q.data.split(":", 1)

    if len(parts) != 2:
        await show_stale_callback(update, context)
        return

    cat_key = parts[1]

    cat = category_get(cat_key)

    if not cat:
        await show_stale_callback(update, context, "❌ Категорію не знайдено. Відкрив актуальне меню.")
        return

    keyboard = []

    for brand_key, brand in cat["brands"].items():
        keyboard.append([
            InlineKeyboardButton(
                brand["title"],
                callback_data=f"brand:{cat_key}:{brand_key}",
            )
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ Назад",
            callback_data="catalog",
        )
    ])

    await show_photo(
        update,
        context,
        cat.get("photo", ""),
        f"{cat['title']}\n\nОберіть бренд:",
        InlineKeyboardMarkup(keyboard),
    )


async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    parts = q.data.split(":")

    if len(parts) != 3:
        await show_stale_callback(update, context)
        return

    _, cat_key, brand_key = parts

    brand = brand_get(cat_key, brand_key)

    if not brand:
        await show_stale_callback(update, context, "❌ Бренд не знайдено. Відкрив актуальне меню.")
        return

    keyboard = []

    items = items_get(brand)

    for idx, item in enumerate(items):

        # nicotine block
        if isinstance(item, dict) and "nicotine" in item:
            keyboard.append([
                InlineKeyboardButton(
                    f"{item['nicotine']} — {fmt_price(item['price'])}",
                    callback_data=f"nic:{cat_key}:{brand_key}:{idx}",
                )
            ])

        # item with flavors
        elif isinstance(item, dict) and isinstance(item.get("items"), list):
            keyboard.append([
                InlineKeyboardButton(
                    f"{item['name']} — {fmt_price(item['price'])}",
                    callback_data=f"flavors:{cat_key}:{brand_key}:{idx}",
                )
            ])

        # normal item
        else:
            key = item_key("brand", cat_key, brand_key, idx)

            st = stock_get(key)

            if st.get("in_stock", True):
                label = f"{item['name']} — {fmt_price(item['price'])} ✅"
                callback = f"add:{key}"
            else:
                eta = st.get("eta")
                label = f"{item['name']} ❌"
                if eta:
                    label += f" ({eta})"

                callback = f"reserve:{key}"

            keyboard.append([
                InlineKeyboardButton(label, callback_data=callback)
            ])

    keyboard.append([
        InlineKeyboardButton(
            "🛒 Кошик",
            callback_data="cart",
        )
    ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ Назад",
            callback_data=f"cat:{cat_key}",
        )
    ])

    text = f"{brand['title']}"

    if brand.get("price_range"):
        text += f"\n💶 {brand['price_range']}"

    text += "\n\nОберіть товар:"

    await show_photo(
        update,
        context,
        brand.get("photo", ""),
        text,
        InlineKeyboardMarkup(keyboard),
    )


async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    parts = q.data.split(":")

    if len(parts) != 4:
        await show_stale_callback(update, context)
        return

    _, cat_key, brand_key, block_idx = parts

    brand = brand_get(cat_key, brand_key)
    block_index = parse_idx(block_idx)

    if not brand or block_index is None:
        await show_stale_callback(update, context)
        return

    brand_items = items_get(brand)

    if block_index < 0 or block_index >= len(brand_items):
        await show_stale_callback(update, context)
        return

    block = brand_items[block_index]

    if not isinstance(block, dict):
        await show_stale_callback(update, context)
        return

    keyboard = []

    for idx, flavor in enumerate(items_get(block)):
        flavor_name = extract_flavor_name(flavor)
        key = item_key(
            "nic",
            cat_key,
            brand_key,
            block_idx,
            idx,
        )

        st = stock_get(key)

        if st.get("in_stock", True):
            label = f"{flavor_name} ✅"
            callback = f"add:{key}"
        else:
            eta = st.get("eta")

            label = f"{flavor_name} ❌"

            if eta:
                label += f" ({eta})"

            callback = f"reserve:{key}"

        keyboard.append([
            InlineKeyboardButton(label, callback_data=callback)
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ Назад",
            callback_data=f"brand:{cat_key}:{brand_key}",
        )
    ])

    await show_photo(
        update,
        context,
        brand.get("photo", ""),
        f"{brand['title']} {block['nicotine']}\n\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def flavors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    parts = q.data.split(":")

    if len(parts) != 4:
        await show_stale_callback(update, context)
        return

    _, cat_key, brand_key, parent_idx = parts

    brand = brand_get(cat_key, brand_key)
    parent_index = parse_idx(parent_idx)

    if not brand or parent_index is None:
        await show_stale_callback(update, context)
        return

    brand_items = items_get(brand)

    if parent_index < 0 or parent_index >= len(brand_items):
        await show_stale_callback(update, context)
        return

    parent = brand_items[parent_index]

    if not isinstance(parent, dict):
        await show_stale_callback(update, context)
        return

    keyboard = []

    for idx, flavor in enumerate(items_get(parent)):
        flavor_name = extract_flavor_name(flavor)

        key = item_key(
            "flv",
            cat_key,
            brand_key,
            parent_idx,
            idx,
        )

        st = stock_get(key)

        if st.get("in_stock", True):
            label = f"{flavor_name} ✅"
            callback = f"add:{key}"
        else:
            eta = st.get("eta")

            label = f"{flavor_name} ❌"

            if eta:
                label += f" ({eta})"

            callback = f"reserve:{key}"

        keyboard.append([
            InlineKeyboardButton(label, callback_data=callback)
        ])

    keyboard.append([
        InlineKeyboardButton(
            "⬅ Назад",
            callback_data=f"brand:{cat_key}:{brand_key}",
        )
    ])

    await show_photo(
        update,
        context,
        brand.get("photo", ""),
        f"{parent['name']}\n\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )

# =========================================================
# ADD / RESERVE
# =========================================================


async def add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    key = q.data.split(":", 1)[1]

    item = resolve_item(key)

    if not item:
        await show_text(update, "❌ Товар не знайдено")
        return

    st = stock_get(key)

    if not st.get("in_stock", True):
        await show_text(
            update,
            "❌ Цього товару вже немає в наявності",
            kb_main(),
        )
        return

    cart = cart_get(context)

    cart.append({
        "key": item["key"],
        "name": item["name"],
        "price": item["price"],
    })

    await show_photo(
        update,
        context,
        item.get("photo", ""),
        "✅ Додано в кошик\n\n"
        f"🧾 {item['name']}\n"
        f"💶 {fmt_price(item['price'])}",
        kb_after_add(),
    )


async def reserve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    q = update.callback_query

    key = q.data.split(":", 1)[1]

    item = resolve_item(key)

    if not item:
        await show_text(update, "❌ Товар не знайдено")
        return

    st = stock_get(key)

    context.user_data["reserve_key"] = key

    await show_text(
        update,
        "📌 Бронювання\n\n"
        f"🧾 {item['name']}\n"
        f"💶 {fmt_price(item['price'])}\n"
        f"🗓 Очікується: {st.get('eta') or 'дату уточнюйте'}\n\n"
        "✍️ Напишіть контакт або коментар:",
    )

# =========================================================
# CART
# =========================================================


async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    cart = cart_get(context)

    if not cart:
        await show_text(
            update,
            "🛒 Кошик порожній",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
                [InlineKeyboardButton("⬅ На головну", callback_data="main")],
            ]),
        )
        return

    lines = []

    for idx, item in enumerate(cart):
        lines.append(
            f"{idx + 1}. {item['name']} — {fmt_price(item['price'])}"
        )

    text = (
        "🛒 Ваше замовлення:\n\n"
        + "\n".join(lines)
        + f"\n\n💰 Разом: {fmt_price(cart_total(cart))}"
    )

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("➖ Прибрати останній", callback_data="remove_last")],
        [InlineKeyboardButton("🗑 Очистити кошик", callback_data="clear_cart")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ])

    await show_text(update, text, keyboard)


async def remove_last_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    cart = cart_get(context)

    if cart:
        cart.pop()

    await cart_handler(update, context)


async def clear_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    context.user_data["cart"] = []

    await show_text(
        update,
        "🗑 Кошик очищено",
        kb_main(),
    )

# =========================================================
# CHECKOUT
# =========================================================


async def checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)

    if context.user_data.get("checkout_in_progress"):
        await show_text(
            update,
            "⏳ Замовлення вже обробляється. Зачекай кілька секунд.",
        )
        return

    context.user_data["checkout_in_progress"] = True

    user = update.effective_user

    try:
        cart = cart_get(context)

        if not cart:
            await show_text(update, "🛒 Кошик порожній")
            return

        unavailable = []

        for item in cart:
            st = stock_get(item["key"])

            if not st.get("in_stock", True):
                unavailable.append(item["name"])

        if unavailable:
            await show_text(
                update,
                "❌ Деякі товари вже не в наявності:\n\n"
                + "\n".join(f"• {x}" for x in unavailable),
                kb_main(),
            )
            return

        city = get_city_title(context)
        city_cfg = get_city_config(context)

        order_id = f"{user.id}-{int(datetime.now().timestamp())}"

        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

        items_text = "\n".join(
            f"• {item['name']} — {fmt_price(item['price'])}"
            for item in cart
        )

        order_text = (
            "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
            f"🆔 ID: {order_id}\n"
            f"👤 {get_username(user)}\n"
            f"📍 Місто: {city}\n\n"
            f"🛒 Товари:\n{items_text}\n\n"
            f"💰 Разом: {fmt_price(cart_total(cart))}\n"
            f"🕒 {timestamp}"
        )

        delivered_admins, failed_admins = await notify_targets(
            context,
            ADMIN_IDS,
            order_text,
        )
        courier_sent = await send_message_safe(
            context,
            city_cfg["courier_chat_id"],
            order_text,
        )

        if not delivered_admins and not courier_sent:
            await show_text(
                update,
                "❌ Не вдалося передати замовлення. Кошик збережено, спробуй ще раз трохи пізніше.",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
                    [InlineKeyboardButton("⬅ На головну", callback_data="main")],
                ]),
            )
            return

        save_order({
            "order_id": order_id,
            "city": city,
            "user_id": user.id,
            "items": cart,
            "total": cart_total(cart),
            "created_at": timestamp,
            "courier_sent": courier_sent,
            "admin_delivered": delivered_admins,
            "admin_failed": failed_admins,
        })

        context.user_data["cart"] = []

        if courier_sent:
            success_text = (
                "✅ Дякуємо за замовлення\n\n"
                f"Курʼєр звʼяжеться з вами:\n"
                f"{city_cfg['courier_username']}"
            )
        else:
            success_text = (
                "✅ Замовлення прийнято\n\n"
                "Курʼєру не вдалося відправити повідомлення автоматично, "
                "але адміністратор уже отримав замовлення і звʼяжеться з вами."
            )

        await show_text(
            update,
            success_text,
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
                [InlineKeyboardButton("⬅ На головну", callback_data="main")],
            ]),
        )
    finally:
        context.user_data.pop("checkout_in_progress", None)


async def unknown_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await answer_callback(update)
    await show_stale_callback(update, context)

# =========================================================
# MAIN
# =========================================================


def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_router,
        )
    )

    app.add_handler(CallbackQueryHandler(city_handler, pattern=r"^city:"))
    app.add_handler(CallbackQueryHandler(main_handler, pattern=r"^main$"))

    app.add_handler(CallbackQueryHandler(catalog_handler, pattern=r"^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern=r"^cat:"))
    app.add_handler(CallbackQueryHandler(brand_handler, pattern=r"^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern=r"^nic:"))
    app.add_handler(CallbackQueryHandler(flavors_handler, pattern=r"^flavors:"))

    app.add_handler(CallbackQueryHandler(add_handler, pattern=r"^add:"))
    app.add_handler(CallbackQueryHandler(reserve_handler, pattern=r"^reserve:"))

    app.add_handler(CallbackQueryHandler(cart_handler, pattern=r"^cart$"))
    app.add_handler(CallbackQueryHandler(remove_last_handler, pattern=r"^remove_last$"))
    app.add_handler(CallbackQueryHandler(clear_cart_handler, pattern=r"^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout_handler, pattern=r"^checkout$"))
    app.add_handler(CallbackQueryHandler(unknown_callback_handler))

    logging.info("Bot started")

    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
