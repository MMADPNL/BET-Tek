import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError, TelegramError
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# ============================================================
# CONFIG
# ============================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

OWNER_ID = 8935601841
OWNER_IDS = {OWNER_ID}

CHANNEL_USERNAME = "@BET_1XZX"
CHANNEL_URL = "https://t.me/BET_1XZX"

GROUP_USERNAME = "@GAP_BAZIN1"
GROUP_URL = "https://t.me/GAP_BAZIN1"

GAME_CHAT_USERNAME = "GAP_BAZIN1"

DB_FILE = "bot.db"

CURRENCY = "DOGS"

MIN_GAME_BET = Decimal("50")
MAX_GAME_BET = Decimal("3000")
MAX_GAME_COUNT = 3

REFERRAL_REWARD = Decimal("35")
MIN_WITHDRAWAL = Decimal("2500")

COMMISSION_RATE = Decimal("0.10")  # ۱۰٪

GAME_TIMEOUT_MINUTES = 30

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("BET_BT")

_background_tasks = set()


def spawn_background(coro):
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)
    return task


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = sqlite3.connect(DB_FILE, timeout=30, check_same_thread=False)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    return db


def init_db():
    with closing(get_db()) as db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                username TEXT DEFAULT '',
                first_name TEXT DEFAULT '',
                balance TEXT NOT NULL DEFAULT '0',
                referrer_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                amount TEXT NOT NULL,
                description TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_id INTEGER NOT NULL,
                referred_id INTEGER NOT NULL UNIQUE,
                reward TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER,
                game_type TEXT NOT NULL,
                count INTEGER NOT NULL,
                bet TEXT NOT NULL,
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                creator_total INTEGER DEFAULT 0,
                opponent_total INTEGER DEFAULT 0,
                creator_rolls INTEGER DEFAULT 0,
                opponent_rolls INTEGER DEFAULT 0,
                message_id INTEGER DEFAULT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMP DEFAULT NULL
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                wallet TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                processed_at TIMESTAMP DEFAULT NULL,
                processed_by INTEGER DEFAULT NULL
            )
        """)

        columns = {
            row["name"]
            for row in db.execute("PRAGMA table_info(games)").fetchall()
        }

        migrations = {
            "creator_rolls":
                "ALTER TABLE games ADD COLUMN creator_rolls INTEGER DEFAULT 0",
            "opponent_rolls":
                "ALTER TABLE games ADD COLUMN opponent_rolls INTEGER DEFAULT 0",
            "message_id":
                "ALTER TABLE games ADD COLUMN message_id INTEGER DEFAULT NULL",
            "finished_at":
                "ALTER TABLE games ADD COLUMN finished_at TIMESTAMP DEFAULT NULL",
        }

        for column, sql in migrations.items():
            if column not in columns:
                try:
                    db.execute(sql)
                except Exception:
                    logger.exception("Migration failed: %s", column)

        db.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES ('bot_enabled', '1')
        """)

        db.commit()


# ============================================================
# HELPERS
# ============================================================

def normalize_digits(text):
    if text is None:
        return ""
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def parse_amount(text):
    text = normalize_digits(str(text)).strip()
    text = text.replace("٫", ".").replace(",", ".")
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if value <= 0:
        return None
    return value


def money(value):
    value = Decimal(str(value))
    if value == value.to_integral():
        return str(int(value))
    return f"{value:.8f}".rstrip("0").rstrip(".")


def is_owner(user_id):
    try:
        return int(user_id) in OWNER_IDS
    except Exception:
        return False


def ensure_user(user):
    if not user:
        return
    with closing(get_db()) as db:
        db.execute("""
            INSERT OR IGNORE INTO users(
                user_id, username, first_name, balance
            )
            VALUES (?, ?, ?, '0')
        """, (user.id, user.username or "", user.first_name or ""))
        db.execute("""
            UPDATE users SET username = ?, first_name = ?
            WHERE user_id = ?
        """, (user.username or "", user.first_name or "", user.id))
        db.commit()


def ensure_user_id(user_id, first_name="", username=""):
    with closing(get_db()) as db:
        db.execute("""
            INSERT OR IGNORE INTO users(
                user_id, username, first_name, balance
            )
            VALUES (?, ?, ?, '0')
        """, (user_id, username or "", first_name or ""))
        db.commit()


def get_balance(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT balance FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if not row:
        return Decimal("0")
    try:
        return Decimal(row["balance"])
    except Exception:
        return Decimal("0")


def get_user_display_name(user_id):
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT first_name, username FROM users WHERE user_id = ?",
            (user_id,)
        ).fetchone()
    if row:
        if row["first_name"]:
            return row["first_name"]
        if row["username"]:
            return f"@{row['username']}"
    return f"کاربر {user_id}"


def user_tag(user_id):
    name = get_user_display_name(user_id)
    return f'<a href="tg://user?id={user_id}">{name}</a>'


# ============================================================
# ATOMIC BALANCE
# ============================================================

def change_balance(user_id, amount, transaction_type, description=""):
    amount = Decimal(str(amount))
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user_id,)
            ).fetchone()
            if not row:
                raise ValueError("user_not_found")
            current = Decimal(row["balance"])
            new_balance = current + amount
            if new_balance < 0:
                raise ValueError("insufficient_balance")
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), user_id)
            )
            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (user_id, transaction_type, str(amount), description))
            db.commit()
            return new_balance
        except Exception:
            db.rollback()
            raise


# ============================================================
# BOT STATUS
# ============================================================

def bot_enabled():
    with closing(get_db()) as db:
        row = db.execute(
            "SELECT value FROM settings WHERE key = 'bot_enabled'"
        ).fetchone()
    return bool(row and row["value"] == "1")


def set_bot_enabled(enabled):
    with closing(get_db()) as db:
        db.execute("""
            INSERT INTO settings(key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, ("1" if enabled else "0",))
        db.commit()


# ============================================================
# SAFE TELEGRAM
# ============================================================

async def safe_send_message(bot, chat_id, text, **kwargs):
    for attempt in range(6):
        try:
            return await bot.send_message(
                chat_id=chat_id, text=text, **kwargs
            )
        except (TimedOut, NetworkError) as e:
            logger.warning("Telegram network error: %s", e)
            if attempt < 5:
                await asyncio.sleep(0.35)
        except TelegramError as e:
            logger.error("Telegram error: %s", e)
            return None
        except Exception as e:
            logger.exception("Unexpected Telegram error: %s", e)
            return None
    return None


async def safe_delete_message(bot, chat_id, message_id):
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
        return True
    except Exception as e:
        logger.warning("Delete message failed: %s", e)
        return False


async def safe_send_dice(bot, chat_id, emoji):
    for attempt in range(6):
        try:
            return await bot.send_dice(chat_id=chat_id, emoji=emoji)
        except (TimedOut, NetworkError) as e:
            logger.warning("Dice network error: %s", e)
            if attempt < 5:
                await asyncio.sleep(0.35)
        except TelegramError as e:
            logger.error("Dice Telegram error: %s", e)
            return None
        except Exception as e:
            logger.exception("Dice error: %s", e)
            return None
    return None


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(user_id, context):
    allowed = {"member", "administrator", "creator"}
    try:
        channel_member = await context.bot.get_chat_member(
            CHANNEL_USERNAME, user_id
        )
        group_member = await context.bot.get_chat_member(
            GROUP_USERNAME, user_id
        )
        return (
            channel_member.status in allowed
            and group_member.status in allowed
        )
    except Exception as e:
        logger.warning("Membership error: %s", e)
        return False


async def require_membership(update, context):
    user = update.effective_user
    if not user:
        return False
    if await check_membership(user.id, context):
        return True

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 عضویت در کانال BET_1XZX", url=CHANNEL_URL)],
        [InlineKeyboardButton("💬 عضویت در گپ GAP_BAZIN1", url=GROUP_URL)],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")]
    ])

    if update.message:
        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔒 برای بازی باید عضو هر دو مورد باشید:\n"
            "📢 کانال: BET_1XZX\n"
            "💬 گپ: GAP_BAZIN1",
            reply_markup=keyboard
        )
    return False


async def membership_callback(update, context):
    query = update.callback_query
    try:
        await query.answer()
    except Exception:
        pass

    if await check_membership(query.from_user.id, context):
        try:
            await query.edit_message_text("✅ عضویت شما تأیید شد.")
        except Exception:
            pass
    else:
        try:
            await query.answer(
                "❌ هنوز عضو کانال و گپ نیستید.",
                show_alert=True
            )
        except Exception:
            pass


# ============================================================
# GAME CHAT CHECK
# ============================================================

def is_game_chat(chat):
    try:
        username = (chat.username or "").lower()
        return username == GAME_CHAT_USERNAME.lower()
    except Exception:
        return False


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 موجودی", callback_data="user_balance"),
            InlineKeyboardButton("👥 زیرمجموعه", callback_data="user_ref")
        ],
        [
            InlineKeyboardButton("🏧 برداشت DOGS", callback_data="user_withdraw"),
            InlineKeyboardButton("🆔 آیدی من", callback_data="user_myid")
        ],
        [
            InlineKeyboardButton("📚 راهنما", callback_data="user_help")
        ]
    ])


def result_pv_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("💰 موجودی", callback_data="user_balance"),
            InlineKeyboardButton("🏧 برداشت", callback_data="user_withdraw"),
        ],
        [
            InlineKeyboardButton("🎮 بازی جدید", url=GROUP_URL),
            InlineKeyboardButton("📚 راهنما", callback_data="user_help"),
        ],
    ])


def admin_keyboard():
    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(status, callback_data="admin_status")],
        [
            InlineKeyboardButton("🟢 روشن", callback_data="admin_on"),
            InlineKeyboardButton("🔴 خاموش", callback_data="admin_off")
        ],
        [
            InlineKeyboardButton("➕ شارژ", callback_data="admin_charge"),
            InlineKeyboardButton("➖ کسر", callback_data="admin_remove")
        ],
        [
            InlineKeyboardButton("📊 آمار", callback_data="admin_stats"),
            InlineKeyboardButton("🏧 برداشت‌ها", callback_data="admin_withdrawals")
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))
            except Exception:
                referrer_id = None

            if referrer_id and referrer_id != user.id:
                with closing(get_db()) as db:
                    row = db.execute(
                        "SELECT referrer_id FROM users WHERE user_id = ?",
                        (user.id,)
                    ).fetchone()

                    if row and row["referrer_id"] is None:
                        already = db.execute(
                            "SELECT id FROM referrals WHERE referred_id = ?",
                            (user.id,)
                        ).fetchone()

                        ref_exists = db.execute(
                            "SELECT user_id FROM users WHERE user_id = ?",
                            (referrer_id,)
                        ).fetchone()

                        if not already and ref_exists:
                            db.execute("""
                                UPDATE users
                                SET referrer_id = ?
                                WHERE user_id = ?
                            """, (referrer_id, user.id))

                            db.execute("""
                                INSERT INTO referrals(
                                    referrer_id, referred_id, reward
                                )
                                VALUES (?, ?, ?)
                            """, (
                                referrer_id,
                                user.id,
                                str(REFERRAL_REWARD)
                            ))
                            db.commit()

                            try:
                                change_balance(
                                    referrer_id,
                                    REFERRAL_REWARD,
                                    "referral",
                                    f"Referral {user.id}"
                                )
                            except Exception:
                                logger.exception("Referral reward failed")

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(get_balance(user.id))} {CURRENCY}\n"
        f"🆔 آیدی شما: <code>{user.id}</code>\n\n"
        "💰 موجودی (یا بنویس: م)\n"
        "👥 زیر مجموعه (پاداش 35 DOGS)\n"
        "🏧 برداشت (حداقل 2500 DOGS)\n"
        "🔄 انتقال در گپ\n\n"
        "🎮 بازی فقط در گپ:\n"
        f"{GROUP_URL}\n\n"
        "مثال: 3 تاس 50\n"
        f"• حداقل شرط: {money(MIN_GAME_BET)} {CURRENCY}\n"
        f"• حداکثر شرط: {money(MAX_GAME_BET)} {CURRENCY}\n"
        f"• حداکثر پرتاب: {MAX_GAME_COUNT}\n\n"
        "📌 نتیجه بازی در گپ ارسال می‌شود.",
        parse_mode="HTML",
        reply_markup=main_keyboard()
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    text = (
        "💰 موجودی شما\n\n"
        f"{money(get_balance(user.id))} {CURRENCY}"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_keyboard()
            )
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(
            text, reply_markup=main_keyboard()
        )


# ============================================================
# SHOW MY ID
# ============================================================

async def show_my_id(update, context):
    user = update.effective_user
    ensure_user(user)

    text = (
        "🆔 آیدی عددی شما\n\n"
        f"<code>{user.id}</code>\n\n"
        "این آیدی رو می‌تونی به دوستات بدی تا\n"
        "بدون ریپلای، برات DOGS بفرستن:\n"
        f"<code>انتقال 50 {user.id}</code>"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, parse_mode="HTML",
                reply_markup=main_keyboard()
            )
        except Exception:
            pass
    elif update.message:
        await update.message.reply_text(
            text, parse_mode="HTML",
            reply_markup=main_keyboard()
        )


# ============================================================
# REFERRAL
# ============================================================

async def show_referral(update, context):
    user = update.effective_user
    if not user:
        return
    ensure_user(user)

    try:
        bot = await context.bot.get_me()
        username = bot.username
    except Exception:
        username = None

    if username:
        link = f"https://t.me/{username}?start=ref_{user.id}"
    else:
        link = "لینک دعوت در دسترس نیست."

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT COUNT(*) AS total FROM referrals
            WHERE referrer_id = ?
        """, (user.id,)).fetchone()

    total = row["total"] if row else 0

    text = (
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {total}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} {CURRENCY}"
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_keyboard()
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(text)


# ============================================================
# HELP
# ============================================================

async def show_help(update, context):
    text = (
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "بنویس: موجودی یا م\n\n"
        "👥 زیرمجموعه\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} {CURRENCY}\n\n"
        "🏧 برداشت DOGS\n"
        f"حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n"
        "فرمت:\n"
        "برداشت <مبلغ> <آدرس ولت>\n"
        "مثال:\n"
        "برداشت 2500 UQxxxxxxxxxxxxxxxxxxxx\n\n"
        "🔄 انتقال در گپ:\n"
        "روش ۱ — ریپلای روی پیام کاربر:\n"
        "انتقال 50\n\n"
        "روش ۲ — با آیدی عددی:\n"
        "انتقال 50 123456789\n"
        "✅ حتی اگه طرف ربات رو استارت نکرده باشه\n\n"
        "🎮 بازی (فقط در گپ GAP_BAZIN1):\n"
        "فرمت: <تعداد> <نوع> <شرط>\n"
        "مثال: 3 تاس 50\n\n"
        f"• حداقل شرط: {money(MIN_GAME_BET)} {CURRENCY}\n"
        f"• حداکثر شرط: {money(MAX_GAME_BET)} {CURRENCY}\n"
        f"• حداکثر تعداد پرتاب: {MAX_GAME_COUNT}\n\n"
        "انواع بازی:\n"
        "🎲 تاس\n🎯 دارت\n🎳 بولینگ\n🏀 بسکتبال\n\n"
        "📌 نتیجه بازی در گپ ارسال می‌شود."
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_keyboard()
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            text, reply_markup=main_keyboard()
        )


# ============================================================
# TRANSFER (با ریپلای)
# ============================================================

async def transfer(update, context, amount):
    user = update.effective_user
    message = update.message

    if not message.reply_to_message:
        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\nانتقال 50"
        )
        return

    target = message.reply_to_message.from_user
    if not target:
        await message.reply_text("❌ گیرنده پیدا نشد.")
        return
    if target.id == user.id:
        await message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return
    if target.is_bot:
        await message.reply_text("❌ انتقال به ربات امکان‌پذیر نیست.")
        return

    ensure_user(target)
    error_code = None

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")
            sender = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user.id,)
            ).fetchone()
            receiver = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (target.id,)
            ).fetchone()

            if not sender or not receiver:
                raise ValueError("user_not_found")

            sender_balance = Decimal(sender["balance"])
            receiver_balance = Decimal(receiver["balance"])

            if sender_balance < amount:
                raise ValueError("insufficient_balance")

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_sender), user.id)
            )
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_receiver), target.id)
            )
            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (user.id, "transfer_out",
                  str(-amount), f"To {target.id}"))
            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (target.id, "transfer_in",
                  str(amount), f"From {user.id}"))
            db.commit()
        except ValueError as e:
            db.rollback()
            error_code = str(e)
        except Exception:
            db.rollback()
            logger.exception("Transfer failed")
            error_code = "internal"

    if error_code == "insufficient_balance":
        await message.reply_text("❌ موجودی شما کافی نیست.")
        return
    if error_code == "user_not_found":
        await message.reply_text("❌ کاربر پیدا نشد.")
        return
    if error_code:
        await message.reply_text("❌ انتقال انجام نشد.")
        return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} {CURRENCY}\n"
        f"👤 گیرنده: {target.first_name or target.id}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} {CURRENCY}"
    )


# ============================================================
# TRANSFER BY ID (حتی اگه طرف ربات رو استارت نکرده)
# ============================================================

async def transfer_by_id(update, context, amount, target_id):
    user = update.effective_user
    message = update.message

    if target_id == user.id:
        await message.reply_text("❌ نمی‌توانید به خودتان انتقال دهید.")
        return

    if target_id <= 0:
        await message.reply_text("❌ آیدی عددی صحیح نیست.")
        return

    ensure_user_id(target_id)

    error_code = None

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            sender = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user.id,)
            ).fetchone()

            receiver = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (target_id,)
            ).fetchone()

            if not sender or not receiver:
                raise ValueError("user_not_found")

            sender_balance = Decimal(sender["balance"])
            receiver_balance = Decimal(receiver["balance"])

            if sender_balance < amount:
                raise ValueError("insufficient_balance")

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_sender), user.id)
            )
            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_receiver), target_id)
            )

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (user.id, "transfer_out",
                  str(-amount), f"To {target_id}"))

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (target_id, "transfer_in",
                  str(amount), f"From {user.id}"))

            db.commit()
        except ValueError as e:
            db.rollback()
            error_code = str(e)
        except Exception:
            db.rollback()
            logger.exception("Transfer by ID failed")
            error_code = "internal"

    if error_code == "insufficient_balance":
        await message.reply_text("❌ موجودی شما کافی نیست.")
        return
    if error_code == "user_not_found":
        await message.reply_text("❌ کاربر پیدا نشد.")
        return
    if error_code:
        await message.reply_text("❌ انتقال انجام نشد.")
        return

    target_name = get_user_display_name(target_id)

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} {CURRENCY}\n"
        f"🆔 گیرنده: {target_id}\n"
        f"👤 نام: {target_name}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} {CURRENCY}"
    )

    try:
        await safe_send_message(
            context.bot,
            target_id,
            "💸 انتقال دریافت کردید!\n\n"
            f"👤 از: {user.first_name or user.id}\n"
            f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
            f"💰 موجودی: "
            f"{money(get_balance(target_id))} {CURRENCY}",
            reply_markup=main_keyboard()
        )
    except Exception:
        logger.warning(
            "Could not notify receiver %s (PV not started)",
            target_id
        )


# ============================================================
# WITHDRAW
# ============================================================

async def show_withdraw_info(update, context):
    user = update.effective_user
    ensure_user(user)

    text = (
        "🏧 برداشت DOGS\n\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} {CURRENCY}\n"
        f"📉 حداقل برداشت: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n"
        "📝 فرمت:\n"
        "برداشت <مبلغ> <آدرس ولت>\n\n"
        "مثال:\n"
        "برداشت 2500 UQxxxxxxxxxxxxxxxxxxxx\n\n"
        "⏳ پس از ثبت، درخواست برای تأیید به مدیریت ارسال می‌شود."
    )

    if update.callback_query:
        try:
            await update.callback_query.edit_message_text(
                text, reply_markup=main_keyboard()
            )
        except Exception:
            pass
    else:
        await update.message.reply_text(
            text, reply_markup=main_keyboard()
        )


async def process_withdrawal(update, context, amount, wallet):
    user = update.effective_user
    message = update.message

    ensure_user(user)

    if amount is None or amount <= 0:
        await message.reply_text("❌ مبلغ صحیح نیست.")
        return

    if amount < MIN_WITHDRAWAL:
        await message.reply_text(
            f"❌ حداقل برداشت {money(MIN_WITHDRAWAL)} {CURRENCY} است."
        )
        return

    wallet = wallet.strip()

    if len(wallet) < 20 or len(wallet) > 128:
        await message.reply_text(
            "❌ آدرس ولت باید بین ۲۰ تا ۱۲۸ کاراکتر باشد."
        )
        return

    if not re.match(r"^[A-Za-z0-9_\-:]+$", wallet):
        await message.reply_text("❌ آدرس ولت معتبر نیست.")
        return

    with closing(get_db()) as db:
        pending = db.execute("""
            SELECT id FROM withdrawals
            WHERE user_id = ? AND status = 'pending'
            LIMIT 1
        """, (user.id,)).fetchone()

    if pending:
        await message.reply_text(
            "❌ شما یک درخواست برداشت در انتظار دارید.\n"
            "لطفاً تا بررسی آن صبر کنید."
        )
        return

    balance = get_balance(user.id)

    if balance < amount:
        await message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(balance)} {CURRENCY}\n"
            f"💸 درخواست: {money(amount)} {CURRENCY}"
        )
        return

    try:
        change_balance(
            user.id, -amount, "withdraw",
            f"Withdraw {wallet[:20]}"
        )
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await message.reply_text("❌ موجودی کافی نیست.")
        else:
            await message.reply_text("❌ برداشت انجام نشد.")
        return
    except Exception:
        logger.exception("Withdraw deduction failed")
        await message.reply_text("❌ خطا در برداشت.")
        return

    try:
        with closing(get_db()) as db:
            cursor = db.execute("""
                INSERT INTO withdrawals(user_id, amount, wallet)
                VALUES (?, ?, ?)
            """, (user.id, str(amount), wallet))
            wid = cursor.lastrowid
            db.commit()
    except Exception:
        logger.exception("Withdraw insert failed")
        try:
            change_balance(
                user.id, amount,
                "withdraw_rollback",
                "Insert failed"
            )
        except Exception:
            logger.exception("Withdraw rollback failed")
        await message.reply_text("❌ برداشت ثبت نشد.")
        return

    short_wallet = (
        wallet if len(wallet) <= 40
        else wallet[:20] + "..." + wallet[-15:]
    )

    for owner in OWNER_IDS:
        try:
            await safe_send_message(
                context.bot, owner,
                "🏧 درخواست برداشت جدید\n\n"
                f"🆔 شماره: {wid}\n"
                f"👤 نام: {user.first_name or '-'}\n"
                f"🔗 یوزر: @{user.username or '-'}\n"
                f"🆔 آیدی: {user.id}\n"
                f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
                f"📮 ولت:\n{short_wallet}",
                reply_markup=InlineKeyboardMarkup([
                    [
                        InlineKeyboardButton(
                            "✅ تأیید",
                            callback_data=f"wd_ok_{wid}"
                        ),
                        InlineKeyboardButton(
                            "❌ رد",
                            callback_data=f"wd_no_{wid}"
                        ),
                    ]
                ])
            )
        except Exception:
            logger.exception("Notify owner failed")

    await message.reply_text(
        "✅ درخواست برداشت ثبت شد.\n\n"
        f"🆔 شماره: {wid}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"📮 ولت: {short_wallet}\n\n"
        "⏳ در انتظار تأیید مدیریت."
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text("❌ شما مالک ربات نیستید.")
        return

    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"

    await update.message.reply_text(
        f"👑 پنل مدیریت BET_BT\n\n"
        f"وضعیت: {status}\n\n"
        "➕ شارژ از گپ:\n"
        "روی پیام کاربر Reply کنید:\n"
        "شارژ 100\n\n"
        "➖ کسر از گپ:\n"
        "روی پیام کاربر Reply کنید:\n"
        "کسر 100\n\n"
        "یا از دکمه‌ها استفاده کنید.",
        reply_markup=admin_keyboard()
    )


async def ask_admin_balance(update, context, operation):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    context.user_data["admin_operation"] = operation
    context.user_data["admin_waiting"] = True

    title = "➕ شارژ موجودی" if operation == "charge" else "➖ کسر موجودی"

    try:
        await query.answer()
    except Exception:
        pass

    await safe_send_message(
        context.bot,
        query.message.chat_id,
        f"{title}\n\n"
        "فرمت:\n"
        "آیدی عددی مبلغ\n\n"
        "مثال:\n"
        "8935601841 100\n\n"
        "برای لغو:\nلغو"
    )


async def admin_balance_by_id(update, context):
    user = update.effective_user

    if not user or not is_owner(user.id):
        return False

    operation = context.user_data.get("admin_operation")
    waiting = context.user_data.get("admin_waiting")

    if not operation or not waiting:
        return False

    text = normalize_digits(update.message.text.strip())

    if text == "لغو":
        context.user_data.pop("admin_operation", None)
        context.user_data.pop("admin_waiting", None)
        await update.message.reply_text("❌ عملیات لغو شد.")
        return True

    match = re.match(r"^(\d+)\s+([0-9]+(?:\.[0-9]+)?)$", text)

    if not match:
        await update.message.reply_text(
            "❌ فرمت اشتباه.\n\nمثال:\n8935601841 100"
        )
        return True

    target_id = int(match.group(1))
    amount = parse_amount(match.group(2))

    if amount is None:
        await update.message.reply_text("❌ مبلغ صحیح نیست.")
        return True

    if target_id <= 0:
        await update.message.reply_text("❌ آیدی کاربر صحیح نیست.")
        return True

    ensure_user_id(target_id)

    context.user_data.pop("admin_operation", None)
    context.user_data.pop("admin_waiting", None)

    try:
        if operation == "charge":
            new_balance = change_balance(
                target_id, amount, "admin_charge", f"Owner {user.id}"
            )
            title = "شارژ"
        else:
            new_balance = change_balance(
                target_id, -amount, "admin_remove", f"Owner {user.id}"
            )
            title = "کسر"
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await update.message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )
        else:
            await update.message.reply_text("❌ عملیات انجام نشد.")
        return True
    except Exception:
        logger.exception("Admin balance by ID failed")
        await update.message.reply_text("❌ خطا در تغییر موجودی.")
        return True

    await update.message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"💳 موجودی جدید: {money(new_balance)} {CURRENCY}"
    )

    return True


async def admin_change_by_reply(update, context, operation, amount):
    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return

    if not message.reply_to_message:
        await message.reply_text("❌ باید روی پیام کاربر Reply کنید.")
        return

    target = message.reply_to_message.from_user

    if not target:
        await message.reply_text("❌ کاربر پیدا نشد.")
        return

    if target.is_bot:
        await message.reply_text("❌ نمی‌توان موجودی ربات را تغییر داد.")
        return

    ensure_user(target)

    try:
        if operation == "charge":
            new_balance = change_balance(
                target.id, amount, "admin_charge", f"Owner {user.id}"
            )
            title = "شارژ"
        else:
            new_balance = change_balance(
                target.id, -amount, "admin_remove", f"Owner {user.id}"
            )
            title = "کسر"
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )
        else:
            await message.reply_text("❌ عملیات انجام نشد.")
        return
    except Exception:
        logger.exception("Admin reply balance failed")
        await message.reply_text("❌ عملیات انجام نشد.")
        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: {target.first_name or target.id}\n"
        f"🆔 آیدی: {target.id}\n"
        f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
        f"💳 موجودی جدید: {money(new_balance)} {CURRENCY}"
    )


async def admin_list_withdrawals(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT * FROM withdrawals
            WHERE status = 'pending'
            ORDER BY id ASC
            LIMIT 10
        """).fetchall()

    try:
        await query.answer()
    except Exception:
        pass

    if not rows:
        try:
            await query.edit_message_text(
                "🏧 برداشت‌های در انتظار\n\n"
                "✅ هیچ درخواست در انتظاری نیست.",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass
        return

    lines = ["🏧 برداشت‌های در انتظار:\n"]

    buttons = []
    for wd in rows:
        wd_id = wd["id"]
        wd_uid = wd["user_id"]
        amount = wd["amount"]
        wallet = wd["wallet"]
        short_wallet = (
            wallet if len(wallet) <= 30
            else wallet[:15] + "..." + wallet[-10:]
        )

        lines.append(
            f"🆔 {wd_id} | 👤 {wd_uid}\n"
            f"💰 {amount} {CURRENCY}\n"
            f"📮 {short_wallet}\n"
        )

        buttons.append([
            InlineKeyboardButton(
                f"✅ تأیید #{wd_id}", callback_data=f"wd_ok_{wd_id}"
            ),
            InlineKeyboardButton(
                f"❌ رد #{wd_id}", callback_data=f"wd_no_{wd_id}"
            ),
        ])

    buttons.append([
        InlineKeyboardButton("🔙 بازگشت", callback_data="admin_back")
    ])

    text = "\n".join(lines)

    try:
        await query.edit_message_text(
            text,
            reply_markup=InlineKeyboardMarkup(buttons)
        )
    except Exception:
        pass


async def withdraw_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    try:
        parts = query.data.split("_")
        action = parts[1]
        wd_id = int(parts[2])
    except Exception:
        try:
            await query.answer("❌ داده نامعتبر.", show_alert=True)
        except Exception:
            pass
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            wd = db.execute("""
                SELECT * FROM withdrawals WHERE id = ?
            """, (wd_id,)).fetchone()

            if not wd or wd["status"] != "pending":
                db.rollback()
                try:
                    await query.answer(
                        "❌ این درخواست در دسترس نیست.",
                        show_alert=True
                    )
                except Exception:
                    pass
                return

            amount = Decimal(wd["amount"])
            wd_user_id = int(wd["user_id"])

            if action == "ok":
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (wd_user_id,)
                ).fetchone()

                if not row:
                    db.rollback()
                    try:
                        await query.answer(
                            "❌ کاربر پیدا نشد.", show_alert=True
                        )
                    except Exception:
                        pass
                    return

                db.execute("""
                    UPDATE withdrawals
                    SET status = 'approved',
                        processed_at = CURRENT_TIMESTAMP,
                        processed_by = ?
                    WHERE id = ?
                """, (user.id, wd_id))

                db.commit()

                try:
                    await query.answer("✅ تأیید شد.")
                except Exception:
                    pass

                await safe_send_message(
                    context.bot, wd_user_id,
                    f"✅ درخواست برداشت شما تأیید شد.\n\n"
                    f"🆔 شماره: {wd_id}\n"
                    f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
                    "📮 مبلغ به آدرس ثبت‌شده ارسال می‌شود."
                )
            else:
                db.execute("""
                    UPDATE withdrawals
                    SET status = 'rejected',
                        processed_at = CURRENT_TIMESTAMP,
                        processed_by = ?
                    WHERE id = ?
                """, (user.id, wd_id))

                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (wd_user_id,)
                ).fetchone()

                if row:
                    current = Decimal(row["balance"])
                    new_balance = current + amount

                    db.execute(
                        "UPDATE users SET balance = ? WHERE user_id = ?",
                        (str(new_balance), wd_user_id)
                    )

                    db.execute("""
                        INSERT INTO transactions(
                            user_id, type, amount, description
                        )
                        VALUES (?, ?, ?, ?)
                    """, (
                        wd_user_id,
                        "withdraw_rejected_refund",
                        str(amount),
                        f"Withdraw rejected {wd_id}"
                    ))

                db.commit()

                try:
                    await query.answer("❌ رد شد.")
                except Exception:
                    pass

                await safe_send_message(
                    context.bot, wd_user_id,
                    f"❌ درخواست برداشت شما رد شد.\n\n"
                    f"🆔 شماره: {wd_id}\n"
                    f"💰 مبلغ: {money(amount)} {CURRENCY}\n"
                    "💳 مبلغ به موجودی شما برگشت داده شد."
                )

        except Exception:
            db.rollback()
            logger.exception("Withdraw callback failed")
            try:
                await query.answer(
                    "❌ خطا در پردازش.", show_alert=True
                )
            except Exception:
                pass


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        try:
            await query.answer("❌ دسترسی ندارید.", show_alert=True)
        except Exception:
            pass
        return

    data = query.data

    if data == "admin_status":
        try:
            await query.answer(
                "وضعیت: " + ("روشن" if bot_enabled() else "خاموش")
            )
        except Exception:
            pass
        return

    if data == "admin_on":
        set_bot_enabled(True)
        try:
            await query.answer("🟢 ربات روشن شد.")
        except Exception:
            pass
        try:
            await query.edit_message_text(
                "👑 پنل مدیریت\n\n🟢 ربات روشن است.",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass
        return

    if data == "admin_off":
        set_bot_enabled(False)
        try:
            await query.answer("🔴 ربات خاموش شد.")
        except Exception:
            pass
        try:
            await query.edit_message_text(
                "👑 پنل مدیریت\n\n🔴 ربات خاموش است.",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass
        return

    if data == "admin_charge":
        await ask_admin_balance(update, context, "charge")
        return

    if data == "admin_remove":
        await ask_admin_balance(update, context, "remove")
        return

    if data == "admin_withdrawals":
        await admin_list_withdrawals(update, context)
        return

    if data == "admin_back":
        try:
            await query.edit_message_text(
                "👑 پنل مدیریت BET_BT",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass
        return

    if data == "admin_stats":
        with closing(get_db()) as db:
            users = db.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            total_row = db.execute("""
                SELECT COALESCE(SUM(CAST(balance AS REAL)), 0) AS total
                FROM users
            """).fetchone()

            referrals = db.execute(
                "SELECT COUNT(*) AS c FROM referrals"
            ).fetchone()["c"]

            games = db.execute(
                "SELECT COUNT(*) AS c FROM games"
            ).fetchone()["c"]

            pending_wd = db.execute("""
                SELECT COUNT(*) AS c FROM withdrawals
                WHERE status = 'pending'
            """).fetchone()["c"]

        total = float(total_row["total"] or 0)

        try:
            await query.answer()
        except Exception:
            pass

        try:
            await query.edit_message_text(
                "📊 آمار BET_BT\n\n"
                f"👤 کاربران: {users}\n"
                f"💰 مجموع موجودی: {total:.2f} {CURRENCY}\n"
                f"👥 زیرمجموعه‌ها: {referrals}\n"
                f"🎮 بازی‌ها: {games}\n"
                f"🏧 برداشت در انتظار: {pending_wd}\n\n"
                f"وضعیت: "
                f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass


# ============================================================
# USER CALLBACK
# ============================================================

async def user_callback(update, context):
    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    if query.data == "user_balance":
        await show_balance(update, context)
    elif query.data == "user_ref":
        await show_referral(update, context)
    elif query.data == "user_help":
        await show_help(update, context)
    elif query.data == "user_withdraw":
        await show_withdraw_info(update, context)
    elif query.data == "user_myid":
        await show_my_id(update, context)


# ============================================================
# GAME CONFIG
# ============================================================

GAME_INFO = {
    "dice": ("🎲", "تاس"),
    "darts": ("🎯", "دارت"),
    "bowling": ("🎳", "بولینگ"),
    "basketball": ("🏀", "بسکتبال"),
}

GAME_NAMES = {
    "تاس": "dice", "دارت": "darts", "دارتس": "darts",
    "بولینگ": "bowling", "بولينگ": "bowling",
    "بسکتبال": "basketball",
    "dice": "dice", "darts": "darts",
    "bowling": "bowling", "basketball": "basketball",
}

GAME_EMOJI = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


def parse_game(text):
    text = normalize_digits(text.strip())

    pattern = (
        r"^(\d+)\s+"
        r"([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$"
    )

    match = re.match(pattern, text)

    if not match:
        return None

    count = int(match.group(1))
    name = match.group(2).lower()
    bet = parse_amount(match.group(3))

    if count < 1:
        return None

    if MAX_GAME_COUNT is not None and count > MAX_GAME_COUNT:
        return "count_too_high"

    if bet is None:
        return None

    if name not in GAME_NAMES:
        return None

    return (count, GAME_NAMES[name], bet)


def get_active_game_for_user(chat_id, user_id):
    with closing(get_db()) as db:
        row = db.execute("""
            SELECT * FROM games
            WHERE chat_id = ?
              AND status IN ('waiting', 'creator_turn', 'opponent_turn')
              AND (creator_id = ? OR opponent_id = ?)
            ORDER BY id DESC
            LIMIT 1
        """, (chat_id, user_id, user_id)).fetchone()
    return row


def get_game(game_id):
    with closing(get_db()) as db:
        return db.execute(
            "SELECT * FROM games WHERE id = ?", (game_id,)
        ).fetchone()


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, count, game_type, bet):
    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if not await require_membership(update, context):
        return

    if not bot_enabled():
        await update.message.reply_text("🔴 ربات خاموش است.")
        return

    if bet < MIN_GAME_BET:
        await update.message.reply_text(
            f"❌ حداقل شرط {money(MIN_GAME_BET)} {CURRENCY} است."
        )
        return

    if bet > MAX_GAME_BET:
        await update.message.reply_text(
            f"❌ حداکثر شرط {money(MAX_GAME_BET)} {CURRENCY} است."
        )
        return

    active = get_active_game_for_user(chat.id, user.id)

    if active:
        await update.message.reply_text(
            "❌ شما در حال حاضر یک بازی فعال دارید.\n"
            "ابتدا آن بازی را تمام یا ریست کنید."
        )
        return

    try:
        change_balance(
            user.id, -bet, "game_bet", f"Game {game_type}"
        )
    except ValueError as e:
        if str(e) == "insufficient_balance":
            await update.message.reply_text(
                "❌ موجودی شما کافی نیست.\n\n"
                f"💰 موجودی: {money(get_balance(user.id))} {CURRENCY}"
            )
        else:
            await update.message.reply_text("❌ ساخت بازی انجام نشد.")
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            cursor = db.execute("""
                INSERT INTO games(
                    chat_id, creator_id, game_type, count, bet,
                    mode, status,
                    creator_total, opponent_total,
                    creator_rolls, opponent_rolls
                )
                VALUES (?, ?, ?, ?, ?, 'friends', 'waiting', 0, 0, 0, 0)
            """, (chat.id, user.id, game_type, count, str(bet)))

            game_id = cursor.lastrowid
            db.commit()
        except Exception:
            db.rollback()
            try:
                change_balance(
                    user.id, bet,
                    "game_create_rollback",
                    f"Game create rollback {game_type}"
                )
            except Exception:
                logger.exception("Game creation rollback failed")
            await update.message.reply_text("❌ ساخت بازی انجام نشد.")
            return

    emoji, name = GAME_INFO[game_type]

    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "👥 بازی با دوستان", callback_data=f"join_{game_id}"
        )],
        [InlineKeyboardButton(
            "🤖 بازی با ربات", callback_data=f"bot_{game_id}"
        )],
        [InlineKeyboardButton(
            "❌ لغو بازی", callback_data=f"cancel_{game_id}"
        )]
    ])

    sent = await update.message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} {CURRENCY}\n"
        f"👤 سازنده: {user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not await check_membership(user.id, context):
        try:
            await query.answer(
                "❌ ابتدا عضو کانال و گپ شوید.",
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        game_id = int(query.data.replace("join_", ""))
    except Exception:
        return

    ensure_user(user)

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return

            if game["status"] != "waiting":
                db.rollback()
                await query.answer(
                    "❌ این بازی دیگر فعال نیست.", show_alert=True
                )
                return

            if game["creator_id"] == user.id:
                db.rollback()
                await query.answer(
                    "❌ نمی‌توانید با خودتان بازی کنید.",
                    show_alert=True
                )
                return

            bet = Decimal(game["bet"])

            balance_row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (user.id,)
            ).fetchone()

            if not balance_row:
                db.rollback()
                await query.answer(
                    "❌ کاربر پیدا نشد.", show_alert=True
                )
                return

            current = Decimal(balance_row["balance"])

            if current < bet:
                db.rollback()
                await query.answer(
                    "❌ موجودی شما کافی نیست.", show_alert=True
                )
                return

            new_balance = current - bet

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), user.id)
            )

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (user.id, "game_bet", str(-bet), f"Join game {game_id}"))

            db.execute("""
                UPDATE games
                SET opponent_id = ?,
                    mode = 'friends',
                    status = 'creator_turn',
                    creator_rolls = 0,
                    opponent_rolls = 0,
                    creator_total = 0,
                    opponent_total = 0
                WHERE id = ?
            """, (user.id, game_id))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Join game failed")
            await query.answer(
                "❌ پیوستن به بازی انجام نشد.", show_alert=True
            )
            return

    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    emoji, name = GAME_INFO[game["game_type"]]

    creator_tag = user_tag(game["creator_id"])
    opponent_tag = user_tag(user.id)

    sent = await safe_send_message(
        context.bot,
        query.message.chat_id,
        "🎮 بازی دوستان شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد پرتاب: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} {CURRENCY}\n\n"
        f"👤 {creator_tag}\n"
        f"👤 {opponent_tag}\n\n"
        f"🎯 نوبت {creator_tag} است.\n"
        f"{emoji} را {game['count']} بار بفرستید.\n\n"
        "⚠️ فقط پرتاب‌های خود بازیکن حساب می‌شود.",
        parse_mode="HTML"
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


# ============================================================
# BOT GAME
# ============================================================

async def bot_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not await check_membership(user.id, context):
        try:
            await query.answer(
                "❌ ابتدا عضو کانال و گپ شوید.",
                show_alert=True
            )
        except Exception:
            pass
        return

    try:
        game_id = int(query.data.replace("bot_", ""))
    except Exception:
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return

            if game["status"] != "waiting":
                db.rollback()
                await query.answer("❌ بازی فعال نیست.", show_alert=True)
                return

            if game["creator_id"] != user.id:
                db.rollback()
                await query.answer(
                    "❌ فقط سازنده می‌تواند.", show_alert=True
                )
                return

            db.execute("""
                UPDATE games
                SET opponent_id = -1,
                    mode = 'bot',
                    status = 'creator_turn',
                    creator_rolls = 0,
                    opponent_rolls = 0,
                    creator_total = 0,
                    opponent_total = 0
                WHERE id = ?
            """, (game_id,))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Bot game start failed")
            return

    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    emoji, name = GAME_INFO[game["game_type"]]

    player_tag = user_tag(user.id)

    sent = await safe_send_message(
        context.bot,
        query.message.chat_id,
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد پرتاب: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} {CURRENCY}\n\n"
        f"🎯 نوبت {player_tag} است.\n\n"
        f"{emoji} را {game['count']} بار بفرستید.\n"
        "بعد از تمام شدن پرتاب‌های شما، ربات خودش بازی می‌کند.",
        parse_mode="HTML"
    )

    if sent:
        with closing(get_db()) as db:
            db.execute(
                "UPDATE games SET message_id = ? WHERE id = ?",
                (sent.message_id, game_id)
            )
            db.commit()


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game_callback(update, context):
    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.replace("cancel_", ""))
    except Exception:
        return

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                await query.answer("❌ بازی پیدا نشد.", show_alert=True)
                return

            if game["status"] != "waiting":
                db.rollback()
                await query.answer(
                    "❌ بازی دیگر قابل لغو نیست.", show_alert=True
                )
                return

            if game["creator_id"] != user.id and not is_owner(user.id):
                db.rollback()
                await query.answer(
                    "❌ فقط سازنده یا مالک.", show_alert=True
                )
                return

            bet = Decimal(game["bet"])

            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (game["creator_id"],)
            ).fetchone()

            current = Decimal(row["balance"])
            new_balance = current + bet

            db.execute(
                "UPDATE users SET balance = ? WHERE user_id = ?",
                (str(new_balance), game["creator_id"])
            )

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, ?, ?, ?)
            """, (game["creator_id"], "game_refund",
                  str(bet), f"Cancel game {game_id}"))

            db.execute("""
                UPDATE games
                SET status = 'cancelled',
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (game_id,))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Cancel failed")
            await query.answer(
                "❌ لغو انجام نشد.", show_alert=True
            )
            return

    try:
        await query.answer("بازی لغو شد.")
    except Exception:
        pass

    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    await safe_send_message(
        context.bot,
        query.message.chat_id,
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(bet)} {CURRENCY} به سازنده برگشت داده شد."
    )


# ============================================================
# USER DICE HANDLER
# ============================================================

def dice_matches_game(dice_emoji, game_type):
    return dice_emoji == GAME_EMOJI[game_type]


async def user_game_dice_handler(update, context):
    message = update.message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user:
        return

    if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return

    if not is_game_chat(chat):
        return

    if not message.dice:
        return

    game = get_active_game_for_user(chat.id, user.id)

    if not game:
        return

    if game["status"] not in ("creator_turn", "opponent_turn"):
        return

    game_type = game["game_type"]

    if not dice_matches_game(message.dice.emoji, game_type):
        return

    is_creator = (user.id == game["creator_id"])
    is_opponent = (game["opponent_id"] == user.id)

    if game["status"] == "creator_turn":
        if not is_creator:
            return

        with closing(get_db()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")

                fresh = db.execute(
                    "SELECT * FROM games WHERE id = ?", (game["id"],)
                ).fetchone()

                if not fresh:
                    db.rollback()
                    return

                rolls = int(fresh["creator_rolls"])

                if rolls >= int(fresh["count"]):
                    db.rollback()
                    return

                new_rolls = rolls + 1
                current_total = int(fresh["creator_total"])
                new_total = current_total + int(message.dice.value)

                if new_rolls >= int(fresh["count"]):
                    if fresh["mode"] == "bot":
                        new_status = "bot_turn"
                    else:
                        new_status = "opponent_turn"
                else:
                    new_status = "creator_turn"

                db.execute("""
                    UPDATE games
                    SET creator_rolls = ?,
                        creator_total = ?,
                        status = ?
                    WHERE id = ?
                      AND status = 'creator_turn'
                      AND creator_rolls = ?
                """, (
                    new_rolls, new_total, new_status,
                    fresh["id"], rolls
                ))

                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Creator dice processing failed")
                return

        if game["mode"] == "bot" and new_rolls >= int(game["count"]):
            bot_total = await bot_roll_game(context, game["id"])

            if bot_total is not None:
                await finish_game(context, game["id"])
            return

        if game["mode"] == "friends" and new_rolls >= int(game["count"]):
            fresh = get_game(game["id"])
            if not fresh:
                return

            opponent_id = fresh["opponent_id"]
            opponent_tag = user_tag(opponent_id)

            await safe_send_message(
                context.bot,
                chat.id,
                "✅ پرتاب‌های سازنده تمام شد.\n\n"
                f"🎯 حالا نوبت {opponent_tag} است.\n"
                f"{GAME_EMOJI[game_type]} را "
                f"{game['count']} بار بفرستید.",
                parse_mode="HTML"
            )

        return

    if game["status"] == "opponent_turn":
        if not is_opponent:
            return

        with closing(get_db()) as db:
            try:
                db.execute("BEGIN IMMEDIATE")

                fresh = db.execute(
                    "SELECT * FROM games WHERE id = ?", (game["id"],)
                ).fetchone()

                if not fresh:
                    db.rollback()
                    return

                rolls = int(fresh["opponent_rolls"])

                if rolls >= int(fresh["count"]):
                    db.rollback()
                    return

                new_rolls = rolls + 1
                current_total = int(fresh["opponent_total"])
                new_total = current_total + int(message.dice.value)

                if new_rolls >= int(fresh["count"]):
                    new_status = "finishing"
                else:
                    new_status = "opponent_turn"

                db.execute("""
                    UPDATE games
                    SET opponent_rolls = ?,
                        opponent_total = ?,
                        status = ?
                    WHERE id = ?
                      AND status = 'opponent_turn'
                      AND opponent_rolls = ?
                """, (
                    new_rolls, new_total, new_status,
                    fresh["id"], rolls
                ))

                db.commit()
            except Exception:
                db.rollback()
                logger.exception("Opponent dice processing failed")
                return

        if new_rolls >= int(game["count"]):
            await finish_game(context, game["id"])


# ============================================================
# BOT ROLLS
# ============================================================

async def bot_roll_game(context, game_id):
    game = get_game(game_id)

    if not game:
        return None

    if game["mode"] != "bot":
        return None

    if game["status"] != "bot_turn":
        return None

    total = 0

    for _ in range(int(game["count"])):
        fresh = get_game(game_id)

        if not fresh or fresh["status"] != "bot_turn":
            return None

        msg = await safe_send_dice(
            context.bot,
            game["chat_id"],
            GAME_EMOJI[game["game_type"]]
        )

        if not msg:
            await reset_game_internal(
                context, game_id, reason="bot_roll_failed"
            )
            return None

        try:
            total += int(msg.dice.value)
        except Exception:
            pass

    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            fresh = db.execute(
                "SELECT status FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not fresh or fresh["status"] != "bot_turn":
                db.rollback()
                return None

            db.execute("""
                UPDATE games
                SET opponent_total = ?,
                    opponent_rolls = ?,
                    status = 'finishing'
                WHERE id = ? AND status = 'bot_turn'
            """, (total, int(game["count"]), game_id))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Bot result save failed")
            return None

    return total


# ============================================================
# FINISH GAME — نتیجه در گپ بدون کیبورد + کیبورد در PV
# ============================================================

async def finish_game(context, game_id):
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                return

            if game["status"] not in ("finishing", "opponent_turn"):
                db.rollback()
                return

            db.execute("""
                UPDATE games
                SET status = 'finished',
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
                  AND status IN ('finishing', 'opponent_turn')
            """, (game_id,))

            if db.total_changes != 1:
                db.rollback()
                return

            creator_total = int(game["creator_total"])
            opponent_total = int(game["opponent_total"])
            bet = Decimal(game["bet"])
            creator_id = int(game["creator_id"])
            opponent_id = game["opponent_id"]
            mode = game["mode"]

            pool = bet * 2
            commission = (pool * COMMISSION_RATE).quantize(
                Decimal("0.00000001")
            )
            payout = pool - commission

            result_type = None
            winner_id = None
            loser_id = None
            payout_value = Decimal("0")
            commission_value = Decimal("0")
            refund_value = Decimal("0")

            def credit(uid, amt, ttype, desc):
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (uid,)
                ).fetchone()

                if not row:
                    db.execute("""
                        INSERT OR IGNORE INTO users(user_id, balance)
                        VALUES (?, '0')
                    """, (uid,))
                    row = db.execute(
                        "SELECT balance FROM users WHERE user_id = ?",
                        (uid,)
                    ).fetchone()

                cur = Decimal(row["balance"])
                db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (str(cur + amt), uid)
                )
                db.execute("""
                    INSERT INTO transactions(
                        user_id, type, amount, description
                    )
                    VALUES (?, ?, ?, ?)
                """, (uid, ttype, str(amt), desc))

            if mode == "bot":
                if creator_total == opponent_total:
                    credit(creator_id, bet, "game_draw_refund",
                           f"Bot draw {game_id}")
                    result_type = "bot_draw"
                    refund_value = bet

                elif creator_total > opponent_total:
                    credit(creator_id, payout, "game_win",
                           f"Bot game win {game_id}")
                    credit(OWNER_ID, commission, "commission",
                           f"Commission game {game_id}")
                    result_type = "bot_user_win"
                    winner_id = creator_id
                    payout_value = payout
                    commission_value = commission

                else:
                    credit(OWNER_ID, bet, "commission",
                           f"Bot won vs {creator_id} game {game_id}")
                    result_type = "bot_bot_win"
                    commission_value = bet

            elif creator_total == opponent_total:
                credit(creator_id, bet, "game_draw_refund",
                       f"Draw {game_id}")
                if opponent_id and int(opponent_id) > 0:
                    credit(int(opponent_id), bet, "game_draw_refund",
                           f"Draw {game_id}")
                result_type = "two_draw"
                refund_value = bet

            else:
                if creator_total > opponent_total:
                    winner = creator_id
                    loser = (
                        int(opponent_id)
                        if opponent_id and int(opponent_id) > 0
                        else None
                    )
                else:
                    winner = (
                        int(opponent_id)
                        if opponent_id and int(opponent_id) > 0
                        else None
                    )
                    loser = creator_id

                if winner is None:
                    db.rollback()
                    await reset_game_internal(
                        context, game_id, reason="winner_missing"
                    )
                    return

                credit(winner, payout, "game_win",
                       f"Game win {game_id}")
                credit(OWNER_ID, commission, "commission",
                       f"Commission game {game_id}")

                result_type = "two_win"
                winner_id = winner
                loser_id = loser
                payout_value = payout
                commission_value = commission

            db.commit()

        except Exception:
            db.rollback()
            logger.exception("Finish game failed")
            return

    # =============================
    # ارسال نتیجه در گپ (بدون کیبورد)
    # =============================

    chat_id = game["chat_id"]
    emoji, name = GAME_INFO[game["game_type"]]

    creator_tag = user_tag(creator_id)

    if mode == "bot":
        opponent_tag = "🤖 ربات"
    else:
        opponent_tag = user_tag(int(opponent_id)) if opponent_id else "?"

    # --- مساوی ---
    if result_type in ("bot_draw", "two_draw"):
        await safe_send_message(
            context.bot,
            chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_total}</b>\n"
            f"👤 {opponent_tag}: <b>{opponent_total}</b>\n\n"
            "🤝 مساوی شد.\n"
            f"💰 {money(refund_value)} {CURRENCY} به هر نفر برگشت.",
            parse_mode="HTML"
        )

        if mode == "bot":
            await send_pv_result(
                context, creator_id, name,
                "🤝 مساوی شدی!",
                f"💰 {money(refund_value)} {CURRENCY} برگشت."
            )
        else:
            await send_pv_result(
                context, creator_id, name,
                "🤝 مساوی شدی!",
                f"💰 {money(refund_value)} {CURRENCY} برگشت."
            )
            if opponent_id and int(opponent_id) > 0:
                await send_pv_result(
                    context, int(opponent_id), name,
                    "🤝 مساوی شدی!",
                    f"💰 {money(refund_value)} {CURRENCY} برگشت."
                )
        return

    # --- ربات برد ---
    if result_type == "bot_bot_win":
        await safe_send_message(
            context.bot,
            chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_total}</b>\n"
            f"👤 🤖 ربات: <b>{opponent_total}</b>\n\n"
            f"😔 {creator_tag} باخت.\n"
            f"💰 {money(bet)} {CURRENCY} از دست رفت.",
            parse_mode="HTML"
        )

        await send_pv_result(
            context, creator_id, name,
            "😔 باختی!",
            f"💰 {money(bet)} {CURRENCY} از دست رفت."
        )
        return

    # --- بازیکن برد مقابل ربات ---
    if result_type == "bot_user_win":
        await safe_send_message(
            context.bot,
            chat_id,
            "🏁 <b>نتیجه بازی</b>\n\n"
            f"{emoji} {name}\n\n"
            f"👤 {creator_tag}: <b>{creator_total}</b>\n"
            f"👤 🤖 ربات: <b>{opponent_total}</b>\n\n"
            f"🏆 {creator_tag} برنده شد!\n"
            f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>",
            parse_mode="HTML"
        )

        await send_pv_result(
            context, creator_id, name,
            "🏆 برنده شدی!",
            f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>"
        )
        return

    # --- دوستان ---
    winner_tag = user_tag(winner_id)
    loser_tag = user_tag(loser_id) if loser_id else "?"

    await safe_send_message(
        context.bot,
        chat_id,
        "🏁 <b>نتیجه بازی</b>\n\n"
        f"{emoji} {name}\n\n"
        f"👤 {creator_tag}: <b>{creator_total}</b>\n"
        f"👤 {opponent_tag}: <b>{opponent_total}</b>\n\n"
        f"🏆 {winner_tag} برنده شد!\n"
        f"😔 {loser_tag} باخت.\n\n"
        f"💰 جایزه برنده: <b>{money(payout_value)} {CURRENCY}</b>",
        parse_mode="HTML"
    )

    await send_pv_result(
        context, winner_id, name,
        "🏆 برنده شدی!",
        f"💰 جایزه: <b>{money(payout_value)} {CURRENCY}</b>"
    )

    if loser_id:
        await send_pv_result(
            context, loser_id, name,
            "😔 باختی!",
            f"💰 {money(bet)} {CURRENCY} از دست رفت."
        )


# ============================================================
# PV RESULT HELPER
# ============================================================

async def send_pv_result(context, user_id, game_name, header, detail):
    text = (
        f"{header}\n\n"
        f"🎮 بازی: {game_name}\n"
        f"{detail}\n\n"
        f"💰 موجودی: {money(get_balance(user_id))} {CURRENCY}"
    )

    try:
        await safe_send_message(
            context.bot,
            user_id,
            text,
            parse_mode="HTML",
            reply_markup=result_pv_keyboard()
        )
    except Exception:
        logger.warning("PV result failed for user %s", user_id)


# ============================================================
# RESET GAME INTERNAL
# ============================================================

async def reset_game_internal(context, game_id, reason="manual"):
    with closing(get_db()) as db:
        try:
            db.execute("BEGIN IMMEDIATE")

            game = db.execute(
                "SELECT * FROM games WHERE id = ?", (game_id,)
            ).fetchone()

            if not game:
                db.rollback()
                return False

            if game["status"] in ("finished", "cancelled", "reset"):
                db.rollback()
                return False

            bet = Decimal(game["bet"])

            row = db.execute(
                "SELECT balance FROM users WHERE user_id = ?",
                (game["creator_id"],)
            ).fetchone()

            if row:
                balance = Decimal(row["balance"])
                db.execute(
                    "UPDATE users SET balance = ? WHERE user_id = ?",
                    (str(balance + bet), game["creator_id"])
                )
                db.execute("""
                    INSERT INTO transactions(
                        user_id, type, amount, description
                    )
                    VALUES (?, ?, ?, ?)
                """, (game["creator_id"], "game_reset_refund",
                      str(bet), f"Reset {game_id}: {reason}"))

            if (game["mode"] == "friends"
                    and game["opponent_id"]
                    and int(game["opponent_id"]) > 0):
                row = db.execute(
                    "SELECT balance FROM users WHERE user_id = ?",
                    (game["opponent_id"],)
                ).fetchone()

                if row:
                    balance = Decimal(row["balance"])
                    db.execute(
                        "UPDATE users SET balance = ? WHERE user_id = ?",
                        (str(balance + bet), game["opponent_id"])
                    )
                    db.execute("""
                        INSERT INTO transactions(
                            user_id, type, amount, description
                        )
                        VALUES (?, ?, ?, ?)
                    """, (game["opponent_id"], "game_reset_refund",
                          str(bet), f"Reset {game_id}: {reason}"))

            db.execute("""
                UPDATE games
                SET status = 'reset',
                    finished_at = CURRENT_TIMESTAMP
                WHERE id = ?
            """, (game_id,))

            db.commit()
        except Exception:
            db.rollback()
            logger.exception("Reset game failed")
            return False

    return True


# ============================================================
# RESET COMMANDS
# ============================================================

async def reset_game_command(update, context):
    user = update.effective_user
    chat = update.effective_chat

    if not is_owner(user.id):
        await update.message.reply_text(
            "❌ فقط مالک می‌تواند بازی را ریست کند."
        )
        return

    args = context.args

    if args:
        try:
            game_id = int(normalize_digits(args[0]))
        except Exception:
            await update.message.reply_text(
                "❌ آیدی بازی صحیح نیست.\n\nمثال:\n/resetgame 25"
            )
            return
    else:
        with closing(get_db()) as db:
            row = db.execute("""
                SELECT id FROM games
                WHERE chat_id = ?
                  AND status NOT IN ('finished', 'cancelled', 'reset')
                ORDER BY id DESC
                LIMIT 1
            """, (chat.id,)).fetchone()

        if not row:
            await update.message.reply_text(
                "❌ بازی فعالی در این گپ نیست."
            )
            return

        game_id = int(row["id"])

    success = await reset_game_internal(
        context, game_id, reason=f"Owner {user.id}"
    )

    if success:
        await update.message.reply_text(
            "♻️ بازی ریست شد.\n\n"
            f"🎮 Game ID: {game_id}\n"
            "💰 شرط‌ها برگشت داده شد."
        )
    else:
        await update.message.reply_text(
            "❌ بازی پیدا نشد یا قبلاً بسته شده است."
        )


async def reset_all_games_command(update, context):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text("❌ فقط مالک.")
        return

    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id FROM games
            WHERE status NOT IN ('finished', 'cancelled', 'reset')
        """).fetchall()

    count = 0
    for row in rows:
        if await reset_game_internal(
            context, int(row["id"]), reason=f"Owner all {user.id}"
        ):
            count += 1

    await update.message.reply_text(
        "♻️ ریست انجام شد.\n\n"
        f"🎮 تعداد بازی‌های ریست‌شده: {count}"
    )


# ============================================================
# CLEANUP
# ============================================================

async def cleanup_old_games(context):
    with closing(get_db()) as db:
        rows = db.execute("""
            SELECT id FROM games
            WHERE status IN (
                'waiting', 'creator_turn', 'opponent_turn',
                'bot_turn', 'finishing'
            )
            AND datetime(created_at) < datetime('now', ?)
        """, (f"-{GAME_TIMEOUT_MINUTES} minutes",)).fetchall()

    for row in rows:
        await reset_game_internal(
            context, int(row["id"]), reason="timeout"
        )


async def cleanup_job(context):
    try:
        await cleanup_old_games(context)
    except Exception:
        logger.exception("Cleanup job failed")


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    if not message.text:
        return

    ensure_user(user)

    raw_text = message.text.strip()
    text = normalize_digits(raw_text).strip()

    # ========================================================
    # ADMIN INPUT
    # ========================================================
    if is_owner(user.id):
        handled = await admin_balance_by_id(update, context)
        if handled:
            return

    if is_owner(user.id):
        match_charge = re.match(
            r"^شارژ\s+([0-9]+(?:\.[0-9]+)?)$", text
        )
        if match_charge:
            amount = parse_amount(match_charge.group(1))
            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست.\nمثال: شارژ 100"
                )
                return
            await admin_change_by_reply(
                update, context, "charge", amount
            )
            return

    if is_owner(user.id):
        match_remove = re.match(
            r"^کسر\s+([0-9]+(?:\.[0-9]+)?)$", text
        )
        if match_remove:
            amount = parse_amount(match_remove.group(1))
            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست.\nمثال: کسر 100"
                )
                return
            await admin_change_by_reply(
                update, context, "remove", amount
            )
            return

    if is_owner(user.id) and text == "روشن":
        set_bot_enabled(True)
        await message.reply_text("🟢 ربات روشن شد.")
        return

    if is_owner(user.id) and text == "خاموش":
        set_bot_enabled(False)
        await message.reply_text("🔴 ربات خاموش شد.")
        return

    # ========================================================
    # BALANCE
    # ========================================================
    if text in (
        "موجودی", "م", "موجودی من", "موجودی‌من", "balance"
    ):
        await show_balance(update, context)
        return

    # ========================================================
    # MY ID
    # ========================================================
    if text in ("آیدی", "ایدی", "myid", "id"):
        await show_my_id(update, context)
        return

    # ========================================================
    # REFERRAL
    # ========================================================
    if text in (
        "زیر مجموعه", "زیرمجموعه", "زیر‌مجموعه",
        "رفرال", "referral"
    ):
        await show_referral(update, context)
        return

    # ========================================================
    # WITHDRAW
    # ========================================================
    if text.startswith("برداشت"):
        match = re.match(
            r"^برداشت\s+([0-9]+(?:\.[0-9]+)?)\s+(\S+)$", text
        )

        if not match:
            await message.reply_text(
                "❌ فرمت صحیح:\n\n"
                "برداشت <مبلغ> <آدرس ولت>\n\n"
                f"حداقل: {money(MIN_WITHDRAWAL)} {CURRENCY}\n\n"
                "مثال:\n"
                "برداشت 2500 UQxxxxxxxxxxxxxxxxxxxx"
            )
            return

        amount = parse_amount(match.group(1))
        wallet = match.group(2).strip()

        await process_withdrawal(
            update, context, amount, wallet
        )
        return

    # ========================================================
    # TRANSFER
    # ========================================================
    if text.startswith("انتقال"):
        # حالت ۱: انتقال با ریپلای (فقط مبلغ)
        # حالت ۲: انتقال با آیدی عددی (مبلغ + آیدی)
        match_reply = re.match(
            r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)$", text
        )
        match_id = re.match(
            r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)\s+(\d{5,15})$", text
        )

        if match_id:
            amount = parse_amount(match_id.group(1))
            target_id = int(match_id.group(2))

            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return

            await transfer_by_id(update, context, amount, target_id)
            return

        if match_reply:
            amount = parse_amount(match_reply.group(1))
            if amount is None:
                await message.reply_text("❌ مبلغ صحیح نیست.")
                return
            await transfer(update, context, amount)
            return

        await message.reply_text(
            "❌ فرمت صحیح:\n\n"
            "1️⃣ با ریپلای:\n"
            "انتقال 50\n\n"
            "2️⃣ با آیدی عددی:\n"
            "انتقال 50 123456789\n\n"
            "⚠️ آیدی خودت رو از دکمه «🆔 آیدی من» بگیر."
        )
        return

    # ========================================================
    # GAME
    # ========================================================
    game = parse_game(text)

    if game == "count_too_high":
        await message.reply_text(
            f"❌ حداکثر تعداد پرتاب {MAX_GAME_COUNT} است.\n\n"
            "مثال:\n3 تاس 50"
        )
        return

    if game:
        if message.chat.type not in (
            ChatType.GROUP, ChatType.SUPERGROUP
        ):
            await message.reply_text(
                "❌ بازی‌ها فقط داخل گپ انجام می‌شوند."
            )
            return

        if not is_game_chat(message.chat):
            await message.reply_text(
                "❌ بازی فقط در گپ زیر مجاز است:\n"
                f"{GROUP_URL}"
            )
            return

        count, game_type, bet = game
        await create_game(update, context, count, game_type, bet)
        return

    # ========================================================
    # HELP
    # ========================================================
    if text in ("راهنما", "کمک", "help"):
        await show_help(update, context)
        return

    return


# ============================================================
# COMMANDS
# ============================================================

async def admin_command(update, context):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text("❌ دسترسی ندارید.")
        return

    await admin_panel(update, context)


async def balance_command(update, context):
    await show_balance(update, context)


async def referral_command(update, context):
    await show_referral(update, context)


async def help_command(update, context):
    await show_help(update, context)


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    error = context.error

    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning("Temporary Telegram error: %s", error)
        return

    logger.exception("Unhandled exception", exc_info=error)


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است.\n"
            "در GitHub Secrets مقدار BOT_TOKEN را قرار بده."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("balance", balance_command))
    application.add_handler(CommandHandler("referral", referral_command))
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("resetgame", reset_game_command))
    application.add_handler(CommandHandler("resetgames", reset_all_games_command))

    application.add_handler(CallbackQueryHandler(
        membership_callback,
        pattern=r"^check_membership$"
    ))

    application.add_handler(CallbackQueryHandler(
        admin_callback,
        pattern=r"^admin_(on|off|charge|remove|stats|status|withdrawals|back)$"
    ))

    application.add_handler(CallbackQueryHandler(
        withdraw_callback,
        pattern=r"^wd_(ok|no)_\d+$"
    ))

    application.add_handler(CallbackQueryHandler(
        user_callback,
        pattern=r"^user_(balance|ref|help|withdraw|myid)$"
    ))

    application.add_handler(CallbackQueryHandler(
        join_game_callback, pattern=r"^join_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        bot_game_callback, pattern=r"^bot_\d+$"
    ))
    application.add_handler(CallbackQueryHandler(
        cancel_game_callback, pattern=r"^cancel_\d+$"
    ))

    application.add_handler(MessageHandler(
        filters.Dice.ALL,
        user_game_dice_handler
    ))

    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        text_handler
    ))

    application.add_error_handler(error_handler)

    if application.job_queue:
        application.job_queue.run_repeating(
            cleanup_job, interval=300, first=60
        )

    logger.info("BET_BT started successfully.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
