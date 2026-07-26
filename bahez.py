from messages import USER_ERROR_MESSAGE, ADMIN_ERROR_MESSAGE
import asyncio
import base64
import json
import os
import re
import shutil
import sqlite3
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import quote, urlparse

import requests
import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
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

# Add your Telegram numeric ID in Railway Variables as OWNER_ID for admin commands.
OWNER_ID = int(os.getenv("OWNER_ID", "0") or 0)
REQUIRED_CHANNEL = os.getenv("REQUIRED_CHANNEL", "").strip()  # Example: @YourChannel
SPONSOR_TEXT = os.getenv("SPONSOR_TEXT", "").strip()
ERROR_GROUP_ID = os.getenv("ERROR_GROUP_ID", "").strip()  # Example: -1001234567890 or @your_error_group

DB_PATH = os.getenv("DB_PATH", "bot.db")
DOWNLOAD_DIR = "downloads"
SHARE_INTERVAL = timedelta(days=5)
FREE_DAILY_LIMIT = int(os.getenv("FREE_DAILY_LIMIT", "20"))
VIP_DAILY_LIMIT = int(os.getenv("VIP_DAILY_LIMIT", "9999"))
MAX_TELEGRAM_SIZE = 49 * 1024 * 1024

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v"}
TIKWM_API_URL = "https://www.tikwm.com/api/"
DEFAULT_COOKIES_FILE = "cookies.txt"
RUNTIME_COOKIES_FILE = os.path.join(DOWNLOAD_DIR, "youtube_cookies.txt")
RUNTIME_INSTAGRAM_COOKIES_FILE = os.path.join(DOWNLOAD_DIR, "instagram_cookies.txt")
SUPPORTED_HOST_SUFFIXES = (
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "youtu.be",
    "youtube.com",
    "youtube-nocookie.com",
    "x.com",
    "twitter.com",
)

os.makedirs(DOWNLOAD_DIR, exist_ok=True)
db_directory = os.path.dirname(os.path.abspath(DB_PATH))
os.makedirs(db_directory, exist_ok=True)


def utc_now():
    return datetime.now(UTC)


def today_prefix():
    return utc_now().date().isoformat()


def is_owner(user_id):
    return OWNER_ID and user_id == OWNER_ID


def db_connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


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
                is_vip INTEGER NOT NULL DEFAULT 0,
                is_banned INTEGER NOT NULL DEFAULT 0,
                joined_at TEXT NOT NULL DEFAULT ''
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                platform TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT 'video',
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS broadcasts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_id INTEGER NOT NULL,
                message TEXT NOT NULL,
                sent_count INTEGER NOT NULL DEFAULT 0,
                failed_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            )
            """
        )
        ensure_columns(conn)


def ensure_columns(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    if "is_vip" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_vip INTEGER NOT NULL DEFAULT 0")
    if "is_banned" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN is_banned INTEGER NOT NULL DEFAULT 0")
    if "joined_at" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN joined_at TEXT NOT NULL DEFAULT ''")
    if "share_referral_checkpoint" not in cols:
        conn.execute("ALTER TABLE users ADD COLUMN share_referral_checkpoint INTEGER NOT NULL DEFAULT 0")


def register_user(user, referrer_id=None):
    now = utc_now().isoformat()
    with db_connect() as conn:
        existing = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (user.id,)).fetchone()
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
            valid_referrer = conn.execute("SELECT user_id FROM users WHERE user_id = ?", (referrer_id,)).fetchone()

        conn.execute(
            """
            INSERT INTO users (user_id, first_name, username, referrer_id, last_shared_at, joined_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (user.id, user.first_name, user.username, referrer_id if valid_referrer else None, now, now),
        )
        if valid_referrer:
            conn.execute("UPDATE users SET referral_count = referral_count + 1 WHERE user_id = ?", (referrer_id,))


def user_row(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT user_id, first_name, username, referral_count, is_vip, is_banned FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()


def is_banned(user_id):
    row = user_row(user_id)
    return bool(row and row[5])


def is_vip(user_id):
    row = user_row(user_id)
    return bool(row and row[4])


def set_vip(user_id, value):
    with db_connect() as conn:
        conn.execute("UPDATE users SET is_vip = ? WHERE user_id = ?", (1 if value else 0, user_id))


def set_ban(user_id, value):
    with db_connect() as conn:
        conn.execute("UPDATE users SET is_banned = ? WHERE user_id = ?", (1 if value else 0, user_id))


def count_users():
    with db_connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]


def downloads_today(user_id):
    with db_connect() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM downloads WHERE user_id = ? AND created_at LIKE ?",
            (user_id, f"{today_prefix()}%"),
        ).fetchone()[0]


def can_download(user_id):
    limit = VIP_DAILY_LIMIT if is_vip(user_id) else FREE_DAILY_LIMIT
    return downloads_today(user_id) < limit, limit


def log_download(user_id, url, kind="video"):
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO downloads (user_id, platform, kind, created_at) VALUES (?, ?, ?, ?)",
            (user_id, platform_name(url), kind, utc_now().isoformat()),
        )


def should_share(user_id):
    with db_connect() as conn:
        row = conn.execute("SELECT last_shared_at FROM users WHERE user_id = ?", (user_id,)).fetchone()
    if not row:
        return False
    last_shared_at = datetime.fromisoformat(row[0])
    if last_shared_at.tzinfo is None:
        last_shared_at = last_shared_at.replace(tzinfo=UTC)
    return utc_now() - last_shared_at >= SHARE_INTERVAL


def mark_shared(user_id):
    with db_connect() as conn:
        conn.execute(
            """
            UPDATE users
            SET last_shared_at = ?, share_referral_checkpoint = referral_count
            WHERE user_id = ?
            """,
            (utc_now().isoformat(), user_id),
        )


def has_new_referral(user_id):
    with db_connect() as conn:
        row = conn.execute(
            "SELECT referral_count, share_referral_checkpoint FROM users WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return bool(row and row[0] > row[1])


def platform_name(url):
    lowered = url.lower()
    if "tiktok" in lowered:
        return "TikTok"
    if "instagram" in lowered:
        return "Instagram"
    if "facebook" in lowered or "fb.watch" in lowered:
        return "Facebook"
    if "youtube" in lowered or "youtu.be" in lowered:
        return "YouTube"
    if "x.com" in lowered or "twitter" in lowered:
        return "X/Twitter"
    return "Other"


def url_host(url):
    return (urlparse(url).hostname or "").lower().rstrip(".")


def host_matches(host, domain):
    return host == domain or host.endswith(f".{domain}")


def is_supported_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return False
    host = url_host(url)
    return any(host_matches(host, domain) for domain in SUPPORTED_HOST_SUFFIXES)


def main_menu():
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("👤 Profile", callback_data="profile"), InlineKeyboardButton("🏆 Top", callback_data="top")],
            [InlineKeyboardButton("📊 Stats", callback_data="stats"), InlineKeyboardButton("💎 VIP", callback_data="vip_info")],
            [InlineKeyboardButton("ℹ️ Help", callback_data="help")],
        ]
    )


async def check_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not REQUIRED_CHANNEL:
        return True
    try:
        member = await context.bot.get_chat_member(REQUIRED_CHANNEL, update.effective_user.id)
        if member.status in ("member", "administrator", "creator"):
            return True
    except Exception:
        pass
    await update.effective_message.reply_text(
        f"📢 Please join our channel first: {REQUIRED_CHANNEL}\nThen send your link again."
    )
    return False


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    referrer_id = None
    if context.args and context.args[0].startswith("ref_"):
        try:
            referrer_id = int(context.args[0].replace("ref_", "", 1))
        except ValueError:
            referrer_id = None
    register_user(update.effective_user, referrer_id)
    total = count_users()
    await update.message.reply_text(
        "👋 Welcome to Bahez Video Downloader\n\n"
        f"👥 Subscribers: {total}\n\n"
        "Send a TikTok, Instagram, Facebook, YouTube, or X link.\n\n"
        "Commands:\n"
        "/mp3 <link> - download audio\n"
        "/top - top inviters\n"
        "/profile - your account\n"
        "/stats - bot stats",
        reply_markup=main_menu(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ How to use:\n\n"
        "1. Send a video link to download video.\n"
        "2. Use /mp3 <link> for audio only.\n"
        "3. Use /profile to see your account.\n"
        "4. Invite friends to climb /top."
    )


async def top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await send_top(update.effective_message)


async def send_top(message):
    total = count_users()
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
    lines = [f"👥 Total subscribers: {total}", "", "🏆 Top inviters:"]
    if not rows:
        lines.append("No invites yet.")
    else:
        for index, (first_name, username, referral_count) in enumerate(rows, start=1):
            name = f"@{username}" if username else first_name or "Unknown"
            lines.append(f"{index}. {name} - {referral_count} joined")
    await message.reply_text("\n".join(lines))


async def profile(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await send_profile(update.effective_message, update.effective_user.id)


async def send_profile(message, user_id):
    row = user_row(user_id)
    invites = row[3] if row else 0
    vip = "✅ Yes" if is_vip(user_id) else "❌ No"
    used = downloads_today(user_id)
    limit = VIP_DAILY_LIMIT if is_vip(user_id) else FREE_DAILY_LIMIT
    with db_connect() as conn:
        total_downloads = conn.execute("SELECT COUNT(*) FROM downloads WHERE user_id = ?", (user_id,)).fetchone()[0]
    await message.reply_text(
        "👤 Your Profile\n\n"
        f"💎 VIP: {vip}\n"
        f"📥 Downloads today: {used}/{limit}\n"
        f"📦 Total downloads: {total_downloads}\n"
        f"👥 Invites: {invites}"
    )


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    await send_stats(update.effective_message)


async def send_stats(message):
    now = utc_now()
    today = today_prefix()
    week_ago = (now - timedelta(days=7)).isoformat()
    with db_connect() as conn:
        total_users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        vip_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_vip = 1").fetchone()[0]
        banned_users = conn.execute("SELECT COUNT(*) FROM users WHERE is_banned = 1").fetchone()[0]
        total_downloads = conn.execute("SELECT COUNT(*) FROM downloads").fetchone()[0]
        downloads_today_count = conn.execute("SELECT COUNT(*) FROM downloads WHERE created_at LIKE ?", (f"{today}%",)).fetchone()[0]
        new_today = conn.execute("SELECT COUNT(*) FROM users WHERE joined_at LIKE ?", (f"{today}%",)).fetchone()[0]
        active_week = conn.execute("SELECT COUNT(DISTINCT user_id) FROM downloads WHERE created_at >= ?", (week_ago,)).fetchone()[0]
        top_platform = conn.execute(
            "SELECT platform, COUNT(*) AS c FROM downloads GROUP BY platform ORDER BY c DESC LIMIT 1"
        ).fetchone()
    platform_text = f"{top_platform[0]} ({top_platform[1]})" if top_platform else "No downloads yet"
    await message.reply_text(
        "📊 Bot Statistics\n\n"
        f"👥 Subscribers: {total_users}\n"
        f"🆕 New today: {new_today}\n"
        f"💎 VIP users: {vip_users}\n"
        f"🚫 Banned users: {banned_users}\n"
        f"📥 Total downloads: {total_downloads}\n"
        f"📥 Downloads today: {downloads_today_count}\n"
        f"🔥 Active this week: {active_week}\n"
        f"🏅 Top platform: {platform_text}"
    )


async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    await update.message.reply_text(
        "👑 Admin Panel\n\n"
        "/stats - bot statistics\n"
        "/broadcast <message> - send to all users\n"
        "/vip <user_id> - give VIP\n"
        "/unvip <user_id> - remove VIP\n"
        "/ban <user_id> - ban user\n"
        "/unban <user_id> - unban user"
    )
async def groupid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.message.reply_text(
        f"Chat Name: {chat.title}\n"
        f"Chat ID: {chat.id}"
    )

async def parse_admin_user_id(update, context, usage):
    if not context.args:
        await update.message.reply_text(f"Use: {usage}")
        return None
    try:
        user_id = int(context.args[0])
    except (TypeError, ValueError):
        await update.message.reply_text(f"Invalid user ID. Use: {usage}")
        return None
    if user_id <= 0:
        await update.message.reply_text("User ID must be a positive number.")
        return None
    return user_id


async def vip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    user_id = await parse_admin_user_id(update, context, "/vip user_id")
    if user_id is None:
        return
    set_vip(user_id, True)
    await update.message.reply_text("✅ VIP added.")


async def unvip_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    user_id = await parse_admin_user_id(update, context, "/unvip user_id")
    if user_id is None:
        return
    set_vip(user_id, False)
    await update.message.reply_text("✅ VIP removed.")


async def ban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    user_id = await parse_admin_user_id(update, context, "/ban user_id")
    if user_id is None:
        return
    set_ban(user_id, True)
    await update.message.reply_text("🚫 User banned.")


async def unban_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    user_id = await parse_admin_user_id(update, context, "/unban user_id")
    if user_id is None:
        return
    set_ban(user_id, False)
    await update.message.reply_text("✅ User unbanned.")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("Use: /broadcast your message here")
        return
    with db_connect() as conn:
        user_ids = [row[0] for row in conn.execute("SELECT user_id FROM users WHERE is_banned = 0").fetchall()]
    sent = failed = 0
    status = await update.message.reply_text(f"📢 Broadcasting to {len(user_ids)} users...")
    for uid in user_ids:
        try:
            await context.bot.send_message(chat_id=uid, text=text)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    with db_connect() as conn:
        conn.execute(
            "INSERT INTO broadcasts (admin_id, message, sent_count, failed_count, created_at) VALUES (?, ?, ?, ?, ?)",
            (update.effective_user.id, text, sent, failed, utc_now().isoformat()),
        )
    await status.edit_text(f"📢 Broadcast done.\n✅ Sent: {sent}\n❌ Failed: {failed}")


async def send_share_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    bot_username = (await context.bot.get_me()).username
    invite_link = f"https://t.me/{bot_username}?start=ref_{update.effective_user.id}"
    share_text = "Join this video downloader bot:"
    share_url = f"tg://msg_url?url={quote(invite_link)}&text={quote(share_text)}"
    context.user_data["pending_download_url"] = url

    await update.message.reply_text(
        "To continue, invite one new friend with your personal link. After they start the bot, press Check invitation.",
        reply_markup=keyboard,
    )


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    if not await check_channel(update, context):
        return
    ok, limit = can_download(update.effective_user.id)
    if not ok:
        await update.message.reply_text(f"Daily limit reached ({limit}). Upgrade to VIP for unlimited downloads.")
        return
    text = update.message.text or ""
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await update.message.reply_text("Please send a valid video link.")
        return
    url = urls[0].strip()
    if not is_supported_url(url):
        await update.message.reply_text("Unsupported link. Send TikTok, Instagram, Facebook, YouTube, or X only.")
        return
    if should_share(update.effective_user.id):
        await send_share_prompt(update, context, url)
        return
    success = await send_download(update.message, url, context=context, kind="video")
    if success:
        log_download(update.effective_user.id, url, "video")
        if SPONSOR_TEXT:
            await update.message.reply_text(SPONSOR_TEXT)


async def mp3(update: Update, context: ContextTypes.DEFAULT_TYPE):
    register_user(update.effective_user)
    if is_banned(update.effective_user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    if not await check_channel(update, context):
        return
    ok, limit = can_download(update.effective_user.id)
    if not ok:
        await update.message.reply_text(f"Daily limit reached ({limit}). Upgrade to VIP for unlimited downloads.")
        return
    text = " ".join(context.args)
    urls = re.findall(r"https?://[^\s]+", text)
    if not urls:
        await update.message.reply_text("Use: /mp3 https://youtube.com/...")
        return
    url = urls[0].strip()
    if not is_supported_url(url):
        await update.message.reply_text("Unsupported link. Send TikTok, Instagram, Facebook, YouTube, or X only.")
        return
    success = await send_download(update.message, url, context=context, kind="mp3")
    if success:
        log_download(update.effective_user.id, url, "mp3")


def write_cookies_from_env(text_name, b64_name, runtime_path):
    cookies_text = os.getenv(text_name)
    cookies_b64 = os.getenv(b64_name)

    if cookies_b64:
        try:
            cookies_text = base64.b64decode(cookies_b64).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            cookies_text = None

    if cookies_text:
        with open(runtime_path, "w", encoding="utf-8") as cookies_file:
            cookies_file.write(cookies_text)
        return runtime_path

    return None


def prepare_cookies_file():
    youtube_cookie_file = write_cookies_from_env(
        "YOUTUBE_COOKIES_TEXT",
        "YOUTUBE_COOKIES_B64",
        RUNTIME_COOKIES_FILE,
    )
    if youtube_cookie_file:
        return youtube_cookie_file

    custom_cookies_file = os.getenv("COOKIES_FILE")
    if custom_cookies_file:
        return custom_cookies_file

    if os.path.exists(DEFAULT_COOKIES_FILE):
        return DEFAULT_COOKIES_FILE

    return None


def prepare_instagram_cookies_file():
    instagram_cookie_file = write_cookies_from_env(
        "INSTAGRAM_COOKIES_TEXT",
        "INSTAGRAM_COOKIES_B64",
        RUNTIME_INSTAGRAM_COOKIES_FILE,
    )
    if instagram_cookie_file:
        return instagram_cookie_file

    custom_instagram_file = os.getenv("INSTAGRAM_COOKIES_FILE")
    if custom_instagram_file:
        return custom_instagram_file

    return None


COOKIES_FILE = prepare_cookies_file()
INSTAGRAM_COOKIES_FILE = prepare_instagram_cookies_file()


def is_tiktok_url(url):
    return host_matches(url_host(url), "tiktok.com")


def is_facebook_url(url):
    host = url_host(url)
    return host_matches(host, "facebook.com") or host_matches(host, "fb.watch")


def is_youtube_url(url):
    host = url_host(url)
    return any(host_matches(host, domain) for domain in ("youtube.com", "youtu.be", "youtube-nocookie.com"))


def is_instagram_url(url):
    return host_matches(url_host(url), "instagram.com")


def instagram_cookie_error(error):
    error_text = str(error).lower()
    return any(
        phrase in error_text
        for phrase in (
            "empty media response",
            "login required",
            "requested content is not available",
            "cookies",
            "authentication",
            "private",
        )
    )


def instagram_cookie_message():
    return (
        "Instagram did not allow the server to access this Reel.\n\n"
        "Make sure Railway Variables has INSTAGRAM_COOKIES_TEXT, then Redeploy. "
        "If it still fails, export fresh Instagram cookies while logged in and replace the old value."
    )

def youtube_cookie_error(error):
    error_text = str(error).lower()
    return any(
        phrase in error_text
        for phrase in ("sign in to confirm", "not a bot", "use --cookies", "cookies-from-browser", "confirm you're not a bot")
    )


def youtube_cookie_message():
    return (
        "YouTube is asking the server to sign in before downloading this video.\n\n"
        "Fix: export fresh YouTube cookies and set YOUTUBE_COOKIES_TEXT or YOUTUBE_COOKIES_B64 in Railway Variables."
    )


def downloaded_files(before_files, prepared_file_path):
    after_files = {os.path.join(DOWNLOAD_DIR, file_name) for file_name in os.listdir(DOWNLOAD_DIR)}
    new_files = after_files - before_files
    if prepared_file_path and os.path.exists(prepared_file_path):
        new_files.add(prepared_file_path)
    return sorted(new_files, key=os.path.getmtime)


def ydl_options(url, kind="video"):
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
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0.0.0 Safari/537.36"
        },
    }
    if kind == "mp3":
        options.update(
            {
                "format": "bestaudio/best",
                "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
            }
        )
    else:
        if is_instagram_url(url):
            # Prefer the original Instagram Reel stream. This avoids the cropped/zoomed
            # 1:1 or center-cropped variants that Instagram sometimes exposes.
            options["format"] = "bv*+ba/b"
            options["merge_output_format"] = "mp4"
        else:
            options["format"] = "best[ext=mp4][height<=720]/best[height<=720]/best[ext=mp4]/best"
    if is_instagram_url(url) and INSTAGRAM_COOKIES_FILE and os.path.exists(INSTAGRAM_COOKIES_FILE):
        options["cookiefile"] = INSTAGRAM_COOKIES_FILE
    elif COOKIES_FILE and os.path.exists(COOKIES_FILE):
        options["cookiefile"] = COOKIES_FILE

    if is_instagram_url(url):
        options["extractor_args"] = {"instagram": {"app_id": ["936619743392459"]}}

    if is_youtube_url(url):
        options["extractor_args"] = {"youtube": {"player_client": ["default", "ios", "web_safari", "mweb"]}}
    return options


def instagram_retry_options(url, kind="video", use_cookies=True):
    options = ydl_options(url, kind=kind)
    if kind == "video":
        # Instagram often exposes a ready-to-send combined MP4 even when its
        # separate video/audio formats cannot be merged on a hosted server.
        options["format"] = "best[ext=mp4]/best"
        options.pop("merge_output_format", None)
    if not use_cookies:
        options.pop("cookiefile", None)
    return options


def download_with_yt_dlp(url, options):
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(url, download=True)
        return {
            "file_path": ydl.prepare_filename(info),
            "thumbnail_url": info.get("thumbnail"),
        }


def normalize_url(url):
    return url.replace("www.tiktok.com/t/", "www.tiktok.com/")


def download_tiktok_api(url):
    response = requests.get(TIKWM_API_URL, params={"url": url}, headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
    response.raise_for_status()
    data = (response.json()).get("data") or {}
    file_paths = []
    item_id = data.get("id") or f"tiktok_{uuid.uuid4().hex[:8]}"
    image_urls = data.get("images") or []
    for index, image_url in enumerate(image_urls, start=1):
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


def video_dimensions(file_path):
    if not shutil.which("ffprobe"):
        return None, None
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=width,height,display_aspect_ratio,sample_aspect_ratio",
                "-of",
                "json",
                file_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        streams = json.loads(result.stdout).get("streams") or []
        if not streams:
            return None, None
        stream = streams[0]
        width = stream.get("width")
        height = stream.get("height")
        display_ratio = stream.get("display_aspect_ratio")

        # Some downloaded streams use non-square pixels. Telegram ignores that
        # metadata when creating its preview unless we provide display dimensions.
        if height and display_ratio and display_ratio not in ("0:1", "N/A"):
            try:
                ratio_width, ratio_height = (int(value) for value in display_ratio.split(":", 1))
                if ratio_width > 0 and ratio_height > 0:
                    display_width = round(height * ratio_width / ratio_height)
                    return display_width, height
            except (TypeError, ValueError, ZeroDivisionError):
                pass

        return width, height
    except Exception:
        return None, None


def extract_first_frame(file_path):
    if not shutil.which("ffmpeg"):
        return None
    frame_path = os.path.join(DOWNLOAD_DIR, f"first_frame_{uuid.uuid4().hex}.jpg")
    try:
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                file_path,
                "-frames:v",
                "1",
                "-q:v",
                "2",
                "-y",
                frame_path,
            ],
            check=True,
            capture_output=True,
            timeout=30,
        )
        return frame_path if os.path.exists(frame_path) else None
    except Exception as error:
        print(f"Could not extract first frame: {error}")
        try:
            if os.path.exists(frame_path):
                os.remove(frame_path)
        except OSError:
            pass
        return None


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
            return True

        if extension in VIDEO_EXTENSIONS:
            # Sending every video as a document preserves the source quality,
            # dimensions, and aspect ratio without Telegram recompression.
            await message.reply_document(
                document=media,
                filename=os.path.basename(file_path),
                caption="📁 ڤیدیۆکە بە فول کوالیتی 🔥 ",
            )
            return True

        await message.reply_document(
            document=media,
            filename=os.path.basename(file_path),
        )
        return True


async def send_video_cover(message, thumbnail_url, video_path):
    if thumbnail_url:
        try:
            await message.reply_photo(
                photo=thumbnail_url,
                caption="🖼 وێنەی سەرەتای ڤیدیۆکە",
            )
            return
        except Exception as error:
            print(f"Could not send source thumbnail: {error}")

    frame_path = await asyncio.to_thread(extract_first_frame, video_path)
    if not frame_path:
        return
    try:
        with open(frame_path, "rb") as frame:
            await message.reply_photo(
                photo=frame,
                caption="🖼 وێنەی سەرەتای ڤیدیۆکە",
            )
    finally:
        try:
            os.remove(frame_path)
        except OSError:
            pass


def short_text(value, limit=2500):
    value = str(value)
    return value if len(value) <= limit else value[:limit] + "..."


async def report_download_error(context, message, url, error, kind="video"):
    if not ERROR_GROUP_ID or context is None:
        return

    user = getattr(message, "from_user", None)
    chat = getattr(message, "chat", None)
    username = f"@{user.username}" if user and user.username else "No username"
    full_name = user.full_name if user else "Unknown user"
    user_id = user.id if user else "Unknown"
    chat_id = chat.id if chat else "Unknown"
    now = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    report = (
        "🚨 BahezBot Download Error\n\n"
        f"🕒 Time: {now}\n"
        f"👤 User: {full_name} ({username})\n"
        f"🆔 User ID: {user_id}\n"
        f"💬 Chat ID: {chat_id}\n"
        f"📱 Platform: {platform_name(url)}\n"
        f"📦 Type: {kind}\n\n"
        f"🔗 URL:\n{url}\n\n"
        f"❌ Error:\n{short_text(error)}"
    )

    try:
        await context.bot.send_message(chat_id=ERROR_GROUP_ID, text=report)
    except Exception as send_error:
        print(f"Could not send error report to group: {send_error}")


async def send_clean_error(message):
    await message.reply_text(USER_ERROR_MESSAGE)


async def send_download(message, url, context=None, kind="video"):
    files_to_send = []
    cleanup_files = []
    thumbnail_url = None
    # Send Instagram and Facebook as documents to preserve the original aspect ratio.
    preserve_video_scale = is_facebook_url(url) or is_instagram_url(url)
    url = normalize_url(url)
    await message.reply_text("چاوەڕوانە... ⏳")
    try:
        before_files = {os.path.join(DOWNLOAD_DIR, file_name) for file_name in os.listdir(DOWNLOAD_DIR)}
        download_result = await asyncio.to_thread(download_with_yt_dlp, url, ydl_options(url, kind=kind))
        file_path = download_result["file_path"]
        thumbnail_url = download_result.get("thumbnail_url")
        files_to_send = downloaded_files(before_files, file_path)
        cleanup_files = list(files_to_send)
        if not files_to_send:
            raise RuntimeError("Download finished, but no media file was found.")
        if kind == "video":
            first_video = next(
                (path for path in files_to_send if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS),
                None,
            )
            if first_video:
                await send_video_cover(message, thumbnail_url, first_video)
        sent_any = False
        for path in files_to_send:
            sent_any = await send_file(message, path, preserve_video_scale=preserve_video_scale) or sent_any
        return sent_any
    except Exception as error:
        print(f"Primary {platform_name(url)} {kind} download failed: {error}")
        if is_tiktok_url(url) and kind == "video":
            try:
                files_to_send = await asyncio.to_thread(download_tiktok_api, url)
                cleanup_files = list(files_to_send)
                if files_to_send:
                    first_video = next(
                        (
                            path
                            for path in files_to_send
                            if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
                        ),
                        None,
                    )
                    if first_video:
                        await send_video_cover(message, None, first_video)
                    sent_any = False
                    for path in files_to_send:
                        sent_any = await send_file(message, path) or sent_any
                    return sent_any
            except Exception as fallback_error:
                print(f"TikTok fallback failed: {fallback_error}")
                await report_download_error(context, message, url, fallback_error, kind)
                await send_clean_error(message)
                return False
        if is_instagram_url(url):
            retry_errors = [error]
            for use_cookies in (True, False):
                try:
                    retry_before = {
                        os.path.join(DOWNLOAD_DIR, file_name)
                        for file_name in os.listdir(DOWNLOAD_DIR)
                    }
                    retry_result = await asyncio.to_thread(
                        download_with_yt_dlp,
                        url,
                        instagram_retry_options(url, kind=kind, use_cookies=use_cookies),
                    )
                    retry_path = retry_result["file_path"]
                    retry_thumbnail = retry_result.get("thumbnail_url")
                    files_to_send = downloaded_files(retry_before, retry_path)
                    cleanup_files.extend(files_to_send)
                    if not files_to_send:
                        raise RuntimeError("Instagram retry finished without a media file.")
                    if kind == "video":
                        first_video = next(
                            (
                                path
                                for path in files_to_send
                                if os.path.splitext(path)[1].lower() in VIDEO_EXTENSIONS
                            ),
                            None,
                        )
                        if first_video:
                            await send_video_cover(message, retry_thumbnail, first_video)
                    sent_any = False
                    for path in files_to_send:
                        sent_any = await send_file(
                            message,
                            path,
                            preserve_video_scale=True,
                        ) or sent_any
                    if sent_any:
                        return True
                except Exception as retry_error:
                    retry_errors.append(retry_error)
                    mode = "cookies" if use_cookies else "no cookies"
                    print(f"Instagram retry ({mode}) failed: {retry_error}")

            final_error = retry_errors[-1]
            await report_download_error(context, message, url, final_error, kind)
            if instagram_cookie_error(final_error):
                await message.reply_text(instagram_cookie_message())
            else:
                await send_clean_error(message)
            return False
        if is_instagram_url(url) and instagram_cookie_error(error):
            await report_download_error(context, message, url, error, kind)
            await message.reply_text(instagram_cookie_message())
            return False
        if is_youtube_url(url) and youtube_cookie_error(error):
            await report_download_error(context, message, url, error, kind)
            await message.reply_text(youtube_cookie_message())
            return False
        await report_download_error(context, message, url, error, kind)
        await send_clean_error(message)
        return False
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
    if not has_new_referral(update.effective_user.id):
        await query.message.reply_text(
            "No new invitation was found yet. Your friend must open your link and press Start, then check again."
        )
        return
    mark_shared(update.effective_user.id)
    url = context.user_data.pop("pending_download_url", None)
    if not url:
        await query.message.reply_text("Thanks. Send your video link again.")
        return
    await query.message.reply_text("Thanks for sharing. Your download will start now.")
    success = await send_download(query.message, url, context=context)
    if success:
        log_download(update.effective_user.id, url, "video")


async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data == "profile":
        await send_profile(query.message, query.from_user.id)
    elif data == "top":
        await send_top(query.message)
    elif data == "stats":
        await send_stats(query.message)
    elif data == "vip_info":
        await query.message.reply_text("💎 VIP gives more downloads and priority access. Contact the bot owner.")
    elif data == "help":
        await query.message.reply_text("Send a video link, or use /mp3 <link> for audio.")


async def handle_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    if isinstance(context.error, Conflict):
        print("Telegram conflict: another bot instance is polling with the same TOKEN.")
        return
    if isinstance(context.error, (NetworkError, TimedOut, BadRequest, Forbidden)):
        print(f"Telegram error: {context.error}")
        return
    print(f"Unhandled bot error: {context.error}")


def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("top", top))
    app.add_handler(CommandHandler("profile", profile))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("broadcast", broadcast))
    app.add_handler(CommandHandler("vip", vip_command))
    app.add_handler(CommandHandler("unvip", unvip_command))
    app.add_handler(CommandHandler("ban", ban_command))
    app.add_handler(CommandHandler("unban", unban_command))
    app.add_handler(CommandHandler("mp3", mp3))
    app.add_handler(CallbackQueryHandler(share_done, pattern="^share_done$"))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))
    app.add_handler(CommandHandler("groupid", groupid))
    app.add_error_handler(handle_error)
    print("BahezBot v3 running...")
    app.run_polling(drop_pending_updates=True)
    


if __name__ == "__main__":
    main()
