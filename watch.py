"""
Watch ben-ran.timepad.ru events page for new active events.

Strategy:
  - Fetch /events/all/page/N/ (server-rendered HTML)
  - Parse div.t-card_event cards
  - Exclude cards with class 't-card_event__passed' (past events)
  - Track by numeric event id (from /event/<id>/ URL)
  - Send Telegram message when a new active id appears

Env vars:
  TELEGRAM_TOKEN   - bot token
  TELEGRAM_CHAT_ID - chat id
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup, Tag

SITE_LABEL = "БЕН РАН (Timepad)"
BASE_URL = "https://ben-ran.timepad.ru/events/"
PAGE_URL = "https://ben-ran.timepad.ru/events/all/page/{page}/"

STATE_FILE = Path(__file__).resolve().parent / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8,"
        "application/signed-exchange;v=b3;q=0.7"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
    "Sec-Ch-Ua": "\"Chromium\";v=\"124\", \"Google Chrome\";v=\"124\", \"Not-A.Brand\";v=\"99\"",
    "Sec-Ch-Ua-Mobile": "?0",
    "Sec-Ch-Ua-Platform": "\"Windows\"",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Upgrade-Insecure-Requests": "1",
    "Referer": "https://ben-ran.timepad.ru/",
}

EVENT_ID_RE = re.compile(r"/event/(\d+)/?")


def fetch_url(url: str) -> str:
    last_err = None
    for attempt in range(3):
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:  # noqa: BLE001
            last_err = e
            time.sleep(2 + attempt * 2)
    raise RuntimeError(f"Failed to fetch {url}: {last_err}")


def total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
    # Pagination links: /events/all/page/N/
    nums = set()
    for a in soup.find_all("a", href=True):
        m = re.search(r"/events/all/page/(\d+)/?", a["href"])
        if m:
            nums.add(int(m.group(1)))
    if not nums:
        return 1
    return max(nums)


def parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[dict] = []
    for card in soup.select("div.t-card.t-card_event, div.t-card_event"):
        classes = card.get("class") or []
        if "t-card_event__passed" in classes:
            # past event — skip
            continue
        # Find a link to /event/<id>/
        link = None
        eid = None
        for a in card.find_all("a", href=True):
            m = EVENT_ID_RE.search(a["href"])
            if m:
                eid = m.group(1)
                link = a["href"]
                break
        if not eid:
            continue
        # Title: prefer h1/h2/h3 text
        title = None
        for h in card.select("h1, h2, h3, h4"):
            t = h.get_text(" ", strip=True)
            if t:
                title = t
                break
        if not title:
            # Fallback: longest link text
            for a in card.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if t and (not title or len(t) > len(title)):
                    title = t
        if not title:
            continue
        # Normalize URL
        if link.startswith("/"):
            url = "https://ben-ran.timepad.ru" + link
        else:
            url = link
        out.append({"id": eid, "title": title, "url": url})
    return out


def fetch_all_active() -> list[dict]:
    page1 = fetch_url(PAGE_URL.format(page=1))
    pages = min(total_pages(page1), 20)
    items = parse_cards(page1)
    for p in range(2, pages + 1):
        try:
            html = fetch_url(PAGE_URL.format(page=p))
        except Exception as e:  # noqa: BLE001
            print(f"page {p} failed: {e}", file=sys.stderr)
            return items
        items.extend(parse_cards(html))
        time.sleep(1)
    # Dedup by id
    seen_ids: set[str] = set()
    unique: list[dict] = []
    for it in items:
        if it["id"] in seen_ids:
            continue
        seen_ids.add(it["id"])
        unique.append(it)
    return unique


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {"seen": [], "last_run": None, "last_count": 0}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def send_telegram(text: str) -> None:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        print("TELEGRAM_TOKEN/TELEGRAM_CHAT_ID not set; skipping send", file=sys.stderr)
        return
    api = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    r = requests.post(api, data={
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }, timeout=20)
    if not r.ok:
        print(f"Telegram error {r.status_code}: {r.text}", file=sys.stderr)
        r.raise_for_status()


def escape_html(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_message(events: list[dict], total_active: int) -> str:
    lines = [f"<b>🆕 Новые события на {SITE_LABEL} — {len(events)} шт.</b>"]
    lines.append(f"Всего активных: {total_active}")
    lines.append("")
    for e in events[:40]:
        title = escape_html(e["title"])
        lines.append(f"• <a href=\"{e['url']}\">{title}</a>")
    if len(events) > 40:
        lines.append("")
        lines.append(f"…и ещё {len(events) - 40} (см. сайт)")
    return "\n".join(lines)


def main() -> int:
    state = load_state()
    seen = set(str(x) for x in state.get("seen", []))
    bootstrap = not seen

    try:
        items = fetch_all_active()
    except Exception as e:  # noqa: BLE001
        print(f"Fetch failed: {e}", file=sys.stderr)
        return 2

    if not items and state.get("last_count", 0) > 3:
        print(
            f"Suspicious: 0 items but state had {state.get('last_count')}. "
            "Aborting without state update.",
            file=sys.stderr,
        )
        return 3

    current_ids = {it["id"] for it in items}
    by_id = {it["id"]: it for it in items}
    new_ids = current_ids - seen

    print(f"Active: {len(items)}, new: {len(new_ids)}")

    if bootstrap:
        print("First run — recording state without notifications")
    elif new_ids:
        new_events = [by_id[i] for i in new_ids]
        send_telegram(format_message(new_events, len(items)))

    state["seen"] = sorted(current_ids)
    state["last_run"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    state["last_count"] = len(items)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
