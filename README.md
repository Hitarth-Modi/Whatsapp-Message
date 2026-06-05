# WhatsApp Message Scheduler

Schedule WhatsApp messages from your laptop using WhatsApp Web.

This is meant for personal, explicit messages that you choose to schedule. Avoid bulk messaging or spam.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

The script opens WhatsApp Web in Chrome through Playwright. On the first send, log in with the QR code in the browser window that opens.

If you do not have Google Chrome installed:

```bash
python -m playwright install chromium
```

Then add `--browser-channel chromium` to `run` or `send-now`.

## Schedule A Message

Easiest way:

```bash
python whatsapp_scheduler.py interactive
```

Use a phone number with country code:

```bash
python whatsapp_scheduler.py add \
  --to "+919876543210" \
  --message "Hi, this is a scheduled message." \
  --at "today 6:30 PM"
```

Other accepted time examples:

```bash
python whatsapp_scheduler.py add --to "+919876543210" --message "Hello" --at "2026-06-05 18:30"
python whatsapp_scheduler.py add --to "+919876543210" --message "Hello" --at "tomorrow 9 AM"
```

Or save a contact alias first:

```bash
python whatsapp_scheduler.py contact-add rahul "+919876543210"
python whatsapp_scheduler.py add --to rahul --message "Hi Rahul" --at "today 6:30 PM"
```

## Run The Scheduler

Keep this command running when messages are due:

```bash
python whatsapp_scheduler.py run
```

At the scheduled time, the script opens WhatsApp Web and sends the message.

Your laptop must be awake, your browser must stay logged into WhatsApp Web, and it is best not to type while a message is being sent.

On macOS, this keeps the laptop awake while the scheduler runs:

```bash
caffeinate -dimsu python whatsapp_scheduler.py run
```

## Important: Laptop Closed / Cloud Running

Pushing this code to GitHub does not make it run by itself. GitHub stores the code; a computer or server still has to be awake and running `python whatsapp_scheduler.py run`.

This WhatsApp Web version needs an active browser session, so it cannot send from your closed laptop unless it is running on another always-on machine that is logged into WhatsApp Web.

For true cloud scheduling, use the official WhatsApp Business Cloud API instead of browser automation. That requires a Meta developer app, a WhatsApp Business account, API credentials, and usually approved message templates for starting conversations.

## Useful Commands

List pending messages:

```bash
python whatsapp_scheduler.py list
```

List saved contacts:

```bash
python whatsapp_scheduler.py contacts
```

Cancel a message:

```bash
python whatsapp_scheduler.py cancel 1
```

Test without sending:

```bash
python whatsapp_scheduler.py send-now --to "+919876543210" --message "Testing" --dry-run
```

Send immediately:

```bash
python whatsapp_scheduler.py send-now --to "+919876543210" --message "Hello now"
```

Use Chromium instead of Chrome:

```bash
python whatsapp_scheduler.py send-now --to "+919876543210" --message "Hello now" --browser-channel chromium
```
