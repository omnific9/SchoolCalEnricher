import imaplib
import email
import email.header
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

LAST_RUN_FILE = Path(__file__).parent / ".last_run"
DATE_FMT = "%d-%b-%Y"  # IMAP date format, e.g. 14-Feb-2026
TOKEN_FILE = Path(__file__).parent / "token.json"
CREDENTIALS_FILE = Path(__file__).parent / "credentials.json"
SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/spreadsheets.readonly",
]

EXTRACTION_SYSTEM_PROMPT = """\
You are an assistant that extracts school events from school notification emails.

You will be given an email subject and body, plus a list of existing calendar events.

Extract all events mentioned in the email. Return a JSON object with a single key "events" containing a list. Each event has:
- "title": short event title
- "description": fuller description of the event
- "start_date": ISO date string (YYYY-MM-DD) for when the event starts
- "end_date": ISO date string (YYYY-MM-DD) for when the event ends (same as start_date for single-day events)
- "start_time": ISO time string (HH:MM) or null if not specified
- "end_time": ISO time string (HH:MM) or null if not specified
- "parent_actions": list of objects, each with "action" (short, concise description — 10 words max) and "link" (a URL where they can complete the action, or null if no link). Be thorough — capture EVERY call to action directed at parents, including but not limited to: submitting preferences, signing up to volunteer or help, filling out forms, RSVPing, making payments, signing permission slips, completing surveys. If the email has a URL associated with any of these actions, you MUST include it. Do NOT include generic links, unsubscribe links, or footer links.
- "matching_event_id": the ID of an existing calendar event that refers to the same real-world event, or null if this is a new event

When deciding matching_event_id, consider that existing events may have different titles or descriptions but refer to the same event. For example, "Book Club" and "Laurel Parent Book Club" on the same date are the same event. A correction email updating the time for an event should match the original event.

If an event spans multiple days (e.g. "Feb 14-18"), use the first day as start_date and the last day as end_date.

If a date is vague or not explicitly stated but there is a parent action required, do your best to estimate a date. Use context clues like "this Friday", "next week", "by end of month", the email's send date, or the event it relates to. Always provide your best estimate for start_date rather than leaving it null — parents need a deadline on their calendar.

If the email contains no events, return {"events": []}.
Only return the JSON object, nothing else.\
"""


def load_last_run_date():
    if LAST_RUN_FILE.exists():
        text = LAST_RUN_FILE.read_text().strip()
        if text:
            return datetime.strptime(text, DATE_FMT)
    return None


def save_last_run_date():
    LAST_RUN_FILE.write_text(datetime.now(timezone.utc).strftime(DATE_FMT))


def decode_header_value(raw):
    parts = email.header.decode_header(raw)
    decoded = []
    for data, charset in parts:
        if isinstance(data, bytes):
            decoded.append(data.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(data)
    return "".join(decoded)


def get_text_body(msg):
    if msg.is_multipart():
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/plain" and part.get("Content-Disposition") != "attachment":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # fallback to text/html if no plain text
        for part in msg.walk():
            ct = part.get_content_type()
            if ct == "text/html" and part.get("Content-Disposition") != "attachment":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def get_calendar_service():
    """Authenticate with Google Calendar API and return a service object."""
    creds = None

    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                print(
                    "Error: credentials.json not found.\n"
                    "Download OAuth2 desktop credentials from Google Cloud Console\n"
                    "and place the file in the project root."
                )
                sys.exit(1)
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_FILE), SCOPES
            )
            creds = flow.run_local_server(port=0)
        TOKEN_FILE.write_text(creds.to_json())

    return build("calendar", "v3", credentials=creds)


def format_existing_events(existing_events):
    """Format existing calendar events into a compact string for the LLM prompt."""
    if not existing_events:
        return "No existing calendar events."
    lines = []
    for evt in existing_events:
        eid = evt.get("id", "")
        summary = evt.get("summary", "Untitled")
        start = evt.get("start", {})
        date = start.get("dateTime", start.get("date", ""))[:10]
        lines.append(f"- id={eid} | {summary} | {date}")
    return "\n".join(lines)


def extract_events(client, subject, body, existing_events, email_date=""):
    """Send email content + existing events to OpenAI and return extracted events."""
    today = datetime.now().strftime("%Y-%m-%d")
    existing_str = format_existing_events(existing_events)
    user_message = (
        f"Today's date: {today}\n"
        f"Email date: {email_date}\n\n"
        f"Subject: {subject}\n\n"
        f"Body:\n{body[:6000]}\n\n"
        f"Existing calendar events:\n{existing_str}"
    )

    response = client.chat.completions.create(
        model="gpt-5.2",
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
    )

    result = json.loads(response.choices[0].message.content)
    return result.get("events", [])


def build_cal_event_body(event_data):
    """Build a Google Calendar event body dict from extracted event data."""
    description_parts = []
    if event_data.get("description"):
        description_parts.append(event_data["description"])
    if event_data.get("parent_actions"):
        description_parts.append("\nParent action items:")
        for item in event_data["parent_actions"]:
            if isinstance(item, dict):
                action = item.get("action", "")
                link = item.get("link")
                if link:
                    description_parts.append(f"  - {action}: {link}")
                else:
                    description_parts.append(f"  - {action}")
            else:
                description_parts.append(f"  - {item}")

    description = "\n".join(description_parts)

    cal_event = {
        "summary": event_data.get("title", "School Event"),
        "description": description,
    }

    start_date = event_data.get("start_date")
    end_date = event_data.get("end_date") or start_date
    start_time = event_data.get("start_time")
    end_time = event_data.get("end_time")

    if start_time and end_time:
        cal_event["start"] = {
            "dateTime": f"{start_date}T{start_time}:00",
            "timeZone": "America/Los_Angeles",
        }
        cal_event["end"] = {
            "dateTime": f"{end_date}T{end_time}:00",
            "timeZone": "America/Los_Angeles",
        }
    elif start_time:
        cal_event["start"] = {
            "dateTime": f"{start_date}T{start_time}:00",
            "timeZone": "America/Los_Angeles",
        }
        hour, minute = map(int, start_time.split(":"))
        end_h = hour + 1
        cal_event["end"] = {
            "dateTime": f"{end_date}T{end_h:02d}:{minute:02d}:00",
            "timeZone": "America/Los_Angeles",
        }
    else:
        # All-day event — end date is exclusive, so add one day
        exclusive_end = (datetime.strptime(end_date, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        cal_event["start"] = {"date": start_date}
        cal_event["end"] = {"date": exclusive_end}

    return cal_event


def fetch_existing_events(service, calendar_id, since_date=None):
    """Fetch events from the calendar starting from since_date (or now) up to 31 days out."""
    if since_date:
        time_min = since_date.replace(tzinfo=timezone.utc).isoformat()
    else:
        time_min = datetime.now(timezone.utc).isoformat()
    time_max = (datetime.now(timezone.utc) + timedelta(days=31)).isoformat()

    existing = []
    page_token = None
    while True:
        resp = service.events().list(
            calendarId=calendar_id,
            timeMin=time_min,
            timeMax=time_max,
            singleEvents=True,
            maxResults=250,
            pageToken=page_token,
        ).execute()
        existing.extend(resp.get("items", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    return existing


def find_existing_by_id(existing_events, event_id):
    """Look up an existing calendar event by its ID."""
    for event in existing_events:
        if event.get("id") == event_id:
            return event
    return None


def main():
    load_dotenv()

    address = os.getenv("GMAIL_ADDRESS")
    password = os.getenv("GMAIL_APP_PASSWORD")
    openai_key = os.getenv("OPENAI_API_KEY")
    calendar_id = os.getenv("GOOGLE_CALENDAR_ID", "primary")
    school_email_from = os.getenv("SCHOOL_EMAIL_FROM")

    if not address or not password:
        print("Error: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your .env file.")
        sys.exit(1)

    if not openai_key:
        print("Error: set OPENAI_API_KEY in your .env file.")
        sys.exit(1)

    if not school_email_from:
        print("Error: set SCHOOL_EMAIL_FROM in your .env file (e.g. @school.edu).")
        sys.exit(1)

    openai_client = OpenAI(api_key=openai_key)
    calendar_service = get_calendar_service()

    since_date = load_last_run_date()

    # Load existing calendar events from last run date onward
    print("Loading existing calendar events...")
    existing_events = fetch_existing_events(calendar_service, calendar_id, since_date)
    print(f"Found {len(existing_events)} existing event(s) in calendar.\n")

    # Build IMAP search criteria
    search_parts = [f'FROM "{school_email_from}"']
    if since_date:
        search_parts.append(f'SINCE {since_date.strftime(DATE_FMT)}')
        print(f"Fetching emails from {school_email_from} since {since_date.strftime(DATE_FMT)} ...")
    else:
        print(f"No previous run recorded. Fetching all emails from {school_email_from} ...")

    search_query = "(" + " ".join(search_parts) + ")"

    try:
        imap = imaplib.IMAP4_SSL("imap.gmail.com")
        imap.login(address, password)
    except imaplib.IMAP4.error as e:
        print(f"Login failed: {e}")
        print("Make sure you are using a Gmail App Password, not your regular password.")
        print("Generate one at https://myaccount.google.com/apppasswords")
        sys.exit(1)

    imap.select("INBOX")
    status, data = imap.search(None, search_query)

    if status != "OK":
        print("IMAP search failed.")
        imap.logout()
        sys.exit(1)

    msg_ids = data[0].split()
    print(f"Found {len(msg_ids)} email(s).\n")

    events_created = 0
    events_updated = 0

    for num in msg_ids:
        status, msg_data = imap.fetch(num, "(RFC822)")
        if status != "OK":
            continue

        raw = msg_data[0][1]
        msg = email.message_from_bytes(raw)

        subject = decode_header_value(msg.get("Subject", "(no subject)"))
        sender = decode_header_value(msg.get("From", ""))
        date = msg.get("Date", "")
        body = get_text_body(msg)

        if "you signed up for" in body.lower():
            print(f"Skipping sign-up confirmation: {subject}")
            continue

        print(f"Processing: {subject}")

        # Extract events via OpenAI (with existing calendar context)
        try:
            events = extract_events(openai_client, subject, body, existing_events, date)
        except Exception as e:
            print(f"  Error extracting events: {e}")
            continue

        if not events:
            print("  No events found in this email.")
            continue

        print(f"  Found {len(events)} event(s).")

        for evt in events:
            if not evt.get("start_date"):
                print(f"  Skipping event with no date: {evt.get('title', 'Untitled')}")
                continue
            try:
                cal_body = build_cal_event_body(evt)
                match_id = evt.get("matching_event_id")
                match = find_existing_by_id(existing_events, match_id) if match_id else None

                if match:
                    # Update existing event
                    updated = calendar_service.events().update(
                        calendarId=calendar_id,
                        eventId=match["id"],
                        body=cal_body,
                    ).execute()
                    events_updated += 1
                    print(f"  Updated: {evt.get('title', 'Untitled')} on {evt.get('start_date', '?')}")
                    link = updated.get("htmlLink")
                    if link:
                        print(f"    Link: {link}")
                else:
                    # Create new event
                    created = calendar_service.events().insert(
                        calendarId=calendar_id, body=cal_body
                    ).execute()
                    # Add to existing list so later emails won't duplicate it
                    existing_events.append(created)
                    events_created += 1
                    print(f"  Created: {evt.get('title', 'Untitled')} on {evt.get('start_date', '?')}")
                    link = created.get("htmlLink")
                    if link:
                        print(f"    Link: {link}")
            except Exception as e:
                print(f"  Error processing calendar event: {e}")

        print()

    imap.logout()

    # Record today as the start date for the next run
    save_last_run_date()
    print(f"Done. Created {events_created}, updated {events_updated} calendar event(s).")
    print(f"Next run will start from {datetime.now(timezone.utc).strftime(DATE_FMT)}.")


if __name__ == "__main__":
    main()
