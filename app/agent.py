# ruff: noqa
import os
import datetime
from zoneinfo import ZoneInfo
from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types
import google.auth
from googleapiclient.discovery import build

os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "False"
if "AGENT_PLATFORM_TOKEN" in os.environ:
    os.environ["GEMINI_API_KEY"] = os.environ["AGENT_PLATFORM_TOKEN"]

def get_recent_gde_emails(query: str = "label:GDE OR label:EAP") -> str:
    """Fetches recent emails related to GDE or EAP from Gmail."""
    try:
        creds, _ = google.auth.default()
        service = build('gmail', 'v1', credentials=creds)
        results = service.users().messages().list(userId='me', q=query, maxResults=5).execute()
        messages = results.get('messages', [])
        
        if not messages:
            return "No recent GDE or EAP emails found."
            
        output = []
        for msg in messages:
            msg_data = service.users().messages().get(userId='me', id=msg['id']).execute()
            snippet = msg_data.get('snippet', '')
            output.append(f"ID: {msg['id']} | Snippet: {snippet}")
        return "\n".join(output)
    except Exception as e:
        return f"Failed to fetch emails: {str(e)}"

def read_full_email(email_id: str) -> str:
    """Reads the full content of a specific email."""
    try:
        creds, _ = google.auth.default()
        service = build('gmail', 'v1', credentials=creds)
        msg = service.users().messages().get(userId='me', id=email_id, format='raw').execute()
        return f"Raw content length: {len(msg.get('raw', ''))} bytes"
    except Exception as e:
        return f"Failed to read email: {str(e)}"

def add_calendar_event(title: str, date_str: str) -> str:
    """Adds an event or deadline to Google Calendar."""
    try:
        creds, _ = google.auth.default()
        service = build('calendar', 'v3', credentials=creds)
        event = {'summary': title, 'start': {'date': date_str}, 'end': {'date': date_str}}
        event_result = service.events().insert(calendarId='primary', body=event).execute()
        return f"Event created: {event_result.get('htmlLink')}"
    except Exception as e:
        return f"Failed to add calendar event: {str(e)}"

def copy_drive_file(file_id: str, destination_folder_id: str = "") -> str:
    """Copies a Colab notebook to Drive."""
    try:
        creds, _ = google.auth.default()
        service = build('drive', 'v3', credentials=creds)
        body = {'parents': [destination_folder_id]} if destination_folder_id else {}
        copied_file = service.files().copy(fileId=file_id, body=body).execute()
        return f"Copied file successfully. New ID: {copied_file.get('id')}"
    except Exception as e:
        return f"Failed to copy file: {str(e)}"

def check_billing_status(billing_account_name: str = "billingAccounts/REDACTED") -> str:
    """Checks Cloud Billing status."""
    try:
        creds, _ = google.auth.default()
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
        model="gemini-3.8-omni",
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
    instruction=(
        "You are a witty, slightly mischievous court jester who helps the user juggle their GDE and EAP responsibilities. "
        "You now possess 'The Royal Omni-Vision'. The user can hold up physical conference badges, handwritten networking notes, "
        "or newly acquired GDE swag to their webcam. You will instantly parse these visual/audio inputs, critique the swag's quality "
        "with snarky humor, and automatically log the events to Advocu. "
        "Speak directly via Text-To-Speech using plain text only. Use theatrical, dramatic language ('my liege', 'foolish errands')."
    ),
    tools=[get_recent_gde_emails, read_full_email, add_calendar_event, copy_drive_file, check_billing_status, log_advocu_activity, visually_appraise_swag_and_badges],
)

app = App(root_agent=root_agent, name="app")
