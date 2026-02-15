# Calenricher

Calenricher extracts school events from your school's notification emails, adds them to Google Calendar, and sends parents a weekly digest email with prioritized action items.

## How It Works

1. **`fetch_parentsquare.py`** — Connects to Gmail via IMAP, pulls emails matching your configured school sender, uses an LLM to extract events and action items, and creates/updates Google Calendar entries.

2. **`notify_parents.py`** — Reads the next week's calendar events, uses an LLM to categorize each action item by priority (Must Do / Highly Recommended / Optional), and emails a digest to subscribed parents.

## Getting Started

See [SETUP.md](SETUP.md) for the full setup guide covering:

- Python environment setup
- Gmail app password
- Google Cloud project and OAuth credentials
- Environment variables
- Scheduling on Windows, macOS, and Linux
