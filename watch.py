"""
Watch ben-ran.timepad.ru for new active events.
Uses Timepad's public events endpoint instead of parsing HTML — more robust.

Env vars:
  TELEGRAM_TOKEN   - bot token
  TELEGRAM_CHAT_ID - chat id
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

# ---- target ----
ORG_ID = 239824
SITE_LABEL = "БЕН РАН (Timepad)"
SITE_URL = "https://ben-ran.timepad.ru/events/"
API = "https://api.timepad.ru/v1/events"
# ----------------

STATE_FILE = Path(__file__).resolve().parent / "state.json"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

HEADERS = {
    "User-Agent": "ben-ran-watcher/1.0 (https://github.com/tatianajagoda-debug)",
    "Accept": "application/json",
}


def fetch_events() -> list[dict]:
    """Fetch up to 200 of the most-recently-starting public events."""
    out: list[dict] = []
    for skip in (0, 100):
        params = {
            "organization_ids": ORG_ID,
            "limit": 100,
            "skip": skip,
            "access_statuses": "public",
            "fields": "id,name,starts_at,ends_at,url,access_status,location",
            "sort": "-starts_at",
        }
        last_err = None
        for attempt in range(3):
            try:
                r = requests.get(API, params=params, headers=HEADERS, timeout=30)
                r.raise_for_status()
                data = r.json()
                values = data.get("values", [])
                out.extend(values)
                # If fewer than 100 returned, no more pages.
                if len(values) < 100:
                    return out
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                time.sleep(2 + attempt * 2)
        else:
            raise RuntimeError(f"API call failed (skip={skip}): {last_err}")
    return out


def parse_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        # Timepad uses "YYYY-MM-DD HH:MM:SS+HH:MM" or ISO
        s = raw.replace(" ", "T") if "T" not in raw else raw
        s = s.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        # Assume Moscow time if Timepad didn't include tz
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def is_active(event: dict, now: datetime) -> bool:
    """Active = event still upcoming or currently ongoing."""
    end = parse_dt(event.get("ends_at")) or parse_dt(event.get("starts_at"))
    if not end:
        return False
    return end >= now


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
        title = escape_html(e.get("name") or "(без названия)")
        url = e.get("url") or f"https://ben-ran.timepad.ru/event/{e['id']}/"
        lines.append(f"• <a href=\"{url}\">{title}</a>")
    if len(events) > 40:
        lines.append("")
        lines.append(f"…и ещё {len(events) - 40} (см. сайт)")
    return "\n".join(lines)


def main() -> int:
    state = load_state()
    seen = set(str(x) for x in state.get("seen", []))
    bootstrap = not seen

    try:
        events = fetch_events()
    except Exception as e:  # noqa: BLE001
        print(f"API fetch failed: {e}", file=sys.stderr)
        return 2

    now = datetime.now(timezone.utc)
    active = [e for e in events if is_active(e, now)]

    if not active and state.get("last_count", 0) > 3:
        print(
            f"Suspicious: 0 active events now but state had {state.get('last_count')}. "
            "Aborting without state update.",
            file=sys.stderr,
        )
        return 3

    current_ids = {str(e["id"]) for e in active}
    new_ids = current_ids - seen

    print(f"Fetched: {len(events)}, active: {len(active)}, new: {len(new_ids)}")

    if bootstrap:
        print("First run — recording state without notifications")
    elif new_ids:
        new_events = [e for e in active if str(e["id"]) in new_ids]
        new_events.sort(key=lambda x: parse_dt(x.get("starts_at")) or now)
        send_telegram(format_message(new_events, len(active)))

    state["seen"] = sorted(current_ids)
    state["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S UTC")
    state["last_count"] = len(active)
    save_state(state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
