import os
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, TimedOut, NetworkError
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
    raise RuntimeError("BOT_TOKEN not set")


def parse_admin_ids() -> List[int]:
    raw = os.getenv("ADMIN_IDS", "7406405860,721379009")
    ids = []

    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))

    if not ids:
        raise RuntimeError("ADMIN_IDS not set correctly")

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
        "title": "Інше місто",
        "courier_chat_id": 8449852526,
        "courier_username": "@courier_fox",
    },
}

# Для цих категорій не надсилаємо фото товару після додавання
NO_ITEM_PHOTO_CATS = {"liquids"}

# ================== JSON HELPERS ==================


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default

        return json.loads(path.read_text(encoding="utf-8"))

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
STOCK: Dict[str, Dict[str, Any]] = read_json(STOCK_PATH, {})

if not isinstance(STOCK, dict):
    STOCK = {}

# ================== BASIC HELPERS ==================


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def fmt_price(price: Any) -> str:
    try:
        return f"{float(price):g} {CURRENCY}"
    except Exception:
        return f"{price} {CURRENCY}"


def get_username(user) -> str:
    return f"@{user.username}" if user.username else f"id:{user.id}"


def item_key(*parts: Any) -> str:
    return ":".join(str(p) for p in parts)


def stock_get(key: str) -> Dict[str, Any]:
    return STOCK.get(key, {"in_stock": True, "eta": None})


def stock_set(key: str, in_stock: bool, eta: Optional[str] = None) -> None:
    STOCK[key] = {
        "in_stock": in_stock,
        "eta": eta,
    }
    write_json(STOCK_PATH, STOCK)


def extract_flavor_name(flavor: Any) -> str:
    if isinstance(flavor, str):
        return flavor

    if isinstance(flavor, dict):
        return str(flavor.get("name") or flavor.get("title") or flavor)

    return str(flavor)


def cart_get(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
    return context.user_data.setdefault("cart", [])


def cart_total(cart: List[Dict[str, Any]]) -> float:
    return round(sum(float(item["price"]) for item in cart), 2)


def get_city_key(context: ContextTypes.DEFAULT_TYPE) -> str:
    return context.user_data.get("city_key", "Other")


def get_city_title(context: ContextTypes.DEFAULT_TYPE) -> str:
    city_key = get_city_key(context)

    if city_key == "Other":
        return context.user_data.get("custom_city", "Інше місто")

    return CITY_CONFIG[city_key]["title"]


def get_city_config(context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
    city_key = get_city_key(context)
    return CITY_CONFIG.get(city_key, CITY_CONFIG["Other"])


def save_order(order: Dict[str, Any]) -> None:
    orders = read_json(ORDERS_PATH, [])

    if not isinstance(orders, list):
        orders = []

    orders.append(order)
    write_json(ORDERS_PATH, orders[-500:])

# ================== UI HELPERS ==================


async def show_text(
    update: Update,
    text: str,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> None:
    """
    Стабільна логіка:
    - якщо натиснули кнопку — пробуємо редагувати це саме повідомлення;
    - якщо не вийшло — надсилаємо нове;
    - нічого масово не видаляємо, щоб бот не вис.
    """

    if update.callback_query:
        q = update.callback_query

        try:
            await q.edit_message_text(text=text, reply_markup=reply_markup)
            return
        except BadRequest as e:
            if "Message is not modified" in str(e):
                return

            try:
                await q.message.reply_text(text=text, reply_markup=reply_markup)
                return
            except Exception as e2:
                logging.exception("Failed to reply after edit failed: %s", e2)
                return

    if update.message:
        await update.message.reply_text(text=text, reply_markup=reply_markup)


async def answer_callback(update: Update) -> None:
    if update.callback_query:
        try:
            await update.callback_query.answer()
        except Exception:
            pass


async def send_message_safe(
    context: ContextTypes.DEFAULT_TYPE,
    chat_id: int,
    text: str,
) -> None:
    try:
        await context.bot.send_message(chat_id=chat_id, text=text)
    except (TimedOut, NetworkError) as e:
        logging.warning("Telegram network problem while sending to %s: %s", chat_id, e)
    except Exception as e:
        logging.exception("Failed to send message to %s: %s", chat_id, e)

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


def kb_back_to_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ])

# ================== ITEM RESOLVER ==================


def resolve_item(key: str) -> Optional[Dict[str, Any]]:
    try:
        parts = key.split(":")
        kind = parts[0]

        if kind == "brand":
            _, cat_key, brand_key, idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            item = brand["items"][int(idx)]

            return {
                "key": key,
                "cat_key": cat_key,
                "brand_key": brand_key,
                "name": item["name"],
                "price": float(item["price"]),
            }

        if kind == "nic":
            _, cat_key, brand_key, block_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            block = brand["items"][int(block_idx)]
            flavor = block["items"][int(flavor_idx)]

            name = f"{brand.get('title', brand_key)} {block.get('nicotine')} — {flavor}"

            return {
                "key": key,
                "cat_key": cat_key,
                "brand_key": brand_key,
                "name": name,
                "price": float(block["price"]),
            }

        if kind == "flv":
            _, cat_key, brand_key, parent_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            parent = brand["items"][int(parent_idx)]
            flavor = parent["items"][int(flavor_idx)]

            flavor_name = extract_flavor_name(flavor)
            name = f"{parent.get('name', brand.get('title', brand_key))} — {flavor_name}"

            return {
                "key": key,
                "cat_key": cat_key,
                "brand_key": brand_key,
                "name": name,
                "price": float(parent["price"]),
            }

    except Exception as e:
        logging.exception("resolve_item failed for key=%s: %s", key, e)

    return None


def build_stock_button(label: str, price: Any, key: str) -> InlineKeyboardButton:
    st = stock_get(key)

    if st.get("in_stock", True):
        return InlineKeyboardButton(
            f"{label} — {fmt_price(price)} ✅",
            callback_data=f"add:{key}",
        )

    eta = st.get("eta")
    eta_text = f" з {eta}" if eta else ""

    return InlineKeyboardButton(
        f"{label} — {fmt_price(price)} ❌{eta_text}",
        callback_data=f"reserve:{key}",
    )


def build_flavor_button(label: str, key: str) -> InlineKeyboardButton:
    st = stock_get(key)

    if st.get("in_stock", True):
        return InlineKeyboardButton(
            f"{label} ✅",
            callback_data=f"add:{key}",
        )

    eta = st.get("eta")
    eta_text = f" з {eta}" if eta else ""

    return InlineKeyboardButton(
        f"{label} ❌{eta_text}",
        callback_data=f"reserve:{key}",
    )

# ================== START / CITY ==================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    context.user_data.clear()

    await show_text(
        update,
        "📍 Звідки ви?\nОберіть місто:",
        kb_city(),
    )


async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    city_key = q.data.split(":", 1)[1]

    context.user_data["city_key"] = city_key
    context.user_data.pop("custom_city", None)

    if city_key == "Other":
        context.user_data["awaiting_city"] = True
        await show_text(update, "✍️ Напишіть назву вашого міста:")
        return

    await show_main(update, context)


async def show_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    city = get_city_title(context)

    await show_text(
        update,
        f"Вітаю 👋\nВаше місто: {city}\n\nОберіть дію:",
        kb_main(),
    )


async def main_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)
    await show_main(update, context)

# ================== TEXT ROUTER ==================


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    if context.user_data.get("awaiting_city"):
        context.user_data["custom_city"] = text
        context.user_data["city_key"] = "Other"
        context.user_data.pop("awaiting_city", None)

        await show_main(update, context)
        return

    if context.user_data.get("awaiting_eta_key"):
        if not is_admin(user_id):
            context.user_data.pop("awaiting_eta_key", None)
            return

        try:
            datetime.strptime(text, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text(
                "❌ Невірний формат.\nНапиши дату так: 2026-01-20"
            )
            return

        key = context.user_data.pop("awaiting_eta_key")
        stock_set(key, in_stock=False, eta=text)

        item = resolve_item(key)
        name = item["name"] if item else key

        await update.message.reply_text(
            f"✅ Товар позначено як ❌ немає в наявності.\n\n"
            f"🧾 {name}\n"
            f"🗓 Очікується з: {text}"
        )
        return

    if context.user_data.get("reserve_key"):
        key = context.user_data.pop("reserve_key")
        item = resolve_item(key)

        if not item:
            await update.message.reply_text("❌ Товар не знайдено.")
            return

        city = get_city_title(context)
        st = stock_get(key)

        reservation_text = (
            "📌 НОВЕ БРОНЮВАННЯ\n\n"
            f"👤 Клієнт: {get_username(update.effective_user)}\n"
            f"ID: {update.effective_user.id}\n"
            f"📍 Місто: {city}\n\n"
            f"🧾 Товар: {item['name']}\n"
            f"💶 Ціна: {fmt_price(item['price'])}\n"
            f"🗓 Очікується: {st.get('eta') or 'не вказано'}\n\n"
            f"💬 Контакт/коментар: {text}"
        )

        for admin_id in ADMIN_IDS:
            await send_message_safe(context, admin_id, reservation_text)

        await update.message.reply_text(
            "✅ Бронювання передано адміну.",
            reply_markup=kb_main(),
        )
        return

# ================== CATALOG ==================


async def catalog_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    if not context.user_data.get("city_key"):
        await start(update, context)
        return

    keyboard = []

    for cat_key, cat in CATALOG.get("categories", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                cat.get("title", cat_key),
                callback_data=f"cat:{cat_key}",
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅ На головну", callback_data="main")])

    await show_text(
        update,
        "🛍 Каталог\nОберіть категорію:",
        InlineKeyboardMarkup(keyboard),
    )


async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    cat_key = q.data.split(":", 1)[1]

    cat = CATALOG.get("categories", {}).get(cat_key)
    if not cat:
        await show_text(update, "❌ Категорію не знайдено.", kb_main())
        return

    keyboard = []

    for brand_key, brand in cat.get("brands", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                brand.get("title", brand_key),
                callback_data=f"brand:{cat_key}:{brand_key}",
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    await show_text(
        update,
        f"{cat.get('title', 'Категорія')}\nОберіть бренд:",
        InlineKeyboardMarkup(keyboard),
    )


async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    _, cat_key, brand_key = q.data.split(":", 2)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    except Exception:
        await show_text(update, "❌ Бренд не знайдено.", kb_main())
        return

    items = brand.get("items", [])
    keyboard = []

    for idx, item in enumerate(items):
        if isinstance(item, dict) and "nicotine" in item and "items" in item:
            keyboard.append([
                InlineKeyboardButton(
                    f"{item.get('nicotine')} — {fmt_price(item.get('price'))}",
                    callback_data=f"nic:{cat_key}:{brand_key}:{idx}",
                )
            ])

        elif isinstance(item, dict) and "name" in item:
            if isinstance(item.get("items"), list) and item["items"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"{item['name']} — {fmt_price(item['price'])}",
                        callback_data=f"flavors:{cat_key}:{brand_key}:{idx}",
                    )
                ])
            else:
                key = item_key("brand", cat_key, brand_key, idx)
                keyboard.append([
                    build_stock_button(item["name"], item["price"], key)
                ])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"cat:{cat_key}")])

    title = brand.get("title", brand_key)

    if brand.get("price_range"):
        title += f"\n💶 {brand['price_range']}"

    await show_text(
        update,
        title + "\n\nОберіть товар:",
        InlineKeyboardMarkup(keyboard),
    )


async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    _, cat_key, brand_key, block_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        block = brand["items"][int(block_idx)]
    except Exception:
        await show_text(update, "❌ Позицію не знайдено.", kb_main())
        return

    keyboard = []

    for idx, flavor in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, block_idx, idx)
        keyboard.append([
            build_flavor_button(str(flavor), key)
        ])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")
    ])

    await show_text(
        update,
        f"{brand.get('title', brand_key)} {block.get('nicotine')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def flavors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    _, cat_key, brand_key, parent_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        parent = brand["items"][int(parent_idx)]
        flavors = parent.get("items", [])
    except Exception:
        await show_text(update, "❌ Смаки не знайдено.", kb_main())
        return

    keyboard = []

    for idx, flavor in enumerate(flavors):
        flavor_name = extract_flavor_name(flavor)
        key = item_key("flv", cat_key, brand_key, parent_idx, idx)

        keyboard.append([
            build_flavor_button(flavor_name, key)
        ])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")
    ])

    await show_text(
        update,
        f"{parent.get('name', 'Товар')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )

# ================== ADD / RESERVE ==================


async def add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    key = q.data.split(":", 1)[1]

    item = resolve_item(key)
    if not item:
        await show_text(update, "❌ Товар не знайдено.", kb_main())
        return

    st = stock_get(key)
    if not st.get("in_stock", True):
        await show_text(
            update,
            "❌ Цього товару вже немає в наявності.",
            kb_main(),
        )
        return

    cart = cart_get(context)
    cart.append({
        "key": item["key"],
        "name": item["name"],
        "price": item["price"],
    })

    await show_text(
        update,
        "✅ Додано в кошик\n\n"
        f"🧾 {item['name']}\n"
        f"💶 {fmt_price(item['price'])}",
        kb_after_add(),
    )


async def reserve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    key = q.data.split(":", 1)[1]

    item = resolve_item(key)
    if not item:
        await show_text(update, "❌ Товар не знайдено.", kb_main())
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

# ================== CART ==================


async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    cart = cart_get(context)

    if not cart:
        await show_text(
            update,
            "🛒 Кошик порожній.",
            InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
                [InlineKeyboardButton("⬅ На головну", callback_data="main")],
            ]),
        )
        return

    lines = [
        f"{i + 1}. {item['name']} — {fmt_price(item['price'])}"
        for i, item in enumerate(cart)
    ]

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("➖ Прибрати останній", callback_data="remove_last")],
        [InlineKeyboardButton("🗑 Очистити кошик", callback_data="clear_cart")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
        [InlineKeyboardButton("⬅ На головну", callback_data="main")],
    ]

    await show_text(
        update,
        "🛒 Ваше замовлення:\n\n"
        + "\n".join(lines)
        + f"\n\n💰 Разом: {fmt_price(cart_total(cart))}",
        InlineKeyboardMarkup(keyboard),
    )


async def remove_last_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    cart = cart_get(context)

    if cart:
        cart.pop()

    await cart_handler(update, context)


async def clear_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    context.user_data["cart"] = []

    await show_text(update, "🗑 Кошик очищено.", kb_main())

# ================== CHECKOUT ==================


async def checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query
    user = q.from_user
    cart = cart_get(context)

    if not cart:
        await show_text(update, "🛒 Кошик порожній.", kb_main())
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
            + "\n".join(f"• {name}" for name in unavailable)
            + "\n\nОчистіть кошик і виберіть товари заново.",
            kb_main(),
        )
        return

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

    for admin_id in ADMIN_IDS:
        await send_message_safe(context, admin_id, order_text)

    await send_message_safe(
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
        "courier_chat_id": city_cfg["courier_chat_id"],
        "courier_username": city_cfg["courier_username"],
        "created_at": timestamp,
    })

    context.user_data["cart"] = []

    await show_text(
        update,
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{city_cfg['courier_username']}",
        InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
            [InlineKeyboardButton("⬅ На головну", callback_data="main")],
        ]),
    )

# ================== ADMIN ==================


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

    await update.message.reply_text(
        "🛠 Адмін-панель\nОберіть категорію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def admin_cat_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG.get("categories", {}).get(cat_key)

    if not cat:
        await show_text(update, "❌ Категорію не знайдено.")
        return

    keyboard = []

    for brand_key, brand in cat.get("brands", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                brand.get("title", brand_key),
                callback_data=f"admin_brand:{cat_key}:{brand_key}",
            )
        ])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_home")])

    await show_text(
        update,
        f"⚙️ {cat.get('title', cat_key)}\nОберіть бренд:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key = q.data.split(":", 2)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    except Exception:
        await show_text(update, "❌ Бренд не знайдено.")
        return

    keyboard = []
    items = brand.get("items", [])

    for idx, item in enumerate(items):
        if isinstance(item, dict) and "nicotine" in item:
            keyboard.append([
                InlineKeyboardButton(
                    f"{item.get('nicotine')} — {fmt_price(item.get('price'))}",
                    callback_data=f"admin_nic:{cat_key}:{brand_key}:{idx}",
                )
            ])

        elif isinstance(item, dict) and "name" in item:
            if isinstance(item.get("items"), list) and item["items"]:
                keyboard.append([
                    InlineKeyboardButton(
                        f"🍓 {item['name']}",
                        callback_data=f"admin_flv:{cat_key}:{brand_key}:{idx}",
                    )
                ])
            else:
                key = item_key("brand", cat_key, brand_key, idx)
                keyboard.append([admin_item_button(key)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])

    await show_text(
        update,
        f"⚙️ {brand.get('title', brand_key)}\nОберіть позицію:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_nic_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        block = brand["items"][int(block_idx)]
    except Exception:
        await show_text(update, "❌ Блок не знайдено.")
        return

    keyboard = []

    for idx, _flavor in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, block_idx, idx)
        keyboard.append([admin_item_button(key)])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")
    ])

    await show_text(
        update,
        f"⚙️ {brand.get('title', brand_key)} {block.get('nicotine')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_flv_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key, parent_idx = q.data.split(":", 3)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        parent = brand["items"][int(parent_idx)]
        flavors = parent["items"]
    except Exception:
        await show_text(update, "❌ Смаки не знайдено.")
        return

    keyboard = []

    for idx, _flavor in enumerate(flavors):
        key = item_key("flv", cat_key, brand_key, parent_idx, idx)
        keyboard.append([admin_item_button(key)])

    keyboard.append([
        InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")
    ])

    await show_text(
        update,
        f"⚙️ {parent.get('name', 'Товар')}\nОберіть смак:",
        InlineKeyboardMarkup(keyboard),
    )


async def admin_toggle_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    key = q.data.split(":", 1)[1]
    item = resolve_item(key)

    if not item:
        await show_text(update, "❌ Товар не знайдено.")
        return

    st = stock_get(key)

    if st.get("in_stock", True):
        context.user_data["awaiting_eta_key"] = key

        await show_text(
            update,
            "❌ Ставимо товар як «немає в наявності»\n\n"
            f"🧾 {item['name']}\n"
            f"💶 {fmt_price(item['price'])}\n\n"
            "Напиши дату надходження у форматі YYYY-MM-DD:",
        )
        return

    stock_set(key, in_stock=True, eta=None)

    await show_text(
        update,
        "✅ Товар знову в наявності:\n\n"
        f"🧾 {item['name']}",
    )


async def admin_home_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await answer_callback(update)

    q = update.callback_query

    if not is_admin(q.from_user.id):
        return

    keyboard = []

    for cat_key, cat in CATALOG.get("categories", {}).items():
        keyboard.append([
            InlineKeyboardButton(
                f"⚙️ {cat.get('title', cat_key)}",
                callback_data=f"admin_cat:{cat_key}",
            )
        ])

    await show_text(
        update,
        "🛠 Адмін-панель\nОберіть категорію:",
        InlineKeyboardMarkup(keyboard),
    )

# ================== ERROR HANDLER ==================


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    logging.error("Exception in handler", exc_info=context.error)

# ================== MAIN ==================


def main() -> None:
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin_cmd))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

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

    app.add_handler(CallbackQueryHandler(admin_home_handler, pattern=r"^admin_home$"))
    app.add_handler(CallbackQueryHandler(admin_cat_handler, pattern=r"^admin_cat:"))
    app.add_handler(CallbackQueryHandler(admin_brand_handler, pattern=r"^admin_brand:"))
    app.add_handler(CallbackQueryHandler(admin_nic_handler, pattern=r"^admin_nic:"))
    app.add_handler(CallbackQueryHandler(admin_flv_handler, pattern=r"^admin_flv:"))
    app.add_handler(CallbackQueryHandler(admin_toggle_handler, pattern=r"^admin_toggle:"))

    app.add_error_handler(error_handler)

    logging.info("Bot started")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
