#!/usr/bin/env python3
"""Small web dashboard for the WhatsApp scheduler."""

from __future__ import annotations

import os
import secrets
import threading
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from flask import Flask, flash, redirect, render_template, request, session, url_for

from whatsapp_scheduler import (
    DEFAULT_CLOUD_API_VERSION,
    DEFAULT_DB,
    DEFAULT_TZ,
    add_message,
    cancel_message,
    connect,
    due_messages,
    list_messages,
    load_env_file,
    local_now,
    retry_message,
    send_one,
)

PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_RECIPIENT = "+918511468069"
POLL_SECONDS = 15

load_env_file(PROJECT_DIR / ".env")

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY") or os.urandom(32)

runner_lock = threading.Lock()
runner_stop = threading.Event()
runner_thread: threading.Thread | None = None


def configured_recipient() -> str:
    return os.environ.get("WHATSAPP_DEFAULT_RECIPIENT", DEFAULT_RECIPIENT)


def dashboard_password() -> str | None:
    return os.environ.get("WEB_DASHBOARD_PASSWORD")


def authenticated() -> bool:
    password = dashboard_password()
    return not password or session.get("authenticated") is True


def cloud_args() -> SimpleNamespace:
    return SimpleNamespace(
        backend="cloud",
        cloud_access_token=None,
        cloud_phone_number_id=None,
        cloud_api_version=os.environ.get(
            "WHATSAPP_CLOUD_API_VERSION", DEFAULT_CLOUD_API_VERSION
        ),
        timezone=os.environ.get("WHATSAPP_TIMEZONE", DEFAULT_TZ),
        dry_run=False,
        wait_time=20,
        close_tab=False,
        close_time=3,
        browser_profile=None,
        browser_channel="chrome",
    )


def process_due_once(limit: int = 10) -> int:
    sent_or_attempted = 0
    args = cloud_args()
    with connect(DEFAULT_DB) as conn:
        messages = due_messages(conn, local_now(args.timezone), limit)
        for item in messages:
            send_one(conn, item, args)
            sent_or_attempted += 1
    return sent_or_attempted


def runner_loop() -> None:
    while not runner_stop.is_set():
        process_due_once()
        runner_stop.wait(POLL_SECONDS)


def scheduler_running() -> bool:
    return runner_thread is not None and runner_thread.is_alive()


def start_scheduler() -> bool:
    global runner_thread
    with runner_lock:
        if scheduler_running():
            return False
        runner_stop.clear()
        runner_thread = threading.Thread(
            target=runner_loop,
            name="whatsapp-web-scheduler",
            daemon=True,
        )
        runner_thread.start()
        return True


def stop_scheduler() -> bool:
    global runner_thread
    with runner_lock:
        if not scheduler_running():
            return False
        runner_stop.set()
        if runner_thread:
            runner_thread.join(timeout=2)
        runner_thread = None
        return True


def parse_schedule_time(value: str) -> datetime:
    if not value:
        raise ValueError("Choose a schedule time.")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M")
    except ValueError as exc:
        raise ValueError("Schedule time is not valid.") from exc
    return parsed.replace(tzinfo=local_now(DEFAULT_TZ).tzinfo)


@app.before_request
def require_login():
    if request.endpoint in {"login", "static"}:
        return None
    if authenticated():
        return None
    return redirect(url_for("login"))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        expected = dashboard_password()
        submitted = request.form.get("password", "")
        if expected and secrets.compare_digest(submitted, expected):
            session["authenticated"] = True
            return redirect(url_for("index"))
        flash("Incorrect password.", "error")
    return render_template("login.html", password_enabled=dashboard_password() is not None)


@app.post("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.get("/")
def index():
    with connect(DEFAULT_DB) as conn:
        messages = list_messages(conn, include_done=True)
    messages.reverse()
    return render_template(
        "index.html",
        messages=messages[:40],
        recipient=configured_recipient(),
        running=scheduler_running(),
        now=local_now(DEFAULT_TZ),
    )


@app.route("/schedule", methods=["GET", "POST"])
def schedule():
    if request.method == "GET":
        return redirect(url_for("index"))

    message = request.form.get("message", "").strip()
    try:
        scheduled_at = parse_schedule_time(request.form.get("scheduled_at", ""))
        with connect(DEFAULT_DB) as conn:
            message_id = add_message(
                conn,
                configured_recipient(),
                message,
                scheduled_at,
                DEFAULT_TZ,
                allow_past=False,
            )
    except Exception as exc:
        flash(f"Could not schedule message: {exc}", "error")
    else:
        flash(f"Scheduled #{message_id}.", "success")
    return redirect(url_for("index"))


@app.post("/scheduler/start")
def scheduler_start():
    if start_scheduler():
        flash("Scheduler running.", "success")
    else:
        flash("Scheduler already running.", "info")
    return redirect(url_for("index"))


@app.post("/scheduler/stop")
def scheduler_stop():
    if stop_scheduler():
        flash("Scheduler stopped.", "info")
    else:
        flash("Scheduler was not running.", "info")
    return redirect(url_for("index"))


@app.post("/scheduler/run-due")
def scheduler_run_due():
    count = process_due_once()
    flash(f"Processed {count} due message{'s' if count != 1 else ''}.", "info")
    return redirect(url_for("index"))


@app.post("/messages/<int:message_id>/cancel")
def message_cancel(message_id: int):
    with connect(DEFAULT_DB) as conn:
        cancelled = cancel_message(conn, message_id)
    flash(f"Cancelled #{message_id}." if cancelled else f"Could not cancel #{message_id}.")
    return redirect(url_for("index"))


@app.post("/messages/<int:message_id>/retry")
def message_retry(message_id: int):
    with connect(DEFAULT_DB) as conn:
        retried = retry_message(conn, message_id)
    flash(f"Retrying #{message_id}." if retried else f"Could not retry #{message_id}.")
    return redirect(url_for("index"))


if __name__ == "__main__":
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "5050"))
    app.run(host=host, port=port, debug=False)
