import os
import json
import logging
import asyncio
from datetime import datetime
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple, Union

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
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

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN not set")


def get_admin_ids() -> List[int]:
    ids: List[int] = []
    for key in ("ADMIN_ID", "ADMIN_ID1"):
        v = os.getenv(key)
        if v and v.isdigit():
            ids.append(int(v))
    if not ids:
        raise RuntimeError("ADMIN_ID variables not set correctly")
    return ids


ADMIN_IDS = get_admin_ids()

# chat_id курʼєрів / груп
COURIER_CHAT_IDS = {
    "Leipzig": 8401636475,
    "Dresden": 8501964969,
    "Berlin": 8449852526,
    "DEFAULT": 8449852526,
}

COURIERS = {
    "Dresden": "@dresden_fox",
    "Leipzig": "@leipzig_foxs",
    "DEFAULT": "@courier_fox",
}


def get_courier_chat_id(city: str) -> int:
    return COURIER_CHAT_IDS.get(city, COURIER_CHAT_IDS["DEFAULT"])


def get_courier_for_city(city: str) -> str:
    return COURIERS.get(city, COURIERS["DEFAULT"])


# ================== BEHAVIOR FLAGS ==================
NO_ITEM_PHOTO_CATS = {"pods", "liquids"}

USER_LOCKS: Dict[int, asyncio.Lock] = {}


def get_user_lock(user_id: int) -> asyncio.Lock:
    lock = USER_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        USER_LOCKS[user_id] = lock
    return lock


# ================== LOGGING ==================
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(message)s",
    level=logging.INFO,
)

# ================== LOAD CATALOG ==================
with open(CATALOG_PATH, "r", encoding="utf-8") as f:
    CATALOG: Dict[str, Any] = json.load(f)

CURRENCY = CATALOG.get("currency", "EUR")


# ================== STOCK CACHE (fast) ==================
STOCK_CACHE: Dict[str, Dict[str, Any]] = {}
STOCK_DIRTY = False


def load_stock_cache() -> None:
    global STOCK_CACHE
    if not STOCK_PATH.exists():
        STOCK_CACHE = {}
        return
    try:
        STOCK_CACHE = json.loads(STOCK_PATH.read_text(encoding="utf-8"))
        if not isinstance(STOCK_CACHE, dict):
            STOCK_CACHE = {}
    except Exception:
        STOCK_CACHE = {}


def save_stock_cache() -> None:
    global STOCK_DIRTY
    if not STOCK_DIRTY:
        return
    try:
        STOCK_PATH.write_text(json.dumps(STOCK_CACHE, ensure_ascii=False, indent=2), encoding="utf-8")
        STOCK_DIRTY = False
    except Exception as e:
        logging.exception("Failed to save stock.json: %s", e)


def stock_get(key: str) -> Dict[str, Any]:
    return STOCK_CACHE.get(key, {"in_stock": True, "eta": None})


def stock_set(key: str, in_stock: bool, eta: Optional[str] = None) -> None:
    global STOCK_DIRTY
    STOCK_CACHE[key] = {"in_stock": in_stock, "eta": eta}
    STOCK_DIRTY = True


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


def item_key(*parts: str) -> str:
    return ":".join(parts)


def _extract_flavor_name(fl: Any) -> str:
    """Підтримка смаків як рядків або як dict {name: ...}."""
    if isinstance(fl, str):
        return fl
    if isinstance(fl, dict):
        return str(fl.get("name") or fl.get("title") or fl)
    return str(fl)


def resolve_item_by_key(key: str) -> Tuple[str, Optional[float]]:
    """
    Повертає (title, price) або (key, None) якщо не знайшли.

    Формати ключів:
      - cat:<cat_key>:<idx>
      - brand:<cat_key>:<brand_key>:<idx>
      - nic:<cat_key>:<brand_key>:<block_idx>:<flavor_idx>
      - flv:<cat_key>:<brand_key>:<parent_idx>:<flavor_idx>    <-- NEW (смаки як підменю)
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

        # ===== NEW: flavors submenu =====
        if t == "flv":
            _, cat_key, brand_key, parent_idx, flavor_idx = parts
            brand = CATALOG["categories"][cat_key]["brands"][brand_key]
            parent = brand["items"][int(parent_idx)]
            flavors = parent.get("items", [])
            fl = flavors[int(flavor_idx)]
            fl_name = _extract_flavor_name(fl)
            base_name = parent.get("name", brand.get("title", ""))
            title = f"{base_name} — {fl_name}"
            return title.strip(), parent.get("price")

    except Exception:
        pass

    return key, None


# ================== CART HELPERS ==================
def get_cart(context: ContextTypes.DEFAULT_TYPE) -> List[Dict[str, Any]]:
    return context.user_data.setdefault("cart", [])


def cart_total(cart: List[Dict[str, Any]]) -> float:
    return round(sum(float(item["price"]) for item in cart), 2)


def get_username(user) -> str:
    return f"@{user.username}" if user.username else f"id:{user.id}"


# ================== PHOTO SENDER ==================
def resolve_local_photo_path(path: str) -> str:
    return str((BASE_DIR / path).resolve())


async def safe_send_photo(
    message_or_chat,
    path: Optional[str],
    caption: Optional[str] = None,
    reply_markup: Optional[InlineKeyboardMarkup] = None,
) -> bool:
    if not path:
        return False

    try:
        p = path.strip()

        # URL
        if p.startswith("http://") or p.startswith("https://"):
            if hasattr(message_or_chat, "reply_photo"):
                await message_or_chat.reply_photo(photo=p, caption=caption, reply_markup=reply_markup)
            else:
                await message_or_chat.send_photo(photo=p, caption=caption, reply_markup=reply_markup)
            return True

        # Local file
        abs_path = resolve_local_photo_path(p)
        if os.path.exists(abs_path):
            file = InputFile(abs_path)
            if hasattr(message_or_chat, "reply_photo"):
                await message_or_chat.reply_photo(photo=file, caption=caption, reply_markup=reply_markup)
            else:
                await message_or_chat.send_photo(photo=file, caption=caption, reply_markup=reply_markup)
            return True

        logging.warning("Photo not found: %s (resolved: %s)", p, abs_path)
        return False

    except Exception as e:
        logging.exception("Failed to send photo %s: %s", path, e)
        return False


# ================== UI BUILDERS ==================
def kb_main() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
    ])


def kb_after_add() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("🛒 Кошик", callback_data="cart")],
        [InlineKeyboardButton("⬅ На головну", callback_data="start")],
    ])


def fmt_price(p: Any) -> str:
    try:
        return f"{float(p):g} {CURRENCY}"
    except Exception:
        return f"{p} {CURRENCY}"


# ================== SMART EDIT / REPLY ==================
async def smart_edit_or_reply(q, text: str, reply_markup: Optional[InlineKeyboardMarkup] = None):
    """
    Гарантовано показує меню незалежно від того, під яким повідомленням натиснули кнопку:
    - якщо це текстове повідомлення -> edit_message_text
    - якщо це фото/інші типи -> reply_text (бо edit_message_text не можна)
    """
    try:
        await q.edit_message_text(text, reply_markup=reply_markup)
    except Exception:
        await q.message.reply_text(text, reply_markup=reply_markup)


# ================== START & CITY ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("📍 Берлін", callback_data="city:Berlin")],
        [InlineKeyboardButton("📍 Дрезден", callback_data="city:Dresden")],
        [InlineKeyboardButton("📍 Лейпциг", callback_data="city:Leipzig")],
        [InlineKeyboardButton("✍️ Інше місто", callback_data="city:OTHER")],
    ]
    text = "📍 Звідки ви?\nОберіть місто:"
    if update.message:
        await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard))
    else:
        await update.callback_query.edit_message_text(text, reply_markup=InlineKeyboardMarkup(keyboard))


async def city_callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    city = q.data.split(":", 1)[1]

    if city == "OTHER":
        context.user_data["awaiting_city"] = True
        await smart_edit_or_reply(q, "✍️ Напишіть, будь ласка, назву вашого міста:")
    else:
        context.user_data["city"] = city
        await show_main_menu(q, context)


async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    user_id = update.effective_user.id

    
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
        stock_set(key, in_stock=False, eta=eta)
        save_stock_cache()
        context.user_data.pop("awaiting_eta_key", None)

        title, price = resolve_item_by_key(key)
        extra = f" — {fmt_price(price)}" if price is not None else ""
        await update.message.reply_text(
            f"✅ Позначено як ❌ Нема в наявності.\nТовар: {title}{extra}\nОчікується з: {eta}"
        )
        return

    
    if context.user_data.get("reserve_key"):
        key = context.user_data["reserve_key"]
        context.user_data.pop("reserve_key", None)

        city = context.user_data.get("city", "Невідомо")
        st = stock_get(key)
        eta = st.get("eta")

        title, price = resolve_item_by_key(key)
        price_txt = fmt_price(price) if price is not None else "—"

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

   
    if context.user_data.get("awaiting_city"):
        context.user_data["city"] = text
        context.user_data.pop("awaiting_city", None)
        await show_main_menu(update, context)
        return


# ================== MAIN MENU ==================
async def show_main_menu(update_or_query, context: Optional[ContextTypes.DEFAULT_TYPE] = None):
    text = "Вітаю 👋\nОберіть дію:"
    if hasattr(update_or_query, "edit_message_text"):
        await smart_edit_or_reply(update_or_query, text, reply_markup=kb_main())
    elif hasattr(update_or_query, "message"):
        await update_or_query.message.reply_text(text, reply_markup=kb_main())


async def show_main_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await show_main_menu(q, context)


# ================== CATALOG ==================
async def catalog_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    keyboard = []
    for cat_key, cat_data in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(cat_data["title"], callback_data=f"category:{cat_key}")])
    keyboard.append([InlineKeyboardButton("⬅ На головну", callback_data="start")])

    await smart_edit_or_reply(q, "🛍 Каталог\nОберіть категорію:", reply_markup=InlineKeyboardMarkup(keyboard))


async def category_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cat_key = q.data.split(":", 1)[1]
    cat = CATALOG["categories"].get(cat_key)
    if not cat:
        await smart_edit_or_reply(q, "❌ Вибрана категорія не знайдена.")
        return

    
    await safe_send_photo(q.message, cat.get("photo"), caption=cat.get("title"))

    
    keyboard = []
    if "brands" in cat:
        for brand_key, brand in cat["brands"].items():
            label = brand.get("title", brand_key)
            keyboard.append([InlineKeyboardButton(label, callback_data=f"brand:{cat_key}:{brand_key}")])
    else:
        for idx, item in enumerate(cat.get("items", [])):
            key = item_key("cat", cat_key, str(idx))
            st = stock_get(key)
            if st.get("in_stock", True):
                label = f"{item['name']} — {fmt_price(item['price'])} ✅"
                cb = f"add:{cat_key}:{idx}"
            else:
                eta = st.get("eta")
                eta_txt = f" (з {eta})" if eta else ""
                label = f"{item['name']} — {fmt_price(item['price'])} ❌{eta_txt}"
                cb = f"reserve:{key}"
            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data="catalog")])
    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(keyboard))


async def brand_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key = q.data.split(":", 2)
    cat = CATALOG["categories"].get(cat_key)
    if not cat or "brands" not in cat:
        await smart_edit_or_reply(q, "❌ Бренд не знайдено.")
        return

    brand = cat["brands"].get(brand_key)
    if not brand:
        await smart_edit_or_reply(q, "❌ Бренд не знайдено.")
        return

    caption = brand.get("title", "")
    pr = brand.get("price_range")
    if pr:
        caption = f"{caption}\n💶 {pr}"

    await safe_send_photo(q.message, brand.get("photo"), caption=caption)

    items = brand.get("items", [])
    keyboard = []

    if items:
        first = items[0]

        # nicotine blocks
        if isinstance(first, dict) and "nicotine" in first and "items" in first:
            for idx, block in enumerate(items):
                label = f"{block.get('nicotine')} — {fmt_price(block.get('price'))}"
                keyboard.append([InlineKeyboardButton(label, callback_data=f"nic:{cat_key}:{brand_key}:{idx}")])

        # normal dict items (name/price)
        elif isinstance(first, dict) and "name" in first:
            for idx, it in enumerate(items):
               
                has_flavors = isinstance(it, dict) and isinstance(it.get("items"), list) and len(it.get("items")) > 0

                key_parent = item_key("brand", cat_key, brand_key, str(idx))
                st = stock_get(key_parent)

                if not st.get("in_stock", True):
                    eta = st.get("eta")
                    eta_txt = f" (з {eta})" if eta else ""
                    label = f"{it['name']} — {fmt_price(it['price'])} ❌{eta_txt}"
                    cb = f"reserve:{key_parent}"
                    keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
                    continue

                if has_flavors:
                    
                    label = f"{it['name']} — {fmt_price(it['price'])} ✅"
                    cb = f"flavors:{cat_key}:{brand_key}:{idx}"
                    keyboard.append([InlineKeyboardButton(label, callback_data=cb)])
                else:
                    label = f"{it['name']} — {fmt_price(it['price'])} ✅"
                    cb = f"addb:{cat_key}:{brand_key}:{idx}"
                    keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        else:
           
            for idx, name in enumerate(items):
                keyboard.append([InlineKeyboardButton(str(name), callback_data=f"addb:{cat_key}:{brand_key}:{idx}")])

    keyboard.append([InlineKeyboardButton("🛒 Кошик", callback_data="cart")])
    keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"category:{cat_key}")])
    await q.message.reply_text("Оберіть:", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== NEW: FLAVORS MENU ==================
async def flavors_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Відкриває список смаків для конкретного товару бренду:
    callback: flavors:<cat_key>:<brand_key>:<parent_idx>
    """
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key, parent_idx = q.data.split(":", 3)
    parent_idx_i = int(parent_idx)

    try:
        brand = CATALOG["categories"][cat_key]["brands"][brand_key]
        parent = brand["items"][parent_idx_i]

        flavors = parent.get("items", [])
        if not isinstance(flavors, list) or not flavors:
            await q.message.reply_text("❌ Для цього товару немає смаків.")
            return

        keyboard = []
        for fidx, fl in enumerate(flavors):
            fl_name = _extract_flavor_name(fl)

         
            key = item_key("flv", cat_key, brand_key, str(parent_idx_i), str(fidx))
            st = stock_get(key)

            if st.get("in_stock", True):
                label = f"{fl_name} ✅"
                cb = f"addf:{cat_key}:{brand_key}:{parent_idx_i}:{fidx}"
            else:
                eta = st.get("eta")
                eta_txt = f" (з {eta})" if eta else ""
                label = f"{fl_name} ❌{eta_txt}"
                cb = f"reserve:{key}"

            keyboard.append([InlineKeyboardButton(label, callback_data=cb)])

        keyboard.append([InlineKeyboardButton("⬅ Назад", callback_data=f"brand:{cat_key}:{brand_key}")])
        await q.message.reply_text("Оберіть смак:", reply_markup=InlineKeyboardMarkup(keyboard))

    except Exception as e:
        logging.exception("flavors_handler error: %s", e)
        await q.message.reply_text("❌ Сталась помилка при відкритті смаків.")


async def nicotine_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)
    brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    block = brand["items"][int(block_idx)]

    keyboard = []
    for idx, flavor in enumerate(block["items"]):
        key = item_key("nic", cat_key, brand_key, str(block_idx), str(idx))
        st = stock_get(key)

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

    key = q.data.split(":", 1)[1]
    st = stock_get(key)
    eta = st.get("eta")

    title, price = resolve_item_by_key(key)
    price_txt = fmt_price(price) if price is not None else "—"

    context.user_data["reserve_key"] = key
    eta_text = f"Очікується з: {eta}" if eta else "Очікується (дату уточнюйте)"

    await smart_edit_or_reply(
        q,
        f"📌 Бронювання\n\n"
        f"🧾 {title}\n"
        f"💶 {price_txt}\n"
        f"🗓 {eta_text}\n\n"
        "✍️ Напишіть контакт/коментар (телефон, месенджер, коли зручно):"
    )


# ================== ADD TO CART ==================
async def send_item_confirmation(q, item_name: str, price: float, photo: Optional[str], force_no_photo: bool):
    text = (
        "✅ Додано в кошик\n\n"
        f"🧾 {item_name}\n"
        f"💶 {fmt_price(price)}"
    )

    if force_no_photo:
        await q.message.reply_text(text, reply_markup=kb_after_add())
        return

    sent = await safe_send_photo(q.message, photo, caption=text, reply_markup=kb_after_add())
    if not sent:
        await q.message.reply_text(text, reply_markup=kb_after_add())


async def add_to_cart_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user_id = q.from_user.id
    lock = get_user_lock(user_id)

    if lock.locked():
        await q.answer("⏳ Зачекайте…", show_alert=False)

    async with lock:
        parts = q.data.split(":")
        cart = get_cart(context)

        item_name = ""
        price: Optional[float] = None
        photo: Optional[str] = None
        selected_cat_key: Optional[str] = None

        try:
            if parts[0] == "add":
                _, cat_key, idx = parts
                selected_cat_key = cat_key
                item = CATALOG["categories"][cat_key]["items"][int(idx)]
                item_name = item["name"]
                price = float(item["price"])
                photo = item.get("photo")
                cart.append({"name": item_name, "price": price})

            elif parts[0] == "addb":
                _, cat_key, brand_key, idx = parts
                selected_cat_key = cat_key
                item = CATALOG["categories"][cat_key]["brands"][brand_key]["items"][int(idx)]
                item_name = item["name"]
                price = float(item["price"])
                photo = item.get("photo") or CATALOG["categories"][cat_key]["brands"][brand_key].get("photo")
                cart.append({"name": item_name, "price": price})

            elif parts[0] == "addn":
                _, cat_key, brand_key, block_idx, flavor_idx = parts
                selected_cat_key = cat_key
                brand = CATALOG["categories"][cat_key]["brands"][brand_key]
                block = brand["items"][int(block_idx)]
                flavor = block["items"][int(flavor_idx)]
                price = float(block["price"])
                item_name = f"{brand.get('title','')} {block.get('nicotine')} — {flavor}".strip()
                photo = brand.get("photo")
                cart.append({"name": item_name, "price": price})

            # ===== NEW: add flavor from brand item =====
            elif parts[0] == "addf":
                _, cat_key, brand_key, parent_idx, flavor_idx = parts
                selected_cat_key = cat_key
                brand = CATALOG["categories"][cat_key]["brands"][brand_key]
                parent = brand["items"][int(parent_idx)]
                flavors = parent.get("items", [])
                fl = flavors[int(flavor_idx)]
                fl_name = _extract_flavor_name(fl)

                price = float(parent["price"])
                base_name = parent.get("name", brand.get("title", ""))
                item_name = f"{base_name} — {fl_name}".strip()

                
                photo = parent.get("photo") or brand.get("photo")
                cart.append({"name": item_name, "price": price})

            else:
                await q.message.reply_text("❌ Невідома дія.")
                return

        except Exception as e:
            logging.exception("add_to_cart_handler error: %s", e)
            await q.message.reply_text("❌ Сталась помилка. Спробуйте ще раз.")
            return

        force_no_photo = (selected_cat_key in NO_ITEM_PHOTO_CATS)
        await send_item_confirmation(
            q,
            item_name=item_name,
            price=float(price),
            photo=photo,
            force_no_photo=force_no_photo
        )


# ================== CART ==================
async def cart_view_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cart = get_cart(context)
    if not cart:
        await smart_edit_or_reply(
            q,
            "🛒 Кошик порожній.\n\nНатисніть «Каталог», щоб додати товари.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
                [InlineKeyboardButton("⬅ На головну", callback_data="start")],
            ])
        )
        return

    lines = [f"{i+1}. {item['name']} — {fmt_price(item['price'])}" for i, item in enumerate(cart)]
    text = "🛒 Ваше замовлення:\n\n" + "\n".join(lines)
    text += f"\n\n💰 Разом: {fmt_price(cart_total(cart))}"

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("➕ Додати ще", callback_data="catalog")],
        [InlineKeyboardButton("➖ Прибрати 1 товар", callback_data="remove_one")],
        [InlineKeyboardButton("✅ Оформити", callback_data="checkout")],
        [InlineKeyboardButton("⬅ На головну", callback_data="start")],
    ])

    await smart_edit_or_reply(q, text, reply_markup=kb)


async def remove_one_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    cart = get_cart(context)
    if not cart:
        await smart_edit_or_reply(q, "🛒 Кошик порожній.", reply_markup=kb_main())
        return

    removed = cart.pop()
    await q.message.reply_text(f"➖ Прибрано: {removed['name']}")
    await cart_view_handler(update, context)


# ================== CHECKOUT ==================
async def checkout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    user = q.from_user
    cart = get_cart(context)
    if not cart:
        await smart_edit_or_reply(q, "🛒 Кошик порожній.", reply_markup=kb_main())
        return

    city = context.user_data.get("city", "Невідомо")
    timestamp = datetime.now().strftime("%d.%m.%Y %H:%M")

    order_text = (
        "📦 НОВЕ ЗАМОВЛЕННЯ\n\n"
        f"👤 Клієнт: {get_username(user)}\n"
        f"ID: {user.id}\n"
        f"📍 Місто: {city}\n\n"
        "🛒 Товари:\n" +
        "\n".join(f"• {i['name']} — {fmt_price(i['price'])}" for i in cart) +
        f"\n\n💰 Разом: {fmt_price(cart_total(cart))}\n"
        f"🕒 {timestamp}"
    )

    for admin_id in ADMIN_IDS:
        try:
            await context.bot.send_message(chat_id=admin_id, text=order_text)
        except Exception as e:
            logging.exception("Failed to send order to admin %s: %s", admin_id, e)

    courier_chat_id = get_courier_chat_id(city)
    if courier_chat_id:
        try:
            await context.bot.send_message(chat_id=courier_chat_id, text=order_text)
        except Exception as e:
            logging.exception("Failed to send order to courier %s: %s", courier_chat_id, e)

    courier = get_courier_for_city(city)
    context.user_data.pop("cart", None)

    await smart_edit_or_reply(
        q,
        "✅ Дякуємо за замовлення!\n\n"
        "Курʼєр звʼяжеться з вами:\n"
        f"{courier}",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("🛍 Каталог", callback_data="catalog")],
            [InlineKeyboardButton("⬅ На головну", callback_data="start")],
        ])
    )


# ================== ADMIN (як було) ==================
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
        await smart_edit_or_reply(q, "Категорія не знайдена.")
        return

    if "brands" in cat:
        kb = []
        for brand_key, brand in cat["brands"].items():
            kb.append([InlineKeyboardButton(brand.get("title", brand_key), callback_data=f"admin_brand:{cat_key}:{brand_key}")])
        kb.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_back")])
        await smart_edit_or_reply(q, "Оберіть бренд:", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb = []
    lines = []
    for idx, it in enumerate(cat.get("items", [])):
        key = item_key("cat", cat_key, str(idx))
        st = stock_get(key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{idx+1}. {mark} {it['name']}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {it['name']}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data="admin_back")])
    await smart_edit_or_reply(q, "Керування наявністю:\n\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def admin_brand(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key = q.data.split(":", 2)
    brand = CATALOG["categories"][cat_key]["brands"].get(brand_key)
    if not brand:
        await smart_edit_or_reply(q, "Бренд не знайдено.")
        return

    items = brand.get("items", [])
    if not items:
        await smart_edit_or_reply(q, "Немає позицій у бренді.")
        return

    first = items[0]
    if isinstance(first, dict) and "nicotine" in first and "items" in first:
        kb = []
        for bidx, block in enumerate(items):
            kb.append([InlineKeyboardButton(
                f"{block.get('nicotine')} — {fmt_price(block.get('price'))}",
                callback_data=f"admin_block:{cat_key}:{brand_key}:{bidx}"
            )])
        kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])
        await smart_edit_or_reply(q, "Оберіть блок:", reply_markup=InlineKeyboardMarkup(kb))
        return

    kb = []
    lines = []
    for idx, it in enumerate(items):
        if not isinstance(it, dict) or "name" not in it:
            continue

        key = item_key("brand", cat_key, brand_key, str(idx))
        st = stock_get(key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{idx+1}. {mark} {it['name']}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {it['name']}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_cat:{cat_key}")])
    await smart_edit_or_reply(q, "Керування наявністю:\n\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def admin_block(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    _, cat_key, brand_key, block_idx = q.data.split(":", 3)
    brand = CATALOG["categories"][cat_key]["brands"][brand_key]
    block = brand["items"][int(block_idx)]

    kb = []
    lines = []
    for fidx, flavor in enumerate(block.get("items", [])):
        key = item_key("nic", cat_key, brand_key, str(block_idx), str(fidx))
        st = stock_get(key)
        mark = "✅" if st.get("in_stock", True) else "❌"
        eta = st.get("eta")
        eta_txt = f" (з {eta})" if (not st.get("in_stock", True) and eta) else ""
        lines.append(f"{fidx+1}. {mark} {flavor}{eta_txt}")
        kb.append([InlineKeyboardButton(f"{mark} {flavor}", callback_data=f"admin_toggle:{key}")])

    kb.append([InlineKeyboardButton("⬅ Назад", callback_data=f"admin_brand:{cat_key}:{brand_key}")])
    await smart_edit_or_reply(q, "Керування наявністю:\n\n" + "\n".join(lines), reply_markup=InlineKeyboardMarkup(kb))


async def admin_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    key = q.data.split(":", 1)[1]
    st = stock_get(key)

    if st.get("in_stock", True):
        context.user_data["awaiting_eta_key"] = key
        title, price = resolve_item_by_key(key)
        extra = f" — {fmt_price(price)}" if price is not None else ""
        await smart_edit_or_reply(
            q,
            f"❌ Ставимо 'нема в наявності'\n"
            f"Товар: {title}{extra}\n\n"
            "Вкажи дату надходження у форматі YYYY-MM-DD (наприклад 2026-01-20):"
        )
    else:
        stock_set(key, in_stock=True, eta=None)
        save_stock_cache()
        title, price = resolve_item_by_key(key)
        extra = f" — {fmt_price(price)}" if price is not None else ""
        await smart_edit_or_reply(q, f"✅ Тепер 'в наявності'\nТовар: {title}{extra}")


async def admin_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if not is_admin(q.from_user.id):
        return

    keyboard = []
    for cat_key, cat in CATALOG["categories"].items():
        keyboard.append([InlineKeyboardButton(f"⚙️ {cat.get('title', cat_key)}", callback_data=f"admin_cat:{cat_key}")])

    await smart_edit_or_reply(q, "🛠 Адмін-панель (наявність):", reply_markup=InlineKeyboardMarkup(keyboard))


# ================== ERROR HANDLER ==================
async def error_handler(update, context):
    logging.error("Exception in handler", exc_info=context.error)


# ================== MAIN ==================
def main():
    load_stock_cache()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    # Start / city
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(city_callback_handler, pattern="^city:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # Main menu & catalog
    app.add_handler(CallbackQueryHandler(show_main_menu_handler, pattern="^start$"))
    app.add_handler(CallbackQueryHandler(catalog_menu, pattern="^catalog$"))
    app.add_handler(CallbackQueryHandler(category_handler, pattern="^category:"))

    # Brands, nicotine, flavors and adding
    app.add_handler(CallbackQueryHandler(brand_handler, pattern="^brand:"))
    app.add_handler(CallbackQueryHandler(nicotine_handler, pattern="^nic:"))
    app.add_handler(CallbackQueryHandler(flavors_handler, pattern="^flavors:"))  # <-- NEW
    app.add_handler(CallbackQueryHandler(add_to_cart_handler, pattern="^(add:|addb:|addn:|addf:)"))  # <-- addf

    # Reserve
    app.add_handler(CallbackQueryHandler(reserve_handler, pattern="^reserve:"))

    # Cart / remove one / checkout
    app.add_handler(CallbackQueryHandler(cart_view_handler, pattern="^cart$"))
    app.add_handler(CallbackQueryHandler(remove_one_handler, pattern="^remove_one$"))
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
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
