import os
import json
import logging
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters
)

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")


def get_admin_ids():
    ids = []
    for key in ("ADMIN_ID", "ADMIN_ID1"):
        v = os.getenv(key)
        if v and v.isdigit():
            ids.append(int(v))
    if not ids:
        raise RuntimeError("ADMIN_ID variables not set correctly")
    return ids


ADMIN_IDS = get_admin_ids()

COURIERS = {
    "Dresden": "@dresden_fox",
    "Leipzig": "@leipzig_foxs",
    "DEFAULT": "@courier_fox"
}

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

# ================== LOAD CATALOG ==================
with open("catalog.json", "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

CURRENCY = CATALOG.get("currency", "EUR")

# ================== STOCK (availability) ==================
STOCK_FILE = "stock.json"


def load_stock() -> dict:
    if not os.path.exists(STOCK_FILE):
        return {}
    try:
        with open(STOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_stock(data: dict) -> None:
    with open(STOCK_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def item_key(*parts: str) -> str:
    # короткий, стабільний ключ
    return ":".join(parts)


def stock_get(stock: dict, key: str) -> dict:
    # за замовчуванням "є в наявності"
    return stock.get(key, {"in_stock": True, "eta": None})


def resolve_item_by_key(key: str):
    """
    Повертає (title, price) або (key, None) якщо не знайшли.
    Формати ключів:
      - cat:<cat_key>:<idx>
      - brand:<cat_key>:<brand_key>:<idx>
      - nic:<cat_key>:<brand_key>:<block_idx>:<flavor_idx>
    """
    try:
        parts = key.split(":")
        t = parts[0]

        if t == "cat":
            _, cat_key, idx = parts
            it = CATALOG["categories"][cat_key]["items"][int(idx)]
            return it.get("name", key), it.get("price")

        if t == "brand":
            _, cat_key, brand_key, idx = parts
            it = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][int(idx)]
            return it.get("name", key), it.get("price")

        if t == "nic":
            _, cat_key, brand_key, block_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            block = brand["items"][int(block_idx)]
            flavor = block["items"][int(flavor_idx)]
            title = f"{brand.get('title','')} {block.get('nicotine')} — {flavor}"
            return title.strip(), block.get("price")

    except Exception:
        pass

    return key, None


# ================== HELPERS ==================
async def send_photo(bot, chat_id, photo, caption=None):
    if not photo:
        return
    try:
        await bot.send_photo(
            chat_id=chat_id,
            photo=photo,
            caption=caption
        )
    except Exception as e:
        logging.warning(f"Не вдалося надіслати фото: {e}")


def get_cart(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.setdefault("cart", [])


def cart_total(cart):
    return round(sum(item["price"] for item in cart), 2)


def get_username(user):
    return f"@{user.username}" if user.username else f"id:{user.id}"


def get_courier_for_city(city: str):
    return COURIERS.get(city, COURIERS["DEFAULT"])


async def safe_send_photo(message_or_chat, path: str | None, caption: str | None = None):
    if not path:
        return False
    try:
        if path.startswith("http://") or path.startswith("https://"):
            if hasattr(message_or_chat, "reply_photo"):
                await message_or_chat.reply_photo(photo=path, caption=caption)
            else:
                await message_or_chat.send_photo(photo=path, caption=caption)
            return True
        elif os.path.exists(path):
            file = InputFile(path)
            if hasattr(message_or_chat, "reply_photo"):
                await message_or_chat.reply_photo(photo=file, caption=caption)
            else:
                await message_or_chat.send_photo(photo=file, caption=caption)
            return True
        else:
            logging.warning("Photo not found: %s", path)
            return False
    except Exception as e:
        logging.exception("Failed to send photo %s: %s", path, e)
        return False


# ================== START & CITY SELECTION ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:OTHER")],
    ]
    if update.message:
        await update.message.reply_text("Звідки ви?", reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.edit_message_text("Звідки ви?", reply_markup=InlineKeyboardMarkup(keyboard))


async def city_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    city = q.data.split(":", 1)[1]

    if city == "OTHER":
        context.user_data["awaiting_city"] = True
        await q.edit_message_text("✍️ Напишіть, будь ласка, назву вашого міста:")
    else:
        context.user_data["city"] = city
        await show_main_menu(q, context)


# ЄДИНИЙ текстовий router (щоб city/reserve/admin не конфліктували)
async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    # 1) Адмін вводить ETA
    if context.user_data.get("awaiting_eta_key"):
        if not is_admin(user_id):
            context.user_data.pop("awaiting_eta_key", None)
            return

        eta = text
        try:
            datetime.strptime(eta, "%Y-%m-%d")
        except ValueError:
            await update.message.reply_text("❌ Невірний формат. Треба YYYY-MM-DD (наприклад 2026-01-20).")
            return

        key = context.user_data["awaiting_eta_key"]
        stock = load_stock()
        stock[key] = {"in_stock": False, "eta": eta}
        save_stock(stock)

        context.user_data.pop("awaiting_eta_key", None)

        title, price = resolve_item_by_key(key)
        extra = f" — {price} {CURRENCY}" if price is not None else ""
        await update.message.reply_text(f"✅ Позначено як ❌ Нема в наявності.\nТовар: {title}{extra}\nОчікується з: {eta}")
        return

    # 2) Клієнт вводить контакт/коментар для бронювання
    if context.user_data.get("reserve_key"):
        key = context.user_data["reserve_key"]
        context.user_data.pop("reserve_key", None)

        city = context.user_data.get("city", "Невідомо")
        stock = load_stock()
        st = stock_get(stock, key)
        eta = st.get("eta")

        title, price = resolve_item_by_key(key)
        price_txt = f"{price} {CURRENCY}" if price is not None else "—"

        admin_text = (
            "📌 НОВЕ БРОНЮВАННЯ\n\n"
            f"👤 Клієнт: {get_username(update.effective_user)}\n"
            f"ID: {update.effective_user.id}\n"
            f"📍 Місто: {city}\n\n"
            f"🧾 Товар: {title}\n"
            f"💶 Ціна: {price_txt}\n"
            f"🗓 Очікується з: {eta or 'не вказано'}\n\n"
            f"💬 Контакт/коментар: {text}"
        )

        for admin_id in ADMIN_IDS:
            try:
                await context.bot.send_message(chat_id=admin_id, text=admin_text)
            except Exception as e:
                logging.exception("Failed to send reservation to admin %s: %s", admin_id, e)

        eta_text = f"Очікується з {eta}." if eta else "Дата надходження ще не вказана."
        await update.message.reply_text(f"✅ Дякую! Бронювання передано адміну.\n{eta_text}")
        return

    # 3) Введення міста (коли OTHER)
    if context.user_data.get("awaiting_city"):
        context.user_data["city"] = text
        context.user_data.pop("awaiting_city", None)
        await show_main_menu(update, context)
        return

    # якщо це “просто текст” — ігноруємо
    return


# ================== MAIN MENU ==================
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE | None = None):
    kb = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
    ]
    text = "Вітаю 👋\nОберіть дію:"

    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        logging.warning("show_main_menu: unknown update type")


# wrapper щоб кнопка "На головну" працювала коректно
async def show_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_main_menu(q, context)


# ================== CATALOG: categories list ==================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    keyboard = []
    for cat_key, cat_data in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(cat_data["title"], callback_data=f"category:{cat_key}")])

    keyboard.append([InlineKeyboardButton("⬅ На головну", callback_data="start")])
    await q.edit_message_text("Оберіть категорію:", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== CATEGORY ==================
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat_key = q.data.split(":", 1)[1]

    if cat_key not in CATALOG["categories"]:
        await q.edit_message_text("Вибрана категорія не знайдена.")
        return

    cat = CATALOG["categories"][cat_key]
    await safe_send_photo(q.message, cat.get("photo"), caption=cat.get("title"))

    stock = load_stock()
    keyboard = []

    if "brands" in cat:
        for brand_key, brand in cat["brands"].items():
            label = brand.get("title", brand_key)
            keyboard.append([InlineKeyboardButton(label, callback_data=f"brand:{cat_key}:{brand_key}")])
    else:
        for idx, item in enumerate(cat.get("items", [])):
            key = item_key("cat", cat_key, str(idx))
            st = stock_get(stock, key)

            if st.get("in_stock", True):
                label = f"{item['name']} — {item['price']} {CURRENCY} ✅"
                cb = f"add:{cat_key}:{idx}"
            else:
                eta = st.get("eta")
                eta_txt = f" (з {eta})" if eta else ""
                label = f"{item['name']} — {item['price']} {CURRENCY} ❌{eta_txt}"
                cb = f"reserve:{key}"

            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])
    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== BRAND ==================
async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cat_key, brand_key = q.data.split(":", 2)

    cat = CATALOG["categories"].get(cat_key)
    if not cat or "brands" not in cat or brand_key not in cat["brands"]:
        await q.edit_message_text("Бренд не знайдено.")
        return

    brand = cat["brands"][brand_key]
    caption = brand.get("title")
    pr = brand.get("price_range")
    if pr:
        caption = f"{caption}\n{pr}"

    await safe_send_photo(q.message, brand.get("photo"), caption=caption)

    stock = load_stock()
    keyboard = []
    items = brand.get("items", [])

    if items:
        first = items[0]

        if isinstance(first, dict) and "nicotine" in first and "items" in first:
            # nicotine blocks
            for idx, block in enumerate(items):
                label = f"{block.get('nicotine')} — {block.get('price')} {CURRENCY}"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"nic:{cat_key}:{brand_key}:{idx}")])

        elif isinstance(first, dict) and "name" in first:
            # direct items are objects with name & price
            for idx, it in enumerate(items):
                key = item_key("brand", cat_key, brand_key, str(idx))
                st = stock_get(stock, key)

                if st.get("in_stock", True):
                    label = f"{it['name']} — {it['price']} {CURRENCY} ✅"
                    cb = f"addb:{cat_key}:{brand_key}:{idx}"
                else:
                    eta = st.get("eta")
                    eta_txt = f" (з {eta})" if eta else ""
                    label = f"{it['name']} — {it['price']} {CURRENCY} ❌{eta_txt}"
                    cb = f"reserve:{key}"

                keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        else:
            for idx, name in enumerate(items):
                label = name
                keyboard.append([InlineKeyboardButton(label, callback_data=f"addb:{cat_key}:{brand_key}:{idx}")])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{cat_key}")])
    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== NICOTINE (block) ==================
async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, cat_key, brand_key, block_idx = q.data.split(":", 3)

    brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    block = brand["items"][int(block_idx)]

    stock = load_stock()
    keyboard = []

    for idx, flavor in enumerate(block["items"]):
        key = item_key("nic", cat_key, brand_key, str(block_idx), str(idx))
        st = stock_get(stock, key)

        if st.get("in_stock", True):
            label = f"{flavor} ✅"
            cb = f"addn:{cat_key}:{brand_key}:{block_idx}:{idx}"
        else:
            eta = st.get("eta")
            eta_txt = f" (з {eta})" if eta else ""
            label = f"{flavor} ❌{eta_txt}"
            cb = f"reserve:{key}"

        keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])
    await q.message.reply_text("Оберіть смак:", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== RESERVE ==================
async def reserve_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    key = q.data.split(":", 1)[1]  # reserve:<key>
    stock = load_stock()
    st = stock_get(stock, key)
    eta = st.get("eta")

    title, price = resolve_item_by_key(key)
    price_txt = f"{price} {CURRENCY}" if price is not None else "—"

    context.user_data["reserve_key"] = key
    eta_text = f"Очікується з: {eta}" if eta else "Очікується (дату уточнюйте)"

    await q.edit_message_text(
        f"📌 Бронювання\n\n"
        f"🧾 {title}\n"
        f"💶 {price_txt}\n"
        f"🗓 {eta_text}\n\n"
        "Напишіть контакт/коментар (телефон, месенджер, коли зручно):"
    )


# ================== ADD TO CART (uniform, index-based) ==================
async def add_to_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    parts = data.split(":")

    cart = get_cart(context)

    if parts[0] == "add":
        _, cat_key, idx = parts
        idx = int(idx)
        item = CATALOG["categories"][cat_key]["items"][idx]
        cart.append({"name": item["name"], "price": item["price"]})

    elif parts[0] == "addb":
        _, cat_key, brand_key, idx = parts
        idx = int(idx)
        item = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][idx]
        cart.append({"name": f"{item['name']}", "price": item["price"]})

    elif parts[0] == "addn":
        _, cat_key, brand_key, block_idx, flavor_idx = parts
        block_idx = int(block_idx)
        flavor_idx = int(flavor_idx)
        block = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][block_idx]
        flavor = block["items"][flavor_idx]
        price = block["price"]
        cart.append({
            "name": f"{CATALOG['categories'][cat_key]['brands'][brand_key].get('title','')} {block.get('nicotine')} — {flavor}",
            "price": price
        })

    else:
        await q.edit_message_text("Невідома дія.")
        return

    await q.edit_message_text(
        f"✅ Додано: {cart[-1]['name']}\n💶 {cart[-1]['price']} {CURRENCY}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
            [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
        ])
    )


# ================== CART VIEW ==================
async def cart_view_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cart = get_cart(context)
    if not cart:
        await q.edit_message_text("🛒 Кошик порожній")
        return

    lines = [f"{i+1}. {item['name']} — {item['price']} {CURRENCY}" for i, item in enumerate(cart)]
    text = "🛒 Ваше замовлення:\n\n" + "\n".join(lines)
    text += f"\n\n💰 Разом: {cart_total(cart)} {CURRENCY}"

    kb = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
        [InlineKeyboardButton("❌ Очистити", callback_data="clear_cart")]
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))


# ================== CLEAR CART ==================
async def clear_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["cart"] = []
    await q.edit_message_text("🗑 Кошик очищено")


# ================== CHECKOUT ==================
async def checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    cart = get_cart(context)
    if not cart:
        await q.edit_message_text("🛒 Кошик порожній")
        return

    city = context.user_data.get("city", "Невідомо")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    admin_text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"👤 Клієнт: {get_username(user)}\n"
        f"ID: {user.id}\n"
        f"📍 Місто: {city}\n\n"
        "🛒 Товари:\n" +
        "\n".join(f"• {i['name']} — {i['price']} {CURRENCY}" for i in cart) +
        f"\n\n💰 Разом: {cart_total(cart)} {CURRENCY}\n"
        f"🕒 {timestamp}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception as e:
            logging.exception("Failed to send order to admin %s: %s", admin_id, e)

    courier = get_courier_for_city(city)
    context.user_data.pop("cart", None)

    await q.edit_message_text(
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{courier}"
    )


# ================== ADMIN PANEL ==================
async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return

    keyboard = []
    for cat_key, cat in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(f"⚙️ {cat.get('title', cat_key)}", callback_data=f"admin_cat:{cat_key}")])

    await update.message.reply_text("🛠 Адмін-панель (наявність):", reply_markup=InlineKeyboardMarkup(keyboard))


async def admin_cat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG["categories"].get(cat_key)
    if not cat:
        await q.edit_message_text("Категорія не знайдена.")
        return

    # якщо є бренди — покажемо бренди
    if "brands" in cat:
        kb = []
        for brand_key, brand in cat["brands"].items():
            kb.append([InlineKeyboardButton(brand.get("title", brand_key), callback_data=f"admin_brand:{cat_key}:{brand_key}")])
        kb.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_back")])
        await q.edit_message_text("Оберіть бренд:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # інакше — список item’ів категорії
    stock = load_stock()
    kb = []
    lines = []

    for idx, it in enumerate(cat.get("items", [])):
        key = item_key("cat", cat_key, str(idx))
        st = stock_get(stock, key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{idx+1}. {mark} {it['name']}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {it['name']}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_back")])

    await q.edit_message_text(
        "Керування наявністю:\n\n" + ("\n".join(lines) if lines else "Порожньо."),
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key = q.data.split(":", 2)
    brand = CATALOG["categories"][cat_key]["brands"].get(brand_key)
    if not brand:
        await q.edit_message_text("Бренд не знайдено.")
        return

    items = brand.get("items", [])
    if not items:
        await q.edit_message_text("Немає позицій у бренді.")
        return

    first = items[0]

    # якщо nicotine blocks — покажемо блоки
    if isinstance(first, dict) and "nicotine" in first and "items" in first:
        kb = []
        for bidx, block in enumerate(items):
            kb.append([InlineKeyboardButton(f"{block.get('nicotine')} — {block.get('price')} {CURRENCY}", callback_data=f"admin_block:{cat_key}:{brand_key}:{bidx}")])
        kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])
        await q.edit_message_text("Оберіть блок:", reply_markup=InlineKeyboardMarkup(kb))
        return

    # інакше — звичайні items (name/price)
    stock = load_stock()
    kb = []
    lines = []

    for idx, it in enumerate(items):
        if not isinstance(it, dict) or "name" not in it:
            continue

        key = item_key("brand", cat_key, brand_key, str(idx))
        st = stock_get(stock, key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{idx+1}. {mark} {it['name']}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {it['name']}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])

    await q.edit_message_text(
        "Керування наявністю:\n\n" + ("\n".join(lines) if lines else "Порожньо."),
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)
    brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    block = brand["items"][int(block_idx)]

    stock = load_stock()
    kb = []
    lines = []

    for fidx, flavor in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, str(block_idx), str(fidx))
        st = stock_get(stock, key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{fidx+1}. {mark} {flavor}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {flavor}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")])

    await q.edit_message_text(
        "Керування наявністю:\n\n" + ("\n".join(lines) if lines else "Порожньо."),
        reply_markup=InlineKeyboardMarkup(kb)
    )


async def admin_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    key = q.data.split(":", 1)[1]
    stock = load_stock()
    st = stock_get(stock, key)

    if st.get("in_stock", True):
        # робимо "нема", але спочатку запитаємо дату
        context.user_data["awaiting_eta_key"] = key
        title, price = resolve_item_by_key(key)
        extra = f" — {price} {CURRENCY}" if price is not None else ""
        await q.edit_message_text(
            f"❌ Ставимо 'нема в наявності'\n"
            f"Товар: {title}{extra}\n\n"
            "Вкажи дату надходження у форматі YYYY-MM-DD (наприклад 2026-01-20):"
        )
    else:
        # робимо "є"
        stock[key] = {"in_stock": True, "eta": None}
        save_stock(stock)

        title, price = resolve_item_by_key(key)
        extra = f" — {price} {CURRENCY}" if price is not None else ""
        await q.edit_message_text(f"✅ Тепер 'в наявності'\nТовар: {title}{extra}")


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not is_admin(q.from_user.id):
        return
    # повернемо список категорій
    keyboard = []
    for cat_key, cat in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(f"⚙️ {cat.get('title', cat_key)}", callback_data=f"admin_cat:{cat_key}")])
    await q.edit_message_text("🛠 Адмін-панель (наявність):", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== ERROR HANDLER ==================
async def error_handler(update, context):
    logging.error("Exception in handler", exc_info=context.error)


# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Start / city
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(city_callback_handler, pattern="^city:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Main menu & catalog
    app.add_handler(CallbackQueryHandler(show_main_menu_handler, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))

    # Brands, nicotine and adding
    app.add_handler(CallbackQueryHandler(brand_handler, pattern="^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern="^nic:"))
    app.add_handler(CallbackQueryHandler(add_to_cart_handler, pattern="^(add:|addb:|addn:)"))

    # Reserve
    app.add_handler(CallbackQueryHandler(reserve_handler, pattern="^reserve:"))

    # Cart / clear / checkout
    app.add_handler(CallbackQueryHandler(cart_view_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(clear_cart_handler, pattern="^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout_handler, pattern="^checkout$"))

    # Admin
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CallbackQueryHandler(admin_cat, pattern="^admin_cat:"))
    app.add_handler(CallbackQueryHandler(admin_brand, pattern="^admin_brand:"))
    app.add_handler(CallbackQueryHandler(admin_block, pattern="^admin_block:"))
    app.add_handler(CallbackQueryHandler(admin_toggle, pattern="^admin_toggle:"))
    app.add_handler(CallbackQueryHandler(admin_back, pattern="^admin_back$"))

    app.add_error_handler(error_handler)

    logging.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
