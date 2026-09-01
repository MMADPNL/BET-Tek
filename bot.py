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
    ConversationHandler,
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

MIN_GAME_BET = Decimal("0.1")
REFERRAL_REWARD = Decimal("0.05")
MAX_GAME_COUNT = 20

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
                referred_id INTEGER UNIQUE NOT NULL,
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


# =========================================================
# HELPERS
# =========================================================

def normalize_digits(text):
    table = str.maketrans(
        "۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩",
        "01234567890123456789"
    )
    return str(text).translate(table)


def parse_decimal(text):
    text = normalize_digits(text).strip()
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
        return f"{value:.0f}"

    return f"{value:.2f}"


def is_owner(user_id):
    return user_id in OWNER_IDS


def game_name(game_type):
    return {
        "dice": "🎲 تاس",
        "darts": "🎯 دارت",
        "bowling": "🎳 بولینگ",
        "basketball": "🏀 بسکتبال",
    }.get(game_type, game_type)


def parse_game_command(text):
    text = normalize_digits(text.strip())

    match = re.match(
        r"^(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)$",
        text
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


# =========================================================
# USERS / BALANCE
# =========================================================

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

    return Decimal(row["balance"])


def change_balance(user_id, amount, transaction_type, description=""):
    amount = Decimal(str(amount))

    with closing(get_db()) as db:

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


# =========================================================
# BOT STATUS
# =========================================================

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
        """, (
            "1" if enabled else "0",
        ))

        db.commit()


# =========================================================
# SAFE TELEGRAM SEND
# =========================================================

async def safe_send_message(
    context,
    chat_id,
    text,
    **kwargs
):
    for attempt in range(4):

        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs
            )

        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)

        except (TimedOut, NetworkError):
            if attempt >= 3:
                raise

            await asyncio.sleep(2 ** attempt)

        except Exception:
            raise

    return None


async def safe_send_dice(
    context,
    chat_id,
    emoji
):
    for attempt in range(4):

        try:
            return await context.bot.send_dice(
                chat_id=chat_id,
                emoji=emoji,
                read_timeout=30,
                write_timeout=30,
                connect_timeout=30,
                pool_timeout=30,
            )

        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)

        except (TimedOut, NetworkError):

            if attempt >= 3:
                raise

            await asyncio.sleep(2 ** attempt)

    return None


# =========================================================
# MEMBERSHIP
# =========================================================

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
            "Membership check error: %s",
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

        await update.message.reply_text(
            "🔒 برای استفاده از بازی‌ها ابتدا عضو کانال شوید.",
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

        await query.edit_message_text(
            "✅ عضویت تأیید شد.\n"
            "حالا می‌توانید بازی کنید."
        )

    else:

        try:
            await query.answer(
                "❌ هنوز عضو کانال نیستید.",
                show_alert=True
            )
        except Exception:
            pass


# =========================================================
# START
# =========================================================

async def start(update, context):

    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    # Referral
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

                    user_row = db.execute("""
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                    """, (
                        user.id,
                    )).fetchone()

                    if (
                        user_row
                        and user_row["referrer_id"] is None
                    ):

                        already = db.execute("""
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                        """, (
                            user.id,
                        )).fetchone()

                        if not already:

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
                                    f"Referral {user.id}"
                                )
                            except Exception as e:
                                logger.error(
                                    "Referral reward error: %s",
                                    e
                                )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="menu_balance"
            ),
            InlineKeyboardButton(
                "👥 زیر مجموعه",
                callback_data="menu_referral"
            )
        ],
        [
            InlineKeyboardButton(
                "📚 راهنما",
                callback_data="menu_help"
            )
        ]
    ])

    balance = get_balance(user.id)

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی شما: {money(balance)} BT\n\n"
        "دستورات:\n"
        "موجودی\n"
        "زیر مجموعه\n"
        "انتقال 0.1\n\n"
        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1",
        reply_markup=keyboard
    )


# =========================================================
# MENU CALLBACKS
# =========================================================

async def menu_callback(update, context):

    query = update.callback_query

    try:
        await query.answer()
    except Exception:
        pass

    user = query.from_user
    ensure_user(user)

    if query.data == "menu_balance":

        balance = get_balance(user.id)

        await query.edit_message_text(
            f"💰 موجودی شما: {money(balance)} BT"
        )

    elif query.data == "menu_referral":

        bot = await context.bot.get_me()

        link = (
            f"https://t.me/{bot.username}"
            f"?start=ref_{user.id}"
        )

        with closing(get_db()) as db:

            row = db.execute("""
                SELECT COUNT(*) AS c
                FROM referrals
                WHERE referrer_id = ?
            """, (
                user.id,
            )).fetchone()

        count = row["c"]

        await query.edit_message_text(
            "👥 زیر مجموعه\n\n"
            f"🔗 لینک دعوت:\n{link}\n\n"
            f"👤 تعداد: {count}\n"
            f"🎁 پاداش هر نفر: "
            f"{money(REFERRAL_REWARD)} BT"
        )

    elif query.data == "menu_help":

        await query.edit_message_text(
            "📚 راهنما\n\n"
            "💰 موجودی\n"
            "👥 زیر مجموعه\n"
            "🔄 انتقال 0.1\n\n"
            "🎮 بازی:\n"
            "1 تاس 0.1\n"
            "2 دارت 0.1\n"
            "3 بولینگ 0.1\n"
            "4 بسکتبال 0.1\n\n"
            "برای بازی دوستان بعد از ساخت بازی "
            "دکمه «بازی با دوستان» را بزنید."
        )


# =========================================================
# BALANCE
# =========================================================

async def balance(update, context):

    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    await update.message.reply_text(
        f"💰 موجودی شما: "
        f"{money(get_balance(user.id))} BT"
    )


# =========================================================
# REFERRAL
# =========================================================

async def referral(update, context):

    user = update.effective_user

    if not user or not update.message:
        return

    ensure_user(user)

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start=ref_{user.id}"
    )

    with closing(get_db()) as db:

        row = db.execute("""
            SELECT COUNT(*) AS c
            FROM referrals
            WHERE referrer_id = ?
        """, (
            user.id,
        )).fetchone()

    count = row["c"]

    await update.message.reply_text(
        "👥 زیر مجموعه\n\n"
        f"🔗 لینک دعوت شما:\n{link}\n\n"
        f"👤 تعداد زیرمجموعه: {count}\n"
        f"🎁 پاداش هر نفر: "
        f"{money(REFERRAL_REWARD)} BT"
    )


# =========================================================
# TRANSFER
# =========================================================

async def transfer(update, context, amount):

    message = update.message
    user = update.effective_user

    if not message.reply_to_message:

        await message.reply_text(
            "❌ برای انتقال باید روی پیام کاربر Reply کنید.\n\n"
            "مثال:\n"
            "انتقال 0.1\n"
            "انتقال ۰.۱"
        )

        return

    target = message.reply_to_message.from_user

    if not target:

        await message.reply_text(
            "❌ کاربر مقصد پیدا نشد."
        )

        return

    if target.id == user.id:

        await message.reply_text(
            "❌ نمی‌توانید به خودتان انتقال دهید."
        )

        return

    ensure_user(user)
    ensure_user(target)

    try:

        # انتقال اتمیک با تراکنش واحد
        with closing(get_db()) as db:

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

    except ValueError as e:

        if str(e) == "insufficient_balance":

            await message.reply_text(
                "❌ موجودی شما کافی نیست."
            )

        else:

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )

        return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} BT\n"
        f"👤 گیرنده: "
        f"{target.first_name or target.id}\n"
        f"💰 موجودی جدید: "
        f"{money(get_balance(user.id))} BT"
    )


# =========================================================
# ADMIN CHARGE / REMOVE
# =========================================================

async def admin_change(update, context, amount, operation):

    user = update.effective_user
    message = update.message

    if not is_owner(user.id):
        await message.reply_text(
            "❌ دسترسی ندارید."
        )
        return

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

    ensure_user(target)

    if operation == "charge":

        change_balance(
            target.id,
            amount,
            "admin_charge",
            f"Owner {user.id}"
        )

        title = "شارژ"

    else:

        try:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {user.id}"
            )

        except ValueError:

            await message.reply_text(
                "❌ موجودی کاربر کافی نیست."
            )

            return

        title = "کسر"

    await message.reply_text(
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: "
        f"{target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} BT\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} BT"
    )


# =========================================================
# ADMIN PANEL
# =========================================================

async def admin_panel(update, context):

    user = update.effective_user

    if not is_owner(user.id):

        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )

        return

    enabled = is_bot_enabled()

    keyboard = InlineKeyboardMarkup([
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
                "📊 آمار",
                callback_data="admin_stats"
            )
        ]
    ])

    await update.message.reply_text(
        "👑 پنل مدیریت\n\n"
        f"وضعیت: "
        f"{'🟢 روشن' if enabled else '🔴 خاموش'}\n\n"
        "شارژ در گپ:\n"
        "روی پیام کاربر Reply کنید:\n"
        "شارژ 100\n\n"
        "کسر در گپ:\n"
        "روی پیام کاربر Reply کنید:\n"
        "کسر 100",
        reply_markup=keyboard
    )


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

    if query.data == "admin_on":

        set_bot_enabled(True)

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            "🟢 ربات روشن شد."
        )

    elif query.data == "admin_off":

        set_bot_enabled(False)

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            "🔴 ربات خاموش شد."
        )

    elif query.data == "admin_stats":

        with closing(get_db()) as db:

            users = db.execute("""
                SELECT COUNT(*) AS c
                FROM users
            """).fetchone()["c"]

            total = db.execute("""
                SELECT COALESCE(
                    SUM(CAST(balance AS REAL)), 0
                ) AS total
                FROM users
            """).fetchone()["total"]

            games = db.execute("""
                SELECT COUNT(*) AS c
                FROM games
            """).fetchone()["c"]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👤 کاربران: {users}\n"
            f"💰 مجموع موجودی: {total:.2f} BT\n"
            f"🎮 تعداد بازی‌ها: {games}"
        )


# =========================================================
# GAME ROLL
# =========================================================

GAME_EMOJIS = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}


async def roll_game(context, chat_id, game_type):

    message = await safe_send_dice(
        context,
        chat_id,
        GAME_EMOJIS[game_type]
    )

    if not message or not message.dice:
        raise RuntimeError("dice_result_missing")

    return message.dice.value


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
    message = update.message
    chat = update.effective_chat

    if not is_bot_enabled():

        await message.reply_text(
            "🔴 ربات خاموش است."
        )

        return

    if not await require_membership(update, context):
        return

    ensure_user(user)

    if bet < MIN_GAME_BET:

        await message.reply_text(
            f"❌ حداقل شرط "
            f"{money(MIN_GAME_BET)} BT است."
        )

        return

    try:

        # رزرو شرط قبل از ساخت بازی
        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Game {game_type}"
        )

    except ValueError:

        await message.reply_text(
            "❌ موجودی شما کافی نیست.\n\n"
            f"💰 موجودی: "
            f"{money(get_balance(user.id))} BT"
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
            chat.id,
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
        ]
    ])

    sent = await message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{game_name(game_type)}\n"
        f"🔢 تعداد بازی: {count}\n"
        f"💰 شرط: {money(bet)} BT\n"
        f"👤 سازنده: {user.first_name or user.id}\n\n"
        "گزینه موردنظر را انتخاب کنید:",
        reply_markup=keyboard
    )

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


# =========================================================
# JOIN FRIEND GAME
# =========================================================

async def join_game(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    if not await check_membership(user.id, context):

        try:
            await query.answer(
                "❌ ابتدا عضو BET_Tek شوید.",
                show_alert=True
            )
        except Exception:
            pass

        return

    game_id = int(
        query.data.replace("join_", "")
    )

    ensure_user(user)

    with closing(get_db()) as db:

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
                "❌ این بازی دیگر منتظر بازیکن نیست.",
                show_alert=True
            )

            return

        if game["creator_id"] == user.id:

            db.rollback()

            await query.answer(
                "❌ خودتان نمی‌توانید وارد بازی خودتان شوید.",
                show_alert=True
            )

            return

        bet = Decimal(game["bet"])

        row = db.execute("""
            SELECT balance
            FROM users
            WHERE user_id = ?
        """, (
            user.id,
        )).fetchone()

        if not row or Decimal(row["balance"]) < bet:

            db.rollback()

            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
            )

            return

        current = Decimal(row["balance"])

        db.execute("""
            UPDATE users
            SET balance = ?
            WHERE user_id = ?
        """, (
            str(current - bet),
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
            f"Join game {game_id}",
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

    await query.edit_message_text(
        "🎮 بازی شروع شد!\n\n"
        f"{game_name(game['game_type'])}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} BT\n\n"
        f"👤 سازنده: {game['creator_id']}\n"
        f"👤 بازیکن دوم: {user.first_name or user.id}\n\n"
        "🎯 ابتدا سازنده تمام پرتاب‌های خود را انجام می‌دهد."
    )

    await play_turn(
        context,
        game_id,
        game["creator_id"]
    )


# =========================================================
# BOT GAME
# =========================================================

async def bot_game(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    game_id = int(
        query.data.replace("bot_", "")
    )

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
            return

        if game["creator_id"] != user.id:

            await query.answer(
                "❌ فقط سازنده می‌تواند بازی با ربات را انتخاب کند.",
                show_alert=True
            )

            return

        db.execute("""
            UPDATE games
            SET mode = 'bot',
                opponent_id = -1,
                status = 'playing'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    await query.edit_message_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{game_name(game['game_type'])}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} BT\n\n"
        "🎯 تمام پرتاب‌های شما ابتدا انجام می‌شود."
    )

    await play_turn(
        context,
        game_id,
        user.id
    )


# =========================================================
# CANCEL GAME
# =========================================================

async def cancel_game(update, context):

    query = update.callback_query
    user = query.from_user

    try:
        await query.answer()
    except Exception:
        pass

    game_id = int(
        query.data.replace("cancel_", "")
    )

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
                "❌ بازی قابل لغو نیست.",
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

        db.execute("""
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    bet = Decimal(game["bet"])

    try:

        change_balance(
            game["creator_id"],
            bet,
            "game_refund",
            f"Cancel game {game_id}"
        )

    except Exception as e:

        logger.error(
            "Refund error: %s",
            e
        )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(bet)} BT به سازنده برگشت داده شد."
    )


# =========================================================
# PLAY TURN
# =========================================================

async def play_turn(context, game_id, player_id):

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

    total = 0

    for _ in range(game["count"]):

        try:

            value = await roll_game(
                context,
                game["chat_id"],
                game["game_type"]
            )

            total += value

            # فاصله کوچک برای جلوگیری از Flood/Timeout
            await asyncio.sleep(0.5)

        except Exception as e:

            logger.error(
                "Roll error game=%s: %s",
                game_id,
                e
            )

            await safe_send_message(
                context,
                game["chat_id"],
                "⚠️ خطا در ارسال یکی از پرتاب‌ها. بازی متوقف شد."
            )

            await cancel_running_game(
                context,
                game_id
            )

            return

    with closing(get_db()) as db:

        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (
            game_id,
        )).fetchone()

        if not game or game["status"] != "playing":
            return

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

    if player_id == game["creator_id"]:

        if game["mode"] == "bot":

            await safe_send_message(
                context,
                game["chat_id"],
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
            )

            bot_total = 0

            for _ in range(game["count"]):

                try:

                    value = await roll_game(
                        context,
                        game["chat_id"],
                        game["game_type"]
                    )

                    bot_total += value

                    await asyncio.sleep(0.5)

                except Exception as e:

                    logger.error(
                        "Bot roll error: %s",
                        e
                    )

                    await cancel_running_game(
                        context,
                        game_id
                    )

                    return

            with closing(get_db()) as db:

                db.execute("""
                    UPDATE games
                    SET opponent_total = ?
                    WHERE id = ?
                """, (
                    bot_total,
                    game_id,
                ))

                db.commit()

            await finish_game(
                context,
                game_id
            )

        else:

            await safe_send_message(
                context,
                game["chat_id"],
                "✅ پرتاب‌های سازنده تمام شد.\n\n"
                "🎯 حالا بازیکن دوم تمام پرتاب‌های خودش را انجام می‌دهد."
            )

            await play_turn(
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
# CANCEL RUNNING GAME
# =========================================================

async def cancel_running_game(context, game_id):

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

        db.execute("""
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    bet = Decimal(game["bet"])

    # برگرداندن شرط سازنده
    try:

        change_balance(
            game["creator_id"],
            bet,
            "game_refund",
            f"Game error refund {game_id}"
        )

    except Exception:
        pass

    # در بازی دوستان شرط نفر دوم هم برگردانده شود
    if (
        game["mode"] == "friends"
        and game["opponent_id"]
    ):

        try:

            change_balance(
                game["opponent_id"],
                bet,
                "game_refund",
                f"Game error refund {game_id}"
            )

        except Exception:
            pass

    await safe_send_message(
        context,
        game["chat_id"],
        "❌ بازی به دلیل خطای ارتباطی متوقف شد.\n"
        "💰 شرط‌های ثبت‌شده به موجودی بازگردانده شد."
    )


# =========================================================
# FINISH GAME
# =========================================================

async def finish_game(context, game_id):

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

        creator_total = float(
            game["creator_total"]
        )

        opponent_total = float(
            game["opponent_total"]
        )

        bet = Decimal(game["bet"])

        db.execute("""
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    # مساوی
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

        except Exception as e:

            logger.error(
                "Draw refund error: %s",
                e
            )

        await safe_send_message(
            context,
            game["chat_id"],
            "🤝 بازی مساوی شد.\n\n"
            f"👤 سازنده: {creator_total:g}\n"
            f"👤 بازیکن دوم: {opponent_total:g}\n\n"
            "💰 شرط‌ها برگشت داده شدند."
        )

        return

    # برنده
    if creator_total > opponent_total:

        winner_id = game["creator_id"]
        winner_text = "👤 سازنده"

    else:

        winner_id = game["opponent_id"]
        winner_text = "👤 بازیکن دوم"

    # بازی با ربات
    if game["mode"] == "bot":

        if winner_id == game["creator_id"]:

            payout = bet * 2

            try:

                change_balance(
                    winner_id,
                    payout,
                    "game_win",
                    f"Bot game win {game_id}"
                )

            except Exception as e:

                logger.error(
                    "Bot payout error: %s",
                    e
                )

            result = (
                "🏆 شما برنده شدید!\n"
                f"💰 جایزه: {money(payout)} BT"
            )

        else:

            result = (
                "🤖 ربات برنده شد.\n"
                "💰 این دور جایزه‌ای دریافت نشد."
            )

    else:

        payout = bet * 2

        try:

            change_balance(
                winner_id,
                payout,
                "game_win",
                f"Friend game win {game_id}"
            )

        except Exception as e:

            logger.error(
                "Friend payout error: %s",
                e
            )

        result = (
            f"🏆 برنده: {winner_text}\n"
            f"💰 جایزه: {money(payout)} BT"
        )

    await safe_send_message(
        context,
        game["chat_id"],
        "🏁 نتیجه بازی\n\n"
        f"{game_name(game['game_type'])}\n\n"
        f"👤 سازنده: {creator_total:g}\n"
        f"👤 بازیکن دوم: {opponent_total:g}\n\n"
        f"{result}"
    )


# =========================================================
# TEXT HANDLER
# =========================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    ensure_user(user)

    text = normalize_digits(
        message.text.strip()
    )

    # -------------------------
    # OWNER COMMANDS
    # -------------------------

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

        if text.startswith("شارژ "):

            amount = parse_decimal(
                text[6:].strip()
            )

            if amount is None:

                await message.reply_text(
                    "❌ فرمت صحیح:\n"
                    "شارژ 100"
                )

                return

            await admin_change(
                update,
                context,
                amount,
                "charge"
            )

            return

        if text.startswith("کسر "):

            amount = parse_decimal(
                text[4:].strip()
            )

            if amount is None:

                await message.reply_text(
                    "❌ فرمت صحیح:\n"
                    "کسر 100"
                )

                return

            await admin_change(
                update,
                context,
                amount,
                "remove"
            )

            return

    # -------------------------
    # BALANCE
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
    # REFERRAL
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
    # TRANSFER
    # -------------------------

    if text.startswith("انتقال "):

        amount = parse_decimal(
            text[7:].strip()
        )

        if amount is None:

            await message.reply_text(
                "❌ فرمت صحیح:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱"
            )

            return

        await transfer(
            update,
            context,
            amount
        )

        return

    # -------------------------
    # GAME
    # -------------------------

    game = parse_game_command(text)

    if game:

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
        ):

            await message.reply_text(
                "❌ بازی‌ها فقط داخل گپ قابل اجرا هستند."
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


# =========================================================
# ERROR HANDLER
# =========================================================

async def error_handler(update, context):

    error = context.error

    if isinstance(error, RetryAfter):

        logger.warning(
            "Telegram rate limit: %s",
            error
        )

        return

    if isinstance(error, (TimedOut, NetworkError)):

        logger.warning(
            "Telegram network timeout/error: %s",
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
            "BOT_TOKEN در Environment Variables قرار نگرفته."
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

    # -------------------------
    # COMMANDS
    # -------------------------

    application.add_handler(
        CommandHandler(
            "start",
            start
        )
    )

    application.add_handler(
        CommandHandler(
            "help",
            lambda update, context: help_message(
                update,
                context
            )
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

    # -------------------------
    # CALLBACKS
    # -------------------------

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            menu_callback,
            pattern=r"^menu_(balance|referral|help)$"
        )
    )

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

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|stats)$"
        )
    )

    # -------------------------
    # TEXT
    # -------------------------

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
        "BET_BT started."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


# =========================================================
# HELP
# =========================================================

async def help_message(update, context):

    if not update.message:
        return

    await update.message.reply_text(
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n\n"
        "🎮 بازی‌ها در گپ:\n"
        "1 تاس 0.1\n"
        "2 دارت 0.1\n"
        "3 بولینگ 0.1\n"
        "4 بسکتبال 0.1\n\n"
        "🔹 انتقال باید با Reply انجام شود.\n"
        "🔹 شارژ و کسر فقط توسط مالک انجام می‌شود."
    )


# =========================================================
# RUN
# =========================================================

if __name__ == "__main__":
    main()
