from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, filters, ContextTypes
import yt_dlp
import os

TOKEN = os.getenv("TOKEN")

# create downloads folder
if not os.path.exists("downloads"):
    os.makedirs("downloads")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "سڵاو 👋\n\n"
        "بەخێربێیت بۆ Bahez Video Downloader 🚀\n\n"
        "لینکی ڤیدیۆ بنێرە، من دایدەبەزێنم بۆت.\n\n"
        "پشتگیری دەکات:\n"
        "• TikTok\n"
        "• YouTube\n"
        "• Instagram Public\n"
        "• Facebook Public"
    )


async def download(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text

    await update.message.reply_text("چاوەڕوانبە.....")

    ydl_opts = {
        "outtmpl": "downloads/%(title)s.%(ext)s",
        "quiet": True,
        "cookiefile": "cookies.txt",
        "format": "mp4",
        "noplaylist": True
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            filename = ydl.prepare_filename(info)

        with open(filename, "rb") as video:
            await update.message.reply_video(video=video)

        os.remove(filename)

    except Exception as e:
        await update.message.reply_text(
            f"ببوره، ئەم ڤیدیۆیە ناتوانرێت دابەزێندرێت.\n\n{e}"
        )


app = ApplicationBuilder().token(TOKEN).build()

app.add_handler(CommandHandler("start", start))
app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, download))

print("Bahez Bot is running...")

app.run_polling()