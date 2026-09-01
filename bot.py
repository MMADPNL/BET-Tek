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

BOT_TOKEN = os.getenv("BOT_TOKEN", "")

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
    format="%(asctime)s - %(levelname)s - %(message)s",
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
            CREATE TABLE IF NOT EXISTS games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER,
                game_type TEXT NOT NULL,
                count INTEGER NOT NULL,
                bet TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'friends',
                status TEXT NOT NULL DEFAULT 'waiting',
                creator_total REAL DEFAULT 0,
                opponent_total REAL DEFAULT 0,
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


def parse_decimal(value):
    value = normalize_digits(str(value)).strip()

    value = value.replace("٫", ".")
    value = value.replace(",", ".")

    try:
        number = Decimal(value)
    except (InvalidOperation, ValueError):
        return None

    if number <= 0:
        return None

    return number


def money(value):
    value = Decimal(str(value))

    if value == value.to_integral():
        return str(int(value))

    return f"{value:.2f}"


def is_owner(user_id):
    return user_id in OWNER_IDS


# ============================================================
# SAFE TELEGRAM SEND
# ============================================================

async def safe_send_message(
    bot,
    chat_id,
    text,
    reply_markup=None,
    reply_to_message_id=None,
):
    for attempt in range(3):
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                reply_markup=reply_markup,
                reply_to_message_id=reply_to_message_id,
            )

        except (TimedOut, NetworkError) as e:
            logger.warning(
                "send_message timeout/network attempt %s: %s",
                attempt + 1,
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
            else:
                return None

        except TelegramError as e:
            logger.error("Telegram error: %s", e)
            return None

        except Exception as e:
            logger.exception("Unexpected send error: %s", e)
            return None

    return None


async def safe_edit_message(
    query,
    text,
    reply_markup=None,
):
    for attempt in range(3):
        try:
            return await query.edit_message_text(
                text=text,
                reply_markup=reply_markup,
            )

        except (TimedOut, NetworkError) as e:
            logger.warning(
                "edit timeout attempt %s: %s",
                attempt + 1,
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

        except TelegramError as e:
            logger.error("Edit Telegram error: %s", e)
            return None

        except Exception as e:
            logger.exception("Unexpected edit error: %s", e)
            return None

    return None


# ============================================================
# USER
# ============================================================

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
    description="",
):
    amount = Decimal(str(amount))

    with closing(get_db()) as db:
        db.execute("BEGIN IMMEDIATE")

        row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user_id,)).fetchone()

        if not row:
            db.rollback()
            raise ValueError("user_not_found")

        current = Decimal(row["balance"])
        new_balance = current + amount

        if new_balance < 0:
            db.rollback()
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


# ============================================================
# BOT STATUS
# ============================================================

def is_bot_enabled():
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
        """, ("1" if enabled else "0",))

        db.commit()


# ============================================================
# MEMBERSHIP
# ============================================================

async def check_membership(user_id, context):
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

    except Exception as e:
        logger.warning(
            "Membership check failed: %s",
            e,
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
            "🔒 برای استفاده از بازی‌ها ابتدا عضو کانال BET_Tek شوید.",
            reply_markup=keyboard,
        )

    return False


# ============================================================
# REFERRAL
# ============================================================

async def process_referral(user, referrer_id):
    if not referrer_id:
        return

    if referrer_id == user.id:
        return

    ensure_user(user)

    with closing(get_db()) as db:
        current = db.execute("""
            SELECT referrer_id
            FROM users
            WHERE user_id = ?
        """, (user.id,)).fetchone()

        if not current:
            return

        if current["referrer_id"] is not None:
            return

        referrer = db.execute("""
            SELECT user_id
            FROM users
            WHERE user_id = ?
        """, (referrer_id,)).fetchone()

        if not referrer:
            return

        exists = db.execute("""
            SELECT id
            FROM referrals
            WHERE referred_id = ?
        """, (user.id,)).fetchone()

        if exists:
            return

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
    except Exception as e:
        logger.error(
            "Referral reward error: %s",
            e,
        )


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    if context.args:
        arg = normalize_digits(context.args[0])

        if arg.startswith("ref_"):
            try:
                referrer_id = int(
                    arg.replace("ref_", "")
                )
                await process_referral(
                    user,
                    referrer_id,
                )
            except Exception as e:
                logger.error(
                    "Referral error: %s",
                    e,
                )

    balance = get_balance(user.id)

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="user_balance",
            ),
            InlineKeyboardButton(
                "👥 زیر مجموعه",
                callback_data="user_referral",
            ),
        ],
        [
            InlineKeyboardButton(
                "📚 راهنما",
                callback_data="user_help",
            )
        ],
    ])

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        (
            "🤖 BET_BT\n\n"
            f"💰 موجودی شما: {money(balance)} TRX\n\n"
            "دستورات:\n"
            "موجودی\n"
            "زیر مجموعه\n"
            "انتقال 0.1\n\n"
            "🎮 بازی در گپ:\n"
            "1 تاس 0.1\n"
            "1 دارت 0.1\n"
            "1 بولینگ 0.1\n"
            "1 بسکتبال 0.1"
        ),
        reply_markup=keyboard,
    )


# ============================================================
# BALANCE
# ============================================================

async def show_balance(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    balance = get_balance(user.id)

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message(
            update.callback_query,
            f"💰 موجودی شما: {money(balance)} TRX",
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 برگشت",
                        callback_data="back_start",
                    )
                ]
            ]),
        )

    elif update.message:
        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            f"💰 موجودی شما: {money(balance)} TRX",
        )


# ============================================================
# REFERRAL PAGE
# ============================================================

async def show_referral(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    try:
        bot = await context.bot.get_me()
    except Exception:
        return

    link = (
        f"https://t.me/{bot.username}"
        f"?start=ref_{user.id}"
    )

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id = ?
        """, (user.id,)).fetchone()

    count = row["total"] if row else 0

    text = (
        "👥 زیر مجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد زیرمجموعه: {count}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} TRX"
    )

    if update.callback_query:
        await update.callback_query.answer()
        await safe_edit_message(
            update.callback_query,
            text,
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 برگشت",
                        callback_data="back_start",
                    )
                ]
            ]),
        )

    else:
        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            text,
        )


# ============================================================
# HELP
# ============================================================

async def show_help(update, context):
    text = (
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "موجودی\n\n"
        "👥 زیر مجموعه\n"
        "زیر مجموعه\n\n"
        "🔄 انتقال در گپ\n"
        "روی پیام کاربر Reply کنید:\n"
        "انتقال 0.1\n"
        "انتقال ۰.۱\n\n"
        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "2 دارت 0.1\n"
        "3 بولینگ 0.1\n"
        "4 بسکتبال 0.1\n\n"
        "سازنده ابتدا تمام پرتاب‌های تعداد تعیین‌شده را انجام می‌دهد."
    )

    if update.callback_query:
        await update.callback_query.answer()

        await safe_edit_message(
            update.callback_query,
            text,
            InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "🔙 برگشت",
                        callback_data="back_start",
                    )
                ]
            ]),
        )

    else:
        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            text,
        )


# ============================================================
# TRANSFER
# ============================================================

async def transfer(update, context, amount):
    user = update.effective_user
    message = update.message

    if not message.reply_to_message:
        await safe_send_message(
            context.bot,
            message.chat_id,
            (
                "❌ باید روی پیام کاربر Reply کنید.\n\n"
                "مثال:\n"
                "انتقال 0.1"
            ),
            reply_to_message_id=message.message_id,
        )
        return

    target = message.reply_to_message.from_user

    if not target:
        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ کاربر مقصد پیدا نشد.",
            reply_to_message_id=message.message_id,
        )
        return

    if target.id == user.id:
        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ نمی‌توانید به خودتان انتقال دهید.",
            reply_to_message_id=message.message_id,
        )
        return

    ensure_user(user)
    ensure_user(target)

    with closing(get_db()) as db:
        db.execute("BEGIN IMMEDIATE")

        sender = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user.id,)).fetchone()

        receiver = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (target.id,)).fetchone()

        if not sender or not receiver:
            db.rollback()
            return

        sender_balance = Decimal(sender["balance"])

        if sender_balance < amount:
            db.rollback()

            await safe_send_message(
                context.bot,
                message.chat_id,
                (
                    "❌ موجودی شما کافی نیست.\n\n"
                    f"💰 موجودی: {money(sender_balance)} TRX"
                ),
                reply_to_message_id=message.message_id,
            )
            return

        receiver_balance = Decimal(receiver["balance"])

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

    await safe_send_message(
        context.bot,
        message.chat_id,
        (
            "✅ انتقال انجام شد.\n\n"
            f"💸 مبلغ: {money(amount)} TRX\n"
            f"👤 گیرنده: {target.first_name or target.id}\n"
            f"💰 موجودی شما: {money(get_balance(user.id))} TRX"
        ),
        reply_to_message_id=message.message_id,
    )


# ============================================================
# GAME PARSER
# ============================================================

def parse_game_command(text):
    text = normalize_digits(text).strip()

    match = re.match(
        r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)$",
        text,
    )

    if not match:
        return None

    count = int(match.group(1))
    name = match.group(2).lower()
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

    if name not in games:
        return None

    return count, games[name], bet


def game_name(game_type):
    return {
        "dice": "🎲 تاس",
        "darts": "🎯 دارت",
        "bowling": "🎳 بولینگ",
        "basketball": "🏀 بسکتبال",
    }.get(game_type, game_type)


GAME_EMOJI = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


# ============================================================
# SEND DICE WITH RETRY
# ============================================================

async def safe_send_dice(bot, chat_id, emoji):
    for attempt in range(3):
        try:
            return await bot.send_dice(
                chat_id=chat_id,
                emoji=emoji,
            )

        except (TimedOut, NetworkError) as e:
            logger.warning(
                "Dice timeout attempt %s: %s",
                attempt + 1,
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))

        except TelegramError as e:
            logger.error(
                "Dice telegram error: %s",
                e,
            )
            return None

        except Exception as e:
            logger.exception(
                "Dice unexpected error: %s",
                e,
            )
            return None

    return None


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
    message = update.message

    if not user or not message:
        return

    if not is_bot_enabled():
        await safe_send_message(
            context.bot,
            message.chat_id,
            "🔴 ربات در حال حاضر خاموش است.",
        )
        return

    if not await require_membership(update, context):
        return

    ensure_user(user)

    if bet < MIN_GAME_BET:
        await safe_send_message(
            context.bot,
            message.chat_id,
            f"❌ حداقل شرط {money(MIN_GAME_BET)} TRX است.",
        )
        return

    balance = get_balance(user.id)

    if balance < bet:
        await safe_send_message(
            context.bot,
            message.chat_id,
            (
                "❌ موجودی کافی نیست.\n\n"
                f"💰 موجودی: {money(balance)} TRX"
            ),
        )
        return

    try:
        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type} #{count}",
        )
    except ValueError:
        await safe_send_message(
            context.bot,
            message.chat_id,
            "❌ موجودی کافی نیست.",
        )
        return

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
            message.chat_id,
            user.id,
            game_type,
            count,
            str(bet),
        ))

        game_id = cursor.lastrowid
        db.commit()

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
                "❌ لغو",
                callback_data=f"cancel_{game_id}",
            )
        ],
    ])

    sent = await safe_send_message(
        context.bot,
        message.chat_id,
        (
            "🎮 بازی جدید\n\n"
            f"{game_name(game_type)}\n"
            f"🔢 تعداد بازی: {count}\n"
            f"💰 شرط: {money(bet)} TRX\n\n"
            f"👤 سازنده: {user.first_name or user.id}\n\n"
            "یک گزینه را انتخاب کنید:"
        ),
        reply_markup=keyboard,
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
# JOIN FRIEND GAME
# ============================================================

async def join_game(update, context):
    query = update.callback_query
    user = query.from_user

    await query.answer()

    if not await check_membership(user.id, context):
        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True,
        )
        return

    try:
        game_id = int(
            query.data.replace("join_", "")
        )
    except ValueError:
        return

    ensure_user(user)

    with closing(get_db()) as db:
        db.execute("BEGIN IMMEDIATE")

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

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
                "❌ این بازی قبلاً شروع شده.",
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

        bet = Decimal(game["bet"])

        row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (user.id,)).fetchone()

        if not row:
            db.rollback()
            return

        balance = Decimal(row["balance"])

        if balance < bet:
            db.rollback()
            await query.answer(
                "❌ موجودی شما کافی نیست.",
                show_alert=True,
            )
            return

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
            VALUES (?, 'game_bet', ?, ?)
        """, (
            user.id,
            str(-bet),
            f"Joined game #{game_id}",
        ))

        db.execute("""
            UPDATE games
            SET opponent_id = ?,
                status = 'playing'
            WHERE id = ?
        """, (
            user.id,
            game_id,
        ))

        db.commit()

    await safe_edit_message(
        query,
        (
            "🎮 بازی شروع شد!\n\n"
            f"{game_name(game['game_type'])}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط هر نفر: {money(bet)} TRX\n\n"
            "🎯 ابتدا سازنده تمام پرتاب‌های خود را انجام می‌دهد."
        ),
    )

    await play_turn(
        context,
        game_id,
        game["creator_id"],
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(update, context):
    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.replace("bot_", "")
        )
    except ValueError:
        return

    with closing(get_db()) as db:
        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

        if not game:
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ بازی فعال نیست.",
                show_alert=True,
            )
            return

        if game["creator_id"] != user.id:
            await query.answer(
                "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                show_alert=True,
            )
            return

        db.execute("""
            UPDATE games
            SET mode = 'bot',
                opponent_id = -1,
                status = 'playing'
            WHERE id = ?
        """, (game_id,))

        db.commit()

    await safe_edit_message(
        query,
        (
            "🤖 بازی با ربات شروع شد!\n\n"
            f"{game_name(game['game_type'])}\n"
            f"🔢 تعداد: {game['count']}\n"
            f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
            "🎯 ابتدا تمام پرتاب‌های شما انجام می‌شود."
        ),
    )

    await play_turn(
        context,
        game_id,
        user.id,
    )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):
    query = update.callback_query
    user = query.from_user

    await query.answer()

    try:
        game_id = int(
            query.data.replace("cancel_", "")
        )
    except ValueError:
        return

    with closing(get_db()) as db:
        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

        if not game:
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ بازی قابل لغو نیست.",
                show_alert=True,
            )
            return

        if (
            game["creator_id"] != user.id
            and not is_owner(user.id)
        ):
            await query.answer(
                "❌ فقط سازنده یا مالک.",
                show_alert=True,
            )
            return

        db.execute("""
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
        """, (game_id,))

        db.commit()

    bet = Decimal(game["bet"])

    try:
        change_balance(
            game["creator_id"],
            bet,
            "game_refund",
            f"Cancelled game #{game_id}",
        )
    except Exception as e:
        logger.error(
            "Refund error: %s",
            e,
        )

    await safe_edit_message(
        query,
        (
            "❌ بازی لغو شد.\n\n"
            f"💰 {money(bet)} TRX به موجودی سازنده برگشت داده شد."
        ),
    )


# ============================================================
# PLAY TURN
# ============================================================

async def play_turn(
    context,
    game_id,
    player_id,
):
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

    total = 0
    emoji = GAME_EMOJI[game["game_type"]]

    for _ in range(game["count"]):
        result = await safe_send_dice(
            context.bot,
            game["chat_id"],
            emoji,
        )

        if result is not None:
            try:
                total += int(result.dice.value)
            except Exception:
                pass

        await asyncio.sleep(1)

    with closing(get_db()) as db:
        if player_id == game["creator_id"]:
            db.execute("""
                UPDATE games
                SET creator_total = ?
                WHERE id = ?
            """, (
                total,
                game_id,
            ))
        else:
            db.execute("""
                UPDATE games
                SET opponent_total = ?
                WHERE id = ?
            """, (
                total,
                game_id,
            ))

        db.commit()

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

    if player_id == game["creator_id"]:

        if game["mode"] == "bot":
            await safe_send_message(
                context.bot,
                game["chat_id"],
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد...",
            )

            await bot_turn(
                context,
                game_id,
            )

        else:
            await safe_send_message(
                context.bot,
                game["chat_id"],
                "✅ پرتاب‌های سازنده تمام شد.\n\n🎯 حالا بازیکن دوم شروع می‌کند.",
            )

            await play_turn(
                context,
                game_id,
                game["opponent_id"],
            )

    else:
        await finish_game(
            context,
            game_id,
        )


# ============================================================
# BOT TURN
# ============================================================

async def bot_turn(context, game_id):
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

    total = 0
    emoji = GAME_EMOJI[game["game_type"]]

    for _ in range(game["count"]):
        result = await safe_send_dice(
            context.bot,
            game["chat_id"],
            emoji,
        )

        if result is not None:
            try:
                total += int(result.dice.value)
            except Exception:
                pass

        await asyncio.sleep(1)

    with closing(get_db()) as db:
        db.execute("""
            UPDATE games
            SET opponent_total = ?
            WHERE id = ?
        """, (
            total,
            game_id,
        ))

        db.commit()

    await finish_game(
        context,
        game_id,
    )


# ============================================================
# FINISH GAME
# ============================================================

async def finish_game(context, game_id):
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

        creator_total = float(game["creator_total"])
        opponent_total = float(game["opponent_total"])

        db.execute("""
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
        """, (game_id,))

        db.commit()

    bet = Decimal(game["bet"])

    # مساوی
    if creator_total == opponent_total:

        try:
            change_balance(
                game["creator_id"],
                bet,
                "draw_refund",
                f"Draw #{game_id}",
            )

            if game["mode"] == "friends":
                change_balance(
                    game["opponent_id"],
                    bet,
                    "draw_refund",
                    f"Draw #{game_id}",
                )

        except Exception as e:
            logger.error(
                "Draw refund error: %s",
                e,
            )

        await safe_send_message(
            context.bot,
            game["chat_id"],
            (
                "🤝 بازی مساوی شد.\n\n"
                f"{game_name(game['game_type'])}\n"
                f"🎯 نتیجه: {creator_total:g} - {opponent_total:g}\n\n"
                "💰 مبلغ شرط برگشت داده شد."
            ),
        )

        return

    if creator_total > opponent_total:
        winner = game["creator_id"]
        winner_total = creator_total
        loser_total = opponent_total
        winner_text = "👤 سازنده"

    else:
        winner = game["opponent_id"]
        winner_total = opponent_total
        loser_total = creator_total
        winner_text = "👤 بازیکن دوم"

    # بازی با ربات
    if game["mode"] == "bot":

        if winner == game["creator_id"]:
            payout = bet * 2

            try:
                change_balance(
                    winner,
                    payout,
                    "game_win",
                    f"Bot game #{game_id}",
                )
            except Exception as e:
                logger.error(
                    "Bot payout error: %s",
                    e,
                )

            result_text = (
                "🏆 شما برنده شدید!\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        else:
            result_text = "🤖 ربات برنده شد."

    else:

        payout = bet * 2

        try:
            change_balance(
                winner,
                payout,
                "game_win",
                f"Game #{game_id}",
            )
        except Exception as e:
            logger.error(
                "Game payout error: %s",
                e,
            )

        result_text = (
            f"🏆 برنده: {winner_text}\n"
            f"💰 جایزه: {money(payout)} TRX"
        )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        (
            "🏁 نتیجه بازی\n\n"
            f"{game_name(game['game_type'])}\n\n"
            f"👤 سازنده: {creator_total:g}\n"
            f"👤 بازیکن دوم: {loser_total:g}\n\n"
            f"{result_text}"
        ),
    )


# ============================================================
# ADMIN PANEL
# ============================================================

async def admin_panel(update, context):
    user = update.effective_user

    if not user or not is_owner(user.id):
        if update.message:
            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                "❌ دسترسی ندارید.",
            )
        return

    enabled = is_bot_enabled()

    keyboard = InlineKeyboardMarkup([
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
                "📊 آمار",
                callback_data="admin_stats",
            )
        ],
    ])

    await safe_send_message(
        context.bot,
        update.effective_chat.id,
        (
            "👑 پنل مدیریت BET_BT\n\n"
            f"وضعیت: {'🟢 روشن' if enabled else '🔴 خاموش'}\n\n"
            "برای شارژ یا کسر در گپ روی پیام کاربر Reply کنید:\n\n"
            "شارژ 100\n"
            "کسر 100"
        ),
        reply_markup=keyboard,
    )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def admin_balance_change(
    update,
    context,
    amount,
    operation,
):
    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        return

    if not message.reply_to_message:
        await safe_send_message(
            context.bot,
            message.chat_id,
            (
                "❌ باید روی پیام کاربر Reply کنید.\n\n"
                "مثال:\n"
                "شارژ 100\n"
                "کسر 100"
            ),
            reply_to_message_id=message.message_id,
        )
        return

    target = message.reply_to_message.from_user

    if not target:
        return

    ensure_user(target)

    if operation == "charge":
        change_balance(
            target.id,
            amount,
            "admin_charge",
            f"Charged by {user.id}",
        )

        action = "شارژ"

    else:
        try:
            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Removed by {user.id}",
            )
        except ValueError:
            await safe_send_message(
                context.bot,
                message.chat_id,
                "❌ موجودی کاربر برای کسر این مبلغ کافی نیست.",
                reply_to_message_id=message.message_id,
            )
            return

        action = "کسر"

    await safe_send_message(
        context.bot,
        message.chat_id,
        (
            f"✅ {action} انجام شد.\n\n"
            f"👤 کاربر: {target.first_name or target.id}\n"
            f"💰 مبلغ: {money(amount)} TRX\n"
            f"💳 موجودی جدید: {money(get_balance(target.id))} TRX"
        ),
        reply_to_message_id=message.message_id,
    )


# ============================================================
# ADMIN CALLBACK
# ============================================================

async def admin_callback(update, context):
    query = update.callback_query
    user = query.from_user

    await query.answer()

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

    if query.data == "admin_on":
        set_bot_enabled(True)

        await safe_edit_message(
            query,
            "👑 پنل مدیریت\n\n🟢 ربات روشن شد.",
        )

    elif query.data == "admin_off":
        set_bot_enabled(False)

        await safe_edit_message(
            query,
            "👑 پنل مدیریت\n\n🔴 ربات خاموش شد.",
        )

    elif query.data == "admin_stats":

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

            games = db.execute("""
                SELECT COUNT(*) AS c
                FROM games
            """).fetchone()["c"]

        await safe_edit_message(
            query,
            (
                "📊 آمار BET_BT\n\n"
                f"👤 کاربران: {users}\n"
                f"💰 مجموع موجودی: {total:.2f} TRX\n"
                f"🎮 تعداد بازی‌ها: {games}"
            ),
        )


# ============================================================
# USER CALLBACK
# ============================================================

async def user_callback(update, context):
    query = update.callback_query

    if query.data == "user_balance":
        await show_balance(update, context)

    elif query.data == "user_referral":
        await show_referral(update, context)

    elif query.data == "user_help":
        await show_help(update, context)

    elif query.data == "back_start":
        await query.answer()

        user = query.from_user
        ensure_user(user)

        balance = get_balance(user.id)

        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton(
                    "💰 موجودی",
                    callback_data="user_balance",
                ),
                InlineKeyboardButton(
                    "👥 زیر مجموعه",
                    callback_data="user_referral",
                ),
            ],
            [
                InlineKeyboardButton(
                    "📚 راهنما",
                    callback_data="user_help",
                )
            ],
        ])

        await safe_edit_message(
            query,
            (
                "🤖 BET_BT\n\n"
                f"💰 موجودی شما: {money(balance)} TRX\n\n"
                "دستورات:\n"
                "موجودی\n"
                "زیر مجموعه\n"
                "انتقال 0.1"
            ),
            keyboard,
        )


# ============================================================
# MEMBERSHIP CALLBACK
# ============================================================

async def membership_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if await check_membership(user.id, context):
        await query.answer(
            "✅ عضویت تأیید شد.",
            show_alert=True,
        )

        await safe_edit_message(
            query,
            "✅ عضویت شما تأیید شد.\nحالا می‌توانید بازی کنید.",
        )

    else:
        await query.answer(
            "❌ هنوز عضو کانال نیستید.",
            show_alert=True,
        )


# ============================================================
# OWNER TEXT COMMANDS
# ============================================================

async def handle_owner_text(update, context, text):
    user = update.effective_user

    if not is_owner(user.id):
        return False

    normalized = normalize_digits(text).strip()

    if normalized == "روشن":
        set_bot_enabled(True)

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🟢 ربات روشن شد.",
        )

        return True

    if normalized == "خاموش":
        set_bot_enabled(False)

        await safe_send_message(
            context.bot,
            update.effective_chat.id,
            "🔴 ربات خاموش شد.",
        )

        return True

    # FIX:
    # شارژ 100
    if normalized.startswith("شارژ"):
        rest = normalized[len("شارژ"):].strip()

        amount = parse_decimal(rest)

        if amount is None:
            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                (
                    "❌ فرمت صحیح:\n"
                    "شارژ 100\n\n"
                    "باید روی پیام کاربر Reply کنید."
                ),
                reply_to_message_id=update.message.message_id,
            )
            return True

        await admin_balance_change(
            update,
            context,
            amount,
            "charge",
        )

        return True

    # FIX:
    # کسر 100
    if normalized.startswith("کسر"):
        rest = normalized[len("کسر"):].strip()

        amount = parse_decimal(rest)

        if amount is None:
            await safe_send_message(
                context.bot,
                update.effective_chat.id,
                (
                    "❌ فرمت صحیح:\n"
                    "کسر 100\n\n"
                    "باید روی پیام کاربر Reply کنید."
                ),
                reply_to_message_id=update.message.message_id,
            )
            return True

        await admin_balance_change(
            update,
            context,
            amount,
            "remove",
        )

        return True

    return False


# ============================================================
# TEXT HANDLER
# ============================================================

async def text_handler(update, context):
    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    text = normalize_digits(
        message.text or ""
    ).strip()

    # --------------------------------------------------------
    # OWNER
    # --------------------------------------------------------

    if is_owner(user.id):
        handled = await handle_owner_text(
            update,
            context,
            text,
        )

        if handled:
            return

    # --------------------------------------------------------
    # BALANCE
    # --------------------------------------------------------

    if text in (
        "موجودی",
        "موجودی من",
        "balance",
    ):
        await show_balance(
            update,
            context,
        )
        return

    # --------------------------------------------------------
    # REFERRAL
    # --------------------------------------------------------

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

    # --------------------------------------------------------
    # TRANSFER
    # --------------------------------------------------------

    if text.startswith("انتقال"):
        rest = text[len("انتقال"):].strip()

        amount = parse_decimal(rest)

        if amount is None:
            await safe_send_message(
                context.bot,
                message.chat_id,
                (
                    "❌ فرمت صحیح:\n"
                    "انتقال 0.1\n"
                    "انتقال ۰.۱\n\n"
                    "باید روی پیام کاربر Reply کنید."
                ),
                reply_to_message_id=message.message_id,
            )
            return

        await transfer(
            update,
            context,
            amount,
        )
        return

    # --------------------------------------------------------
    # GAME
    # --------------------------------------------------------

    game = parse_game_command(text)

    if game:
        count, game_type, bet = game

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):
            await safe_send_message(
                context.bot,
                message.chat_id,
                "❌ بازی‌ها فقط داخل گپ انجام می‌شوند.",
            )
            return

        await create_game(
            update,
            context,
            count,
            game_type,
            bet,
        )
        return


# ============================================================
# HELP COMMAND
# ============================================================

async def help_command(update, context):
    await show_help(
        update,
        context,
    )


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):
    error = context.error

    if isinstance(error, (TimedOut, NetworkError)):
        logger.warning(
            "Network/Timeout error handled: %s",
            error,
        )
        return

    logger.exception(
        "Unhandled error:",
        exc_info=error,
    )


# ============================================================
# MAIN
# ============================================================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN در Environment Variables تنظیم نشده است."
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

    # --------------------------------------------------------
    # COMMANDS
    # --------------------------------------------------------

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
            show_balance,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_panel,
        )
    )

    # --------------------------------------------------------
    # MEMBERSHIP
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$",
        )
    )

    # --------------------------------------------------------
    # ADMIN CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|stats)$",
        )
    )

    # --------------------------------------------------------
    # USER CALLBACK
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            user_callback,
            pattern=r"^(user_balance|user_referral|user_help|back_start)$",
        )
    )

    # --------------------------------------------------------
    # GAME CALLBACKS
    # --------------------------------------------------------

    application.add_handler(
        CallbackQueryHandler(
            join_game,
            pattern=r"^join_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            bot_game,
            pattern=r"^bot_\d+$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            cancel_game,
            pattern=r"^cancel_\d+$",
        )
    )

    # --------------------------------------------------------
    # TEXT
    # --------------------------------------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    # --------------------------------------------------------
    # ERROR
    # --------------------------------------------------------

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started successfully."
    )

    # --------------------------------------------------------
    # POLLING
    # --------------------------------------------------------

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


if __name__ == "__main__":
    main()
