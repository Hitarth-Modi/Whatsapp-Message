# WhatsApp Message Scheduler

Schedule WhatsApp messages using either WhatsApp Web automation or the official WhatsApp Business Cloud API.

This is meant for personal, explicit messages that you choose to schedule. Avoid bulk messaging or spam.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

If you are using Python 3.14, make sure Playwright installs at `1.60.0` or newer. Older Playwright versions depend on an old `greenlet` package that fails to build on Python 3.14.

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

## Official Cloud API Mode

This is the better path if you want the scheduler to run on an always-on server while your laptop is closed.

You need these from Meta:

- WhatsApp Business Account
- Meta developer app with WhatsApp enabled
- Cloud API access token
- Phone Number ID
- Approved message templates for business-initiated scheduled messages

Official docs:

- Cloud API overview: <https://developers.facebook.com/docs/whatsapp/cloud-api/>
- Get started: <https://developers.facebook.com/docs/whatsapp/cloud-api/get-started>
- Send messages endpoint: <https://developers.facebook.com/docs/whatsapp/cloud-api/reference/messages>
- Message templates: <https://developers.facebook.com/docs/whatsapp/cloud-api/guides/send-message-templates>

Set credentials in your terminal:

```bash
export WHATSAPP_CLOUD_ACCESS_TOKEN="YOUR_META_ACCESS_TOKEN"
export WHATSAPP_CLOUD_PHONE_NUMBER_ID="YOUR_PHONE_NUMBER_ID"
export WHATSAPP_CLOUD_API_VERSION="v23.0"
```

Send a Cloud API text message:

```bash
python whatsapp_scheduler.py send-now \
  --backend cloud \
  --to "+919876543210" \
  --message "Hello from Cloud API"
```

Important: normal text messages usually work only inside the 24-hour customer service window after the user messages your business number. For scheduled messages that start a conversation, use an approved template:

```bash
python whatsapp_scheduler.py send-template-now \
  --to "+919876543210" \
  --template-name "hello_world" \
  --language "en_US"
```

Schedule an approved template:

```bash
python whatsapp_scheduler.py add-template \
  --to "+919876543210" \
  --template-name "hello_world" \
  --language "en_US" \
  --at "today 6:30 PM"
```

Run scheduled messages through Cloud API:

```bash
python whatsapp_scheduler.py run --backend cloud
```

For a template with variables, pass Meta template components as JSON:

```bash
python whatsapp_scheduler.py add-template \
  --to "+919876543210" \
  --template-name "appointment_reminder" \
  --language "en_US" \
  --components-json '[{"type":"body","parameters":[{"type":"text","text":"Hitarth"},{"type":"text","text":"6:30 PM"}]}]' \
  --at "today 6:00 PM"
```

## Laptop Closed / Cloud Running

Pushing this code to GitHub does not make it run by itself. GitHub stores the code; a computer or server still has to be awake and running `python whatsapp_scheduler.py run`.

This WhatsApp Web version needs an active browser session, so it cannot send from your closed laptop unless it is running on another always-on machine that is logged into WhatsApp Web.

For true cloud scheduling, deploy the Cloud API mode on a service like Render, Railway, Fly.io, a VPS, or another always-on machine. Do not put your access token directly in GitHub; use environment variables/secrets.

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
