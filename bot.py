import os
import re
import sqlite3
import logging
import asyncio
from decimal import Decimal, InvalidOperation
from contextlib import closing

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatType
from telegram.error import TimedOut, NetworkError, RetryAfter
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

# سه مالک
OWNER_IDS = {
    8552447077,
    7221112088,
}

# =========================================================
# BET_Tek = GROUP
# =========================================================
# اینجا BET_Tek به عنوان گپ/گروه اصلی ربات استفاده می‌شود.
# ربات باید داخل این گپ عضو باشد و برای check_membership
# بهتر است ربات ادمین گپ باشد.

GROUP_USERNAME = "@BET_Tek"
GROUP_URL = "https://t.me/BET_Tek"

DB_FILE = "bot.db"

MIN_GAME_BET = Decimal("0.1")
MIN_TRANSFER = Decimal("0.1")
REFERRAL_REWARD = Decimal("0.05")
MAX_GAME_COUNT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("BET_BT")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    db = sqlite3.connect(DB_FILE, timeout=30)
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
                referrer_id INTEGER,
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
                creator_total REAL NOT NULL DEFAULT 0,
                opponent_total REAL NOT NULL DEFAULT 0,
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
            INSERT OR IGNORE INTO settings(key, value)
            VALUES ('bot_enabled', '1')
        """)

        db.commit()


# =========================================================
# HELPERS
# =========================================================

def normalize_digits(text):
    return str(text).translate(
        str.maketrans(
            "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
            "01234567890123456789"
        )
    )


def parse_decimal(text):
    text = normalize_digits(str(text)).strip()

    text = (
        text
        .replace("٫", ".")
        .replace(",", ".")
    )

    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None

    if value <= 0:
        return None

    return value


def money(value):
    value = Decimal(str(value))

    s = format(value, "f")

    if "." in s:
        s = s.rstrip("0").rstrip(".")

    return s or "0"


def is_owner(user_id):
    return user_id in OWNER_IDS


def is_group(chat):
    return (
        chat
        and chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        )
    )


def ensure_user(user):
    if not user:
        return

    with closing(get_db()) as db:

        db.execute("""
            INSERT OR IGNORE INTO users(
                user_id,
                username,
                first_name
            )
            VALUES (?, ?, ?)
        """, (
            user.id,
            user.username or "",
            user.first_name or "",
        ))

        db.execute("""
            UPDATE users
            SET
                username = ?,
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

        row = db.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

    if not row:
        return Decimal("0")

    try:
        return Decimal(row["balance"])
    except Exception:
        return Decimal("0")


def change_balance(
    user_id,
    amount,
    tx_type,
    description=""
):
    amount = Decimal(str(amount))

    with closing(get_db()) as db:

        db.execute("BEGIN IMMEDIATE")

        row = db.execute(
            """
            SELECT balance
            FROM users
            WHERE user_id = ?
            """,
            (user_id,),
        ).fetchone()

        if not row:
            db.rollback()
            raise ValueError("user_not_found")

        current = Decimal(row["balance"])

        new_balance = current + amount

        # ضد موجودی منفی
        if new_balance < 0:
            db.rollback()
            raise ValueError("insufficient_balance")

        db.execute(
            """
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
            """,
            (
                str(new_balance),
                user_id,
            ),
        )

        db.execute(
            """
            INSERT INTO transactions(
                user_id,
                type,
                amount,
                description
            )
            VALUES (?, ?, ?, ?)
            """,
            (
                user_id,
                tx_type,
                str(amount),
                description,
            ),
        )

        db.commit()


def set_bot_enabled(enabled):
    with closing(get_db()) as db:

        db.execute(
            """
            INSERT INTO settings(key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            (
                "1" if enabled else "0",
            ),
        )

        db.commit()


def bot_enabled():
    with closing(get_db()) as db:

        row = db.execute(
            """
            SELECT value
            FROM settings
            WHERE key = 'bot_enabled'
            """
        ).fetchone()

    return bool(
        row and row["value"] == "1"
    )


# =========================================================
# SAFE TELEGRAM
# =========================================================

async def safe_send_message(
    bot,
    *args,
    **kwargs
):
    for attempt in range(3):

        try:
            return await bot.send_message(
                *args,
                **kwargs
            )

        except RetryAfter as e:

            await asyncio.sleep(
                min(float(e.retry_after), 30)
            )

        except (
            TimedOut,
            NetworkError
        ) as e:

            logger.warning(
                "send_message network error: %s",
                e
            )

            if attempt == 2:
                return None

            await asyncio.sleep(
                2 * (attempt + 1)
            )

        except Exception:

            logger.exception(
                "send_message failed"
            )

            return None

    return None


async def safe_send_dice(
    bot,
    *args,
    **kwargs
):
    for attempt in range(3):

        try:
            return await bot.send_dice(
                *args,
                **kwargs
            )

        except RetryAfter as e:

            await asyncio.sleep(
                min(float(e.retry_after), 30)
            )

        except (
            TimedOut,
            NetworkError
        ) as e:

            logger.warning(
                "send_dice network error: %s",
                e
            )

            if attempt == 2:
                return None

            await asyncio.sleep(
                2 * (attempt + 1)
            )

        except Exception:

            logger.exception(
                "send_dice failed"
            )

            return None

    return None


# =========================================================
# MEMBERSHIP / GROUP
# =========================================================

async def check_membership(
    user_id,
    context
):
    """
    بررسی عضویت کاربر در BET_Tek.

    چون BET_Tek گپ است، get_chat_member
    روی گروه بررسی می‌شود.
    """

    try:

        member = await context.bot.get_chat_member(
            GROUP_USERNAME,
            user_id
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception as e:

        logger.warning(
            "group membership check failed: %s",
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
                "👥 ورود به گپ BET_Tek",
                url=GROUP_URL
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_membership"
            )
        ],
    ])

    if update.message:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔒 برای استفاده از ربات ابتدا باید "
            "عضو گپ BET_Tek شوید.",
            reply_markup=keyboard
        )

    return False


async def membership_callback(
    update,
    context
):
    query = update.callback_query

    try:

        if await check_membership(
            query.from_user.id,
            context
        ):

            await query.answer(
                "✅ عضویت تأیید شد.",
                show_alert=True
            )

            await query.edit_message_text(
                "✅ عضویت شما در BET_Tek تأیید شد.\n"
                "حالا می‌توانید از ربات استفاده کنید."
            )

        else:

            await query.answer(
                "❌ هنوز عضو گپ BET_Tek نیستید.",
                show_alert=True
            )

    except Exception:

        logger.exception(
            "membership callback error"
        )


# =========================================================
# MENU
# =========================================================

def main_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="menu_balance"
            ),
            InlineKeyboardButton(
                "👥 زیر مجموعه",
                callback_data="menu_ref"
            ),
        ],
        [
            InlineKeyboardButton(
                "🔄 انتقال در گپ",
                callback_data="menu_transfer"
            ),
            InlineKeyboardButton(
                "🎮 راهنمای بازی",
                callback_data="menu_games"
            ),
        ],
    ])


async def start(
    update,
    context
):
    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    # ============================================
    # ضد سوءاستفاده Referral
    # ============================================

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):

            try:
                referrer_id = int(
                    arg[4:]
                )
            except ValueError:
                referrer_id = None

            if (
                referrer_id
                and referrer_id != user.id
            ):

                with closing(get_db()) as db:

                    row = db.execute(
                        """
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                        """,
                        (user.id,),
                    ).fetchone()

                    exists = db.execute(
                        """
                        SELECT id
                        FROM referrals
                        WHERE referred_id = ?
                        """,
                        (user.id,),
                    ).fetchone()

                    if (
                        row
                        and row["referrer_id"] is None
                        and not exists
                    ):

                        # فقط اگر معرف وجود داشته باشد
                        ref_user = db.execute(
                            """
                            SELECT user_id
                            FROM users
                            WHERE user_id = ?
                            """,
                            (referrer_id,),
                        ).fetchone()

                        if ref_user:

                            db.execute(
                                """
                                UPDATE users
                                SET referrer_id = ?
                                WHERE user_id = ?
                                """,
                                (
                                    referrer_id,
                                    user.id,
                                ),
                            )

                            db.execute(
                                """
                                INSERT INTO referrals(
                                    referrer_id,
                                    referred_id,
                                    reward
                                )
                                VALUES (?, ?, ?)
                                """,
                                (
                                    referrer_id,
                                    user.id,
                                    str(
                                        REFERRAL_REWARD
                                    ),
                                ),
                            )

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
                                    "referral reward failed"
                                )

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: "
        f"{money(get_balance(user.id))} TRX\n\n"
        "از دکمه‌های زیر استفاده کنید:",
        reply_markup=main_keyboard()
    )


async def help_command(
    update,
    context
):
    await update.message.reply_text(
        "📚 راهنمای BET_BT\n\n"

        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال در گپ با Reply\n\n"

        "🎮 بازی‌ها فقط داخل گپ:\n\n"

        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1\n\n"

        "عدد فارسی و انگلیسی هر دو قابل استفاده است."
    )


async def balance(
    update,
    context
):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    await update.message.reply_text(
        f"💰 موجودی شما: "
        f"{money(get_balance(user.id))} TRX"
    )


async def referral(
    update,
    context
):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/"
        f"{bot.username}"
        f"?start=ref_{user.id}"
    )

    with closing(get_db()) as db:

        row = db.execute(
            """
            SELECT COUNT(*) c
            FROM referrals
            WHERE referrer_id = ?
            """,
            (user.id,),
        ).fetchone()

    count = row["c"] if row else 0

    await update.message.reply_text(
        "👥 زیر مجموعه\n\n"

        f"🔗 لینک دعوت:\n"
        f"{link}\n\n"

        f"👤 تعداد: {count}\n"
        f"🎁 پاداش هر نفر: "
        f"{money(REFERRAL_REWARD)} TRX"
    )


# =========================================================
# TRANSFER
# =========================================================

async def do_transfer(
    update,
    context,
    amount
):
    user = update.effective_user
    msg = update.message

    if not user or not msg:
        return

    if not msg.reply_to_message:

        await msg.reply_text(
            "❌ روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    target = msg.reply_to_message.from_user

    if not target:

        await msg.reply_text(
            "❌ گیرنده پیدا نشد."
        )

        return

    if target.id == user.id:

        await msg.reply_text(
            "❌ انتقال به خودتان امکان‌پذیر نیست."
        )

        return

    if amount < MIN_TRANSFER:

        await msg.reply_text(
            "❌ حداقل انتقال "
            "0.1 TRX است."
        )

        return

    # جلوگیری از انتقال به خود ربات
    me = await context.bot.get_me()

    if target.id == me.id:

        await msg.reply_text(
            "❌ انتقال به ربات امکان‌پذیر نیست."
        )

        return

    ensure_user(target)

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            sender = db.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user.id,),
            ).fetchone()

            receiver = db.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (target.id,),
            ).fetchone()

            if not sender or not receiver:
                raise ValueError(
                    "user_not_found"
                )

            sender_balance = Decimal(
                sender["balance"]
            )

            receiver_balance = Decimal(
                receiver["balance"]
            )

            # ضد موجودی
            if sender_balance < amount:
                raise ValueError(
                    "insufficient"
                )

            new_sender = (
                sender_balance - amount
            )

            new_receiver = (
                receiver_balance + amount
            )

            # هر دو در یک تراکنش
            db.execute(
                """
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
                """,
                (
                    str(new_sender),
                    user.id,
                ),
            )

            db.execute(
                """
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
                """,
                (
                    str(new_receiver),
                    target.id,
                ),
            )

            db.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, 'transfer_out', ?, ?)
                """,
                (
                    user.id,
                    str(-amount),
                    f"To {target.id}",
                ),
            )

            db.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (?, 'transfer_in', ?, ?)
                """,
                (
                    target.id,
                    str(amount),
                    f"From {user.id}",
                ),
            )

            db.commit()

        except ValueError as e:

            db.rollback()

            if str(e) == "insufficient":

                await msg.reply_text(
                    "❌ موجودی شما کافی نیست."
                )

            else:

                await msg.reply_text(
                    "❌ انتقال انجام نشد."
                )

            return

        except Exception:

            db.rollback()

            logger.exception(
                "transfer failed"
            )

            await msg.reply_text(
                "❌ انتقال انجام نشد."
            )

            return

    await msg.reply_text(
        "✅ انتقال انجام شد.\n\n"

        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: "
        f"{target.first_name or target.id}\n"
        f"💰 موجودی شما: "
        f"{money(get_balance(user.id))} TRX"
    )


# =========================================================
# ADMIN
# =========================================================

async def admin_panel(
    update,
    context
):
    user = update.effective_user

    # ضد دستور مالک
    if not user or not is_owner(user.id):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    state = (
        "🟢 روشن"
        if bot_enabled()
        else
        "🔴 خاموش"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 روشن",
                callback_data="admin_on"
            ),
            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_off"
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ],
    ])

    await update.message.reply_text(
        "👑 پنل مدیریت\n\n"
        f"وضعیت: {state}\n\n"

        "برای شارژ یا کسر موجودی:\n"
        "در گپ روی پیام کاربر Reply کنید:\n\n"

        "شارژ 100\n"
        "کسر 100",

        reply_markup=keyboard
    )


async def admin_change(
    update,
    context,
    amount,
    operation
):
    owner = update.effective_user

    if not owner or not is_owner(owner.id):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    if not update.message.reply_to_message:

        await update.message.reply_text(
            "❌ برای شارژ یا کسر باید "
            "روی پیام کاربر Reply کنید.\n\n"

            "مثال:\n"
            "شارژ 100\n"
            "کسر 100"
        )

        return

    target = (
        update.message
        .reply_to_message
        .from_user
    )

    if not target:

        await update.message.reply_text(
            "❌ کاربر پیدا نشد."
        )

        return

    # مالک نمی‌تواند موجودی خودش را با این دستور تغییر دهد
    if target.id in OWNER_IDS:

        await update.message.reply_text(
            "❌ تغییر موجودی مالک از این دستور مجاز نیست."
        )

        return

    ensure_user(target)

    try:

        if operation == "charge":

            signed = amount
            tx_type = "admin_charge"

        else:

            signed = -amount
            tx_type = "admin_remove"

        change_balance(
            target.id,
            signed,
            tx_type,
            f"Owner {owner.id}"
        )

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await update.message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )

        else:

            await update.message.reply_text(
                "❌ عملیات انجام نشد."
            )

        return

    except Exception:

        logger.exception(
            "admin balance change failed"
        )

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return

    word = (
        "شارژ"
        if operation == "charge"
        else
        "کسر"
    )

    await update.message.reply_text(
        f"✅ {word} انجام شد.\n"
        f"👤 {target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
    )


async def admin_callback(
    update,
    context
):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):

        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )

        return

    try:

        if query.data == "admin_on":

            set_bot_enabled(True)

            await query.answer(
                "🟢 ربات روشن شد."
            )

            await query.edit_message_text(
                "👑 پنل مدیریت\n\n"
                "🟢 ربات روشن است."
            )

        elif query.data == "admin_off":

            set_bot_enabled(False)

            await query.answer(
                "🔴 ربات خاموش شد."
            )

            await query.edit_message_text(
                "👑 پنل مدیریت\n\n"
                "🔴 ربات خاموش است."
            )

        elif query.data == "admin_stats":

            with closing(get_db()) as db:

                users = db.execute(
                    """
                    SELECT COUNT(*) c
                    FROM users
                    """
                ).fetchone()["c"]

                games = db.execute(
                    """
                    SELECT COUNT(*) c
                    FROM games
                    """
                ).fetchone()["c"]

                total = db.execute(
                    """
                    SELECT
                        COALESCE(
                            SUM(
                                CAST(
                                    balance AS REAL
                                )
                            ),
                            0
                        ) s
                    FROM users
                    """
                ).fetchone()["s"]

            await query.answer()

            await query.edit_message_text(
                "📊 آمار\n\n"
                f"👤 کاربران: {users}\n"
                f"🎮 بازی‌ها: {games}\n"
                f"💰 مجموع موجودی: "
                f"{total:.2f} TRX"
            )

    except Exception:

        logger.exception(
            "admin callback error"
        )


# =========================================================
# MENU CALLBACKS
# =========================================================

async def menu_callback(
    update,
    context
):
    query = update.callback_query
    user = query.from_user

    ensure_user(user)

    try:

        if query.data == "menu_balance":

            await query.answer()

            await query.message.reply_text(
                f"💰 موجودی شما: "
                f"{money(get_balance(user.id))} TRX"
            )

        elif query.data == "menu_ref":

            await query.answer()

            bot = await context.bot.get_me()

            link = (
                f"https://t.me/"
                f"{bot.username}"
                f"?start=ref_{user.id}"
            )

            with closing(get_db()) as db:

                count = db.execute(
                    """
                    SELECT COUNT(*) c
                    FROM referrals
                    WHERE referrer_id = ?
                    """,
                    (user.id,),
                ).fetchone()["c"]

            await query.message.reply_text(
                "👥 زیر مجموعه\n\n"
                f"🔗 {link}\n\n"
                f"👤 تعداد: {count}\n"
                f"🎁 هر رف: "
                f"{money(REFERRAL_REWARD)} TRX"
            )

        elif query.data == "menu_transfer":

            await query.answer()

            await query.message.reply_text(
                "🔄 انتقال در گپ\n\n"
                "روی پیام کاربر Reply کنید و بنویسید:\n\n"
                "انتقال 0.1\n"
                "یا\n"
                "انتقال ۰.۱"
            )

        elif query.data == "menu_games":

            await query.answer()

            await query.message.reply_text(
                "🎮 بازی‌ها فقط داخل گپ:\n\n"

                "1 تاس 0.1\n"
                "1 دارت 0.1\n"
                "1 بولینگ 0.1\n"
                "1 بسکتبال 0.1\n\n"

                "بعد از ساخت بازی، یکی از گزینه‌ها را بزنید."
            )

    except Exception:

        logger.exception(
            "menu callback error"
        )


# =========================================================
# GAME PARSING
# =========================================================

GAME_NAMES = {

    "تاس": "dice",
    "dice": "dice",

    "دارت": "darts",
    "دارتس": "darts",
    "darts": "darts",

    "بولینگ": "bowling",
    "بولينگ": "bowling",
    "bowling": "bowling",

    "بسکتبال": "basketball",
    "basketball": "basketball",
}


GAME_EMOJIS = {

    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",

}


GAME_LABELS = {

    "dice": "🎲 تاس",
    "darts": "🎯 دارت",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",

}


def parse_game_command(text):

    text = normalize_digits(
        text.strip()
    )

    m = re.fullmatch(
        r"(\d+)\s+([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)",
        text
    )

    if not m:
        return None

    count = int(m.group(1))

    game_name = (
        m.group(2)
        .lower()
    )

    bet = parse_decimal(
        m.group(3)
    )

    if not (
        1 <= count <= MAX_GAME_COUNT
    ):
        return None

    if (
        game_name not in GAME_NAMES
        or bet is None
    ):
        return None

    return (
        count,
        GAME_NAMES[game_name],
        bet
    )


# =========================================================
# CREATE GAME
# =========================================================

async def create_game(
    update,
    context,
    count,
    game_type,
    bet
):
    user = update.effective_user
    chat = update.effective_chat

    # فقط گپ
    if not is_group(chat):

        await update.message.reply_text(
            "❌ بازی‌ها فقط داخل گپ هستند."
        )

        return

    if not bot_enabled():

        await update.message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    # جوین اجباری
    if not await require_membership(
        update,
        context
    ):
        return

    if bet < MIN_GAME_BET:

        await update.message.reply_text(
            "❌ حداقل شرط "
            "0.1 TRX است."
        )

        return

    ensure_user(user)

    # کسر اتمیک موجودی
    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type} #{count}"
        )

    except ValueError:

        await update.message.reply_text(
            "❌ موجودی کافی نیست.\n"
            f"💰 موجودی: "
            f"{money(get_balance(user.id))} TRX"
        )

        return

    with closing(get_db()) as db:

        cur = db.execute(
            """
            INSERT INTO games(
                chat_id,
                creator_id,
                game_type,
                count,
                bet,
                mode,
                status
            )
            VALUES (
                ?, ?, ?, ?, ?,
                'friends',
                'waiting'
            )
            """,
            (
                chat.id,
                user.id,
                game_type,
                count,
                str(bet),
            ),
        )

        game_id = cur.lastrowid

        db.commit()

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
                "❌ لغو",
                callback_data=f"cancel_{game_id}"
            )
        ],
    ])

    await update.message.reply_text(

        "🎮 بازی جدید\n\n"

        f"{GAME_LABELS[game_type]}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: "
        f"{user.first_name or user.id}\n\n"

        "یکی را انتخاب کنید:",

        reply_markup=keyboard
    )


# =========================================================
# JOIN GAME
# =========================================================

async def join_game(
    update,
    context
):
    query = update.callback_query
    user = query.from_user

    # عضویت اجباری
    if not await check_membership(
        user.id,
        context
    ):

        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True
        )

        return

    try:

        game_id = int(
            query.data.split("_")[1]
        )

    except Exception:

        return

    ensure_user(user)

    with closing(get_db()) as db:

        try:

            db.execute(
                "BEGIN IMMEDIATE"
            )

            game = db.execute(
                """
                SELECT *
                FROM games
                WHERE id = ?
                """,
                (game_id,),
            ).fetchone()

            if not game:

                raise ValueError(
                    "not_waiting"
                )

            if game["status"] != "waiting":

                raise ValueError(
                    "not_waiting"
                )

            if game["creator_id"] == user.id:

                raise ValueError(
                    "self"
                )

            bet = Decimal(
                game["bet"]
            )

            row = db.execute(
                """
                SELECT balance
                FROM users
                WHERE user_id = ?
                """,
                (user.id,),
            ).fetchone()

            if (
                not row
                or Decimal(row["balance"]) < bet
            ):

                raise ValueError(
                    "balance"
                )

            current = Decimal(
                row["balance"]
            )

            new_balance = (
                current - bet
            )

            # ضد موجودی منفی
            if new_balance < 0:

                raise ValueError(
                    "balance"
                )

            db.execute(
                """
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
                """,
                (
                    str(new_balance),
                    user.id,
                ),
            )

            db.execute(
                """
                INSERT INTO transactions(
                    user_id,
                    type,
                    amount,
                    description
                )
                VALUES (
                    ?,
                    'game_bet',
                    ?,
                    ?
                )
                """,
                (
                    user.id,
                    str(-bet),
                    f"Joined game #{game_id}",
                ),
            )

            # شرط status باعث می‌شود
            # فقط یک نفر بتواند بازی را بگیرد.
            cur = db.execute(
                """
                UPDATE games
                SET
                    opponent_id = ?,
                    status = 'playing'
                WHERE
                    id = ?
                    AND status = 'waiting'
                """,
                (
                    user.id,
                    game_id,
                ),
            )

            if cur.rowcount != 1:

                raise ValueError(
                    "not_waiting"
                )

            db.commit()

        except Exception as e:

            db.rollback()

            if str(e) == "self":

                await query.answer(
                    "❌ نمی‌توانید با خودتان بازی کنید.",
                    show_alert=True
                )

            elif str(e) == "balance":

                await query.answer(
                    "❌ موجودی کافی نیست.",
                    show_alert=True
                )

            else:

                await query.answer(
                    "❌ بازی قبلاً گرفته شده یا وجود ندارد.",
                    show_alert=True
                )

            return

    await query.edit_message_text(

        "🎮 بازی شروع شد!\n\n"

        f"{GAME_LABELS[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: "
        f"{money(bet)} TRX\n\n"

        "🎯 ابتدا سازنده تمام پرتاب‌ها را انجام می‌دهد."
    )

    await run_turn(
        context,
        game_id,
        game["creator_id"]
    )


# =========================================================
# BOT GAME
# =========================================================

async def bot_game(
    update,
    context
):
    query = update.callback_query
    user = query.from_user

    if not await check_membership(
        user.id,
        context
    ):

        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True
        )

        return

    try:

        game_id = int(
            query.data.split("_")[1]
        )

    except Exception:

        return

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

        if not game:

            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )

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

        db.execute(
            """
            UPDATE games
            SET
                mode = 'bot',
                opponent_id = -1,
                status = 'playing'
            WHERE
                id = ?
                AND status = 'waiting'
            """,
            (game_id,),
        )

        db.commit()

    await query.edit_message_text(

        "🤖 بازی با ربات شروع شد!\n\n"

        f"{GAME_LABELS[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: "
        f"{money(Decimal(game['bet']))} TRX\n\n"

        "🎯 ابتدا تمام پرتاب‌های شما انجام می‌شود."
    )

    await run_turn(
        context,
        game_id,
        user.id
    )


# =========================================================
# CANCEL GAME
# =========================================================

async def cancel_game(
    update,
    context
):
    query = update.callback_query
    user = query.from_user

    try:

        game_id = int(
            query.data.split("_")[1]
        )

    except Exception:

        return

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

        if not game:

            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )

            return

        if game["status"] != "waiting":

            await query.answer(
                "❌ این بازی قابل لغو نیست.",
                show_alert=True
            )

            return

        if (
            game["creator_id"] != user.id
            and not is_owner(user.id)
        ):

            await query.answer(
                "❌ دسترسی ندارید.",
                show_alert=True
            )

            return

        db.execute(
            """
            UPDATE games
            SET status = 'cancelled'
            WHERE
                id = ?
                AND status = 'waiting'
            """,
            (game_id,),
        )

        db.commit()

    # فقط یک بار برگشت
    try:

        change_balance(
            game["creator_id"],
            Decimal(game["bet"]),
            "game_refund",
            f"Cancelled game #{game_id}"
        )

    except Exception:

        logger.exception(
            "cancel refund failed"
        )

    await query.edit_message_text(

        "❌ بازی لغو شد.\n"
        f"💰 {money(Decimal(game['bet']))} "
        "TRX برگشت داده شد."
    )


# =========================================================
# ROLL
# =========================================================

async def roll_once(
    context,
    chat_id,
    game_type
):

    msg = await safe_send_dice(
        context.bot,
        chat_id=chat_id,
        emoji=GAME_EMOJIS[game_type]
    )

    if (
        msg is None
        or not msg.dice
    ):
        return None

    return msg.dice.value


# =========================================================
# PLAYER TURN
# =========================================================

async def run_turn(
    context,
    game_id,
    player_id
):

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

    if (
        not game
        or game["status"] != "playing"
    ):
        return

    total = 0
    successful = 0

    for _ in range(
        game["count"]
    ):

        value = await roll_once(
            context,
            game["chat_id"],
            game["game_type"]
        )

        if value is not None:

            total += value
            successful += 1

        await asyncio.sleep(0.5)

    # اگر همه پرتاب‌ها اجرا نشدند
    if successful != game["count"]:

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "⚠️ خطای موقت تلگرام در اجرای بازی.\n"
            "بازی لغو و شرط‌ها برگردانده می‌شوند."
        )

        await refund_failed_game(
            context,
            game_id
        )

        return

    with closing(get_db()) as db:

        if player_id == game["creator_id"]:

            db.execute(
                """
                UPDATE games
                SET creator_total = ?
                WHERE id = ?
                """,
                (
                    total,
                    game_id,
                ),
            )

        else:

            db.execute(
                """
                UPDATE games
                SET opponent_total = ?
                WHERE id = ?
                """,
                (
                    total,
                    game_id,
                ),
            )

        db.commit()

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

    # سازنده
    if player_id == game["creator_id"]:

        if game["mode"] == "bot":

            await safe_send_message(
                context.bot,
                game["chat_id"],
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
            )

            await run_bot_turn(
                context,
                game_id
            )

        else:

            await safe_send_message(
                context.bot,
                game["chat_id"],
                "✅ پرتاب‌های سازنده تمام شد.\n"
                "🎯 حالا بازیکن دوم بازی می‌کند."
            )

            await run_turn(
                context,
                game_id,
                game["opponent_id"]
            )

    else:

        await finish_game(
            context,
            game_id
        )


# =========================================================
# BOT TURN
# =========================================================

async def run_bot_turn(
    context,
    game_id
):

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

    if (
        not game
        or game["status"] != "playing"
    ):
        return

    total = 0
    successful = 0

    for _ in range(
        game["count"]
    ):

        value = await roll_once(
            context,
            game["chat_id"],
            game["game_type"]
        )

        if value is not None:

            total += value
            successful += 1

        await asyncio.sleep(0.5)

    if successful != game["count"]:

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "⚠️ خطای موقت تلگرام.\n"
            "بازی لغو شد و شرط برگشت داده می‌شود."
        )

        await refund_failed_game(
            context,
            game_id
        )

        return

    with closing(get_db()) as db:

        db.execute(
            """
            UPDATE games
            SET opponent_total = ?
            WHERE id = ?
            """,
            (
                total,
                game_id,
            ),
        )

        db.commit()

    await finish_game(
        context,
        game_id
    )


# =========================================================
# FAILED GAME REFUND
# =========================================================

async def refund_failed_game(
    context,
    game_id
):

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

        if (
            not game
            or game["status"] != "playing"
        ):
            return

        db.execute(
            """
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
            """,
            (game_id,),
        )

        db.commit()

    bet = Decimal(
        game["bet"]
    )

    # برگشت سازنده
    try:

        change_balance(
            game["creator_id"],
            bet,
            "game_refund",
            f"Failed game #{game_id}"
        )

    except Exception:

        logger.exception(
            "creator refund failed"
        )

    # برگشت بازیکن دوم
    if (
        game["mode"] == "friends"
        and game["opponent_id"]
    ):

        try:

            change_balance(
                game["opponent_id"],
                bet,
                "game_refund",
                f"Failed game #{game_id}"
            )

        except Exception:

            logger.exception(
                "opponent refund failed"
            )


# =========================================================
# FINISH GAME
# =========================================================

async def finish_game(
    context,
    game_id
):

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()

        if (
            not game
            or game["status"] != "playing"
        ):
            return

        creator_total = float(
            game["creator_total"]
        )

        opponent_total = float(
            game["opponent_total"]
        )

        bet = Decimal(
            game["bet"]
        )

        # مهم:
        # فقط همینجا بازی finished می‌شود
        db.execute(
            """
            UPDATE games
            SET status = 'finished'
            WHERE
                id = ?
                AND status = 'playing'
            """,
            (game_id,),
        )

        db.commit()

    # =====================================================
    # DRAW
    # =====================================================

    if creator_total == opponent_total:

        try:

            change_balance(
                game["creator_id"],
                bet,
                "game_draw_refund",
                f"Draw #{game_id}"
            )

            if game["mode"] == "friends":

                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_draw_refund",
                    f"Draw #{game_id}"
                )

        except Exception:

            logger.exception(
                "draw refund failed"
            )

        result = (
            "🤝 بازی مساوی شد.\n"
            "💰 شرط بازیکنان برگشت داده شد."
        )

    # =====================================================
    # CREATOR WIN
    # =====================================================

    elif creator_total > opponent_total:

        if game["mode"] == "friends":

            winner = game["creator_id"]

            payout = bet * 2

            try:

                change_balance(
                    winner,
                    payout,
                    "game_win",
                    f"Win #{game_id}"
                )

            except Exception:

                logger.exception(
                    "friend payout failed"
                )

            result = (
                "🏆 سازنده برنده شد.\n"
                f"💰 جایزه: "
                f"{money(payout)} TRX"
            )

        else:

            payout = bet * 2

            try:

                change_balance(
                    game["creator_id"],
                    payout,
                    "game_win",
                    f"Bot win #{game_id}"
                )

            except Exception:

                logger.exception(
                    "bot payout failed"
                )

            result = (
                "🏆 شما برنده شدید.\n"
                f"💰 جایزه: "
                f"{money(payout)} TRX"
            )

    # =====================================================
    # OPPONENT / BOT WIN
    # =====================================================

    else:

        if game["mode"] == "friends":

            winner = game["opponent_id"]

            payout = bet * 2

            try:

                change_balance(
                    winner,
                    payout,
                    "game_win",
                    f"Win #{game_id}"
                )

            except Exception:

                logger.exception(
                    "opponent payout failed"
                )

            result = (
                "🏆 بازیکن دوم برنده شد.\n"
                f"💰 جایزه: "
                f"{money(payout)} TRX"
            )

        else:

            result = (
                "🤖 ربات برنده شد.\n"
                "💰 این شرط برگشت داده نشد."
            )

    await safe_send_message(

        context.bot,
        game["chat_id"],

        "🏁 نتیجه بازی\n\n"

        f"{GAME_LABELS[game['game_type']]}\n"

        f"👤 سازنده: "
        f"{creator_total:g}\n"

        f"👤 بازیکن دوم: "
        f"{opponent_total:g}\n\n"

        f"{result}"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(
    update,
    context
):

    msg = update.message
    user = update.effective_user

    if (
        not msg
        or not user
        or not msg.text
    ):
        return

    ensure_user(user)

    text = normalize_digits(
        msg.text.strip()
    )

    # =====================================================
    # ADMIN / OWNER COMMANDS
    # =====================================================
    #
    # ضد دستور:
    # کاربران عادی نمی‌توانند روشن/خاموش،
    # شارژ یا کسر را اجرا کنند.
    # =====================================================

    if text == "روشن":

        if not is_owner(user.id):

            # هیچ عملیات مدیریتی انجام نمی‌شود
            return

        set_bot_enabled(True)

        await msg.reply_text(
            "🟢 ربات روشن شد."
        )

        return

    if text == "خاموش":

        if not is_owner(user.id):

            return

        set_bot_enabled(False)

        await msg.reply_text(
            "🔴 ربات خاموش شد."
        )

        return

    if text.startswith("شارژ "):

        # ضد دستور مالک
        if not is_owner(user.id):
            return

        amount = parse_decimal(
            text[6:].strip()
        )

        if amount is None:

            await msg.reply_text(
                "❌ مثال صحیح:\n"
                "شارژ 100"
            )

        else:

            await admin_change(
                update,
                context,
                amount,
                "charge"
            )

        return

    if text.startswith("کسر "):

        # ضد دستور مالک
        if not is_owner(user.id):
            return

        amount = parse_decimal(
            text[4:].strip()
        )

        if amount is None:

            await msg.reply_text(
                "❌ مثال صحیح:\n"
                "کسر 100"
            )

        else:

            await admin_change(
                update,
                context,
                amount,
                "remove"
            )

        return

    # =====================================================
    # PUBLIC
    # =====================================================

    if text in (
        "موجودی",
        "موجودی من",
        "balance",
    ):

        await balance(
            update,
            context
        )

        return

    if text in (
        "زیر مجموعه",
        "زیرمجموعه",
        "رفرال",
        "referral",
    ):

        await referral(
            update,
            context
        )

        return

    if text.startswith("انتقال "):

        amount = parse_decimal(
            text[7:].strip()
        )

        if amount is None:

            await msg.reply_text(
                "❌ مثال:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱"
            )

        else:

            await do_transfer(
                update,
                context,
                amount
            )

        return

    if text in (
        "راهنما",
        "کمک",
        "help",
    ):

        await help_command(
            update,
            context
        )

        return

    # =====================================================
    # GAME
    # =====================================================

    parsed = parse_game_command(
        text
    )

    if parsed:

        # بازی فقط داخل گپ
        if not is_group(msg.chat):

            await msg.reply_text(
                "❌ بازی‌ها فقط داخل گپ هستند."
            )

            return

        count, game_type, bet = parsed

        await create_game(
            update,
            context,
            count,
            game_type,
            bet
        )


# =========================================================
# COMMAND GUARD
# =========================================================

async def command_guard(
    update,
    context
):
    """
    ضد دستور:

    اگر کاربر عادی دستور ناشناخته بفرستد،
    هیچ عملیات مدیریتی انجام نمی‌شود.
    """

    msg = update.message
    user = update.effective_user

    if not msg or not user:
        return

    # دستورات مدیریتی فقط برای مالک
    admin_commands = {
        "/admin",
        "/on",
        "/off",
    }

    command = (
        msg.text.split()[0]
        .lower()
        if msg.text
        else ""
    )

    if command in admin_commands:

        if not is_owner(user.id):

            await msg.reply_text(
                "❌ دسترسی ندارید."
            )

            return

        if command == "/on":

            set_bot_enabled(True)

            await msg.reply_text(
                "🟢 ربات روشن شد."
            )

        elif command == "/off":

            set_bot_enabled(False)

            await msg.reply_text(
                "🔴 ربات خاموش شد."
            )

        elif command == "/admin":

            await admin_panel(
                update,
                context
            )

        return


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(
    update,
    context
):

    error = context.error

    if isinstance(
        error,
        RetryAfter
    ):

        logger.warning(
            "Telegram RetryAfter: %s",
            error
        )

        return

    if isinstance(
        error,
        (
            TimedOut,
            NetworkError
        )
    ):

        logger.warning(
            "Temporary Telegram network error: %s",
            error
        )

        return

    logger.exception(
        "Unhandled bot error:",
        exc_info=error
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN در Environment Variables "
            "تنظیم نشده است."
        )

    # دیتابیس موجود حفظ می‌شود
    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(60)
        .write_timeout(60)
        .pool_timeout(30)
        .build()
    )

    # =====================================================
    # COMMANDS
    # =====================================================

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
            balance
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_panel
        )
    )

    application.add_handler(
        CommandHandler(
            "on",
            command_guard
        )
    )

    application.add_handler(
        CommandHandler(
            "off",
            command_guard
        )
    )

    # =====================================================
    # MEMBERSHIP
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    # =====================================================
    # MENU
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^menu_(balance|ref|transfer|games)$"
        )
    )

    # =====================================================
    # ADMIN
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|stats)$"
        )
    )

    # =====================================================
    # GAMES
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            join_game,
            pattern=r"^join_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game,
            pattern=r"^bot_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_\d+$"
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # =====================================================
    # ERROR
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started"
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
