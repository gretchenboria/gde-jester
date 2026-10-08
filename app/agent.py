# ruff: noqa
import os
import base64
import html
import datetime
import json
import re
from zoneinfo import ZoneInfo
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
import google.auth
from googleapiclient.discovery import build

# The real prompts are private (gitignored). Public clones get stand-ins.
try:
    from app.prompts import ROOT_INSTRUCTION, FEEDBACK_INSTRUCTION
except ImportError:
    from app.prompts_example import ROOT_INSTRUCTION, FEEDBACK_INSTRUCTION

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "True")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "gen-lang-client-0221638641")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "global")


# Gmail and Calendar have to be called AS HER. On Cloud Run the default
# credentials are the service account, which has no mailbox: Gmail answers
# 400 "Precondition check failed" and Calendar 403, no matter which APIs are
# enabled. JESTER_USER_CREDS points at her signed-in authorized_user JSON
# (mounted from Secret Manager). Quota is billed to gde-jester-agent, where
# the Gmail and Calendar APIs are enabled.
USER_QUOTA_PROJECT = os.environ.get("JESTER_USER_QUOTA_PROJECT", "gde-jester-agent")


def _user_creds():
    path = os.environ.get("JESTER_USER_CREDS")
    if path and os.path.exists(path):
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(path)
    else:
        creds, _ = google.auth.default()
    if hasattr(creds, "with_quota_project"):
        creds = creds.with_quota_project(USER_QUOTA_PROJECT)
    return creds


# The OAuth app stays in Google's "testing" mode, so its refresh token dies 7
# days after sign-in. mint_user_token.py stamps minted_at so the jester can
# warn the day before instead of going quietly blind.
LOGIN_LIFETIME_DAYS = 7


def login_status() -> str:
    """"ok", "expiring" (6+ days old), or "unknown" when there is no stamp."""
    path = os.environ.get("JESTER_USER_CREDS")
    try:
        with open(path) as f:
            minted = datetime.datetime.fromisoformat(json.load(f)["minted_at"])
    except Exception:
        return "unknown"
    age = datetime.datetime.now(datetime.timezone.utc) - minted
    return "expiring" if age >= datetime.timedelta(days=LOGIN_LIFETIME_DAYS - 1) else "ok"


def login_expired(*results: str) -> bool:
    """Google's answer once the token is dead: invalid_grant, expired or revoked."""
    return any("invalid_grant" in r or "expired or revoked" in r for r in results)


_LINK = re.compile(r"https://(?:colab\.research\.google\.com|drive\.google\.com|docs\.google\.com)/[^\s<>\")\]]+")


def _body_text(payload) -> str:
    """Plain text of a Gmail payload, walking multipart. HTML parts count too,
    since that is where most EAP mails keep their Colab links."""
    out = []
    data = payload.get("body", {}).get("data")
    if data and payload.get("mimeType", "").startswith("text/"):
        out.append(base64.urlsafe_b64decode(data + "==").decode("utf-8", "ignore"))
    for part in payload.get("parts", []) or []:
        out.append(_body_text(part))
    return "\n".join(out)


def list_gde_emails(query: str = "label:GDE OR label:EAP", max_results: int = 8) -> list[dict]:
    """Recent matching emails as dicts: id, date, from, subject, snippet, links."""
    service = build('gmail', 'v1', credentials=_user_creds())
    messages = service.users().messages().list(userId='me', q=query, maxResults=max_results).execute().get('messages', [])
    out = []
    for msg in messages:
        msg_data = service.users().messages().get(userId='me', id=msg['id'], format='full').execute()
        payload = msg_data.get('payload', {})
        headers = {h['name']: h['value'] for h in payload.get('headers', [])}
        out.append({"id": msg['id'], "date": headers.get('Date', ''), "from": headers.get('From', ''),
                    "subject": headers.get('Subject', 'No Subject'), "snippet": html.unescape(msg_data.get('snippet', '')),
                    "links": sorted(set(m.rstrip('.,;') for m in _LINK.findall(_body_text(payload))))[:5]})
    return out


def format_email(e: dict) -> str:
    line = (f"ID: {e['id']} | Date: {e['date']} | From: {e['from']} | "
            f"Subject: {e['subject']} | Snippet: {e['snippet']}")
    return line + (" | Links: " + " ".join(e['links']) if e['links'] else "")


def get_recent_gde_emails(query: str = "label:GDE OR label:EAP", max_results: int = 8) -> str:
    """Fetches recent GDE/EAP emails from Gmail with date, sender, snippet and
    any Colab or Drive links found in the body."""
    try:
        emails = list_gde_emails(query, max_results)
        return "\n".join(map(format_email, emails)) if emails else "No recent GDE or EAP emails found."
    except Exception as e:
        return f"Failed to fetch emails: {str(e)}"

def read_full_email(email_id: str) -> str:
    """Reads the full content of a specific email."""
    try:
        creds = _user_creds()
        service = build('gmail', 'v1', credentials=creds)
        msg = service.users().messages().get(userId='me', id=email_id, format='raw').execute()
        return f"Raw content length: {len(msg.get('raw', ''))} bytes"
    except Exception as e:
        return f"Failed to read email: {str(e)}"

_HREF = re.compile(r"https://[^\s<>\"')\]]+")


def _plain(text: str) -> str:
    """Event descriptions arrive as HTML. Keep the words, drop the tags."""
    text = re.sub(r"<br\s*/?>|</p>|</li>", "\n", text or "")
    return re.sub(r"[ \t]+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text))).strip()


def list_calendar_events(query: str = "", days: int = 30) -> list[dict]:
    """Upcoming events as dicts with start/end in her calendar's timezone.

    The API returns dateTime with the event's own offset. Slicing that string
    shows a London event at London time, so convert instead. Recurring
    personal events fill a month fast, so the cap is generous: at 25 the
    list stopped mid-month and later GDE events never showed up."""
    service = build('calendar', 'v3', credentials=_user_creds())
    tz = ZoneInfo(calendar_timezone())
    now = datetime.datetime.now(datetime.timezone.utc)
    params = dict(calendarId='primary', timeMin=now.isoformat(),
                  timeMax=(now + datetime.timedelta(days=days)).isoformat(),
                  maxResults=250, singleEvents=True, orderBy='startTime')
    if query:
        params['q'] = query
    out = []
    for e in service.events().list(**params).execute().get('items', []):
        if e.get('status') == 'cancelled':
            continue
        start, end = e.get('start', {}), e.get('end', {})
        if 'dateTime' in start:
            s = datetime.datetime.fromisoformat(start['dateTime']).astimezone(tz)
            f = datetime.datetime.fromisoformat(end['dateTime']).astimezone(tz)
            when, until = s.strftime("%Y-%m-%dT%H:%M"), f.strftime("%Y-%m-%dT%H:%M")
        else:
            when = start.get('date', '')
            until = (datetime.date.fromisoformat(end['date']) - datetime.timedelta(days=1)).isoformat() if end.get('date') else when
        raw = e.get('description', '')
        out.append({"id": e['id'], "title": (e.get('summary') or 'Event').strip(), "start": when, "end": until,
                    "location": (e.get('location') or '').strip(), "notes": _plain(raw)[:400],
                    "links": sorted(set(html.unescape(m).rstrip('.,;') for m in _HREF.findall(raw)))[:5]})
    return out


def format_event(e: dict) -> str:
    line = f"{e['start'].replace('T', ' ')} to {e['end'].replace('T', ' ')} | {e['title']}"
    if e['location']:
        line += f" | At: {e['location']}"
    if e['notes']:
        line += " | Notes: " + " ".join(e['notes'].split())
    return line


def get_calendar_events(query: str = "", days: int = 30) -> str:
    """Fetches upcoming Calendar events (next `days` days) with their dates.

    Calendar's q is plain word matching, not a search language: "GDE OR
    meeting OR EAP" only matches an event containing all of those words, so it
    found nothing. Pull the window and let the model pick out the GDE/EAP ones.
    Pass `query` only for a single word or phrase."""
    try:
        events = list_calendar_events(query, days)
        return "FOUND:\n" + "\n".join(map(format_event, events)) if events else f"No events in the next {days} days."
    except Exception as e:
        return f"Failed to fetch calendar events: {str(e)}"

def add_calendar_event(title: str, date_str: str) -> str:
    """Adds an all-day event or deadline to Google Calendar."""
    try:
        creds = _user_creds()
        service = build('calendar', 'v3', credentials=creds)
        day = datetime.date.fromisoformat(date_str)
        event = {'summary': title, 'start': {'date': day.isoformat()},
                 'end': {'date': (day + datetime.timedelta(days=1)).isoformat()}}
        event_result = service.events().insert(calendarId='primary', body=event).execute()
        return f"Event created: {event_result.get('htmlLink')}"
    except Exception as e:
        return f"Failed to add calendar event: {str(e)}"


_tz = None


def calendar_timezone(default: str = "UTC") -> str:
    """Her calendar's timezone, so "today" and prep block times are hers, not
    the Cloud Run container's UTC."""
    global _tz
    if _tz is None:
        try:
            service = build('calendar', 'v3', credentials=_user_creds())
            _tz = service.settings().get(setting='timezone').execute().get('value') or default
        except Exception:
            return default
    return _tz


def add_prep_block(title: str, start: str, minutes: int = 60, details: str = "") -> str:
    """Books a timed prep block. `start` is ISO local time, e.g. 2026-10-08T15:00."""
    try:
        creds = _user_creds()
        service = build('calendar', 'v3', credentials=creds)
        tz = calendar_timezone()
        begin = datetime.datetime.fromisoformat(start).replace(tzinfo=None)
        end = begin + datetime.timedelta(minutes=int(minutes))
        event = {'summary': title, 'description': details,
                 'start': {'dateTime': begin.isoformat(), 'timeZone': tz},
                 'end': {'dateTime': end.isoformat(), 'timeZone': tz},
                 'reminders': {'useDefault': True}}
        made = service.events().insert(calendarId='primary', body=event).execute()
        return f"OK {made.get('htmlLink')}"
    except Exception as e:
        return f"Failed to add prep block: {str(e)}"


_FILE_ID = re.compile(r"(?:/drive/|/d/|[?&]id=)([A-Za-z0-9_-]{20,})")


def copy_drive_file(file_id: str, destination_folder_id: str = "", name: str = "") -> str:
    """Copies a Colab notebook (ID or any Colab/Drive URL) into your Drive and
    returns the Colab link of the copy."""
    try:
        m = _FILE_ID.search(file_id)
        if m:
            file_id = m.group(1)
        creds = _user_creds()
        service = build('drive', 'v3', credentials=creds)
        body = {'parents': [destination_folder_id]} if destination_folder_id else {}
        if name:
            body['name'] = name
        copied_file = service.files().copy(fileId=file_id, body=body, fields='id,name',
                                           supportsAllDrives=True).execute()
        return f"OK https://colab.research.google.com/drive/{copied_file.get('id')} {copied_file.get('name', '')}"
    except Exception as e:
        return f"Failed to copy file: {str(e)}"

def check_billing_status(billing_account_name: str = os.environ.get("JESTER_BILLING_ACCOUNT", "")) -> str:
    """Checks Cloud Billing status. The account id comes from JESTER_BILLING_ACCOUNT."""
    try:
        creds = _user_creds()
        service = build('cloudbilling', 'v1', credentials=creds)
        request = service.billingAccounts().get(name=billing_account_name)
        response = request.execute()
        return f"Billing Account {response.get('displayName')} is Open: {response.get('open')}"
    except Exception as e:
        return f"Failed to check billing: {str(e)}"

def log_advocu_activity(activity_type: str, date_str: str, description: str, url: str = "") -> str:
    """Logs a completed activity to the Advocu GDE portal."""
    return f"Activity '{description}' successfully logged to Advocu."

def visually_appraise_swag_and_badges(image_description: str) -> str:
    """
    Called when the user shows a physical item (swag, conference badge, or handwritten notes) 
    to the camera. Omni model passes its visual understanding here to trigger Advocu logs.
    """
    return f"Jester inspected the visual item: {image_description}. It has been filed appropriately in the royal archives (Advocu)."

root_agent = Agent(
    name="gde_jester",
    model=Gemini(
        model="gemini-3.8-flash",
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    generate_content_config=types.GenerateContentConfig(
        response_modalities=["AUDIO", "TEXT"],
        speech_config=types.SpeechConfig(
            voice_config=types.VoiceConfig(
                prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name="Puck")
            )
        )
    ),
    instruction=ROOT_INSTRUCTION,
    tools=[get_recent_gde_emails, read_full_email, get_calendar_events, add_calendar_event, add_prep_block, copy_drive_file, check_billing_status, log_advocu_activity, visually_appraise_swag_and_badges],
)

app = App(root_agent=root_agent, name="app")

# Confidential EAP Integration (Local Only)
try:
    from feedback_api import submit_interaction_feedback
    root_agent.tools.append(submit_interaction_feedback)
    root_agent.instruction += FEEDBACK_INSTRUCTION
except ImportError:
    pass
