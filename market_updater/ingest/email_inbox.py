"""Newsletter inbox over IMAP (optional). For FT, Bloomberg, Apollo and other emailed newsletters.

Set IMAP_HOST, IMAP_USER and IMAP_APP_PASSWORD (an app-specific password) in .env.
Messages from senders listed in email_inbox.subscriber_senders are marked as subscriber
content: the briefing quotes at most a line from them and always links back.
"""
from __future__ import annotations

import email
import imaplib
import os
import re
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime

from ..settings import Settings
from ..util import html_to_text, log
from . import Item


def _decode(s: str | None) -> str:
    try:
        return str(make_header(decode_header(s or "")))
    except Exception:
        return s or ""


def _body(msg) -> tuple[str, str]:
    html, plain = "", ""
    for part in msg.walk() if msg.is_multipart() else [msg]:
        ctype = part.get_content_type()
        if part.get("Content-Disposition", "").startswith("attachment"):
            continue
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        text = payload.decode(part.get_content_charset() or "utf-8", errors="replace")
        if ctype == "text/html" and len(text) > len(html):
            html = text
        elif ctype == "text/plain" and len(text) > len(plain):
            plain = text
    return html, plain


def _web_link(html: str) -> str | None:
    m = re.search(r'href="(https?://[^"]+)"[^>]*>\s*(?:View|Read)[^<]{0,30}(?:browser|online|web)', html, re.I)
    return m.group(1) if m else None


def fetch_inbox(settings: Settings, lookback_hours: int = 36) -> list[Item]:
    host, user, pw = (os.environ.get(k) for k in ("IMAP_HOST", "IMAP_USER", "IMAP_APP_PASSWORD"))
    if not (host and user and pw):
        return []
    cfg = settings.get("email_inbox", {}) or {}
    subscriber_senders = [s.lower() for s in cfg.get("subscriber_senders", [])]
    since = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).strftime("%d-%b-%Y")
    out: list[Item] = []
    with imaplib.IMAP4_SSL(host) as M:
        M.login(user, pw)
        M.select(cfg.get("folder", "INBOX"))
        typ, data = M.search(None, f'(SINCE "{since}")')
        for num in (data[0].split() if data and data[0] else [])[-80:]:
            typ, raw = M.fetch(num, "(RFC822)")
            if not raw or not raw[0]:
                continue
            msg = email.message_from_bytes(raw[0][1])
            sender = _decode(msg.get("From", "")).lower()
            subject = _decode(msg.get("Subject", ""))
            html, plain = _body(msg)
            text = html_to_text(html) if html else plain
            if len(text) < 300:
                continue
            try:
                published = parsedate_to_datetime(msg.get("Date"))
            except Exception:
                published = datetime.now(timezone.utc)
            link = _web_link(html) or f"email:{msg.get('Message-ID', num.decode())}"
            name = re.sub(r"\s*<.*?>", "", _decode(msg.get("From", ""))).strip('" ') or sender
            out.append(Item(url=link, title=subject, text=text[:60000], source_name=name,
                            source_type="email", kind="email", published=published,
                            is_subscriber=any(s in sender for s in subscriber_senders)))
    log.info("Email inbox: %d messages", len(out))
    return out
