"""One-time sign-in so the jester can read Gmail and Calendar as you, book
prep blocks, and copy EAP Colab notebooks into your Drive.

    .venv/bin/python mint_user_token.py ~/Downloads/client_secret_XXXX.json

Opens a browser, you approve, and the refresh token lands in
~/.config/gde-jester/user_creds.json (outside the repo, mode 600). That file
is pushed to Secret Manager (jester-user-creds) for Cloud Run.
Run it again whenever the jester says its key has expired (every 7 days).
"""
import datetime
import json
import subprocess
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/calendar.readonly",
    # Booking prep blocks.
    "https://www.googleapis.com/auth/calendar.events",
    # Copying a notebook someone shared with you. drive.file only covers files
    # this app created, so the copy needs the full scope.
    "https://www.googleapis.com/auth/drive",
]
OUT = os.path.expanduser("~/.config/gde-jester/user_creds.json")

if len(sys.argv) > 1 and sys.argv[1]:
    flow = InstalledAppFlow.from_client_secrets_file(sys.argv[1], SCOPES)
else:
    # No JSON download: paste the two values from the Console's client page.
    from getpass import getpass
    client_id = input("Client ID: ").strip()
    client_secret = getpass("Client secret (hidden): ").strip()
    flow = InstalledAppFlow.from_client_config({"installed": {
        "client_id": client_id,
        "client_secret": client_secret,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }}, SCOPES)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
if not creds.refresh_token:
    sys.exit("No refresh token came back. Remove the app at myaccount.google.com/permissions and run again.")

os.makedirs(os.path.dirname(OUT), mode=0o700, exist_ok=True)
info = json.loads(creds.to_json())
info["quota_project_id"] = "gde-jester-agent"
info["minted_at"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
fd = os.open(OUT, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
with os.fdopen(fd, "w") as f:
    json.dump(info, f)
print("Saved", OUT)

# Hand it to the live jester too, so renewing is this one command.
r = subprocess.run(["gcloud", "secrets", "versions", "add", "jester-user-creds",
                    "--project", "gde-jester-agent", "--data-file", OUT],
                   capture_output=True, text=True)
print("Pushed to Secret Manager." if r.returncode == 0 else "Secret Manager push failed: " + r.stderr.strip())
