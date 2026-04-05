import os
import base64
import email as email_lib
import email.mime.text
from datetime import datetime, timedelta, timezone
import re
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/drive.readonly",
]

BASE_DIR = os.path.dirname(__file__)
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
TOKEN_FILE = os.path.join(BASE_DIR, "token.json")


def _get_creds() -> Credentials:
    creds = None
    if os.path.exists(TOKEN_FILE):
        creds = Credentials.from_authorized_user_file(TOKEN_FILE, SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(CREDENTIALS_FILE, SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())
    return creds


def _calendar():
    return build("calendar", "v3", credentials=_get_creds())


def _gmail():
    return build("gmail", "v1", credentials=_get_creds())


def _drive():
    return build("drive", "v3", credentials=_get_creds())


def _extract_drive_file_id(source: str) -> str | None:
    if not source:
        return None
    match = re.search(r"/d/([a-zA-Z0-9_-]+)", source)
    if match:
        return match.group(1)
    match = re.search(r"[?&]id=([a-zA-Z0-9_-]+)", source)
    if match:
        return match.group(1)
    if re.fullmatch(r"[a-zA-Z0-9_-]{20,}", source.strip()):
        return source.strip()
    return None


def get_drive_file_text(source: str) -> dict:
    file_id = _extract_drive_file_id(source)
    if not file_id:
        raise ValueError("Could not extract a Google Drive file ID from that source.")

    meta = _drive().files().get(
        fileId=file_id,
        fields="id,name,mimeType,webViewLink",
        supportsAllDrives=True,
    ).execute()
    mime_type = meta.get("mimeType", "")
    name = meta.get("name", file_id)
    web_url = meta.get("webViewLink") or source

    if mime_type == "application/vnd.google-apps.document":
        payload = _drive().files().export_media(fileId=file_id, mimeType="text/plain").execute()
        text = payload.decode("utf-8", errors="replace")
    elif mime_type == "application/vnd.google-apps.spreadsheet":
        payload = _drive().files().export_media(fileId=file_id, mimeType="text/csv").execute()
        text = payload.decode("utf-8", errors="replace")
    elif mime_type == "application/pdf":
        payload = _drive().files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        text = payload
    else:
        payload = _drive().files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        if isinstance(payload, bytes):
            text = payload.decode("utf-8", errors="replace")
        else:
            text = str(payload)

    return {
        "id": file_id,
        "name": name,
        "mime_type": mime_type,
        "web_url": web_url,
        "text": text,
    }


# ── Calendar ──────────────────────────────────────────────────────────────────

def get_todays_events() -> str:
    now = datetime.now(timezone.utc)
    start = now.replace(hour=0, minute=0, second=0).isoformat()
    end = now.replace(hour=23, minute=59, second=59).isoformat()

    result = _calendar().events().list(
        calendarId="primary",
        timeMin=start,
        timeMax=end,
        singleEvents=True,
        orderBy="startTime"
    ).execute()

    events = result.get("items", [])
    if not events:
        return "You have no events today."

    lines = []
    for e in events:
        start_str = e["start"].get("dateTime", e["start"].get("date", ""))
        if "T" in start_str:
            dt = datetime.fromisoformat(start_str)
            time_str = dt.strftime("%-I:%M %p")
        else:
            time_str = "all day"
        lines.append(f"{time_str} — {e.get('summary', 'No title')}")

    return "Here are your events today: " + ". ".join(lines) + "."


def create_event(title: str, start_dt: datetime, duration_minutes: int = 60) -> str:
    end_dt = start_dt + timedelta(minutes=duration_minutes)
    event = {
        "summary": title,
        "start": {"dateTime": start_dt.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": "America/Los_Angeles"},
    }
    created = _calendar().events().insert(calendarId="primary", body=event).execute()
    return f"Event '{title}' created for {start_dt.strftime('%-I:%M %p on %B %-d')}."


# ── Gmail ─────────────────────────────────────────────────────────────────────

def get_unread_emails(max_results: int = 5) -> str:
    result = _gmail().users().messages().list(
        userId="me",
        labelIds=["INBOX", "UNREAD"],
        maxResults=max_results
    ).execute()

    messages = result.get("messages", [])
    if not messages:
        return "You have no unread emails."

    summaries = []
    for msg in messages:
        detail = _gmail().users().messages().get(
            userId="me", id=msg["id"], format="metadata",
            metadataHeaders=["From", "Subject"]
        ).execute()
        headers = {h["name"]: h["value"] for h in detail["payload"]["headers"]}
        sender = headers.get("From", "Unknown").split("<")[0].strip()
        subject = headers.get("Subject", "No subject")
        summaries.append(f"From {sender}: {subject}")

    return f"You have {len(messages)} unread emails. " + ". ".join(summaries) + "."


def send_email(to: str, subject: str, body: str) -> str:
    message = email_lib.mime.text.MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    _gmail().users().messages().send(userId="me", body={"raw": raw}).execute()
    return f"Email sent to {to}."
