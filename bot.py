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

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")

COURIERS = {
    "Dresden": "@dresden_fox",
    "Leipzig": "@leipzig_foxs",
    "DEFAULT": "@courier_fox"
}

def get_admin_ids():
    ids = []
    for k in ("ADMIN_ID", "ADMIN_ID1"):
        v = os.getenv(k)
        if v and v.isdigit():
            ids.append(int(v))
    return ids

ADMIN_IDS = get_admin_ids()

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)

# ================= LOAD CATALOG =================
with open("catalog.json", "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

CURRENCY = CATALOG.get("currency", "EUR")

# ================= HELPERS =================
def get_cart(ctx):
    return ctx.user_data.setdefault("cart", [])

def cart_total(cart):
    return round(sum(i["price"] for i in cart), 2)

def get_username(user):
    return f"@{user.username}" if user.username else f"id:{user.id}"

def get_courier(city):
    return COURIERS.get(city, COURIERS["DEFAULT"])

async def send_photo(chat, path, caption=None):
    if path and os.path.exists(path):
        await chat.send_photo(InputFile(path), caption=caption)

# ================= START / CITY =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    kb = [
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:OTHER")]
    ]
    await update.message.reply_text("Звідки ви?", reply_markup=InlineKeyboardMarkup(kb))

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

async def city_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("awaiting_city"):
        context.user_data["city"] = update.message.text.strip()
        context.user_data.pop("awaiting_city")
        await show_main_menu(update)

# ================= MAIN MENU =================
async def show_main_menu(u):
    kb = [[InlineKeyboardButton("🛍 Каталог", callback_data="catalog")]]
    text = "Вітаю 👋\nОберіть дію:"
    if hasattr(u, "edit_message_text"):
        await u.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))
    else:
        await u.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb))

# ================= CATALOG =================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    kb = [
        [InlineKeyboardButton(cat["title"], callback_data=f"category:{key}")]
        for key, cat in CATALOG["categories"].items()
    ]
    await q.edit_message_text("Оберіть категорію:", reply_markup=InlineKeyboardMarkup(kb))

# ================= CATEGORY =================
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cat_key = q.data.split(":")[1]
    cat = CATALOG["categories"][cat_key]
    context.user_data["category"] = cat_key

    await send_photo(q.message.chat, cat.get("photo"), cat["title"])

    kb = []

    if "brands" in cat:
        for b_key, b in cat["brands"].items():
            kb.append([
                InlineKeyboardButton(b["title"], callback_data=f"brand:{cat_key}:{b_key}")
            ])
    else:
        for i in cat["items"]:
            kb.append([
                InlineKeyboardButton(
                    f"{i['name']} — {i['price']} {CURRENCY}",
                    callback_data=f"add:{cat_key}:{i['name']}"
                )
            ])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(kb))

# ================= BRAND =================
async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key = q.data.split(":")
    brand = CATALOG["categories"][cat_key]["brands"][brand_key]

    await send_photo(q.message.chat, brand.get("photo"), brand["title"])

    kb = []

    # CHASER logic
    if isinstance(brand["items"][0], dict):
        for idx, block in enumerate(brand["items"]):
            kb.append([
                InlineKeyboardButton(
                    f"{block['nicotine']} — {block['price']} {CURRENCY}",
                    callback_data=f"nic:{cat_key}:{brand_key}:{idx}"
                )
            ])
    else:
        for item in brand["items"]:
            kb.append([
                InlineKeyboardButton(
                    f"{item['name']} — {item['price']} {CURRENCY}",
                    callback_data=f"addb:{cat_key}:{brand_key}:{item['name']}"
                )
            ])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{cat_key}")])
    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(kb))

# ================= NICOTINE =================
async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat, brand, idx = q.data.split(":")
    block = CATALOG["categories"][cat]["brands"][brand]["items"][int(idx)]

    kb = []
    for flavor in block["items"]:
        kb.append([
            InlineKeyboardButton(
                flavor,
                callback_data=f"addn:{cat}:{brand}:{idx}:{flavor}"
            )
        ])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat}:{brand}")])
    await q.message.reply_text("Оберіть смак:", reply_markup=InlineKeyboardMarkup(kb))

# ================= ADD TO CART =================
async def add_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    parts = q.data.split(":")
    cart = get_cart(context)

    if parts[0] == "add":
        _, cat, name = parts
        item = next(i for i in CATALOG["categories"][cat]["items"] if i["name"] == name)
        cart.append(item)

    elif parts[0] == "addb":
        _, cat, brand, name = parts
        item = next(i for i in CATALOG["categories"][cat]["brands"][brand]["items"] if i["name"] == name)
        cart.append(item)

    elif parts[0] == "addn":
        _, cat, brand, idx, flavor = parts
        block = CATALOG["categories"][cat]["brands"][brand]["items"][int(idx)]
        cart.append({"name": f"{brand.upper()} {block['nicotine']} — {flavor}", "price": block["price"]})

    await q.edit_message_text("✅ Додано в кошик")

# ================= CART =================
async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    cart = get_cart(context)

    if not cart:
        await q.edit_message_text("🛒 Кошик порожній")
        return

    text = "🛒 Замовлення:\n\n" + "\n".join(
        f"• {i['name']} — {i['price']} {CURRENCY}" for i in cart
    )
    text += f"\n\n💰 Разом: {cart_total(cart)} {CURRENCY}"

    kb = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")]
    ]

    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb))

# ================= CHECKOUT =================
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    cart = get_cart(context)
    city = context.user_data.get("city", "Невідомо")

    text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"👤 {get_username(user)}\n"
        f"📍 {city}\n\n"
        + "\n".join(f"• {i['name']} — {i['price']} {CURRENCY}" for i in cart) +
        f"\n\n💰 {cart_total(cart)} {CURRENCY}"
    )

    for admin in ADMIN_IDS:
        await context.bot.send_message(admin, text)

    await q.edit_message_text(
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{get_courier(city)}"
    )

    context.user_data.clear()

# ================= MAIN =================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(city_handler, pattern="^city:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, city_text))

    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))
    app.add_handler(CallbackQueryHandler(brand_handler, pattern="^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern="^nic:"))
    app.add_handler(CallbackQueryHandler(add_handler, pattern="^(add|addb|addn):"))
    app.add_handler(CallbackQueryHandler(cart_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(checkout, pattern="^checkout$"))

    app.run_polling()

if __name__ == "__main__":
    main()
