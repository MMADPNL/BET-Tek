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

OWNER_IDS = {
    8552447077,
    7966359658,
    7221112088,
}

CHANNEL_USERNAME = "@BET_Tek"
CHANNEL_URL = "https://t.me/BET_Tek"

DB_FILE = "bot.db"

MIN_GAME_BET = Decimal("0.1")
REFERRAL_REWARD = Decimal("0.05")
MAX_GAME_COUNT = 20

# برای شرط 0.1:
# برنده = 0.185
# سهم مالک = 0.015
WINNER_RATE = Decimal("1.85")
OWNER_RATE = Decimal("0.15")

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("BET_BT")


# ============================================================
# DATABASE
# ============================================================

def get_db():
    db = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )

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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

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

    text = text.replace("٫", ".")
    text = text.replace(",", ".")

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
                user_id,
                username,
                first_name,
                balance
            )
            VALUES (?, ?, ?, '0')
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
        ))

        db.execute("""
            UPDATE users
            SET username = ?,
                first_name = ?
            WHERE user_id = ?
        """, (
            user.username or "",
            user.first_name or "",
            user.id,
        ))

        db.commit()


def get_balance(user_id):
    with closing(get_db()) as db:

        row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return Decimal("0")

    try:
        return Decimal(row["balance"])
    except Exception:
        return Decimal("0")


def change_balance(
    user_id,
    amount,
    transaction_type,
    description=""
):
    amount = Decimal(str(amount))

    with closing(get_db()) as db:

        try:
            db.execute("BEGIN IMMEDIATE")

            row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (user_id,)).fetchone()

            if not row:
                raise ValueError("user_not_found")

            current = Decimal(row["balance"])
            new_balance = current + amount

            if new_balance < 0:
                raise ValueError("insufficient_balance")

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_balance),
                user_id,
            ))

            db.execute("""
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, ?, ?, ?)
            """, (
                user_id,
                transaction_type,
                str(amount),
                description,
            ))

            db.commit()

        except Exception:
            db.rollback()
            raise


def bot_enabled():
    with closing(get_db()) as db:

        row = db.execute("""
            SELECT value
            FROM settings
            WHERE key = 'bot_enabled'
        """).fetchone()

    return bool(row and row["value"] == "1")


def set_bot_enabled(enabled):
    with closing(get_db()) as db:

        db.execute("""
            INSERT INTO settings(key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
        """, (
            "1" if enabled else "0",
        ))

        db.commit()


# ============================================================
# SAFE TELEGRAM
# ============================================================

async def safe_send_message(
    bot,
    chat_id,
    text,
    **kwargs
):
    for attempt in range(3):

        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs
            )

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Telegram network error: %s",
                e
            )

            if attempt < 2:
                await asyncio.sleep(2)

        except TelegramError as e:

            logger.error(
                "Telegram error: %s",
                e
            )

            return None

        except Exception as e:

            logger.exception(
                "Unexpected Telegram error: %s",
                e
            )

            return None

    return None


async def safe_send_dice(
    bot,
    chat_id,
    emoji
):
    for attempt in range(3):

        try:
            return await bot.send_dice(
                chat_id=chat_id,
                emoji=emoji
            )

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Dice network error: %s",
                e
            )

            if attempt < 2:
                await asyncio.sleep(2)

        except TelegramError as e:

            logger.error(
                "Dice Telegram error: %s",
                e
            )

            return None

        except Exception as e:

            logger.exception(
                "Unexpected dice error: %s",
                e
            )

            return None

    return None


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(user_id, context):

    try:

        member = await context.bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        logger.warning(
            "Membership check failed: %s",
            e
        )

        return False


async def require_membership(update, context):

    user = update.effective_user

    if not user:
        return False

    if await check_membership(user.id, context):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در BET_Tek",
                url=CHANNEL_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_membership"
            )
        ]
    ])

    if update.message:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔒 ابتدا عضو کانال BET_Tek شوید.",
            reply_markup=keyboard
        )

    return False


async def membership_callback(update, context):

    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    if await check_membership(
        query.from_user.id,
        context
    ):

        try:

            await query.edit_message_text(
                "✅ عضویت شما تأیید شد.\n\n"
                "حالا می‌توانید از ربات استفاده کنید."
            )

        except Exception:
            pass

    else:

        try:

            await query.answer(
                "❌ هنوز عضو کانال نیستید.",
                show_alert=True
            )

        except Exception:
            pass


# ============================================================
# KEYBOARDS
# ============================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="user_balance"
            ),
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="user_ref"
            )
        ],
        [
            InlineKeyboardButton(
                "📚 راهنما",
                callback_data="user_help"
            )
        ]
    ])


def admin_keyboard():

    status = "🟢 روشن" if bot_enabled() else "🔴 خاموش"

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 روشن",
                callback_data="admin_on"
            ),
            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_off"
            )
        ],
        [
            InlineKeyboardButton(
                "➕ شارژ موجودی",
                callback_data="admin_charge"
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ]
    ])


# ============================================================
# START
# ============================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):

            try:
                referrer_id = int(
                    arg.replace("ref_", "")
                )
            except Exception:
                referrer_id = None

            if (
                referrer_id
                and referrer_id != user.id
            ):

                with closing(get_db()) as db:

                    row = db.execute("""
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                    """, (
                        user.id,
                    )).fetchone()

                    if row and row["referrer_id"] is None:

                        ref_exists = db.execute("""
                            SELECT user_id
                            FROM users
                            WHERE user_id = ?
                        """, (
                            referrer_id,
                        )).fetchone()

                        if ref_exists:

                            try:

                                db.execute(
                                    "BEGIN IMMEDIATE"
                                )

                                db.execute("""
                                    UPDATE users
                                    SET referrer_id = ?
                                    WHERE user_id = ?
                                      AND referrer_id IS NULL
                                """, (
                                    referrer_id,
                                    user.id
                                ))

                                db.execute("""
                                    INSERT OR IGNORE INTO referrals(
                                        referrer_id,
                                        referred_id,
                                        reward
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

                                    logger.exception(
                                        "Referral reward failed"
                                    )

                            except Exception:

                                db.rollback()

    # --------------------------------------------------------
    # START MESSAGE
    # --------------------------------------------------------

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(get_balance(user.id))} TRX\n\n"
        "دستورات:\n"
        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n\n"
        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1",
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
        f"{money(get_balance(user.id))} TRX"
    )

    if update.callback_query:

        try:

            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_keyboard()
            )

        except Exception:
            pass

    elif update.message:

        await update.message.reply_text(text)


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

        link = (
            f"https://t.me/{username}"
            f"?start=ref_{user.id}"
        )

    else:

        link = "لینک دعوت در دسترس نیست."

    with closing(get_db()) as db:

        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id = ?
        """, (
            user.id,
        )).fetchone()

    total = row["total"] if row else 0

    text = (
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {total}\n"
        f"🎁 پاداش هر نفر: "
        f"{money(REFERRAL_REWARD)} TRX"
    )

    if update.callback_query:

        try:

            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_keyboard()
            )

        except Exception:
            pass

    elif update.message:

        await update.message.reply_text(text)


# ============================================================
# HELP
# ============================================================

async def show_help(update, context):

    text = (
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "موجودی\n\n"
        "👥 زیرمجموعه\n"
        "زیر مجموعه\n\n"
        "🔄 انتقال در گپ\n"
        "روی پیام کاربر Reply کنید:\n"
        "انتقال 0.1\n"
        "انتقال ۰.۱\n\n"
        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1"
    )

    if update.callback_query:

        try:

            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_keyboard()
            )

        except Exception:
            pass

    elif update.message:

        await update.message.reply_text(text)


# ============================================================
# ATOMIC TRANSFER
# ============================================================

def atomic_transfer(
    sender_id,
    receiver_id,
    amount
):

    amount = Decimal(str(amount))

    if amount <= 0:
        raise ValueError("invalid_amount")

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            sender = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                sender_id,
            )).fetchone()

            receiver = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                receiver_id,
            )).fetchone()

            if not sender or not receiver:
                raise ValueError("user_not_found")

            sender_balance = Decimal(
                sender["balance"]
            )

            receiver_balance = Decimal(
                receiver["balance"]
            )

            if sender_balance < amount:
                raise ValueError(
                    "insufficient_balance"
                )

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_sender),
                sender_id
            ))

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_receiver),
                receiver_id
            ))

            db.execute("""
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, ?, ?, ?)
            """, (
                sender_id,
                "transfer_out",
                str(-amount),
                f"To {receiver_id}"
            ))

            db.execute("""
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, ?, ?, ?)
            """, (
                receiver_id,
                "transfer_in",
                str(amount),
                f"From {sender_id}"
            ))

            db.commit()

        except Exception:
            db.rollback()
            raise


async def transfer(update, context, amount):

    user = update.effective_user
    message = update.message

    if not message.reply_to_message:

        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ گیرنده پیدا نشد."
        )

        return

    if target.id == user.id:

        await message.reply_text(
            "❌ نمی‌توانید به خودتان انتقال دهید."
        )

        return

    if target.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )

        return

    ensure_user(target)

    try:

        atomic_transfer(
            user.id,
            target.id,
            amount
        )

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await message.reply_text(
                "❌ موجودی شما کافی نیست.\n\n"
                f"💰 موجودی: "
                f"{money(get_balance(user.id))} TRX"
            )

        else:

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "Atomic transfer failed"
        )

        await message.reply_text(
            "❌ انتقال انجام نشد."
        )

        return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: "
        f"{target.first_name or target.id}\n"
        f"💰 موجودی شما: "
        f"{money(get_balance(user.id))} TRX"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):

    user = update.effective_user

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ شما مالک ربات نیستید."
        )

        return

    status = (
        "🟢 روشن"
        if bot_enabled()
        else "🔴 خاموش"
    )

    await update.message.reply_text(
        "👑 پنل مدیریت BET_BT\n\n"
        f"وضعیت: {status}\n\n"
        "➕ شارژ موجودی:\n"
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "شارژ 100\n\n"
        "➖ کسر موجودی:\n"
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "کسر 100",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CHANGE
# ============================================================

async def admin_change_balance(
    update,
    context,
    operation,
    amount
):

    user = update.effective_user

    if not is_owner(user.id):
        return

    message = update.message

    if not message.reply_to_message:

        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "شارژ 100\n"
            "کسر 100"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    if target.is_bot:

        await message.reply_text(
            "❌ نمی‌توان موجودی ربات را تغییر داد."
        )

        return

    if target.id in OWNER_IDS:

        await message.reply_text(
            "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
        )

        return

    ensure_user(target)

    try:

        if operation == "charge":

            change_balance(
                target.id,
                amount,
                "admin_charge",
                f"Owner {user.id}"
            )

            title = "شارژ"

        else:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {user.id}"
            )

            title = "کسر"

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )

        else:

            await message.reply_text(
                "❌ عملیات انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "Admin balance operation failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: "
        f"{target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):

    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):

        try:

            await query.answer(
                "❌ دسترسی ندارید.",
                show_alert=True
            )

        except Exception:
            pass

        return

    data = query.data

    try:
        await query.answer()
    except Exception:
        pass

    # --------------------------------------------------------
    # ON
    # --------------------------------------------------------

    if data == "admin_on":

        set_bot_enabled(True)

        try:

            await query.edit_message_text(
                "👑 پنل مدیریت BET_BT\n\n"
                "🟢 ربات روشن است.",
                reply_markup=admin_keyboard()
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # OFF
    # --------------------------------------------------------

    if data == "admin_off":

        set_bot_enabled(False)

        try:

            await query.edit_message_text(
                "👑 پنل مدیریت BET_BT\n\n"
                "🔴 ربات خاموش است.",
                reply_markup=admin_keyboard()
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # CHARGE
    # --------------------------------------------------------

    if data == "admin_charge":

        context.user_data["admin_operation"] = "charge"

        try:

            await query.message.reply_text(
                "➕ حالت شارژ فعال شد.\n\n"
                "حالا داخل گپ روی پیام کاربر Reply کنید و "
                "بنویسید:\n\n"
                "شارژ 100\n"
                "یا\n"
                "شارژ ۱۰۰"
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        context.user_data["admin_operation"] = "remove"

        try:

            await query.message.reply_text(
                "➖ حالت کسر فعال شد.\n\n"
                "حالا داخل گپ روی پیام کاربر Reply کنید و "
                "بنویسید:\n\n"
                "کسر 100\n"
                "یا\n"
                "کسر ۱۰۰"
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # STATS
    # --------------------------------------------------------

    if data == "admin_stats":

        with closing(get_db()) as db:

            users = db.execute("""
                SELECT COUNT(*) AS c
                FROM users
            """).fetchone()["c"]

            total = db.execute("""
                SELECT COALESCE(
                    SUM(CAST(balance AS REAL)),
                    0
                ) AS total
                FROM users
            """).fetchone()["total"]

            referrals = db.execute("""
                SELECT COUNT(*) AS c
                FROM referrals
            """).fetchone()["c"]

        try:

            await query.edit_message_text(
                "📊 آمار BET_BT\n\n"
                f"👤 کاربران: {users}\n"
                f"💰 مجموع موجودی: "
                f"{float(total):.2f} TRX\n"
                f"👥 زیرمجموعه‌ها: {referrals}\n\n"
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

        await show_balance(
            update,
            context
        )

    elif query.data == "user_ref":

        await show_referral(
            update,
            context
        )

    elif query.data == "user_help":

        await show_help(
            update,
            context
        )


# ============================================================
# GAME
# ============================================================

GAME_INFO = {
    "dice": ("🎲", "تاس"),
    "darts": ("🎯", "دارت"),
    "bowling": ("🎳", "بولینگ"),
    "basketball": ("🏀", "بسکتبال"),
}

GAME_NAMES = {
    "تاس": "dice",
    "دارت": "darts",
    "دارتس": "darts",
    "بولینگ": "bowling",
    "بولينگ": "bowling",
    "بسکتبال": "basketball",
    "dice": "dice",
    "darts": "darts",
    "bowling": "bowling",
    "basketball": "basketball",
}

GAME_EMOJI = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


def parse_game(text):

    text = normalize_digits(
        text.strip()
    )

    pattern = (
        r"^(\d+)\s+([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$"
    )

    match = re.match(
        pattern,
        text
    )

    if not match:
        return None

    count = int(match.group(1))

    name = match.group(2).lower()

    bet = parse_amount(
        match.group(3)
    )

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if bet is None:
        return None

    if name not in GAME_NAMES:
        return None

    return (
        count,
        GAME_NAMES[name],
        bet
    )


async def create_game(
    update,
    context,
    count,
    game_type,
    bet
):

    user = update.effective_user
    chat = update.effective_chat

    ensure_user(user)

    if not await require_membership(
        update,
        context
    ):
        return

    if not bot_enabled():

        await update.message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    if bet < MIN_GAME_BET:

        await update.message.reply_text(
            f"❌ حداقل شرط "
            f"{money(MIN_GAME_BET)} TRX است."
        )

        return

    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type}"
        )

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await update.message.reply_text(
                "❌ موجودی شما کافی نیست.\n\n"
                f"💰 موجودی: "
                f"{money(get_balance(user.id))} TRX"
            )

        else:

            await update.message.reply_text(
                "❌ ایجاد بازی انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "Game bet failed"
        )

        await update.message.reply_text(
            "❌ ایجاد بازی انجام نشد."
        )

        return

    try:

        with closing(get_db()) as db:

            cursor = db.execute("""
                INSERT INTO games(
                    chat_id,
                    creator_id,
                    game_type,
                    count,
                    bet,
                    mode,
                    status,
                    creator_total,
                    opponent_total
                )
                VALUES (
                    ?, ?, ?, ?, ?,
                    'friends',
                    'waiting',
                    0,
                    0
                )
            """, (
                chat.id,
                user.id,
                game_type,
                count,
                str(bet),
            ))

            game_id = cursor.lastrowid

            db.commit()

    except Exception:

        try:

            change_balance(
                user.id,
                bet,
                "game_refund",
                "Game creation rollback"
            )

        except Exception:

            logger.exception(
                "Game creation rollback failed"
            )

        await update.message.reply_text(
            "❌ بازی ایجاد نشد."
        )

        return

    emoji, name = GAME_INFO[game_type]

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel_{game_id}"
            )
        ]
    ])

    await update.message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد بازی: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: "
        f"{user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )


# ============================================================
# ROLLS
# ============================================================

async def perform_rolls(
    context,
    chat_id,
    game_type,
    count
):

    total = 0

    for _ in range(count):

        msg = await safe_send_dice(
            context.bot,
            chat_id,
            GAME_EMOJI[game_type]
        )

        if msg is None:
            continue

        try:

            total += int(
                msg.dice.value
            )

        except Exception:
            pass

        await asyncio.sleep(0.7)

    return total


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game_callback(
    update,
    context
):

    query = update.callback_query
    user = query.from_user

    if not await check_membership(
        user.id,
        context
    ):

        try:

            await query.answer(
                "❌ ابتدا عضو BET_Tek شوید.",
                show_alert=True
            )

        except Exception:
            pass

        return

    try:

        await query.answer()

    except Exception:
        pass

    try:

        game_id = int(
            query.data.replace(
                "join_",
                ""
            )
        )

    except Exception:
        return

    ensure_user(user)

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

    if not game:

        try:

            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )

        except Exception:
            pass

        return

    if game["status"] != "waiting":

        try:

            await query.answer(
                "❌ این بازی دیگر فعال نیست.",
                show_alert=True
            )

        except Exception:
            pass

        return

    if game["creator_id"] == user.id:

        try:

            await query.answer(
                "❌ نمی‌توانید با خودتان بازی کنید.",
                show_alert=True
            )

        except Exception:
            pass

        return

    bet = Decimal(game["bet"])

    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Join game {game_id}"
        )

    except ValueError:

        try:

            await query.answer(
                "❌ موجودی شما کافی نیست.",
                show_alert=True
            )

        except Exception:
            pass

        return

    try:

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET opponent_id = ?,
                    status = 'playing',
                    mode = 'friends'
                WHERE id = ?
                  AND status = 'waiting'
            """, (
                user.id,
                game_id
            ))

            db.commit()

    except Exception:

        try:

            change_balance(
                user.id,
                bet,
                "game_refund",
                f"Join rollback {game_id}"
            )

        except Exception:

            logger.exception(
                "Join rollback failed"
            )

        return

    emoji, name = GAME_INFO[
        game["game_type"]
    ]

    try:

        await query.edit_message_text(
            "🎮 بازی شروع شد!\n\n"
            f"{emoji} {name}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط هر نفر: "
            f"{money(bet)} TRX\n\n"
            "🎯 ابتدا سازنده تمام پرتاب‌های خود "
            "را انجام می‌دهد."
        )

    except Exception:
        pass

    await play_game(
        context,
        game_id
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game_callback(
    update,
    context
):

    query = update.callback_query
    user = query.from_user

    if not await check_membership(
        user.id,
        context
    ):

        try:

            await query.answer(
                "❌ ابتدا عضو BET_Tek شوید.",
                show_alert=True
            )

        except Exception:
            pass

        return

    try:

        await query.answer()

    except Exception:
        pass

    try:

        game_id = int(
            query.data.replace(
                "bot_",
                ""
            )
        )

    except Exception:
        return

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

    if not game:
        return

    if game["status"] != "waiting":

        try:

            await query.answer(
                "❌ بازی فعال نیست.",
                show_alert=True
            )

        except Exception:
            pass

        return

    if game["creator_id"] != user.id:

        try:

            await query.answer(
                "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                show_alert=True
            )

        except Exception:
            pass

        return

    with closing(get_db()) as db:

        db.execute("""
            UPDATE games
            SET opponent_id = -1,
                mode = 'bot',
                status = 'playing'
            WHERE id = ?
              AND status = 'waiting'
        """, (
            game_id,
        ))

        db.commit()

    emoji, name = GAME_INFO[
        game["game_type"]
    ]

    try:

        await query.edit_message_text(
            "🤖 بازی با ربات شروع شد!\n\n"
            f"{emoji} {name}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط: "
            f"{money(Decimal(game['bet']))} TRX\n\n"
            "🎯 ابتدا تمام پرتاب‌های شما انجام می‌شود."
        )

    except Exception:
        pass

    await play_game(
        context,
        game_id
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game_callback(
    update,
    context
):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    try:

        game_id = int(
            query.data.replace(
                "cancel_",
                ""
            )
        )

    except Exception:
        return

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

        if not game:
            return

        if game["status"] != "waiting":

            try:

                await query.answer(
                    "❌ بازی دیگر قابل لغو نیست.",
                    show_alert=True
                )

            except Exception:
                pass

            return

        if (
            game["creator_id"] != user.id
            and not is_owner(user.id)
        ):

            try:

                await query.answer(
                    "❌ فقط سازنده یا مالک.",
                    show_alert=True
                )

            except Exception:
                pass

            return

        db.execute("""
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    try:

        change_balance(
            game["creator_id"],
            Decimal(game["bet"]),
            "game_refund",
            f"Cancel game {game_id}"
        )

    except Exception:

        logger.exception(
            "Game refund failed"
        )

    try:

        await query.edit_message_text(
            "❌ بازی لغو شد.\n\n"
            f"💰 {money(Decimal(game['bet']))} TRX "
            "به سازنده برگشت داده شد."
        )

    except Exception:
        pass


# ============================================================
# PLAY GAME
# ============================================================

async def play_game(
    context,
    game_id
):

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

    if not game:
        return

    if game["status"] != "playing":
        return

    chat_id = game["chat_id"]
    game_type = game["game_type"]
    count = int(game["count"])

    # --------------------------------------------------------
    # PLAYER FIRST
    # --------------------------------------------------------

    player_total = await perform_rolls(
        context,
        chat_id,
        game_type,
        count
    )

    with closing(get_db()) as db:

        db.execute("""
            UPDATE games
            SET creator_total = ?
            WHERE id = ?
        """, (
            player_total,
            game_id
        ))

        db.commit()

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if game["mode"] == "bot":

        await safe_send_message(
            context.bot,
            chat_id,
            "🤖 پرتاب‌های شما تمام شد.\n\n"
            "🎯 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
        )

        bot_total = await perform_rolls(
            context,
            chat_id,
            game_type,
            count
        )

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET opponent_total = ?
                WHERE id = ?
            """, (
                bot_total,
                game_id
            ))

            db.commit()

        await finish_game(
            context,
            game_id
        )

        return

    # --------------------------------------------------------
    # FRIEND
    # --------------------------------------------------------

    await safe_send_message(
        context.bot,
        chat_id,
        "✅ پرتاب‌های سازنده تمام شد.\n\n"
        "🎯 حالا بازیکن دوم تمام پرتاب‌های خودش "
        "را انجام می‌دهد..."
    )

    opponent_total = await perform_rolls(
        context,
        chat_id,
        game_type,
        count
    )

    with closing(get_db()) as db:

        db.execute("""
            UPDATE games
            SET opponent_total = ?
            WHERE id = ?
        """, (
            opponent_total,
            game_id
        ))

        db.commit()

    await finish_game(
        context,
        game_id
    )


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(
    context,
    game_id
):

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

    if not game:
        return

    if game["status"] != "playing":
        return

    creator_total = int(
        game["creator_total"]
    )

    opponent_total = int(
        game["opponent_total"]
    )

    bet = Decimal(
        game["bet"]
    )

    # --------------------------------------------------------
    # DRAW
    # --------------------------------------------------------

    if creator_total == opponent_total:

        try:

            change_balance(
                game["creator_id"],
                bet,
                "game_draw_refund",
                f"Draw {game_id}"
            )

            if game["mode"] == "friends":

                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_draw_refund",
                    f"Draw {game_id}"
                )

        except Exception:

            logger.exception(
                "Draw refund failed"
            )

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET status = 'finished'
                WHERE id = ?
            """, (
                game_id,
            ))

            db.commit()

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "🤝 بازی مساوی شد.\n\n"
            f"🎯 نتیجه: "
            f"{creator_total} - {opponent_total}\n\n"
            "💰 مبلغ شرط برگشت داده شد."
        )

        return

    # --------------------------------------------------------
    # WINNER
    # --------------------------------------------------------

    if creator_total > opponent_total:

        winner = game["creator_id"]

    else:

        winner = game["opponent_id"]

    # --------------------------------------------------------
    # PAYOUT
    # --------------------------------------------------------

    payout = (
        bet * WINNER_RATE
    ).quantize(
        Decimal("0.00000001")
    )

    owner_share = (
        bet * OWNER_RATE
    ).quantize(
        Decimal("0.00000001")
    )

    # --------------------------------------------------------
    # PAY WINNER
    # --------------------------------------------------------

    try:

        change_balance(
            winner,
            payout,
            "game_win",
            f"Game win {game_id}"
        )

    except Exception:

        logger.exception(
            "Winner payout failed"
        )

    # --------------------------------------------------------
    # OWNER SHARE
    # --------------------------------------------------------

    # سهم مالک فقط ثبت می‌شود و به کاربر نمایش داده نمی‌شود.
    # برای هر دو بازیکن، سهم مالک برابر 0.15 * bet است.
    # مجموع سهم مالک = 0.30 * bet.
    #
    # در این نسخه به دلیل اینکه موجودی مالک باید
    # فقط از پنل کنترل شود، سهم مالک به موجودی مالک
    # اضافه نمی‌شود؛ فقط تراکنش ثبت می‌شود.

    with closing(get_db()) as db:

        for owner_id in OWNER_IDS:

            db.execute("""
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, ?, ?, ?)
            """, (
                owner_id,
                "owner_game_share",
                str(owner_share),
                f"Game {game_id}"
            ))

        db.execute("""
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    # --------------------------------------------------------
    # RESULT
    # --------------------------------------------------------

    if game["mode"] == "bot":

        if winner == game["creator_id"]:

            result = (
                "🏆 شما برنده شدید!\n"
                f"💰 جایزه: "
                f"{money(payout)} TRX"
            )

        else:

            result = "🤖 ربات برنده شد."

    else:

        result = (
            "🏆 بازی تمام شد.\n"
            f"💰 جایزه برنده: "
            f"{money(payout)} TRX"
        )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🏁 نتیجه بازی\n\n"
        f"{GAME_INFO[game['game_type']][0]} "
        f"{GAME_INFO[game['game_type']][1]}\n\n"
        f"👤 سازنده: {creator_total}\n"
        f"👤 بازیکن دوم: {opponent_total}\n\n"
        f"{result}"
    )


# ============================================================
# ADMIN TEXT PARSER
# ============================================================

async def handle_admin_text(
    update,
    context,
    operation,
    amount
):

    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return False

    if not message.reply_to_message:

        await message.reply_text(
            "❌ برای این عملیات باید روی پیام کاربر "
            "Reply کنید.\n\n"
            f"{'شارژ' if operation == 'charge' else 'کسر'} "
            f"{money(amount)}"
        )

        return True

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ کاربر مقصد پیدا نشد."
        )

        return True

    if target.is_bot:

        await message.reply_text(
            "❌ نمی‌توان موجودی ربات را تغییر داد."
        )

        return True

    if target.id in OWNER_IDS:

        await message.reply_text(
            "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
        )

        return True

    ensure_user(target)

    try:

        if operation == "charge":

            change_balance(
                target.id,
                amount,
                "admin_charge",
                f"Owner {user.id}"
            )

            title = "شارژ"

        else:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {user.id}"
            )

            title = "کسر"

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )

        else:

            await message.reply_text(
                "❌ عملیات انجام نشد."
            )

        return True

    except Exception:

        logger.exception(
            "Admin text operation failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return True

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: "
        f"{target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
    )

    # بعد از انجام عملیات، حالت را پاک می‌کنیم.
    context.user_data.pop(
        "admin_operation",
        None
    )

    return True


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    raw_text = message.text or ""

    text = normalize_digits(
        raw_text.strip()
    )

    # ========================================================
    # ADMIN CHARGE / REMOVE
    # ========================================================

    if is_owner(user.id):

        charge_match = re.match(
            r"^شارژ\s+([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if charge_match:

            amount = parse_amount(
                charge_match.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست.\n"
                    "مثال: شارژ 100"
                )

                return

            await handle_admin_text(
                update,
                context,
                "charge",
                amount
            )

            return

        remove_match = re.match(
            r"^کسر\s+([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if remove_match:

            amount = parse_amount(
                remove_match.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست.\n"
                    "مثال: کسر 100"
                )

                return

            await handle_admin_text(
                update,
                context,
                "remove",
                amount
            )

            return

        # ----------------------------------------------------
        # MODE FROM ADMIN BUTTON
        # ----------------------------------------------------

        operation = context.user_data.get(
            "admin_operation"
        )

        if operation and message.reply_to_message:

            simple_match = re.match(
                r"^([0-9]+(?:\.[0-9]+)?)$",
                text
            )

            if simple_match:

                amount = parse_amount(
                    simple_match.group(1)
                )

                if amount:

                    await handle_admin_text(
                        update,
                        context,
                        operation,
                        amount
                    )

                    return

        # ----------------------------------------------------
        # ON
        # ----------------------------------------------------

        if text == "روشن":

            set_bot_enabled(True)

            await message.reply_text(
                "🟢 ربات روشن شد."
            )

            return

        # ----------------------------------------------------
        # OFF
        # ----------------------------------------------------

        if text == "خاموش":

            set_bot_enabled(False)

            await message.reply_text(
                "🔴 ربات خاموش شد."
            )

            return

    # ========================================================
    # BALANCE
    # ========================================================

    if text in (
        "موجودی",
        "موجودی من",
        "موجودی‌من",
        "balance"
    ):

        await show_balance(
            update,
            context
        )

        return

    # ========================================================
    # REFERRAL
    # ========================================================

    if text in (
        "زیر مجموعه",
        "زیرمجموعه",
        "زیر‌مجموعه",
        "رفرال",
        "referral"
    ):

        await show_referral(
            update,
            context
        )

        return

    # ========================================================
    # TRANSFER
    # ========================================================

    if text.startswith("انتقال"):

        match = re.match(
            r"^انتقال\s+"
            r"([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if not match:

            await message.reply_text(
                "❌ فرمت صحیح:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱\n\n"
                "⚠️ باید روی پیام کاربر Reply کنید."
            )

            return

        amount = parse_amount(
            match.group(1)
        )

        if amount is None:

            await message.reply_text(
                "❌ مبلغ صحیح نیست."
            )

            return

        await transfer(
            update,
            context,
            amount
        )

        return

    # ========================================================
    # GAME
    # ========================================================

    game = parse_game(text)

    if game:

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "❌ بازی‌ها فقط داخل گپ انجام می‌شوند."
            )

            return

        count, game_type, bet = game

        await create_game(
            update,
            context,
            count,
            game_type,
            bet
        )

        return


# ============================================================
# COMMANDS
# ============================================================

async def admin_command(
    update,
    context
):

    if not is_owner(
        update.effective_user.id
    ):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    await admin_panel(
        update,
        context
    )


async def balance_command(
    update,
    context
):

    await show_balance(
        update,
        context
    )


async def referral_command(
    update,
    context
):

    await show_referral(
        update,
        context
    )


async def help_command(
    update,
    context
):

    await show_help(
        update,
        context
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context
):

    error = context.error

    if isinstance(
        error,
        (TimedOut, NetworkError)
    ):

        logger.warning(
            "Temporary Telegram network error: %s",
            error
        )

        return

    logger.exception(
        "Unhandled exception",
        exc_info=error
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است.\n"
            "در GitHub Secrets مقدار BOT_TOKEN "
            "را قرار بده."
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

    # ========================================================
    # COMMANDS
    # ========================================================

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance_command
        )
    )

    application.add_handler(
        CommandHandler(
            "referral",
            referral_command
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command
        )
    )

    # ========================================================
    # CALLBACKS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|charge|remove|stats)$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern=r"^user_(balance|ref|help)$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_game_callback,
            pattern=r"^join_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game_callback,
            pattern=r"^bot_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game_callback,
            pattern=r"^cancel_\d+$"
        )
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # ========================================================
    # ERROR
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started successfully."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
