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
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',

                creator_total INTEGER DEFAULT 0,
                opponent_total INTEGER DEFAULT 0,

                creator_rolls INTEGER DEFAULT 0,
                opponent_rolls INTEGER DEFAULT 0,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        # اگر دیتابیس قبلی باشد و ستون‌ها وجود نداشته باشند
        columns = {
            row["name"]
            for row in db.execute(
                "PRAGMA table_info(games)"
            ).fetchall()
        }

        if "creator_rolls" not in columns:
            db.execute("""
                ALTER TABLE games
                ADD COLUMN creator_rolls INTEGER DEFAULT 0
            """)

        if "opponent_rolls" not in columns:
            db.execute("""
                ALTER TABLE games
                ADD COLUMN opponent_rolls INTEGER DEFAULT 0
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
            "creator",
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
    enabled = bot_enabled()

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

                        already = db.execute("""
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                        """, (
                            user.id,
                        )).fetchone()

                        if not already:

                            ref_exists = db.execute("""
                                SELECT user_id
                                FROM users
                                WHERE user_id = ?
                            """, (
                                referrer_id,
                            )).fetchone()

                            if ref_exists:

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

    else:

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
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "انتقال 0.1\n"
        "انتقال ۰.۱\n\n"

        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "2 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "3 بسکتبال 0.1\n\n"

        "🤖 بازی با ربات:\n"
        "بعد از شروع بازی، خود کاربر باید "
        "ایموجی بازی را بفرستد.\n"
        "مثلاً برای تاس: 🎲\n\n"

        "👥 بازی دوستان:\n"
        "سازنده خودش پرتاب می‌کند، "
        "بعد بازیکن دوم."
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

    # --------------------------------------------------------
    # انتقال اتمیک
    # --------------------------------------------------------

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            sender_row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                user.id,
            )).fetchone()

            target_row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                target.id,
            )).fetchone()

            if not sender_row or not target_row:
                raise ValueError("user_not_found")

            sender_balance = Decimal(
                sender_row["balance"]
            )

            target_balance = Decimal(
                target_row["balance"]
            )

            if sender_balance < amount:
                raise ValueError(
                    "insufficient_balance"
                )

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(sender_balance - amount),
                user.id
            ))

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(target_balance + amount),
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
                    f"{money(get_balance(user.id))} TRX"
                )

            else:

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
        "روی دکمه شارژ بزنید و سپس در PV ربات "
        "به صورت زیر بفرستید:\n\n"
        "ID مبلغ\n"
        "مثال:\n"
        "8552447077 100\n\n"

        "➖ کسر نیز به همین شکل است.\n"
        "در گپ هم می‌توانید با Reply استفاده کنید:\n"
        "شارژ 100\n"
        "کسر 100",
        reply_markup=admin_keyboard()
    )


# ============================================================
# ADMIN PRIVATE CHANGE
# ============================================================

async def admin_private_change(
    update,
    context,
    operation
):
    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return

    text = normalize_digits(
        message.text or ""
    ).strip()

    parts = text.split()

    if len(parts) != 2:

        await message.reply_text(
            "❌ فرمت صحیح:\n\n"
            "ID مبلغ\n\n"
            "مثال:\n"
            "8552447077 100"
        )

        return

    try:
        target_id = int(parts[0])
    except Exception:

        await message.reply_text(
            "❌ آیدی عددی صحیح نیست."
        )

        return

    amount = parse_amount(parts[1])

    if amount is None:

        await message.reply_text(
            "❌ مبلغ صحیح نیست."
        )

        return

    # مالک هم می‌تواند کاربر مقصد باشد،
    # اما عملیات فقط توسط مالک انجام می‌شود.

    with closing(get_db()) as db:

        row = db.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
        """, (
            target_id,
        )).fetchone()

    if not row:

        await message.reply_text(
            "❌ این کاربر هنوز در ربات ثبت نشده است."
        )

        return

    try:

        if operation == "charge":

            change_balance(
                target_id,
                amount,
                "admin_charge",
                f"Owner {user.id}"
            )

            title = "شارژ"

        else:

            change_balance(
                target_id,
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
            "Admin private balance change failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"🆔 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )


# ============================================================
# ADMIN GROUP REPLY CHANGE
# ============================================================

async def admin_group_change(
    update,
    context,
    operation,
    amount
):
    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):

        return

    if not message.reply_to_message:

        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنید."
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
            "Group admin balance change failed"
        )

        await message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: "
        f"{target.first_name or target.id}\n"
        f"🆔 آیدی: {target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
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
            "admin_private_operation"
        ] = "charge"

        try:
            await query.message.reply_text(
                "➕ شارژ موجودی\n\n"
                "آیدی عددی کاربر و مبلغ را بفرستید:\n\n"
                "ID مبلغ\n\n"
                "مثال:\n"
                "8552447077 100\n\n"
                "در گپ هم می‌توانید روی پیام کاربر "
                "Reply کنید و بنویسید:\n"
                "شارژ 100"
            )
        except Exception:
            pass

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        context.user_data[
            "admin_private_operation"
        ] = "remove"

        try:
            await query.message.reply_text(
                "➖ کسر موجودی\n\n"
                "آیدی عددی کاربر و مبلغ را بفرستید:\n\n"
                "ID مبلغ\n\n"
                "مثال:\n"
                "8552447077 100\n\n"
                "در گپ هم می‌توانید روی پیام کاربر "
                "Reply کنید و بنویسید:\n"
                "کسر 100"
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
        r"^(\d+)\s+"
        r"([^\s]+)\s+"
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

    if get_balance(user.id) < bet:

        await update.message.reply_text(
            "❌ موجودی شما کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{money(get_balance(user.id))} TRX"
        )

        return

    # --------------------------------------------------------
    # Deduct creator stake
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # Create waiting game
    # --------------------------------------------------------

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
                opponent_total,
                creator_rolls,
                opponent_rolls
            )
            VALUES (
                ?, ?, ?, ?, ?,
                'friends',
                'waiting',
                0, 0, 0, 0
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
        f"🔢 تعداد پرتاب: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: "
        f"{user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )


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

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

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

            row = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                user.id,
            )).fetchone()

            if not row:

                db.rollback()

                await query.answer(
                    "❌ کاربر پیدا نشد.",
                    show_alert=True
                )

                return

            balance = Decimal(
                row["balance"]
            )

            if balance < bet:

                db.rollback()

                await query.answer(
                    "❌ موجودی شما کافی نیست.",
                    show_alert=True
                )

                return

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(balance - bet),
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
            """, (
                user.id,
                game_id
            ))

            db.commit()

        except Exception:

            db.rollback()

            logger.exception(
                "Join game failed"
            )

            await query.answer(
                "❌ شروع بازی انجام نشد.",
                show_alert=True
            )

            return

    emoji, name = GAME_INFO[
        game["game_type"]
    ]

    try:

        await query.edit_message_text(
            "🎮 بازی شروع شد!\n\n"
            f"{emoji} {name}\n"
            f"🔢 تعداد پرتاب: "
            f"{game['count']}\n"
            f"💰 شرط هر نفر: "
            f"{money(bet)} TRX\n\n"
            "🎯 سازنده باید خودش "
            "ایموجی بازی را بفرستد.\n\n"
            f"مثال: {emoji}\n"
            f"تعداد پرتاب لازم: {game['count']}"
        )

    except Exception:
        pass

    # هیچ پرتابی از طرف ربات انجام نمی‌شود.
    # کاربر باید خودش ایموجی را بفرستد.


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

            await query.answer(
                "❌ بازی فعال نیست.",
                show_alert=True
            )

            return

        if game["creator_id"] != user.id:

            await query.answer(
                "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                show_alert=True
            )

            return

        db.execute("""
            UPDATE games
            SET opponent_id = -1,
                mode = 'bot',
                status = 'playing',
                creator_rolls = 0,
                opponent_rolls = 0,
                creator_total = 0,
                opponent_total = 0
            WHERE id = ?
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
            f"🔢 تعداد پرتاب: "
            f"{game['count']}\n"
            f"💰 شرط: "
            f"{money(Decimal(game['bet']))} TRX\n\n"
            "🎯 حالا خودت بازی را انجام بده.\n"
            f"ایموجی {emoji} را "
            f"{game['count']} بار بفرست.\n\n"
            "⚠️ ربات به‌جای تو پرتاب نمی‌کند."
        )

    except Exception:
        pass


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

        try:

            db.execute("BEGIN IMMEDIATE")

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

            if game["status"] != "waiting":

                db.rollback()

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

                db.rollback()

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

        except Exception:

            db.rollback()

            logger.exception(
                "Cancel game failed"
            )

            return

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
# INCOMING USER DICE / GAME MESSAGE
# ============================================================

async def incoming_game_roll(
    update,
    context
):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    if message.chat.type not in (
        ChatType.GROUP,
        ChatType.SUPERGROUP
    ):
        return

    if not message.dice:
        return

    emoji = message.dice.emoji
    value = int(message.dice.value)

    emoji_to_game = {
        "🎲": "dice",
        "🎯": "darts",
        "🎳": "bowling",
        "🏀": "basketball",
    }

    if emoji not in emoji_to_game:
        return

    game_type = emoji_to_game[emoji]

    ensure_user(user)

    # --------------------------------------------------------
    # پیدا کردن بازی فعال مربوط به این کاربر
    # --------------------------------------------------------

    with closing(get_db()) as db:

        games = db.execute("""
            SELECT *
            FROM games
            WHERE chat_id = ?
              AND status = 'playing'
              AND (
                    creator_id = ?
                    OR opponent_id = ?
              )
            ORDER BY id DESC
        """, (
            message.chat.id,
            user.id,
            user.id
        )).fetchall()

    if not games:
        return

    game = None

    for candidate in games:

        if candidate["game_type"] != game_type:
            continue

        # BOT:
        # فقط سازنده اجازه دارد پرتاب کند
        if candidate["mode"] == "bot":

            if candidate["creator_id"] == user.id:
                game = candidate
                break

        # FRIENDS:
        else:

            if (
                candidate["creator_id"] == user.id
                or candidate["opponent_id"] == user.id
            ):
                game = candidate
                break

    if not game:
        return

    # --------------------------------------------------------
    # ثبت پرتاب اتمیک
    # --------------------------------------------------------

    game_id = game["id"]

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            current = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

            if not current:

                db.rollback()
                return

            if current["status"] != "playing":

                db.rollback()
                return

            count = int(
                current["count"]
            )

            # ------------------------------------------------
            # BOT GAME
            # ------------------------------------------------

            if current["mode"] == "bot":

                if current["creator_id"] != user.id:

                    db.rollback()
                    return

                rolls = int(
                    current["creator_rolls"]
                )

                if rolls >= count:

                    db.rollback()
                    return

                new_rolls = rolls + 1

                new_total = (
                    int(current["creator_total"])
                    + value
                )

                db.execute("""
                    UPDATE games
                    SET creator_rolls = ?,
                        creator_total = ?
                    WHERE id = ?
                """, (
                    new_rolls,
                    new_total,
                    game_id
                ))

                db.commit()

                remaining = count - new_rolls

            # ------------------------------------------------
            # FRIEND GAME - CREATOR
            # ------------------------------------------------

            elif current["creator_id"] == user.id:

                rolls = int(
                    current["creator_rolls"]
                )

                # سازنده باید تمام پرتاب‌ها را قبل از نفر دوم بزند
                if rolls >= count:

                    db.rollback()
                    return

                new_rolls = rolls + 1

                new_total = (
                    int(current["creator_total"])
                    + value
                )

                db.execute("""
                    UPDATE games
                    SET creator_rolls = ?,
                        creator_total = ?
                    WHERE id = ?
                """, (
                    new_rolls,
                    new_total,
                    game_id
                ))

                db.commit()

                remaining = count - new_rolls

            # ------------------------------------------------
            # FRIEND GAME - OPPONENT
            # ------------------------------------------------

            elif current["opponent_id"] == user.id:

                creator_rolls = int(
                    current["creator_rolls"]
                )

                if creator_rolls < count:

                    db.rollback()
                    return

                rolls = int(
                    current["opponent_rolls"]
                )

                if rolls >= count:

                    db.rollback()
                    return

                new_rolls = rolls + 1

                new_total = (
                    int(current["opponent_total"])
                    + value
                )

                db.execute("""
                    UPDATE games
                    SET opponent_rolls = ?,
                        opponent_total = ?
                    WHERE id = ?
                """, (
                    new_rolls,
                    new_total,
                    game_id
                ))

                db.commit()

                remaining = count - new_rolls

            else:

                db.rollback()
                return

        except Exception:

            db.rollback()

            logger.exception(
                "Incoming game roll failed"
            )

            return

    # --------------------------------------------------------
    # BOT GAME: USER FINISHED
    # --------------------------------------------------------

    if game["mode"] == "bot":

        with closing(get_db()) as db:

            updated = db.execute("""
                SELECT *
                FROM games
                WHERE id = ?
            """, (
                game_id,
            )).fetchone()

        if not updated:
            return

        creator_rolls = int(
            updated["creator_rolls"]
        )

        if creator_rolls < int(
            updated["count"]
        ):

            await safe_send_message(
                context.bot,
                message.chat.id,
                "🎯 پرتاب ثبت شد.\n"
                f"تعداد باقی‌مانده: "
                f"{int(updated['count']) - creator_rolls}"
            )

            return

        # کاربر تمام پرتاب‌ها را انجام داده
        await safe_send_message(
            context.bot,
            message.chat.id,
            "✅ پرتاب‌های شما تمام شد.\n\n"
            "🤖 حالا نوبت ربات است..."
        )

        # ----------------------------------------------------
        # ربات اکنون پرتاب می‌کند
        # ----------------------------------------------------

        bot_total = 0

        for _ in range(
            int(updated["count"])
        ):

            bot_msg = await safe_send_dice(
                context.bot,
                message.chat.id,
                GAME_EMOJI[
                    updated["game_type"]
                ]
            )

            if bot_msg and bot_msg.dice:

                bot_total += int(
                    bot_msg.dice.value
                )

            await asyncio.sleep(0.8)

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET opponent_total = ?,
                    opponent_rolls = ?
                WHERE id = ?
            """, (
                bot_total,
                int(updated["count"]),
                game_id
            ))

            db.commit()

        await finish_game(
            context,
            game_id
        )

        return

    # --------------------------------------------------------
    # FRIEND GAME
    # --------------------------------------------------------

    with closing(get_db()) as db:

        updated = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

    if not updated:
        return

    creator_rolls = int(
        updated["creator_rolls"]
    )

    opponent_rolls = int(
        updated["opponent_rolls"]
    )

    count = int(
        updated["count"]
    )

    # سازنده هنوز تمام نکرده
    if creator_rolls < count:

        await safe_send_message(
            context.bot,
            message.chat.id,
            "🎯 پرتاب ثبت شد.\n"
            f"پرتاب باقی‌مانده سازنده: "
            f"{count - creator_rolls}"
        )

        return

    # سازنده تمام کرده ولی نفر دوم هنوز شروع نکرده
    if opponent_rolls == 0 and user.id == updated["creator_id"]:

        await safe_send_message(
            context.bot,
            message.chat.id,
            "✅ پرتاب‌های سازنده تمام شد.\n\n"
            "🎯 حالا بازیکن دوم باید خودش "
            f"{count} بار {GAME_EMOJI[updated['game_type']]} "
            "بفرستد."
        )

        return

    # نفر دوم در حال بازی است
    if opponent_rolls < count:

        await safe_send_message(
            context.bot,
            message.chat.id,
            "🎯 پرتاب بازیکن دوم ثبت شد.\n"
            f"پرتاب باقی‌مانده: "
            f"{count - opponent_rolls}"
        )

        return

    # هر دو تمام کردند
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

        try:

            db.execute("BEGIN IMMEDIATE")

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

            count = int(
                game["count"]
            )

            if int(game["creator_rolls"]) < count:
                db.rollback()
                return

            if int(game["opponent_rolls"]) < count:
                db.rollback()
                return

            db.execute("""
                UPDATE games
                SET status = 'finished'
                WHERE id = ?
            """, (
                game_id,
            ))

            db.commit()

        except Exception:

            db.rollback()

            logger.exception(
                "Finish game failed"
            )

            return

    chat_id = game["chat_id"]

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

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if game["mode"] == "bot":

        if creator_total > opponent_total:

            payout = bet * 2

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
                "🏆 شما برنده شدید!\n"
                f"💰 جایزه: "
                f"{money(payout)} TRX"
            )

        else:

            result = (
                "🤖 ربات برنده شد.\n"
                f"🎯 نتیجه: "
                f"{creator_total} - "
                f"{opponent_total}"
            )

    # --------------------------------------------------------
    # FRIENDS
    # --------------------------------------------------------

    else:

        payout = bet * 2

        if creator_total > opponent_total:

            winner = game["creator_id"]

        else:

            winner = game["opponent_id"]

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
            "🏆 برنده:\n"
            f"{winner}\n"
            f"💰 جایزه: "
            f"{money(payout)} TRX"
        )

    await safe_send_message(
        context.bot,
        chat_id,
        "🏁 نتیجه بازی\n\n"

        f"{GAME_INFO[game['game_type']][0]} "
        f"{GAME_INFO[game['game_type']][1]}\n\n"

        f"👤 سازنده: "
        f"{creator_total}\n"

        f"👤 بازیکن دوم: "
        f"{opponent_total}\n\n"

        f"{result}"
    )


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
    # ADMIN PRIVATE PANEL INPUT
    # ========================================================

    if (
        is_owner(user.id)
        and message.chat.type == ChatType.PRIVATE
    ):

        operation = context.user_data.get(
            "admin_private_operation"
        )

        if operation:

            parts = text.split()

            # ID مبلغ
            if len(parts) == 2:

                try:
                    target_id = int(parts[0])
                except Exception:
                    target_id = None

                amount = parse_amount(
                    parts[1]
                )

                if target_id and amount:

                    await admin_private_change(
                        update,
                        context,
                        operation
                    )

                    context.user_data.pop(
                        "admin_private_operation",
                        None
                    )

                    return

    # ========================================================
    # OWNER GROUP CHARGE
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
                    "❌ مبلغ صحیح نیست.\n"
                    "مثال: شارژ 100"
                )

                return

            if not message.reply_to_message:

                await message.reply_text(
                    "❌ برای شارژ باید روی پیام "
                    "کاربر Reply کنید.\n\n"
                    "مثال:\n"
                    "شارژ 100"
                )

                return

            await admin_group_change(
                update,
                context,
                "charge",
                amount
            )

            return

        # ----------------------------------------------------
        # REMOVE
        # ----------------------------------------------------

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
                    "❌ مبلغ صحیح نیست.\n"
                    "مثال: کسر 100"
                )

                return

            if not message.reply_to_message:

                await message.reply_text(
                    "❌ برای کسر باید روی پیام "
                    "کاربر Reply کنید.\n\n"
                    "مثال:\n"
                    "کسر 100"
                )

                return

            await admin_group_change(
                update,
                context,
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
# ADMIN COMMAND
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


# ============================================================
# COMMANDS
# ============================================================

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
            "Telegram temporary error: %s",
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
    # INCOMING DICE
    #
    # مهم:
    # این Handler باید قبل از TEXT باشد.
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.Dice.ALL,
            incoming_game_roll
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


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    main()
