#!/usr/bin/env python3
"""Schedule WhatsApp Web messages from your laptop.

This tool stores scheduled messages in a local SQLite database. Keep the
`run` command open; it will send due messages through WhatsApp Web.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

try:
    import dateparser
except ImportError:  # pragma: no cover - handled at runtime for users.
    dateparser = None

DEFAULT_TZ = "Asia/Kolkata"
DEFAULT_DB = Path.home() / ".whatsapp_scheduler" / "messages.sqlite3"
DEFAULT_BROWSER_PROFILE = Path.home() / ".whatsapp_scheduler" / "browser-profile"
DEFAULT_CLOUD_API_VERSION = "v23.0"


@dataclass(frozen=True)
class ScheduledMessage:
    id: int
    recipient: str
    message: str
    scheduled_at: datetime
    status: str
    error: str | None
    message_kind: str = "text"
    template_name: str | None = None
    template_language: str | None = None
    template_components: str | None = None


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
    existing_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(scheduled_messages)").fetchall()
    }
    migrations = {
        "message_kind": "ALTER TABLE scheduled_messages ADD COLUMN message_kind TEXT NOT NULL DEFAULT 'text'",
        "template_name": "ALTER TABLE scheduled_messages ADD COLUMN template_name TEXT",
        "template_language": "ALTER TABLE scheduled_messages ADD COLUMN template_language TEXT",
        "template_components": "ALTER TABLE scheduled_messages ADD COLUMN template_components TEXT",
    }
    for column, statement in migrations.items():
        if column not in existing_columns:
            conn.execute(statement)
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


def add_template_message(
    conn: sqlite3.Connection,
    recipient: str,
    template_name: str,
    language: str,
    components_json: str | None,
    scheduled_at: datetime,
    tz_name: str,
    allow_past: bool,
) -> int:
    clean_template = template_name.strip()
    if not clean_template:
        raise ValueError("Template name cannot be empty.")
    if scheduled_at <= local_now(tz_name) and not allow_past:
        raise ValueError("Scheduled time is in the past.")
    if components_json:
        cloud_components(components_json)

    now = local_now(tz_name).isoformat(timespec="seconds")
    cur = conn.execute(
        """
        INSERT INTO scheduled_messages (
            recipient,
            message,
            scheduled_at,
            created_at,
            message_kind,
            template_name,
            template_language,
            template_components
        )
        VALUES (?, ?, ?, ?, 'template', ?, ?, ?)
        """,
        (
            resolve_recipient(conn, recipient),
            f"template:{clean_template}",
            scheduled_at.isoformat(timespec="seconds"),
            now,
            clean_template,
            language,
            components_json,
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
            message_kind=row["message_kind"],
            template_name=row["template_name"],
            template_language=row["template_language"],
            template_components=row["template_components"],
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
        WHERE status = 'pending'
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


def retry_message(conn: sqlite3.Connection, message_id: int) -> bool:
    cur = conn.execute(
        """
        UPDATE scheduled_messages
        SET status = 'pending', error = NULL
        WHERE id = ? AND status = 'failed'
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


def cloud_value(cli_value: str | None, env_name: str) -> str:
    value = cli_value or os.environ.get(env_name)
    if not value:
        raise RuntimeError(
            f"Missing {env_name}. Set it in your shell or pass the matching CLI option."
        )
    return value


def post_cloud_message(
    phone_number_id: str,
    access_token: str,
    api_version: str,
    payload: dict,
) -> dict:
    url = f"https://graph.facebook.com/{api_version}/{phone_number_id}/messages"
    data = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=30) as response:
            body = response.read().decode("utf-8")
    except HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Cloud API returned HTTP {exc.code}: {error_body}") from exc
    except URLError as exc:
        raise RuntimeError(f"Could not reach WhatsApp Cloud API: {exc}") from exc

    return json.loads(body) if body else {}


def send_with_cloud_api(
    recipient: str,
    message: str,
    access_token: str,
    phone_number_id: str,
    api_version: str,
) -> dict:
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient.lstrip("+"),
        "type": "text",
        "text": {"preview_url": False, "body": message},
    }
    return post_cloud_message(phone_number_id, access_token, api_version, payload)


def send_template_with_cloud_api(
    recipient: str,
    template_name: str,
    language: str,
    components: list[dict] | None,
    access_token: str,
    phone_number_id: str,
    api_version: str,
) -> dict:
    template: dict = {
        "name": template_name,
        "language": {"code": language},
    }
    if components:
        template["components"] = components

    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient.lstrip("+"),
        "type": "template",
        "template": template,
    }
    return post_cloud_message(phone_number_id, access_token, api_version, payload)


def cloud_components(value: str | None) -> list[dict] | None:
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"--components-json is not valid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise ValueError("--components-json must be a JSON array.")
    return parsed


def send_message(
    recipient: str,
    message: str,
    args: argparse.Namespace,
) -> dict | None:
    if args.backend == "cloud":
        return send_with_cloud_api(
            recipient,
            message,
            cloud_value(args.cloud_access_token, "WHATSAPP_CLOUD_ACCESS_TOKEN"),
            cloud_value(args.cloud_phone_number_id, "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
            args.cloud_api_version,
        )

    send_with_whatsapp_web(
        recipient,
        message,
        args.wait_time,
        args.close_tab,
        args.close_time,
        args.browser_profile,
        args.browser_channel,
    )
    return None


def format_message(item: ScheduledMessage) -> str:
    if item.message_kind == "template":
        preview = f"template:{item.template_name} ({item.template_language or 'en_US'})"
    else:
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


def cmd_add_template(args: argparse.Namespace) -> int:
    scheduled_at = parse_when(args.at, args.timezone)
    with connect(args.db) as conn:
        message_id = add_template_message(
            conn,
            args.to,
            args.template_name,
            args.language,
            args.components_json,
            scheduled_at,
            args.timezone,
            args.allow_past,
        )
    print(
        f"Scheduled template #{message_id} "
        f"for {scheduled_at:%Y-%m-%d %H:%M %Z}."
    )
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


def cmd_retry(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        retried = retry_message(conn, args.id)
    if not retried:
        print(f"Message #{args.id} was not failed or was not found.")
        return 1
    print(f"Message #{args.id} is pending again.")
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
        if item.message_kind == "template":
            if args.backend != "cloud":
                raise RuntimeError("Template messages require --backend cloud.")
            send_template_with_cloud_api(
                item.recipient,
                item.template_name or "",
                item.template_language or "en_US",
                cloud_components(item.template_components),
                cloud_value(args.cloud_access_token, "WHATSAPP_CLOUD_ACCESS_TOKEN"),
                cloud_value(args.cloud_phone_number_id, "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
                args.cloud_api_version,
            )
        else:
            send_message(item.recipient, item.message, args)
    except Exception as exc:  # Browser failures should not kill the runner.
        set_status(conn, item.id, "failed", args.timezone, str(exc))
        print(f"Failed #{item.id}: {exc}")
        return
    set_status(conn, item.id, "sent", args.timezone)
    print(f"Sent #{item.id}.")


def cmd_run(args: argparse.Namespace) -> int:
    print("WhatsApp scheduler is running. Keep this terminal open.")
    if args.backend == "cloud":
        print("Using WhatsApp Cloud API backend.")
    else:
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
    response = send_message(recipient, args.message, args)
    if response:
        print(json.dumps(response, indent=2))
    print("Message sent.")
    return 0


def cmd_send_template_now(args: argparse.Namespace) -> int:
    with connect(args.db) as conn:
        recipient = resolve_recipient(conn, args.to)
    if args.dry_run:
        print(
            "DRY RUN: would send template "
            f"{args.template_name} ({args.language}) to {recipient}"
        )
        return 0
    response = send_template_with_cloud_api(
        recipient,
        args.template_name,
        args.language,
        cloud_components(args.components_json),
        cloud_value(args.cloud_access_token, "WHATSAPP_CLOUD_ACCESS_TOKEN"),
        cloud_value(args.cloud_phone_number_id, "WHATSAPP_CLOUD_PHONE_NUMBER_ID"),
        args.cloud_api_version,
    )
    print(json.dumps(response, indent=2))
    print("Template message sent.")
    return 0


def add_web_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--wait-time", type=int, default=20)
    parser.add_argument("--close-tab", action="store_true")
    parser.add_argument("--close-time", type=int, default=3)
    parser.add_argument("--browser-profile", type=Path, default=DEFAULT_BROWSER_PROFILE)
    parser.add_argument("--browser-channel", default="chrome", choices=("chrome", "chromium"))


def add_cloud_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--cloud-access-token",
        default=None,
        help="WhatsApp Cloud API access token. Defaults to WHATSAPP_CLOUD_ACCESS_TOKEN.",
    )
    parser.add_argument(
        "--cloud-phone-number-id",
        default=None,
        help="Cloud API phone number ID. Defaults to WHATSAPP_CLOUD_PHONE_NUMBER_ID.",
    )
    parser.add_argument(
        "--cloud-api-version",
        default=os.environ.get("WHATSAPP_CLOUD_API_VERSION", DEFAULT_CLOUD_API_VERSION),
        help=f"Meta Graph API version. Default: {DEFAULT_CLOUD_API_VERSION}",
    )


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

    add_template = sub.add_parser("add-template", help="Schedule a Cloud API template.")
    add_template.add_argument("--to", required=True, help="Phone number with country code.")
    add_template.add_argument("--template-name", required=True)
    add_template.add_argument("--language", default="en_US")
    add_template.add_argument(
        "--components-json",
        default=None,
        help="Optional JSON array for template components and variables.",
    )
    add_template.add_argument(
        "--at",
        required=True,
        help="When to send. Examples: '2026-06-05 18:30', 'today 6:30 PM'.",
    )
    add_template.add_argument(
        "--allow-past",
        action="store_true",
        help="Allow creating a template message that is already due.",
    )
    add_template.set_defaults(func=cmd_add_template)

    list_cmd = sub.add_parser("list", help="List scheduled messages.")
    list_cmd.add_argument("--all", action="store_true", help="Include sent/cancelled items.")
    list_cmd.set_defaults(func=cmd_list)

    cancel = sub.add_parser("cancel", help="Cancel a pending message.")
    cancel.add_argument("id", type=int, help="Message ID to cancel.")
    cancel.set_defaults(func=cmd_cancel)

    retry = sub.add_parser("retry", help="Retry a failed message.")
    retry.add_argument("id", type=int, help="Failed message ID to retry.")
    retry.set_defaults(func=cmd_retry)

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
    run.add_argument("--backend", default="web", choices=("web", "cloud"))
    add_web_args(run)
    add_cloud_args(run)
    run.add_argument("--dry-run", action="store_true")
    run.set_defaults(func=cmd_run)

    now = sub.add_parser("send-now", help="Send one WhatsApp message immediately.")
    now.add_argument("--to", required=True, help="Phone number with country code.")
    now.add_argument("--message", required=True, help="Message text to send.")
    now.add_argument("--backend", default="web", choices=("web", "cloud"))
    add_web_args(now)
    add_cloud_args(now)
    now.add_argument("--dry-run", action="store_true")
    now.set_defaults(func=cmd_send_now)

    template = sub.add_parser(
        "send-template-now",
        help="Send an approved WhatsApp Cloud API template immediately.",
    )
    template.add_argument("--to", required=True, help="Phone number with country code.")
    template.add_argument("--template-name", required=True)
    template.add_argument("--language", default="en_US")
    template.add_argument(
        "--components-json",
        default=None,
        help="Optional JSON array for template components and variables.",
    )
    add_cloud_args(template)
    template.add_argument("--dry-run", action="store_true")
    template.set_defaults(func=cmd_send_template_now)

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
