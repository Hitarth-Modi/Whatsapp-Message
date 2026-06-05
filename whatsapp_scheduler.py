#!/usr/bin/env python3
"""Schedule WhatsApp Web messages from your laptop.

This tool stores scheduled messages in a local SQLite database. Keep the
`run` command open; it will send due messages through WhatsApp Web.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

try:
    import dateparser
except ImportError:  # pragma: no cover - handled at runtime for users.
    dateparser = None

DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_DB = Path.home() / ".whatsapp_scheduler" / "messages.sqlite3"
DEFAULT_BROWSER_PROFILE = Path.home() / ".whatsapp_scheduler" / "browser-profile"


@dataclass(frozen=True)
class ScheduledMessage:
    id: int
    recipient: str
    message: str
    scheduled_at: datetime
    status: str
    error: str | None


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS scheduled_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            recipient TEXT NOT NULL,
            message TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL,
            sent_at TEXT,
            error TEXT
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS contacts (
            name TEXT PRIMARY KEY,
            phone TEXT NOT NULL,
            created_at TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def local_now(tz_name: str) -> datetime:
    return datetime.now(ZoneInfo(tz_name))


def parse_when(value: str, tz_name: str) -> datetime:
    tz = ZoneInfo(tz_name)

    parsed = None
    if dateparser is not None:
        parsed = dateparser.parse(
            value,
            settings={
                "TIMEZONE": tz_name,
                "RETURN_AS_TIMEZONE_AWARE": True,
                "PREFER_DATES_FROM": "future",
            },
        )

    if parsed is None:
        formats = (
            "%Y-%m-%d %H:%M",
            "%Y-%m-%d %I:%M %p",
            "%d-%m-%Y %H:%M",
            "%d-%m-%Y %I:%M %p",
        )
        for fmt in formats:
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue

    if parsed is None:
        raise ValueError(
            "Could not understand the time. Try: '2026-06-05 18:30' or 'today 6:30 PM'."
        )

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def normalize_recipient(value: str) -> str:
    recipient = value.strip().replace(" ", "")
    if not recipient:
        raise ValueError("Recipient cannot be empty.")
    if recipient.startswith("+"):
        digits = recipient[1:]
    else:
        digits = recipient
    if not digits.isdigit() or len(digits) < 8:
        raise ValueError(
            "Use a phone number with country code, for example +919876543210."
        )
    return f"+{digits}"


def resolve_recipient(conn: sqlite3.Connection, value: str) -> str:
    try:
        return normalize_recipient(value)
    except ValueError:
        pass

    name = value.strip().lower()
    row = conn.execute("SELECT phone FROM contacts WHERE name = ?", (name,)).fetchone()
    if row is None:
        raise ValueError(
            "Recipient must be a phone number with country code or a saved contact name."
        )
    return row["phone"]


def add_message(
    conn: sqlite3.Connection,
    recipient: str,
    message: str,
    scheduled_at: datetime,
    tz_name: str,
    allow_past: bool,
) -> int:
    if not message.strip():
        raise ValueError("Message cannot be empty.")
    if scheduled_at <= local_now(tz_name) and not allow_past:
        raise ValueError("Scheduled time is in the past.")

    now = local_now(tz_name).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO scheduled_messages (recipient, message, scheduled_at, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (
            resolve_recipient(conn, recipient),
            message,
            scheduled_at.isoformat(timespec="seconds"),
            now,
        ),
    )
    conn.commit()
    return int(cur.lastrowid)


def rows_to_messages(rows: Iterable[sqlite3.Row]) -> list[ScheduledMessage]:
    return [
        ScheduledMessage(
            id=row["id"],
            recipient=row["recipient"],
            message=row["message"],
            scheduled_at=datetime.fromisoformat(row["scheduled_at"]),
            status=row["status"],
            error=row["error"],
        )
        for row in rows
    ]


def list_messages(conn: sqlite3.Connection, include_done: bool) -> list[ScheduledMessage]:
    if include_done:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_messages
            ORDER BY scheduled_at ASC, id ASC
            """
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT * FROM scheduled_messages
            WHERE status IN ('pending', 'sending', 'failed')
            ORDER BY scheduled_at ASC, id ASC
            """
        ).fetchall()
    return rows_to_messages(rows)


def due_messages(
    conn: sqlite3.Connection, now: datetime, limit: int
) -> list[ScheduledMessage]:
    rows = conn.execute(
        """
        SELECT * FROM scheduled_messages
        WHERE status IN ('pending', 'failed')
          AND scheduled_at <= ?
        ORDER BY scheduled_at ASC, id ASC
        LIMIT ?
        """,
        (now.isoformat(timespec="seconds"), limit),
    ).fetchall()
    return rows_to_messages(rows)


def set_status(
    conn: sqlite3.Connection,
    message_id: int,
    status: str,
    tz_name: str,
    error: str | None = None,
) -> None:
    sent_at = local_now(tz_name).isoformat(timespec="seconds") if status == "sent" else None
    conn.execute(
        """
        UPDATE scheduled_messages
        SET status = ?, sent_at = COALESCE(?, sent_at), error = ?
        WHERE id = ?
        """,
        (status, sent_at, error, message_id),
    )
    conn.commit()


def cancel_message(conn: sqlite3.Connection, message_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE scheduled_messages
        SET status = 'cancelled'
        WHERE id = ? AND status IN ('pending', 'failed')
        """,
        (message_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def add_contact(conn: sqlite3.Connection, name: str, phone: str, tz_name: str) -> None:
    clean_name = name.strip().lower()
    if not clean_name:
        raise ValueError("Contact name cannot be empty.")
    if any(char.isspace() for char in clean_name):
        raise ValueError("Use a short contact name without spaces, like mom or rahul.")
    conn.execute(
        """
        INSERT INTO contacts (name, phone, created_at)
        VALUES (?, ?, ?)
        ON CONFLICT(name) DO UPDATE SET phone = excluded.phone
        """,
        (
            clean_name,
            normalize_recipient(phone),
            local_now(tz_name).isoformat(timespec="seconds"),
        ),
    )
    conn.commit()


def list_contacts(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT * FROM contacts ORDER BY name ASC").fetchall()


def remove_contact(conn: sqlite3.Connection, name: str) -> bool:
    cur = conn.execute("DELETE FROM contacts WHERE name = ?", (name.strip().lower(),))
    conn.commit()
    return cur.rowcount > 0


def send_with_whatsapp_web(
    recipient: str,
    message: str,
    wait_time: int,
    close_tab: bool,
    close_time: int,
    browser_profile: Path,
    browser_channel: str,
) -> None:
    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "playwright is not installed. Run: python3 -m pip install -r requirements.txt"
        ) from exc

    phone = recipient.lstrip("+")
    url = f"https://web.whatsapp.com/send?phone={phone}&text={quote(message)}"
    browser_profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        launch_options = {
            "headless": False,
            "args": ["--disable-blink-features=AutomationControlled"],
        }
        if browser_channel != "chromium":
            launch_options["channel"] = browser_channel

        try:
            context = p.chromium.launch_persistent_context(
                str(browser_profile), **launch_options
            )
        except Exception as exc:
            if browser_channel == "chrome":
                raise RuntimeError(
                    "Could not open Google Chrome. Install Chrome, or run "
                    "`python -m playwright install chromium` and use "
                    "`--browser-channel chromium`."
                ) from exc
            raise

        page = context.pages[0] if context.pages else context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=max(wait_time, 20) * 1000)

        send_button = page.get_by_role("button", name=re.compile(r"^send$", re.I))
        icon_button = page.locator('span[data-icon="send"]').last()

        deadline = time.time() + max(wait_time, 20)
        sent = False
        last_error = "send button did not appear"
        while time.time() < deadline and not sent:
            button_seen = False
            for target in (send_button, icon_button):
                try:
                    if target.count() > 0 and target.is_visible(timeout=500):
                        button_seen = True
                    target.click(timeout=1500)
                    sent = True
                    break
                except PlaywrightTimeoutError as exc:
                    last_error = str(exc)
                except Exception as exc:
                    last_error = str(exc)
            if not sent and button_seen:
                try:
                    page.keyboard.press("Enter")
                    sent = True
                except Exception as exc:
                    last_error = str(exc)
                    time.sleep(1)
            if not sent:
                time.sleep(1)

        if not sent:
            context.close()
            raise RuntimeError(
                "Message was prepared but not sent. "
                f"WhatsApp Web did not expose the send button: {last_error}"
            )

        page.wait_for_timeout(close_time * 1000)
        if close_tab:
            page.close()
        context.close()


def format_message(item: ScheduledMessage) -> str:
    preview = item.message.replace("\n", " ")
    if len(preview) > 60:
        preview = f"{preview[:57]}..."
    suffix = f" | error: {item.error}" if item.error else ""
    return (
        f"#{item.id} | {item.status:<9} | {item.scheduled_at:%Y-%m-%d %H:%M} "
        f"| {item.recipient} | {preview}{suffix}"
    )


def cmd_add(args: argparse.Namespace) -> int:
    scheduled_at = parse_when(args.at, args.timezone)
    with connect(args.db) as conn:
        message_id = add_message(
            conn,
            args.to,
            args.message,
            scheduled_at,
            args.timezone,
            args.allow_past,
        )
    print(f"Scheduled message #{message_id} for {scheduled_at:%Y-%m-%d %H:%M %Z}.")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        messages = list_messages(conn, args.all)
    if not messages:
        print("No scheduled messages found.")
        return 0
    for item in messages:
        print(format_message(item))
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        cancelled = cancel_message(conn, args.id)
    if not cancelled:
        print(f"Message #{args.id} was not pending or was not found.")
        return 1
    print(f"Cancelled message #{args.id}.")
    return 0


def cmd_contact_add(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        add_contact(conn, args.name, args.phone, args.timezone)
    print(f"Saved contact '{args.name.strip().lower()}'.")
    return 0


def cmd_contacts(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        contacts = list_contacts(conn)
    if not contacts:
        print("No saved contacts found.")
        return 0
    for row in contacts:
        print(f"{row['name']} | {row['phone']}")
    return 0


def cmd_contact_remove(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        removed = remove_contact(conn, args.name)
    if not removed:
        print(f"Contact '{args.name}' was not found.")
        return 1
    print(f"Removed contact '{args.name.strip().lower()}'.")
    return 0


def cmd_interactive(args: argparse.Namespace) -> int:
    recipient = input("Send to phone number or saved contact name: ").strip()
    message = input("Message: ").strip()
    at = input("When should it send? Example 'today 6:30 PM': ").strip()
    scheduled_at = parse_when(at, args.timezone)

    with connect(args.db) as conn:
        message_id = add_message(
            conn,
            recipient,
            message,
            scheduled_at,
            args.timezone,
            allow_past=False,
        )
    print(f"Scheduled message #{message_id} for {scheduled_at:%Y-%m-%d %H:%M %Z}.")
    return 0


def send_one(
    conn: sqlite3.Connection,
    item: ScheduledMessage,
    args: argparse.Namespace,
) -> None:
    print(f"Sending #{item.id} to {item.recipient}...")
    if args.dry_run:
        print(f"DRY RUN: {item.message}")
        set_status(conn, item.id, "sent", args.timezone)
        return

    set_status(conn, item.id, "sending", args.timezone)
    try:
        send_with_whatsapp_web(
            item.recipient,
            item.message,
            args.wait_time,
            args.close_tab,
            args.close_time,
            args.browser_profile,
            args.browser_channel,
        )
    except Exception as exc:  # Browser failures should not kill the runner.
        set_status(conn, item.id, "failed", args.timezone, str(exc))
        print(f"Failed #{item.id}: {exc}")
        return
    set_status(conn, item.id, "sent", args.timezone)
    print(f"Sent #{item.id}.")


def cmd_run(args: argparse.Namespace) -> int:
    print("WhatsApp scheduler is running. Keep this terminal open.")
    print("Make sure WhatsApp Web is logged in and your laptop stays awake.")
    with connect(args.db) as conn:
        while True:
            messages = due_messages(conn, local_now(args.timezone), args.batch_size)
            for item in messages:
                send_one(conn, item, args)
            time.sleep(args.poll_seconds)


def cmd_send_now(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        recipient = resolve_recipient(conn, args.to)
    if args.dry_run:
        print(f"DRY RUN: would send to {recipient}: {args.message}")
        return 0
    send_with_whatsapp_web(
        recipient,
        args.message,
        args.wait_time,
        args.close_tab,
        args.close_time,
        args.browser_profile,
        args.browser_channel,
    )
    print("Message sent.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Schedule WhatsApp Web messages from your laptop."
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB,
        help=f"SQLite database path. Default: {DEFAULT_DB}",
    )
    parser.add_argument(
        "--timezone",
        default=DEFAULT_TZ,
        help=f"Timezone for parsing and running schedules. Default: {DEFAULT_TZ}",
    )

    sub = parser.add_subparsers(required=True)

    add = sub.add_parser("add", help="Add a scheduled message.")
    add.add_argument("--to", required=True, help="Phone number with country code.")
    add.add_argument("--message", required=True, help="Message text to send.")
    add.add_argument(
        "--at",
        required=True,
        help="When to send. Examples: '2026-06-05 18:30', 'today 6:30 PM'.",
    )
    add.add_argument(
        "--allow-past",
        action="store_true",
        help="Allow creating a message that is already due.",
    )
    add.set_defaults(func=cmd_add)

    list_cmd = sub.add_parser("list", help="List scheduled messages.")
    list_cmd.add_argument("--all", action="store_true", help="Include sent/cancelled items.")
    list_cmd.set_defaults(func=cmd_list)

    cancel = sub.add_parser("cancel", help="Cancel a pending message.")
    cancel.add_argument("id", type=int, help="Message ID to cancel.")
    cancel.set_defaults(func=cmd_cancel)

    contact_add = sub.add_parser("contact-add", help="Save or update a contact alias.")
    contact_add.add_argument("name", help="Short name, for example mom or rahul.")
    contact_add.add_argument("phone", help="Phone number with country code.")
    contact_add.set_defaults(func=cmd_contact_add)

    contacts = sub.add_parser("contacts", help="List saved contact aliases.")
    contacts.set_defaults(func=cmd_contacts)

    contact_remove = sub.add_parser("contact-remove", help="Remove a saved contact alias.")
    contact_remove.add_argument("name", help="Contact alias to remove.")
    contact_remove.set_defaults(func=cmd_contact_remove)

    interactive = sub.add_parser("interactive", help="Schedule a message through prompts.")
    interactive.set_defaults(func=cmd_interactive)

    run = sub.add_parser("run", help="Run the scheduler loop.")
    run.add_argument("--poll-seconds", type=int, default=15)
    run.add_argument("--batch-size", type=int, default=5)
    run.add_argument("--wait-time", type=int, default=20)
    run.add_argument("--close-tab", action="store_true")
    run.add_argument("--close-time", type=int, default=3)
    run.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    run.add_argument("--browser-channel", default="chrome", choices=("chrome", "chromium"))
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    now = sub.add_parser("send-now", help="Send one WhatsApp message immediately.")
    now.add_argument("--to", required=True, help="Phone number with country code.")
    now.add_argument("--message", required=True, help="Message text to send.")
    now.add_argument("--wait-time", type=int, default=20)
    now.add_argument("--close-tab", action="store_true")
    now.add_argument("--close-time", type=int, default=3)
    now.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    now.add_argument("--browser-channel", default="chrome", choices=("chrome", "chromium"))
    now.add_argument("--dry-run", action="store_true")
    now.set_defaults(func=cmd_send_now)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
