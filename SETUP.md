# Calenricher Setup Guide

Calenricher extracts school events from your school's notification emails, adds them to Google Calendar, and sends parents a weekly digest email with prioritized action items.

## Prerequisites

- Python 3.10+
- A Gmail account with IMAP enabled
- An OpenAI API key
- A Google Cloud project (free tier is fine)

---

## 1. Clone and Install

```bash
git clone <repo-url>
cd schoolemail/calenricher

# Create virtual environment
python -m venv venv

# Activate it
# Windows (PowerShell)
venv\Scripts\Activate.ps1
# Windows (cmd)
venv\Scripts\activate.bat
# macOS / Linux
source venv/bin/activate

pip install -r requirements.txt
```

---

## 2. Gmail App Password

Google requires an app-specific password when IMAP is accessed programmatically.

1. Go to [Google Account Security](https://myaccount.google.com/security).
2. Ensure **2-Step Verification** is enabled.
3. Go to [App Passwords](https://myaccount.google.com/apppasswords).
4. Create a new app password (name it anything, e.g. "Calenricher").
5. Copy the 16-character password — you'll need it for `.env`.

Also make sure IMAP is enabled: **Gmail Settings > Forwarding and POP/IMAP > Enable IMAP**.

---

## 3. Google Cloud Project

### Create the project

1. Go to [Google Cloud Console](https://console.cloud.google.com/).
2. Click **Select a project** > **New Project**. Name it (e.g. "Calenricher").

### Enable APIs

Go to **APIs & Services > Library** and enable these two APIs:

- **Google Calendar API**
- **Google Sheets API** (only needed if using the sign-up sheet feature)

### Configure OAuth Consent Screen

1. Go to **APIs & Services > OAuth consent screen**.
2. Choose **External** user type (or Internal if using Google Workspace).
3. Fill in the required fields (app name, support email).
4. Under **Scopes**, add:
   - `https://www.googleapis.com/auth/calendar`
   - `https://www.googleapis.com/auth/spreadsheets.readonly`
5. Under **Test users**, add the Gmail address you'll run the script with.
6. Save.

### Create OAuth Credentials

1. Go to **APIs & Services > Credentials**.
2. Click **Create Credentials > OAuth client ID**.
3. Application type: **Desktop app**.
4. Download the JSON file and save it as `credentials.json` in the `calenricher/` directory.

### First-run Authorization

The first time you run either script, a browser window will open asking you to authorize access. After granting permission, a `token.json` file is saved locally so you won't be prompted again.

---

## 4. Environment Variables

Copy the example file and fill it in:

```bash
cp .env.example .env
```

| Variable | Required | Description |
|---|---|---|
| `GMAIL_ADDRESS` | Yes | Your Gmail address |
| `GMAIL_APP_PASSWORD` | Yes | App password from step 2 |
| `OPENAI_API_KEY` | Yes | Your OpenAI API key |
| `GOOGLE_CALENDAR_ID` | No | Calendar ID (default: `primary`) |
| `SIGNUP_SHEET_ID` | No | Google Sheet ID for form sign-up emails |
| `SCHOOL_EMAIL_FROM` | Yes | Sender domain or address to filter school emails (e.g. `@school.edu`) |

To use a shared calendar instead of your primary one, find its ID under **Google Calendar Settings > Integrate calendar**.

---

## 5. Subscriber Emails

The digest email is sent to recipients gathered from three sources:

1. **Calendar ACL** — anyone with access to the Google Calendar.
2. **`email_list.txt`** — one email per line (create this file manually). Lines starting with `#` are ignored.
3. **Google Sheet** — if `SIGNUP_SHEET_ID` is set, emails are pulled from column B (for a Google Form responses sheet).

Duplicates across sources are automatically removed.

---

## 6. Running the Scripts

```bash
# Fetch new emails and populate the calendar
python fetch_parentsquare.py

# Generate and send the weekly digest
python notify_parents.py
```

`fetch_parentsquare.py` records the last run date in `.last_run` so subsequent runs only process new emails.

---

## 7. Scheduling

### Windows (Task Scheduler)

1. Open **Task Scheduler** (`taskschd.msc`).
2. Click **Create Basic Task**.
3. Set the trigger (e.g. Daily at 6:00 AM for fetch, Weekly on Monday at 7:00 AM for digest).
4. Action: **Start a program**.
   - Program/script: full path to `python.exe` inside your venv, e.g.
     ```
     C:\path\to\calenricher\venv\Scripts\python.exe
     ```
   - Arguments: the script name, e.g.
     ```
     fetch_parentsquare.py
     ```
   - Start in: the project directory, e.g.
     ```
     C:\path\to\calenricher
     ```
5. Repeat for `notify_parents.py` with a weekly trigger.

> Check **"Run whether user is logged on or not"** in the task properties for headless execution.

### macOS / Linux (cron)

Open your crontab:

```bash
crontab -e
```

Add entries (adjust paths):

```cron
# Fetch new emails daily at 6 AM
0 6 * * * cd /path/to/calenricher && /path/to/calenricher/venv/bin/python fetch_parentsquare.py >> /path/to/calenricher/cron.log 2>&1

# Send weekly digest every Monday at 7 AM
0 7 * * 1 cd /path/to/calenricher && /path/to/calenricher/venv/bin/python notify_parents.py >> /path/to/calenricher/cron.log 2>&1
```

### macOS (launchd alternative)

Create `~/Library/LaunchAgents/com.calenricher.fetch.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.calenricher.fetch</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/calenricher/venv/bin/python</string>
        <string>/path/to/calenricher/fetch_parentsquare.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/calenricher</string>
    <key>StartCalendarInterval</key>
    <dict>
        <key>Hour</key>
        <integer>6</integer>
        <key>Minute</key>
        <integer>0</integer>
    </dict>
    <key>StandardOutPath</key>
    <string>/path/to/calenricher/launchd.log</string>
    <key>StandardErrorPath</key>
    <string>/path/to/calenricher/launchd.log</string>
</dict>
</plist>
```

Load it:

```bash
launchctl load ~/Library/LaunchAgents/com.calenricher.fetch.plist
```

Create a second plist for `notify_parents.py` with a weekly schedule (add `Weekday` key set to `1` for Monday).

---

## 8. Troubleshooting

| Problem | Solution |
|---|---|
| `Login failed` on IMAP | Make sure you're using an **App Password**, not your regular password. |
| `credentials.json not found` | Download OAuth desktop credentials from Google Cloud Console. |
| `token.json` errors after changing scopes | Delete `token.json` and re-authorize on the next run. |
| No emails found | Check that `SCHOOL_EMAIL_FROM` matches the sender domain and that IMAP is enabled. |
| Google API `403 Forbidden` | Ensure the Calendar and Sheets APIs are enabled in your Google Cloud project. |
| Scheduled task not running | Verify the paths in your task/cron point to the venv Python, not the system Python. |
