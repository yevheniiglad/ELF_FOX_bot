import os
import json
import logging
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes
)

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
import os

BOT_TOKEN = os.getenv("BOT_TOKEN")

ADMIN_IDS = [
    int(os.getenv("ADMIN_ID")),
    int(os.getenv("ADMIN_ID1")),
]

COURIER_URL = "https://t.me/managervapeshopdd"

if not BOT_TOKEN or not ADMIN_IDS[0]:
    raise RuntimeError("❌ BOT_TOKEN або ADMIN_ID не задані")

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO
)

# ================== LOAD CATALOG ==================
with open("catalog.json", "r", encoding="utf-8") as f:
    CATALOG = json.load(f)

# ================== HELPERS ==================
def get_cart(context):
    return context.user_data.setdefault("cart", [])

def get_username(user):
    return f"@{user.username}" if user.username else f"(id: {user.id})"

def calculate_total(cart):
    total = 0
    for item in cart:
        total += item["price"]
    return total

# ================== START ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("🛍 Каталог продукції", callback_data="catalog")],
        [InlineKeyboardButton("ℹ️ Контакти адміністратора", url=COURIER_URL)]
    ]

    if update.message:
        await update.message.reply_text(
            "Вітаю 👋\nОберіть, що бажаєте замовити:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
    else:
        await update.callback_query.edit_message_text(
            "Вітаю 👋\nОберіть, що бажаєте замовити:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )

# ================== CATALOG ==================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton("💧 Рідини", callback_data="category:liquids")],
        [InlineKeyboardButton("🔌 Девайси Vaporesso", callback_data="category:devices")],
        [InlineKeyboardButton("🔧 Картриджі Vaporesso", callback_data="category:pods")],
        [InlineKeyboardButton("🔥 Vozol 10k / 25k", callback_data="category:vozol")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
        [InlineKeyboardButton("⬅ На головну", callback_data="start")]
    ]

    await query.edit_message_text(
        "Оберіть категорію:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== ITEMS ==================
async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    category_key = query.data.split(":")[1]
    items = CATALOG["categories"][category_key]["items"]

    keyboard = [
        [InlineKeyboardButton(
            f"{item['name']} — {item['price']} €",
            callback_data=f"add:{category_key}:{item['name']}"
        )]
        for item in items
    ]

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])

    await query.edit_message_text(
        "Оберіть товар:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== ADD TO CART ==================
async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    _, category, name = query.data.split(":", 2)
    item = next(
        i for i in CATALOG["categories"][category]["items"]
        if i["name"] == name
    )

    cart = get_cart(context)
    cart.append(item)

    total = calculate_total(cart)

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")]
    ]

    await query.edit_message_text(
        f"✅ Додано: {item['name']}\n\n"
        f"💶 Поточна сума: {total} €",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

# ================== CART ==================
async def cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cart = get_cart(context)

    if not cart:
        text = "🛒 Ваш кошик порожній"
    else:
        total = calculate_total(cart)
        text = (
            "🛒 Ваше замовлення:\n\n" +
            "\n".join(f"{i+1}. {item['name']} — {item['price']} €"
                      for i, item in enumerate(cart)) +
            f"\n\n💶 Разом до оплати: {total} €"
        )

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("✅ Оформити замовлення", callback_data="checkout")],
        [InlineKeyboardButton("❌ Очистити кошик", callback_data="clear_cart")],
        [InlineKeyboardButton("⬅ На головну", callback_data="start")]
    ]

    await query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))

# ================== CLEAR CART ==================
async def clear_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["cart"] = []
    await query.edit_message_text("🗑 Кошик очищено", reply_markup=InlineKeyboardMarkup([
        [InlineKeyboardButton("⬅ На головну", callback_data="start")]
    ]))

# ================== CHECKOUT ==================
async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    user = query.from_user
    cart = get_cart(context)

    if not cart:
        await query.edit_message_text("🛒 Кошик порожній")
        return

    total = calculate_total(cart)

    order_text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"👤 Клієнт: {get_username(user)}\n"
        f"ID: {user.id}\n\n"
        "🛒 Товари:\n" +
        "\n".join(f"• {item['name']} — {item['price']} €" for item in cart) +
        f"\n\n💶 СУМА: {total} €"
        f"\n🕒 {datetime.now().strftime('%d.%m.%Y %H:%M')}"
    )

    for admin_id in ADMIN_IDS:
        await context.bot.send_message(chat_id=admin_id, text=order_text)

    context.user_data.clear()

    await query.edit_message_text(
        "✅ Дякуємо за замовлення!\n\n"
        f"💶 Сума до оплати: {total} €\n\n"
        "З вами звʼяжеться адміністратор:\n"
        f"{COURIER_URL}"
    )

# ================== ERROR ==================
async def error_handler(update, context):
    logging.error("Помилка:", exc_info=context.error)

# ================== MAIN ==================
def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(start, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add:"))
    app.add_handler(CallbackQueryHandler(cart_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(clear_cart, pattern="^clear_cart$"))
    app.add_handler(CallbackQueryHandler(checkout, pattern="^checkout$"))
    app.add_error_handler(error_handler)

    app.run_polling()

if __name__ == "__main__":
    main()
