# FFmpeg / FFprobe Railway deployment check

The existing `nixpacks.toml` requests FFmpeg, but the running Railway worker reported that both `ffmpeg` and `ffprobe` were missing.

This branch adds a `Dockerfile` based on Python 3.11 slim. It installs the Debian `ffmpeg` package (which includes `ffprobe`), validates both commands during the image build, installs the existing Python dependencies, and launches `bahez.py`.

## Before deployment

1. Keep the production `main` branch unchanged until the image builds successfully.
2. In Railway service settings, check **Build**. Use the **Dockerfile** builder (or auto-detection if Railway recognizes the Dockerfile), not a forced Railpack/Nixpacks builder.
3. Deploy the test branch to a separate Railway environment/service if available. Do not run two pollers using the same Telegram bot token simultaneously.
4. Verify `ffmpeg -version` and `ffprobe -version` from build logs or a shell, then test a video that requires audio/video merging and a TikTok MP3.
5. After confirming the new image works, merge the PR and deploy deliberately.

This change does **not** resolve Instagram/YouTube authentication, TikTok IP blocks, or Telegram error-group permissions.
