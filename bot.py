"""英文 Telegram 新闻 -> 5 条微缓冲 -> Gemini 批量翻译 -> 双语逐段 Telegram 推送。"""
import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from gemini_translate import translate_news_batch

STATE_FILE = Path(__file__).resolve().parent / "state.json"
TG_EN_URL = os.getenv("TG_EN_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

MODE = "gemini_batch_v1"
BATCH_SIZE = 5
MAX_WAIT_MINUTES = 30  # 凑不满 5 条，最早一条等待超过 30 分钟后也翻译
MAX_BATCHES_PER_RUN = 2  # 每次 Actions 最多调用 Gemini 两次
MAX_SEND_PER_RUN = 10    # 每次最多完成 10 条新闻，防止恢复后刷屏
TEXT_CHUNK_SIZE = 3500  # 比 Telegram 4096 字符限制留足余量


def now_utc():
    return datetime.now(timezone.utc)


def parse_time(value):
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


def save_state(state):
    # 写入临时文件后原子替换，避免程序异常时破坏本地 state.json
    temporary = STATE_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(STATE_FILE)


def load_state():
    if not STATE_FILE.exists():
        return {"initialized": False, "sent_ids": [], "pending": {}, "alerts_sent": []}
    return json.loads(STATE_FILE.read_text(encoding="utf-8"))


def get_english_news(n=20):
    if not TG_EN_URL:
        raise RuntimeError("未配置 TG_EN_URL")
    response = requests.get(TG_EN_URL, timeout=30)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")
    messages = soup.select(".tgme_widget_message[data-post]")
    news_list = []

    for message in messages[-n:]:
        post_id = (message.get("data-post") or "").strip()
        text_tag = message.select_one(".tgme_widget_message_text")
        if not post_id or text_tag is None:
            continue
        for br in text_tag.find_all("br"):
            br.replace_with("\n")
        raw = text_tag.get_text()
        parts = [p.strip() for p in raw.split("\n\n") if p.strip() and not p.strip().startswith("🌸")]
        parts = [p for p in parts if p.casefold() not in {
            "flower community's assistant", "hua community's assistant"
        }]
        if not parts:
            continue
        date_tag = message.select_one(".tgme_widget_message_date time[datetime]")
        media = []
        for photo in message.select(".tgme_widget_message_photo_wrap"):
            style = photo.get("style", "")
            match = re.search(r"background-image\s*:\s*url\(\s*['\"]?([^'\")]+)", style)
            if match:
                media.append({"type": "photo", "url": match.group(1)})

        news_list.append({
            "id": post_id,
            "title": parts[0],
            "paragraphs": parts[1:],
            "published": date_tag.get("datetime", "") if date_tag else "",
            "link": "https://t.me/" + post_id,
            "media": media,
            "media_sent": False,
        })
    return news_list


def english_parts(item):
    title = (item.get("title") or "").strip()
    paragraphs = [p.strip() for p in item.get("paragraphs", [])
                  if isinstance(p, str) and p.strip()]
    if title and paragraphs and paragraphs[0] == title:
        paragraphs.pop(0)
    parts = ([title] if title else []) + paragraphs
    if not parts:
        raise ValueError("新闻正文为空：" + item.get("id", ""))
    return parts


def migrate_legacy_once(state, current_news):
    """首次切换时归档旧 pending，并跳过当前页面已有消息，避免历史消息集中补发。"""
    if state.get("mode") == MODE:
        return False
    old_pending = state.get("pending", {})
    state["legacy_pending"] = old_pending  # 保留旧数据，不丢弃
    skipped = set(state.get("skipped_ids", []))
    skipped.update(old_pending)
    sent = set(state.get("sent_ids", []))
    skipped.update(x["id"] for x in current_news if x["id"] not in sent)
    state["skipped_ids"] = sorted(skipped)
    state["pending"] = {}
    state["mode"] = MODE
    state["initialized"] = True
    save_state(state)
    print(f"首次切换完成：归档旧 pending {len(old_pending)} 条；跳过当前频道历史消息。")
    return True


def post_telegram(method, *, data=None, files=None, timeout=45):
    if not BOT_TOKEN or not CHAT_ID:
        raise RuntimeError("缺少 TELEGRAM_BOT_TOKEN 或 TELEGRAM_CHAT_ID")
    response = requests.post(
        f"https://api.telegram.org/bot{BOT_TOKEN}/{method}",
        data=data, files=files, timeout=timeout,
    )
    response.raise_for_status()
    result = response.json()
    if not result.get("ok"):
        raise RuntimeError(f"Telegram {method} 失败：{result.get('description')}")
    return result["result"]


def send_photo(url):
    image = requests.get(url, timeout=30)
    image.raise_for_status()
    return post_telegram(
        "sendPhoto", data={"chat_id": CHAT_ID},
        files={"photo": ("news.jpg", image.content, "image/jpeg")}, timeout=75,
    )


def send_album(items):
    files = {}
    album = []
    for index, item in enumerate(items):
        image = requests.get(item["url"], timeout=30)
        image.raise_for_status()
        key = f"photo{index}"
        files[key] = (f"photo{index}.jpg", image.content, "image/jpeg")
        album.append({"type": "photo", "media": f"attach://{key}"})
    return post_telegram(
        "sendMediaGroup",
        data={"chat_id": CHAT_ID, "media": json.dumps(album)},
        files=files, timeout=90,
    )


def message_chunks(english_item):
    en_parts = english_parts(english_item)
    zh_parts = english_item.get("zh_parts")
    if not isinstance(zh_parts, list) or len(en_parts) != len(zh_parts):
        raise ValueError("译文尚未准备好或段落数量不一致")

    pair_texts = [f"{en}\n\n{zh}" for en, zh in zip(en_parts, zh_parts)]
    chunks = []
    current = ""
    for pair in pair_texts:
        # 单组中英文本过长时按字符分片，避免 Telegram 的 4096 字符限制
        fragments = [pair[i:i + TEXT_CHUNK_SIZE] for i in range(0, len(pair), TEXT_CHUNK_SIZE)]
        for fragment in fragments:
            candidate = f"{current}\n\n{fragment}" if current else fragment
            if len(candidate) > TEXT_CHUNK_SIZE:
                if current:
                    chunks.append(current)
                current = fragment
            else:
                current = candidate

    if current:
        chunks.append(current)
    note = "\n\n🤖 中文由 Gemini 翻译"
    if len(chunks[-1]) + len(note) <= TEXT_CHUNK_SIZE:
        chunks[-1] += note
    else:
        chunks.append(note.strip())
    return chunks


def deliver_one(state, news_id):
    """逐步发送并保存进度。成功完成时返回 True，否则返回 False。"""
    item = state["pending"][news_id]
    if "zh_parts" not in item:
        return False

    photos = [m for m in item.get("media", []) if m.get("type") == "photo"]
    media_index = item.get("media_next_index", 0)
    if not item.get("media_sent", False):
        while media_index < len(photos):
            count = min(10, len(photos) - media_index)
            group = photos[media_index:media_index + count]
            try:
                if len(group) == 1:
                    send_photo(group[0]["url"])
                else:
                    send_album(group)
            except (requests.RequestException, RuntimeError, ValueError) as exc:
                item["media_failures"] = item.get("media_failures", 0) + 1
                print(f"图片发送失败 {news_id}，第 {item['media_failures']} 次：{exc}")
                if item["media_failures"] < 3:
                    save_state(state)
                    return False
                # 图片链接可能过期。连续三次失败后只发送文字，避免新闻永远积压。
                print(f"已连续三次发送图片失败，跳过此条剩余图片：{news_id}")
                item["media_skipped"] = True
                break
            media_index += count
            item["media_next_index"] = media_index
            item["media_failures"] = 0
            save_state(state)
        item["media_sent"] = True
        save_state(state)

    chunks = message_chunks(item)
    cursor = item.get("text_next_index", 0)
    while cursor < len(chunks):
        try:
            post_telegram("sendMessage", data={"chat_id": CHAT_ID, "text": chunks[cursor]}, timeout=40)
        except (requests.RequestException, RuntimeError, ValueError) as exc:
            print(f"双语文字发送失败 {news_id}，第 {cursor + 1} 段：{exc}")
            return False
        cursor += 1
        item["text_next_index"] = cursor
        save_state(state)

    if news_id not in state["sent_ids"]:
        state["sent_ids"].append(news_id)
    state["pending"].pop(news_id, None)
    save_state(state)
    print("成功推送：", news_id)
    return True


def main():
    state = load_state()
    recent = get_english_news(20)
    print("本次抓取英文消息：", len(recent))

    if migrate_legacy_once(state, recent):
        return

    state.setdefault("sent_ids", [])
    state.setdefault("skipped_ids", [])
    state.setdefault("pending", {})
    sent_ids = set(state["sent_ids"])
    skipped_ids = set(state["skipped_ids"])
    first_seen = now_utc().isoformat()
    new_count = 0
    for item in recent:
        news_id = item["id"]
        if news_id in sent_ids or news_id in skipped_ids or news_id in state["pending"]:
            continue
        item["first_seen_at"] = first_seen
        item["media_next_index"] = 0
        item["text_next_index"] = 0
        state["pending"][news_id] = item
        new_count += 1
    save_state(state)
    print(f"新增待处理新闻：{new_count}，待处理总数：{len(state['pending'])}")

    # 先恢复上次已完成翻译但尚未成功推送的新闻，不再浪费 Gemini 调用。
    send_count = 0
    order = lambda pair: (
        parse_time(pair[1].get("published")) or parse_time(pair[1].get("first_seen_at")) or now_utc(),
        pair[0]
    )
    for news_id, item in sorted(list(state["pending"].items()), key=order):
        if send_count >= MAX_SEND_PER_RUN:
            break
        if "zh_parts" in item:
            if not deliver_one(state, news_id):
                print("已有译文推送未完成，停止本轮后续请求")
                return
            send_count += 1

    batch_count = 0
    while batch_count < MAX_BATCHES_PER_RUN and send_count < MAX_SEND_PER_RUN:
        untranslated = sorted(
            [(news_id, item) for news_id, item in state["pending"].items() if "zh_parts" not in item],
            key=order,
        )
        if not untranslated:
            break
        oldest = parse_time(untranslated[0][1].get("first_seen_at")) or now_utc()
        minutes = (now_utc() - oldest).total_seconds() / 60
        if len(untranslated) < BATCH_SIZE and minutes < MAX_WAIT_MINUTES:
            print(f"微缓冲池：{len(untranslated)}/{BATCH_SIZE} 条，最早等待 {minutes:.1f}/{MAX_WAIT_MINUTES} 分钟")
            break
        batch = untranslated[:min(BATCH_SIZE, MAX_SEND_PER_RUN - send_count)]
        payload = [{"id": news_id, "parts": english_parts(item)} for news_id, item in batch]
        print(f"本次调用 Gemini：合并 {len(payload)} 条新闻")
        try:
            translations = translate_news_batch(payload)
        except (requests.RequestException, RuntimeError, ValueError, KeyError) as exc:
            print("Gemini 批量翻译失败，保留待处理消息，下次重试：", exc)
            return

        for news_id, _ in batch:
            state["pending"][news_id]["zh_parts"] = translations[news_id]
        save_state(state)  # 翻译结果先落盘，之后 Telegram 发送失败不必再次调用 Gemini
        batch_count += 1

        for news_id, _ in batch:
            if not deliver_one(state, news_id):
                print("消息发送未完成，留到下次继续")
                return
            send_count += 1

    print(f"本轮完成推送 {send_count} 条，剩余待处理 {len(state['pending'])} 条")


if __name__ == "__main__":
    main()
