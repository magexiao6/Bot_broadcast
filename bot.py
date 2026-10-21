"""Fetch public Telegram channel history, translate via Gemini, publish bilingual news."""
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from gemini_translate import translate_news_batch

BASE_DIR = Path(__file__).resolve().parent
STATE_FILE = BASE_DIR / "state.json"
TG_EN_URL = os.getenv("TG_EN_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MODE = "gemini_batch_v2"
BATCH_SIZE = 5
MAX_HISTORY_PAGES = int(os.getenv("MAX_HISTORY_PAGES", "25"))
MAX_BATCHES_PER_RUN = int(os.getenv("MAX_BATCHES_PER_RUN", "12"))
MAX_SEND_PER_RUN = int(os.getenv("MAX_SEND_PER_RUN", "60"))
PAGE_DELAY_SECONDS = 0.3
TEXT_CHUNK_SIZE = 3400  # UTF-16 units; Telegram limit is 4096.
PHOTO_MAX_FAILURES = 3


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def save_state(state):
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {"initialized": False, "sent_ids": [], "pending": {}, "alerts_sent": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def channel_name():
    parsed = urlparse(TG_EN_URL)
    path = parsed.path.strip("/").split("/")
    if parsed.hostname not in ("t.me", "www.t.me") or len(path) != 2 or path[0] != "s":
        raise ValueError("TG_EN_URL must look like https://t.me/s/zaihua_news")
    return path[1]


def number_from_post_id(post_id, expected_channel):
    prefix, separator, number = str(post_id).partition("/")
    if separator and prefix.casefold() == expected_channel.casefold() and number.isdecimal():
        return int(number)
    return None


def fetch_telegram_page(before=None):
    """Read one preview page and its older-page cursor."""
    channel = channel_name()
    base_url = f"https://t.me/s/{channel}"
    response = requests.get(
        base_url,
        params={"before": before} if before is not None else None,
        headers={"User-Agent": "Mozilla/5.0 (compatible; TelegramNewsBot/1.0)"},
        timeout=30,
    )
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    posts = soup.select(".tgme_widget_message[data-post]")

    numbers = []
    news = {}
    for post in posts:
        post_id = (post.get("data-post") or "").strip()
        number = number_from_post_id(post_id, channel)
        if number is None:
            continue
        numbers.append(number)

        text_tag = post.select_one(".tgme_widget_message_text")
        if text_tag is None:
            continue
        for br in text_tag.find_all("br"):
            br.replace_with("\n")
        raw = text_tag.get_text()
        parts = [part.strip() for part in raw.split("\n\n") if part.strip()]
        parts = [part for part in parts if not part.startswith("\U0001f338")]
        parts = [part for part in parts if part.casefold() not in {
            "flower community's assistant", "hua community's assistant"
        }]
        if not parts:
            continue

        date_tag = post.select_one(".tgme_widget_message_date time[datetime]")
        media = []
        for photo in post.select(".tgme_widget_message_photo_wrap"):
            match = re.search(
                r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)",
                photo.get("style", ""),
            )
            if match:
                media.append({"type": "photo", "url": match.group(1)})

        news[number] = {
            "id": post_id,
            "title": parts[0],
            "paragraphs": parts[1:],
            "published": date_tag.get("datetime", "") if date_tag else "",
            "link": "https://t.me/" + post_id,
            "media": media,
            "media_sent": False,
        }

    if not numbers:
        raise RuntimeError("Telegram returned no channel posts; refusing to move the bookmark")

    older_link = soup.select_one("a.tme_messages_more[data-before]")
    cursor_text = older_link.get("data-before", "") if older_link else ""
    next_before = int(cursor_text) if cursor_text.isdecimal() else None
    return news, numbers, next_before


def fetch_since(last_seen_id):
    """Backfill every visible post newer than last_seen_id, page by page.

    Returns (new_items, newest_id, complete, pages_scanned).
    Never call the scan complete unless an older page crosses the bookmark.
    """
    collected = {}
    scanned_ids = set()
    before = None
    newest_id = None
    complete = False
    pages_scanned = 0

    for index in range(MAX_HISTORY_PAGES):
        page, numbers, next_before = fetch_telegram_page(before)
        pages_scanned += 1
        if index == 0:
            newest_id = max(numbers)
        if scanned_ids and not (set(numbers) - scanned_ids):
            raise RuntimeError("Telegram history pagination made no progress; bookmark unchanged")
        scanned_ids.update(numbers)

        for number, item in page.items():
            if number > last_seen_id:
                collected[number] = item

        if min(numbers) <= last_seen_id or newest_id <= last_seen_id:
            complete = True
            break

        if next_before is None or next_before > min(numbers):
            break
        if before is not None and next_before >= before:
            break
        before = next_before
        if index + 1 < MAX_HISTORY_PAGES:
            time.sleep(PAGE_DELAY_SECONDS)

    ordered = [collected[number] for number in sorted(collected)]
    return ordered, newest_id, complete, pages_scanned


def highest_known_id(state):
    channel = channel_name()
    values = []
    sources = [
        state.get("sent_ids", []),
        state.get("skipped_ids", []),
        list(state.get("pending", {}).keys()),
    ]
    for source in sources:
        for post_id in source:
            number = number_from_post_id(post_id, channel)
            if number is not None:
                values.append(number)
    return max(values, default=None)


def upgrade_state(state):
    """Keep v1 pending intact; archive RSS-era pending once on initial switch."""
    if state.get("mode") == MODE and isinstance(state.get("last_seen_post_id"), int):
        return False

    if state.get("mode") in ("gemini_batch_v1", MODE):
        anchor = state.get("last_seen_post_id") or highest_known_id(state)
        if anchor is not None:
            state["last_seen_post_id"] = int(anchor)
            state["mode"] = MODE
            save_state(state)
            print("Updated existing Gemini state; history bookmark:", anchor)
            return False

    # Switching from the old RSS matching version, or a new empty state.
    page, numbers, _ = fetch_telegram_page()
    old_pending = state.get("pending", {})
    state.setdefault("sent_ids", [])
    state.setdefault("alerts_sent", [])
    state["legacy_pending"] = old_pending
    skipped = set(state.get("skipped_ids", []))
    skipped.update(old_pending)
    skipped.update(item["id"] for item in page.values())
    state["skipped_ids"] = sorted(skipped)
    state["pending"] = {}
    state["last_seen_post_id"] = max(numbers)
    state["mode"] = MODE
    state["initialized"] = True
    save_state(state)
    print("First migration: archived", len(old_pending),
          "legacy items and skipped existing preview posts.")
    return True


def english_parts(item):
    title = (item.get("title") or "").strip()
    paragraphs = [p.strip() for p in item.get("paragraphs", [])
                  if isinstance(p, str) and p.strip()]
    if title and paragraphs and paragraphs[0] == title:
        paragraphs.pop(0)
    parts = ([title] if title else []) + paragraphs
    if not parts:
        raise ValueError("News has no English text: " + item.get("id", ""))
    return parts


def utf16_length(text):
    return len(text.encode("utf-16-le")) // 2


def split_text(text, limit):
    pieces = []
    current = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > limit and current:
            pieces.append("".join(current))
            current = []
            units = 0
        current.append(char)
        units += width
    if current:
        pieces.append("".join(current))
    return pieces


def message_chunks(item):
    english = english_parts(item)
    chinese = item.get("zh_parts")
    if not isinstance(chinese, list) or len(english) != len(chinese):
        raise ValueError("Translation not ready or paragraph count mismatch")

    chunks = []
    current = ""
    for en, zh in zip(english, chinese):
        pair = en + "\n\n" + zh
        for fragment in split_text(pair, TEXT_CHUNK_SIZE):
            candidate = current + "\n\n" + fragment if current else fragment
            if utf16_length(candidate) > TEXT_CHUNK_SIZE:
                if current:
                    chunks.append(current)
                current = fragment
            else:
                current = candidate
    if current:
        chunks.append(current)
    if not chunks:
        raise ValueError("News body is empty")

    note = "\n\n\U0001f916 \u4e2d\u6587\u7531 Gemini \u7ffb\u8bd1"
    if utf16_length(chunks[-1] + note) <= TEXT_CHUNK_SIZE:
        chunks[-1] += note
    else:
        chunks.append(note.strip())
    return chunks


def post_telegram(method, *, data=None, files=None, timeout=45):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("Missing TELEGRAM_BOT_TOKEN or TELEGRAM_CHAT_ID")
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
            data=data, files=files, timeout=timeout,
        )
    except requests.RequestException as exc:
        # Never log the request URL; it embeds the private Telegram bot token.
        raise RuntimeError(f"Telegram {method} network error: {type(exc).__name__}") from None
    try:
        result = response.json()
    except ValueError:
        raise RuntimeError(f"Telegram {method} non-JSON response: HTTP {response.status_code}") from None
    if not response.ok or not result.get("ok"):
        description = str(result.get("description", "Unknown error"))
        retry_after = result.get("parameters", {}).get("retry_after")
        raise RuntimeError(
            f"Telegram {method} HTTP {response.status_code}: {description}; "
            f"retry_after={retry_after}"
        )
    return result["result"]


def send_photo(url):
    photo = requests.get(url, timeout=30)
    photo.raise_for_status()
    return post_telegram(
        "sendPhoto", data={"chat_id": CHAT_ID},
        files={"photo": ("news.jpg", photo.content, "image/jpeg")}, timeout=75,
    )


def send_album(photos):
    files = {}
    media = []
    for index, item in enumerate(photos):
        photo = requests.get(item["url"], timeout=30)
        photo.raise_for_status()
        key = f"photo{index}"
        files[key] = (f"photo{index}.jpg", photo.content, "image/jpeg")
        media.append({"type": "photo", "media": f"attach://{key}"})
    return post_telegram(
        "sendMediaGroup", data={"chat_id": CHAT_ID, "media": json.dumps(media)},
        files=files, timeout=90,
    )


def deliver_one(state, news_id):
    item = state["pending"][news_id]
    if "zh_parts" not in item:
        return False

    photos = [x for x in item.get("media", []) if x.get("type") == "photo"]
    cursor = item.get("media_next_index", 0)
    if not item.get("media_sent", False):
        while cursor < len(photos):
            group = photos[cursor:cursor + 10]
            try:
                if len(group) == 1:
                    send_photo(group[0]["url"])
                else:
                    send_album(group)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                item["media_failures"] = item.get("media_failures", 0) + 1
                print("Photo upload failed:", news_id, type(exc).__name__)
                if item["media_failures"] < PHOTO_MAX_FAILURES:
                    save_state(state)
                    return False
                print("Skipping expired photo after repeated failures:", news_id)
                item["media_skipped"] = True
                break
            cursor += len(group)
            item["media_next_index"] = cursor
            item["media_failures"] = 0
            save_state(state)
        item["media_sent"] = True
        save_state(state)

    pieces = message_chunks(item)
    text_cursor = item.get("text_next_index", 0)
    while text_cursor < len(pieces):
        try:
            post_telegram(
                "sendMessage",
                data={"chat_id": CHAT_ID, "text": pieces[text_cursor]},
                timeout=45,
            )
        except RuntimeError as exc:
            print("Text send failed:", news_id, str(exc))
            return False
        text_cursor += 1
        item["text_next_index"] = text_cursor
        save_state(state)

    if news_id not in state["sent_ids"]:
        state["sent_ids"].append(news_id)
    state["pending"].pop(news_id)
    save_state(state)
    print("Sent:", news_id)
    return True


def pending_order(pair):
    news_id, item = pair
    return (parse_time(item.get("published")) or
            parse_time(item.get("first_seen_at")) or datetime.max.replace(tzinfo=timezone.utc),
            news_id)


def main():
    state = load_state()
    if upgrade_state(state):
        return

    state.setdefault("sent_ids", [])
    state.setdefault("pending", {})
    state.setdefault("skipped_ids", [])
    watermark = state["last_seen_post_id"]
    recent, newest, complete, page_count = fetch_since(watermark)
    print(f"Scanned {page_count} Telegram pages; found {len(recent)} posts after {watermark}.")

    sent_ids = set(state["sent_ids"])
    skipped_ids = set(state["skipped_ids"])
    discovered = now_utc().isoformat()
    new_count = 0
    for item in recent:
        news_id = item["id"]
        if news_id in sent_ids or news_id in skipped_ids or news_id in state["pending"]:
            continue
        item["first_seen_at"] = discovered
        item["media_next_index"] = 0
        item["text_next_index"] = 0
        state["pending"][news_id] = item
        new_count += 1

    # The watermark advances ONLY after reaching the old watermark.
    if complete:
        state["last_seen_post_id"] = max(watermark, newest)
    save_state(state)
    print(f"Added {new_count}; pending {len(state['pending'])}; bookmark {state['last_seen_post_id']}.")
    if not complete:
        raise RuntimeError(
            "Telegram history backfill incomplete. Pending saved, bookmark NOT advanced. "
            "Increase MAX_HISTORY_PAGES or inspect Telegram preview pagination."
        )

    batch_count = 0
    sent_count = 0
    while sent_count < MAX_SEND_PER_RUN:
        ordered = sorted(state["pending"].items(), key=pending_order)
        if not ordered:
            break

        news_id, item = ordered[0]
        if "zh_parts" not in item:
            if batch_count >= MAX_BATCHES_PER_RUN:
                print("Reached Gemini batch budget for this run; remaining news kept in pending.")
                break
            untranslated = [(key, value) for key, value in ordered if "zh_parts" not in value]
            capacity = min(BATCH_SIZE, MAX_SEND_PER_RUN - sent_count)
            batch = untranslated[:capacity]
            payload = [{"id": key, "parts": english_parts(value)} for key, value in batch]
            print(f"Gemini request {batch_count + 1}: translating {len(payload)} news posts.")
            try:
                translations = translate_news_batch(payload)
            except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
                raise RuntimeError(f"Gemini translation failed; pending preserved: {exc}") from exc
            for key, _ in batch:
                state["pending"][key]["zh_parts"] = translations[key]
            save_state(state)
            batch_count += 1
            continue

        if not deliver_one(state, news_id):
            raise RuntimeError("Telegram delivery unfinished; pending progress saved")
        sent_count += 1

    print(f"Run finished: sent {sent_count}; left {len(state['pending'])} pending; "
          f"Gemini requests {batch_count}.")


if __name__ == "__main__":
    main()
