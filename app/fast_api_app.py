import os
import hmac
import asyncio
import base64
import datetime
import time
from typing import Literal
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from google.genai import Client, types
from google.cloud import texttospeech
from pydantic import BaseModel, Field

from app.agent import (list_gde_emails, list_calendar_events, format_email, format_event, add_prep_block,
                       copy_drive_file, calendar_timezone, login_status, login_expired)
try:
    from app.prompts import PERSONA, BOARD_PROMPT
except ImportError:
    from app.prompts_example import PERSONA, BOARD_PROMPT

app = FastAPI()
app.title = "gde-jester"

# The service is public on Cloud Run, and behind it sit her inbox, calendar
# and Drive. Without this anyone with the URL could read her week and book
# events. Open the URL once with ?key=<jester-access-key secret> and the
# device keeps a cookie for a year. Unset locally, so dev needs no key.
ACCESS_KEY = os.environ.get("JESTER_ACCESS_KEY", "")
ACCESS_COOKIE = "jester_access"


@app.middleware("http")
async def require_key(request: Request, call_next):
    if not ACCESS_KEY:
        return await call_next(request)
    given = request.query_params.get("key")
    if given and hmac.compare_digest(given, ACCESS_KEY):
        response = RedirectResponse(request.url.path or "/", status_code=303,
                                    headers={"Clear-Site-Data": '"cache"'})
        response.set_cookie(ACCESS_COOKIE, ACCESS_KEY, max_age=365 * 86400,
                            secure=True, httponly=True, samesite="lax")
        return response
    if hmac.compare_digest(request.cookies.get(ACCESS_COOKIE, ""), ACCESS_KEY):
        return await call_next(request)
    return PlainTextResponse("This jester only juggles for her owner.", status_code=401)

AGENT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ui_dir = os.path.join(AGENT_DIR, "ui")
if os.path.exists(ui_dir):
    app.mount("/static", StaticFiles(directory=ui_dir), name="static")

    @app.get("/")
    async def serve_ui():
        # Without this, browsers kept serving the previous page from cache
        # after a deploy, still calling endpoints that no longer exist.
        return FileResponse(os.path.join(ui_dir, "index.html"), headers={"Cache-Control": "no-cache"})

# Strict split: Gemini 3.8 Flash does the reasoning (what the jester says),
# Spicy Mayo / Nano Banana 2.1 only draws. Both resolve on Vertex in the
# global region only; us-east1 and us-central1 return 404 for each.
GCP_PROJECT = os.environ.get("JESTER_PROJECT", "gen-lang-client-0221638641")
GCP_LOCATION = os.environ.get("JESTER_LOCATION", "global")
REASONING_MODEL = "gemini-3.8-flash"
IMAGE_MODEL = "gemini-nano-banana-2.1"

RENEW_HOW = "run mint_user_token.py on the Mac to sign me back in"
EXPIRED_LINE = ("Uh oh, boss. My key to your inbox and calendar expired, so I'm totally blind to your GDE stuff. "
                f"Please {RENEW_HOW}.")
EXPIRING_LINE = f"Oh, and heads up, boss: my key to your inbox conks out within a day. Please {RENEW_HOW}."
FALLBACK_LINE = "Huh. The spicy oracle just stared at me. Rude! Ask me again, boss?"
RESCOPE_LINE = ("Aw, nuts. My key opens your inbox but not that door. Run mint_user_token.py once more "
                "so I can scribble on your calendar and Drive.")

# Journey voices threw intermittent 503s on Cloud Run. Chirp 3 HD replaces
# them, and one retry covers the transient case. Laomedeia is the upbeat young
# woman. Gemini TTS can act out a style prompt, but it adds ~7s a reply even
# sentence-parallel, so the comedy lives in the writing instead.
TTS_VOICE = "en-US-Chirp3-HD-Laomedeia"
TTS_RATE = 1.08


class Action(BaseModel):
    kind: Literal["prep_block", "copy_notebook", "open_link"]
    label: str
    title: str = ""
    start: str = ""
    minutes: int = 60
    url: str = ""
    details: str = ""


class Turn(BaseModel):
    heard: str = ""
    say: str
    visual: str = ""
    actions: list[Action] = Field(default_factory=list)


class Item(BaseModel):
    """One juggling ball as the page gets it. Built on the server from the real
    events and emails, so titles, times and links can't drift from the source."""
    kind: Literal["eap", "event", "ta", "gde"]
    title: str
    when: str = ""
    days_left: int = -1
    summary: str
    source: str = ""
    links: list[str] = Field(default_factory=list)
    actions: list[Action] = Field(default_factory=list)


class Pick(BaseModel):
    """What the model decides: which sources are one piece of GDE work, and
    what kind. It never writes the dates or the event details."""
    kind: Literal["eap", "event", "ta", "gde"]
    event_refs: list[str] = Field(default_factory=list)
    email_refs: list[str] = Field(default_factory=list)
    title: str = ""
    when: str = ""
    todo: str = ""
    actions: list[Action] = Field(default_factory=list)


class Picks(BaseModel):
    items: list[Pick] = Field(default_factory=list)


def _speak(text: str):
    """Cloud TTS as base64 mp3, or None so the browser voice takes over."""
    err = None
    for _ in range(2):
        try:
            tts_response = texttospeech.TextToSpeechClient().synthesize_speech(
                input=texttospeech.SynthesisInput(text=text),
                voice=texttospeech.VoiceSelectionParams(language_code="en-US", name=TTS_VOICE),
                audio_config=texttospeech.AudioConfig(audio_encoding=texttospeech.AudioEncoding.MP3,
                                                      speaking_rate=TTS_RATE),
            )
            return base64.b64encode(tts_response.audio_content).decode(), None
        except Exception as e:
            err = str(e)
    return None, err


# One shared client. A throwaway Client() is garbage collected mid-request and
# its HTTP pool closes under it: "Cannot send a request, as the client has
# been closed."
_vertex = None


def _client():
    global _vertex
    if _vertex is None:
        _vertex = Client(vertexai=True, project=GCP_PROJECT, location=GCP_LOCATION)
    return _vertex


# Follow-ups arrive seconds apart. Re-reading Gmail every turn added ~3s for
# nothing, so the inbox and calendar are cached briefly.
CONTEXT_TTL = 120
_context = {"at": 0.0, "emails": "", "cal": "", "email_list": [], "event_list": []}


def _fetch(fn, label, *args):
    try:
        return fn(*args), ""
    except Exception as e:
        return [], f"Failed to fetch {label}: {e}"


async def _inbox_and_calendar(logs, fresh=False):
    if not fresh and time.time() - _context["at"] < CONTEXT_TTL:
        logs.append("Inbox and calendar from the last two minutes.")
        return _context["emails"], _context["cal"]
    logs.append("Reading Gmail and Calendar...")
    (email_list, email_err), (event_list, cal_err) = await asyncio.gather(
        asyncio.to_thread(_fetch, list_gde_emails, "emails",
                          "GDE OR EAP OR TA OR colab OR deadline OR event OR DevFest OR Advocu OR handoff OR spicy-mayo newer_than:30d",
                          25),
        asyncio.to_thread(_fetch, list_calendar_events, "calendar events"),
    )
    emails = email_err or "\n".join(map(format_email, email_list)) or "No recent GDE or EAP emails found."
    cal = cal_err or ("FOUND:\n" + "\n".join(map(format_event, event_list)) if event_list else "No events in the next 30 days.")
    for err, label in ((email_err, "emails"), (cal_err, "calendar")):
        logs.append(err[:160] if err else f"Read {label}.")
    if not (email_err or cal_err):
        _context.update(at=time.time(), emails=emails, cal=cal, email_list=email_list, event_list=event_list)
    return emails, cal


def _now():
    tz = calendar_timezone()
    return datetime.datetime.now(ZoneInfo(tz)), tz


def _sort_board(email_list, event_list):
    now, tz = _now()
    events = {f"E{i}": e for i, e in enumerate(event_list, 1)}
    emails = {f"M{i}": e for i, e in enumerate(email_list, 1)}
    response = _client().models.generate_content(
        model=REASONING_MODEL,
        contents="Pick out my GDE work.",
        config=types.GenerateContentConfig(
            system_instruction=BOARD_PROMPT.format(
                today=now.strftime("%A %Y-%m-%d %H:%M"), tz=tz,
                calendar="\n".join(f"{r} | {format_event(e)}" for r, e in events.items()) or "(none)",
                emails="\n".join(f"{r} | {format_email(e)}" for r, e in emails.items()) or "(none)"),
            response_mime_type="application/json",
            response_schema=Picks,
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    picks = response.parsed if isinstance(response.parsed, Picks) else Picks.model_validate_json(response.text)
    items = [it for p in picks.items if (it := _build_item(p, events, emails, now.date()))]
    return sorted(items, key=lambda it: (it.days_left < 0, it.days_left, it.when))[:12]


def _clock(iso):
    if "T" not in iso:
        return datetime.date.fromisoformat(iso).strftime("%a %b %-d, all day")
    return datetime.datetime.fromisoformat(iso).strftime("%a %b %-d, %-I:%M %p")


def _span(e):
    if "T" not in e["start"]:
        return _clock(e["start"])
    return f"{_clock(e['start'])} to {datetime.datetime.fromisoformat(e['end']).strftime('%-I:%M %p')}"


def _sender(m):
    return m["from"].split("<")[0].strip(' "') or m["from"]


def _build_item(p, events, emails, today):
    """Titles, times, places and links come straight from the calendar and
    Gmail. The model only chose what goes together. When it wrote them, it
    renamed a DevFest debrief into what read as DevFest itself and padded
    summaries with agendas nobody had sent."""
    evs = sorted((events[r] for r in dict.fromkeys(p.event_refs) if r in events), key=lambda e: e["start"])
    mails = [emails[r] for r in dict.fromkeys(p.email_refs) if r in emails]
    if not evs and not mails:
        return None
    if evs:
        title = evs[0]["title"] if len(evs) == 1 else (p.title or evs[0]["title"])
        when = evs[0]["start"]
        lines = [f"{_span(e)}: {e['title']}" + (f", {e['location']}" if e["location"] else "") for e in evs]
        # Invites often open by repeating their own title, and their line
        # breaks come from HTML layout, so flatten and trim that first.
        notes = " ".join(evs[0]["notes"].split()) if len(evs) == 1 else ""
        notes = notes[len(title):].lstrip(" -:") if notes.startswith(title) else notes
        if notes:
            lines.append(notes[:240].rstrip() + ("..." if len(notes) > 240 else ""))
    else:
        title, when, lines = p.title or mails[0]["subject"], "", []
        try:
            when = datetime.date.fromisoformat(p.when[:10]).isoformat() if p.when else ""
        except ValueError:
            pass
    days_left = (datetime.date.fromisoformat(when[:10]) - today).days if when else -1
    if when and days_left < 0:
        return None
    if p.todo:
        lines.append(p.todo)
    links = list(dict.fromkeys(l for src in evs + mails for l in src["links"]))
    sources = (["Calendar"] if evs else []) + [f"Email from {_sender(m)}: {m['subject']}" for m in mails[:2]]
    actions = [a for a in p.actions if a.kind == "prep_block" or a.url in links][:2]
    return Item(kind=p.kind, title=title, when=when, days_left=days_left, summary="\n".join(lines),
                source=" · ".join(sources), links=links[:5], actions=actions)


def _reason(history, message, audio, audio_mime, emails, cal):
    now, tz = _now()
    contents = []
    for h in history[-16:]:
        role = "model" if h.get("role") == "jester" else "user"
        if h.get("text"):
            contents.append(types.Content(role=role, parts=[types.Part.from_text(text=h["text"])]))
    parts = []
    if audio:
        parts.append(types.Part.from_bytes(data=audio, mime_type=audio_mime))
        parts.append(types.Part.from_text(text="(spoken by the user; transcribe into heard, then answer)"))
    else:
        parts.append(types.Part.from_text(text=message))
    contents.append(types.Content(role="user", parts=parts))

    response = _client().models.generate_content(
        model=REASONING_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=PERSONA.format(today=now.strftime("%A %Y-%m-%d %H:%M"), tz=tz,
                                              emails=emails, calendar=cal),
            response_mime_type="application/json",
            response_schema=Turn,
            # Default thinking took ~18s a turn, which kills a conversation.
            # Low still picks sane, clash-free prep slots in ~4s.
            thinking_config=types.ThinkingConfig(thinking_level="low"),
        ),
    )
    turn = response.parsed if isinstance(response.parsed, Turn) else Turn.model_validate_json(response.text)
    turn.say = " ".join(turn.say.split())
    return turn


@app.post("/board")
async def board(request: Request):
    """The silent sweep on page load: fills the juggling balls, says nothing.
    She only speaks when spoken to."""
    data = await request.json() if await request.body() else {}
    logs = []
    emails, cal = await _inbox_and_calendar(logs, fresh=bool(data.get("fresh", True)))
    if login_expired(emails, cal):
        return JSONResponse({"items": [], "logs": logs + ["Google login EXPIRED: " + RENEW_HOW], "login": "expired"})
    try:
        items = await asyncio.to_thread(_sort_board, _context["email_list"], _context["event_list"])
        logs.append(f"Sorted {len(items)} items to juggle.")
    except Exception as e:
        logs.append(f"{REASONING_MODEL} failed sorting the board: {str(e)[:200]}")
        items = []
    return JSONResponse({"items": [i.model_dump() for i in items], "logs": logs,
                         "login": login_status()})


@app.post("/converse")
async def converse(request: Request):
    """One turn of conversation, typed or spoken. The client keeps the history
    and sends it back, so any Cloud Run instance can answer a follow-up."""
    data = await request.json()
    history = data.get("history", [])
    message = (data.get("message") or "").strip()
    audio = base64.b64decode(data["audio"]) if data.get("audio") else None
    audio_mime = (data.get("audio_mime") or "audio/webm").split(";")[0]
    if not message and not audio:
        message = ("Sweep my inbox and calendar. What GDE and EAP work is pending, "
                   "what deadlines are coming, and what should I do first?")
    logs = [f"{REASONING_MODEL} reasons, Spicy-Mayo ({IMAGE_MODEL}) draws."]

    emails, cal = await _inbox_and_calendar(logs)

    turn = None
    if login_expired(emails, cal):
        turn = Turn(say=EXPIRED_LINE)
        logs.append("Google login EXPIRED: " + RENEW_HOW)
    else:
        logs.append(f"Asking {REASONING_MODEL}" + (" (with your voice)..." if audio else "..."))
        try:
            turn = await asyncio.to_thread(_reason, history, message, audio, audio_mime, emails, cal)
            logs.append(f"{REASONING_MODEL} answered with {len(turn.actions)} action(s).")
        except Exception as e:
            logs.append(f"{REASONING_MODEL} failed: {str(e)[:200]}")
            turn = Turn(heard=message, say=FALLBACK_LINE)
        if login_status() == "expiring":
            turn.say += " " + EXPIRING_LINE
            logs.append("Google login expires within a day: " + RENEW_HOW)
    if not turn.heard:
        turn.heard = message

    audio_out, tts_error = await asyncio.to_thread(_speak, turn.say)
    if tts_error:
        logs.append(f"Cloud TTS unavailable, using browser voice: {tts_error[:120]}")

    return JSONResponse({**turn.model_dump(), "logs": logs, "audio": audio_out,
                         "paint": bool(turn.visual) and turn.say not in (FALLBACK_LINE, EXPIRED_LINE)})


def _paint(prompt):
    response = _client().models.generate_content(
        model=IMAGE_MODEL,
        contents=("One square, bold, colorful illustration in a playful royal court style. "
                  f"No words or lettering. {prompt}"),
        config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
    )
    parts = response.candidates[0].content.parts if response.candidates else []
    for part in parts or []:
        if part.inline_data:
            return f"data:{part.inline_data.mime_type};base64," + base64.b64encode(part.inline_data.data).decode()
    return None


@app.post("/paint")
async def paint(request: Request):
    """Fetched by the page while he is already talking, so the slowest model
    never holds up the voice."""
    data = await request.json()
    try:
        image = await asyncio.to_thread(_paint, data.get("prompt") or data.get("text", ""))
        return JSONResponse({"image": image, "log": "Spicy-Mayo painted." if image else "Spicy-Mayo returned no image."})
    except Exception as e:
        return JSONResponse({"image": None, "log": f"Spicy-Mayo failed: {str(e)[:200]}"})


def _needs_rescope(err: str) -> bool:
    return "insufficient" in err.lower() and ("scope" in err.lower() or "permission" in err.lower())


@app.post("/act")
async def act(request: Request):
    """Runs one action the jester offered, after a tap. Nothing writes to her
    calendar or Drive without one."""
    a = Action.model_validate(await request.json())
    if a.kind == "prep_block":
        result = await asyncio.to_thread(add_prep_block, a.title or a.label, a.start, a.minutes, a.details)
    elif a.kind == "copy_notebook":
        result = await asyncio.to_thread(copy_drive_file, a.url, "", a.title)
    else:
        return JSONResponse({"ok": True, "link": a.url, "say": "Off ya go!"})

    if result.startswith("OK "):
        link = result.split()[1]
        _context["at"] = 0  # the calendar just changed
        say = ("Booked it! That hour's all yours, boss. No sneaky meetings allowed." if a.kind == "prep_block"
               else "Ta-da! Your notebook's sittin' pretty in your Drive.")
        audio, _ = await asyncio.to_thread(_speak, say)
        return JSONResponse({"ok": True, "link": link, "say": say, "audio": audio})
    say = RESCOPE_LINE if _needs_rescope(result) else "Whoopsie. That trick flopped, boss."
    audio, _ = await asyncio.to_thread(_speak, say)
    return JSONResponse({"ok": False, "error": result[:300], "say": say, "audio": audio})


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
