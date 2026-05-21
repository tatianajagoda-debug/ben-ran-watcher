"""
Watch ben-ran.timepad.ru events page for new active events.

Strategy:
  - Use Playwright (headless Chromium) to fetch /events/all/page/N/
    — bypasses Cloudflare anti-bot that blocks plain requests
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
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright

SITE_LABEL = "БЕН РАН (Timepad)"
PAGE_URL = "https://ben-ran.timepad.ru/events/all/page/{page}/"

STATE_FILE = Path(__file__).resolve().parent / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

EVENT_ID_RE = re.compile(r"/event/(\d+)/?")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def fetch_pages_with_playwright() -> list[str]:
    """Visit pages 1..N and return their rendered HTML."""
    htmls: list[str] = []
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        ctx = browser.new_context(
            user_agent=USER_AGENT,
            locale="ru-RU",
            viewport={"width": 1280, "height": 800},
        )
        # Strip the webdriver flag
        ctx.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        page = ctx.new_page()

        # Page 1 — also yields the pagination info
        page.goto(PAGE_URL.format(page=1), wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_selector("div.t-card_event", timeout=15000)
        except Exception:  # noqa: BLE001
            pass
        htmls.append(page.content())

        # Determine total pages from page 1
        total = total_pages(htmls[0])
        total = min(total, 20)

        for n in range(2, total + 1):
            page.goto(PAGE_URL.format(page=n), wait_until="domcontentloaded", timeout=60000)
            try:
                page.wait_for_selector("div.t-card_event", timeout=15000)
            except Exception:  # noqa: BLE001
                pass
            htmls.append(page.content())

        browser.close()
    return htmls


def total_pages(html: str) -> int:
    soup = BeautifulSoup(html, "html.parser")
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
            continue
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
        title = None
        for h in card.select("h1, h2, h3, h4"):
            t = h.get_text(" ", strip=True)
            if t:
                title = t
                break
        if not title:
            for a in card.find_all("a", href=True):
                t = a.get_text(" ", strip=True)
                if t and (not title or len(t) > len(title)):
                    title = t
        if not title:
            continue
        url = ("https://ben-ran.timepad.ru" + link) if link.startswith("/") else link
        out.append({"id": eid, "title": title, "url": url})
    return out


def collect_active_events() -> list[dict]:
    htmls = fetch_pages_with_playwright()
    items: list[dict] = []
    for html in htmls:
        items.extend(parse_cards(html))
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
        items = collect_active_events()
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
