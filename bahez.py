import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote

import requests
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatMemberStatus
from telegram.error import BadRequest, Conflict, Forbidden, NetworkError, TimedOut
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

TOKEN = os.getenv("TOKEN")
if not TOKEN:
    raise RuntimeError("Missing TOKEN environment variable. Add TOKEN in Railway Variables.")

DB_PATH = "bot.db"
DOWNLOAD_DIR = "downloads"
SHARE_INTERVAL = timedelta(days=5)
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
AUDIO_EXTENSIONS = {".mp3", ".m4a", ".opus", ".ogg", ".wav"}
TIKWM_API_URL = "https://www.tikwm.com/api/"
DEFAULT_COOKIES_FILE = "cookies.txt"
RUNTIME_COOKIES_FILE = os.path.join(DOWNLOAD_DIR, "youtube_cookies.txt")
MAX_TELEGRAM_SIZE = 49 * 1024 * 1024
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "20"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME", "").strip()  # example: @yourchannel
ADMIN_IDS = {int(x.strip()) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}

os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def prepare_cookies_file():
    cookies_text = os.getenv("YOUTUBE_COOKIES_TEXT")
    cookies_b64 = os.getenv("YOUTUBE_COOKIES_B64")

    if cookies_b64:
        try:
            cookies_text = base64.b64decode(cookies_b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            cookies_text = None

    if cookies_text:
        with open(RUNTIME_COOKIES_FILE, "w", encoding="utf-8") as cookies_file:
            cookies_file.write(cookies_text)
        return RUNTIME_COOKIES_FILE

    custom_cookies_file = os.getenv("COOKIES_FILE")
    if custom_cookies_file:
        return custom_cookies_file

    if os.path.exists(DEFAULT_COOKIES_FILE):
        return DEFAULT_COOKIES_FILE

    return None


COOKIES_FILE = prepare_cookies_file()


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
                last_shared_at TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                last_active_at TEXT NOT NULL,
                is_banned INTEGER NOT NULL DEFAULT 0,
                is_vip INTEGER NOT NULL DEFAULT 0,
                downloads_total INTEGER NOT NULL DEFAULT 0,
                downloads_today INTEGER NOT NULL DEFAULT 0,
                downloads_date TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                mode TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        # Upgrade old databases safely.
        columns = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        today = datetime.now(UTC).date().isoformat()
        defaults = {
            "joined_at": "TEXT NOT NULL DEFAULT ''",
            "last_active_at": "TEXT NOT NULL DEFAULT ''",
            "is_banned": "INTEGER NOT NULL DEFAULT 0",
            "is_vip": "INTEGER NOT NULL DEFAULT 0",
            "downloads_total": "INTEGER NOT NULL DEFAULT 0",
            "downloads_today": "INTEGER NOT NULL DEFAULT 0",
            "downloads_date": f"TEXT NOT NULL DEFAULT '{today}'",
        }
        for column, definition in defaults.items():
            if column not in columns:
                conn.execute(f"ALTER TABLE users ADD COLUMN {column} {definition}")


def utc_now():
    return datetime.now(UTC)


def today_str():
    return utc_now().date().isoformat()


def is_admin(user_id):
    return user_id in ADMIN_IDS


def register_user(user, referrer_id=None):
    now = utc_now().isoformat()
    today = today_str()
    with db_connect() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone()

        if existing:
            conn.execute(
                "UPDATE users SET first_name = ?, username = ?, last_active_at = ? WHERE user_id = ?",
                (user.first_name, user.username, now, user.id),
            )
            return

        if referrer_id == user.id:
            referrer_id = None

        valid_referrer = None
        if referrer_id:
            valid_referrer = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)).fetchone()

        conn.execute(
            """
            INSERT INTO users (
                user_id, first_name, username, referrer_id, referral_count,
                last_shared_at, joined_at, last_active_at, is_banned, is_vip,
                downloads_total, downloads_today, downloads_date
            ) VALUES (?, ?, ?, ?, 0, ?, ?, ?, 0, 0, 0, 0, ?)
            """,
            (user.id, user.first_name, user.username, referrer_id if valid_referrer else None, now, now, now, today),
        )

        if valid_referrer:
            conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (referrer_id,))


def get_user_row(user_id):
    with db_connect() as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()


def is_banned(user_id):
    row = get_user_row(user_id)
    return bool(row and row["is_banned"])


def reset_daily_if_needed(user_id):
    today = today_str()
    with db_connect() as conn:
        row = conn.execute("SELECT downloads_date FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row and row[0] != today:
            conn.execute(
                "UPDATE users SET downloads_today = 0, downloads_date = ? WHERE user_id = ?",
                (today, user_id),
            )


def can_download(user_id):
    if is_admin(user_id):
        return True, ""
    reset_daily_if_needed(user_id)
    row = get_user_row(user_id)
    if not row:
        return False, "Please use /start first."
    if row["is_banned"]:
        return False, "Your account is banned."
    if row["is_vip"]:
        return True, ""
    if row["downloads_today"] >= FREE_DAILY_LIMIT:
        return False, f"Daily limit reached ({FREE_DAILY_LIMIT}). Try again tomorrow or ask admin for VIP."
    return True, ""


def log_download(user_id, platform, mode):
    now = utc_now().isoformat()
    reset_daily_if_needed(user_id)
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO downloads (user_id, platform, mode, created_at) VALUES (?, ?, ?, ?)",
            (user_id, platform, mode, now),
        )
        conn.execute(
            """
            UPDATE users
            SET downloads_total = downloads_total + 1,
                downloads_today = downloads_today + 1,
                last_active_at = ?
            WHERE user_id = ?
            """,
            (now, user_id),
        )


def should_share(user_id):
    row = get_user_row(user_id)
    if not row or row["is_vip"] or is_admin(user_id):
        return False
    last_shared_at = datetime.fromisoformat(row["last_shared_at"])
    if last_shared_at.tzinfo is None:
        last_shared_at = last_shared_at.replace(tzinfo=UTC)
    return utc_now() - last_shared_at >= SHARE_INTERVAL


def mark_shared(user_id):
    with db_connect() as conn:
        conn.execute("UPDATE users SET last_shared_at = ? WHERE user_id = ?", (utc_now().isoformat(), user_id))


async def check_force_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not CHANNEL_USERNAME or is_admin(update.effective_user.id):
        return True
    try:
        member = await context.bot.get_chat_member(CHANNEL_USERNAME, update.effective_user.id)
        if member.status in {ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER}:
            return True
    except Exception:
        return True  # Do not block downloads if Telegram cannot check the channel.

    keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Join Channel", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")]])
    await update.message.reply_text("Join our channel first, then send the link again.", reply_markup=keyboard)
    return False


def extract_first_url(text):
    urls = re.findall(r"https?://[^\s]+", text or "")
    return urls[0].strip() if urls else None


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referrer_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].replace("ref_", "", 1))
        except ValueError:
            referrer_id = None
    register_user(update.effective_user, referrer_id)
    await update.message.reply_text(
        "👋 Welcome to Bahez Video Downloader\n\n"
        "Send a TikTok, Instagram, Facebook, or YouTube link.\n"
        "Commands:\n"
        "/mp3 <link> - download audio\n"
        "/top - top inviters\n"
        "/profile - your account"
    )


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    row = get_user_row(update.effective_user.id)
    await update.message.reply_text(
        "👤 Your Profile\n\n"
        f"VIP: {'✅ Yes' if row['is_vip'] else '❌ No'}\n"
        f"Downloads today: {row['downloads_today']}/{FREE_DAILY_LIMIT}\n"
        f"Total downloads: {row['downloads_total']}\n"
        f"Invites: {row['referral_count']}"
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    with db_connect() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        rows = conn.execute(
            """
            SELECT first_name, username, referral_count
            FROM users
            WHERE referral_count > 0
            ORDER BY referral_count DESC, first_name ASC
            LIMIT 10
            """
        ).fetchall()

    lines = [f"👥 Total subscribers: {total_users}", "", "🏆 Top inviters:"]
    if not rows:
        lines.append("No invites yet.")
    else:
        for index, (first_name, username, referral_count) in enumerate(rows, start=1):
            name = f"@{username}" if username else first_name or "Unknown"
            lines.append(f"{index}. {name} - {referral_count} joined")
    await update.message.reply_text("\n".join(lines))


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    today = today_str()
    week_ago = (utc_now() - timedelta(days=7)).isoformat()
    month_ago = (utc_now() - timedelta(days=30)).isoformat()
    with db_connect() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        vip_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip = 1").fetchone()[0]
        banned_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
        new_today = conn.execute("SELECT COUNT(*) FROM users WHERE substr(joined_at, 1, 10) = ?", (today,)).fetchone()[0]
        downloads_today = conn.execute("SELECT COUNT(*) FROM downloads WHERE substr(created_at, 1, 10) = ?", (today,)).fetchone()[0]
        downloads_week = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at >= ?", (week_ago,)).fetchone()[0]
        downloads_month = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at >= ?", (month_ago,)).fetchone()[0]
        platform_rows = conn.execute(
            "SELECT platform, COUNT(*) FROM downloads GROUP BY platform ORDER BY COUNT(*) DESC LIMIT 5"
        ).fetchall()

    platforms = "\n".join(f"• {p}: {c}" for p, c in platform_rows) or "No downloads yet."
    await update.message.reply_text(
        "📊 Admin Stats\n\n"
        f"👥 Subscribers: {total_users}\n"
        f"🆕 New today: {new_today}\n"
        f"👑 VIP: {vip_users}\n"
        f"🚫 Banned: {banned_users}\n\n"
        f"📥 Downloads today: {downloads_today}\n"
        f"📥 Downloads 7 days: {downloads_week}\n"
        f"📥 Downloads 30 days: {downloads_month}\n\n"
        f"🔥 Platforms:\n{platforms}"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if not is_admin(update.effective_user.id):
        return
    await update.message.reply_text(
        "🛠 Admin Panel\n\n"
        "/stats - bot statistics\n"
        "/broadcast Your message - send text to all users\n"
        "/ban USER_ID - ban user\n"
        "/unban USER_ID - unban user\n"
        "/vip USER_ID - add VIP\n"
        "/unvip USER_ID - remove VIP\n"
        "/users - recent users\n"
        "/backup - send database backup"
    )


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    with db_connect() as conn:
        rows = conn.execute(
            "SELECT user_id, first_name, username, downloads_total FROM users ORDER BY joined_at DESC LIMIT 20"
        ).fetchall()
    lines = ["👥 Recent users:"]
    for user_id, first_name, username, downloads_total in rows:
        name = f"@{username}" if username else first_name or "Unknown"
        lines.append(f"{user_id} | {name} | downloads: {downloads_total}")
    await update.message.reply_text("\n".join(lines))


async def backup(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    if not os.path.exists(DB_PATH):
        await update.message.reply_text("No database yet.")
        return
    with open(DB_PATH, "rb") as db_file:
        await update.message.reply_document(document=db_file, filename="bot_backup.db")


async def set_flag(update: Update, context: ContextTypes.DEFAULT_TYPE, column: str, value: int, label: str):
    if not is_admin(update.effective_user.id):
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Send user ID. Example: /vip 123456789")
        return
    user_id = int(context.args[0])
    with db_connect() as conn:
        conn.execute(f"UPDATE users SET {column} = ? WHERE user_id = ?", (value, user_id))
    await update.message.reply_text(f"Done: {label} {user_id}")


async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_flag(update, context, "is_banned", 1, "banned")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_flag(update, context, "is_banned", 0, "unbanned")


async def vip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_flag(update, context, "is_vip", 1, "VIP added")


async def unvip(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await set_flag(update, context, "is_vip", 0, "VIP removed")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Use: /broadcast Your message here")
        return

    with db_connect() as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()]

    sent = 0
    failed = 0
    status_msg = await update.message.reply_text(f"Broadcast started to {len(user_ids)} users...")
    for user_id in user_ids:
        try:
            await context.bot.send_message(chat_id=user_id, text=text)
            sent += 1
        except (Forbidden, BadRequest, NetworkError, TimedOut):
            failed += 1
    await status_msg.edit_text(f"Broadcast finished.\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def send_share_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str, mode: str):
    bot_username = (await context.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start=ref_{update.effective_user.id}"
    share_text = "Join this video downloader bot:"
    share_url = f"tg://msg_url?url={quote(invite_link)}&text={quote(share_text)}"
    context.user_data["pending_download_url"] = url
    context.user_data["pending_download_mode"] = mode
    keyboard = InlineKeyboardMarkup(
        [[InlineKeyboardButton("Share to 1 person", url=share_url)], [InlineKeyboardButton("Done after sharing", callback_data="share_done")]]
    )
    await update.message.reply_text("Every 5 days, share your invite link with 1 person to keep downloading.", reply_markup=keyboard)


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        await update.message.reply_text("Your account is banned.")
        return
    if not await check_force_join(update, context):
        return

    url = extract_first_url(update.message.text)
    if not url:
        await update.message.reply_text("Please send a valid photo or video link.")
        return

    if should_share(update.effective_user.id):
        await send_share_prompt(update, context, url, "video")
        return

    ok, reason = can_download(update.effective_user.id)
    if not ok:
        await update.message.reply_text(reason)
        return
    await send_download(update.message, update.effective_user.id, url, mode="video")


async def mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        await update.message.reply_text("Your account is banned.")
        return
    if not await check_force_join(update, context):
        return
    url = extract_first_url(" ".join(context.args)) or extract_first_url(update.message.text)
    if not url:
        await update.message.reply_text("Use: /mp3 https://youtube.com/...")
        return
    ok, reason = can_download(update.effective_user.id)
    if not ok:
        await update.message.reply_text(reason)
        return
    await send_download(update.message, update.effective_user.id, url, mode="mp3")


def is_tiktok_url(url):
    return any(domain in url.lower() for domain in ("tiktok.com", "vt.tiktok.com", "vm.tiktok.com"))


def is_facebook_url(url):
    return any(domain in url.lower() for domain in ("facebook.com", "fb.watch", "fb.com", "m.facebook.com"))


def is_youtube_url(url):
    return any(domain in url.lower() for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def platform_name(url):
    url_l = url.lower()
    if is_tiktok_url(url_l):
        return "TikTok"
    if "instagram.com" in url_l:
        return "Instagram"
    if is_facebook_url(url_l):
        return "Facebook"
    if is_youtube_url(url_l):
        return "YouTube"
    return "Other"


def youtube_cookie_error(error):
    error_text = str(error).lower()
    return any(
        phrase in error_text
        for phrase in (
            "sign in to confirm",
            "not a bot",
            "use --cookies",
            "cookies-from-browser",
            "confirm you’re not a bot",
            "confirm you're not a bot",
        )
    )


def youtube_cookie_message():
    return (
        "YouTube is asking the server to sign in before downloading this video.\n\n"
        "Fix: export fresh YouTube cookies from your browser and set them in YOUTUBE_COOKIES_TEXT or YOUTUBE_COOKIES_B64."
    )


def downloaded_files(before_files, prepared_file_path):
    after_files = {os.path.join(DOWNLOAD_DIR, file_name) for file_name in os.listdir(DOWNLOAD_DIR)}
    new_files = after_files - before_files
    if prepared_file_path and os.path.exists(prepared_file_path):
        new_files.add(prepared_file_path)
    return sorted(new_files, key=os.path.getmtime)


def video_dimensions(file_path):
    extension = os.path.splitext(file_path)[1].lower()
    if extension not in VIDEO_EXTENSIONS or not shutil.which("ffprobe"):
        return None, None
    command = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,sample_aspect_ratio:stream_tags=rotate",
        "-of", "json", file_path,
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


async def send_file(message, file_path, preserve_video_scale=False):
    if not os.path.exists(file_path):
        return False
    if os.path.getsize(file_path) > MAX_TELEGRAM_SIZE:
        await message.reply_text("The file is bigger than Telegram bot limit. Try a shorter/lower quality video.")
        return False
    extension = os.path.splitext(file_path)[1].lower()
    with open(file_path, "rb") as media:
        if extension in IMAGE_EXTENSIONS:
            await message.reply_photo(photo=media)
        elif extension in VIDEO_EXTENSIONS:
            if preserve_video_scale:
                await message.reply_document(document=media)
                return True
            width, height = video_dimensions(file_path)
            options = {"video": media, "supports_streaming": True}
            if width and height:
                options["width"] = width
                options["height"] = height
            await message.reply_video(**options)
        elif extension in AUDIO_EXTENSIONS:
            await message.reply_audio(audio=media)
        else:
            await message.reply_document(document=media)
    return True


def ydl_options(url, mode="video"):
    unique_name = f"%(extractor)s_%(id)s_{uuid.uuid4().hex[:8]}.%(ext)s"
    options = {
        "outtmpl": os.path.join(DOWNLOAD_DIR, unique_name),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 3,
        "socket_timeout": 30,
        "http_headers": {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125 Safari/537.36"},
    }
    if mode == "mp3":
        options.update({
            "format": "bestaudio/best",
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        })
    else:
        options["format"] = "best[ext=mp4][height<=720]/best[height<=720]/best[ext=mp4]/best"
    if COOKIES_FILE and os.path.exists(COOKIES_FILE):
        options["cookiefile"] = COOKIES_FILE
    if is_youtube_url(url):
        options["extractor_args"] = {"youtube": {"player_client": ["default", "ios", "web_safari", "mweb"]}}
    return options


def download_with_yt_dlp(url, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        filename = ydl.prepare_filename(info)
        if options.get("postprocessors"):
            base, _ = os.path.splitext(filename)
            mp3_path = base + ".mp3"
            if os.path.exists(mp3_path):
                return mp3_path
        return filename


def normalize_url(url):
    return url.replace("www.tiktok.com/t/", "www.tiktok.com/")


def download_tiktok_api(url):
    response = requests.get(TIKWM_API_URL, params={"url": url}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    data = (response.json().get("data") or {})
    file_paths = []
    item_id = data.get("id") or f"tiktok_{uuid.uuid4().hex[:8]}"
    for index, image_url in enumerate(data.get("images") or [], start=1):
        if image_url.startswith("//"):
            image_url = f"https:{image_url}"
        elif image_url.startswith("/"):
            image_url = f"https://www.tikwm.com{image_url}"
        image_response = requests.get(image_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        image_response.raise_for_status()
        file_path = os.path.join(DOWNLOAD_DIR, f"{item_id}_{index}.jpg")
        with open(file_path, "wb") as image_file:
            image_file.write(image_response.content)
        file_paths.append(file_path)
    video_url = data.get("play") or data.get("wmplay")
    if video_url and not file_paths:
        if video_url.startswith("//"):
            video_url = f"https:{video_url}"
        elif video_url.startswith("/"):
            video_url = f"https://www.tikwm.com{video_url}"
        video_response = requests.get(video_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        video_response.raise_for_status()
        file_path = os.path.join(DOWNLOAD_DIR, f"{item_id}.mp4")
        with open(file_path, "wb") as video_file:
            video_file.write(video_response.content)
        file_paths.append(file_path)
    return file_paths


async def send_download(message, user_id, url, mode="video"):
    files_to_send = []
    cleanup_files = []
    preserve_video_scale = is_facebook_url(url)
    url = normalize_url(url)
    await message.reply_text("Downloading... ⏳")
    try:
        before_files = {os.path.join(DOWNLOAD_DIR, file_name) for file_name in os.listdir(DOWNLOAD_DIR)}
        file_path = download_with_yt_dlp(url, ydl_options(url, mode=mode))
        files_to_send = downloaded_files(before_files, file_path)
        cleanup_files = list(files_to_send)
        if not files_to_send:
            raise RuntimeError("Download finished, but no media file was found.")
        sent_any = False
        for path in files_to_send:
            sent_any = await send_file(message, path, preserve_video_scale=preserve_video_scale) or sent_any
        if sent_any:
            log_download(user_id, platform_name(url), mode)
    except Exception as error:
        if is_tiktok_url(url) and mode == "video":
            try:
                files_to_send = download_tiktok_api(url)
                cleanup_files = list(files_to_send)
                if files_to_send:
                    sent_any = False
                    for path in files_to_send:
                        sent_any = await send_file(message, path) or sent_any
                    if sent_any:
                        log_download(user_id, "TikTok", mode)
                    return
            except Exception as fallback_error:
                await message.reply_text(f"TikTok error:\n{fallback_error}")
                return
        if is_youtube_url(url) and youtube_cookie_error(error):
            await message.reply_text(youtube_cookie_message())
            return
        await message.reply_text(f"Error:\n{error}")
    finally:
        for path in set(cleanup_files):
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass


async def share_done(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mark_shared(update.effective_user.id)
    url = context.user_data.pop("pending_download_url", None)
    mode = context.user_data.pop("pending_download_mode", "video")
    if not url:
        await query.message.reply_text("Thanks. Send your video link again.")
        return
    await query.message.reply_text("Thanks for sharing. Your download will start now.")
    await send_download(query.message, update.effective_user.id, url, mode=mode)


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        print("Telegram conflict: another bot instance is polling with the same TOKEN.")
        return
    if isinstance(context.error, (NetworkError, TimedOut)):
        print(f"Telegram network error: {context.error}")
        return
    print(f"Unhandled bot error: {context.error}")


def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("users", users_cmd))
    app.add_handler(CommandHandler("backup", backup))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("ban", ban))
    app.add_handler(CommandHandler("unban", unban))
    app.add_handler(CommandHandler("vip", vip))
    app.add_handler(CommandHandler("unvip", unvip))
    app.add_handler(CommandHandler("mp3", mp3))
    app.add_handler(CallbackQueryHandler(share_done, pattern="^share_done$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))
    app.add_error_handler(handle_error)
    print("Bot running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
