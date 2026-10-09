import os
import json
import requests
import feedparser
from pathlib import Path
from bs4 import BeautifulSoup
from datetime import datetime
from email.utils import parsedate_to_datetime

STATE_FILE = Path("state.json")

"""----------------读取英文tg、中文rss网址及bot_token等信息----------------"""
TG_EN_URL = os.getenv("TG_EN_URL", "").strip()
"""
  读取 TG_EN_URL 这个变量的值，找不到就返回 ""
  后面的 strip 作用是去掉字符串前后的空格、换行
"""
RSS_ZH_URL = os.getenv("RSS_ZH_URL", "").strip()
BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "").strip()

"""---------------------读取、修改、保存JSON---------------------"""
def load_state():
    # json 文件转化为 python 字典，这里需要return是因为要把数据拿回来
    with STATE_FILE.open("r", encoding="utf-8") as f: # with STATE_FILE as f 即打开 STATE_FILE 并暂时把它叫做 f
        state = json.load(f) # 从文件 f 中读取 JSON，并转换成 Python 数据，放到 state 变量里面
    return state

def save_state(state):
    # 将信息写入json文件，这里不需要return是因为它只把state中的内容写入json，并不用返回什么
    with STATE_FILE.open("w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2) # indent=2可以自动给json排版，2的意思是每一级缩进两个空格

def get_article_content(article_url):
    # 获取文章正文
    # 按照 HTML 规则解析 article_response.text
    article_response = requests.get(article_url, timeout=30)
    article_response.raise_for_status()
    """Soup 获取网页内的正文，将正文内容存储在 paragraphs 中"""
    soup = BeautifulSoup(
        article_response.text,
        "html.parser"
    )
    title_tag = soup.select_one("article.reading-shell.article-section h1")
    title = title_tag.get_text(" ", strip=True) if title_tag else ""

    article_paragraph_tags = soup.select("article.reading-shell.article-section div.msg-prose > p")
    paragraphs = []
    for p in article_paragraph_tags:
        text = p.get_text(" ", strip=True)
        if not text:
            continue
        if text.startswith("🌸"):
            continue
        paragraphs.append(text)
    return title, paragraphs

def get_chinese_news_metadata(n):
    # 获取 n 个中文新闻，n <= 50
    # 获取 rss 池里文章的id、标题、发布时间、文章链接
    response = requests.get(RSS_ZH_URL, timeout=30)
    response.raise_for_status()  # 如果http状态码不是200，直接报错
    feed = feedparser.parse(response.content)
    chinese_news = []
    for entry in feed.entries[:n]:
        # 将新闻id、标题、链接、发布时间存储在字典中，后面会在匹配到中文新闻后将title替换，并补充正文
        news_item = {
            "id": entry.get("id", ""),
            "title": entry.get("title", ""),
            "link": entry.get("link", ""),
            "published": entry.get("published", ""),
        }
        chinese_news.append(news_item)
    return chinese_news

def get_english_news(n):
    # 读取tg页面上的英文消息
    english_response = requests.get(TG_EN_URL, timeout=30)
    english_response.raise_for_status()  # 如果http状态码不是200，直接报错

    english_soup = BeautifulSoup(english_response.text, "html.parser")
    message_tags = english_soup.select(".tgme_widget_message") # 消息池中的所有新闻，是一个列表

    english_news = []
    for message in message_tags[-n:]:
        post_id = message.get("data-post", "")
        message_link = "https://t.me/" + post_id

        time_element = message.select_one(".tgme_widget_message_date time[datetime]")
        if time_element:
            published = time_element.get("datetime", "").strip()
        else:
            published = ""

        text_tag = message.select_one(".tgme_widget_message_text")
        if not text_tag:
            continue
        for br in text_tag.find_all("br"):
            br.replace_with("\n") # 把正文每段前后的 <br> 替换为换行符

        text = text_tag.get_text()
        parts = text.split("\n\n")
        paragraphs = []
        for part in parts:
            part = part.strip() # 提取每段内容并去掉空格
            if not part:
                continue
            if part.startswith("🌸"):
                continue
            paragraphs.append(part)
            if len(paragraphs) >= 2:
                title = paragraphs[0]
                body_paragraphs = paragraphs[1:]
            elif len(paragraphs) == 1: # 防止出现只有一句话导致标题数量为1，段落数量为0的情况
                title = paragraphs[0]
                body_paragraphs = paragraphs.copy()
            else:
                title = ""
                body_paragraphs = []
        news_item = {
            "id": post_id,
            "title": title,
            "link": message_link,
            "published": published,
            "paragraphs": body_paragraphs
        } # 保持与中文相同的结构，先把每条新闻组装成字典，再放入english_news新闻列表中
        english_news.append(news_item)
    return english_news

def parse_published_time(published, language):
    # 把时间转换为真实时间
    if not published:
        return None
    if language == 'zh':
        time = parsedate_to_datetime(published)
    elif language == 'en':
        time = datetime.fromisoformat(published)
    else:
        return None
    return time

def get_time_difference_seconds(zh_time, en_time):
    # 计算中英新闻时间差
    if zh_time is None or en_time is None:
        return None
    difference_seconds = abs((en_time - zh_time).total_seconds())
    return difference_seconds

def find_best_match(english_item, chinese_news, max_time_difference=60):
    # 传入参数为一条英文和中文列表
    # 在中文新闻列表中寻找与某一篇英文新闻发布时间最接近的中文新闻。
    best_match = None
    best_difference = None
    en_time = parse_published_time(english_item["published"], 'en')
    if en_time is None:
        return None, None

    for chinese_item in chinese_news:
        zh_time = parse_published_time(chinese_item["published"], 'zh')
        difference_seconds = get_time_difference_seconds(zh_time, en_time)
        if difference_seconds is None:
            continue
        if best_difference is None or difference_seconds < best_difference:
            best_difference = difference_seconds
            best_match = chinese_item

    if best_match is None or best_difference > max_time_difference:
        return None, None
    return best_match, best_difference

def send_telegram_message(text):
    # 给机器人发送消息
    if not BOT_TOKEN:
        print("未读取到BOT_TOKEN")
        return False
    if not CHAT_ID:
        print("未读取到CHAT_ID")
        return False

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage" # fstring
    data = {"chat_id": CHAT_ID, "text": text}

    try:
        response = requests.post(url, data=data, timeout=30)
        # 尝试读取 Telegram 返回的 JSON
        try:
            result = response.json()
        except ValueError:
            print("Telegram 返回内容不是有效 JSON，HTTP状态码：", response.status_code)
            return False

        # Telegram 明确说明发送成功
        if result.get("ok"):
            print("Telegram发送成功")
            return True
        else:
            print("Telegram发送失败：", result.get("description", f"HTTP {response.status_code}"))
            return False

    except requests.exceptions.Timeout:
        print("Telegram 发送失败：连接超时")
        return False

    except requests.exceptions.SSLError as e:
        print("Telegram 发送失败：SSL连接异常", str(e))
        return False

    except requests.exceptions.ConnectionError as e:
        print("Telegram 发送失败：网络连接异常", str(e))
        return False

    except requests.exceptions.RequestException as e:
        print("Telegram 发送失败：请求异常", str(e))
        return False

def format_bilingual_message(english_item, chinese_item):
    en_parts = english_item["paragraphs"]
    if not en_parts: # 单段消息
        en_parts = [english_item["title"]] # 只发送这一段即可
    elif en_parts[0] != english_item["title"]: # 正常多段消息
        en_parts = [english_item["title"]] + en_parts # 组合成标题+内容

    zh_parts = [chinese_item["title"]] + chinese_item["paragraphs"]
    message_parts = []
    if len(en_parts) != len(zh_parts):
        print("中英文段落数量不一致，采用整段双语排版")
        message_parts = [en_parts[0], zh_parts[0]]
        if len(en_parts) > 1:
            message_parts.append("\n\n".join(en_parts[1:]))
        if len(zh_parts) > 1:
            message_parts.append("\n\n".join(zh_parts[1:]))
        message = "\n\n".join(message_parts)
        return message
    for en_part, zh_part in zip(en_parts, zh_parts):
        message_parts.append(en_part)
        message_parts.append(zh_part)
    message = "\n\n".join(message_parts)
    return message

def send_status_message(message):
    text = "🤖 Bot运行通知\n\n" + message
    return send_telegram_message(text)

def main():
    state = load_state()
    """"初始化，将已在消息池内的消息略过，等待处理下一条新消息"""
    english_news = get_english_news(10)
    if not state["initialized"]:
        for english_item in english_news:
            english_id = english_item["id"]
            if english_id not in state["sent_ids"]:
                state["sent_ids"].append(english_id)
        state["initialized"] = True
        save_state(state)
        print("首次初始化完成")
        return # 这里的return表示main()到这里直接结束，不会运行下面的部分（目的就是为了把现在消息池里面已有的消息标为已处理）

    """初始化结束后的第二次运行，就执行下面的代码，如果有新消息传入，就在设置的3条中文消息中进行匹配"""
    for english_item in english_news:
        # 将已发送和未发送的消息id分别放入sent_ids和pending
        english_id = english_item["id"]
        if english_id in state["sent_ids"]:
            continue
        if english_id in state["pending"]:
            if english_item.get("published"):
                state["pending"][english_id]["published"] = (english_item["published"])
            continue
        state["pending"][english_id] = english_item

    if not state["pending"]:
        print("当前没有待匹配的英文消息")
        save_state(state)
        return

    chinese_news = get_chinese_news_metadata(50)
    pending_items = list(state["pending"].items()) # 创建 pending 的快照

    for english_id, english_item in pending_items:
        if not english_item.get("published"):
            alert_key = english_id + ":time_error"
            if alert_key not in state["alerts_sent"]:
                error_message = "⚠️ 英文消息发布时间提取失败\n英文ID：" + english_id
                alert_success = send_status_message(error_message)
                if alert_success:
                    state["alerts_sent"].append(alert_key)
        best_match, best_difference = find_best_match(english_item, chinese_news)
        if best_match:
            zh_title, paragraphs = get_article_content(best_match["link"])
            best_match["title"] = zh_title
            best_match["paragraphs"] = paragraphs
            message = format_bilingual_message(english_item, best_match)
            if message is None:
                alert_key = english_id + ":format_error"
                if alert_key not in state["alerts_sent"]:
                    error_message = "❌ 双语消息格式化失败\n英文ID：" + english_id + "\n中文ID：" + best_match["id"]
                    alert_success = send_status_message(error_message)
                    if alert_success:
                        state["alerts_sent"].append(alert_key)
                continue
            success = send_telegram_message(message)
            if success:
                state["sent_ids"].append(english_id)
                state["pending"].pop(english_id)
            else:
                print("发送失败，保留在pending：", english_id)
        else:
            alert_key = english_id + ":no_match"
            if alert_key not in state["alerts_sent"]:
                error_message = "⚠️ 暂未找到对应中文\n英文ID：" + english_id
                alert_success = send_status_message(error_message)
                if alert_success:
                    state["alerts_sent"].append(alert_key)
            continue
    save_state(state)

if __name__ == "__main__":
    main()
