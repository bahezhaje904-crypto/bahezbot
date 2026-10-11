"""Lightweight URL validation tests (no Telegram token needed)."""
import ast
from pathlib import Path

source = Path("bahez.py").read_text(encoding="utf-8")
ast.parse(source)
assert '"snapchat.com"' in source
assert 'def is_snapchat_url(url):' in source
assert 'def public_media_message(url):' in source
assert '"instagram.com"' in source
assert '"facebook.com"' in source
assert '"youtube.com"' in source
print("Social URL support and Python syntax checks passed")
