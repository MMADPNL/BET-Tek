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
from telegram.helpers import mention_html

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
WIN_MULTIPLIER = Decimal("1.85")
MAX_GAME_COUNT = 20

# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger("BET_TEK")

# ============================================================
# GLOBAL GAME LOCKS
# ============================================================

GAME_LOCKS = {}
GAME_LOCKS_MASTER = asyncio.Lock()


async def get_game_lock(game_id):
    async with GAME_LOCKS_MASTER:
        if game_id not in GAME_LOCKS:
            GAME_LOCKS[game_id] = asyncio.Lock()
        return GAME_LOCKS[game_id]


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
                message_id INTEGER DEFAULT NULL,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER DEFAULT NULL,
                game_type TEXT NOT NULL,
                count INTEGER NOT NULL,
                bet TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'friends',
                status TEXT NOT NULL DEFAULT 'waiting',

                creator_total INTEGER DEFAULT 0,
                opponent_total INTEGER DEFAULT 0,

                creator_rolls_done INTEGER DEFAULT 0,
                opponent_rolls_done INTEGER DEFAULT 0,

                creator_refunded INTEGER DEFAULT 0,
                opponent_refunded INTEGER DEFAULT 0,

                payout_done INTEGER DEFAULT 0,

                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            INSERT OR IGNORE INTO settings(key, value)
            VALUES ('bot_enabled', '1')
        """)

        db.commit()


# ============================================================
# DIGITS / MONEY
# ============================================================

def normalize_digits(text):

    if text is None:
        return ""

    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789",
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

    return value.quantize(Decimal("0.01"))


def money(value):

    value = Decimal(str(value))

    if value == value.to_integral():
        return str(int(value))

    return f"{value:.2f}".rstrip("0").rstrip(".")


# ============================================================
# USERS
# ============================================================

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
                balance
            )
            VALUES (?, '0')
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


# ============================================================
# ATOMIC BALANCE CHANGE
# ============================================================

def change_balance(
    user_id,
    amount,
    transaction_type,
    description="",
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


# ============================================================
# BOT STATUS
# ============================================================

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
# MENTION
# ============================================================

def user_mention(user):

    if not user:
        return "کاربر"

    try:
        return mention_html(
            user.id,
            user.first_name or "کاربر",
        )
    except Exception:
        return (
            f'<a href="tg://user?id={user.id}">'
            f'{user.first_name or "کاربر"}'
            f'</a>'
        )


def id_mention(user_id, name="کاربر"):

    return (
        f'<a href="tg://user?id={int(user_id)}">'
        f'{name}'
        f'</a>'
    )


# ============================================================
# SAFE TELEGRAM
# ============================================================

async def safe_send_message(
    bot,
    chat_id,
    text,
    **kwargs,
):

    for attempt in range(3):

        try:

            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs,
            )

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Telegram network error: %s",
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2)

        except TelegramError as e:

            logger.error(
                "Telegram error: %s",
                e,
            )

            return None

        except Exception as e:

            logger.exception(
                "Send message error: %s",
                e,
            )

            return None

    return None


async def safe_delete_message(
    bot,
    chat_id,
    message_id,
):

    try:

        await bot.delete_message(
            chat_id=chat_id,
            message_id=message_id,
        )

        return True

    except Exception as e:

        logger.warning(
            "Delete failed: %s",
            e,
        )

        return False


async def safe_send_dice(
    bot,
    chat_id,
    emoji,
):

    for attempt in range(3):

        try:

            return await bot.send_dice(
                chat_id=chat_id,
                emoji=emoji,
            )

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Dice network error: %s",
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2)

        except TelegramError as e:

            logger.error(
                "Dice Telegram error: %s",
                e,
            )

            return None

        except Exception as e:

            logger.exception(
                "Dice error: %s",
                e,
            )

            return None

    return None


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(
    user_id,
    context,
):

    if is_owner(user_id):
        return True

    try:

        member = await context.bot.get_chat_member(
            CHANNEL_USERNAME,
            user_id,
        )

        return member.status in (
            "member",
            "administrator",
            "creator",
        )

    except Exception:

        return False


async def require_membership(
    update,
    context,
):

    user = update.effective_user

    if not user:
        return False

    if await check_membership(
        user.id,
        context,
    ):
        return True

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "📢 عضویت در BET_Tek",
                url=CHANNEL_URL,
            )
        ],
        [
            InlineKeyboardButton(
                "✅ بررسی عضویت",
                callback_data="check_membership",
            )
        ],
    ])

    if update.message:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔒 ابتدا عضو کانال BET_Tek شوید.",
            reply_markup=keyboard,
        )

    return False


async def membership_callback(
    update,
    context,
):

    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    if await check_membership(
        query.from_user.id,
        context,
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
                show_alert=True,
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
                callback_data="user_balance",
            ),
            InlineKeyboardButton(
                "👥 زیرمجموعه",
                callback_data="user_ref",
            ),
        ],
        [
            InlineKeyboardButton(
                "📚 راهنما",
                callback_data="user_help",
            ),
        ],
    ])


def admin_keyboard():

    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 روشن",
                callback_data="admin_on",
            ),
            InlineKeyboardButton(
                "🔴 خاموش",
                callback_data="admin_off",
            ),
        ],
        [
            InlineKeyboardButton(
                "➕ شارژ موجودی",
                callback_data="admin_charge",
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove",
            ),
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats",
            ),
        ],
    ])


# ============================================================
# START
# ============================================================

async def start(
    update,
    context,
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
                    arg.replace(
                        "ref_",
                        "",
                    )
                )
            except Exception:
                referrer_id = None

            if (
                referrer_id
                and referrer_id != user.id
            ):

                with closing(get_db()) as db:

                    current = db.execute("""
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                    """, (
                        user.id,
                    )).fetchone()

                    ref_user = db.execute("""
                        SELECT user_id
                        FROM users
                        WHERE user_id = ?
                    """, (
                        referrer_id,
                    )).fetchone()

                    already = db.execute("""
                        SELECT id
                        FROM referrals
                        WHERE referred_id = ?
                    """, (
                        user.id,
                    )).fetchone()

                    if (
                        current
                        and current["referrer_id"] is None
                        and ref_user
                        and not already
                    ):

                        db.execute("""
                            UPDATE users
                            SET referrer_id = ?
                            WHERE user_id = ?
                        """, (
                            referrer_id,
                            user.id,
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
                            str(REFERRAL_REWARD),
                        ))

                        db.commit()

                        try:

                            change_balance(
                                referrer_id,
                                REFERRAL_REWARD,
                                "referral",
                                f"Referral {user.id}",
                            )

                        except Exception:

                            logger.exception(
                                "Referral reward failed"
                            )

    await update.message.reply_text(
        "🤖 BET_TEK\n\n"
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
        reply_markup=main_keyboard(),
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(
    update,
    context,
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
                reply_markup=main_keyboard(),
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
    context,
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

        link = "لینک در دسترس نیست."

    with closing(get_db()) as db:

        row = db.execute("""
            SELECT COUNT(*) AS c
            FROM referrals
            WHERE referrer_id = ?
        """, (
            user.id,
        )).fetchone()

    total = row["c"] if row else 0

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
                reply_markup=main_keyboard(),
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
    context,
):

    text = (
        "📚 راهنمای BET_TEK\n\n"

        "💰 موجودی\n"
        "موجودی\n\n"

        "👥 زیرمجموعه\n"
        "زیر مجموعه\n\n"

        "🔄 انتقال\n"
        "روی پیام کاربر Reply کنید:\n"
        "انتقال 0.1\n\n"

        "🎮 بازی:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1\n\n"

        "🎮 بعد از ساخت بازی:\n"
        "👥 بازی با دوستان\n"
        "🤖 بازی با ربات\n"
        "❌ لغو بازی"
    )

    if update.callback_query:

        try:

            await update.callback_query.edit_message_text(
                text,
                reply_markup=main_keyboard(),
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
    amount,
):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "❌ روی پیام کاربر Reply کنید.\n\n"
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
            "❌ انتقال به خودتان مجاز نیست."
        )

        return

    if target.is_bot:

        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )

        return

    ensure_user(target)

    with closing(get_db()) as db:

        try:

            db.execute("BEGIN IMMEDIATE")

            sender = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                user.id,
            )).fetchone()

            receiver = db.execute("""
                SELECT balance
                FROM users
                WHERE user_id = ?
            """, (
                target.id,
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

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(sender_balance - amount),
                user.id,
            ))

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(receiver_balance + amount),
                target.id,
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
                f"To {target.id}",
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
                f"From {user.id}",
            ))

            db.commit()

        except Exception as e:

            db.rollback()

            if str(e) == "insufficient_balance":

                await message.reply_text(
                    "❌ موجودی شما کافی نیست."
                )

            else:

                logger.exception(
                    "Transfer error"
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
        parse_mode="HTML",
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(
    update,
    context,
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
        "👑 پنل مدیریت BET_TEK\n\n"
        f"⚙️ وضعیت: {status}\n\n"

        "➕ شارژ:\n"
        "از دکمه استفاده کن و بنویس:\n"
        "ID 100\n\n"

        "➖ کسر:\n"
        "از دکمه استفاده کن و بنویس:\n"
        "ID 100\n\n"

        "یا داخل گپ روی کاربر Reply کن:\n"
        "شارژ 100\n"
        "کسر 100\n\n"

        "دستورات مالک:\n"
        "/admin\n"
        "/resetgame ID",
        reply_markup=admin_keyboard(),
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(
    update,
    context,
):

    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):

        try:

            await query.answer(
                "❌ دسترسی ندارید.",
                show_alert=True,
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
                "👑 پنل مدیریت BET_TEK\n\n"
                "🟢 ربات روشن است.",
                reply_markup=admin_keyboard(),
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
                "👑 پنل مدیریت BET_TEK\n\n"
                "🔴 ربات خاموش است.",
                reply_markup=admin_keyboard(),
            )

        except Exception:
            pass

        return

    # --------------------------------------------------------
    # CHARGE
    # --------------------------------------------------------

    if data == "admin_charge":

        context.user_data["admin_operation"] = "charge"

        await query.message.reply_text(
            "➕ شارژ موجودی\n\n"
            "آیدی عددی + مبلغ را بفرست:\n\n"
            "مثال:\n"
            "8552447077 100\n"
            "8552447077 0.5\n\n"
            "⚠️ مالک هم می‌تواند شارژ شود."
        )

        return

    # --------------------------------------------------------
    # REMOVE
    # --------------------------------------------------------

    if data == "admin_remove":

        context.user_data["admin_operation"] = "remove"

        await query.message.reply_text(
            "➖ کسر موجودی\n\n"
            "آیدی عددی + مبلغ را بفرست:\n\n"
            "مثال:\n"
            "8552447077 100\n"
            "8552447077 0.5\n\n"
            "⚠️ مالک هم می‌تواند کسر شود."
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

            active_games = db.execute("""
                SELECT COUNT(*) AS c
                FROM games
                WHERE status IN ('waiting', 'playing')
            """).fetchone()["c"]

        await query.edit_message_text(
            "📊 آمار BET_TEK\n\n"
            f"👤 کاربران: {users}\n"
            f"💰 مجموع موجودی: {total:.2f} TRX\n"
            f"👥 زیرمجموعه‌ها: {referrals}\n"
            f"🎮 کل بازی‌ها: {games}\n"
            f"🔥 بازی‌های فعال: {active_games}\n\n"
            f"⚙️ وضعیت: "
            f"{'🟢 روشن' if bot_enabled() else '🔴 خاموش'}",
            reply_markup=admin_keyboard(),
        )


# ============================================================
# ADMIN ID + AMOUNT
# ============================================================

async def admin_id_amount(
    update,
    context,
):

    user = update.effective_user

    if not user or not is_owner(user.id):
        return False

    operation = context.user_data.get(
        "admin_operation"
    )

    if operation not in (
        "charge",
        "remove",
    ):

        return False

    text = normalize_digits(
        update.message.text.strip()
    )

    match = re.fullmatch(
        r"(\d+)\s+([0-9]+(?:\.[0-9]+)?)",
        text,
    )

    if not match:
        return False

    target_id = int(
        match.group(1)
    )

    amount = parse_amount(
        match.group(2)
    )

    if amount is None:

        await update.message.reply_text(
            "❌ مبلغ صحیح نیست."
        )

        return True

    # --------------------------------------------------------
    # مهم:
    # مالک هم مجاز است
    # --------------------------------------------------------

    ensure_user_id(target_id)

    try:

        if operation == "charge":

            change_balance(
                target_id,
                amount,
                "admin_charge",
                f"Owner {user.id}",
            )

            operation_name = "شارژ"

        else:

            change_balance(
                target_id,
                -amount,
                "admin_remove",
                f"Owner {user.id}",
            )

            operation_name = "کسر"

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await update.message.reply_text(
                "❌ موجودی کاربر برای کسر کافی نیست."
            )

        else:

            await update.message.reply_text(
                "❌ عملیات انجام نشد."
            )

        return True

    except Exception:

        logger.exception(
            "Admin balance error"
        )

        await update.message.reply_text(
            "❌ عملیات انجام نشد."
        )

        return True

    context.user_data.pop(
        "admin_operation",
        None,
    )

    await update.message.reply_text(
        "✅ عملیات با موفقیت انجام شد.\n\n"
        f"📌 عملیات: {operation_name}\n"
        f"🆔 آیدی: {target_id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target_id))} TRX"
    )

    return True


# ============================================================
# USER CALLBACK
# ============================================================

async def user_callback(
    update,
    context,
):

    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    if query.data == "user_balance":

        await show_balance(
            update,
            context,
        )

    elif query.data == "user_ref":

        await show_referral(
            update,
            context,
        )

    elif query.data == "user_help":

        await show_help(
            update,
            context,
        )


# ============================================================
# GAMES
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

    match = re.fullmatch(
        r"(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)",
        text,
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

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if bet is None:
        return None

    if name not in GAME_NAMES:
        return None

    return (
        count,
        GAME_NAMES[name],
        bet,
    )


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(
    update,
    context,
    count,
    game_type,
    bet,
):

    user = update.effective_user
    chat = update.effective_chat
    message = update.message

    ensure_user(user)

    if not await require_membership(
        update,
        context,
    ):
        return

    if not bot_enabled():

        await message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    if bet < MIN_GAME_BET:

        await message.reply_text(
            f"❌ حداقل شرط "
            f"{money(MIN_GAME_BET)} TRX است."
        )

        return

    # --------------------------------------------------------
    # ضد بازی همزمان
    # --------------------------------------------------------

    with closing(get_db()) as db:

        active = db.execute("""
            SELECT id
            FROM games
            WHERE
                (
                    creator_id = ?
                    OR opponent_id = ?
                )
                AND status IN ('waiting', 'playing')
            LIMIT 1
        """, (
            user.id,
            user.id,
        )).fetchone()

    if active:

        await message.reply_text(
            "❌ شما یک بازی فعال دارید.\n"
            "ابتدا بازی قبلی را تمام یا لغو کنید."
        )

        return

    # --------------------------------------------------------
    # کسر شرط اتمیک
    # --------------------------------------------------------

    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type}",
        )

    except ValueError:

        await message.reply_text(
            "❌ موجودی شما کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{money(get_balance(user.id))} TRX"
        )

        return

    # --------------------------------------------------------
    # ساخت بازی
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
                status
            )
            VALUES (?, ?, ?, ?, ?, 'friends', 'waiting')
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
                callback_data=f"join_{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}",
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو بازی",
                callback_data=f"cancel_{game_id}",
            )
        ],
    ])

    sent = await message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{emoji} {name}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: {user_mention(user)}\n\n"
        "👇 نوع بازی را انتخاب کنید.",
        reply_markup=keyboard,
        parse_mode="HTML",
    )

    if sent:

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET message_id = ?
                WHERE id = ?
            """, (
                sent.message_id,
                game_id,
            ))

            db.commit()


# ============================================================
# ROLL
# ============================================================

async def perform_rolls(
    context,
    chat_id,
    game_type,
    count,
):

    total = 0
    successful = 0

    for _ in range(count):

        msg = await safe_send_dice(
            context.bot,
            chat_id,
            GAME_EMOJI[game_type],
        )

        if not msg:

            # اگر تلگرام پرتاب را نفرستاد
            # ادامه نده؛ بازی نباید نتیجه جعلی بسازد
            raise RuntimeError(
                "dice_send_failed"
            )

        try:

            value = int(
                msg.dice.value
            )

        except Exception:

            raise RuntimeError(
                "invalid_dice_value"
            )

        total += value
        successful += 1

        await asyncio.sleep(1)

    return total, successful


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game_callback(
    update,
    context,
):

    query = update.callback_query
    user = query.from_user

    if (
        not await check_membership(
            user.id,
            context,
        )
        and not is_owner(user.id)
    ):

        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True,
        )

        return

    try:

        game_id = int(
            query.data.replace(
                "join_",
                "",
            )
        )

    except Exception:

        return

    ensure_user(user)

    lock = await get_game_lock(
        game_id
    )

    async with lock:

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
                        show_alert=True,
                    )

                    return

                if game["status"] != "waiting":

                    db.rollback()

                    await query.answer(
                        "❌ این بازی دیگر فعال نیست.",
                        show_alert=True,
                    )

                    return

                if game["creator_id"] == user.id:

                    db.rollback()

                    await query.answer(
                        "❌ نمی‌توانید با خودتان بازی کنید.",
                        show_alert=True,
                    )

                    return

                # ضد بازی فعال
                active = db.execute("""
                    SELECT id
                    FROM games
                    WHERE
                        (
                            creator_id = ?
                            OR opponent_id = ?
                        )
                        AND status IN ('waiting', 'playing')
                        AND id != ?
                    LIMIT 1
                """, (
                    user.id,
                    user.id,
                    game_id,
                )).fetchone()

                if active:

                    db.rollback()

                    await query.answer(
                        "❌ شما یک بازی فعال دیگر دارید.",
                        show_alert=True,
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
                        show_alert=True,
                    )

                    return

                balance = Decimal(
                    row["balance"]
                )

                if balance < bet:

                    db.rollback()

                    await query.answer(
                        "❌ موجودی شما کافی نیست.",
                        show_alert=True,
                    )

                    return

                # کسر شرط نفر دوم
                db.execute("""
                    UPDATE users
                    SET balance = ?
                    WHERE user_id = ?
                """, (
                    str(balance - bet),
                    user.id,
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
                    f"Join game {game_id}",
                ))

                cursor = db.execute("""
                    UPDATE games
                    SET
                        opponent_id = ?,
                        status = 'playing',
                        mode = 'friends'
                    WHERE id = ?
                    AND status = 'waiting'
                """, (
                    user.id,
                    game_id,
                ))

                if cursor.rowcount != 1:

                    db.rollback()

                    await query.answer(
                        "❌ بازی قبلاً توسط شخص دیگری گرفته شد.",
                        show_alert=True,
                    )

                    return

                db.commit()

            except Exception:

                db.rollback()

                logger.exception(
                    "Join game error"
                )

                await query.answer(
                    "❌ شروع بازی ناموفق بود.",
                    show_alert=True,
                )

                return

        # ----------------------------------------------------
        # حذف پیام دکمه‌دار
        # ----------------------------------------------------

        await safe_delete_message(
            context.bot,
            query.message.chat_id,
            query.message.message_id,
        )

        emoji, name = GAME_INFO[
            game["game_type"]
        ]

        opponent_tag = user_mention(
            user
        )

        creator_tag = id_mention(
            game["creator_id"],
            "سازنده",
        )

        new_message = await safe_send_message(
            context.bot,
            game["chat_id"],
            "🎮 بازی شروع شد!\n\n"
            f"{emoji} {name}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط هر نفر: "
            f"{money(Decimal(game['bet']))} TRX\n\n"
            f"👤 سازنده: {creator_tag}\n"
            f"👤 بازیکن دوم: {opponent_tag}\n\n"
            f"🎯 ابتدا نوبت {creator_tag} است.\n"
            "تمام پرتاب‌های سازنده انجام می‌شود.",
            parse_mode="HTML",
        )

        if new_message:

            with closing(get_db()) as db:

                db.execute("""
                    UPDATE games
                    SET message_id = ?
                    WHERE id = ?
                """, (
                    new_message.message_id,
                    game_id,
                ))

                db.commit()

        await play_game(
            context,
            game_id,
        )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game_callback(
    update,
    context,
):

    query = update.callback_query
    user = query.from_user

    if (
        not await check_membership(
            user.id,
            context,
        )
        and not is_owner(user.id)
    ):

        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True,
        )

        return

    try:

        game_id = int(
            query.data.replace(
                "bot_",
                "",
            )
        )

    except Exception:

        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

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
                        show_alert=True,
                    )

                    return

                if game["status"] != "waiting":

                    db.rollback()

                    await query.answer(
                        "❌ بازی فعال نیست.",
                        show_alert=True,
                    )

                    return

                if game["creator_id"] != user.id:

                    db.rollback()

                    await query.answer(
                        "❌ فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
                        show_alert=True,
                    )

                    return

                db.execute("""
                    UPDATE games
                    SET
                        opponent_id = -1,
                        mode = 'bot',
                        status = 'playing'
                    WHERE id = ?
                    AND status = 'waiting'
                """, (
                    game_id,
                ))

                db.commit()

            except Exception:

                db.rollback()

                logger.exception(
                    "Bot game start error"
                )

                return

        # ----------------------------------------------------
        # پیام قبلی حذف شود
        # ----------------------------------------------------

        await safe_delete_message(
            context.bot,
            query.message.chat_id,
            query.message.message_id,
        )

        emoji, name = GAME_INFO[
            game["game_type"]
        ]

        player_tag = user_mention(
            user
        )

        new_message = await safe_send_message(
            context.bot,
            game["chat_id"],
            "🤖 بازی با ربات شروع شد!\n\n"
            f"{emoji} {name}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط: "
            f"{money(Decimal(game['bet']))} TRX\n\n"
            f"🎯 نوبت {player_tag}\n"
            "ابتدا تمام پرتاب‌های شما انجام می‌شود.\n"
            "بعد از اتمام، ربات پرتاب می‌کند.",
            parse_mode="HTML",
        )

        if new_message:

            with closing(get_db()) as db:

                db.execute("""
                    UPDATE games
                    SET message_id = ?
                    WHERE id = ?
                """, (
                    new_message.message_id,
                    game_id,
                ))

                db.commit()

        await play_game(
            context,
            game_id,
        )


# ============================================================
# CANCEL
# ============================================================

async def cancel_game_callback(
    update,
    context,
):

    query = update.callback_query
    user = query.from_user

    try:

        game_id = int(
            query.data.replace(
                "cancel_",
                "",
            )
        )

    except Exception:

        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

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
                        show_alert=True,
                    )

                    return

                if game["status"] != "waiting":

                    db.rollback()

                    await query.answer(
                        "❌ بازی دیگر قابل لغو نیست.",
                        show_alert=True,
                    )

                    return

                if (
                    game["creator_id"] != user.id
                    and not is_owner(user.id)
                ):

                    db.rollback()

                    await query.answer(
                        "❌ فقط سازنده یا مالک.",
                        show_alert=True,
                    )

                    return

                cursor = db.execute("""
                    UPDATE games
                    SET status = 'cancelled'
                    WHERE id = ?
                    AND status = 'waiting'
                """, (
                    game_id,
                ))

                if cursor.rowcount != 1:

                    db.rollback()

                    await query.answer(
                        "❌ بازی قبلاً تغییر کرده است.",
                        show_alert=True,
                    )

                    return

                db.commit()

            except Exception:

                db.rollback()

                logger.exception(
                    "Cancel game error"
                )

                return

        # برگشت شرط فقط یک بار
        try:

            change_balance(
                game["creator_id"],
                Decimal(game["bet"]),
                "game_cancel_refund",
                f"Cancel {game_id}",
            )

        except Exception:

            logger.exception(
                "Cancel refund failed"
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
    game_id,
):

    lock = await get_game_lock(
        game_id
    )

    # اگر قبلاً در حال اجراست، دوباره اجرا نشود
    if lock.locked():
        # اگر همین coroutine خودش lock را گرفته باشد،
        # نباید دوباره lock بگیریم.
        # بنابراین اجرای واقعی این تابع با یک lock داخلی
        # در متدهای callback مدیریت نمی‌شود.
        pass

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

    try:

        creator_total, creator_rolls = await perform_rolls(
            context,
            chat_id,
            game_type,
            count,
        )

    except Exception:

        logger.exception(
            "Creator rolls failed"
        )

        await safe_send_message(
            context.bot,
            chat_id,
            "⚠️ بازی در زمان پرتاب دچار خطا شد.\n"
            f"🆔 شناسه بازی: {game_id}\n\n"
            "مالک می‌تواند دستور زیر را بزند:\n"
            f"/resetgame {game_id}",
        )

        return

    with closing(get_db()) as db:

        db.execute("""
            UPDATE games
            SET
                creator_total = ?,
                creator_rolls_done = ?
            WHERE id = ?
            AND status = 'playing'
        """, (
            creator_total,
            creator_rolls,
            game_id,
        ))

        db.commit()

    # --------------------------------------------------------
    # BOT
    # --------------------------------------------------------

    if game["mode"] == "bot":

        player_tag = id_mention(
            game["creator_id"],
            "بازیکن",
        )

        await safe_send_message(
            context.bot,
            chat_id,
            "✅ تمام پرتاب‌های "
            f"{player_tag} تمام شد.\n\n"
            "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد...",
            parse_mode="HTML",
        )

        try:

            bot_total, bot_rolls = await perform_rolls(
                context,
                chat_id,
                game_type,
                count,
            )

        except Exception:

            logger.exception(
                "Bot rolls failed"
            )

            await safe_send_message(
                context.bot,
                chat_id,
                "⚠️ پرتاب ربات با خطا مواجه شد.\n"
                f"🆔 شناسه بازی: {game_id}\n\n"
                "مالک می‌تواند بازی را ریست کند:\n"
                f"/resetgame {game_id}",
            )

            return

        with closing(get_db()) as db:

            db.execute("""
                UPDATE games
                SET
                    opponent_total = ?,
                    opponent_rolls_done = ?
                WHERE id = ?
                AND status = 'playing'
            """, (
                bot_total,
                bot_rolls,
                game_id,
            ))

            db.commit()

        await finish_game(
            context,
            game_id,
        )

        return

    # --------------------------------------------------------
    # FRIEND
    # --------------------------------------------------------

    opponent_id = game["opponent_id"]

    opponent_tag = id_mention(
        opponent_id,
        "بازیکن دوم",
    )

    await safe_send_message(
        context.bot,
        chat_id,
        "✅ تمام پرتاب‌های سازنده تمام شد.\n\n"
        f"🎯 حالا نوبت {opponent_tag} است.\n"
        "تمام پرتاب‌های بازیکن دوم انجام می‌شود...",
        parse_mode="HTML",
    )

    try:

        opponent_total, opponent_rolls = await perform_rolls(
            context,
            chat_id,
            game_type,
            count,
        )

    except Exception:

        logger.exception(
            "Opponent rolls failed"
        )

        await safe_send_message(
            context.bot,
            chat_id,
            "⚠️ پرتاب بازیکن دوم با خطا مواجه شد.\n"
            f"🆔 شناسه بازی: {game_id}\n\n"
            "مالک می‌تواند بازی را ریست کند:\n"
            f"/resetgame {game_id}",
        )

        return

    with closing(get_db()) as db:

        db.execute("""
            UPDATE games
            SET
                opponent_total = ?,
                opponent_rolls_done = ?
            WHERE id = ?
            AND status = 'playing'
        """, (
            opponent_total,
            opponent_rolls,
            game_id,
        ))

        db.commit()

    await finish_game(
        context,
        game_id,
    )


# ============================================================
# FINISH GAME - ATOMIC PAYOUT
# ============================================================

async def finish_game(
    context,
    game_id,
):

    lock = await get_game_lock(
        game_id
    )

    async with lock:

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

                # ------------------------------------------------
                # ضد پرداخت دوباره
                # ------------------------------------------------

                if int(game["payout_done"]) == 1:

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

                creator_id = int(
                    game["creator_id"]
                )

                opponent_id = int(
                    game["opponent_id"]
                )

                mode = game["mode"]

                # ------------------------------------------------
                # نتیجه
                # ------------------------------------------------

                if creator_total == opponent_total:

                    db.execute("""
                        UPDATE users
                        SET balance =
                            CAST(balance AS DECIMAL)
                            + ?
                        WHERE user_id = ?
                    """, (
                        str(bet),
                        creator_id,
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
                        creator_id,
                        "game_draw_refund",
                        str(bet),
                        f"Draw {game_id}",
                    ))

                    if mode == "friends":

                        db.execute("""
                            UPDATE users
                            SET balance =
                                CAST(balance AS DECIMAL)
                                + ?
                            WHERE user_id = ?
                        """, (
                            str(bet),
                            opponent_id,
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
                            opponent_id,
                            "game_draw_refund",
                            str(bet),
                            f"Draw {game_id}",
                        ))

                else:

                    payout = (
                        bet * WIN_MULTIPLIER
                    )

                    if creator_total > opponent_total:

                        winner_id = creator_id

                    else:

                        winner_id = opponent_id

                    db.execute("""
                        UPDATE users
                        SET balance =
                            CAST(balance AS DECIMAL)
                            + ?
                        WHERE user_id = ?
                    """, (
                        str(payout),
                        winner_id,
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
                        winner_id,
                        "game_win",
                        str(payout),
                        f"Game win {game_id}",
                    ))

                # ------------------------------------------------
                # فقط همین‌جا بازی finished می‌شود
                # ------------------------------------------------

                cursor = db.execute("""
                    UPDATE games
                    SET
                        status = 'finished',
                        payout_done = 1
                    WHERE id = ?
                    AND status = 'playing'
                    AND payout_done = 0
                """, (
                    game_id,
                ))

                if cursor.rowcount != 1:

                    db.rollback()
                    return

                db.commit()

            except Exception:

                db.rollback()

                logger.exception(
                    "Atomic finish error"
                )

                return

    # ========================================================
    # FINAL MESSAGE
    # ========================================================

    if mode == "friends":

        creator_tag = id_mention(
            creator_id,
            "سازنده",
        )

        opponent_tag = id_mention(
            opponent_id,
            "بازیکن دوم",
        )

        if creator_total == opponent_total:

            result_text = (
                "🤝 بازی مساوی شد.\n"
                "💰 شرط هر دو نفر برگشت داده شد."
            )

        else:

            winner_id = (
                creator_id
                if creator_total > opponent_total
                else opponent_id
            )

            winner_tag = id_mention(
                winner_id,
                "برنده",
            )

            payout = bet * WIN_MULTIPLIER

            result_text = (
                f"🏆 برنده: {winner_tag}\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        final_text = (
            "🏁 نتیجه بازی\n\n"
            f"{GAME_INFO[game['game_type']][0]} "
            f"{GAME_INFO[game['game_type']][1]}\n\n"
            f"👤 {creator_tag}: {creator_total}\n"
            f"👤 {opponent_tag}: {opponent_total}\n\n"
            f"{result_text}"
        )

    else:

        player_tag = id_mention(
            creator_id,
            "بازیکن",
        )

        if creator_total == opponent_total:

            result_text = (
                "🤝 بازی مساوی شد.\n"
                "💰 شرط برگشت داده شد."
            )

        elif creator_total > opponent_total:

            payout = bet * WIN_MULTIPLIER

            result_text = (
                f"🏆 {player_tag} برنده شد!\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        else:

            result_text = (
                "🤖 ربات برنده شد."
            )

        final_text = (
            "🏁 نتیجه بازی\n\n"
            f"{GAME_INFO[game['game_type']][0]} "
            f"{GAME_INFO[game['game_type']][1]}\n\n"
            f"👤 {player_tag}: {creator_total}\n"
            f"🤖 ربات: {opponent_total}\n\n"
            f"{result_text}"
        )

    # --------------------------------------------------------
    # نتیجه هیچ‌وقت حذف نمی‌شود
    # --------------------------------------------------------

    await safe_send_message(
        context.bot,
        chat_id,
        final_text,
        parse_mode="HTML",
    )


# ============================================================
# RESET GAME
# ============================================================

async def reset_game(
    update,
    context,
):

    user = update.effective_user

    if not user or not is_owner(user.id):

        await update.message.reply_text(
            "❌ فقط مالک می‌تواند بازی را ریست کند."
        )

        return

    if not context.args:

        await update.message.reply_text(
            "❌ فرمت صحیح:\n\n"
            "/resetgame ID\n\n"
            "مثال:\n"
            "/resetgame 123"
        )

        return

    try:

        game_id = int(
            normalize_digits(
                context.args[0]
            )
        )

    except Exception:

        await update.message.reply_text(
            "❌ آیدی بازی صحیح نیست."
        )

        return

    lock = await get_game_lock(
        game_id
    )

    async with lock:

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

                    await update.message.reply_text(
                        "❌ بازی پیدا نشد."
                    )

                    return

                status = game["status"]

                # ------------------------------------------------
                # بازی finished:
                # هیچ پولی دوباره برنگردد
                # ------------------------------------------------

                if status == "finished":

                    db.rollback()

                    await update.message.reply_text(
                        "❌ این بازی قبلاً تمام شده است.\n"
                        "برای جلوگیری از دوباره‌پرداخت، ریست نمی‌شود."
                    )

                    return

                # ------------------------------------------------
                # reset
                # ------------------------------------------------

                db.execute("""
                    UPDATE games
                    SET status = 'reset'
                    WHERE id = ?
                    AND status IN ('waiting', 'playing')
                """, (
                    game_id,
                ))

                # ------------------------------------------------
                # creator refund
                # ------------------------------------------------

                if int(game["creator_refunded"]) == 0:

                    creator_id = int(
                        game["creator_id"]
                    )

                    bet = Decimal(
                        game["bet"]
                    )

                    db.execute("""
                        UPDATE users
                        SET balance =
                            CAST(balance AS DECIMAL)
                            + ?
                        WHERE user_id = ?
                    """, (
                        str(bet),
                        creator_id,
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
                        creator_id,
                        "game_reset_refund",
                        str(bet),
                        f"Reset {game_id}",
                    ))

                    db.execute("""
                        UPDATE games
                        SET creator_refunded = 1
                        WHERE id = ?
                    """, (
                        game_id,
                    ))

                # ------------------------------------------------
                # opponent refund
                # ------------------------------------------------

                opponent_id = game["opponent_id"]

                if (
                    game["mode"] == "friends"
                    and opponent_id
                    and int(opponent_id) > 0
                    and int(game["opponent_refunded"]) == 0
                ):

                    opponent_id = int(
                        opponent_id
                    )

                    bet = Decimal(
                        game["bet"]
                    )

                    db.execute("""
                        UPDATE users
                        SET balance =
                            CAST(balance AS DECIMAL)
                            + ?
                        WHERE user_id = ?
                    """, (
                        str(bet),
                        opponent_id,
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
                        opponent_id,
                        "game_reset_refund",
                        str(bet),
                        f"Reset {game_id}",
                    ))

                    db.execute("""
                        UPDATE games
                        SET opponent_refunded = 1
                        WHERE id = ?
                    """, (
                        game_id,
                    ))

                db.commit()

            except Exception:

                db.rollback()

                logger.exception(
                    "Reset error"
                )

                await update.message.reply_text(
                    "❌ ریست بازی انجام نشد."
                )

                return

    await update.message.reply_text(
        "♻️ بازی با موفقیت ریست شد.\n\n"
        f"🆔 بازی: {game_id}\n"
        "💰 شرط‌های کسرشده فقط یک بار برگشت داده شدند.\n"
        "🛡 ضد دوباره‌پرداخت فعال است."
    )


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(
    update,
    context,
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    raw = message.text or ""

    text = normalize_digits(
        raw.strip()
    )

    # ========================================================
    # ADMIN PANEL OPERATION
    # ========================================================

    if is_owner(user.id):

        handled = await admin_id_amount(
            update,
            context,
        )

        if handled:
            return

    # ========================================================
    # ADMIN GROUP CHARGE
    # ========================================================

    if is_owner(user.id):

        charge_match = re.fullmatch(
            r"شارژ\s+([0-9]+(?:\.[0-9]+)?)",
            text,
        )

        if charge_match:

            amount = parse_amount(
                charge_match.group(1)
            )

            if amount is None:

                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )

                return

            # ------------------------------------------------
            # روش اول: Reply
            # ------------------------------------------------

            if message.reply_to_message:

                target = (
                    message.reply_to_message.from_user
                )

                if not target or target.is_bot:

                    await message.reply_text(
                        "❌ کاربر مقصد معتبر نیست."
                    )

                    return

                target_id = target.id
                target_tag = user_mention(
                    target
                )

            else:

                await message.reply_text(
                    "❌ برای شارژ در گپ باید روی پیام کاربر Reply کنی.\n\n"
                    "مثال:\n"
                    "شارژ 100\n\n"
                    "یا از پنل مدیریت استفاده کن:\n"
                    "ID 100"
                )

                return

            ensure_user_id(target_id)

            try:

                change_balance(
                    target_id,
                    amount,
                    "admin_charge",
                    f"Owner {user.id}",
                )

            except Exception:

                logger.exception(
                    "Group charge error"
                )

                await message.reply_text(
                    "❌ شارژ انجام نشد."
                )

                return

            await message.reply_text(
                "✅ شارژ انجام شد.\n\n"
                f"👤 کاربر: {target_tag}\n"
                f"💰 مبلغ: {money(amount)} TRX\n"
                f"💳 موجودی جدید: "
                f"{money(get_balance(target_id))} TRX",
                parse_mode="HTML",
            )

            return

        # ====================================================
        # ADMIN GROUP REMOVE
        # ====================================================

        remove_match = re.fullmatch(
            r"کسر\s+([0-9]+(?:\.[0-9]+)?)",
            text,
        )

        if remove_match:

            amount = parse_amount(
                remove_match.group(1)
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

                if not target or target.is_bot:

                    await message.reply_text(
                        "❌ کاربر معتبر نیست."
                    )

                    return

                target_id = target.id
                target_tag = user_mention(
                    target
                )

            else:

                await message.reply_text(
                    "❌ برای کسر در گپ باید روی پیام کاربر Reply کنی.\n\n"
                    "مثال:\n"
                    "کسر 100\n\n"
                    "یا از پنل مدیریت استفاده کن:\n"
                    "ID 100"
                )

                return

            ensure_user_id(target_id)

            try:

                change_balance(
                    target_id,
                    -amount,
                    "admin_remove",
                    f"Owner {user.id}",
                )

            except ValueError as e:

                if str(e) == "insufficient_balance":

                    await message.reply_text(
                        "❌ موجودی کاربر کافی نیست."
                    )

                else:

                    await message.reply_text(
                        "❌ کسر انجام نشد."
                    )

                return

            except Exception:

                logger.exception(
                    "Group remove error"
                )

                await message.reply_text(
                    "❌ کسر انجام نشد."
                )

                return

            await message.reply_text(
                "✅ کسر انجام شد.\n\n"
                f"👤 کاربر: {target_tag}\n"
                f"💰 مبلغ: {money(amount)} TRX\n"
                f"💳 موجودی جدید: "
                f"{money(get_balance(target_id))} TRX",
                parse_mode="HTML",
            )

            return

        # ====================================================
        # ON
        # ====================================================

        if text == "روشن":

            set_bot_enabled(True)

            await message.reply_text(
                "🟢 ربات روشن شد."
            )

            return

        # ====================================================
        # OFF
        # ====================================================

        if text == "خاموش":

            set_bot_enabled(False)

            await message.reply_text(
                "🔴 ربات خاموش شد."
            )

            return

    # ========================================================
    # BOT OFF
    # ========================================================

    # دستورات مدیریتی قبلاً پردازش شده‌اند.
    if not bot_enabled():

        return

    # ========================================================
    # BALANCE
    # ========================================================

    if text in (
        "موجودی",
        "موجودی من",
        "موجودی‌من",
        "balance",
    ):

        await show_balance(
            update,
            context,
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
        "referral",
    ):

        await show_referral(
            update,
            context,
        )

        return

    # ========================================================
    # TRANSFER
    # ========================================================

    if text.startswith("انتقال"):

        match = re.fullmatch(
            r"انتقال\s+([0-9]+(?:\.[0-9]+)?)",
            text,
        )

        if not match:

            await message.reply_text(
                "❌ فرمت صحیح:\n"
                "انتقال 0.1\n\n"
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
            amount,
        )

        return

    # ========================================================
    # GAME
    # ========================================================

    game = parse_game(
        text
    )

    if game:

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):

            await message.reply_text(
                "❌ بازی فقط داخل گپ انجام می‌شود."
            )

            return

        count, game_type, bet = game

        await create_game(
            update,
            context,
            count,
            game_type,
            bet,
        )

        return

    # ========================================================
    # ضد دستور:
    # هر پیام فقط یک مسیر را اجرا می‌کند.
    # ========================================================


# ============================================================
# COMMANDS
# ============================================================

async def admin_command(
    update,
    context,
):

    await admin_panel(
        update,
        context,
    )


async def balance_command(
    update,
    context,
):

    await show_balance(
        update,
        context,
    )


async def referral_command(
    update,
    context,
):

    await show_referral(
        update,
        context,
    )


async def help_command(
    update,
    context,
):

    await show_help(
        update,
        context,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(
    update,
    context,
):

    error = context.error

    if isinstance(
        error,
        (TimedOut, NetworkError),
    ):

        logger.warning(
            "Temporary Telegram error: %s",
            error,
        )

        return

    logger.exception(
        "Unhandled exception",
        exc_info=error,
    )


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN تنظیم نشده است.\n"
            "در Secrets مقدار BOT_TOKEN را قرار بده."
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
            start,
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            help_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "balance",
            balance_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "referral",
            referral_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_command,
        )
    )

    application.add_handler(
        CommandHandler(
            "resetgame",
            reset_game,
        )
    )

    # ========================================================
    # CALLBACKS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|charge|remove|stats)$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern=r"^user_(balance|ref|help)$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_game_callback,
            pattern=r"^join_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game_callback,
            pattern=r"^bot_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game_callback,
            pattern=r"^cancel_\d+$",
        )
    )

    # ========================================================
    # TEXT
    # ========================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    # ========================================================
    # ERRORS
    # ========================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_TEK started."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
