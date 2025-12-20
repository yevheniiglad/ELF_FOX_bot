import os
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ApplicationBuilder, CommandHandler, CallbackQueryHandler, ContextTypes

TOKEN = os.environ["BOT_TOKEN"]
OWNER_ID = int(os.environ["OWNER_ID"])
CATALOG_URL = os.environ["CATALOG_URL"]

PRODUCTS = [
    {"id": "p1", "name": "Товар 1", "price": 10},
    {"id": "p2", "name": "Товар 2", "price": 15},
    {"id": "p3", "name": "Товар 3", "price": 20},
]

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton("📦 Переглянути каталог", url=CATALOG_URL)],
        [InlineKeyboardButton("🛒 Зробити замовлення", callback_data="order")]
    ]
    await update.message.reply_text(
        "Вітаю! Оберіть дію:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = []
    for p in PRODUCTS:
        keyboard.append([
            InlineKeyboardButton(
                f"{p['name']} — {p['price']} €",
                callback_data=f"buy_{p['id']}"
            )
        ])

    await update.callback_query.message.reply_text(
        "Оберіть товар:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def buy(update: Update, context: ContextTypes.DEFAULT_TYPE):
    product_id = update.callback_query.data.replace("buy_", "")
    product = next(p for p in PRODUCTS if p["id"] == product_id)

    cart = context.user_data.get("cart", [])
    cart.append(product)
    context.user_data["cart"] = cart

    total = sum(p["price"] for p in cart)

    text = "🛒 Ваш кошик:\n"
    for p in cart:
        text += f"• {p['name']} — {p['price']} €\n"
    text += f"\nСума: {total} €"

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="order")],
        [InlineKeyboardButton("✅ Підтвердити", callback_data="confirm")]
    ]

    await update.callback_query.message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(keyboard)
    )

async def confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    cart = context.user_data.get("cart", [])
    total = sum(p["price"] for p in cart)

    text = "🆕 НОВЕ ЗАМОВЛЕННЯ\n"
    text += f"Клієнт: {update.effective_user.full_name}\n\n"
    for p in cart:
        text += f"• {p['name']} — {p['price']} €\n"
    text += f"\nСума: {total} €"

    await context.bot.send_message(chat_id=OWNER_ID, text=text)
    await update.callback_query.message.reply_text("✅ Замовлення прийнято!")
    context.user_data.clear()

app = ApplicationBuilder().token(TOKEN).build()
app.add_handler(CommandHandler("start", start))
app.add_handler(CallbackQueryHandler(order, pattern="order"))
app.add_handler(CallbackQueryHandler(buy, pattern="buy_"))
app.add_handler(CallbackQueryHandler(confirm, pattern="confirm"))

app.run_webhook(
    listen="0.0.0.0",
    port=int(os.environ.get("PORT", 10000)),
    webhook_url=os.environ["WEBHOOK_URL"]
)
