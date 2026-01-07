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

# ================== START & CITY ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:OTHER")],
    ]
    await update.message.reply_text(
        "Звідки ви?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def city_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    city = q.data.split(":", 1)[1]

    if city == "OTHER":
        context.user_data["awaiting_city"] = True
        await q.edit_message_text("✍️ Напишіть назву міста:")
    else:
        context.user_data["city"] = city
        await show_main_menu(q)

async def city_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_city"):
        context.user_data["city"] = update.message.text.strip()
        context.user_data.pop("awaiting_city", None)
        await show_main_menu(update)

# ================== MAIN MENU ==================
async def show_main_menu(update_or_query):
    kb = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
    ]
    text = "Вітаю 👋\nОберіть дію:"
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

# ================== CATALOG ==================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    keyboard = [
        [InlineKeyboardButton(cat["title"], callback_data=f"category:{key}")]
        for key, cat in CATALOG["categories"].items()
    ]
    keyboard.append([InlineKeyboardButton("⬅ На головну", callback_data="start")])

    await q.edit_message_text(
        "Оберіть категорію:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CATEGORY ==================
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG["categories"][cat_key]

    keyboard = [
        [InlineKeyboardButton(
            brand.get("title", brand_key),
            callback_data=f"brand:{cat_key}:{brand_key}"
        )]
        for brand_key, brand in cat["brands"].items()
    ]

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    await q.message.reply_text(
        "Оберіть товар:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== BRAND (ФОТО + СМАКИ) ==================
async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key = q.data.split(":", 2)
    brand = CATALOG["categories"][cat_key]["brands"][brand_key]

    caption = brand.get("title", "")
    if brand.get("price_range"):
        caption += f"\n{brand['price_range']}"

    # ✅ ФОТО БРЕНДУ (URL)
    await send_photo(
        bot=context.bot,
        chat_id=q.message.chat.id,
        photo=brand.get("photo"),
        caption=caption
    )

    keyboard = []
    items = brand.get("items", [])

    # Прямі смаки (ELFLIQ, HQD, NASTY)
    if items and isinstance(items[0], dict) and "name" in items[0]:
        for idx, it in enumerate(items):
            keyboard.append([
                InlineKeyboardButton(
                    f"{it['name']} — {it['price']} {CURRENCY}",
                    callback_data=f"addb:{cat_key}:{brand_key}:{idx}"
                )
            ])
    # Блоки нікотину (CHASER)
    else:
        for idx, block in enumerate(items):
            keyboard.append([
                InlineKeyboardButton(
                    f"{block['nicotine']} — {block['price']} {CURRENCY}",
                    callback_data=f"nic:{cat_key}:{brand_key}:{idx}"
                )
            ])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{cat_key}")])

    await q.message.reply_text(
        "Оберіть:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== NICOTINE ==================
async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)
    block = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][int(block_idx)]

    keyboard = [
        [InlineKeyboardButton(flavor, callback_data=f"addn:{cat_key}:{brand_key}:{block_idx}:{i}")]
        for i, flavor in enumerate(block["items"])
    ]
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])

    await q.message.reply_text(
        "Оберіть смак:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== ADD TO CART ==================
async def add_to_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    cart = get_cart(context)

    if parts[0] == "addb":
        _, cat, brand, idx = parts
        item = CATALOG["categories"][cat]["brands"][brand]["items"][int(idx)]
        cart.append({"name": item["name"], "price": item["price"]})

    elif parts[0] == "addn":
        _, cat, brand, bidx, fidx = parts
        block = CATALOG["categories"][cat]["brands"][brand]["items"][int(bidx)]
        flavor = block["items"][int(fidx)]
        cart.append({
            "name": f"{brand} {block['nicotine']} — {flavor}",
            "price": block["price"]
        })

    await q.edit_message_text(
        f"✅ Додано:\n{cart[-1]['name']} — {cart[-1]['price']} {CURRENCY}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Ще", callback_data="catalog")],
            [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
        ])
    )

# ================== CART ==================
async def cart_view_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cart = get_cart(context)
    if not cart:
        await q.edit_message_text("🛒 Кошик порожній")
        return

    text = "🛒 Ваше замовлення:\n\n"
    for i, item in enumerate(cart, 1):
        text += f"{i}. {item['name']} — {item['price']} {CURRENCY}\n"
    text += f"\n💰 Разом: {cart_total(cart)} {CURRENCY}"

    await q.edit_message_text(text)

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
