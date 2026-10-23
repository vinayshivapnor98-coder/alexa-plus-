"""Local web interface for AlexaPlus Personal Agent.

Run alongside the MCP server with: python web_app.py
This is a local hackathon prototype. Do not expose it to the public internet.
"""
from __future__ import annotations

import json
import os
import re
import hashlib
import secrets
import uuid
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import send_file
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from flask import Flask, jsonify, render_template, request
try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv(Path(__file__).resolve().parent / ".env", override=True)
except ImportError:
    pass
import server as core
import smart
import features
import random
import subprocess
import sys
import webbrowser
from urllib.parse import urlparse


def get_ai_key() -> str:
    key = os.getenv("AI_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY") or os.getenv("OPENAI_API_KEY") or ""
    return key.strip().strip('"').strip("'")

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False
app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 16 MB upload limit
FAMILY_AUDIO_DIR = Path(__file__).resolve().parent / "data" / "family_audio"
FAMILY_AUDIO_DIR.mkdir(parents=True, exist_ok=True)


_LAST_PERSON = {"slug": "default"}


@app.before_request
def _select_person():
    """Every browser carries a 'person' cookie; all data calls then use that person's own database."""
    slug = core.slugify_person(request.cookies.get("person") or "default")
    if slug != "default" and not core.person_db_path(slug).exists():
        features.ensure_person(slug)
    core.CURRENT_PERSON.set(slug)
    if slug != _LAST_PERSON["slug"]:       # never leave one person's unlocked password vault open for another
        _LAST_PERSON["slug"] = slug
        try:
            core.lock_password_vault()
        except Exception:  # noqa: BLE001
            pass


@app.get("/api/people")
def people_list():
    return ok({"people": core.list_people(), "current": core.CURRENT_PERSON.get()})


@app.post("/api/people")
def people_create():
    name = str(payload().get("name", "")).strip()
    slug = core.slugify_person(name)
    if not name or slug == "default" and name.lower() not in {"default", "main"}:
        return ok({"ok": False, "message": "Enter a name (letters and numbers)."}, 400)
    features.ensure_person(slug)
    return ok({"ok": True, "person": slug, "people": core.list_people()}, 201)


@app.route("/api/location", methods=["GET", "POST"])
def location_api():
    if request.method == "GET":
        return ok({"location": features.last_location()})
    d = payload()
    try:
        lat, lng = float(d.get("lat")), float(d.get("lng"))
        if not (-90 <= lat <= 90 and -180 <= lng <= 180):
            raise ValueError
    except (TypeError, ValueError):
        return ok({"ok": False, "message": "Invalid coordinates."}, 400)
    acc = d.get("accuracy")
    features.save_location(lat, lng, float(acc) if acc not in (None, "") else None)
    return ok({"ok": True})


@app.post("/api/study-plan")
def study_plan_api():
    d = payload()
    days = max(1, min(14, int(d.get("days") or 7)))
    res = features.build_study_plan(days=days, save=True)
    ins = res["insights"]
    return ok({"ok": True, "plan": res["plan"], "has_history": ins["has_history"], "best_hour": ins["best_hour"],
               "daily_minutes": ins["budget"], "weak_subjects": ins["weak"], "overdue": len(ins["overdue"])})


@app.get("/api/notifications")
def notifications_api():
    return ok({"notifications": features.new_notifications(mark=request.args.get("peek") != "1")})


def payload():
    return request.get_json(silent=True) or request.form.to_dict()


def ok(data, status=200):
    return jsonify(data), status


@app.get("/")
def home():
    return render_template("index.html")

@app.get("/visitor")
def visitor_portal():
    """Public-facing visitor form. Do not expose homeowner records on this route."""
    return render_template("visitor.html")


def ensure_extra_tables(path=None):
    import sqlite3
    with sqlite3.connect(path or core.DB_PATH) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS deliveries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          item_name TEXT NOT NULL,
          retailer TEXT NOT NULL DEFAULT '',
          courier TEXT NOT NULL DEFAULT '',
          tracking_number TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'Order confirmed',
          estimated_date TEXT NOT NULL DEFAULT '',
          recipient TEXT NOT NULL DEFAULT '',
          notes TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS visitor_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          visitor_name TEXT NOT NULL DEFAULT 'Visitor',
          category TEXT NOT NULL DEFAULT 'message',
          urgency TEXT NOT NULL DEFAULT 'normal',
          message TEXT NOT NULL,
          item_description TEXT NOT NULL DEFAULT '',
          item_location TEXT NOT NULL DEFAULT '',
          contact_hint TEXT NOT NULL DEFAULT '',
          status TEXT NOT NULL DEFAULT 'unread',
          created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS chat_log (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_message TEXT NOT NULL,
          assistant_reply TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS family_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          sender TEXT NOT NULL,
          recipient TEXT NOT NULL,
          message_type TEXT NOT NULL DEFAULT 'personal',
          title TEXT NOT NULL DEFAULT '',
          text_content TEXT NOT NULL DEFAULT '',
          audio_filename TEXT NOT NULL,
          is_important INTEGER NOT NULL DEFAULT 0,
          is_heard INTEGER NOT NULL DEFAULT 0,
          task_status TEXT NOT NULL DEFAULT 'not-a-task',
          created_at TEXT NOT NULL
        );
        """)

core.register_init_hook(ensure_extra_tables)
ensure_extra_tables()


@app.get("/api/health")
def health():
    return ok({"ok": True, "app": "AlexaPlus Personal Agent", "date": date.today().isoformat(), "ai_configured": bool(get_ai_key())})


def parse_date_from_text(text: str) -> str:
    """Resolve a small, safe set of natural-language dates for the local demo."""
    low = text.casefold()
    today = date.today()
    if "day after tomorrow" in low:
        return (today + timedelta(days=2)).isoformat()
    if "tomorrow" in low:
        return (today + timedelta(days=1)).isoformat()
    if "today" in low or "tonight" in low:
        return today.isoformat()
    match = re.search(r"\b(20\d{2}-\d{2}-\d{2})\b", text)
    if match:
        try:
            return date.fromisoformat(match.group(1)).isoformat()
        except ValueError:
            return ""
    return ""


def _is_loopback() -> bool:
    return (request.remote_addr or "") in {"127.0.0.1", "::1", "localhost"}


_ALLOWED_OPEN_HOSTS = {
    "youtube.com", "www.youtube.com", "music.youtube.com", "m.youtube.com", "open.spotify.com", "www.google.com", "google.com",
    "mail.google.com", "web.whatsapp.com", "www.instagram.com", "www.facebook.com", "x.com", "www.linkedin.com", "github.com",
    "www.netflix.com", "www.amazon.in", "www.flipkart.com", "chatgpt.com", "claude.ai", "gemini.google.com", "drive.google.com",
    "calendar.google.com", "www.hotstar.com", "www.primevideo.com", "www.wikipedia.org", "news.google.com", "gaana.com",
    "www.jiosaavn.com", "wynk.in", "music.apple.com", "music.amazon.in",
}


def _is_public_host(host: str) -> bool:
    """Any normal public website is fine; block localhost / private-network addresses."""
    import ipaddress
    host = host.lower().strip(".")
    if not host or host == "localhost" or host.endswith((".local", ".internal")) or "." not in host and not host.replace(":", "").isalnum():
        return False
    try:
        ip = ipaddress.ip_address(host)
        return ip.is_global
    except ValueError:
        return "." in host


def open_on_this_pc(url: str) -> bool:
    """Open a website (or the Spotify desktop app) on the computer that runs this app.

    Because the web app runs locally, the server can do this without the browser's popup blocker getting in the way.
    Only allow-listed hosts / the spotify: scheme, and only for requests coming from this computer.
    """
    if os.getenv("ALEXA_OPEN_ON_PC", "1").strip().lower() in {"0", "false", "no", "off"} or not _is_loopback():
        return False
    parsed = urlparse(url)
    try:
        if parsed.scheme in {"spotify", "whatsapp"}:
            if sys.platform.startswith("win"):
                os.startfile(url)  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                subprocess.Popen(["open", url])
            else:
                subprocess.Popen(["xdg-open", url])
            return True
        if parsed.scheme in {"http", "https"} and _is_public_host(parsed.hostname or ""):
            return bool(webbrowser.open(url, new=2))
    except Exception:  # noqa: BLE001 - any OS error just means we fall back to the browser
        app.logger.warning("Could not open %s on this PC", url, exc_info=True)
    return False


def _smart_response(result: dict):
    """Turn a smart.handle() result into a JSON response, performing any 'open on this PC' step."""
    body = {"ok": result.get("ok", True), "reply": result.get("reply", "Done."), "mode": "smart"}
    for key in ("speech", "action", "data", "refresh"):
        if result.get(key) is not None:
            body[key] = result[key]
    url = result.get("open_url")
    if url:
        opened = open_on_this_pc(url)
        if not opened and result.get("fallback_url"):
            opened = open_on_this_pc(result["fallback_url"])
        body["opened_on_pc"] = opened
        if not opened and "action" not in body:
            body["action"] = {"type": "open_url", "url": result.get("fallback_url") or url}
        if opened and body.get("action", {}).get("type") == "open_url":
            body.pop("action")  # already opened, the browser doesn't need to try again
    return ok(body)


@app.post("/api/open-external")
def open_external():
    """Used by the browser as a fallback (e.g. a YouTube video that cannot be embedded)."""
    url = str(payload().get("url", ""))
    return ok({"ok": open_on_this_pc(url)})


@app.get("/api/reminders/due")
def reminders_due():
    """Return reminders whose time has come and mark them completed so they ring only once."""
    now = datetime.now()
    now_s = now.isoformat(timespec="seconds")
    due = []
    with core.get_db() as conn:
        rows = conn.execute("SELECT * FROM reminders WHERE status='pending' AND remind_at <= ? ORDER BY remind_at LIMIT 20", (now_s,)).fetchall()
        for r in rows:
            conn.execute("UPDATE reminders SET status='completed' WHERE id=?", (r["id"],))
            try:
                late = int((now - datetime.fromisoformat(r["remind_at"])).total_seconds() // 60)
            except ValueError:
                late = 0
            due.append({"id": r["id"], "title": r["title"], "remind_at": r["remind_at"], "late_minutes": late,
                        "announcement": smart.reminder_announcement(r["title"], late)})
    return ok({"count": len(due), "reminders": due})


LAST_AI_ERROR = {"message": ""}
LAST_AI_PROVIDER = {"name": ""}  # which provider answered last: "primary" (Gemini) or "ollama"


def _call_chat_completions(base_url: str, api_key: str, model: str, messages: list[dict],
                           label: str, timeout: int = 40, temperature: float = 0.6) -> tuple[str | None, str]:
    """Call one OpenAI-compatible /chat/completions endpoint.

    Returns (answer, error). answer is None on any failure and error explains why.
    """
    body_bytes = json.dumps({"model": model, "messages": messages, "temperature": temperature}).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    req = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", data=body_bytes,
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8"))
        answer = body["choices"][0]["message"]["content"]
        if isinstance(answer, str) and answer.strip():
            return answer.strip(), ""
        return None, f"{label} returned an empty reply (possibly blocked by safety filters)."
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        hints = {400: "Bad request or invalid API key.", 401: "API key rejected.",
                 403: "Key not allowed / API not enabled for this key.",
                 404: f"Model '{model}' or URL not found. Check the model name and base URL.",
                 429: "Rate limit or quota reached. Wait a minute or try another model."}
        return None, f"{label} HTTP {exc.code}: {hints.get(exc.code, 'API error.')} {detail}"
    except urllib.error.URLError as exc:
        return None, f"Could not reach {label} (network/firewall/proxy/not running): {exc.reason}"
    except (TimeoutError, OSError) as exc:
        return None, f"{label} request failed or timed out ({type(exc).__name__})."
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        return None, f"Unexpected response format from {label} ({type(exc).__name__})."


def ollama_enabled() -> bool:
    return (os.getenv("OLLAMA_ENABLED", "1").strip().lower() not in {"0", "false", "no", "off"})


def optional_model_reply(message: str, history: list[dict], language: str = "auto", voice: bool = False) -> str | None:
    """Open chat via Gemini (OpenAI-compatible API) with automatic fallback to a local Ollama server.

    Order: 1) Gemini/primary provider if an API key is set, 2) Ollama if the primary fails
    (bad/expired key, quota, network error, empty reply) or no key is set.
    On total failure returns None and stores the reasons in LAST_AI_ERROR so the UI can show them.
    """
    LAST_AI_ERROR["message"] = ""
    LAST_AI_PROVIDER["name"] = ""
    funny = (os.getenv("ALEXA_PERSONALITY", "funny").strip().lower() != "normal")
    persona = (
        " PERSONALITY: you are a witty, playful assistant with the timing of a stand-up comedian. Answer every question "
        "correctly and helpfully FIRST, then add one light joke, pun, or funny comparison (desi/Hinglish humour is welcome "
        "when the user mixes Hindi and English). Stay family-friendly, never insult the user, and drop the jokes completely "
        "for serious topics such as health emergencies, grief, safety or money trouble."
    ) if funny else ""
    messages = [{
        "role": "system",
        "content": (
            "You are Alexa+ Personal Agent, a friendly, concise, conversational personal assistant. "
            f"Respond in this language preference: {language}. If it is 'auto', follow the user's language; "
            "support English, Hindi, and natural Hinglish. Keep follow-up context and be culturally respectful. "
            "You can discuss general topics and help the user plan. The local app handles memory, study, "
            "inventory, expiry reminders, timed reminders, music playback and password-vault actions through deterministic tools, "
            "so never claim you set a reminder or played music yourself in a general answer. "
            "Never claim to have changed or saved anything unless the app confirms it. Never ask users to paste "
            "passwords or other secrets into chat; direct them to the dedicated password vault. Be clear when "
            "you do not have live internet, calendar, notification, or smart-home access."
            + persona
            + (" The user is talking to you by voice and your reply will be read aloud: answer in one to three short, natural "
               "spoken sentences (joke included), with no markdown, bullet points, asterisks, links or emojis." if voice else "")
        )
    }]
    for item in history[-8:]:
        role = item.get("role") if isinstance(item, dict) else None
        content = item.get("content") if isinstance(item, dict) else None
        if role in {"user", "assistant"} and isinstance(content, str) and content.strip():
            messages.append({"role": role, "content": content[:2000]})
    messages.append({"role": "user", "content": message[:4000]})

    errors: list[str] = []

    # 1) Primary provider (Gemini by default)
    api_key = get_ai_key()
    if api_key:
        base_url = (os.getenv("AI_BASE_URL") or "https://api.openai.com/v1").strip()
        model = (os.getenv("AI_MODEL") or "gpt-4o-mini").strip()
        answer, err = _call_chat_completions(base_url, api_key, model, messages, "Gemini", temperature=0.9 if funny else 0.6)
        if answer:
            LAST_AI_PROVIDER["name"] = "primary"
            return answer
        errors.append(err)
        app.logger.warning("Primary AI provider failed (%s); trying Ollama fallback.", err)
    else:
        errors.append("No Gemini API key found (set AI_API_KEY in .env).")

    # 2) Fallback: local Ollama (OpenAI-compatible endpoint, no key needed)
    if ollama_enabled():
        ollama_url = (os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434/v1").strip()
        ollama_model = (os.getenv("OLLAMA_MODEL") or "llama3.2").strip()
        ollama_timeout = int(os.getenv("OLLAMA_TIMEOUT", "120") or 120)  # local models can be slow on first load
        answer, err = _call_chat_completions(ollama_url, "", ollama_model, messages, "Ollama", timeout=ollama_timeout, temperature=0.9 if funny else 0.6)
        if answer:
            LAST_AI_PROVIDER["name"] = "ollama"
            return answer
        errors.append(err)
        app.logger.warning("Ollama fallback failed: %s", err)
    else:
        errors.append("Ollama fallback is disabled (OLLAMA_ENABLED=0).")

    LAST_AI_ERROR["message"] = " | ".join(errors)
    return None


@app.get("/api/ai-test")
def ai_test():
    """Open http://127.0.0.1:5000/api/ai-test to diagnose the Gemini -> Ollama chain."""
    reply = optional_model_reply("Say hello in one short sentence.", [], "en")
    return ok({"ok": bool(reply), "reply": reply, "error": LAST_AI_ERROR["message"],
               "provider_used": LAST_AI_PROVIDER["name"],
               "model": os.getenv("AI_MODEL"), "base_url": os.getenv("AI_BASE_URL"), "key_found": bool(get_ai_key()),
               "ollama_enabled": ollama_enabled(),
               "ollama_model": os.getenv("OLLAMA_MODEL") or "llama3.2",
               "ollama_url": os.getenv("OLLAMA_BASE_URL") or "http://localhost:11434/v1"})


_DB_HIDDEN = {"password_entries", "password_vault_meta", "sync_profiles"}


def _db_tables(conn):
    rows = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name").fetchall()
    return [r[0] for r in rows if r[0] not in _DB_HIDDEN]


@app.get("/api/db/tables")
def db_tables():
    """Read-only list of tables in the local SQL database (vault and sync secrets are hidden)."""
    with core.get_db() as conn:
        out = [{"name": t, "rows": conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]} for t in _db_tables(conn)]
    return ok({"ok": True, "tables": out, "path": str(core.person_db_path())})


@app.get("/api/db/table/<name>")
def db_table(name):
    with core.get_db() as conn:
        if name not in _db_tables(conn):
            return ok({"ok": False, "message": "Unknown table."}, 404)
        cur = conn.execute(f'SELECT * FROM "{name}" ORDER BY rowid DESC LIMIT 200')
        cols = [d[0] for d in cur.description]
        rows = [[(v if not isinstance(v, bytes) else "<binary>") for v in r] for r in cur.fetchall()]
    return ok({"ok": True, "columns": cols, "rows": rows})


@app.after_request
def _log_chat(resp):
    """Keep every chat exchange in the local SQL database (not only browser storage)."""
    try:
        if request.path == "/api/chat" and request.method == "POST":
            body = request.get_json(silent=True) or {}
            reply = (resp.get_json(silent=True) or {}).get("reply", "")
            if body.get("message"):
                with core.get_db() as conn:
                    conn.execute("INSERT INTO chat_log(user_message, assistant_reply) VALUES (?, ?)",
                                 (str(body["message"])[:4000], str(reply)[:8000]))
    except Exception:
        pass
    return resp


@app.post("/api/chat")
def chat():
    """Conversational assistant with deterministic local actions and optional LLM chat."""
    data = payload()
    text = str(data.get("message", "")).strip()
    history = data.get("history", [])
    if not text:
        return ok({"ok": False, "reply": "Tell me what you need, and I'll help."}, 400)
    low = text.casefold()
    try:
        # Smart commands first: timed reminders, inventory + expiry reminders, music, "open youtube", database notes.
        feature_result = features.handle(text)
        if feature_result is not None:
            return _smart_response(feature_result)
        smart_result = smart.handle(text)
        if smart_result is not None:
            return _smart_response(smart_result)

        # Retrieval checks must precede the broad 'remember' save intent.
        if any(x in low for x in ["find memory", "search memory", "what did i tell", "what do you remember", "what have you remembered", "recall", "remember about me"]):
            query = str(data.get("query", "")).strip()
            if not query:
                query = re.sub(r".*?(?:find memory|search memory|recall|what do you remember about|what did i tell you about|remember about me)\s*", "", text, flags=re.I).strip(" ?.")
                if query.casefold() in {"", "you", "me", "about me"}:
                    query = ""
            result = core.search_memories(query)
            if result.get("count", 0):
                lines = [f"• {m['title']}: {m['content']}" for m in result["memories"][:8]]
                reply = "Here's what I found in your saved memories:\n" + "\n".join(lines)
            else:
                reply = "I couldn't find a matching saved memory. Try a keyword, or ask me to save something first."
            return ok({"ok": True, "reply": reply, "data": result})

        if re.search(r"\b(remember (?:that|to|my|i|this|me)\b|remember:|save this|save a note|note that)", low):
            title = str(data.get("title", "")).strip()
            content = str(data.get("content", "")).strip()
            if not content:
                match = re.search(r"(?:remember|save this|save a note|note that)\s+(?:that\s+)?(.+)", text, re.I)
                content = match.group(1).strip(" .") if match else ""
            if not content:
                return ok({"ok": False, "reply": "What would you like me to remember? Please tell me the information to save."})
            if not title:
                title = content[:55] or "Quick note"
            result = core.save_memory(title, content, str(data.get("category", "general")))
            return ok({"ok": result.get("ok", False), "reply": result.get("message", "Memory saved."), "data": result})

        # Daily overview
        if any(x in low for x in ["daily briefing", "my day", "today's plan", "todays plan", "summarize my day", "what should i do today", "plan my day"]):
            result = core.get_daily_briefing()
            parts = [f"Here's your briefing for {result['date']}.", f"You have {len(result['pending_tasks'])} pending study task(s)", f"{len(result['today_study_sessions'])} study session(s) scheduled today", f"{len(result['upcoming_reminders'])} upcoming reminder(s)", f"and {len(result['expired_or_near_expiry_inventory'])} item(s) expired or nearing expiry."]
            if result["pending_tasks"]:
                parts.append("First study task: " + result["pending_tasks"][0]["title"] + ".")
            if result["expired_or_near_expiry_inventory"]:
                parts.append("Please check: " + ", ".join(i["item_name"] for i in result["expired_or_near_expiry_inventory"][:4]) + ".")
            return ok({"ok": True, "reply": " ".join(parts), "data": result})

        # Study task listing/completion/creation
        if any(x in low for x in ["list my tasks", "show my tasks", "study tasks", "what do i need to study", "my study plan", "show my study plan", "what tasks do i have"]):
            result = core.list_study_tasks("pending")
            reply = "You have no pending study tasks right now." if not result["count"] else "Here are your pending study tasks:\n" + "\n".join(f"• {t['title']} ({t['subject'] or 'General'}) — due {t['due_date'] or 'date not set'}; about {t['estimated_minutes']} minutes" for t in result["tasks"])
            return ok({"ok": True, "reply": reply, "data": result})
        complete_match = re.search(r"(?:complete|finish|mark)(?:\s+study)?\s+task\s*(?:number|#|id)?\s*(\d+)", low)
        if complete_match:
            result = core.complete_study_task(int(complete_match.group(1)))
            return ok({"ok": result.get("ok", False), "reply": result.get("message", "Task update processed."), "data": result})
        if any(x in low for x in ["add study task", "add a task", "add task", "create study task", "remind me to study"]):
            title = str(data.get("title", "")).strip()
            subject = str(data.get("subject", "")).strip()
            due = str(data.get("due_date", "")).strip() or parse_date_from_text(text)
            if not title:
                title = re.sub(r".*?(?:add study task|add a task|add task|create study task|remind me to study)\s*", "", text, flags=re.I).strip(" .")
                title = re.sub(r"\s+(?:today|tomorrow|day after tomorrow)\b.*$", "", title, flags=re.I).strip(" .")
                title = re.sub(r"\s+on\s+20\d{2}-\d{2}-\d{2}\b.*$", "", title, flags=re.I).strip(" .")
            if not title:
                return ok({"ok": False, "reply": "What should I call the study task? For example: ‘Add study task revise DBMS tomorrow’."})
            if not subject:
                sub = re.search(r"\b(?:for|subject)\s+([A-Za-z][A-Za-z0-9+# ]{0,25}?)(?:\s+(?:tomorrow|today|on\s+20\d{2}-\d{2}-\d{2})|$)", text, re.I)
                if sub:
                    subject = sub.group(1).strip()
            result = core.add_study_task(title, subject, due, int(data.get("estimated_minutes", 30)))
            reply = result.get("message", "Task request processed.")
            if result.get("ok") and due:
                reply += f" I've set the due date to {due}."
            return ok({"ok": result.get("ok", False), "reply": reply, "data": result})

        # Study session listing
        if any(x in low for x in ["list study sessions", "show study sessions", "my study sessions", "study schedule"]):
            result = core.get_study_sessions()
            reply = "You don't have any study sessions scheduled yet." if not result["count"] else "Here are your scheduled study sessions:\n" + "\n".join(f"• {s['title']} — {s['session_date']} at {s['start_time']} ({s['duration_minutes']} minutes)" for s in result["sessions"])
            return ok({"ok": True, "reply": reply, "data": result})

        # Study session planning
        if any(x in low for x in ["study session", "schedule study", "plan a study session"]):
            session_date = parse_date_from_text(text) or (date.today() + timedelta(days=1)).isoformat()
            title = re.sub(r".*?(?:study session|schedule study|plan a study session)\s*(?:for|to)?\s*", "", text, flags=re.I).strip(" .") or "Study session"
            title = re.sub(r"\s+(?:today|tomorrow|day after tomorrow)\b.*$", "", title, flags=re.I).strip(" .") or "Study session"
            result = core.create_study_session(title, session_date, str(data.get("start_time", "18:00")), int(data.get("duration_minutes", 45)))
            reply = result.get("message", "") if not result.get("ok") else f"I've scheduled {title} for {session_date} at {data.get('start_time', '18:00')} for {int(data.get('duration_minutes', 45))} minutes."
            return ok({"ok": result.get("ok", False), "reply": reply, "data": result})

        # Simple stored reminders. These are saved in the app; they do not send OS push notifications.
        if "remind me" in low or "set a reminder" in low or "add reminder" in low:
            when = parse_date_from_text(text)
            if not when:
                return ok({"ok": False, "reply": "When should I remind you? Try ‘Remind me tomorrow to revise SQL’. I can save reminders in the app, but this prototype doesn't send push notifications yet."})
            title = re.sub(r".*?(?:remind me(?: to)?|set a reminder(?: to)?|add reminder(?: to)?)\s*", "", text, flags=re.I).strip(" .")
            title = re.sub(r"\b(?:today|tomorrow|day after tomorrow)\b", "", title, flags=re.I).strip(" .")
            title = re.sub(r"^to\s+", "", title, flags=re.I).strip(" .") or "Reminder"
            remind_at = f"{when}T09:00"
            result = core.create_reminder(title, remind_at, "Created from assistant chat; default time 09:00.")
            return ok({"ok": result.get("ok", False), "reply": f"{result.get('message', 'Reminder request processed')} I've saved ‘{title}’ for {when} at 09:00. This prototype stores the reminder but doesn't send a notification yet.", "data": result})

        # Inventory
        if any(x in low for x in ["expiring", "near expiry", "expire soon", "expiry items", "about to expire", "expired items"]):
            result = core.check_expiring_items(int(data.get("within_days", 7)))
            if result.get("count"):
                reply = "Here are the expired or soon-to-expire items:\n" + "\n".join(f"• {i['item_name']} — {i['expiry_status']} ({i['days_until_expiry']} day(s); quantity {i['quantity']} {i['unit']})" for i in result["items"])
            else:
                reply = f"Good news — no expired or near-expiry items were found within {result.get('within_days', 7)} days."
            return ok({"ok": True, "reply": reply, "data": result})
        if any(x in low for x in ["show inventory", "list inventory", "what's in inventory", "what is in inventory", "what do i have at home", "what ingredients do i have"]):
            result = core.list_inventory()
            reply = "Your inventory is empty." if not result["count"] else "Here's what you have in inventory:\n" + "\n".join(f"• {i['item_name']}: {i['quantity']} {i['unit']} (expiry {i['expiry_date'] or 'not set'})" for i in result["items"])
            return ok({"ok": True, "reply": reply, "data": result})
        if any(x in low for x in ["add inventory", "add item", "add to inventory", "i bought"]):
            name = str(data.get("item_name", "")).strip()
            if not name:
                name = re.sub(r".*?(?:add inventory|add item|add to inventory|i bought)\s*", "", text, flags=re.I).strip(" .")
            if not name:
                return ok({"ok": False, "reply": "What item should I add? You can include the quantity and expiry date in the Inventory section."})
            result = core.add_inventory_item(name, float(data.get("quantity", 1)), str(data.get("unit", "item")), str(data.get("expiry_date", "")) or parse_date_from_text(text), str(data.get("category", "general")))
            return ok({"ok": result.get("ok", False), "reply": result.get("message", "Inventory request processed."), "data": result})

        # Multi-step meal planning: read inventory + expiry status, then generate meal ideas.
        if any(x in low for x in ["plan meals using inventory", "plan meals with ingredients", "meal plan using expiring", "plan my meals", "plan meals for me"]):
            inv = core.list_inventory()
            expiring = core.check_expiring_items(7)
            priority = [i.get("item_name", "") for i in expiring.get("items", []) if i.get("item_name")]
            all_items = [i.get("item_name", "") for i in inv.get("items", []) if i.get("item_name")]
            ingredients = ", ".join(dict.fromkeys(priority + all_items))
            meal_suggestions = []
            for mood_name in ("healthy", "comfort", "spicy"):
                meal_suggestions.extend(core.suggest_dishes(mood_name, ingredients, str(data.get("dietary_preference", "none")), 40).get("suggestions", [])[:2])
            meal_suggestions = list(dict.fromkeys(meal_suggestions))[:5]
            parts = ["I checked your saved inventory and expiry dates."]
            if priority:
                parts.append("Prioritize using soon: " + ", ".join(priority[:6]) + ".")
            elif not all_items:
                parts.append("Your inventory is empty, so add ingredients for more tailored suggestions.")
            else:
                parts.append("No items are expiring within the next 7 days based on your saved dates.")
            if meal_suggestions:
                parts.append("Meal ideas: " + "; ".join(meal_suggestions) + ".")
            parts.append("These are suggestions, not a verified nutrition or allergen assessment.")
            return ok({"ok": True, "reply": " ".join(parts), "mode": "local-agent", "data": {"ingredients": all_items, "priority_ingredients": priority, "meal_suggestions": meal_suggestions}})

        # Shopping helper: returns marketplace search links, not invented live prices.
        if any(x in low for x in ["find product", "shopping", "compare products", "buy online", "search products", "shop for"]):
            query = str(data.get("query", "")).strip()
            if not query:
                query = re.sub(r".*?(?:find products?|shopping for|compare products?|buy online|search products?|shop for)\s*", "", text, flags=re.I).strip(" .?!")
            if query:
                from urllib.parse import quote_plus
                q = quote_plus(query)
                links = [f"Amazon India: https://www.amazon.in/s?k={q}", f"Flipkart: https://www.flipkart.com/search?q={q}"]
                reply = "I prepared product searches for your request. Open the marketplace results to verify current price, availability, seller, and reviews: " + " | ".join(links) + ". I don't have live retailer inventory or price data connected in this prototype."
                return ok({"ok": True, "reply": reply, "mode": "shopping-links", "data": {"query": query, "links": [{"name":"Amazon India","url":f"https://www.amazon.in/s?k={q}"},{"name":"Flipkart","url":f"https://www.flipkart.com/search?q={q}"}]}})
            return ok({"ok": True, "reply": "What product should I search for? Include your budget and important features.", "mode": "local"})

        # Daily overview
        mood = str(data.get("mood", "")).strip()
        if mood or re.search(r"\b(dish|dishes|food|eat|recipe|recipes|cook|cooking|hungry|dinner|lunch|breakfast)\b", low):
            mood = mood or "comfort"
            result = core.suggest_dishes(mood, str(data.get("available_ingredients", "")), str(data.get("dietary_preference", "none")), int(data.get("max_cooking_minutes", 30)))
            return ok({"ok": True, "reply": f"For a {mood} mood, I'd suggest {', '.join(result['suggestions'])}. If you tell me what ingredients you have, I can narrow the choices. Please check allergens and ingredients before cooking.", "data": result})

        if any(x in low for x in ["password", "password manager", "vault", "login credential"]):
            return ok({"ok": True, "reply": "For your safety, use the dedicated Password Vault section rather than sending a secret through chat. Unlock the vault there, then save or reveal a credential. Passwords are not handled by ordinary chat."})

        # Small-talk and help responses keep the no-key demo conversational and honest.
        if re.fullmatch(r"(?:hi|hello|hey|hey there|good morning|good afternoon|good evening)[!. ]*", low):
            return ok({"ok": True, "reply": random.choice(["Hi! 😊 Fully charged and slightly overcaffeinated. Reminders, music, inventory, jokes: what shall we start with?", "Hello hello! I'm awake, I'm alert, and I have zero chill about helping you. What's up?", "Hey! Your favourite digital bhoot is here. Ask me anything!"]), "mode": "local"})
        if any(x in low for x in ["who are you", "what is your name", "what can you do", "help me", "your features"]):
            return ok({"ok": True, "reply": "I'm Alexa+ Personal Agent, a local assistant that lives on your computer and works for snacks (just kidding, electricity). Try: “remind me to take medicine in 2 minutes”, “play Kesariya”, “add milk to inventory, expires 30 October”, or ask me anything and I'll answer with a joke. I'm not an official Amazon Alexa service.", "mode": "local"})
        if any(x in low for x in ["how are you", "how's it going", "hows it going"]):
            return ok({"ok": True, "reply": random.choice(["I'm doing great, no bugs today. Touch wood. Well, touch plastic, I don't have wood. How are you?", "Living my best life inside a laptop! Want a reminder, a song, or a joke?"]), "mode": "local"})
        if low in {"thanks", "thank you", "thanks!", "thank you!"}:
            return ok({"ok": True, "reply": random.choice(["You're welcome! Tipping is accepted in biryani.", "Anytime! I'm basically free, apart from the electricity.", "My pleasure! Shout if you need me."]), "mode": "local"})

        # If configured, use a real language model for general conversation. Otherwise, stay honest about demo mode.
        model_reply = optional_model_reply(text, history if isinstance(history, list) else [], str(data.get("language", "auto")), bool(data.get("voice")))
        if model_reply:
            return ok({"ok": True, "reply": model_reply, "mode": "ai", "provider": LAST_AI_PROVIDER["name"]})
        err = LAST_AI_ERROR["message"]
        if err:
            prefix = "AI request failed (Gemini and Ollama)" if (get_ai_key() or ollama_enabled()) else "AI is not connected"
            return ok({"ok": False, "reply": f"{prefix}: {err}", "mode": "error",
                       "speech": "Oops, both my brains are on a chai break right now. Please try again in a minute."})
        return ok({"ok": True, "reply": "I'm here to help! I can save and recall memories, manage study tasks and sessions, check inventory and expiry dates, save reminders, suggest dishes, and give you a daily briefing. What would you like to do first?", "mode": "local"})
    except Exception as exc:
        app.logger.exception("Chat request failed")
        return ok({"ok": False, "reply": f"I couldn't finish that request ({type(exc).__name__}). Please check the web-app terminal for details."}, 500)

@app.get("/api/memories")
def memories():
    return ok(core.search_memories(str(request.args.get("q", ""))))


@app.post("/api/memories")
def save_memory():
    d = payload()
    result = core.save_memory(str(d.get("title", "")), str(d.get("content", "")), str(d.get("category", "general")))
    return ok(result, 200 if result.get("ok") else 400)


@app.delete("/api/memories/<int:memory_id>")
def delete_memory(memory_id):
    return ok(core.delete_memory(memory_id))


@app.get("/api/tasks")
def tasks():
    return ok(core.list_study_tasks(str(request.args.get("status", "pending"))))


@app.post("/api/tasks")
def add_task():
    d = payload()
    result = core.add_study_task(str(d.get("title", "")), str(d.get("subject", "")), str(d.get("due_date", "")), int(d.get("estimated_minutes", 30)))
    return ok(result, 200 if result.get("ok") else 400)


@app.post("/api/tasks/<int:task_id>/complete")
def complete_task(task_id):
    return ok(core.complete_study_task(task_id))


@app.get("/api/inventory")
def inventory():
    return ok(core.list_inventory())


@app.post("/api/inventory")
def add_inventory():
    d = payload()
    result = core.add_inventory_item(str(d.get("item_name", "")), float(d.get("quantity", 1)), str(d.get("unit", "item")), str(d.get("expiry_date", "")), str(d.get("category", "general")))
    if result.get("ok") and str(d.get("expiry_date", "")).strip():
        try:
            lead = int(d["lead_days"]) if str(d.get("lead_days", "")).strip() else None
            sched = smart.schedule_expiry_reminders(result["item_id"], str(d.get("item_name", "")).strip(), date.fromisoformat(str(d["expiry_date"])), lead)
            if sched:
                result["message"] += f" I'll remind you {smart.describe_when(min(sched))} before it expires."
        except (ValueError, KeyError):
            pass
    return ok(result, 200 if result.get("ok") else 400)


@app.get("/api/expiring")
def expiring():
    return ok(core.check_expiring_items(int(request.args.get("days", 7))))


@app.get("/api/reminders")
def reminders():
    return ok(core.list_reminders(str(request.args.get("status", "pending"))))


@app.post("/api/reminders")
def add_reminder():
    d = payload()
    result = core.create_reminder(str(d.get("title", "")), str(d.get("remind_at", "")), str(d.get("notes", "")))
    return ok(result, 200 if result.get("ok") else 400)


@app.get("/api/study-sessions")
def study_sessions():
    return ok(core.get_study_sessions(str(request.args.get("from_date", "")), str(request.args.get("to_date", ""))))


@app.post("/api/study-sessions")
def add_study_session():
    d = payload()
    result = core.create_study_session(str(d.get("title", "Study session")), str(d.get("session_date", "")), str(d.get("start_time", "18:00")), int(d.get("duration_minutes", 45)))
    return ok(result, 200 if result.get("ok") else 400)


@app.get("/api/daily-briefing")
def daily_briefing():
    return ok(core.get_daily_briefing())


@app.post("/api/dishes")
def dishes():
    d = payload()
    return ok(core.suggest_dishes(str(d.get("mood", "comfort")), str(d.get("available_ingredients", "")), str(d.get("dietary_preference", "none")), int(d.get("max_cooking_minutes", 30))))


# Password-vault endpoints call the same in-process core module, so the in-memory
# vault key is shared across the UI's vault actions. This is a local-only prototype.
@app.get("/api/vault/status")
def vault_status():
    return ok({"initialized": core._vault_is_initialized(), "unlocked": core._active_fernet() is not None})


@app.post("/api/vault/setup")
def vault_setup():
    d = payload()
    result = core.setup_password_vault(str(d.get("master_password", "")))
    return ok(result, 200 if result.get("ok") else 400)


@app.post("/api/vault/unlock")
def vault_unlock():
    d = payload()
    result = core.unlock_password_vault(str(d.get("master_password", "")))
    return ok(result, 200 if result.get("ok") else 400)


@app.post("/api/vault/lock")
def vault_lock():
    return ok(core.lock_password_vault())


@app.get("/api/vault/entries")
def vault_entries():
    result = core.list_password_entries(str(request.args.get("q", "")))
    return ok(result, 200 if result.get("ok") else 423)


@app.post("/api/vault/entries")
def vault_save_entry():
    d = payload()
    result = core.save_password_entry(str(d.get("title", "")), str(d.get("website", "")), str(d.get("username", "")), str(d.get("password", "")), str(d.get("notes", "")))
    return ok(result, 200 if result.get("ok") else 400)


@app.post("/api/vault/entries/<int:entry_id>/reveal")
def vault_reveal(entry_id):
    result = core.reveal_password_entry(entry_id)
    return ok(result, 200 if result.get("ok") else 400)


@app.delete("/api/vault/entries/<int:entry_id>")
def vault_delete(entry_id):
    result = core.delete_password_entry(entry_id)
    return ok(result, 200 if result.get("ok") else 400)



# Demo home automation, arrival greetings, and local alert center.
# These routes intentionally simulate devices; they never control real appliances.
@app.get("/api/home-devices")
def home_devices():
    with core.get_db() as db:
        events = [dict(r) for r in db.execute("SELECT * FROM home_device_events ORDER BY id DESC LIMIT 30").fetchall()]
        profiles = [dict(r) for r in db.execute("SELECT * FROM family_profiles ORDER BY name").fetchall()]
    states = {}
    for device in ["Bathroom Light", "Heater", "Kitchen Light", "Gas Stove"]:
        latest = next((e for e in events if e["device"] == device), None)
        states[device] = {"on": bool(latest and latest["state"] == "ON"), "updated_at": latest["created_at"] if latest else None}
    return ok({"states": states, "events": events, "profiles": profiles})

@app.post("/api/home-devices")
def home_devices_toggle():
    data = payload(); device = str(data.get("device", "")); state = str(data.get("state", "")).upper()
    allowed = {"Bathroom Light", "Heater", "Kitchen Light", "Gas Stove"}
    if device not in allowed or state not in {"ON", "OFF"}:
        return ok({"ok": False, "message": "Choose a valid demo switch and ON/OFF state."}, 400)
    now = datetime.now().isoformat(timespec="seconds")
    with core.get_db() as db:
        db.execute("INSERT INTO home_device_events(device,state,reason,created_at) VALUES(?,?,?,?)", (device,state,"User demo switch",now))
        latest = {d: db.execute("SELECT state FROM home_device_events WHERE device=? ORDER BY id DESC LIMIT 1",(d,)).fetchone() for d in allowed}
    warnings=[]
    def _on(name):
        row = latest.get(name)
        return bool(row and row["state"] == "ON")
    # A missing event means the switch has never been used, i.e. OFF. The rule is checked
    # whichever of the two devices changed, so switching the heater/stove ON in the dark also warns.
    if device in ("Bathroom Light", "Heater") and _on("Heater") and not _on("Bathroom Light"):
        warnings.append("SIMULATED SAFETY ALERT: Bathroom light is OFF while the heater is ON. Check the heater. This demo is not connected to real hardware.")
    if device in ("Kitchen Light", "Gas Stove") and _on("Gas Stove") and not _on("Kitchen Light"):
        warnings.append("SIMULATED SAFETY ALERT: Kitchen light is OFF while the gas stove is ON. Check the stove. This demo is not connected to real hardware.")
    return ok({"ok": True, "warnings": warnings, "message": f"Demo {device} set to {state}. No physical device was changed."})

@app.post("/api/family-profiles")
def family_profiles():
    data=payload(); action=data.get("action","list")
    with core.get_db() as db:
        if action == "add":
            name=str(data.get("name","")).strip()
            if not name: return ok({"ok":False,"message":"Name is required."},400)
            greeting=str(data.get("greeting","")).strip() or f"Welcome home, {name}!"
            cur=db.execute("INSERT INTO family_profiles(name,greeting) VALUES(?,?)",(name,greeting))
            return ok({"ok":True,"id":cur.lastrowid,"message":f"Saved greeting for {name}."})
        if action == "greet":
            name=str(data.get("name","")).strip()
            row=db.execute("SELECT greeting FROM family_profiles WHERE name=? COLLATE NOCASE ORDER BY id DESC LIMIT 1",(name,)).fetchone() if name else None
            greeting=row["greeting"] if row else (f"Welcome home, {name}!" if name else "Welcome home! It's good to have you back.")
            return ok({"ok":True,"greeting":greeting})
        if action == "delete":
            try: pid=int(data.get("id"))
            except (TypeError, ValueError): return ok({"ok":False,"message":"Profile id is required."},400)
            cur=db.execute("DELETE FROM family_profiles WHERE id=?",(pid,))
            return ok({"ok":bool(cur.rowcount),"message":"Profile removed." if cur.rowcount else "Profile not found."},200 if cur.rowcount else 404)
        return ok({"profiles":[dict(r) for r in db.execute("SELECT * FROM family_profiles ORDER BY name").fetchall()]})

@app.route("/api/local-alerts", methods=["GET","POST"])
def local_alerts():
    if request.method == "POST":
        data=payload(); severity=str(data.get("severity","INFO")).upper()
        if severity not in {"INFO","WARNING","HIGH"}: severity="INFO"
        title=str(data.get("title","")).strip(); details=str(data.get("details","")).strip(); area=str(data.get("area","Demo area")).strip() or "Demo area"
        if not title: return ok({"ok":False,"message":"Alert title is required."},400)
        with core.get_db() as db:
            cur=db.execute("INSERT INTO local_alerts(severity,title,details,area,source,created_at) VALUES(?,?,?,?,?,?)",(severity,title,details,area,"DEMO",datetime.now().isoformat(timespec="seconds")))
        return ok({"ok":True,"id":cur.lastrowid,"message":"Demo alert added. No live emergency source is connected."})
    with core.get_db() as db:
        rows=[dict(r) for r in db.execute("SELECT * FROM local_alerts ORDER BY id DESC LIMIT 100").fetchall()]
        if not rows:
            db.execute("INSERT INTO local_alerts(severity,title,details,area,source,created_at) VALUES(?,?,?,?,?,?)",("INFO","Demo alert center ready","Sample data only. Configure a trusted official feed before using real local alerts.","Demo area","DEMO",datetime.now().isoformat(timespec="seconds")))
            rows=[dict(r) for r in db.execute("SELECT * FROM local_alerts ORDER BY id DESC LIMIT 100").fetchall()]
    return ok({"alerts":rows,"live_feed_connected":False})


@app.get("/api/services")
def services_status():
    return ok({
        "ai": {"configured": bool(get_ai_key()) or ollama_enabled(), "type": "OpenAI-compatible chat completions", "primary": "Gemini" if get_ai_key() else None, "fallback": "Ollama" if ollama_enabled() else None},
        "voice": {"type": "browser speech APIs", "depends_on_browser": True},
        "shopping": {"configured": True, "type": "marketplace search links", "live_prices": False},
        "smart_home": {"configured": True, "type": "simulator only", "real_devices": False},
        "calendar": {"configured": False, "note": "No external calendar provider connected."},
        "notifications": {"configured": False, "note": "Browser alarms work best while the app is open."},
        "cross_device_sync": {"configured": True, "type": "manual secure conversation token", "scope": "conversation history only"}
    })


def _sync_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()

@app.post("/api/sync/profile")
def create_sync_profile():
    token = secrets.token_urlsafe(32)
    now = datetime.now().isoformat(timespec="seconds")
    with core.get_db() as conn:
        conn.execute("INSERT INTO sync_profiles(token_hash, conversation_json, updated_at) VALUES (?, '[]', ?)", (_sync_hash(token), now))
    return ok({"ok": True, "token": token, "message": "Sync token created. Keep it private and enter it on your other device."})

@app.route("/api/sync/conversation", methods=["GET", "PUT"])
def sync_conversation():
    token = request.headers.get("X-Sync-Token", "").strip()
    if len(token) < 20:
        return ok({"ok": False, "message": "A valid sync token is required."}, 401)
    token_hash = _sync_hash(token)
    with core.get_db() as conn:
        row = conn.execute("SELECT conversation_json FROM sync_profiles WHERE token_hash = ?", (token_hash,)).fetchone()
        if not row:
            return ok({"ok": False, "message": "Sync token not found. Check the token or create a new one."}, 404)
        if request.method == "GET":
            try: messages = json.loads(row["conversation_json"])
            except (ValueError, TypeError): messages = []
            return ok({"ok": True, "messages": messages})
        data = payload()
        messages = data.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 100:
            return ok({"ok": False, "message": "Conversation must be a list of at most 100 messages."}, 400)
        clean = []
        for item in messages:
            if isinstance(item, dict) and item.get("role") in {"user", "assistant"} and isinstance(item.get("content"), str):
                clean.append({"role": item["role"], "content": item["content"][:4000]})
        now = datetime.now().isoformat(timespec="seconds")
        conn.execute("UPDATE sync_profiles SET conversation_json = ?, updated_at = ? WHERE token_hash = ?", (json.dumps(clean[-60:], ensure_ascii=False), now, token_hash))
        return ok({"ok": True, "count": len(clean[-60:]), "updated_at": now})

@app.post("/api/shopping-search")
def shopping_search():
    data = payload()
    query = str(data.get("query", "")).strip()
    if not query:
        return ok({"ok": False, "message": "Enter a product or shopping request."}, 400)
    from urllib.parse import quote_plus
    q = quote_plus(query)
    return ok({"ok": True, "query": query, "note": "These links open live retailer search results. This prototype does not fetch or verify current prices, stock, or sellers.", "results": [
        {"name": "Amazon India", "url": f"https://www.amazon.in/s?k={q}"},
        {"name": "Flipkart", "url": f"https://www.flipkart.com/search?q={q}"}
    ]})

@app.post("/api/meal-plan")
def meal_plan():
    data = payload()
    days = max(1, min(7, int(data.get("days", 3))))
    dietary = str(data.get("dietary_preference", "none"))
    inv = core.list_inventory()
    expiring = core.check_expiring_items(int(data.get("expiry_window_days", 7)))
    priority = [i.get("item_name", "") for i in expiring.get("items", []) if i.get("item_name")]
    ingredients = ", ".join(dict.fromkeys(priority + [i.get("item_name", "") for i in inv.get("items", []) if i.get("item_name")]))
    moods = ["healthy", "comfort", "spicy", "happy", "tired", "celebratory", "healthy"]
    meals = []
    for idx in range(days):
        suggestions = core.suggest_dishes(moods[idx], ingredients, dietary, int(data.get("max_cooking_minutes", 40))).get("suggestions", [])
        meals.append({"day": idx + 1, "mood": moods[idx], "suggestions": suggestions[:2]})
    return ok({"ok": True, "days": days, "priority_ingredients": priority, "available_ingredients": [i.get("item_name") for i in inv.get("items", [])], "meals": meals, "note": "Generated from saved inventory and local recipe rules. Verify allergens, nutrition, and food safety."})



# Smart Delivery Assistant. This records tracking data and user-entered status updates;
# live carrier status requires a separately configured carrier/tracking provider.
DELIVERY_STATUSES = ["Order confirmed", "Shipped", "In transit", "Out for delivery", "Delivery attempted", "Delayed", "Delivered", "Cancelled", "Returned"]

@app.route("/api/family-messages", methods=["GET", "POST"])
def family_messages_api():
    """Local prototype API for family voice notes. Add authentication before network deployment."""
    if request.method == "GET":
        recipient = request.args.get("recipient", "").strip()[:120]
        with core.get_db() as db:
            if recipient:
                rows = db.execute("SELECT * FROM family_messages WHERE recipient IN (?, 'Everyone') ORDER BY id DESC LIMIT 200", (recipient,)).fetchall()
            else:
                rows = db.execute("SELECT * FROM family_messages ORDER BY id DESC LIMIT 200").fetchall()
        messages = [dict(r) for r in rows]
        for m in messages:
            m["is_important"] = bool(m["is_important"])
            m["is_heard"] = bool(m["is_heard"])
            m["audio_url"] = f"/api/family-messages/{m['id']}/audio"
        return ok({"messages": messages, "note": "Local demo. Add authenticated family accounts before using across a network."})

    audio = request.files.get("audio")
    sender = request.form.get("sender", "").strip()[:120]
    recipient = request.form.get("recipient", "").strip()[:120]
    if not sender or not recipient or not audio:
        return ok({"message": "Sender, recipient, and a voice recording are required."}, 400)
    if not audio.filename:
        return ok({"message": "Choose a voice recording first."}, 400)
    ext = Path(secure_filename(audio.filename)).suffix.lower()
    allowed = {".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4"}
    if ext not in allowed:
        ext = ".webm" if "webm" in (audio.mimetype or "") else ".ogg"
    filename = f"{uuid.uuid4().hex}{ext}"
    audio.save(FAMILY_AUDIO_DIR / filename)
    message_type = request.form.get("message_type", "personal").strip()
    if message_type not in {"announcement", "personal", "reminder", "urgent"}:
        message_type = "personal"
    task_status = "pending" if request.form.get("is_task") == "yes" else "not-a-task"
    created_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with core.get_db() as db:
            cur = db.execute("INSERT INTO family_messages(sender,recipient,message_type,title,text_content,audio_filename,is_important,is_heard,task_status,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (
                sender, recipient, message_type, request.form.get("title", "").strip()[:160],
                request.form.get("text_content", "").strip()[:2000], filename,
                1 if request.form.get("is_important") == "yes" or message_type == "urgent" else 0,
                0, task_status, created_at))
            message_id = cur.lastrowid
    except Exception:
        (FAMILY_AUDIO_DIR / filename).unlink(missing_ok=True)
        raise
    return ok({"message": "Family voice message saved.", "id": message_id}, 201)


@app.get("/api/family-messages/<int:message_id>/audio")
def family_message_audio(message_id):
    with core.get_db() as db:
        row = db.execute("SELECT audio_filename FROM family_messages WHERE id=?", (message_id,)).fetchone()
    if not row:
        return ok({"message": "Voice message not found."}, 404)
    path = FAMILY_AUDIO_DIR / row["audio_filename"]
    if not path.is_file():
        return ok({"message": "Voice recording file is missing."}, 404)
    return send_file(path, conditional=True, as_attachment=False)


@app.patch("/api/family-messages/<int:message_id>")
def family_message_update(message_id):
    data = payload()
    fields = []
    values = []
    if "is_heard" in data:
        fields.append("is_heard=?"); values.append(1 if str(data["is_heard"]).lower() in {"1", "true", "yes"} else 0)
    if "task_status" in data:
        status = str(data["task_status"])
        if status not in {"pending", "completed", "not-a-task"}:
            return ok({"message": "Invalid task status."}, 400)
        fields.append("task_status=?"); values.append(status)
    if not fields:
        return ok({"message": "No supported update provided."}, 400)
    values.append(message_id)
    with core.get_db() as db:
        cur = db.execute(f"UPDATE family_messages SET {', '.join(fields)} WHERE id=?", values)
    if not cur.rowcount:
        return ok({"message": "Message not found."}, 404)
    return ok({"message": "Family message updated."})


@app.route("/api/deliveries", methods=["GET", "POST"])
def deliveries_api():
    if request.method == "GET":
        with core.get_db() as db:
            rows = [dict(r) for r in db.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]
        return ok({"deliveries": rows, "live_tracking_connected": False, "note": "Status is user-entered/demo data until a tracking provider is configured."})
    data = payload()
    item = str(data.get("item_name", "")).strip()[:160]
    if not item:
        return ok({"ok": False, "message": "Enter an item or order name."}, 400)
    status = str(data.get("status", "Order confirmed")).strip()
    if status not in DELIVERY_STATUSES: status = "Order confirmed"
    now = datetime.now().isoformat(timespec="seconds")
    with core.get_db() as db:
        cur = db.execute("INSERT INTO deliveries(item_name,retailer,courier,tracking_number,status,estimated_date,recipient,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)", (
            item, str(data.get("retailer", "")).strip()[:120], str(data.get("courier", "")).strip()[:120],
            str(data.get("tracking_number", "")).strip()[:180], status, str(data.get("estimated_date", "")).strip()[:20],
            str(data.get("recipient", "")).strip()[:120], str(data.get("notes", "")).strip()[:1000], now, now))
    return ok({"ok": True, "id": cur.lastrowid, "message": "Order saved. Live status updates require a connected tracking provider."}, 201)

@app.patch("/api/deliveries/<int:delivery_id>")
def delivery_update(delivery_id):
    data = payload(); status = str(data.get("status", "")).strip()
    if status not in DELIVERY_STATUSES:
        return ok({"ok": False, "message": "Choose a valid delivery status."}, 400)
    now = datetime.now().isoformat(timespec="seconds")
    with core.get_db() as db:
        cur = db.execute("UPDATE deliveries SET status=?, updated_at=?, estimated_date=COALESCE(NULLIF(?,''),estimated_date), notes=CASE WHEN ?='' THEN notes ELSE ? END WHERE id=?", (status, now, str(data.get("estimated_date", "")).strip(), str(data.get("notes", "")).strip(), str(data.get("notes", "")).strip()[:1000], delivery_id))
    if not cur.rowcount: return ok({"ok": False, "message": "Order not found."}, 404)
    return ok({"ok": True, "message": "Delivery timeline updated. This is a manual/demo update, not a carrier verification."})

@app.delete("/api/deliveries/<int:delivery_id>")
def delivery_delete(delivery_id):
    with core.get_db() as db: cur = db.execute("DELETE FROM deliveries WHERE id=?", (delivery_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Order removed." if cur.rowcount else "Order not found."}, 200 if cur.rowcount else 404)

@app.route("/api/visitor/messages", methods=["GET", "POST"])
def visitor_messages_api():
    if request.method == "GET":
        # Homeowner dashboard endpoint for local prototype only; add authentication before public deployment.
        with core.get_db() as db: rows = [dict(r) for r in db.execute("SELECT * FROM visitor_messages ORDER BY CASE urgency WHEN 'urgent' THEN 0 ELSE 1 END, id DESC LIMIT 200").fetchall()]
        return ok({"messages": rows, "public_portal": "/visitor"})
    data = payload()
    category = str(data.get("category", "message")).strip().lower()
    if category not in {"message", "item"}: category = "message"
    urgency = str(data.get("urgency", "normal")).strip().lower()
    if urgency not in {"normal", "urgent"}: urgency = "normal"
    message = str(data.get("message", "")).strip()[:3000]
    item = str(data.get("item_description", "")).strip()[:500]
    if category == "item" and not message: message = "Visitor reported an item for the homeowner."
    if not message: return ok({"ok": False, "message": "Please enter a message before submitting."}, 400)
    now = datetime.now().isoformat(timespec="seconds")
    with core.get_db() as db:
        cur = db.execute("INSERT INTO visitor_messages(visitor_name,category,urgency,message,item_description,item_location,contact_hint,status,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (
            str(data.get("visitor_name", "")).strip()[:120] or "Visitor", category, urgency, message,
            item, str(data.get("item_location", "")).strip()[:300], str(data.get("contact_hint", "")).strip()[:200], "unread", now))
    return ok({"ok": True, "id": cur.lastrowid, "message": "Your message has been sent to the homeowner's local message inbox. Do not use this demo for emergencies."}, 201)

@app.patch("/api/visitor/messages/<int:message_id>")
def visitor_message_update(message_id):
    status = str(payload().get("status", "read")).strip().lower()
    if status not in {"unread", "read", "resolved"}: return ok({"ok": False, "message": "Invalid message status."}, 400)
    with core.get_db() as db: cur = db.execute("UPDATE visitor_messages SET status=? WHERE id=?", (status, message_id))
    return ok({"ok": bool(cur.rowcount), "message": "Message status updated." if cur.rowcount else "Message not found."}, 200 if cur.rowcount else 404)


# ---------------------------------------------------------------------------
# Management endpoints: edit / delete for items that previously could only be added.
# ---------------------------------------------------------------------------
@app.patch("/api/inventory/<int:item_id>")
def inventory_update(item_id):
    d = payload()
    with core.get_db() as db:
        row = db.execute("SELECT * FROM inventory WHERE id=?", (item_id,)).fetchone()
    if not row:
        return ok({"ok": False, "message": "Item not found."}, 404)
    now = datetime.now().isoformat(timespec="seconds")
    sets, vals = [], []
    if "quantity" in d:
        try:
            qty = float(d["quantity"])
        except (TypeError, ValueError):
            return ok({"ok": False, "message": "Quantity must be a number."}, 400)
        if qty < 0:
            return ok({"ok": False, "message": "Quantity cannot be negative."}, 400)
        sets.append("quantity=?"); vals.append(qty)
    new_expiry = None
    if "expiry_date" in d:
        raw = str(d["expiry_date"]).strip()
        if raw:
            try:
                new_expiry = date.fromisoformat(raw)
            except ValueError:
                return ok({"ok": False, "message": "Expiry must be YYYY-MM-DD."}, 400)
        sets.append("expiry_date=?"); vals.append(raw or None)
    if not sets:
        return ok({"ok": False, "message": "Nothing to update."}, 400)
    sets.append("updated_at=?"); vals.append(now); vals.append(item_id)
    with core.get_db() as db:
        db.execute(f"UPDATE inventory SET {', '.join(sets)} WHERE id=?", vals)
    if "expiry_date" in d:
        # Re-schedule (or drop) the expiry reminders so they match the new date.
        smart._delete_item_reminders(item_id)
        if new_expiry:
            smart.schedule_expiry_reminders(item_id, row["item_name"], new_expiry)
    return ok({"ok": True, "message": "Inventory updated."})


@app.delete("/api/inventory/<int:item_id>")
def inventory_delete(item_id):
    smart._delete_item_reminders(item_id)
    with core.get_db() as db:
        cur = db.execute("DELETE FROM inventory WHERE id=?", (item_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Item removed." if cur.rowcount else "Item not found."}, 200 if cur.rowcount else 404)


@app.delete("/api/tasks/<int:task_id>")
def task_delete(task_id):
    with core.get_db() as db:
        cur = db.execute("DELETE FROM tasks WHERE id=?", (task_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Task deleted." if cur.rowcount else "Task not found."}, 200 if cur.rowcount else 404)


@app.delete("/api/reminders/<int:reminder_id>")
def reminder_delete(reminder_id):
    with core.get_db() as db:
        cur = db.execute("DELETE FROM reminders WHERE id=?", (reminder_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Reminder cancelled." if cur.rowcount else "Reminder not found."}, 200 if cur.rowcount else 404)


@app.delete("/api/local-alerts/<int:alert_id>")
def local_alert_delete(alert_id):
    with core.get_db() as db:
        cur = db.execute("DELETE FROM local_alerts WHERE id=?", (alert_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Alert dismissed." if cur.rowcount else "Alert not found."}, 200 if cur.rowcount else 404)


@app.delete("/api/visitor/messages/<int:message_id>")
def visitor_message_delete(message_id):
    with core.get_db() as db:
        cur = db.execute("DELETE FROM visitor_messages WHERE id=?", (message_id,))
    return ok({"ok": bool(cur.rowcount), "message": "Message deleted." if cur.rowcount else "Message not found."}, 200 if cur.rowcount else 404)


@app.delete("/api/family-messages/<int:message_id>")
def family_message_delete(message_id):
    with core.get_db() as db:
        row = db.execute("SELECT audio_filename FROM family_messages WHERE id=?", (message_id,)).fetchone()
        if not row:
            return ok({"message": "Message not found."}, 404)
        db.execute("DELETE FROM family_messages WHERE id=?", (message_id,))
    (FAMILY_AUDIO_DIR / Path(row["audio_filename"]).name).unlink(missing_ok=True)
    return ok({"message": "Voice message deleted."})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)