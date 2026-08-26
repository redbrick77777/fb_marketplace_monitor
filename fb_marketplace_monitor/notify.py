"""Optional push notification when a watch finds something new.

Telegram rather than email, deliberately: this exists for the watches that
match rarely but matter when they do (a specific tire size turns up roughly
once every three days), where the point is to hear about it while the listing
is still live. A push lands on the phone immediately and can't be filtered
into a spam folder, and it needs no domain, no DNS records and no third-party
sending reputation to maintain.

Note the ceiling on how fast this can possibly be: cron runs the monitor every
3 hours, so a notification arrives up to 3 hours after the seller posted. The
channel is not the limiting factor - the polling interval is.

Nothing here is on the critical path. A notification that fails to send is
logged and dropped; it must never cost a listing, because the caller has
already written the row and marked it seen by the time we get here.
"""
from __future__ import annotations

import html
import logging
import time
from typing import Optional

import requests

logger = logging.getLogger(__name__)

API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# Telegram rejects anything over 4096 characters outright. Stay well under it
# and split instead - a watch that suddenly matches 30 listings should still
# deliver, just as several messages.
MAX_CHARS_PER_MESSAGE = 3500
MAX_LISTINGS_PER_MESSAGE = 10

SEND_ATTEMPTS = 2  # one cheap retry: a blip shouldn't lose a rare-item alert


class NotifyError(RuntimeError):
    """Sending failed. Callers are expected to log this and carry on."""


def _format_listing(listing: dict) -> str:
    """One listing as a Telegram HTML block.

    HTML, not MarkdownV2: listing titles are arbitrary seller text full of
    `*`, `_`, `[` and `.`, every one of which MarkdownV2 requires escaping and
    rejects the whole message over. HTML needs only three characters escaped,
    which html.escape does exactly.
    """
    title = html.escape(listing.get("title") or "(no title)")
    link = listing.get("link") or ""

    facts = []
    if listing.get("price"):
        facts.append(html.escape(str(listing["price"])))
    if listing.get("distance"):
        facts.append(f"{html.escape(str(listing['distance']))} mi")
    if listing.get("posted"):
        facts.append(f"posted {html.escape(str(listing['posted']))}")

    head = f'<a href="{html.escape(link, quote=True)}">{title}</a>' if link else title
    return f"<b>{head}</b>\n{' · '.join(facts)}" if facts else f"<b>{head}</b>"


def build_messages(watch_name: str, listings: list[dict]) -> list[str]:
    """Split a run's finds into sendable messages.

    Split by listing count AND by characters: ten ordinary listings fit
    comfortably, but a single seller writing a 2000-character title would
    otherwise push a message past the API limit and lose the whole batch.
    """
    if not listings:
        return []

    header = f"🔔 <b>{html.escape(watch_name)}</b> — {len(listings)} new"
    messages: list[str] = []
    chunk: list[str] = []
    size = len(header)

    for listing in listings:
        block = _format_listing(listing)
        too_long = size + len(block) + 2 > MAX_CHARS_PER_MESSAGE
        if chunk and (too_long or len(chunk) >= MAX_LISTINGS_PER_MESSAGE):
            messages.append("\n\n".join([header, *chunk]))
            chunk, size = [], len(header)
        chunk.append(block)
        size += len(block) + 2

    if chunk:
        messages.append("\n\n".join([header, *chunk]))
    return messages


class TelegramNotifier:
    def __init__(self, bot_token: str, chat_id: str, timeout: int = 10):
        self._url = API_URL.format(token=bot_token)
        self._chat_id = str(chat_id)
        self._timeout = timeout

    def send(self, watch_name: str, listings: list[dict]) -> int:
        """Sends one message per chunk. Returns how many were delivered.

        Raises NotifyError on the first failure, having already delivered any
        earlier chunks - a partial alert beats a silent one, and the log says
        which happened.
        """
        sent = 0
        for message in build_messages(watch_name, listings):
            self._send_one(message)
            sent += 1
        return sent

    def _send_one(self, text: str) -> None:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            # Several listings per message: previews would bury the text under
            # a stack of images.
            "disable_web_page_preview": True,
        }

        last_error: Optional[str] = None
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                resp = requests.post(self._url, json=payload, timeout=self._timeout)
            except requests.RequestException as exc:
                last_error = str(exc)
            else:
                # Telegram signals failure two ways - an HTTP error, and a 200
                # carrying {"ok": false} - so check both.
                body = {}
                try:
                    body = resp.json()
                except ValueError:
                    pass
                if resp.ok and body.get("ok"):
                    return
                last_error = (
                    f"HTTP {resp.status_code}: {body.get('description') or resp.text[:200]}"
                )
                # 4xx here means a bad token, a wrong chat_id or malformed
                # markup. Retrying sends the identical request and fails
                # identically, so don't.
                if 400 <= resp.status_code < 500:
                    break

            if attempt < SEND_ATTEMPTS:
                time.sleep(2)

        raise NotifyError(f"Telegram send failed: {last_error}")
