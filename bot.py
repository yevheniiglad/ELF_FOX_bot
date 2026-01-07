import os
import json
import logging
from datetime import datetime
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup
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

# ================== HELPERS ==================
def get_cart(context: ContextTypes.DEFAULT_TYPE):
    return context.user_data.setdefault("cart", [])

def cart_total(cart):
    return round(sum(item["price"] for item in cart), 2)

def get_username(user):
    return f"@{user.username}" if user.username else f"id:{user.id}"

def get_courier_for_city(city: str):
    return COURIERS.get(city, COURIERS["DEFAULT"])

# ✅ ЄДИНА ПРАВИЛЬНА ФУНКЦІЯ ДЛЯ ФОТО (URL)
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

# ================== START & CITY SELECTION ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # clear only relevant user state (keep history if needed)
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
        # fallback for callback-based start
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
        # show main menu
        await show_main_menu(q, context)

async def city_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_city"):
        return
    context.user_data["city"] = update.message.text.strip()
    context.user_data.pop("awaiting_city", None)
    await show_main_menu(update, context)

# ================== MAIN MENU ==================
async def show_main_menu(update_or_query, context: ContextTypes.DEFAULT_TYPE | None = None):
    kb = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
    ]
    text = "Вітаю 👋\nОберіть дію:"
    # update_or_query may be Message or CallbackQuery
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    elif hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        # fallback: use context to send to chat if available
        logging.warning("show_main_menu: unknown update type")

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
    # send category photo (if present)
    await safe_send_photo(q.message, cat.get("photo"), caption=cat.get("title"))

    keyboard = []
    # If category has brands -> show brands
    if "brands" in cat:
        for brand_key, brand in cat["brands"].items():
            label = brand.get("title", brand_key)
            keyboard.append([InlineKeyboardButton(label, callback_data=f"brand:{cat_key}:{brand_key}")])
    else:
        # flat items
        for idx, item in enumerate(cat.get("items", [])):
            # item expected to be { "name": "...", "price": N }
            label = f"{item['name']} — {item['price']} {CURRENCY}"
            keyboard.append([InlineKeyboardButton(label, callback_data=f"add:{cat_key}:{idx}")])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])
    # reply with options
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
    # send brand photo if exists
    caption = brand.get("title")
    # if brand has price_range show it in caption
    pr = brand.get("price_range")
    if pr:
        caption = f"{caption}\n{pr}"
    await safe_send_photo(q.message, brand.get("photo"), caption=caption)

    keyboard = []
    items = brand.get("items", [])

    # Two possible shapes:
    # 1) list of dicts with 'name' and 'price' -> direct flavors
    # 2) list of blocks { "nicotine": "...", "price": N, "items": [flavors...] } -> nicotine choices
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
                label = f"{it['name']} — {it['price']} {CURRENCY}"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"addb:{cat_key}:{brand_key}:{idx}")])
        else:
            # fallback: treat as list of strings (unlikely in provided JSON)
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

    keyboard = []
    for idx, flavor in enumerate(block["items"]):
        label = flavor
        keyboard.append([InlineKeyboardButton(label, callback_data=f"addn:{cat_key}:{brand_key}:{block_idx}:{idx}")])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])
    await q.message.reply_text("Оберіть смак:", reply_markup=InlineKeyboardMarkup(keyboard))

# ================== ADD TO CART (uniform, index-based) ==================
async def add_to_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    parts = data.split(":")

    cart = get_cart(context)

    if parts[0] == "add":
        # top-level category item: add:{cat}:{idx}
        _, cat_key, idx = parts
        idx = int(idx)
        item = CATALOG["categories"][cat_key]["items"][idx]
        cart.append({"name": item["name"], "price": item["price"]})

    elif parts[0] == "addb":
        # brand item with name/price objects: addb:{cat}:{brand}:{idx}
        _, cat_key, brand_key, idx = parts
        idx = int(idx)
        item = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][idx]
        cart.append({"name": f"{item['name']}", "price": item["price"]})

    elif parts[0] == "addn":
        # addn:{cat}:{brand}:{block_idx}:{flavor_idx}
        _, cat_key, brand_key, block_idx, flavor_idx = parts
        block_idx = int(block_idx); flavor_idx = int(flavor_idx)
        block = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][block_idx]
        flavor = block["items"][flavor_idx]
        price = block["price"]
        # Compose readable name
        cart.append({"name": f"{CATALOG['categories'][cat_key]['brands'][brand_key].get('title','') } {block.get('nicotine')} — {flavor}", "price": price})
    else:
        await q.edit_message_text("Невідома дія.")
        return

    # acknowledge and present quick options
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

    # Build admin message
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

    # send to all admins
    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=admin_text)
        except Exception as e:
            logging.exception("Failed to send order to admin %s: %s", admin_id, e)

    # Prepare courier message for client
    courier = get_courier_for_city(city)

    # clear user data (keeps history if needed, but clear cart + city)
    context.user_data.pop("cart", None)
    # keep city if you want; currently remove to require reselect if needed:
    # context.user_data.pop("city", None)

    await q.edit_message_text(
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{courier}"
    )

# ================== ERROR HANDLER ==================
async def error_handler(update, context):
    logging.error("Exception in handler", exc_info=context.error)

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Start / city
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(city_callback_handler, pattern="^city:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, city_text_handler))

    # Main menu & catalog
    app.add_handler(CallbackQueryHandler(show_main_menu, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))

    # Brands, nicotine and adding
    app.add_handler(CallbackQueryHandler(brand_handler, pattern="^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern="^nic:"))
    app.add_handler(CallbackQueryHandler(add_to_cart_handler, pattern="^(add:|addb:|addn:)"))

    # Cart / clear / checkout
    app.add_handler(CallbackQueryHandler(cart_view_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(clear_cart_handler, pattern="^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout_handler, pattern="^checkout$"))

    app.add_error_handler(error_handler)

    logging.info("Bot starting...")
    app.run_polling()

if __name__ == "__main__":
    main()
