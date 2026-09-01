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
        check_same_thread=False
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

        # مهاجرت دیتابیس‌های قدیمی
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        if "creator_total" not in columns:
            try:
                db.execute(
                    "ALTER TABLE games ADD COLUMN creator_total INTEGER DEFAULT 0"
                )
            except Exception:
                pass

        if "opponent_total" not in columns:
            try:
                db.execute(
                    "ALTER TABLE games ADD COLUMN opponent_total INTEGER DEFAULT 0"
                )
            except Exception:
                pass

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
                raise ValueError(
                    "insufficient_balance"
                )

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

    return bool(
        row and row["value"] == "1"
    )


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

        except (
            TimedOut,
            NetworkError
        ) as e:

            logger.warning(
                "Network error: %s",
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

        except Exception:

            logger.exception(
                "Unexpected send error"
            )

            return None

    return None


async def safe_delete_message(
    bot,
    chat_id,
    message_id
):

    for attempt in range(3):

        try:

            await bot.delete_message(
                chat_id=chat_id,
                message_id=message_id
            )

            return True

        except (
            TimedOut,
            NetworkError
        ):

            if attempt < 2:
                await asyncio.sleep(2)

        except Exception:

            return False

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

        except (
            TimedOut,
            NetworkError
        ) as e:

            logger.warning(
                "Dice network error: %s",
                e
            )

            if attempt < 2:
                await asyncio.sleep(2)

        except TelegramError as e:

            logger.error(
                "Dice telegram error: %s",
                e
            )

            return None

        except Exception:

            logger.exception(
                "Dice error"
            )

            return None

    return None


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(
    user_id,
    context
):

    try:

        member = await context.bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator"
        )

    except Exception as e:

        logger.warning(
            "Membership check failed: %s",
            e
        )

        return False


async def require_membership(
    update,
    context
):

    user = update.effective_user

    if not user:
        return False

    if await check_membership(
        user.id,
        context
    ):
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


async def membership_callback(
    update,
    context
):

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

async def start(
    update,
    context
):

    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    # -------------------------
    # REFERRAL
    # -------------------------

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):

            try:
                referrer_id = int(
                    arg[4:]
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

                    if (
                        row
                        and row["referrer_id"] is None
                    ):

                        already = db.execute("""
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                        """, (
                            user.id,
                        )).fetchone()

                        ref_exists = db.execute("""
                            SELECT user_id
                            FROM users
                            WHERE user_id = ?
                        """, (
                            referrer_id,
                        )).fetchone()

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

async def show_balance(
    update,
    context
):

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

        await update.message.reply_text(
            text
        )


# ============================================================
# REFERRAL
# ============================================================

async def show_referral(
    update,
    context
):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    try:

        bot = await context.bot.get_me()

        link = (
            f"https://t.me/"
            f"{bot.username}"
            f"?start=ref_{user.id}"
        )

    except Exception:

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

        await update.message.reply_text(
            text
        )


# ============================================================
# HELP
# ============================================================

async def show_help(
    update,
    context
):

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

        "🤖 بازی با ربات:\n"
        "اول تمام پرتاب‌های کاربر انجام می‌شود، "
        "بعد تمام پرتاب‌های ربات.\n\n"

        "👥 بازی دوستان:\n"
        "ابتدا سازنده تمام پرتاب‌ها، "
        "بعد نفر دوم."
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

        await update.message.reply_text(
            text
        )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(
    update,
    context,
    amount
):

    user = update.effective_user
    message = update.message

    if not message.reply_to_message:

        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    target = (
        message.reply_to_message.from_user
    )

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

        change_balance(
            user.id,
            -amount,
            "transfer_out",
            f"To {target.id}"
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

    try:

        change_balance(
            target.id,
            amount,
            "transfer_in",
            f"From {user.id}"
        )

    except Exception:

        try:

            change_balance(
                user.id,
                amount,
                "transfer_rollback",
                f"Rollback {target.id}"
            )

        except Exception:

            logger.exception(
                "Transfer rollback failed"
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

async def admin_panel(
    update,
    context
):

    user = update.effective_user

    if not user or not is_owner(user.id):

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

        "➕ شارژ از گپ:\n"
        "Reply + شارژ 100\n"
        "یا:\n"
        "شارژ USER_ID 100\n\n"

        "➖ کسر از گپ:\n"
        "Reply + کسر 100\n"
        "یا:\n"
        "کسر USER_ID 100\n\n"

        "از دکمه‌های شارژ/کسر هم می‌توانی "
        "آیدی و مبلغ را بفرستی.",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN BY ID
# ============================================================

async def admin_change_by_id(
    update,
    context,
    operation,
    target_id,
    amount
):

    owner = update.effective_user

    if not owner or not is_owner(owner.id):
        return

    try:

        target_id = int(target_id)

    except Exception:

        await update.message.reply_text(
            "❌ آیدی عددی صحیح نیست."
        )

        return

    amount = parse_amount(amount)

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ صحیح نیست."
        )

        return

    if target_id == owner.id:

        await update.message.reply_text(
            "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
        )

        return

    # اگر کاربر در دیتابیس نبود،
    # برای شارژ/کسر با ID ساخته می‌شود.
    ensure_user_id(target_id)

    try:

        if operation == "charge":

            change_balance(
                target_id,
                amount,
                "admin_charge",
                f"Owner {owner.id}"
            )

            title = "شارژ"

        elif operation == "remove":

            change_balance(
                target_id,
                -amount,
                "admin_remove",
                f"Owner {owner.id}"
            )

            title = "کسر"

        else:

            await update.message.reply_text(
                "❌ عملیات نامعتبر."
            )

            return

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await update.message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست.\n\n"
                f"💳 موجودی فعلی: "
                f"{money(get_balance(target_id))} TRX"
            )

        else:

            await update.message.reply_text(
                "❌ عملیات انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "Admin balance change failed"
        )

        await update.message.reply_text(
            "❌ خطا هنگام تغییر موجودی."
        )

        return

    await update.message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update,
    context
):

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

    # --------------------------------------------------------
    # ON
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # OFF
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # CHARGE
    # --------------------------------------------------------

    if data == "admin_charge":

        context.user_data[
            "admin_operation"
        ] = "charge"

        await query.message.reply_text(
            "➕ شارژ موجودی\n\n"
            "آیدی عددی + مبلغ را بفرست:\n\n"
            "مثال:\n"
            "8552447077 100\n\n"
            "یا اعداد فارسی:\n"
            "8552447077 ۱۰۰\n\n"
            "در گپ همچنین می‌توانی Reply کنی و بنویسی:\n"
            "شارژ 100"
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        context.user_data[
            "admin_operation"
        ] = "remove"

        await query.message.reply_text(
            "➖ کسر موجودی\n\n"
            "آیدی عددی + مبلغ را بفرست:\n\n"
            "مثال:\n"
            "8552447077 100\n\n"
            "یا اعداد فارسی:\n"
            "8552447077 ۱۰۰\n\n"
            "در گپ همچنین می‌توانی Reply کنی و بنویسی:\n"
            "کسر 100"
        )

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

async def user_callback(
    update,
    context
):

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
# GAME CONFIG
# ============================================================

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

GAME_INFO = {
    "dice": ("🎲", "تاس"),
    "darts": ("🎯", "دارت"),
    "bowling": ("🎳", "بولینگ"),
    "basketball": ("🏀", "بسکتبال"),
}

GAME_EMOJI = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


# ============================================================
# PARSE GAME
# ============================================================

def parse_game(text):

    text = normalize_digits(
        text.strip()
    )

    pattern = (
        r"^(\d+)\s+"
        r"([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$"
    )

    match = re.match(
        pattern,
        text,
        re.IGNORECASE
    )

    if not match:
        return None

    count = int(
        match.group(1)
    )

    name = match.group(2).lower()

    bet = parse_amount(
        match.group(3)
    )

    if count < 1:
        return None

    if count > MAX_GAME_COUNT:
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

    # ضد ساخت چند بازی همزمان توسط یک کاربر
    with closing(get_db()) as db:

        active = db.execute("""
            SELECT id
            FROM games
            WHERE creator_id = ?
            AND status IN ('waiting', 'playing')
            LIMIT 1
        """, (
            user.id,
        )).fetchone()

    if active:

        await update.message.reply_text(
            "❌ شما یک بازی فعال دارید.\n"
            "ابتدا آن بازی را تمام یا لغو کنید."
        )

        return

    current_balance = get_balance(
        user.id
    )

    if current_balance < bet:

        await update.message.reply_text(
            "❌ موجودی شما کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{money(current_balance)} TRX"
        )

        return

    # کسر شرط قبل از ساخت بازی
    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type}"
        )

    except ValueError:

        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )

        return

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

    emoji, name = GAME_INFO[
        game_type
    ]

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
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: "
        f"{user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )


# ============================================================
# ROLL
# ============================================================

async def perform_rolls(
    context,
    chat_id,
    game_type,
    count
):

    total = 0
    successful = 0

    for _ in range(count):

        msg = await safe_send_dice(
            context.bot,
            chat_id,
            GAME_EMOJI[game_type]
        )

        if msg is None:
            continue

        try:

            value = int(
                msg.dice.value
            )

            total += value
            successful += 1

        except Exception:

            pass

        await asyncio.sleep(1.0)

    return total, successful


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game_callback(
    update,
    context
):

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
            query.data.replace(
                "join_",
                ""
            )
        )

    except Exception:
        return

    ensure_user(user)

    # اتمیک گرفتن بازی
    with closing(get_db()) as db:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

            if not game:

                db.rollback()

                await query.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if game["status"] != "waiting":

                db.rollback()

                await query.answer(
                    "❌ این بازی دیگر فعال نیست.",
                    show_alert=True
                )

                return

            if game["creator_id"] == user.id:

                db.rollback()

                await query.answer(
                    "❌ نمی‌توانید با خودتان بازی کنید.",
                    show_alert=True
                )

                return

            bet = Decimal(
                game["bet"]
            )

            current = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                user.id,
            )).fetchone()

            if not current:

                db.rollback()

                await query.answer(
                    "❌ کاربر ثبت نشده است.",
                    show_alert=True
                )

                return

            balance = Decimal(
                current["balance"]
            )

            if balance < bet:

                db.rollback()

                await query.answer(
                    "❌ موجودی شما کافی نیست.",
                    show_alert=True
                )

                return

            new_balance = balance - bet

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_balance),
                user.id
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
                "game_bet",
                str(-bet),
                f"Join game {game_id}"
            ))

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
                db.rollback()
            except Exception:
                pass

            logger.exception(
                "Join game failed"
            )

            await query.answer(
                "❌ ورود به بازی انجام نشد.",
                show_alert=True
            )

            return

    emoji, name = GAME_INFO[
        game["game_type"]
    ]

    # حذف پیام قبلی
    try:

        await query.message.delete()

    except Exception:

        try:

            await query.edit_message_text(
                "🎮 بازی شروع شد!"
            )

        except Exception:
            pass

    # پیام جدید با تگ نفر اول
    creator_tag = format_user_mention(
        game["creator_id"],
        "سازنده"
    )

    opponent_tag = format_user_mention(
        user.id,
        "بازیکن دوم"
    )

    start_message = await safe_send_message(
        context.bot,
        game["chat_id"],
        "🎮 بازی دوستان شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} TRX\n\n"
        f"👤 {creator_tag}\n"
        f"👤 {opponent_tag}\n\n"
        f"🎯 نوبت {creator_tag} است.\n"
        "تمام پرتاب‌های خود را انجام دهید."
        ,
        parse_mode="HTML"
    )

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
            query.data.replace(
                "bot_",
                ""
            )
        )

    except Exception:
        return

    with closing(get_db()) as db:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

            if not game:

                db.rollback()

                await query.answer(
                    "❌ بازی پیدا نشد.",
                    show_alert=True
                )

                return

            if game["status"] != "waiting":

                db.rollback()

                await query.answer(
                    "❌ بازی فعال نیست.",
                    show_alert=True
                )

                return

            if game["creator_id"] != user.id:

                db.rollback()

                await query.answer(
                    "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                    show_alert=True
                )

                return

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

        except Exception:

            try:
                db.rollback()
            except Exception:
                pass

            logger.exception(
                "Bot game start failed"
            )

            return

    emoji, name = GAME_INFO[
        game["game_type"]
    ]

    # حذف پیام قبلی
    try:

        await query.message.delete()

    except Exception:
        pass

    creator_tag = format_user_mention(
        user.id,
        "کاربر"
    )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
        f"🎯 نوبت {creator_tag} است.\n"
        "تمام پرتاب‌های خود را انجام دهید."
        ,
        parse_mode="HTML"
    )

    await play_game(
        context,
        game_id
    )


# ============================================================
# CANCEL GAME
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

    # ضد اجرای دوباره در حافظه
    running = context.application.bot_data.setdefault(
        "running_games",
        set()
    )

    if game_id in running:
        logger.warning(
            "Game %s already running",
            game_id
        )
        return

    running.add(game_id)

    try:

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

        creator_tag = format_user_mention(
            game["creator_id"],
            "کاربر"
        )

        # ====================================================
        # USER FIRST
        # ====================================================

        player_total, successful = await perform_rolls(
            context,
            chat_id,
            game_type,
            count
        )

        # اگر تلگرام یکی از پرتاب‌ها را نفرستاد،
        # بازی را به نتیجه ناقص تبدیل نکن.
        if successful != count:

            await safe_send_message(
                context.bot,
                chat_id,
                "⚠️ یکی از پرتاب‌ها ارسال نشد.\n"
                "بازی به دلیل خطای تلگرام لغو و شرط برگردانده شد."
            )

            await reset_single_game(
                context,
                game_id,
                refund=True
            )

            return

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET creator_total = ?
                WHERE id = ?
                AND status = 'playing'
            """, (
                player_total,
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
                f"✅ پرتاب‌های {creator_tag} تمام شد.\n\n"
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد...",
                parse_mode="HTML"
            )

            bot_total, bot_successful = await perform_rolls(
                context,
                chat_id,
                game_type,
                count
            )

            if bot_successful != count:

                await safe_send_message(
                    context.bot,
                    chat_id,
                    "⚠️ پرتاب ربات کامل نشد.\n"
                    "بازی لغو و شرط کاربر برگشت داده شد."
                )

                await reset_single_game(
                    context,
                    game_id,
                    refund=True
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

        opponent_id = game["opponent_id"]

        opponent_tag = format_user_mention(
            opponent_id,
            "بازیکن دوم"
        )

        await safe_send_message(
            context.bot,
            chat_id,
            f"✅ پرتاب‌های {creator_tag} تمام شد.\n\n"
            f"🎯 حالا نوبت {opponent_tag} است.\n"
            "تمام پرتاب‌های خود را انجام دهید.",
            parse_mode="HTML"
        )

        opponent_total, opponent_successful = await perform_rolls(
            context,
            chat_id,
            game_type,
            count
        )

        if opponent_successful != count:

            await safe_send_message(
                context.bot,
                chat_id,
                "⚠️ پرتاب‌های بازیکن دوم کامل نشد.\n"
                "بازی لغو و شرط هر دو نفر برگشت داده شد."
            )

            await reset_single_game(
                context,
                game_id,
                refund=True
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

    finally:

        running.discard(game_id)


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(
    context,
    game_id
):

    with closing(get_db()) as db:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

            if not game:
                db.rollback()
                return

            if game["status"] != "playing":
                db.rollback()
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

            # ضد دوباره تسویه
            db.execute("""
                UPDATE games
                SET status = 'finished'
                WHERE id = ?
                AND status = 'playing'
            """, (
                game_id,
            ))

            if db.total_changes != 1:

                db.rollback()
                return

            db.commit()

        except Exception:

            try:
                db.rollback()
            except Exception:
                pass

            logger.exception(
                "Finish game lock failed"
            )

            return

    chat_id = game["chat_id"]

    creator_tag = format_user_mention(
        game["creator_id"],
        "کاربر"
    )

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
            f"🎯 نتیجه: "
            f"{creator_total} - "
            f"{opponent_total}\n\n"
            "💰 مبلغ شرط برگشت داده شد."
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

            await safe_send_message(
                context.bot,
                chat_id,
                "🏁 نتیجه بازی\n\n"
                "🤖 بازی با ربات\n\n"
                f"👤 {creator_tag}: {creator_total}\n"
                f"🤖 ربات: {opponent_total}\n\n"
                "🏆 برنده: "
                f"{creator_tag}\n"
                f"💰 جایزه: {money(payout)} TRX",
                parse_mode="HTML"
            )

        else:

            await safe_send_message(
                context.bot,
                chat_id,
                "🏁 نتیجه بازی\n\n"
                "🤖 بازی با ربات\n\n"
                f"👤 {creator_tag}: {creator_total}\n"
                f"🤖 ربات: {opponent_total}\n\n"
                "🤖 ربات برنده شد.",
                parse_mode="HTML"
            )

        return

    # ========================================================
    # FRIEND
    # ========================================================

    payout = bet * Decimal("2")

    if creator_total > opponent_total:

        winner_id = game["creator_id"]
        winner_tag = format_user_mention(
            winner_id,
            "کاربر"
        )

    else:

        winner_id = game["opponent_id"]
        winner_tag = format_user_mention(
            winner_id,
            "کاربر"
        )

    try:

        change_balance(
            winner_id,
            payout,
            "game_win",
            f"Friend game win {game_id}"
        )

    except Exception:

        logger.exception(
            "Friend payout failed"
        )

    opponent_tag = format_user_mention(
        game["opponent_id"],
        "کاربر"
    )

    await safe_send_message(
        context.bot,
        chat_id,
        "🏁 نتیجه بازی\n\n"
        f"👤 {creator_tag}: {creator_total}\n"
        f"👤 {opponent_tag}: {opponent_total}\n\n"
        f"🏆 برنده: {winner_tag}\n"
        f"💰 جایزه: {money(payout)} TRX",
        parse_mode="HTML"
    )


# ============================================================
# RESET SINGLE GAME
# ============================================================

async def reset_single_game(
    context,
    game_id,
    refund=True
):

    with closing(get_db()) as db:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

            game = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

            if not game:

                db.rollback()
                return False

            if game["status"] not in (
                "waiting",
                "playing"
            ):

                db.rollback()
                return False

            db.execute("""
                UPDATE games
                SET status = 'reset'
                WHERE id = ?
            """, (
                game_id,
            ))

            db.commit()

        except Exception:

            try:
                db.rollback()
            except Exception:
                pass

            logger.exception(
                "Reset game failed"
            )

            return False

    if refund:

        try:

            bet = Decimal(
                game["bet"]
            )

            # سازنده همیشه شرط داده
            change_balance(
                game["creator_id"],
                bet,
                "game_reset_refund",
                f"Reset game {game_id}"
            )

            # اگر دوست وارد شده، شرط او هم برگشت
            if (
                game["mode"] == "friends"
                and game["opponent_id"]
                and game["opponent_id"] > 0
            ):

                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_reset_refund",
                    f"Reset game {game_id}"
                )

        except Exception:

            logger.exception(
                "Reset refund failed"
            )

            return False

    return True


# ============================================================
# RESET COMMAND
# ============================================================

async def reset_game_command(
    update,
    context
):

    user = update.effective_user

    if not user or not is_owner(user.id):

        await update.message.reply_text(
            "❌ فقط مالک می‌تواند بازی را ریست کند."
        )

        return

    args = context.args

    if not args:

        await update.message.reply_text(
            "♻️ ریست بازی\n\n"
            "استفاده:\n"
            "/resetgame GAME_ID\n\n"
            "مثال:\n"
            "/resetgame 15\n\n"
            "برای ریست همه بازی‌های فعال:\n"
            "/resetgame all"
        )

        return

    target = normalize_digits(
        args[0]
    ).lower()

    # --------------------------------------------------------
    # ALL
    # --------------------------------------------------------

    if target == "all":

        with closing(get_db()) as db:

            games = db.execute("""
                SELECT id
                FROM games
                WHERE status IN ('waiting', 'playing')
            """).fetchall()

        count = 0

        for row in games:

            if await reset_single_game(
                context,
                row["id"],
                refund=True
            ):

                count += 1

        await update.message.reply_text(
            f"♻️ ریست انجام شد.\n\n"
            f"🎮 تعداد بازی‌های ریست‌شده: {count}"
        )

        return

    # --------------------------------------------------------
    # ONE GAME
    # --------------------------------------------------------

    try:

        game_id = int(target)

    except Exception:

        await update.message.reply_text(
            "❌ GAME_ID صحیح نیست."
        )

        return

    success = await reset_single_game(
        context,
        game_id,
        refund=True
    )

    if success:

        await update.message.reply_text(
            f"♻️ بازی {game_id} ریست شد.\n"
            "💰 شرط‌های بازی برگشت داده شد."
        )

    else:

        await update.message.reply_text(
            "❌ بازی پیدا نشد یا قبلاً تمام/ریست شده است."
        )


# ============================================================
# FORMAT USER TAG
# ============================================================

def format_user_mention(
    user_id,
    fallback="کاربر"
):

    try:

        user_id = int(user_id)

        with closing(get_db()) as db:

            row = db.execute("""
                SELECT first_name, username
                FROM users
                WHERE user_id = ?
            """, (
                user_id,
            )).fetchone()

        if row:

            name = (
                row["first_name"]
                or row["username"]
                or fallback
            )

        else:

            name = fallback

    except Exception:

        name = fallback

    safe_name = (
        str(name)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )

    return (
        f'<a href="tg://user?id={user_id}">'
        f'{safe_name}'
        f'</a>'
    )


# ============================================================
# ADMIN TEXT
# ============================================================

async def handle_admin_id_amount(
    update,
    context,
    text
):

    user = update.effective_user

    if not user or not is_owner(user.id):
        return False

    operation = context.user_data.get(
        "admin_operation"
    )

    if operation not in (
        "charge",
        "remove"
    ):
        return False

    match = re.match(
        r"^(\d+)\s+([0-9]+(?:\.[0-9]+)?)$",
        text
    )

    if not match:
        return False

    target_id = int(
        match.group(1)
    )

    amount = parse_amount(
        match.group(2)
    )

    context.user_data.pop(
        "admin_operation",
        None
    )

    await admin_change_by_id(
        update,
        context,
        operation,
        target_id,
        amount
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
    # ADMIN PANEL ID + AMOUNT
    # ========================================================

    if is_owner(user.id):

        handled = await handle_admin_id_amount(
            update,
            context,
            text
        )

        if handled:
            return

    # ========================================================
    # ADMIN CHARGE WITH REPLY OR ID
    # ========================================================

    if is_owner(user.id):

        match_charge = re.match(
            r"^شارژ\s+([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_charge:

            amount = parse_amount(
                match_charge.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )

                return

            # Reply
            if message.reply_to_message:

                target = (
                    message.reply_to_message.from_user
                )

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

                if target.id == user.id:

                    await message.reply_text(
                        "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
                    )

                    return

                ensure_user(target)

                try:

                    change_balance(
                        target.id,
                        amount,
                        "admin_charge",
                        f"Owner {user.id}"
                    )

                except Exception:

                    logger.exception(
                        "Reply charge failed"
                    )

                    await message.reply_text(
                        "❌ شارژ انجام نشد."
                    )

                    return

                await message.reply_text(
                    "✅ شارژ انجام شد.\n\n"
                    f"🆔 آیدی: {target.id}\n"
                    f"👤 کاربر: "
                    f"{target.first_name or target.id}\n"
                    f"💰 مبلغ: {money(amount)} TRX\n"
                    f"💳 موجودی جدید: "
                    f"{money(get_balance(target.id))} TRX"
                )

                return

            await message.reply_text(
                "❌ برای «شارژ 100» باید روی پیام کاربر Reply کنی.\n\n"
                "یا بدون Reply بنویس:\n"
                "شارژ USER_ID 100"
            )

            return

    # ========================================================
    # ADMIN REMOVE WITH REPLY
    # ========================================================

    if is_owner(user.id):

        match_remove = re.match(
            r"^کسر\s+([0-9]+(?:\.[0-9]+)?)$",
            text
        )

        if match_remove:

            amount = parse_amount(
                match_remove.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )

                return

            if message.reply_to_message:

                target = (
                    message.reply_to_message.from_user
                )

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

                if target.id == user.id:

                    await message.reply_text(
                        "❌ تغییر موجودی مالک از این مسیر مجاز نیست."
                    )

                    return

                ensure_user(target)

                try:

                    change_balance(
                        target.id,
                        -amount,
                        "admin_remove",
                        f"Owner {user.id}"
                    )

                except ValueError as e:

                    if str(e) == "insufficient_balance":

                        await message.reply_text(
                            "❌ موجودی کاربر کافی نیست.\n\n"
                            f"💳 موجودی فعلی: "
                            f"{money(get_balance(target.id))} TRX"
                        )

                    else:

                        await message.reply_text(
                            "❌ کسر انجام نشد."
                        )

                    return

                except Exception:

                    logger.exception(
                        "Reply remove failed"
                    )

                    await message.reply_text(
                        "❌ کسر انجام نشد."
                    )

                    return

                await message.reply_text(
                    "✅ کسر انجام شد.\n\n"
                    f"🆔 آیدی: {target.id}\n"
                    f"👤 کاربر: "
                    f"{target.first_name or target.id}\n"
                    f"💰 مبلغ: {money(amount)} TRX\n"
                    f"💳 موجودی جدید: "
                    f"{money(get_balance(target.id))} TRX"
                )

                return

            await message.reply_text(
                "❌ برای «کسر 100» باید روی پیام کاربر Reply کنی.\n\n"
                "یا بدون Reply بنویس:\n"
                "کسر USER_ID 100"
            )

            return

    # ========================================================
    # ON / OFF
    # ========================================================

    if is_owner(user.id):

        if text == "روشن":

            set_bot_enabled(True)

            await message.reply_text(
                "🟢 ربات روشن شد."
            )

            return

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
            r"^انتقال\s+([0-9]+(?:\.[0-9]+)?)$",
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

        if not await require_membership(
            update,
            context
        ):
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
# ERROR
# ============================================================

async def error_handler(
    update,
    context
):

    error = context.error

    if isinstance(
        error,
        (
            TimedOut,
            NetworkError
        )
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

    application.bot_data[
        "running_games"
    ] = set()

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
    # MEMBERSHIP
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    # ========================================================
    # ADMIN
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|charge|remove|stats)$"
        )
    )

    # ========================================================
    # USER
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern=r"^user_(balance|ref|help)$"
        )
    )

    # ========================================================
    # GAMES
    # ========================================================

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
