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
from telegram.error import (
    TelegramError,
    TimedOut,
    NetworkError,
    RetryAfter,
)
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    ApplicationHandlerStop,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)


# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

OWNER_IDS = {
    8552447077,
    7966359658,
    7221112088,
}

CHANNEL_USERNAME = "@BET_Tek"
CHANNEL_URL = "https://t.me/BET_Tek"

DB_FILE = "bot.db"

MIN_WITHDRAW = Decimal("3.5")
REFERRAL_REWARD = Decimal("0.05")
MIN_GAME_BET = Decimal("0.1")
MAX_GAME_COUNT = 20

MAX_RETRIES = 3


# =========================================================
# LOGGING
# =========================================================

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger("BET_BT")


# =========================================================
# DATABASE
# =========================================================

def get_db():
    db = sqlite3.connect(
        DB_FILE,
        timeout=30,
        check_same_thread=False,
    )
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=30000")
    db.execute("PRAGMA foreign_keys=ON")
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
                reward TEXT NOT NULL DEFAULT '0.05',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)

        db.execute("""
            CREATE TABLE IF NOT EXISTS withdrawals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount TEXT NOT NULL,
                wallet TEXT NOT NULL,
                card TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                message_chat_id INTEGER,
                message_id INTEGER,
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
                mode TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                creator_total REAL DEFAULT 0,
                opponent_total REAL DEFAULT 0,
                current_player INTEGER,
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
# NUMBER HELPERS
# =========================================================

def normalize_digits(text: str) -> str:
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def parse_decimal(text: str):
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


def money(value) -> str:
    value = Decimal(str(value))

    if value == value.to_integral():
        return f"{value:.0f}"

    return f"{value:.2f}"


def parse_game_command(text: str):

    text = normalize_digits(text.strip())

    pattern = (
        r"^(\d+)\s+([^\s]+)\s+"
        r"([0-9]+(?:\.[0-9]+)?)$"
    )

    match = re.match(pattern, text)

    if not match:
        return None

    count = int(match.group(1))
    game_name = match.group(2).lower()
    bet = parse_decimal(match.group(3))

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if bet is None:
        return None

    games = {
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

    if game_name not in games:
        return None

    return count, games[game_name], bet


def game_farsi_name(game_type):
    return {
        "dice": "🎲 تاس",
        "darts": "🎯 دارت",
        "bowling": "🎳 بولینگ",
        "basketball": "🏀 بسکتبال",
    }.get(game_type, game_type)


GAME_EMOJIS = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


# =========================================================
# SAFE TELEGRAM HELPERS
# =========================================================

async def safe_sleep(seconds):
    try:
        await asyncio.sleep(seconds)
    except asyncio.CancelledError:
        raise


async def safe_send_message(
    bot,
    chat_id,
    text,
    **kwargs
):
    """
    ارسال پیام با تلاش مجدد در صورت Timeout/NetworkError.
    """

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs
            )

        except RetryAfter as e:

            wait_time = float(e.retry_after) + 1

            logger.warning(
                "Telegram asked to retry after %s seconds",
                wait_time
            )

            await safe_sleep(wait_time)

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "send_message network error "
                "(attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                e
            )

            if attempt < MAX_RETRIES:
                await safe_sleep(attempt * 2)

        except TelegramError as e:

            logger.error(
                "Telegram send_message error: %s",
                e
            )

            break

        except Exception as e:

            logger.exception(
                "Unexpected send_message error: %s",
                e
            )

            break

    return None


async def safe_edit_message(
    query,
    text,
    **kwargs
):
    """
    ویرایش پیام با retry.
    """

    for attempt in range(1, MAX_RETRIES + 1):

        try:
            return await query.edit_message_text(
                text=text,
                **kwargs
            )

        except RetryAfter as e:

            wait_time = float(e.retry_after) + 1

            await safe_sleep(wait_time)

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "edit_message network error "
                "(attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                e
            )

            if attempt < MAX_RETRIES:
                await safe_sleep(attempt * 2)

        except TelegramError as e:

            logger.error(
                "Telegram edit error: %s",
                e
            )

            break

        except Exception as e:

            logger.exception(
                "Unexpected edit error: %s",
                e
            )

            break

    return None


async def safe_send_dice(
    bot,
    chat_id,
    emoji
):
    """
    ارسال تاس/دارت/بولینگ/بسکتبال
    با تلاش مجدد خودکار.
    """

    for attempt in range(1, MAX_RETRIES + 1):

        try:

            return await bot.send_dice(
                chat_id=chat_id,
                emoji=emoji
            )

        except RetryAfter as e:

            wait_time = float(e.retry_after) + 1

            logger.warning(
                "send_dice retry after %s seconds",
                wait_time
            )

            await safe_sleep(wait_time)

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "send_dice network error "
                "(attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                e
            )

            if attempt < MAX_RETRIES:
                await safe_sleep(attempt * 2)

        except TelegramError as e:

            logger.error(
                "Telegram send_dice error: %s",
                e
            )

            break

        except Exception as e:

            logger.exception(
                "Unexpected send_dice error: %s",
                e
            )

            break

    return None


async def safe_answer_callback(
    query,
    text=None,
    show_alert=False
):
    try:

        if text:
            await query.answer(
                text,
                show_alert=show_alert
            )
        else:
            await query.answer()

    except (TimedOut, NetworkError):

        logger.warning(
            "Callback answer timed out."
        )

    except TelegramError as e:

        logger.warning(
            "Callback answer error: %s",
            e
        )

    except Exception as e:

        logger.warning(
            "Unexpected callback answer error: %s",
            e
        )


# =========================================================
# USER FUNCTIONS
# =========================================================

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


def set_balance(user_id, new_balance):

    new_balance = Decimal(str(new_balance))

    if new_balance < 0:
        raise ValueError("negative balance")

    with closing(get_db()) as db:

        db.execute(
            """
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
            """,
            (
                str(new_balance),
                user_id
            )
        )

        db.commit()


def change_balance(
    user_id,
    amount,
    transaction_type,
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
            (user_id,)
        ).fetchone()

        if not row:
            raise ValueError("user_not_found")

        current = Decimal(row["balance"])
        new_balance = current + amount

        if new_balance < 0:
            raise ValueError("insufficient_balance")

        db.execute(
            """
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
            """,
            (
                str(new_balance),
                user_id
            )
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
                transaction_type,
                str(amount),
                description,
            )
        )

        db.commit()


def is_owner(user_id):
    return user_id in OWNER_IDS


# =========================================================
# BOT ENABLED
# =========================================================

def is_bot_enabled():

    with closing(get_db()) as db:

        row = db.execute(
            """
            SELECT value
            FROM settings
            WHERE key = 'bot_enabled'
            """
        ).fetchone()

    return bool(row and row["value"] == "1")


def set_bot_enabled(value):

    with closing(get_db()) as db:

        db.execute(
            """
            INSERT INTO settings(key, value)
            VALUES ('bot_enabled', ?)
            ON CONFLICT(key)
            DO UPDATE SET value = excluded.value
            """,
            ("1" if value else "0",)
        )

        db.commit()


# =========================================================
# MEMBERSHIP
# =========================================================

async def check_membership(user_id, context):

    for attempt in range(1, MAX_RETRIES + 1):

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

        except RetryAfter as e:

            await safe_sleep(
                float(e.retry_after) + 1
            )

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Membership check failed "
                "(attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                e
            )

            if attempt < MAX_RETRIES:
                await safe_sleep(attempt * 2)

        except TelegramError as e:

            logger.warning(
                "Membership Telegram error: %s",
                e
            )

            break

        except Exception as e:

            logger.exception(
                "Membership unexpected error: %s",
                e
            )

            break

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
            "🔒 برای استفاده از بازی‌ها ابتدا باید عضو کانال BET_Tek شوید.",
            reply_markup=keyboard
        )

    return False


async def membership_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    user = query.from_user

    if await check_membership(user.id, context):

        await safe_edit_message(
            query,
            "✅ عضویت شما تأیید شد.\n"
            "حالا می‌توانید بازی کنید."
        )

    else:

        await safe_answer_callback(
            query,
            "❌ هنوز عضو کانال نیستید.",
            True
        )


# =========================================================
# START
# =========================================================

async def start(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # -------------------------
    # Referral
    # -------------------------

    if context.args:

        arg = context.args[0]

        if arg.startswith("ref_"):

            try:
                referrer_id = int(
                    arg.replace("ref_", "")
                )
            except ValueError:
                referrer_id = None

            if (
                referrer_id
                and referrer_id != user.id
            ):

                with closing(get_db()) as db:

                    old = db.execute(
                        """
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                        """,
                        (user.id,)
                    ).fetchone()

                    if old and old["referrer_id"] is None:

                        exists = db.execute(
                            """
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                            """,
                            (user.id,)
                        ).fetchone()

                        if not exists:

                            db.execute(
                                """
                                UPDATE users
                                SET referrer_id = ?
                                WHERE user_id = ?
                                """,
                                (
                                    referrer_id,
                                    user.id
                                )
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
                                    str(REFERRAL_REWARD)
                                )
                            )

                            db.commit()

                            try:

                                ensure_user_by_id(
                                    referrer_id
                                )

                                change_balance(
                                    referrer_id,
                                    REFERRAL_REWARD,
                                    "referral",
                                    f"Referral: {user.id}"
                                )

                            except Exception as e:

                                logger.warning(
                                    "Referral reward error: %s",
                                    e
                                )

    balance = get_balance(user.id)

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(balance)} TRX\n\n"
        "دستورات:\n"
        "موجودی\n"
        "برداشت\n"
        "زیر مجموعه\n"
        "انتقال 0.1\n\n"
        "بازی در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1"
    )


def ensure_user_by_id(user_id):

    with closing(get_db()) as db:

        db.execute(
            """
            INSERT OR IGNORE INTO users(
                user_id,
                balance
            )
            VALUES (?, '0')
            """,
            (user_id,)
        )

        db.commit()


# =========================================================
# HELP
# =========================================================

async def help_command(update, context):

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "📚 راهنما\n\n"
        "💰 موجودی\n"
        "💸 برداشت\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n\n"
        "🎮 بازی:\n"
        "1 تاس 0.1\n"
        "2 دارت 0.1\n"
        "3 بولینگ 0.1\n"
        "4 بسکتبال 0.1\n\n"
        "برای بازی با دوست، دکمه "
        "«بازی با دوستان» را بزنید."
    )


# =========================================================
# BALANCE
# =========================================================

async def balance(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    bal = get_balance(user.id)

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        f"💰 موجودی شما: {money(bal)} TRX"
    )


# =========================================================
# REFERRAL
# =========================================================

async def referral(update, context):

    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    try:

        bot = await context.bot.get_me()

    except Exception:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ فعلاً ارتباط با تلگرام برقرار نشد. دوباره تلاش کنید."
        )

        return

    link = (
        f"https://t.me/{bot.username}"
        f"?start=ref_{user.id}"
    )

    with closing(get_db()) as db:

        row = db.execute(
            """
            SELECT COUNT(*) AS c
            FROM referrals
            WHERE referrer_id = ?
            """,
            (user.id,)
        ).fetchone()

    count = row["c"] if row else 0

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "👥 زیرمجموعه\n\n"
        f"🔗 لینک دعوت شما:\n{link}\n\n"
        f"👤 تعداد زیرمجموعه: {count}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} TRX"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer_from_text(
    update,
    context,
    amount
):

    user = update.effective_user
    message = update.message

    if not message.reply_to_message:

        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ کاربر مقصد پیدا نشد."
        )

        return

    if target.id == user.id:

        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ نمی‌توانید به خودتان انتقال دهید."
        )

        return

    ensure_user(user)
    ensure_user(target)

    try:

        change_balance(
            user.id,
            -amount,
            "transfer_out",
            f"To {target.id}"
        )

        try:

            change_balance(
                target.id,
                amount,
                "transfer_in",
                f"From {user.id}"
            )

        except Exception:

            change_balance(
                user.id,
                amount,
                "transfer_rollback",
                "Transfer rollback"
            )

            raise

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await safe_send_message(
                context.bot,
                message.chat_id,
                "❌ موجودی شما کافی نیست."
            )

        else:

            await safe_send_message(
                context.bot,
                message.chat_id,
                "❌ انتقال انجام نشد."
            )

        return

    await safe_send_message(
        context.bot,
        message.chat_id,
        "✅ انتقال با موفقیت انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: {target.first_name or target.id}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} TRX"
    )


# =========================================================
# WITHDRAWAL
# =========================================================

WITHDRAW_AMOUNT, WITHDRAW_WALLET, WITHDRAW_CARD = range(3)


async def withdrawal_start(update, context):

    user = update.effective_user

    if not user:
        return ConversationHandler.END

    ensure_user(user)

    balance = get_balance(user.id)

    if balance < MIN_WITHDRAW:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            f"❌ حداقل موجودی برای برداشت "
            f"{money(MIN_WITHDRAW)} TRX است.\n\n"
            f"💰 موجودی شما: {money(balance)} TRX"
        )

        return ConversationHandler.END

    context.user_data["withdraw"] = {}

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "💸 درخواست برداشت\n\n"
        f"حداقل برداشت: {money(MIN_WITHDRAW)} TRX\n\n"
        "مبلغ برداشت را وارد کنید:"
    )

    return WITHDRAW_AMOUNT


async def withdrawal_amount(update, context):

    amount = parse_decimal(
        update.message.text
    )

    if amount is None:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ مبلغ صحیح نیست.\n"
            "مثال: 3.5"
        )

        return WITHDRAW_AMOUNT

    if amount < MIN_WITHDRAW:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            f"❌ حداقل برداشت {money(MIN_WITHDRAW)} TRX است."
        )

        return WITHDRAW_AMOUNT

    balance = get_balance(
        update.effective_user.id
    )

    if amount > balance:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(balance)} TRX"
        )

        return WITHDRAW_AMOUNT

    context.user_data["withdraw"]["amount"] = str(amount)

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "💳 حالا آدرس ولت TRX را وارد کنید:"
    )

    return WITHDRAW_WALLET


async def withdrawal_wallet(update, context):

    wallet = update.message.text.strip()

    if len(wallet) < 5:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ آدرس ولت صحیح نیست."
        )

        return WITHDRAW_WALLET

    context.user_data["withdraw"]["wallet"] = wallet

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "💳 شماره کارت را وارد کنید:"
    )

    return WITHDRAW_CARD


async def withdrawal_card(update, context):

    user = update.effective_user
    card = update.message.text.strip()

    if len(card) < 4:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ شماره کارت صحیح نیست."
        )

        return WITHDRAW_CARD

    data = context.user_data.get(
        "withdraw",
        {}
    )

    if "amount" not in data or "wallet" not in data:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ درخواست برداشت منقضی شده. دوباره «برداشت» را بزنید."
        )

        context.user_data.pop(
            "withdraw",
            None
        )

        return ConversationHandler.END

    amount = Decimal(data["amount"])
    wallet = data["wallet"]

    balance = get_balance(user.id)

    if amount > balance:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ موجودی شما دیگر کافی نیست."
        )

        context.user_data.pop(
            "withdraw",
            None
        )

        return ConversationHandler.END

    try:

        change_balance(
            user.id,
            -amount,
            "withdraw_pending",
            "Withdrawal request"
        )

    except ValueError:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ موجودی کافی نیست."
        )

        context.user_data.pop(
            "withdraw",
            None
        )

        return ConversationHandler.END

    with closing(get_db()) as db:

        cursor = db.execute(
            """
            INSERT INTO withdrawals(
                user_id,
                amount,
                wallet,
                card,
                status
            )
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (
                user.id,
                str(amount),
                wallet,
                card,
            )
        )

        withdrawal_id = cursor.lastrowid

        db.commit()

    username = (
        f"@{user.username}"
        if user.username
        else "بدون یوزرنیم"
    )

    owner_text = (
        "💸 درخواست برداشت جدید\n\n"
        f"🆔 درخواست: #{withdrawal_id}\n"
        f"👤 کاربر: {user.first_name}\n"
        f"🔹 آیدی: {user.id}\n"
        f"🔹 یوزرنیم: {username}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 ولت: {wallet}\n"
        f"💳 شماره کارت: {card}\n\n"
        "⚠️ ممکن است TRX پولی یا TRX خودش واریز شود."
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تأیید برداشت",
                callback_data=(
                    f"withdraw_approve_{withdrawal_id}"
                )
            ),
            InlineKeyboardButton(
                "❌ رد برداشت",
                callback_data=(
                    f"withdraw_reject_{withdrawal_id}"
                )
            )
        ]
    ])

    for owner_id in OWNER_IDS:

        await safe_send_message(
            context.bot,
            owner_id,
            owner_text,
            reply_markup=keyboard
        )

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "✅ درخواست برداشت شما ثبت شد.\n\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"🆔 درخواست: #{withdrawal_id}\n\n"
        "درخواست برای مالکان ارسال شد."
    )

    context.user_data.pop(
        "withdraw",
        None
    )

    return ConversationHandler.END


async def withdrawal_cancel(update, context):

    context.user_data.pop(
        "withdraw",
        None
    )

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "❌ درخواست برداشت لغو شد."
    )

    return ConversationHandler.END


# =========================================================
# WITHDRAW CALLBACK
# =========================================================

async def withdrawal_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    owner = query.from_user

    if not is_owner(owner.id):

        await safe_answer_callback(
            query,
            "❌ فقط مالک می‌تواند این کار را انجام دهد.",
            True
        )

        return

    data = query.data

    try:

        if data.startswith("withdraw_approve_"):

            action = "approve"

            withdrawal_id = int(
                data.replace(
                    "withdraw_approve_",
                    ""
                )
            )

        elif data.startswith("withdraw_reject_"):

            action = "reject"

            withdrawal_id = int(
                data.replace(
                    "withdraw_reject_",
                    ""
                )
            )

        else:
            return

    except ValueError:
        return

    with closing(get_db()) as db:

        row = db.execute(
            """
            SELECT *
            FROM withdrawals
            WHERE id = ?
            """,
            (withdrawal_id,)
        ).fetchone()

        if not row:

            await safe_answer_callback(
                query,
                "درخواست پیدا نشد.",
                True
            )

            return

        if row["status"] != "pending":

            await safe_answer_callback(
                query,
                "این درخواست قبلاً بررسی شده.",
                True
            )

            return

        user_id = row["user_id"]
        amount = Decimal(row["amount"])

        if action == "approve":

            db.execute(
                """
                UPDATE withdrawals
                SET status = 'approved'
                WHERE id = ?
                """,
                (withdrawal_id,)
            )

            db.commit()

        else:

            db.execute(
                """
                UPDATE withdrawals
                SET status = 'rejected'
                WHERE id = ?
                """,
                (withdrawal_id,)
            )

            db.commit()

            try:

                change_balance(
                    user_id,
                    amount,
                    "withdraw_rejected",
                    f"Withdrawal #{withdrawal_id} rejected"
                )

            except Exception as e:

                logger.exception(
                    "Refund withdrawal failed: %s",
                    e
                )

    if action == "approve":

        await safe_edit_message(
            query,
            query.message.text
            + "\n\n"
            f"✅ تأیید شد توسط مالک {owner.id}"
        )

        await safe_send_message(
            context.bot,
            user_id,
            "✅ درخواست برداشت شما تأیید شد.\n\n"
            f"💰 مبلغ: {money(amount)} TRX\n"
            "وضعیت: تأیید شده"
        )

    else:

        await safe_edit_message(
            query,
            query.message.text
            + "\n\n"
            f"❌ رد شد توسط مالک {owner.id}"
        )

        await safe_send_message(
            context.bot,
            user_id,
            "❌ درخواست برداشت شما رد شد.\n\n"
            f"💰 مبلغ {money(amount)} TRX "
            "به موجودی شما برگشت داده شد."
        )


# =========================================================
# ADMIN BALANCE
# =========================================================

async def admin_balance_change(
    update,
    context,
    amount,
    operation
):

    user = update.effective_user

    if not is_owner(user.id):
        return

    if not update.message.reply_to_message:

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ باید روی پیام کاربر Reply کنید."
        )

        return

    target = update.message.reply_to_message.from_user

    if not target:
        return

    ensure_user(target)

    if operation == "charge":

        change_balance(
            target.id,
            amount,
            "admin_charge",
            f"Charged by {user.id}"
        )

        text = "شارژ"

    else:

        try:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Removed by {user.id}"
            )

        except ValueError:

            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                "❌ موجودی کاربر کافی نیست."
            )

            return

        text = "کسر"

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        f"✅ {text} انجام شد.\n\n"
        f"👤 کاربر: {target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
    )


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(update, context):

    user = update.effective_user

    if not is_owner(user.id):

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "❌ دسترسی ندارید."
        )

        return

    enabled = is_bot_enabled()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "🟢 روشن کردن",
                callback_data="admin_on"
            ),
            InlineKeyboardButton(
                "🔴 خاموش کردن",
                callback_data="admin_off"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ]
    ])

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        "👑 پنل مدیریت\n\n"
        f"وضعیت ربات: "
        f"{'🟢 روشن' if enabled else '🔴 خاموش'}\n\n"
        "برای شارژ/کسر در گپ:\n"
        "روی پیام کاربر Reply کنید:\n\n"
        "شارژ 100\n"
        "کسر 100",
        reply_markup=keyboard
    )


async def admin_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    user = query.from_user

    if not is_owner(user.id):

        await safe_answer_callback(
            query,
            "❌ دسترسی ندارید.",
            True
        )

        return

    if query.data == "admin_on":

        set_bot_enabled(True)

        await safe_edit_message(
            query,
            "👑 پنل مدیریت\n\n"
            "🟢 ربات روشن شد."
        )

    elif query.data == "admin_off":

        set_bot_enabled(False)

        await safe_edit_message(
            query,
            "👑 پنل مدیریت\n\n"
            "🔴 ربات خاموش شد."
        )

    elif query.data == "admin_stats":

        with closing(get_db()) as db:

            users = db.execute(
                "SELECT COUNT(*) AS c FROM users"
            ).fetchone()["c"]

            total = db.execute(
                """
                SELECT COALESCE(
                    SUM(CAST(balance AS REAL)), 0
                ) AS s
                FROM users
                """
            ).fetchone()["s"]

            withdrawals = db.execute(
                """
                SELECT COUNT(*) AS c
                FROM withdrawals
                WHERE status = 'pending'
                """
            ).fetchone()["c"]

        await safe_edit_message(
            query,
            "📊 آمار ربات\n\n"
            f"👤 کاربران: {users}\n"
            f"💰 مجموع موجودی: {total:.2f} TRX\n"
            f"💸 برداشت‌های در انتظار: {withdrawals}"
        )


# =========================================================
# GAME
# =========================================================

async def send_game_roll(
    context,
    chat_id,
    game_type
):

    emoji = GAME_EMOJIS[game_type]

    message = await safe_send_dice(
        context.bot,
        chat_id,
        emoji
    )

    if message is None:
        raise RuntimeError(
            "Telegram dice request failed"
        )

    if not message.dice:
        raise RuntimeError(
            "Dice result missing"
        )

    return message.dice.value


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

    if bet < MIN_GAME_BET:

        await safe_send_message(
            context.bot,
            chat.id,
            f"❌ حداقل شرط {money(MIN_GAME_BET)} TRX است."
        )

        return

    balance = get_balance(user.id)

    if balance < bet:

        await safe_send_message(
            context.bot,
            chat.id,
            "❌ موجودی شما کافی نیست.\n\n"
            f"💰 موجودی: {money(balance)} TRX"
        )

        return

    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"{game_type} game #{count}"
        )

    except ValueError:

        await safe_send_message(
            context.bot,
            chat.id,
            "❌ موجودی کافی نیست."
        )

        return

    with closing(get_db()) as db:

        cursor = db.execute(
            """
            INSERT INTO games(
                chat_id,
                creator_id,
                game_type,
                count,
                bet,
                mode,
                status,
                current_player
            )
            VALUES (?, ?, ?, ?, ?, 'friends', 'waiting', ?)
            """,
            (
                chat.id,
                user.id,
                game_type,
                count,
                str(bet),
                user.id,
            )
        )

        game_id = cursor.lastrowid

        db.commit()

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "👥 بازی با دوستان",
                callback_data=f"join_game_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_game_{game_id}"
            )
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_game_{game_id}"
            )
        ]
    ])

    await safe_send_message(
        context.bot,
        chat.id,
        "🎮 بازی جدید\n\n"
        f"{game_farsi_name(game_type)}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n\n"
        f"👤 سازنده: {user.first_name}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=keyboard
    )


async def join_game_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    user = query.from_user

    if not await check_membership(
        user.id,
        context
    ):

        await safe_answer_callback(
            query,
            "❌ ابتدا عضو BET_Tek شوید.",
            True
        )

        return

    try:

        game_id = int(
            query.data.replace(
                "join_game_",
                ""
            )
        )

    except ValueError:
        return

    ensure_user(user)

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,)
        ).fetchone()

        if not game:

            await safe_answer_callback(
                query,
                "بازی پیدا نشد.",
                True
            )

            return

        if game["status"] != "waiting":

            await safe_answer_callback(
                query,
                "این بازی دیگر در انتظار بازیکن نیست.",
                True
            )

            return

        if game["creator_id"] == user.id:

            await safe_answer_callback(
                query,
                "❌ نمی‌توانید با خودتان بازی کنید.",
                True
            )

            return

        bet = Decimal(game["bet"])

        if get_balance(user.id) < bet:

            await safe_answer_callback(
                query,
                "❌ موجودی شما کافی نیست.",
                True
            )

            return

        try:

            change_balance(
                user.id,
                -bet,
                "game_bet",
                f"Joined game #{game_id}"
            )

        except ValueError:

            await safe_answer_callback(
                query,
                "❌ موجودی کافی نیست.",
                True
            )

            return

        db.execute(
            """
            UPDATE games
            SET opponent_id = ?,
                status = 'playing',
                current_player = creator_id
            WHERE id = ?
            """,
            (
                user.id,
                game_id
            )
        )

        db.commit()

    await safe_edit_message(
        query,
        "🎮 بازی شروع شد!\n\n"
        f"{game_farsi_name(game['game_type'])}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر بازیکن: {money(bet)} TRX\n\n"
        f"👤 سازنده: {game['creator_id']}\n"
        f"👤 حریف: {user.first_name}\n\n"
        "🎯 ابتدا سازنده تمام پرتاب‌های خود را انجام می‌دهد."
    )

    await run_player_turn(
        context,
        game_id,
        game["creator_id"]
    )


async def bot_game_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    user = query.from_user

    if not await check_membership(
        user.id,
        context
    ):

        await safe_answer_callback(
            query,
            "❌ ابتدا عضو BET_Tek شوید.",
            True
        )

        return

    try:

        game_id = int(
            query.data.replace(
                "bot_game_",
                ""
            )
        )

    except ValueError:
        return

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            return

        if game["status"] != "waiting":

            await safe_answer_callback(
                query,
                "این بازی فعال نیست.",
                True
            )

            return

        if game["creator_id"] != user.id:

            await safe_answer_callback(
                query,
                "فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
                True
            )

            return

        db.execute(
            """
            UPDATE games
            SET mode = 'bot',
                opponent_id = -1,
                status = 'playing',
                current_player = creator_id
            WHERE id = ?
            """,
            (game_id,)
        )

        db.commit()

    await safe_edit_message(
        query,
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{game_farsi_name(game['game_type'])}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
        "🎯 ابتدا تمام پرتاب‌های شما انجام می‌شود."
    )

    await run_player_turn(
        context,
        game_id,
        user.id
    )


async def cancel_game_callback(update, context):

    query = update.callback_query

    await safe_answer_callback(query)

    user = query.from_user

    try:

        game_id = int(
            query.data.replace(
                "cancel_game_",
                ""
            )
        )

    except ValueError:
        return

    with closing(get_db()) as db:

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,)
        ).fetchone()

        if not game:
            return

        if game["status"] != "waiting":

            await safe_answer_callback(
                query,
                "این بازی دیگر قابل لغو نیست.",
                True
            )

            return

        if (
            game["creator_id"] != user.id
            and not is_owner(user.id)
        ):

            await safe_answer_callback(
                query,
                "❌ فقط سازنده یا مالک می‌تواند لغو کند.",
                True
            )

            return

        bet = Decimal(game["bet"])

        db.execute(
            """
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
            """,
            (game_id,)
        )

        db.commit()

    try:

        change_balance(
            game["creator_id"],
            bet,
            "game_refund",
            f"Cancelled game #{game_id}"
        )

    except Exception as e:

        logger.exception(
            "Game refund error: %s",
            e
        )

    await safe_edit_message(
        query,
        "❌ بازی لغو شد.\n"
        f"💰 {money(bet)} TRX به سازنده برگشت داده شد."
    )


async def run_player_turn(
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
            (game_id,)
        ).fetchone()

    if not game:
        return

    if game["status"] != "playing":
        return

    chat_id = game["chat_id"]
    game_type = game["game_type"]
    count = game["count"]

    total = 0

    for i in range(count):

        try:

            value = await send_game_roll(
                context,
                chat_id,
                game_type
            )

            total += value

            # کمی فاصله بین پرتاب‌ها
            if i < count - 1:
                await safe_sleep(1)

        except Exception as e:

            logger.exception(
                "Game roll error: %s",
                e
            )

            # اگر یک پرتاب شکست خورد،
            # کل بات را متوقف نکن.
            continue

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
                    game_id
                )
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
                    game_id
                )
            )

        db.commit()

        game = db.execute(
            """
            SELECT *
            FROM games
            WHERE id = ?
            """,
            (game_id,)
        ).fetchone()

    if not game:
        return

    if player_id == game["creator_id"]:

        if game["mode"] == "bot":

            await safe_send_message(
                context.bot,
                chat_id,
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
            )

            bot_total = 0

            for i in range(count):

                try:

                    value = await send_game_roll(
                        context,
                        chat_id,
                        game_type
                    )

                    bot_total += value

                    if i < count - 1:
                        await safe_sleep(1)

                except Exception as e:

                    logger.exception(
                        "Bot roll error: %s",
                        e
                    )

            with closing(get_db()) as db:

                db.execute(
                    """
                    UPDATE games
                    SET opponent_total = ?
                    WHERE id = ?
                    """,
                    (
                        bot_total,
                        game_id
                    )
                )

                db.commit()

            await finish_game(
                context,
                game_id
            )

        else:

            await safe_send_message(
                context.bot,
                chat_id,
                "✅ پرتاب‌های سازنده تمام شد.\n\n"
                "🎯 حالا بازیکن دوم تمام پرتاب‌های خودش را انجام می‌دهد."
            )

            await run_player_turn(
                context,
                game_id,
                game["opponent_id"]
            )

    else:

        await finish_game(
            context,
            game_id
        )


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
            (game_id,)
        ).fetchone()

        if not game:
            return

        if game["status"] != "playing":
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

        if creator_total > opponent_total:

            winner_id = game["creator_id"]

        elif opponent_total > creator_total:

            winner_id = game["opponent_id"]

        else:

            winner_id = None

        db.execute(
            """
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
            """,
            (game_id,)
        )

        db.commit()

    # -------------------------
    # Draw
    # -------------------------

    if winner_id is None:

        try:

            change_balance(
                game["creator_id"],
                bet,
                "game_draw_refund",
                f"Draw game #{game_id}"
            )

            if game["mode"] != "bot":

                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_draw_refund",
                    f"Draw game #{game_id}"
                )

        except Exception as e:

            logger.exception(
                "Draw refund error: %s",
                e
            )

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "🤝 بازی مساوی شد.\n\n"
            f"🎲 نتیجه: "
            f"{creator_total:g} - "
            f"{opponent_total:g}\n"
            "💰 مبلغ هر بازیکن برگشت داده شد."
        )

        return

    # -------------------------
    # Bot game
    # -------------------------

    if game["mode"] == "bot":

        if winner_id == game["creator_id"]:

            payout = bet * 2

            try:

                change_balance(
                    winner_id,
                    payout,
                    "game_win",
                    f"Won bot game #{game_id}"
                )

                result = (
                    "🏆 شما برنده شدید!\n"
                    f"💰 دریافتی: {money(payout)} TRX"
                )

            except Exception as e:

                logger.exception(
                    "Bot game payout error: %s",
                    e
                )

                result = (
                    "🏆 شما برنده شدید!"
                )

        else:

            result = (
                "🤖 ربات برنده شد.\n"
                f"💰 شرط شما: {money(bet)} TRX"
            )

    # -------------------------
    # Friend game
    # -------------------------

    else:

        payout = bet * 2

        try:

            change_balance(
                winner_id,
                payout,
                "game_win",
                f"Won game #{game_id}"
            )

            result = (
                f"🏆 برنده: {winner_id}\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        except Exception as e:

            logger.exception(
                "Friend game payout error: %s",
                e
            )

            result = (
                f"🏆 برنده: {winner_id}"
            )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🏁 نتیجه بازی\n\n"
        f"{game_farsi_name(game['game_type'])}\n\n"
        f"👤 سازنده: {creator_total:g}\n"
        f"👤 بازیکن دوم: {opponent_total:g}\n\n"
        f"{result}"
    )


# =========================================================
# ADMIN TEXT
# =========================================================

async def owner_text_command(
    update,
    context,
    text
):

    user = update.effective_user

    if not is_owner(user.id):
        return False

    normalized = normalize_digits(
        text
    ).strip()

    if normalized == "روشن":

        set_bot_enabled(True)

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🟢 ربات روشن شد."
        )

        return True

    if normalized == "خاموش":

        set_bot_enabled(False)

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔴 ربات خاموش شد."
        )

        return True

    if normalized.startswith("شارژ "):

        amount = parse_decimal(
            normalized[6:].strip()
        )

        if amount is None:

            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                "❌ مثال صحیح:\n"
                "شارژ 100"
            )

            return True

        await admin_balance_change(
            update,
            context,
            amount,
            "charge"
        )

        return True

    if normalized.startswith("کسر "):

        amount = parse_decimal(
            normalized[4:].strip()
        )

        if amount is None:

            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                "❌ مثال صحیح:\n"
                "کسر 100"
            )

            return True

        await admin_balance_change(
            update,
            context,
            amount,
            "remove"
        )

        return True

    return False


# =========================================================
# GENERAL TEXT
# =========================================================

async def text_handler(
    update,
    context
):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    text = normalize_digits(
        message.text.strip()
    )

    # -------------------------
    # Owner commands
    # -------------------------

    if is_owner(user.id):

        handled = await owner_text_command(
            update,
            context,
            text
        )

        if handled:
            return

    # -------------------------
    # Balance
    # -------------------------

    if text in (
        "موجودی",
        "موجودی من",
        "balance"
    ):

        await balance(
            update,
            context
        )

        return

    # -------------------------
    # Referral
    # -------------------------

    if text in (
        "زیر مجموعه",
        "زیرمجموعه",
        "رفرال",
        "referral"
    ):

        await referral(
            update,
            context
        )

        return

    # -------------------------
    # Withdraw
    # -------------------------

    if text in (
        "برداشت",
        "withdraw"
    ):

        await withdrawal_start(
            update,
            context
        )

        return

    # -------------------------
    # Transfer
    # -------------------------

    if text.startswith("انتقال "):

        amount = parse_decimal(
            text[7:].strip()
        )

        if amount is None:

            await safe_send_message(
                context.bot,
                message.chat_id,
                "❌ مثال:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱"
            )

            return

        await transfer_from_text(
            update,
            context,
            amount
        )

        return

    # -------------------------
    # Games
    # -------------------------

    game = parse_game_command(
        text
    )

    if game:

        count, game_type, bet = game

        if not is_bot_enabled():

            await safe_send_message(
                context.bot,
                message.chat_id,
                "🔴 ربات در حال حاضر خاموش است."
            )

            return

        if message.chat.type in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await create_game(
                update,
                context,
                count,
                game_type,
                bet
            )

            return

        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ بازی‌ها باید داخل گپ انجام شوند."
        )

        return


# =========================================================
# COMMAND ALIASES
# =========================================================

async def admin_command(
    update,
    context
):

    await admin_panel(
        update,
        context
    )


# =========================================================
# GLOBAL ERROR HANDLER
# =========================================================

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
            "Temporary Telegram/network error: %s",
            error
        )

        # بات را متوقف نکن
        return

    if isinstance(
        error,
        RetryAfter
    ):

        logger.warning(
            "Telegram flood control: retry after %s",
            error.retry_after
        )

        return

    if isinstance(
        error,
        TelegramError
    ):

        logger.error(
            "Telegram error: %s",
            error
        )

        return

    logger.exception(
        "Unhandled exception:",
        exc_info=error
    )


# =========================================================
# POST INIT
# =========================================================

async def post_init(
    application: Application
):

    logger.info(
        "BET_BT post_init started."
    )

    # فقط برای اطمینان از اتصال
    for attempt in range(
        1,
        MAX_RETRIES + 1
    ):

        try:

            me = await application.bot.get_me()

            logger.info(
                "Connected to Telegram as @%s",
                me.username
            )

            return

        except (TimedOut, NetworkError) as e:

            logger.warning(
                "Initial Telegram connection failed "
                "(attempt %s/%s): %s",
                attempt,
                MAX_RETRIES,
                e
            )

            if attempt < MAX_RETRIES:
                await safe_sleep(attempt * 3)

        except Exception as e:

            logger.exception(
                "Initial Telegram connection error: %s",
                e
            )

            return

    logger.warning(
        "Could not confirm Telegram connection at startup."
    )


# =========================================================
# MAIN
# =========================================================

def main():

    if not BOT_TOKEN:

        raise RuntimeError(
            "BOT_TOKEN پیدا نشد.\n"
            "در GitHub Secrets مقدار BOT_TOKEN را قرار بده."
        )

    init_db()

    # -------------------------
    # Telegram request settings
    # -------------------------

    request = HTTPXRequest(
        connection_pool_size=20,

        connect_timeout=30.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=30.0,

        media_write_timeout=60.0,
    )

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .request(request)
        .post_init(post_init)
        .build()
    )

    # =====================================================
    # WITHDRAWAL CONVERSATION
    # =====================================================

    withdrawal_handler = ConversationHandler(

        entry_points=[
            MessageHandler(
                filters.TEXT
                & filters.Regex(
                    r"^(برداشت|withdraw)$"
                ),
                withdrawal_start
            )
        ],

        states={

            WITHDRAW_AMOUNT: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    withdrawal_amount
                )
            ],

            WITHDRAW_WALLET: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    withdrawal_wallet
                )
            ],

            WITHDRAW_CARD: [
                MessageHandler(
                    filters.TEXT
                    & ~filters.COMMAND,
                    withdrawal_card
                )
            ],
        },

        fallbacks=[
            CommandHandler(
                "cancel",
                withdrawal_cancel
            )
        ],

        allow_reentry=True
    )

    application.add_handler(
        withdrawal_handler
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
            admin_command
        )
    )

    # =====================================================
    # CALLBACKS
    # =====================================================

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            withdrawal_callback,
            pattern=r"^withdraw_(approve|reject)_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            join_game_callback,
            pattern=r"^join_game_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game_callback,
            pattern=r"^bot_game_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game_callback,
            pattern=r"^cancel_game_\d+$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|stats)$"
        )
    )

    # =====================================================
    # TEXT
    # =====================================================

    application.add_handler(
        MessageHandler(
            filters.TEXT
            & ~filters.COMMAND,
            text_handler
        )
    )

    # =====================================================
    # ERROR HANDLER
    # =====================================================

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started successfully."
    )

    # polling خودش در خطاهای موقتی تلاش مجدد می‌کند
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


# =========================================================
# START
# =========================================================

if __name__ == "__main__":
    main()
