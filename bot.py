import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from contextlib import closing

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
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
MAX_GAME_BET = Decimal("1000000")

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
                mode TEXT NOT NULL DEFAULT 'friends',
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
# BASIC HELPERS
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


def ensure_user_id(user_id):
    with closing(get_db()) as db:
        db.execute("""
            INSERT OR IGNORE INTO users(
                user_id,
                username,
                first_name,
                balance
            )
            VALUES (?, '', '', '0')
        """, (user_id,))
        db.commit()


def get_balance(user_id):
    ensure_user_id(user_id)

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return Decimal("0")

    try:
        return Decimal(str(row["balance"]))
    except Exception:
        return Decimal("0")


def change_balance(
    user_id,
    amount,
    transaction_type,
    description=""
):
    amount = Decimal(str(amount))

    ensure_user_id(user_id)

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

            current = Decimal(str(row["balance"]))
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
# USER MENTION
# ============================================================

def user_mention(user):
    if not user:
        return "کاربر"

    name = user.first_name or "کاربر"

    if user.username:
        return f"@{user.username}"

    return f'<a href="tg://user?id={user.id}">{name}</a>'


def user_mention_by_id(user_id):
    try:
        user_id = int(user_id)
    except Exception:
        return "کاربر"

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT username, first_name
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

    if not row:
        return f'<a href="tg://user?id={user_id}">کاربر</a>'

    if row["username"]:
        return f"@{row['username']}"

    name = row["first_name"] or "کاربر"

    return f'<a href="tg://user?id={user_id}">{name}</a>'


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
                "Unexpected send error: %s",
                e
            )

            return None

    return None


async def safe_delete_message(
    bot,
    chat_id,
    message_id
):
    try:
        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id
        )
        return True
    except Exception as e:
        logger.warning(
            "Delete message failed: %s",
            e
        )
        return False


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
                "Dice error: %s",
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
                "✅ عضویت شما تأیید شد.\n"
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
    enabled = bot_enabled()

    status_text = (
        "🟢 روشن است"
        if enabled
        else
        "🔴 خاموش است"
    )

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
        ],
        [
            InlineKeyboardButton(
                status_text,
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

    # -----------------------------
    # REFERRAL
    # -----------------------------

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
                    """, (user.id,)).fetchone()

                    if (
                        row
                        and row["referrer_id"] is None
                    ):

                        already = db.execute("""
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                        """, (user.id,)).fetchone()

                        ref_exists = db.execute("""
                            SELECT user_id
                            FROM users
                            WHERE user_id = ?
                        """, (referrer_id,)).fetchone()

                        if (
                            not already
                            and ref_exists
                        ):

                            db.execute("""
                                UPDATE users
                                SET referrer_id = ?
                                WHERE user_id = ?
                            """, (
                                referrer_id,
                                user.id
                            ))

                            db.execute("""
                                INSERT INTO referrals(
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

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: "
        f"{money(get_balance(user.id))} TRX\n\n"
        "💰 موجودی\n"
        "👥 زیرمجموعه\n"
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
        """, (user.id,)).fetchone()

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

    else:

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

        "🎮 بازی:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1\n\n"

        "👥 بازی دوستان:\n"
        "اول سازنده تمام پرتاب‌ها را انجام می‌دهد، "
        "بعد بازیکن دوم.\n\n"

        "🤖 بازی با ربات:\n"
        "اول کاربر تمام پرتاب‌ها را انجام می‌دهد، "
        "بعد ربات.\n\n"

        "👑 مالک:\n"
        "/admin\n\n"

        "🔄 ریست بازی:\n"
        "/resetgame ID\n"
        "/resetgame all"
    )

    if update.callback_query:

        try:
            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_keyboard()
            )
        except Exception:
            pass

    else:

        await update.message.reply_text(text)


# ============================================================
# TRANSFER
# ============================================================

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

    if amount <= 0:

        await message.reply_text(
            "❌ مبلغ نامعتبر است."
        )

        return

    ensure_user(user)
    ensure_user(target)

    # اتمیک انتقال
    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            sender_row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (user.id,)).fetchone()

            if not sender_row:
                raise ValueError("sender_not_found")

            sender_balance = Decimal(
                str(sender_row["balance"])
            )

            if sender_balance < amount:
                raise ValueError("insufficient_balance")

            receiver_row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (target.id,)).fetchone()

            if not receiver_row:
                raise ValueError("receiver_not_found")

            receiver_balance = Decimal(
                str(receiver_row["balance"])
            )

            new_sender = sender_balance - amount
            new_receiver = receiver_balance + amount

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_sender),
                user.id
            ))

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_receiver),
                target.id
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
                user.id,
                "transfer_out",
                str(-amount),
                f"To {target.id}"
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
                target.id,
                "transfer_in",
                str(amount),
                f"From {user.id}"
            ))

            db.commit()

        except Exception as e:

            db.rollback()

            if str(e) == "insufficient_balance":

                await message.reply_text(
                    "❌ موجودی شما کافی نیست.\n\n"
                    f"💰 موجودی: "
                    f"{money(sender_balance)} TRX"
                )

            else:

                logger.exception(
                    "Transfer failed"
                )

                await message.reply_text(
                    "❌ انتقال انجام نشد."
                )

            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: {user_mention(target)}\n"
        f"💰 موجودی شما: "
        f"{money(get_balance(user.id))} TRX",
        parse_mode="HTML"
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
        else
        "🔴 خاموش"
    )

    await update.message.reply_text(
        "👑 پنل مدیریت BET_BT\n\n"
        f"وضعیت: {status}\n\n"

        "➕ شارژ از پنل:\n"
        "آیدی عددی و مقدار را بنویسید:\n"
        "مثال:\n"
        "شارژ 123456789 100\n\n"

        "➖ کسر از پنل:\n"
        "کسر 123456789 100\n\n"

        "در گپ نیز می‌توانید روی پیام کاربر Reply کنید:\n"
        "شارژ 100\n"
        "کسر 100",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN CHANGE BY ID
# ============================================================

async def admin_change_by_id(
    message,
    owner_id,
    operation,
    target_id,
    amount
):

    if not is_owner(owner_id):

        await message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    try:
        target_id = int(target_id)
    except Exception:

        await message.reply_text(
            "❌ آیدی عددی صحیح نیست."
        )

        return

    if target_id <= 0:

        await message.reply_text(
            "❌ آیدی کاربر نامعتبر است."
        )

        return

    if amount is None or amount <= 0:

        await message.reply_text(
            "❌ مبلغ صحیح نیست."
        )

        return

    if amount > MAX_GAME_BET * Decimal("100"):

        await message.reply_text(
            "❌ مبلغ بیش از حد مجاز است."
        )

        return

    if target_id in OWNER_IDS:

        await message.reply_text(
            "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
        )

        return

    ensure_user_id(target_id)

    try:

        if operation == "charge":

            change_balance(
                target_id,
                amount,
                "admin_charge",
                f"Owner {owner_id}"
            )

            title = "شارژ"

        else:

            change_balance(
                target_id,
                -amount,
                "admin_remove",
                f"Owner {owner_id}"
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
            "Admin balance change failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN CHANGE BY REPLY
# ============================================================

async def admin_change_by_reply(
    message,
    owner_id,
    operation,
    amount
):

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
                f"Owner {owner_id}"
            )

            title = "شارژ"

        else:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {owner_id}"
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
            "Admin reply balance change failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: {user_mention(target)}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX",
        parse_mode="HTML"
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

    try:
        await query.answer()
    except Exception:
        pass

    data = query.data

    if data == "admin_on":

        set_bot_enabled(True)

        try:
            await query.edit_message_text(
                "👑 پنل مدیریت\n\n"
                "🟢 ربات روشن شد.",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass

        return

    if data == "admin_off":

        set_bot_enabled(False)

        try:
            await query.edit_message_text(
                "👑 پنل مدیریت\n\n"
                "🔴 ربات خاموش شد.",
                reply_markup=admin_keyboard()
            )
        except Exception:
            pass

        return

    if data == "admin_charge":

        try:
            await query.message.reply_text(
                "➕ شارژ موجودی\n\n"
                "دو روش:\n\n"

                "روش اول - آیدی عددی:\n"
                "شارژ 123456789 100\n\n"

                "روش دوم - Reply:\n"
                "روی پیام کاربر Reply کنید و بنویسید:\n"
                "شارژ 100"
            )
        except Exception:
            pass

        return

    if data == "admin_remove":

        try:
            await query.message.reply_text(
                "➖ کسر موجودی\n\n"
                "روش اول - آیدی عددی:\n"
                "کسر 123456789 100\n\n"

                "روش دوم - Reply:\n"
                "روی پیام کاربر Reply کنید و بنویسید:\n"
                "کسر 100"
            )
        except Exception:
            pass

        return

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

            games = db.execute("""
                SELECT COUNT(*) AS c
                FROM games
            """).fetchone()["c"]

        try:

            await query.edit_message_text(
                "📊 آمار BET_BT\n\n"
                f"👤 کاربران: {users}\n"
                f"💰 مجموع موجودی: {total:.2f} TRX\n"
                f"👥 زیرمجموعه‌ها: {referrals}\n"
                f"🎮 بازی‌ها: {games}\n\n"
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

    text = normalize_digits(text.strip())

    pattern = (
        r"^(\d+)\s+([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$"
    )

    match = re.match(pattern, text)

    if not match:
        return None

    count = int(match.group(1))

    name = match.group(2).lower()

    bet = parse_amount(match.group(3))

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if bet is None:
        return None

    if bet > MAX_GAME_BET:
        return None

    if name not in GAME_NAMES:
        return None

    return (
        count,
        GAME_NAMES[name],
        bet
    )


# ============================================================
# GAME ATOMIC RESERVE
# ============================================================

def reserve_game_bet(user_id, bet, description):

    ensure_user_id(user_id)

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

            current = Decimal(str(row["balance"]))

            if current < bet:
                raise ValueError("insufficient_balance")

            new_balance = current - bet

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_balance),
                user_id
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
                "game_bet",
                str(-bet),
                description
            ))

            db.commit()

            return True

        except Exception:
            db.rollback()
            raise


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context,
    count,
    game_type,
    bet
):

    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    ensure_user(user)

    if not await require_membership(update, context):
        return

    if not bot_enabled():

        await message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    if chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        await message.reply_text(
            "❌ بازی‌ها فقط داخل گپ انجام می‌شوند."
        )

        return

    if bet < MIN_GAME_BET:

        await message.reply_text(
            f"❌ حداقل شرط "
            f"{money(MIN_GAME_BET)} TRX است."
        )

        return

    if bet > MAX_GAME_BET:

        await message.reply_text(
            "❌ مبلغ شرط بیش از حد مجاز است."
        )

        return

    # جلوگیری از چند بازی همزمان
    with closing(get_db()) as db:

        active = db.execute("""
            SELECT id
            FROM games
            WHERE chat_id = ?
              AND creator_id = ?
              AND status IN ('waiting', 'playing')
            LIMIT 1
        """, (
            chat.id,
            user.id
        )).fetchone()

    if active:

        await message.reply_text(
            "⚠️ شما یک بازی فعال دارید.\n"
            f"🎮 شماره بازی: {active['id']}\n\n"
            "ابتدا آن را تمام یا لغو کنید."
        )

        return

    # قفل موجودی
    try:

        reserve_game_bet(
            user.id,
            bet,
            f"Game reserve {game_type}"
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
                "❌ ایجاد بازی انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "Reserve game bet failed"
        )

        await message.reply_text(
            "❌ ایجاد بازی انجام نشد."
        )

        return

    try:

        with closing(get_db()) as db:

            cursor = db.execute("""
                INSERT INTO games(
                    chat_id,
                    creator_id,
                    opponent_id,
                    game_type,
                    count,
                    bet,
                    mode,
                    status,
                    creator_total,
                    opponent_total
                )
                VALUES (?, ?, NULL, ?, ?, ?, 'friends',
                        'waiting', 0, 0)
            """, (
                chat.id,
                user.id,
                game_type,
                count,
                str(bet)
            ))

            game_id = cursor.lastrowid

            db.commit()

    except Exception:

        logger.exception(
            "Create game DB failed"
        )

        try:

            change_balance(
                user.id,
                bet,
                "game_refund",
                "Create game rollback"
            )

        except Exception:

            logger.exception(
                "Create game rollback failed"
            )

        await message.reply_text(
            "❌ بازی ایجاد نشد و مبلغ برگشت داده شد."
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

    await message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: {user_mention(user)}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard,
        parse_mode="HTML"
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
    successful = 0

    for index in range(count):

        msg = await safe_send_dice(
            context.bot,
            chat_id,
            GAME_EMOJI[game_type]
        )

        if msg is None:
            continue

        try:

            value = int(msg.dice.value)

            total += value
            successful += 1

        except Exception:

            logger.exception(
                "Failed reading dice value"
            )

        await asyncio.sleep(0.7)

    return total, successful


# ============================================================
# GAME LOCK
# ============================================================

GAME_LOCKS = {}
GAME_LOCKS_GUARD = asyncio.Lock()


async def get_game_lock(game_id):

    async with GAME_LOCKS_GUARD:

        if game_id not in GAME_LOCKS:
            GAME_LOCKS[game_id] = asyncio.Lock()

        return GAME_LOCKS[game_id]


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game_callback(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

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
        game_id = int(
            query.data.replace("join_", "")
        )
    except Exception:
        return

    ensure_user(user)

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

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

            bet = Decimal(str(game["bet"]))

        try:

            reserve_game_bet(
                user.id,
                bet,
                f"Join game {game_id}"
            )

        except ValueError as e:

            if str(e) == "insufficient_balance":

                try:
                    await query.answer(
                        "❌ موجودی شما کافی نیست.",
                        show_alert=True
                    )
                except Exception:
                    pass

            return

        except Exception:

            logger.exception(
                "Join reserve failed"
            )

            return

        try:

            with closing(get_db()) as db:

                # دوباره وضعیت را قفل‌شده چک می‌کنیم
                current = db.execute("""
                    SELECT status
                    FROM games
                    WHERE id = ?
                """, (game_id,)).fetchone()

                if (
                    not current
                    or current["status"] != "waiting"
                ):

                    raise ValueError(
                        "game_no_longer_waiting"
                    )

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

            try:
                await query.answer(
                    "❌ ورود به بازی انجام نشد.",
                    show_alert=True
                )
            except Exception:
                pass

            return

    emoji, name = GAME_INFO[game["game_type"]]

    # پیام قبلی حذف شود
    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    # پیام جدید
    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🎮 بازی شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} TRX\n\n"
        f"🎯 نوبت {user_mention_by_id(game['creator_id'])}\n"
        "👤 سازنده باید تمام پرتاب‌های خود را انجام دهد.",
        parse_mode="HTML"
    )

    await play_game(
        context,
        game_id
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game_callback(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

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

        game_id = int(
            query.data.replace("bot_", "")
        )

    except Exception:
        return

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

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
                        "❌ فقط سازنده می‌تواند انتخاب کند.",
                        show_alert=True
                    )
                except Exception:
                    pass

                return

            db.execute("""
                UPDATE games
                SET opponent_id = -1,
                    mode = 'bot',
                    status = 'playing'
                WHERE id = ?
                  AND status = 'waiting'
            """, (game_id,))

            db.commit()

    emoji, name = GAME_INFO[game["game_type"]]

    # پیام قبلی حذف شود
    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    # پیام جدید
    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
        f"🎯 نوبت {user_mention_by_id(game['creator_id'])}\n"
        "👤 تمام پرتاب‌های شما را انجام دهید.",
        parse_mode="HTML"
    )

    await play_game(
        context,
        game_id
    )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game_callback(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    try:

        game_id = int(
            query.data.replace("cancel_", "")
        )

    except Exception:
        return

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

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
                  AND status = 'waiting'
            """, (game_id,))

            db.commit()

    try:

        change_balance(
            game["creator_id"],
            Decimal(str(game["bet"])),
            "game_refund",
            f"Cancel game {game_id}"
        )

    except Exception:

        logger.exception(
            "Cancel refund failed"
        )

    await safe_delete_message(
        context.bot,
        query.message.chat_id,
        query.message.message_id
    )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(Decimal(str(game['bet'])))} TRX "
        "به سازنده برگشت داده شد."
    )


# ============================================================
# PLAY GAME
# ============================================================

async def play_game(context, game_id):

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

        if not game:
            return

        if game["status"] != "playing":
            return

        chat_id = game["chat_id"]
        game_type = game["game_type"]
        count = int(game["count"])

        # ====================================================
        # USER / CREATOR FIRST
        # ====================================================

        creator_total, creator_rolls = await perform_rolls(
            context,
            chat_id,
            game_type,
            count
        )

        # اگر Telegram پرتاب‌ها را ناقص فرستاد
        if creator_rolls != count:

            await safe_send_message(
                context.bot,
                chat_id,
                "⚠️ یکی از پرتاب‌ها ارسال نشد.\n"
                "برای جلوگیری از خراب شدن بازی، بازی ریست شد."
            )

            await reset_single_game(
                context,
                game_id,
                automatic=True
            )

            return

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET creator_total = ?
                WHERE id = ?
                  AND status = 'playing'
            """, (
                creator_total,
                game_id
            ))

            db.commit()

        # ====================================================
        # BOT
        # ====================================================

        if game["mode"] == "bot":

            await safe_send_message(
                context.bot,
                chat_id,
                "✅ پرتاب‌های "
                f"{user_mention_by_id(game['creator_id'])}"
                " تمام شد.\n\n"
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد...",
                parse_mode="HTML"
            )

            bot_total, bot_rolls = await perform_rolls(
                context,
                chat_id,
                game_type,
                count
            )

            if bot_rolls != count:

                await safe_send_message(
                    context.bot,
                    chat_id,
                    "⚠️ پرتاب‌های ربات کامل نشد.\n"
                    "برای جلوگیری از باگ، بازی ریست شد."
                )

                await reset_single_game(
                    context,
                    game_id,
                    automatic=True
                )

                return

            with closing(get_db()) as db:

                db.execute("""
                    UPDATE games
                    SET opponent_total = ?
                    WHERE id = ?
                      AND status = 'playing'
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

        # ====================================================
        # FRIEND
        # ====================================================

        await safe_send_message(
            context.bot,
            chat_id,
            "✅ پرتاب‌های "
            f"{user_mention_by_id(game['creator_id'])}"
            " تمام شد.\n\n"
            f"🎯 حالا نوبت "
            f"{user_mention_by_id(game['opponent_id'])}"
            " است.\n"
            "👤 تمام پرتاب‌های خود را انجام دهید.",
            parse_mode="HTML"
        )

        opponent_total, opponent_rolls = await perform_rolls(
            context,
            chat_id,
            game_type,
            count
        )

        if opponent_rolls != count:

            await safe_send_message(
                context.bot,
                chat_id,
                "⚠️ پرتاب‌های بازیکن دوم کامل نشد.\n"
                "برای جلوگیری از باگ، بازی ریست شد."
            )

            await reset_single_game(
                context,
                game_id,
                automatic=True
            )

            return

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET opponent_total = ?
                WHERE id = ?
                  AND status = 'playing'
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

async def finish_game(context, game_id):

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

            if not game:
                return

            if game["status"] != "playing":
                return

            creator_total = int(game["creator_total"])
            opponent_total = int(game["opponent_total"])

            bet = Decimal(str(game["bet"]))

            # یک‌بار فقط
            db.execute("""
                UPDATE games
                SET status = 'finished'
                WHERE id = ?
                  AND status = 'playing'
            """, (game_id,))

            db.commit()

    chat_id = game["chat_id"]

    creator = user_mention_by_id(
        game["creator_id"]
    )

    if game["mode"] == "friends":

        opponent = user_mention_by_id(
            game["opponent_id"]
        )

    else:

        opponent = "🤖 ربات"

    # ========================================================
    # DRAW
    # ========================================================

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

        await safe_send_message(
            context.bot,
            chat_id,
            "🤝 بازی مساوی شد.\n\n"
            f"🎯 نتیجه:\n"
            f"{creator} {creator_total}\n"
            f"{opponent} {opponent_total}\n\n"
            "💰 مبلغ شرط برگشت داده شد.",
            parse_mode="HTML"
        )

        return

    # ========================================================
    # BOT
    # ========================================================

    if game["mode"] == "bot":

        if creator_total > opponent_total:

            payout = bet * Decimal("2")

            try:

                change_balance(
                    game["creator_id"],
                    payout,
                    "game_win",
                    f"Bot game win {game_id}"
                )

            except Exception:

                logger.exception(
                    "Bot payout failed"
                )

            result = (
                "🏆 "
                f"{creator} برنده شد!\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        else:

            result = (
                "🤖 ربات برنده شد."
            )

    # ========================================================
    # FRIEND
    # ========================================================

    else:

        payout = bet * Decimal("2")

        if creator_total > opponent_total:

            winner = game["creator_id"]
            winner_name = creator

        else:

            winner = game["opponent_id"]
            winner_name = opponent

        try:

            change_balance(
                winner,
                payout,
                "game_win",
                f"Friend game win {game_id}"
            )

        except Exception:

            logger.exception(
                "Friend payout failed"
            )

        result = (
            f"🏆 برنده: {winner_name}\n"
            f"💰 جایزه: {money(payout)} TRX"
        )

    await safe_send_message(
        context.bot,
        chat_id,
        "🏁 نتیجه بازی\n\n"
        f"{GAME_INFO[game['game_type']][0]} "
        f"{GAME_INFO[game['game_type']][1]}\n\n"
        f"👤 {creator}: {creator_total}\n"
        f"👤 {opponent}: {opponent_total}\n\n"
        f"{result}",
        parse_mode="HTML"
    )


# ============================================================
# RESET SINGLE GAME
# ============================================================

async def reset_single_game(
    context,
    game_id,
    automatic=False
):

    try:
        game_id = int(game_id)
    except Exception:
        return False

    lock = await get_game_lock(game_id)

    async with lock:

        with closing(get_db()) as db:

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (game_id,)).fetchone()

            if not game:
                return False

            status = game["status"]

            if status in (
                "finished",
                "cancelled",
                "reset"
            ):
                return False

            creator_id = game["creator_id"]
            opponent_id = game["opponent_id"]
            bet = Decimal(str(game["bet"]))
            mode = game["mode"]

            # اول وضعیت را قفل می‌کنیم
            db.execute("""
                UPDATE games
                SET status = 'reset'
                WHERE id = ?
                  AND status IN ('waiting', 'playing')
            """, (game_id,))

            db.commit()

    # برگشت شرط سازنده
    try:

        change_balance(
            creator_id,
            bet,
            "game_reset_refund",
            f"Reset game {game_id}"
        )

    except Exception:

        logger.exception(
            "Creator reset refund failed"
        )

    # اگر نفر دوم وارد شده بود، شرط او هم برگردد
    if (
        mode == "friends"
        and opponent_id
        and opponent_id != -1
    ):

        try:

            change_balance(
                opponent_id,
                bet,
                "game_reset_refund",
                f"Reset game {game_id}"
            )

        except Exception:

            logger.exception(
                "Opponent reset refund failed"
            )

    if automatic:

        logger.warning(
            "Game %s automatically reset.",
            game_id
        )

    return True


# ============================================================
# RESET COMMAND
# ============================================================

async def reset_game_command(update, context):

    user = update.effective_user

    if not user or not is_owner(user.id):

        await update.message.reply_text(
            "❌ فقط مالک می‌تواند بازی را ریست کند."
        )

        return

    args = context.args

    if not args:

        await update.message.reply_text(
            "🔄 ریست بازی\n\n"
            "برای یک بازی:\n"
            "/resetgame 123\n\n"
            "برای همه بازی‌های گیرکرده:\n"
            "/resetgame all"
        )

        return

    target = normalize_digits(args[0]).lower()

    if target == "all":

        with closing(get_db()) as db:

            rows = db.execute("""
                SELECT id
                FROM games
                WHERE status IN ('waiting', 'playing')
            """).fetchall()

        count = 0

        for row in rows:

            if await reset_single_game(
                context,
                row["id"]
            ):
                count += 1

        await update.message.reply_text(
            "✅ ریست انجام شد.\n\n"
            f"🎮 تعداد بازی‌های ریست‌شده: {count}"
        )

        return

    try:

        game_id = int(target)

    except Exception:

        await update.message.reply_text(
            "❌ شناسه بازی صحیح نیست.\n\n"
            "مثال:\n"
            "/resetgame 123"
        )

        return

    result = await reset_single_game(
        context,
        game_id
    )

    if result:

        await update.message.reply_text(
            f"✅ بازی شماره {game_id} ریست شد.\n"
            "💰 شرط‌های قفل‌شده برگشت داده شدند."
        )

    else:

        await update.message.reply_text(
            "❌ این بازی پیدا نشد یا قبلاً تمام شده است."
        )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    # --------------------------------------------------------
    # ضد دستور:
    # هر پیام فقط یک بار در این handler پردازش می‌شود.
    # بعد از هر دستور return داریم.
    # --------------------------------------------------------

    ensure_user(user)

    raw_text = message.text or ""
    text = normalize_digits(raw_text.strip())

    if not text:
        return

    # ========================================================
    # OWNER
    # ========================================================

    if is_owner(user.id):

        # ----------------------------------------------------
        # شارژ با آیدی عددی + مبلغ
        # شارژ 123456789 100
        # ----------------------------------------------------

        match_charge_id = re.match(
            r"^شارژ\s+(\d+)\s+"
            r"([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_charge_id:

            target_id = match_charge_id.group(1)

            amount = parse_amount(
                match_charge_id.group(2)
            )

            await admin_change_by_id(
                message,
                user.id,
                "charge",
                target_id,
                amount
            )

            return

        # ----------------------------------------------------
        # کسر با آیدی عددی + مبلغ
        # کسر 123456789 100
        # ----------------------------------------------------

        match_remove_id = re.match(
            r"^کسر\s+(\d+)\s+"
            r"([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_remove_id:

            target_id = match_remove_id.group(1)

            amount = parse_amount(
                match_remove_id.group(2)
            )

            await admin_change_by_id(
                message,
                user.id,
                "remove",
                target_id,
                amount
            )

            return

        # ----------------------------------------------------
        # شارژ Reply
        # ----------------------------------------------------

        match_charge_reply = re.match(
            r"^شارژ\s+"
            r"([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_charge_reply:

            amount = parse_amount(
                match_charge_reply.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )

                return

            await admin_change_by_reply(
                message,
                user.id,
                "charge",
                amount
            )

            return

        # ----------------------------------------------------
        # کسر Reply
        # ----------------------------------------------------

        match_remove_reply = re.match(
            r"^کسر\s+"
            r"([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_remove_reply:

            amount = parse_amount(
                match_remove_reply.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )

                return

            await admin_change_by_reply(
                message,
                user.id,
                "remove",
                amount
            )

            return

        # ----------------------------------------------------
        # روشن
        # ----------------------------------------------------

        if text == "روشن":

            set_bot_enabled(True)

            await message.reply_text(
                "🟢 ربات روشن شد."
            )

            return

        # ----------------------------------------------------
        # خاموش
        # ----------------------------------------------------

        if text == "خاموش":

            set_bot_enabled(False)

            await message.reply_text(
                "🔴 ربات خاموش شد."
            )

            return

        # ----------------------------------------------------
        # پنل
        # ----------------------------------------------------

        if text in (
            "پنل",
            "پنل مدیریت",
            "admin"
        ):

            await admin_panel(
                update,
                context
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
    # HELP
    # ========================================================

    if text in (
        "راهنما",
        "کمک",
        "help"
    ):

        await show_help(
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

    # ========================================================
    # UNKNOWN:
    # هیچ چیز دیگری اجرا نشود.
    # ========================================================

    return


# ============================================================
# COMMANDS
# ============================================================

async def admin_command(update, context):

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


async def balance_command(update, context):

    await show_balance(
        update,
        context
    )


async def referral_command(update, context):

    await show_referral(
        update,
        context
    )


async def help_command(update, context):

    await show_help(
        update,
        context
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    error = context.error

    if isinstance(
        error,
        (TimedOut, NetworkError)
    ):

        logger.warning(
            "Temporary Telegram error: %s",
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

    application.add_handler(
        CommandHandler(
            "resetgame",
            reset_game_command
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
