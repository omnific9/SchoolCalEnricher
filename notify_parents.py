import json
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

EMAIL_LIST_FILE = Path(__file__).parent / "email_list.txt"
TOKEN_FILE = Path(__file__).parent / "token.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

EMAIL_PROMPT = """\
You are a helpful school assistant writing a weekly digest email for parents.

You will be given a list of upcoming school events for the next week. Each event has a title, description, date/time, and parent action items.

Write a friendly, concise HTML email with these three sections:

1. MUST DO — Things parents are required to do (e.g. sign permission slips, pay fees, attend mandatory meetings). Include the date/time and what exactly they need to do.

2. HIGHLY RECOMMENDED — Things that are strongly encouraged but not strictly required (e.g. volunteer opportunities, parent-teacher conferences, school fundraisers). Include the date/time and details.

3. OPTIONAL — Nice-to-know events that parents may want to attend or be aware of (e.g. spirit days, book fairs, school performances). Include the date/time and details.

IMPORTANT: Categorize each individual ACTION ITEM independently, not at the event level. A single event may have action items that belong to different priority categories. For example, an event like "Spring Field Trip" might have a permission slip (MUST DO) and a volunteer sign-up (HIGHLY RECOMMENDED) — these should appear in their respective sections, each referencing the event name and date for context. Events with no action items should be categorized as a whole based on their nature. If a section has no items, omit it.

Formatting rules:
- Output valid HTML for the email body (no <html>, <head>, or <body> tags — just the inner content).
- Use a clean, modern style with inline CSS. Use a sans-serif font (e.g. Arial, Helvetica).
- Each section should have a colored header banner:
  - MUST DO: red/urgent background (#D32F2F, white text)
  - HIGHLY RECOMMENDED: orange background (#F57C00, white text)
  - OPTIONAL: blue background (#1976D2, white text)
- Use <strong> and <em> to emphasize key details like deadlines, dates, and action items.
- Use bullet points or numbered lists for items within each section.
- Dates and times should be bold.
- Add a friendly greeting at the top and a brief sign-off at the bottom. Sign off as a fellow parent (e.g. "Best, A Fellow Parent"), NOT as the school or school team.
- Keep the overall width suitable for email (max-width around 600px, centered).
- Add light padding and spacing so it doesn't look cramped.
- IMPORTANT: Event descriptions contain parent action items, some with URLs (formatted as "- Action description: https://..."). You MUST include EVERY action item that has a URL as a clickable hyperlink in the email. For example, if the description says "- Submit workshop preferences: https://example.com/form", render it as a clickable link like '<a href="https://example.com/form">Submit workshop preferences</a>'. Do NOT skip any action links. Do NOT include Google Calendar links.

If there are no events at all, write a short friendly note saying there's nothing on the calendar this week.\
"""


def get_google_services():
    """Authenticate and return both Calendar and Sheets services."""
    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print("Error: credentials.json not found.")
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())
    calendar = build("calendar", "v3", credentials=creds)
    sheets = build("sheets", "v4", credentials=creds)
    return calendar, sheets


def get_subscriber_emails(service, calendar_id):
    """Fetch email addresses from the calendar's ACL."""
    emails = []
    acl = service.acl().list(calendarId=calendar_id).execute()
    for rule in acl.get("items", []):
        scope = rule.get("scope", {})
        if scope.get("type") == "user":
            emails.append(scope.get("value"))
    return emails


def load_email_list():
    """Load additional emails from email_list.txt (one per line)."""
    if not EMAIL_LIST_FILE.exists():
        return []
    emails = []
    for line in EMAIL_LIST_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            emails.append(line)
    return emails


def fetch_sheet_emails(sheets_service, sheet_id):
    """Pull email addresses from the Google Form responses sheet (column B)."""
    result = sheets_service.spreadsheets().values().get(
        spreadsheetId=sheet_id,
        range="B2:B",
    ).execute()
    rows = result.get("values", [])
    return [row[0].strip() for row in rows if row and row[0].strip()]


def fetch_next_week_events(service, calendar_id):
    """Fetch all calendar events for the next 7 days."""
    now = datetime.now(timezone.utc)
    time_min = now.isoformat()
    time_max = (now + timedelta(days=7)).isoformat()

    events = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            orderBy="startTime",
            maxResults=250,
            pageToken=page_token,
        ).execute()
        events.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return events


def format_events_for_prompt(events):
    """Format calendar events into a readable string for the LLM."""
    if not events:
        return "No events in the next week."
    lines = []
    for evt in events:
        summary = evt.get("summary", "Untitled")
        description = evt.get("description", "No description")
        start = evt.get("start", {})
        end = evt.get("end", {})

        if "dateTime" in start:
            start_str = start["dateTime"]
            end_str = end.get("dateTime", "")
        else:
            start_str = start.get("date", "")
            end_str = end.get("date", "")

        lines.append(
            f"Event: {summary}\n"
            f"  Start: {start_str}\n"
            f"  End: {end_str}\n"
            f"  Description: {description}\n"
        )
    return "\n".join(lines)


def generate_email_body(openai_client, events):
    """Use OpenAI to craft the weekly digest email from calendar events."""
    events_str = format_events_for_prompt(events)

    response = openai_client.chat.completions.create(
        model="gpt-5.2",
        messages=[
            {"role": "system", "content": EMAIL_PROMPT},
            {"role": "user", "content": f"Here are the events for the next week:\n\n{events_str}"},
        ],
    )

    return response.choices[0].message.content


def send_email(sender, password, recipient, subject, body):
    """Send an HTML email via Gmail SMTP."""
    msg = MIMEMultipart("alternative")
    msg["From"] = sender
    msg["To"] = recipient
    msg["Subject"] = subject
    msg.attach(MIMEText(body, "html"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(sender, password)
        server.sendmail(sender, recipient, msg.as_string())


def main():
    load_dotenv()

    gmail_address = os.getenv("GMAIL_ADDRESS")
    gmail_password = os.getenv("GMAIL_APP_PASSWORD")
    openai_key = os.getenv("OPENAI_API_KEY")
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    signup_sheet_id = os.getenv("SIGNUP_SHEET_ID", "")

    if not gmail_address or not gmail_password:
        print("Error: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.")
        sys.exit(1)
    if not openai_key:
        print("Error: set OPENAI_API_KEY in your .env file.")
        sys.exit(1)

    openai_client = OpenAI(api_key=openai_key)
    calendar_service, sheets_service = get_google_services()

    # Get recipient emails from all sources
    print("Fetching calendar subscribers...")
    acl_emails = get_subscriber_emails(calendar_service, calendar_id)
    print(f"Found {len(acl_emails)} from calendar ACL.")

    list_emails = load_email_list()
    print(f"Found {len(list_emails)} from email_list.txt.")

    sheet_emails = []
    if signup_sheet_id:
        print("Fetching sign-up form responses...")
        sheet_emails = fetch_sheet_emails(sheets_service, signup_sheet_id)
        print(f"Found {len(sheet_emails)} from Google Form.")

    # Merge and deduplicate (case-insensitive)
    seen = set()
    subscribers = []
    for addr in acl_emails + list_emails + sheet_emails:
        lower = addr.lower()
        if lower not in seen:
            seen.add(lower)
            subscribers.append(addr)
    print(f"Total: {len(subscribers)} unique recipient(s).")

    # Get next week's events
    print("Fetching events for the next week...")
    events = fetch_next_week_events(calendar_service, calendar_id)
    print(f"Found {len(events)} event(s).\n")

    # Generate the digest email via OpenAI
    print("Generating weekly digest email...")
    email_body = generate_email_body(openai_client, events)

    today = datetime.now().strftime("%B %d, %Y")
    subject = f"Weekly School Digest — {today}"

    print(f"\n{'=' * 60}")
    print(f"Subject: {subject}")
    print(f"{'=' * 60}")
    print(email_body)
    print(f"{'=' * 60}\n")

    # Send to each subscriber
    for addr in subscribers:
        try:
            send_email(gmail_address, gmail_password, addr, subject, email_body)
            print(f"  Sent to: {addr}")
        except Exception as e:
            print(f"  Failed to send to {addr}: {e}")

    print(f"\nDone. Sent digest to {len(subscribers)} parent(s).")


if __name__ == "__main__":
    main()
