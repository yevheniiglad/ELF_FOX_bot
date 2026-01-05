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

COURIERS = {
    "Dresden": "@dresden_fox",
    "Leipzig": "@leipzig_foxs",
    "DEFAULT": "@courier_fox"
}

def get_admin_ids():
    ids = []
    for key in ("ADMIN_ID", "ADMIN_ID1"):
        val = os.getenv(key)
        if val and val.isdigit():
            ids.append(int(val))
    if not ids:
        raise RuntimeError("ADMIN_ID not set")
    return ids

ADMIN_IDS = get_admin_ids()

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

# ================== LOAD CATALOG ==================
with open("catalog.json", "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

CURRENCY = CATALOG.get("currency", "EUR")

# ================== HELPERS ==================
def get_cart(context):
    return context.user_data.setdefault("cart", [])

def cart_total(cart):
    return round(sum(i["price"] for i in cart), 2)

def get_username(user):
    return f"@{user.username}" if user.username else f"id:{user.id}"

def get_courier(city: str):
    return COURIERS.get(city, COURIERS["DEFAULT"])

async def send_photo(chat, photo_path, caption=None):
    if photo_path and os.path.exists(photo_path):
        await chat.send_photo(
            photo=InputFile(photo_path),
            caption=caption
        )
        return True
    return False

# ================== START / CITY ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:OTHER")]
    ]
    await update.message.reply_text(
        "Звідки ви?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def city_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    city = q.data.split(":")[1]

    if city == "OTHER":
        context.user_data["awaiting_city"] = True
        await q.edit_message_text("✍️ Напишіть ваше місто:")
    else:
        context.user_data["city"] = city
        await show_main_menu(q)

async def city_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.user_data.get("awaiting_city"):
        return
    context.user_data["city"] = update.message.text.strip()
    context.user_data.pop("awaiting_city")
    await show_main_menu(update)

# ================== MAIN MENU ==================
async def show_main_menu(update_or_query):
    keyboard = [
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")]
    ]
    text = "Вітаю 👋\nОберіть дію:"
    if hasattr(update_or_query, "edit_message_text"):
        await update_or_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update_or_query.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ================== CATALOG ==================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    keyboard = []
    for key, data in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(data["title"], callback_data=f"category:{key}")])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])

    await q.edit_message_text(
        "Оберіть категорію:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CATEGORY ==================
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    category = q.data.split(":")[1]
    cat = CATALOG["categories"][category]

    if cat.get("photo"):
        await send_photo(q.message.chat, cat["photo"], cat["title"])

    keyboard = []

    if "brands" in cat:
        for brand in cat["brands"]:
            keyboard.append([
                InlineKeyboardButton(brand, callback_data=f"brand:{category}:{brand}")
            ])
    else:
        for item in cat["items"]:
            keyboard.append([
                InlineKeyboardButton(
                    f"{item['name']} — {item['price']} {CURRENCY}",
                    callback_data=f"add:{category}:{item['name']}"
                )
            ])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    await q.message.reply_text(
        "Оберіть:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== BRAND ==================
async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    _, category, brand = q.data.split(":", 2)

    data = CATALOG["categories"][category]["brands"][brand]

    if data.get("photo"):
        await send_photo(q.message.chat, data["photo"], f"{brand} — {data['price']} {CURRENCY}")

    keyboard = []
    for flavor in data["items"]:
        keyboard.append([
            InlineKeyboardButton(
                flavor,
                callback_data=f"add:{category}:{brand}:{flavor}"
            )
        ])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{category}")])

    await q.message.reply_text(
        "Оберіть смак:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== ADD TO CART ==================
async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    cart = get_cart(context)

    if len(parts) == 4:
        _, category, brand, flavor = parts
        price = CATALOG["categories"][category]["brands"][brand]["price"]
        name = f"{brand} — {flavor}"
    else:
        _, category, name = parts
        item = next(i for i in CATALOG["categories"][category]["items"] if i["name"] == name)
        price = item["price"]

    cart.append({"name": name, "price": price})

    await q.edit_message_text(
        f"✅ Додано:\n{name}\n💶 {price} {CURRENCY}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
            [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
        ])
    )

# ================== CART ==================
async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cart = get_cart(context)

    if not cart:
        await q.edit_message_text("🛒 Кошик порожній")
        return

    text = "🛒 Ваше замовлення:\n\n"
    text += "\n".join(f"• {i['name']} — {i['price']} {CURRENCY}" for i in cart)
    text += f"\n\n💰 Разом: {cart_total(cart)} {CURRENCY}"

    await q.edit_message_text(
        text,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
            [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
            [InlineKeyboardButton("❌ Очистити", callback_data="clear_cart")]
        ])
    )

async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    context.user_data["cart"] = []
    await q.edit_message_text("🗑 Кошик очищено")

# ================== CHECKOUT ==================
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    cart = get_cart(context)
    city = context.user_data.get("city", "Невідомо")
    courier = get_courier(city)

    text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"👤 Клієнт: {get_username(user)}\n"
        f"ID: {user.id}\n"
        f"📍 Місто: {city}\n\n"
        "🛒 Товари:\n" +
        "\n".join(f"• {i['name']} — {i['price']} {CURRENCY}" for i in cart) +
        f"\n\n💰 Разом: {cart_total(cart)} {CURRENCY}"
        f"\n🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    for admin in ADMIN_IDS:
        await context.bot.send_message(admin, text)

    context.user_data.clear()

    await q.edit_message_text(
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{courier}"
    )

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(city_handler, pattern="^city:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, city_text_handler))

    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))
    app.add_handler(CallbackQueryHandler(brand_handler, pattern="^brand:"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add:"))
    app.add_handler(CallbackQueryHandler(cart_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(clear_cart, pattern="^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout, pattern="^checkout$"))

    app.run_polling()

if __name__ == "__main__":
    main()
