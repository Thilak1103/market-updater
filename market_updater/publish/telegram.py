"""Optional Telegram alerts (failures and the health check)."""
from __future__ import annotations

import os

from ..util import http, log


def send(text: str) -> bool:
    token, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat):
        log.info("Telegram not configured; alert was: %s", text)
        return False
    try:
        r = http().post(f"https://api.telegram.org/bot{token}/sendMessage",
                        json={"chat_id": chat, "text": text[:4000], "disable_web_page_preview": True})
        return r.status_code == 200
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        return False
