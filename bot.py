import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = int(os.getenv("OWNER_ID"))
CATALOG_URL = os.getenv("CATALOG_URL")

if not BOT_TOKEN or not OWNER_ID or not CATALOG_URL:
    raise RuntimeError("❌ Missing required environment variables")

# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ================== DATA ==================
PRODUCTS = {
    "p1": {"name": "Товар 1", "price": 10},
    "p2": {"name": "Товар 2", "price": 15},
    "p3": {"name": "Товар 3", "price": 20},
}

# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 Переглянути каталог", url=CATALOG_URL)],
        [InlineKeyboardButton("🛒 Зробити замовлення", callback_data="order")],
    ]
    await update.message.reply_text(
        "Вітаю! Оберіть дію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [InlineKeyboardButton(
            f"{p['name']} — {p['price']} €",
            callback_data=pid,
        )]
        for pid, p in PRODUCTS.items()
    ]

    await query.message.reply_text(
        "Оберіть товар:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    product = PRODUCTS.get(query.data)
    if not product:
        await query.message.reply_text("❌ Товар не знайдено")
        return

    cart = context.user_data.setdefault("cart", [])
    cart.append(product)

    total = sum(item["price"] for item in cart)

    text = "🛒 Ваш кошик:\n"
    for item in cart:
        text += f"• {item['name']} — {item['price']} €\n"
    text += f"\n💰 Сума: {total} €"

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="order")],
        [InlineKeyboardButton("✅ Підтвердити", callback_data="confirm")],
    ]

    await query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cart = context.user_data.get("cart")
    if not cart:
        await query.message.reply_text("🛒 Кошик порожній")
        return

    total = sum(item["price"] for item in cart)

    text = "🆕 НОВЕ ЗАМОВЛЕННЯ\n"
    text += f"👤 Клієнт: {update.effective_user.full_name}\n\n"
    for item in cart:
        text += f"• {item['name']} — {item['price']} €\n"
    text += f"\n💰 Сума: {total} €"

    await context.bot.send_message(chat_id=OWNER_ID, text=text)
    await query.message.reply_text("✅ Замовлення прийнято!")

    context.user_data.clear()

# ================== MAIN ==================
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(show_products, pattern="^order$"))
    app.add_handler(CallbackQueryHandler(confirm_order, pattern="^confirm$"))
    app.add_handler(CallbackQueryHandler(add_to_cart))

    logger.info("🤖 Bot started successfully")
    app.run_polling()

if __name__ == "__main__":
    main()
