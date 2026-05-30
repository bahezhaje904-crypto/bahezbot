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
import requests
import shutil
import subprocess
import json

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable.")

DB_PATH = "bot.db"
SHARE_INTERVAL = timedelta(days=5)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
TIKWM_API_URL = "https://www.tikwm.com/api/"

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
        "Send a TikTok, Instagram, Facebook, or YouTube photo/video link.\n"
        "Use /top to see the top inviters."
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
        await update.message.reply_text("Please send a valid photo or video link.")
        return

    url = urls[0]
    if should_share(update.effective_user.id):
        await send_share_prompt(update, context, url)
        return

    await send_download(update.message, url)


def downloaded_files(before_files, prepared_file_path):
    after_files = {
        os.path.join("downloads", file_name)
        for file_name in os.listdir("downloads")
    }
    new_files = after_files - before_files

    if prepared_file_path and os.path.exists(prepared_file_path):
        new_files.add(prepared_file_path)

    return sorted(new_files, key=os.path.getmtime)


async def send_file(message, file_path, preserve_video_scale=False):
    extension = os.path.splitext(file_path)[1].lower()

    with open(file_path, "rb") as media:
        if extension in IMAGE_EXTENSIONS:
            await message.reply_photo(photo=media)
        elif extension in VIDEO_EXTENSIONS:
            if preserve_video_scale:
                await message.reply_document(document=media)
                return

            width, height = video_dimensions(file_path)
            video_options = {"video": media, "supports_streaming": True}

            if width and height:
                video_options["width"] = width
                video_options["height"] = height

            await message.reply_video(**video_options)
        else:
            await message.reply_document(document=media)


def video_dimensions(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    if extension not in VIDEO_EXTENSIONS or not shutil.which("ffprobe"):
        return None, None

    command = [
        "ffprobe",
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=width,height,sample_aspect_ratio:stream_tags=rotate",
        "-of",
        "json",
        file_path,
    ]

    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=20)
        streams = json.loads(result.stdout).get("streams") or []
    except (subprocess.SubprocessError, OSError, json.JSONDecodeError):
        return None, None

    if not streams:
        return None, None

    stream = streams[0]
    width = stream.get("width")
    height = stream.get("height")

    if not width or not height:
        return None, None

    sample_aspect_ratio = stream.get("sample_aspect_ratio")
    if sample_aspect_ratio and sample_aspect_ratio != "1:1":
        try:
            sar_width, sar_height = [int(value) for value in sample_aspect_ratio.split(":", 1)]
            if sar_width > 0 and sar_height > 0:
                width = round(width * sar_width / sar_height)
        except ValueError:
            pass

    rotate = (stream.get("tags") or {}).get("rotate")
    if rotate in {"90", "270", "-90"}:
        width, height = height, width

    return width, height


def is_tiktok_url(url):
    return "tiktok.com" in url or "vt.tiktok.com" in url


def is_facebook_url(url):
    facebook_domains = ("facebook.com", "fb.watch", "fb.com", "m.facebook.com")
    return any(domain in url for domain in facebook_domains)


def is_youtube_url(url):
    youtube_domains = ("youtube.com", "youtu.be", "youtube-nocookie.com")
    return any(domain in url for domain in youtube_domains)


def ydl_options(url):
    options = {
        "format": "best[ext=mp4]/best",
        "outtmpl": "downloads/%(id)s.%(ext)s",
        "quiet": True,
        "noplaylist": True,
        "cookiefile": "cookies.txt",
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/125.0.0.0 Safari/537.36"
            )
        },
    }

    if is_youtube_url(url):
        if shutil.which("ffmpeg"):
            options["format"] = (
                "bestvideo[ext=mp4]+bestaudio[ext=m4a]/"
                "best[ext=mp4][vcodec!=none][acodec!=none]/best"
            )
            options["merge_output_format"] = "mp4"
        else:
            options["format"] = "best[ext=mp4][vcodec!=none][acodec!=none]/best"

        options["extractor_args"] = {
            "youtube": {
                "player_client": ["default", "ios", "web_safari", "mweb"],
            }
        }

    return options


def download_tiktok_photos(url):
    response = requests.get(
        TIKWM_API_URL,
        params={"url": url},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=30,
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data") or {}
    image_urls = data.get("images") or []

    if not image_urls:
        return []

    file_paths = []
    item_id = data.get("id") or "tiktok_photo"

    for index, image_url in enumerate(image_urls, start=1):
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        elif image_url.startswith("/"):
            image_url = f"https://www.tikwm.com{image_url}"

        image_response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=30,
        )
        image_response.raise_for_status()

        file_path = os.path.join("downloads", f"{item_id}_{index}.jpg")
        with open(file_path, "wb") as image_file:
            image_file.write(image_response.content)
        file_paths.append(file_path)

    return file_paths


async def send_download(message, url):
    files_to_send = []
    cleanup_files = []
    preserve_video_scale = is_facebook_url(url)
    await message.reply_text("Downloading...")

    ydl_opts = ydl_options(url)

    try:
        before_files = {
            os.path.join("downloads", file_name)
            for file_name in os.listdir("downloads")
        }

        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            file_path = ydl.prepare_filename(info)

        files_to_send = downloaded_files(before_files, file_path)
        cleanup_files = list(files_to_send)

        if not files_to_send:
            await message.reply_text("I could not find a downloaded photo or video for this link.")
            return

        for file_path in files_to_send:
            await send_file(message, file_path, preserve_video_scale=preserve_video_scale)

    except Exception as e:
        if is_tiktok_url(url):
            try:
                files_to_send = download_tiktok_photos(url)
                cleanup_files = list(files_to_send)
                if files_to_send:
                    for file_path in files_to_send:
                        await send_file(message, file_path)
                    return
            except Exception as fallback_error:
                await message.reply_text(f"Error:\n{fallback_error}")
                return

        await message.reply_text(f"Error:\n{e}")
    finally:
        for file_path in set(cleanup_files):
            if os.path.exists(file_path):
                os.remove(file_path)

        if "file_path" in locals() and os.path.exists(file_path):
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
    await send_download(query.message, url)


init_db()

app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(CommandHandler("top", top))
app.add_handler(CallbackQueryHandler(share_done, pattern="^share_done$"))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))

print("Bot running...")
app.run_polling()
