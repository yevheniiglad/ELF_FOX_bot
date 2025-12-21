import os
import sys
from typing import Dict, List

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)

# ================== ENV ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
CATALOG_URL = os.getenv("CATALOG_URL")

if not BOT_TOKEN or not OWNER_ID:
    sys.exit("FATAL: BOT_TOKEN or OWNER_ID not set")

OWNER_ID = int(OWNER_ID)
CATALOG_URL = CATALOG_URL or "https://example.com"
# =========================================


# ================== DATA ==================
PRODUCTS: Dict[str, Dict] = {
    "p1": {"name": "Товар 1", "price": 10},
    "p2": {"name": "Товар 2", "price": 15},
    "p3": {"name": "Товар 3", "price": 20},
}

PAYMENT_METHODS = {
    "cash": "💶 Готівка",
    "bank": "🏦 Банківський переказ",
    "paypal": "💳 PayPal",
}
# =========================================


# ================== HELPERS ==================
def get_cart(context: ContextTypes.DEFAULT_TYPE) -> List[Dict]:
    return context.user_data.setdefault("cart", [])


def cart_total(cart: List[Dict]) -> int:
    return sum(item["price"] for item in cart)


def format_cart(cart: List[Dict]) -> str:
    lines = ["🛒 **Ваш кошик:**\n"]
    for item in cart:
        lines.append(f"• {item['name']} — {item['price']} €")
    lines.append(f"\n💰 **Сума:** {cart_total(cart)} €")
    return "\n".join(lines)
# ============================================


# ================== HANDLERS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    keyboard = [
        [InlineKeyboardButton("📦 Каталог", url=CATALOG_URL)],
        [InlineKeyboardButton("🛒 Зробити замовлення", callback_data="menu_order")],
    ]

    await update.message.reply_text(
        "Вітаю! Я бот для прийому замовлень 👋\nОберіть дію:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    keyboard = [
        [
            InlineKeyboardButton(
                f"{p['name']} — {p['price']} €",
                callback_data=f"add_{pid}",
            )
        ]
        for pid, p in PRODUCTS.items()
    ]
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="menu_start")])

    await query.message.reply_text(
        "Оберіть товар:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def add_to_cart(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    product_id = query.data.replace("add_", "")
    product = PRODUCTS.get(product_id)

    if not product:
        await query.message.reply_text("❌ Товар не знайдено")
        return

    cart = get_cart(context)
    cart.append(product)

    keyboard = [
        [InlineKeyboardButton("➕ Додати ще", callback_data="menu_order")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
    ]

    await query.message.reply_text(
        format_cart(cart),
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


async def checkout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    cart = get_cart(context)
    if not cart:
        await query.message.reply_text("🛒 Кошик порожній")
        return

    keyboard = [
        [InlineKeyboardButton(name, callback_data=f"pay_{key}")]
        for key, name in PAYMENT_METHODS.items()
    ]

    await query.message.reply_text(
        "Оберіть спосіб оплати:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    payment_key = query.data.replace("pay_", "")
    payment = PAYMENT_METHODS.get(payment_key, "Невідомо")

    cart = get_cart(context)
    total = cart_total(cart)
    user = update.effective_user

    text = (
        "🆕 **НОВЕ ЗАМОВЛЕННЯ**\n\n"
        f"👤 Клієнт: {user.full_name}\n"
        f"🆔 ID: {user.id}\n\n"
    )

    for item in cart:
        text += f"• {item['name']} — {item['price']} €\n"

    text += (
        f"\n💰 Сума: {total} €"
        f"\n💳 Оплата: {payment}"
    )

    await context.bot.send_message(
        chat_id=OWNER_ID,
        text=text,
        parse_mode="Markdown",
    )

    await query.message.reply_text(
        "✅ Дякуємо! Замовлення прийнято.\nМи з вами звʼяжемось найближчим часом."
    )

    context.user_data.clear()
# ============================================


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CallbackQueryHandler(show_products, pattern="^menu_order$"))
    app.add_handler(CallbackQueryHandler(start, pattern="^menu_start$"))
    app.add_handler(CallbackQueryHandler(add_to_cart, pattern="^add_"))
    app.add_handler(CallbackQueryHandler(checkout, pattern="^checkout$"))
    app.add_handler(CallbackQueryHandler(confirm_order, pattern="^pay_"))

    print("🤖 Bot started successfully")
    app.run_polling()


if __name__ == "__main__":
    main()
