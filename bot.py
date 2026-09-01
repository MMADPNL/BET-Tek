import os
import re
import sqlite3
import asyncio
import logging
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

# ============================================================
# LOG
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
                creator_id INTEGER NOT NULL,
                opponent_id INTEGER,
                game_type TEXT NOT NULL,
                count INTEGER NOT NULL,
                bet TEXT NOT NULL,
                mode TEXT NOT NULL,
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
    text = normalize_digits(str(text)).strip()
    text = text.replace("٫", ".")
    text = text.replace(",", ".")

    if not re.fullmatch(r"(?:\d+(?:\.\d+)?|\.\d+)", text):
        return None

    try:
        value = Decimal(text)
    except InvalidOperation:
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


# ============================================================
# TELEGRAM SAFE SEND
# ============================================================

async def safe_send_message(
    context,
    chat_id,
    text,
    **kwargs
):
    for attempt in range(3):
        try:
            return await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                **kwargs
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
            logger.warning(
                "Telegram send error: %s",
                e,
            )
            return None

        except Exception as e:
            logger.exception(
                "Unknown send error: %s",
                e,
            )
            return None

    return None


async def safe_send_dice(
    context,
    chat_id,
    emoji
):
    for attempt in range(3):
        try:
            return await context.bot.send_dice(
                chat_id=chat_id,
                emoji=emoji,
            )

        except (TimedOut, NetworkError) as e:
            logger.warning(
                "send_dice timeout attempt %s: %s",
                attempt + 1,
                e,
            )

            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
            else:
                return None

        except TelegramError as e:
            logger.warning(
                "Telegram dice error: %s",
                e,
            )
            return None

        except Exception as e:
            logger.exception(
                "Unknown dice error: %s",
                e,
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
        await update.message.reply_text(
            "🔒 برای استفاده از بازی‌ها ابتدا عضو کانال BET_Tek شوید.",
            reply_markup=keyboard,
        )

    return False


async def membership_callback(update, context):
    query = update.callback_query
    await query.answer()

    if await check_membership(
        query.from_user.id,
        context,
    ):
        await query.edit_message_text(
            "✅ عضویت شما تأیید شد."
        )
    else:
        await query.answer(
            "❌ هنوز عضو کانال نیستید.",
            show_alert=True,
        )


# ============================================================
# MAIN MENU
# ============================================================

def main_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "💰 موجودی",
                callback_data="menu_balance",
            ),
            InlineKeyboardButton(
                "👥 زیر مجموعه",
                callback_data="menu_referral",
            ),
        ],
        [
            InlineKeyboardButton(
                "📚 راهنما",
                callback_data="menu_help",
            ),
        ],
    ])


async def start(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    # --------------------------
    # Referral
    # --------------------------

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

                reward_given = False

                with closing(get_db()) as db:

                    old = db.execute("""
                        SELECT referrer_id
                        FROM users
                        WHERE user_id = ?
                    """, (
                        user.id,
                    )).fetchone()

                    if old and old["referrer_id"] is None:

                        exists = db.execute("""
                            SELECT id
                            FROM referrals
                            WHERE referred_id = ?
                        """, (
                            user.id,
                        )).fetchone()

                        if not exists:

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

                            reward_given = True

                if reward_given:

                    try:
                        change_balance(
                            referrer_id,
                            REFERRAL_REWARD,
                            "referral",
                            f"Referral {user.id}",
                        )
                    except Exception as e:
                        logger.warning(
                            "Referral reward error: %s",
                            e,
                        )

    balance = get_balance(user.id)

    await update.message.reply_text(
        "🤖 BET_BT\n\n"
        f"💰 موجودی: {money(balance)} TRX\n\n"
        "از دکمه‌های زیر استفاده کنید:",
        reply_markup=main_keyboard(),
    )


# ============================================================
# BALANCE
# ============================================================

async def send_balance(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    balance = get_balance(user.id)

    await update.message.reply_text(
        f"💰 موجودی شما: {money(balance)} TRX"
    )


async def balance_callback(update, context):
    query = update.callback_query
    await query.answer()

    ensure_user(query.from_user)

    balance = get_balance(
        query.from_user.id
    )

    await query.message.reply_text(
        f"💰 موجودی شما: {money(balance)} TRX"
    )


# ============================================================
# REFERRAL
# ============================================================

async def send_referral(update, context):
    user = update.effective_user

    if not user:
        return

    ensure_user(user)

    try:
        bot = await context.bot.get_me()
    except Exception:
        await update.message.reply_text(
            "❌ دریافت لینک دعوت ناموفق بود."
        )
        return

    link = (
        f"https://t.me/{bot.username}"
        f"?start=ref_{user.id}"
    )

    with closing(get_db()) as db:
        row = db.execute("""
            SELECT COUNT(*) AS count
            FROM referrals
            WHERE referrer_id = ?
        """, (
            user.id,
        )).fetchone()

    count = row["count"]

    await update.message.reply_text(
        "👥 زیر مجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {count}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} TRX"
    )


async def referral_callback(update, context):
    query = update.callback_query
    await query.answer()

    user = query.from_user

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
            SELECT COUNT(*) AS count
            FROM referrals
            WHERE referrer_id = ?
        """, (
            user.id,
        )).fetchone()

    await query.message.reply_text(
        "👥 زیر مجموعه\n\n"
        f"🔗 لینک دعوت:\n{link}\n\n"
        f"👤 تعداد: {row['count']}\n"
        f"🎁 پاداش هر نفر: {money(REFERRAL_REWARD)} TRX"
    )


# ============================================================
# TRANSFER
# ============================================================

async def do_transfer(update, context, amount):

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
            "❌ کاربر مقصد پیدا نشد."
        )
        return

    if target.is_bot:
        await message.reply_text(
            "❌ انتقال به ربات مجاز نیست."
        )
        return

    if target.id == user.id:
        await message.reply_text(
            "❌ نمی‌توانید به خودتان انتقال دهید."
        )
        return

    if amount < MIN_GAME_BET:
        await message.reply_text(
            f"❌ حداقل مبلغ انتقال "
            f"{money(MIN_GAME_BET)} TRX است."
        )
        return

    ensure_user(user)
    ensure_user(target)

    # انتقال اتمی بین دو حساب
    first_id = min(user.id, target.id)
    second_id = max(user.id, target.id)

    with closing(get_db()) as db:

        try:
            db.execute("BEGIN IMMEDIATE")

            rows = {}

            for uid in (first_id, second_id):
                row = db.execute("""
                    SELECT balance
                    FROM users
                    WHERE user_id = ?
                """, (uid,)).fetchone()

                if not row:
                    raise ValueError("user_not_found")

                rows[uid] = Decimal(row["balance"])

            sender_balance = rows[user.id]

            if sender_balance < amount:
                raise ValueError("insufficient")

            new_sender = sender_balance - amount
            new_target = rows[target.id] + amount

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_sender),
                user.id,
            ))

            db.execute("""
                UPDATE users
                SET balance = ?
                WHERE user_id = ?
            """, (
                str(new_target),
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

            db.rollback()

            if str(e) == "insufficient":
                await message.reply_text(
                    "❌ موجودی شما کافی نیست."
                )
            else:
                await message.reply_text(
                    "❌ انتقال انجام نشد."
                )

            return

        except Exception as e:

            db.rollback()

            logger.exception(
                "Transfer error: %s",
                e,
            )

            await message.reply_text(
                "❌ خطا در انتقال."
            )

            return

    await message.reply_text(
        "✅ انتقال انجام شد.\n\n"
        f"💸 مبلغ: {money(amount)} TRX\n"
        f"👤 گیرنده: {target.first_name or target.id}\n"
        f"💰 موجودی شما: {money(new_sender)} TRX"
    )


# ============================================================
# ADMIN
# ============================================================

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
            ),
        ],
    ])

    await update.message.reply_text(
        "👑 پنل مدیریت BET_BT\n\n"
        f"وضعیت: "
        f"{'🟢 روشن' if enabled else '🔴 خاموش'}\n\n"
        "برای شارژ یا کسر در گپ، روی پیام کاربر Reply کنید:\n\n"
        "شارژ 100\n"
        "کسر 100",
        reply_markup=keyboard,
    )


async def admin_callback(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user

    if not is_owner(user.id):
        await query.answer(
            "❌ دسترسی ندارید.",
            show_alert=True,
        )
        return

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
                    SUM(CAST(balance AS REAL)),
                    0
                ) AS s
                FROM users
            """).fetchone()["s"]

            games = db.execute("""
                SELECT COUNT(*) AS c
                FROM games
            """).fetchone()["c"]

        await query.edit_message_text(
            "📊 آمار\n\n"
            f"👤 کاربران: {users}\n"
            f"💰 مجموع موجودی: {total:.2f} TRX\n"
            f"🎮 تعداد بازی‌ها: {games}"
        )


# ============================================================
# ADMIN BALANCE
# ============================================================

async def admin_change_balance(
    update,
    context,
    amount,
    operation
):

    owner = update.effective_user
    message = update.message

    if not is_owner(owner.id):
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

    if target.is_bot:
        await message.reply_text(
            "❌ برای ربات قابل انجام نیست."
        )
        return

    ensure_user(target)

    try:

        if operation == "charge":

            change_balance(
                target.id,
                amount,
                "admin_charge",
                f"Owner {owner.id}",
            )

            title = "شارژ"

        else:

            change_balance(
                target.id,
                -amount,
                "admin_remove",
                f"Owner {owner.id}",
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
        f"✅ {title} انجام شد.\n\n"
        f"👤 کاربر: {target.first_name or target.id}\n"
        f"💰 مبلغ: {money(amount)} TRX\n"
        f"💳 موجودی جدید: "
        f"{money(get_balance(target.id))} TRX"
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
        text,
    )

    if not match:
        return None

    count = int(match.group(1))
    game_name = match.group(2).lower()
    bet = parse_decimal(match.group(3))

    if count < 1 or count > MAX_GAME_COUNT:
        return None

    if bet is None:
        return None

    game_type = GAME_MAP.get(game_name)

    if not game_type:
        return None

    return count, game_type, bet


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

    if not is_bot_enabled():
        await update.message.reply_text(
            "🔴 ربات در حال حاضر خاموش است."
        )
        return

    if not await require_membership(
        update,
        context,
    ):
        return

    ensure_user(user)

    if bet < MIN_GAME_BET:
        await update.message.reply_text(
            f"❌ حداقل شرط "
            f"{money(MIN_GAME_BET)} TRX است."
        )
        return

    # کسر شرط سازنده قبل از ساخت بازی
    try:

        change_balance(
            user.id,
            -bet,
            "game_bet",
            f"Create game {game_type}",
        )

    except ValueError:

        await update.message.reply_text(
            "❌ موجودی شما کافی نیست."
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
                callback_data=f"join_{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "🤖 بازی با ربات",
                callback_data=f"bot_{game_id}",
            ),
        ],
        [
            InlineKeyboardButton(
                "❌ لغو",
                callback_data=f"cancel_{game_id}",
            ),
        ],
    ])

    await update.message.reply_text(
        "🎮 بازی جدید\n\n"
        f"{GAME_NAME[game_type]}\n"
        f"🔢 تعداد بازی: {count}\n"
        f"💰 شرط: {money(bet)} TRX\n"
        f"👤 سازنده: {user.first_name or user.id}\n\n"
        "یکی از گزینه‌ها را انتخاب کنید.",
        reply_markup=keyboard,
    )


# ============================================================
# JOIN FRIEND GAME
# ============================================================

async def join_game(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user

    if not await check_membership(
        user.id,
        context,
    ):
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
            """, (
                user.id,
            )).fetchone()

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
                VALUES (?, ?, ?, ?)
            """, (
                user.id,
                "game_bet",
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

        except Exception as e:

            db.rollback()

            logger.exception(
                "Join game error: %s",
                e,
            )

            await query.answer(
                "❌ خطا در شروع بازی.",
                show_alert=True,
            )
            return

    await query.edit_message_text(
        "🎮 بازی شروع شد!\n\n"
        f"{GAME_NAME[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط هر نفر: {money(bet)} TRX\n\n"
        "🎯 ابتدا سازنده تمام پرتاب‌های خودش را انجام می‌دهد."
    )

    await run_player_turn(
        context,
        game_id,
        game["creator_id"],
    )


# ============================================================
# BOT GAME
# ============================================================

async def bot_game(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user

    if not await check_membership(
        user.id,
        context,
    ):
        await query.answer(
            "❌ ابتدا عضو BET_Tek شوید.",
            show_alert=True,
        )
        return

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
        """, (
            game_id,
        )).fetchone()

        if not game:
            await query.answer(
                "❌ بازی پیدا نشد.",
                show_alert=True,
            )
            return

        if game["creator_id"] != user.id:
            await query.answer(
                "❌ فقط سازنده می‌تواند این گزینه را بزند.",
                show_alert=True,
            )
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ بازی دیگر فعال نیست.",
                show_alert=True,
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
        f"{GAME_NAME[game['game_type']]}\n"
        f"🔢 تعداد: {game['count']}\n"
        f"💰 شرط: {money(Decimal(game['bet']))} TRX\n\n"
        "🎯 اول شما تمام پرتاب‌های خودتان را انجام می‌دهید.\n"
        "🤖 بعد از تمام شدن پرتاب‌های شما، ربات شروع می‌کند."
    )

    await run_player_turn(
        context,
        game_id,
        user.id,
    )


# ============================================================
# CANCEL GAME
# ============================================================

async def cancel_game(update, context):

    query = update.callback_query
    await query.answer()

    user = query.from_user

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
        """, (
            game_id,
        )).fetchone()

        if not game:
            return

        if game["status"] != "waiting":
            await query.answer(
                "❌ بازی دیگر قابل لغو نیست.",
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
            f"Cancel game {game_id}",
        )
    except Exception as e:
        logger.warning(
            "Refund error: %s",
            e,
        )

    await query.edit_message_text(
        "❌ بازی لغو شد.\n\n"
        f"💰 {money(bet)} TRX به سازنده برگشت داده شد."
    )


# ============================================================
# ROLL
# ============================================================

async def send_roll(context, chat_id, game_type):

    msg = await safe_send_dice(
        context,
        chat_id,
        GAME_EMOJI[game_type],
    )

    if msg is None:
        return None

    if not msg.dice:
        return None

    return msg.dice.value


# ============================================================
# PLAYER TURN
# ============================================================

async def run_player_turn(
    context,
    game_id,
    player_id,
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

    total = 0
    successful_rolls = 0

    # ========================================================
    # مهم:
    # اینجا اول تمام بازی‌های بازیکن انجام می‌شود.
    # بعد از تمام شدن، نوبت نفر بعد/ربات است.
    # ========================================================

    for number in range(game["count"]):

        value = await send_roll(
            context,
            game["chat_id"],
            game["game_type"],
        )

        if value is None:
            continue

        total += value
        successful_rolls += 1

        await asyncio.sleep(1)

    # اگر هیچ پرتابی موفق نبود، بازی را متوقف نکن
    # و نتیجه فعلی را ثبت کن.
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
        """, (
            game_id,
        )).fetchone()

    # ========================================================
    # CREATOR FINISHED
    # ========================================================

    if player_id == game["creator_id"]:

        if game["mode"] == "bot":

            await safe_send_message(
                context,
                game["chat_id"],
                "✅ تمام پرتاب‌های شما انجام شد.\n\n"
                "🤖 حالا ربات تمام پرتاب‌های خودش را انجام می‌دهد..."
            )

            bot_total = 0

            for number in range(game["count"]):

                value = await send_roll(
                    context,
                    game["chat_id"],
                    game["game_type"],
                )

                if value is not None:
                    bot_total += value

                await asyncio.sleep(1)

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
                game_id,
            )

        else:

            await safe_send_message(
                context,
                game["chat_id"],
                "✅ پرتاب‌های سازنده کامل شد.\n\n"
                "🎯 حالا بازیکن دوم تمام پرتاب‌های خودش را انجام می‌دهد."
            )

            await run_player_turn(
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
# FINISH GAME
# ============================================================

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

        if creator_total > opponent_total:
            winner_id = game["creator_id"]

        elif opponent_total > creator_total:
            winner_id = game["opponent_id"]

        else:
            winner_id = None

        db.execute("""
            UPDATE games
            SET status = 'finished'
            WHERE id = ?
        """, (
            game_id,
        ))

        db.commit()

    # ========================================================
    # DRAW
    # ========================================================

    if winner_id is None:

        try:
            change_balance(
                game["creator_id"],
                bet,
                "game_draw_refund",
                f"Draw {game_id}",
            )
        except Exception:
            pass

        if game["mode"] != "bot":

            try:
                change_balance(
                    game["opponent_id"],
                    bet,
                    "game_draw_refund",
                    f"Draw {game_id}",
                )
            except Exception:
                pass

        await safe_send_message(
            context,
            game["chat_id"],
            "🤝 بازی مساوی شد.\n\n"
            f"{GAME_NAME[game['game_type']]}\n"
            f"🎲 نتیجه: {creator_total:g} - {opponent_total:g}\n\n"
            "💰 مبلغ شرط به بازیکنان برگشت داده شد."
        )

        return

    # ========================================================
    # BOT GAME
    # ========================================================

    if game["mode"] == "bot":

        if winner_id == game["creator_id"]:

            # کل شرط مجازی دو طرف به کاربر برمی‌گردد.
            payout = bet * 2

            try:
                change_balance(
                    winner_id,
                    payout,
                    "game_win",
                    f"Bot game win {game_id}",
                )
            except Exception as e:
                logger.warning(
                    "Bot payout error: %s",
                    e,
                )

            result_text = (
                "🏆 شما برنده شدید!\n"
                f"💰 جایزه: {money(payout)} TRX"
            )

        else:

            result_text = (
                "🤖 ربات برنده شد.\n"
                f"💰 شرط شما: {money(bet)} TRX"
            )

    # ========================================================
    # FRIEND GAME
    # ========================================================

    else:

        payout = bet * 2

        try:
            change_balance(
                winner_id,
                payout,
                "game_win",
                f"Friend game win {game_id}",
            )
        except Exception as e:
            logger.warning(
                "Friend payout error: %s",
                e,
            )

        result_text = (
            f"🏆 برنده: {winner_id}\n"
            f"💰 جایزه: {money(payout)} TRX"
        )

    await safe_send_message(
        context,
        game["chat_id"],
        "🏁 نتیجه بازی\n\n"
        f"{GAME_NAME[game['game_type']]}\n\n"
        f"👤 سازنده: {creator_total:g}\n"
        f"👤 بازیکن دوم: {opponent_total:g}\n\n"
        f"{result_text}"
    )


# ============================================================
# HELP
# ============================================================

async def help_command(update, context):

    await update.message.reply_text(
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n"
        "🔄 انتقال ۰.۱\n\n"
        "🎮 بازی‌ها در گپ:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1\n\n"
        "🔢 تعداد بازی از 1 تا 20 است.\n"
        "🎯 در بازی با ربات ابتدا تمام پرتاب‌های کاربر انجام می‌شود و بعد ربات بازی می‌کند.\n"
        "👥 در بازی دوستان نیز ابتدا سازنده تمام پرتاب‌های خودش را انجام می‌دهد."
    )


async def help_callback(update, context):

    query = update.callback_query
    await query.answer()

    await query.message.reply_text(
        "📚 راهنمای BET_BT\n\n"
        "💰 موجودی\n"
        "👥 زیر مجموعه\n"
        "🔄 انتقال 0.1\n\n"
        "🎮 بازی:\n"
        "1 تاس 0.1\n"
        "1 دارت 0.1\n"
        "1 بولینگ 0.1\n"
        "1 بسکتبال 0.1"
    )


# ============================================================
# STRICT TEXT HANDLER / ANTI COMMAND
# ============================================================

async def text_handler(update, context):

    message = update.message
    user = update.effective_user

    if not message or not user:
        return

    if not message.text:
        return

    ensure_user(user)

    raw = message.text.strip()
    text = normalize_digits(raw).strip()

    # ========================================================
    # OWNER COMMANDS
    # ========================================================

    if is_owner(user.id):

        # روشن
        if text == "روشن":
            set_bot_enabled(True)

            await message.reply_text(
                "🟢 ربات روشن شد."
            )
            return

        # خاموش
        if text == "خاموش":
            set_bot_enabled(False)

            await message.reply_text(
                "🔴 ربات خاموش شد."
            )
            return

        # ----------------------------------------------------
        # SHARGE
        # دقیقاً: شارژ + عدد
        # ----------------------------------------------------

        match = re.fullmatch(
            r"شارژ\s+([0-9]+(?:\.[0-9]+)?)",
            text,
        )

        if match:

            amount = parse_decimal(
                match.group(1)
            )

            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )
                return

            await admin_change_balance(
                update,
                context,
                amount,
                "charge",
            )

            return

        # ----------------------------------------------------
        # REMOVE
        # دقیقاً: کسر + عدد
        # ----------------------------------------------------

        match = re.fullmatch(
            r"کسر\s+([0-9]+(?:\.[0-9]+)?)",
            text,
        )

        if match:

            amount = parse_decimal(
                match.group(1)
            )

            if amount is None:
                await message.reply_text(
                    "❌ مبلغ صحیح نیست."
                )
                return

            await admin_change_balance(
                update,
                context,
                amount,
                "remove",
            )

            return

    # ========================================================
    # BALANCE
    # ========================================================

    if text in (
        "موجودی",
        "موجودی من",
        "balance",
    ):

        await send_balance(
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

        await send_referral(
            update,
            context,
        )
        return

    # ========================================================
    # TRANSFER
    # دقیقاً انتقال + عدد
    # ========================================================

    match = re.fullmatch(
        r"انتقال\s+([0-9]+(?:\.[0-9]+)?)",
        text,
    )

    if match:

        amount = parse_decimal(
            match.group(1)
        )

        if amount is None:
            await message.reply_text(
                "❌ فرمت صحیح:\n"
                "انتقال 0.1\n"
                "انتقال ۰.۱"
            )
            return

        await do_transfer(
            update,
            context,
            amount,
        )

        return

    # ========================================================
    # GAME
    # ========================================================

    game = parse_game_command(text)

    if game:

        count, game_type, bet = game

        if message.chat.type not in (
            ChatType.GROUP,
            ChatType.SUPERGROUP,
        ):

            await message.reply_text(
                "❌ بازی‌ها فقط داخل گپ قابل اجرا هستند."
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

    # ========================================================
    # ANTI COMMAND
    # ========================================================
    # هر چیز دیگری هیچ عملیات مالی یا بازی انجام نمی‌دهد.
    # یعنی موجودی با دستور ناشناس تغییر نمی‌کند.
    # ========================================================

    return


# ============================================================
# CALLBACK ROUTER
# ============================================================

async def callback_router(update, context):

    query = update.callback_query

    if not query:
        return

    data = query.data or ""

    if data == "check_membership":
        await membership_callback(
            update,
            context,
        )
        return

    if data == "menu_balance":
        await balance_callback(
            update,
            context,
        )
        return

    if data == "menu_referral":
        await referral_callback(
            update,
            context,
        )
        return

    if data == "menu_help":
        await help_callback(
            update,
            context,
        )
        return

    if data.startswith("admin_"):
        await admin_callback(
            update,
            context,
        )
        return

    if data.startswith("join_"):
        await join_game(
            update,
            context,
        )
        return

    if data.startswith("bot_"):
        await bot_game(
            update,
            context,
        )
        return

    if data.startswith("cancel_"):
        await cancel_game(
            update,
            context,
        )
        return


# ============================================================
# ERROR HANDLER
# ============================================================

async def error_handler(update, context):

    error = context.error

    if isinstance(error, TimedOut):
        logger.warning(
            "Telegram request timed out."
        )
        return

    if isinstance(error, NetworkError):
        logger.warning(
            "Telegram network error: %s",
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
            "BOT_TOKEN در Environment/Secrets قرار داده نشده است."
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
            send_balance,
        )
    )

    application.add_handler(
        CommandHandler(
            "admin",
            admin_panel,
        )
    )

    # ========================================================
    # CALLBACKS
    # ========================================================

    application.add_handler(
        CallbackQueryHandler(
            callback_router,
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

    application.add_error_handler(
        error_handler
    )

    logger.info(
        "BET_BT started."
    )

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
