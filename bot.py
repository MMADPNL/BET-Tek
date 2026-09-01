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
from telegram.error import TimedOut, NetworkError, RetryAfter
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
    format="%(asctime)s | %(levelname)s | %(message)s",
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
                creator_total INTEGER DEFAULT 0,
                opponent_total INTEGER DEFAULT 0,
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
    return int(user_id) in OWNER_IDS


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


def change_balance(
    user_id,
    amount,
    transaction_type,
    description=""
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

        old_balance = Decimal(row["balance"])
        new_balance = old_balance + amount

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
            DO UPDATE SET value=excluded.value
        """, ("1" if enabled else "0",))
        db.commit()


# ============================================================
# SAFE TELEGRAM SEND
# ============================================================

async def safe_send_message(bot, chat_id, text, **kwargs):
    for attempt in range(4):
        try:
            return await bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs
            )

        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)

        except (TimedOut, NetworkError):
            if attempt >= 3:
                raise
            await asyncio.sleep(2 * (attempt + 1))

        except Exception:
            raise

    return None


async def safe_send_dice(bot, chat_id, emoji):
    for attempt in range(4):
        try:
            return await bot.send_dice(
                chat_id=chat_id,
                emoji=emoji
            )

        except RetryAfter as e:
            await asyncio.sleep(float(e.retry_after) + 1)

        except (TimedOut, NetworkError):
            if attempt >= 3:
                raise
            await asyncio.sleep(2 * (attempt + 1))

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
        logger.warning("Membership check: %s", e)
        return False


async def require_membership(update, context):
    user = update.effective_user

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
            "🔒 ابتدا عضو کانال BET_Tek شوید.",
            reply_markup=keyboard
        )

    return False


async def membership_callback(update, context):
    query = update.callback_query

    if await check_membership(query.from_user.id, context):
        await query.answer("✅ عضویت تأیید شد.", show_alert=True)

        try:
            await query.edit_message_text(
                "✅ عضویت شما تأیید شد."
            )
        except Exception:
            pass
    else:
        await query.answer(
            "❌ هنوز عضو کانال نیستید.",
            show_alert=True
        )


# ============================================================
# START
# ============================================================

async def start(update, context):
    user = update.effective_user
    ensure_user(user)

    # referral
    if context.args:
        arg = context.args[0]

        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
            except ValueError:
                referrer_id = None

            if referrer_id and referrer_id != user.id:
                with closing(get_db()) as db:
                    current = db.execute("""
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                    """, (user.id,)).fetchone()

                    exists = db.execute("""
                        SELECT id
                        FROM referrals
                        WHERE referred_id = ?
                    """, (user.id,)).fetchone()

                    if (
                        current
                        and current["referrer_id"] is None
                        and not exists
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
                            ensure_user(
                                await context.bot.get_chat(referrer_id)
                            )
                        except Exception:
                            pass

                        try:
                            change_balance(
                                referrer_id,
                                REFERRAL_REWARD,
                                "referral",
                                f"Referral {user.id}"
                            )
                        except Exception:
                            pass

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(get_balance(user.id))} TRX\n\n"
        "📌 دستورات:\n"
        "موجودی\n"
        "زیر مجموعه\n"
        "انتقال 0.1\n\n"
        "🎮 بازی در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1"
    )


async def help_command(update, context):
    await update.message.reply_text(
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n\n"
        "🎮 بازی‌ها:\n"
        "1 تاس 0.1\n"
        "2 دارت 0.1\n"
        "3 بولینگ 0.1\n"
        "4 بسکتبال 0.1\n\n"
        "حداکثر تعداد بازی: 20"
    )


# ============================================================
# BALANCE
# ============================================================

async def balance(update, context):
    user = update.effective_user
    ensure_user(user)

    await update.message.reply_text(
        f"💰 موجودی شما: {money(get_balance(user.id))} TRX"
    )


# ============================================================
# REFERRAL
# ============================================================

async def referral(update, context):
    user = update.effective_user
    ensure_user(user)

    me = await context.bot.get_me()

    link = f"https://t.me/{me.username}?start=ref_{user.id}"

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT COUNT(*) AS total
            FROM referrals
            WHERE referrer_id = ?
        """, (user.id,)).fetchone()

    count = row["total"] if row else 0

    await update.message.reply_text(
        "👥 زیر مجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {count}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} TRX"
    )


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

    if amount < MIN_GAME_BET:
        await message.reply_text(
            "❌ حداقل انتقال 0.1 TRX است."
        )
        return

    ensure_user(user)
    ensure_user(target)

    # اتمیک: کم کردن از فرستنده و اضافه کردن به گیرنده
    with closing(get_db()) as db:
        try:
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
                await message.reply_text(
                    "❌ خطا در انتقال."
                )
                return

            sender_balance = Decimal(sender["balance"])
            receiver_balance = Decimal(receiver["balance"])

            if sender_balance < amount:
                db.rollback()
                await message.reply_text(
                    f"❌ موجودی کافی نیست.\n"
                    f"💰 موجودی: {money(sender_balance)} TRX"
                )
                return

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
                    user_id, type, amount, description
                )
                VALUES (?, 'transfer_out', ?, ?)
            """, (
                user.id,
                str(-amount),
                f"To {target.id}",
            ))

            db.execute("""
                INSERT INTO transactions(
                    user_id, type, amount, description
                )
                VALUES (?, 'transfer_in', ?, ?)
            """, (
                target.id,
                str(amount),
                f"From {user.id}",
            ))

            db.commit()

        except Exception:
            db.rollback()
            logger.exception("Transfer error")

            await message.reply_text(
                "❌ انتقال انجام نشد."
            )
            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: {target.first_name or target.id}\n"
        f"💰 موجودی شما: {money(get_balance(user.id))} TRX"
    )


# ============================================================
# ADMIN PANEL
# ============================================================

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
                "💰 شارژ موجودی",
                callback_data="admin_charge_help"
            ),
            InlineKeyboardButton(
                "➖ کسر موجودی",
                callback_data="admin_remove_help"
            )
        ],
        [
            InlineKeyboardButton(
                "📊 آمار",
                callback_data="admin_stats"
            )
        ]
    ])


async def admin_panel(update, context):
    user = update.effective_user

    if not is_owner(user.id):
        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )
        return

    status = "🟢 روشن" if is_bot_enabled() else "🔴 خاموش"

    await update.message.reply_text(
        "👑 پنل مدیریت BET_BT\n\n"
        f"وضعیت: {status}\n\n"
        "💰 شارژ در گپ:\n"
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "شارژ 100\n\n"
        "➖ کسر در گپ:\n"
        "روی پیام کاربر Reply کنید و بنویسید:\n"
        "کسر 100",
        reply_markup=admin_keyboard()
    )


async def admin_callback(update, context):
    query = update.callback_query
    user = query.from_user

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True
        )
        return

    await query.answer()

    if query.data == "admin_on":
        set_bot_enabled(True)

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            "🟢 ربات روشن شد.",
            reply_markup=admin_keyboard()
        )

    elif query.data == "admin_off":
        set_bot_enabled(False)

        await query.edit_message_text(
            "👑 پنل مدیریت\n\n"
            "🔴 ربات خاموش شد.",
            reply_markup=admin_keyboard()
        )

    elif query.data == "admin_charge_help":
        await query.answer(
            "در گپ روی پیام کاربر Reply کنید و «شارژ 100» بنویسید.",
            show_alert=True
        )

    elif query.data == "admin_remove_help":
        await query.answer(
            "در گپ روی پیام کاربر Reply کنید و «کسر 100» بنویسید.",
            show_alert=True
        )

    elif query.data == "admin_stats":
        with closing(get_db()) as db:
            users = db.execute("""
                SELECT COUNT(*) AS c FROM users
            """).fetchone()["c"]

            total = db.execute("""
                SELECT COALESCE(
                    SUM(CAST(balance AS REAL)), 0
                ) AS total
                FROM users
            """).fetchone()["total"]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👤 کاربران: {users}\n"
            f"💰 مجموع موجودی: {total:.2f} TRX\n\n"
            f"وضعیت: "
            f"{'🟢 روشن' if is_bot_enabled() else '🔴 خاموش'}",
            reply_markup=admin_keyboard()
        )


# ============================================================
# ADMIN CHARGE / REMOVE
# ============================================================

async def admin_change(update, amount, operation):
    owner = update.effective_user
    message = update.message

    if not is_owner(owner.id):
        return

    if not message.reply_to_message:
        await message.reply_text(
            "❌ باید روی پیام کاربر Reply کنید.\n\n"
            f"مثال:\n"
            f"{'شارژ' if operation == 'charge' else 'کسر'} 100"
        )
        return

    target = message.reply_to_message.from_user

    if not target:
        await message.reply_text(
            "❌ کاربر پیدا نشد."
        )
        return

    ensure_user(target)

    if amount <= 0:
        await message.reply_text(
            "❌ مبلغ صحیح نیست."
        )
        return

    try:
        if operation == "charge":
            change_balance(
                target.id,
                amount,
                "admin_charge",
                f"Owner {owner.id}"
            )
            title = "شارژ"

        else:
            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {owner.id}"
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

    await message.reply_text(
        f"✅ {title} با موفقیت انجام شد.\n\n"
        f"👤 کاربر: {target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: {money(get_balance(target.id))} TRX"
    )


# ============================================================
# GAME PARSER
# ============================================================

GAME_MAP = {
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

GAME_EMOJI = {
    "dice": "🎲",
    "darts": "🎯",
    "bowling": "🎳",
    "basketball": "🏀",
}

GAME_NAME = {
    "dice": "🎲 تاس",
    "darts": "🎯 دارت",
    "bowling": "🎳 بولینگ",
    "basketball": "🏀 بسکتبال",
}


def parse_game_command(text):
    text = normalize_digits(text.strip())

    match = re.fullmatch(
        r"(\d+)\s+([^\s]+)\s+([0-9]+(?:\.[0-9]+)?)",
        text
    )

    if not match:
        return None

    count = int(match.group(1))
    name = match.group(2).lower()
    bet = parse_decimal(match.group(3))

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if name not in GAME_MAP:
        return None

    if bet is None:
        return None

    return count, GAME_MAP[name], bet


# ============================================================
# CREATE GAME
# ============================================================

async def create_game(update, context, count, game_type, bet):
    user = update.effective_user
    chat = update.effective_chat

    if not await require_membership(update, context):
        return

    ensure_user(user)

    if bet < MIN_GAME_BET:
        await update.message.reply_text(
            "❌ حداقل شرط 0.1 TRX است."
        )
        return

    balance = get_balance(user.id)

    if balance < bet:
        await update.message.reply_text(
            f"❌ موجودی کافی نیست.\n"
            f"💰 موجودی: {money(balance)} TRX"
        )
        return

    # کسر شرط سازنده
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

    await update.message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{GAME_NAME[game_type]}\n"
        f"🔢 تعداد: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: {user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard
    )


# ============================================================
# JOIN FRIEND
# ============================================================

async def join_game(update, context):
    query = update.callback_query
    user = query.from_user

    if not await check_membership(user.id, context):
        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True
        )
        return

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
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
                show_alert=True
            )
            return

        if game["status"] != "waiting":
            db.rollback()
            await query.answer(
                "❌ این بازی قبلاً شروع شده.",
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

        bet = Decimal(game["bet"])
        balance = get_balance(user.id)

        if balance < bet:
            db.rollback()
            await query.answer(
                "❌ موجودی کافی نیست.",
                show_alert=True
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
            VALUES (?, 'game_bet', ?, ?)
        """, (
            user.id,
            str(-bet),
            f"Joined game {game_id}",
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
        f"{GAME_NAME[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} TRX\n\n"
        "🎯 ابتدا سازنده تمام پرتاب‌های خود را انجام می‌دهد."
    )

    await play_all(
        context,
        game_id,
        game["creator_id"]
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(update, context):
    query = update.callback_query
    user = query.from_user

    if not await check_membership(user.id, context):
        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True
        )
        return

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
        return

    with closing(get_db()) as db:
        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

        if not game:
            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )
            return

        if game["creator_id"] != user.id:
            await query.answer(
                "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                show_alert=True
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ بازی قبلاً شروع شده.",
                show_alert=True
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

    await query.edit_message_text(
        "🤖 بازی با ربات شروع شد!\n\n"
        f"{GAME_NAME[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
        "🎯 اول تمام پرتاب‌های شما انجام می‌شود."
    )

    # اول کاربر
    await play_all(
        context,
        game_id,
        user.id
    )


# ============================================================
# PLAY ALL ROLLS
# ============================================================

async def play_all(context, game_id, player_id):
    with closing(get_db()) as db:
        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

    if not game or game["status"] != "playing":
        return

    total = 0

    for _ in range(game["count"]):
        try:
            result = await safe_send_dice(
                context.bot,
                game["chat_id"],
                GAME_EMOJI[game["game_type"]]
            )

            if result and result.dice:
                total += int(result.dice.value)

            # کمی فاصله برای جلوگیری از Flood/Timeout
            await asyncio.sleep(0.8)

        except Exception as e:
            logger.error(
                "Roll error game=%s: %s",
                game_id,
                e
            )

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

    # بازی با ربات
    if game["mode"] == "bot":

        if player_id == game["creator_id"]:

            await safe_send_message(
                context.bot,
                game["chat_id"],
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
            )

            bot_total = 0

            for _ in range(game["count"]):
                try:
                    result = await safe_send_dice(
                        context.bot,
                        game["chat_id"],
                        GAME_EMOJI[game["game_type"]]
                    )

                    if result and result.dice:
                        bot_total += int(result.dice.value)

                    await asyncio.sleep(0.8)

                except Exception as e:
                    logger.error(
                        "Bot roll error game=%s: %s",
                        game_id,
                        e
                    )

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

        return

    # بازی دوستانه
    if player_id == game["creator_id"]:

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "✅ پرتاب‌های سازنده تمام شد.\n\n"
            "🎯 حالا بازیکن دوم تمام پرتاب‌های خودش را انجام می‌دهد."
        )

        await play_all(
            context,
            game_id,
            game["opponent_id"]
        )

    else:
        await finish_game(
            context,
            game_id
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

        creator_total = int(game["creator_total"])
        opponent_total = int(game["opponent_total"])
        bet = Decimal(game["bet"])

        if creator_total > opponent_total:
            winner = game["creator_id"]
        elif opponent_total > creator_total:
            winner = game["opponent_id"]
        else:
            winner = None

        db.execute("""
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
        """, (game_id,))

        db.commit()

    # مساوی
    if winner is None:
        try:
            change_balance(
                game["creator_id"],
                bet,
                "game_draw",
                f"Draw {game_id}"
            )

            if game["mode"] == "friends":
                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_draw",
                    f"Draw {game_id}"
                )

        except Exception:
            logger.exception("Draw refund error")

        await safe_send_message(
            context.bot,
            game["chat_id"],
            "🤝 بازی مساوی شد.\n\n"
            f"نتیجه: {creator_total} - {opponent_total}\n"
            f"💰 شرط برگشت داده شد."
        )
        return

    # بازی با ربات
    if game["mode"] == "bot":

        if winner == game["creator_id"]:

            payout = bet * 2

            try:
                change_balance(
                    winner,
                    payout,
                    "game_win",
                    f"Bot game {game_id}"
                )
            except Exception:
                logger.exception("Bot payout error")

            result_text = (
                "🏆 شما برنده شدید!\n"
                f"💰 دریافتی: {money(payout)} TRX"
            )

        else:
            result_text = "🤖 ربات برنده شد."

    # بازی دوستانه
    else:

        payout = bet * 2

        try:
            change_balance(
                winner,
                payout,
                "game_win",
                f"Friend game {game_id}"
            )
        except Exception:
            logger.exception("Friend payout error")

        result_text = (
            f"🏆 برنده: {winner}\n"
            f"💰 جایزه: {money(payout)} TRX"
        )

    await safe_send_message(
        context.bot,
        game["chat_id"],
        "🏁 نتیجه بازی\n\n"
        f"{GAME_NAME[game['game_type']]}\n\n"
        f"👤 سازنده: {creator_total}\n"
        f"👤 بازیکن دوم: {opponent_total}\n\n"
        f"{result_text}"
    )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):
    query = update.callback_query
    user = query.from_user

    try:
        game_id = int(query.data.split("_")[1])
    except Exception:
        return

    with closing(get_db()) as db:
        game = db.execute("""
            SELECT *
            FROM games
            WHERE id = ?
        """, (game_id,)).fetchone()

        if not game:
            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ این بازی دیگر قابل لغو نیست.",
                show_alert=True
            )
            return

        if (
            game["creator_id"] != user.id
            and not is_owner(user.id)
        ):
            await query.answer(
                "❌ اجازه لغو ندارید.",
                show_alert=True
            )
            return

        db.execute("""
            UPDATE games
            SET status = 'cancelled'
            WHERE id = ?
        """, (game_id,))

        db.commit()

    change_balance(
        game["creator_id"],
        Decimal(game["bet"]),
        "game_refund",
        f"Cancel {game_id}"
    )

    await query.answer("لغو شد.")

    await query.edit_message_text(
        "❌ بازی لغو شد.\n"
        f"💰 {money(Decimal(game['bet']))} TRX به سازنده برگشت."
    )


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
        message.text.strip()
    )

    # ---------------------------
    # OWNER COMMANDS
    # ---------------------------

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

        # شارژ
        match = re.fullmatch(
            r"شارژ\s+([0-9]+(?:\.[0-9]+)?)",
            text
        )

        if match:
            amount = parse_decimal(match.group(1))

            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )
                return

            await admin_change(
                update,
                amount,
                "charge"
            )
            return

        # کسر
        match = re.fullmatch(
            r"کسر\s+([0-9]+(?:\.[0-9]+)?)",
            text
        )

        if match:
            amount = parse_decimal(match.group(1))

            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )
                return

            await admin_change(
                update,
                amount,
                "remove"
            )
            return

    # ---------------------------
    # BALANCE
    # ---------------------------

    if text in (
        "موجودی",
        "موجودی من",
        "balance"
    ):
        await balance(update, context)
        return

    # ---------------------------
    # REFERRAL
    # ---------------------------

    if text in (
        "زیر مجموعه",
        "زیرمجموعه",
        "رفرال",
        "referral"
    ):
        await referral(update, context)
        return

    # ---------------------------
    # TRANSFER
    # ---------------------------

    if text.startswith("انتقال "):

        amount_text = text[7:].strip()
        amount = parse_decimal(amount_text)

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

    # ---------------------------
    # GAME
    # ---------------------------

    game = parse_game_command(text)

    if game:

        if not is_bot_enabled():
            await message.reply_text(
                "🔴 ربات خاموش است."
            )
            return

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP
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
            bet
        )
        return


# ============================================================
# MAIN
# ============================================================

def main():

    if not BOT_TOKEN:
        raise RuntimeError(
            "BOT_TOKEN در Environment Variables قرار نگرفته است."
        )

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .get_updates_connect_timeout(30)
        .get_updates_read_timeout(30)
        .get_updates_write_timeout(30)
        .get_updates_pool_timeout(30)
        .build()
    )

    # ---------------------------
    # COMMANDS
    # ---------------------------

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("help", help_command)
    )

    application.add_handler(
        CommandHandler("balance", balance)
    )

    application.add_handler(
        CommandHandler("admin", admin_panel)
    )

    # ---------------------------
    # CALLBACKS
    # ---------------------------

    application.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$"
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^admin_(on|off|charge_help|remove_help|stats)$"
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

    # ---------------------------
    # TEXT
    # ---------------------------

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    # ---------------------------
    # ERROR HANDLER
    # ---------------------------

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
                "Telegram network/timeout error: %s",
                error
            )
            return

        logger.exception(
            "Unhandled bot error:",
            exc_info=error
        )

    application.add_error_handler(
        error_handler
    )

    logger.info("BET_BT started.")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False
    )


if __name__ == "__main__":
    main()
