# Bahez Video Downloader

Telegram bot for downloading public media from TikTok, Instagram, Facebook, YouTube, and X.

## Railway variables

Required:

- `TOKEN`: Telegram BotFather token.
- `OWNER_ID`: Numeric Telegram user ID for admin commands.

Optional:

- `REQUIRED_CHANNEL`: Channel username, such as `@YourChannel`.
- `SPONSOR_TEXT`: Message sent after a successful video download.
- `ERROR_GROUP_ID`: Private Telegram group/chat that receives download errors.
- `FREE_DAILY_LIMIT`: Defaults to `20`.
- `VIP_DAILY_LIMIT`: Defaults to `9999`.
- `YOUTUBE_COOKIES_TEXT` or `YOUTUBE_COOKIES_B64`: Fresh YouTube cookies stored as a Railway secret.
- `INSTAGRAM_COOKIES_TEXT` or `INSTAGRAM_COOKIES_B64`: Fresh Instagram cookies stored as a Railway secret.
- `DB_PATH`: Set to `/data/bot.db` when using a Railway Volume mounted at `/data`.

Never commit tokens or cookie files. If a cookie was committed publicly, log out that account from all sessions before generating a fresh cookie value.

## Railway persistence

Mount a Railway Volume at `/data`, then set `DB_PATH=/data/bot.db`. Without a volume, users, download counts, VIP status, bans, and referral data may be lost after redeployment.

## Start command

The included `Procfile` runs:

```text
worker: python bahez.py
```
