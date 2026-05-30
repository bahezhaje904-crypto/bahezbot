from datetime import datetime, timedelta
from urllib.parse import quote
import sqlite3

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
import yt_dlp
import os
import re

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable.")

DB_PATH = "bot.db"
SHARE_INTERVAL = timedelta(days=5)

os.makedirs("downloads", exist_ok=True)


def db_connect():
    return sqlite3.connect(DB_PATH)


def init_db():
    with db_connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                referrer_id INTEGER,
                referral_count INTEGER NOT NULL DEFAULT 0,
                last_shared_at TEXT NOT NULL
            )
            """
        )


def register_user(user, referrer_id=None):
    now = datetime.utcnow().isoformat()

    with db_connect() as conn:
        existing = conn.execute(
            "SELECT user_id FROM users WHERE user_id = ?",
            (user.id,),
        ).fetchone()

        if existing:
            conn.execute(
                "UPDATE users SET first_name = ?, username = ? WHERE user_id = ?",
                (user.first_name, user.username, user.id),
            )
            return

        if referrer_id == user.id:
            referrer_id = None

        valid_referrer = None
        if referrer_id:
            valid_referrer = conn.execute(
                "SELECT user_id FROM users WHERE user_id = ?",
                (referrer_id,),
            ).fetchone()

        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username, referrer_id, last_shared_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user.id,
                user.first_name,
                user.username,
                referrer_id if valid_referrer else None,
                now,
            ),
        )

        if valid_referrer:
            conn.execute(
                "UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?",
                (referrer_id,),
            )


def should_share(user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT last_shared_at FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()

    if not row:
        return False

    last_shared_at = datetime.fromisoformat(row[0])
    return datetime.utcnow() - last_shared_at >= SHARE_INTERVAL


def mark_shared(user_id):
    with db_connect() as conn:
        conn.execute(
            "UPDATE users SET last_shared_at = ? WHERE user_id = ?",
            (datetime.utcnow().isoformat(), user_id),
        )


async def send_share_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    bot_username = (await context.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start=ref_{update.effective_user.id}"
    share_text = "Join this video downloader bot:"
    share_url = f"tg://msg_url?url={quote(invite_link)}&text={quote(share_text)}"

    context.user_data["pending_download_url"] = url

    keyboard = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("Share to 1 person", url=share_url)],
            [InlineKeyboardButton("Done after sharing", callback_data="share_done")],
        ]
    )

    await update.message.reply_text(
        "Every 5 days, share your invite link with 1 person to keep downloading.",
        reply_markup=keyboard,
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referrer_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].replace("ref_", "", 1))
        except ValueError:
            referrer_id = None

    register_user(update.effective_user, referrer_id)

    await update.message.reply_text(
        "Send a TikTok, Instagram, Facebook, or YouTube link.\nUse /top to see the top inviters."
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)

    with db_connect() as conn:
        rows = conn.execute(
            """
            SELECT first_name, username, referral_count
            FROM users
            WHERE referral_count > 0
            ORDER BY referral_count DESC, first_name ASC
            LIMIT 10
            """
        ).fetchall()

    if not rows:
        await update.message.reply_text("No invites yet.")
        return

    lines = ["Top inviters:"]
    for index, (first_name, username, referral_count) in enumerate(rows, start=1):
        name = f"@{username}" if username else first_name or "Unknown"
        lines.append(f"{index}. {name} - {referral_count} joined")

    await update.message.reply_text("\n".join(lines))


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)

    text = update.message.text
    urls = re.findall(r"https?://[^\s]+", text)

    if not urls:
        await update.message.reply_text("Please send a valid video link.")
        return

    url = urls[0]
    if should_share(update.effective_user.id):
        await send_share_prompt(update, context, url)
        return

    await send_video(update.message, url)


async def send_video(message, url):
    file_path = None
    await message.reply_text("Downloading...")

    ydl_opts = {
        "format": "best[ext=mp4]/best",
        "outtmpl": "downloads/%(id)s.%(ext)s",
        "quiet": True,
        "noplaylist": True,
        "cookiefile": "cookies.txt",
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        with open(file_path, "rb") as video:
            await message.reply_video(video=video)

    except Exception as e:
        await message.reply_text(f"Error:\n{e}")
    finally:
        if file_path and os.path.exists(file_path):
            os.remove(file_path)


async def share_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    mark_shared(update.effective_user.id)
    url = context.user_data.pop("pending_download_url", None)

    if not url:
        await query.message.reply_text("Thanks. Send your video link again.")
        return

    await query.message.reply_text("Thanks for sharing. Your download will start now.")
    await send_video(query.message, url)


init_db()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("top", top))
app.add_handler(CallbackQueryHandler(share_done, pattern="^share_done$"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))

print("Bot running...")
app.run_polling()
