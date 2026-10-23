"""Alexa+ Personal Agent — self-hosted MCP server using Streamable HTTP.

This is a hackathon prototype. Do not expose it to the public internet without
adding authentication, authorization, rate limiting, and deployment hardening.
"""
from __future__ import annotations

import base64
import contextvars
import json
import re
import os
import secrets
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt
from mcp.server.fastmcp import FastMCP

APP_DIR = Path(__file__).resolve().parent
DB_PATH = Path(os.getenv("ALEXA_AGENT_DB", str(APP_DIR / "data" / "assistant.db")))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# Password-vault key exists only in process memory and is cleared on lock/timeout.
_VAULT_KEY: bytes | None = None
_VAULT_LAST_ACTIVITY = 0.0
_VAULT_IDLE_TIMEOUT_SECONDS = 600
_VAULT_VERIFIER = b"AlexaPlus password vault key check v1"

mcp = FastMCP(
    "AlexaPlus Personal Agent",
    instructions=(
        "You are connected to the user's personal assistant tools. Use tools to "
        "read or change stored information. Confirm with the user before destructive "
        "actions or external side effects. Never claim an action succeeded unless "
        "the tool result confirms it."
    ),
)

# ---- per-person data: every person gets their own SQLite file (default person = the original assistant.db) ----
CURRENT_PERSON = contextvars.ContextVar("alexa_person", default="default")
_INIT_HOOKS: list = []      # callables(path) that create extra tables for a database file
_INITED: set = set()
PEOPLE_DIR = DB_PATH.parent / "people"


def slugify_person(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").strip().lower()).strip("-")[:32]
    return slug or "default"


def person_db_path(person: str | None = None) -> Path:
    slug = slugify_person(person or CURRENT_PERSON.get())
    if slug == "default":
        return DB_PATH
    PEOPLE_DIR.mkdir(parents=True, exist_ok=True)
    return PEOPLE_DIR / f"{slug}.db"


def list_people() -> list[str]:
    names = ["default"]
    if PEOPLE_DIR.exists():
        names += sorted(p.stem for p in PEOPLE_DIR.glob("*.db"))
    return names


def register_init_hook(hook) -> None:
    """Register a function(path) that creates extra tables; it also runs for databases already opened."""
    _INIT_HOOKS.append(hook)
    for path in list(_INITED):
        hook(path)


def _bootstrap(path: Path) -> None:
    _INITED.add(path)
    init_db(path)
    for hook in _INIT_HOOKS:
        hook(path)


def get_db() -> sqlite3.Connection:
    path = person_db_path()
    if path not in _INITED:
        _bootstrap(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _raw_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    return conn

def init_db(path: Path | None = None) -> None:
    path = path or DB_PATH
    _INITED.add(path)
    with _raw_db(path) as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT 'general',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            due_date TEXT,
            estimated_minutes INTEGER NOT NULL DEFAULT 30,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS study_sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            session_date TEXT NOT NULL,
            start_time TEXT NOT NULL DEFAULT '18:00',
            duration_minutes INTEGER NOT NULL DEFAULT 45,
            completed INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS inventory (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            item_name TEXT NOT NULL,
            quantity REAL NOT NULL DEFAULT 1,
            unit TEXT NOT NULL DEFAULT 'item',
            expiry_date TEXT,
            category TEXT NOT NULL DEFAULT 'general',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            remind_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_vault_meta (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            salt BLOB NOT NULL,
            verifier_ciphertext BLOB NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS password_entries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ciphertext BLOB NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS home_device_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, device TEXT NOT NULL, state TEXT NOT NULL,
            reason TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS family_profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, greeting TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS local_alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, severity TEXT NOT NULL, title TEXT NOT NULL,
            details TEXT NOT NULL, area TEXT NOT NULL DEFAULT 'Demo area', source TEXT NOT NULL DEFAULT 'DEMO', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_profiles (
            token_hash TEXT PRIMARY KEY,
            conversation_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        );
        """)
        # Small sample inventory is inserted only on first run to make the demo easy.
        count = conn.execute("SELECT COUNT(*) AS n FROM inventory").fetchone()["n"]
        try:
            conn.execute("ALTER TABLE tasks ADD COLUMN completed_at TEXT")
        except sqlite3.OperationalError:
            pass  # column already exists
        if count == 0 and path == DB_PATH and os.getenv("LOAD_DEMO_DATA", "1") == "1":
            today = date.today()
            demo_items = [
                ("Milk", 2, "packets", (today + timedelta(days=1)).isoformat(), "dairy"),
                ("Bread", 1, "packet", (today + timedelta(days=2)).isoformat(), "bakery"),
                ("Tomatoes", 5, "pieces", (today + timedelta(days=4)).isoformat(), "vegetable"),
                ("Rice", 2, "kg", (today + timedelta(days=180)).isoformat(), "grains"),
            ]
            now = datetime.now().isoformat(timespec="seconds")
            conn.executemany(
                """INSERT INTO inventory
                (item_name, quantity, unit, expiry_date, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                [(n, q, u, e, c, now, now) for n, q, u, e, c in demo_items],
            )

def rows_to_dicts(rows):
    return [dict(row) for row in rows]

init_db()

@mcp.tool()
def save_memory(title: str, content: str, category: str = "general") -> dict:
    """Save a user-approved note or preference for later recall."""
    if not title.strip() or not content.strip():
        return {"ok": False, "message": "Title and content are required."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO memories(title, content, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (title.strip(), content.strip(), category.strip() or "general", now, now),
        )
        return {"ok": True, "id": cur.lastrowid, "message": f"Saved memory: {title.strip()}"}

@mcp.tool()
def search_memories(query: str = "", category: str = "") -> dict:
    """Search saved memories by words in title/content, optionally filtering category."""
    sql = "SELECT id, title, content, category, created_at, updated_at FROM memories WHERE 1=1"
    params = []
    if query.strip():
        like = f"%{query.strip()}%"
        sql += " AND (title LIKE ? OR content LIKE ?)"
        params.extend([like, like])
    if category.strip():
        sql += " AND category = ?"
        params.append(category.strip())
    sql += " ORDER BY updated_at DESC LIMIT 50"
    with get_db() as conn:
        found = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"count": len(found), "memories": found}

@mcp.tool()
def delete_memory(memory_id: int) -> dict:
    """Delete a saved memory by its numeric ID. Ask the user to confirm before calling."""
    with get_db() as conn:
        cur = conn.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    return {"ok": cur.rowcount > 0, "message": "Memory deleted." if cur.rowcount else "Memory ID not found."}

@mcp.tool()
def add_study_task(title: str, subject: str = "", due_date: str = "", estimated_minutes: int = 30) -> dict:
    """Add an assignment or study task. due_date must be YYYY-MM-DD or blank."""
    if not title.strip():
        return {"ok": False, "message": "Task title is required."}
    if estimated_minutes < 5 or estimated_minutes > 1440:
        return {"ok": False, "message": "estimated_minutes must be between 5 and 1440."}
    if due_date:
        try:
            date.fromisoformat(due_date)
        except ValueError:
            return {"ok": False, "message": "Use YYYY-MM-DD for due_date."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO tasks(title, subject, due_date, estimated_minutes, status, created_at)
            VALUES (?, ?, ?, ?, 'pending', ?)""",
            (title.strip(), subject.strip(), due_date or None, estimated_minutes, now),
        )
    return {"ok": True, "task_id": cur.lastrowid, "message": f"Added study task: {title.strip()}"}

@mcp.tool()
def list_study_tasks(status: str = "pending") -> dict:
    """List study tasks. status can be pending, completed, or all."""
    if status not in {"pending", "completed", "all"}:
        return {"ok": False, "message": "status must be pending, completed, or all."}
    sql = "SELECT * FROM tasks"
    params = []
    if status != "all":
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY CASE WHEN due_date IS NULL THEN 1 ELSE 0 END, due_date, id"
    with get_db() as conn:
        tasks = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"count": len(tasks), "tasks": tasks}

@mcp.tool()
def complete_study_task(task_id: int) -> dict:
    """Mark a study task as completed."""
    with get_db() as conn:
        cur = conn.execute("UPDATE tasks SET status = 'completed', completed_at = ? WHERE id = ?", (datetime.now().isoformat(timespec="seconds"), task_id))
    return {"ok": cur.rowcount > 0, "message": "Task marked completed." if cur.rowcount else "Task ID not found."}

@mcp.tool()
def create_study_session(title: str, session_date: str, start_time: str = "18:00", duration_minutes: int = 45) -> dict:
    """Save a study session. Date format YYYY-MM-DD and time format HH:MM (24-hour)."""
    try:
        date.fromisoformat(session_date)
        datetime.strptime(start_time, "%H:%M")
    except ValueError:
        return {"ok": False, "message": "Use YYYY-MM-DD for date and HH:MM for time."}
    if duration_minutes < 5 or duration_minutes > 600:
        return {"ok": False, "message": "duration_minutes must be between 5 and 600."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO study_sessions(title, session_date, start_time, duration_minutes, created_at)
            VALUES (?, ?, ?, ?, ?)""",
            (title.strip(), session_date, start_time, duration_minutes, now),
        )
    return {"ok": True, "session_id": cur.lastrowid, "message": "Study session saved."}

@mcp.tool()
def get_study_sessions(from_date: str = "", to_date: str = "") -> dict:
    """List study sessions, optionally between YYYY-MM-DD dates."""
    sql = "SELECT * FROM study_sessions WHERE 1=1"
    params = []
    if from_date:
        try: date.fromisoformat(from_date)
        except ValueError: return {"ok": False, "message": "from_date must be YYYY-MM-DD."}
        sql += " AND session_date >= ?"; params.append(from_date)
    if to_date:
        try: date.fromisoformat(to_date)
        except ValueError: return {"ok": False, "message": "to_date must be YYYY-MM-DD."}
        sql += " AND session_date <= ?"; params.append(to_date)
    sql += " ORDER BY session_date, start_time"
    with get_db() as conn:
        sessions = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"count": len(sessions), "sessions": sessions}

@mcp.tool()
def add_inventory_item(item_name: str, quantity: float = 1, unit: str = "item",
                       expiry_date: str = "", category: str = "general") -> dict:
    """Add an inventory item. expiry_date is optional and must be YYYY-MM-DD."""
    if not item_name.strip():
        return {"ok": False, "message": "item_name is required."}
    if quantity < 0:
        return {"ok": False, "message": "quantity cannot be negative."}
    if expiry_date:
        try: date.fromisoformat(expiry_date)
        except ValueError: return {"ok": False, "message": "expiry_date must be YYYY-MM-DD."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            """INSERT INTO inventory(item_name, quantity, unit, expiry_date, category, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (item_name.strip(), quantity, unit.strip() or "item", expiry_date or None,
             category.strip() or "general", now, now),
        )
    return {"ok": True, "item_id": cur.lastrowid, "message": f"Added {item_name.strip()} to inventory."}

@mcp.tool()
def list_inventory() -> dict:
    """List all inventory items."""
    with get_db() as conn:
        items = rows_to_dicts(conn.execute("SELECT * FROM inventory ORDER BY expiry_date IS NULL, expiry_date, item_name").fetchall())
    return {"count": len(items), "items": items}

@mcp.tool()
def check_expiring_items(within_days: int = 7) -> dict:
    """List expired and soon-to-expire inventory items within the next N days."""
    if within_days < 0 or within_days > 365:
        return {"ok": False, "message": "within_days must be between 0 and 365."}
    today = date.today()
    end = today + timedelta(days=within_days)
    with get_db() as conn:
        items = rows_to_dicts(conn.execute(
            """SELECT * FROM inventory WHERE expiry_date IS NOT NULL AND expiry_date <= ?
            ORDER BY expiry_date, item_name""", (end.isoformat(),)
        ).fetchall())
    for item in items:
        expiry = date.fromisoformat(item["expiry_date"])
        item["expiry_status"] = "expired" if expiry < today else ("expires_today" if expiry == today else "near_expiry")
        item["days_until_expiry"] = (expiry - today).days
    return {"today": today.isoformat(), "within_days": within_days, "count": len(items), "items": items}

@mcp.tool()
def update_inventory_quantity(item_id: int, quantity: float) -> dict:
    """Update the quantity for an inventory item."""
    if quantity < 0:
        return {"ok": False, "message": "quantity cannot be negative."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute("UPDATE inventory SET quantity = ?, updated_at = ? WHERE id = ?", (quantity, now, item_id))
    return {"ok": cur.rowcount > 0, "message": "Inventory updated." if cur.rowcount else "Item ID not found."}

@mcp.tool()
def create_reminder(title: str, remind_at: str, notes: str = "") -> dict:
    """Save a reminder. remind_at must be an ISO datetime like 2026-10-22T18:30."""
    try:
        parsed = datetime.fromisoformat(remind_at)
    except ValueError:
        return {"ok": False, "message": "Use ISO datetime, for example 2026-10-22T18:30."}
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO reminders(title, remind_at, notes, status, created_at) VALUES (?, ?, ?, 'pending', ?)",
            (title.strip(), parsed.isoformat(timespec="seconds" if parsed.second else "minutes"), notes.strip(), now),
        )
    return {"ok": True, "reminder_id": cur.lastrowid, "message": "Reminder saved in the database."}

@mcp.tool()
def list_reminders(status: str = "pending") -> dict:
    """List reminders. status can be pending, completed, or all. This stores reminders but does not send notifications yet."""
    if status not in {"pending", "completed", "all"}:
        return {"ok": False, "message": "status must be pending, completed, or all."}
    sql = "SELECT * FROM reminders"
    params = []
    if status != "all":
        sql += " WHERE status = ?"; params.append(status)
    sql += " ORDER BY remind_at"
    with get_db() as conn:
        reminders = rows_to_dicts(conn.execute(sql, params).fetchall())
    return {"count": len(reminders), "reminders": reminders}

@mcp.tool()
def suggest_dishes(mood: str, available_ingredients: str = "", dietary_preference: str = "none",
                   max_cooking_minutes: int = 30) -> dict:
    """Suggest dish ideas based on mood and available ingredients. This tool returns rule-based ideas, not medical or nutrition advice."""
    mood_key = mood.lower().strip()
    ingredient_text = available_ingredients.lower()
    ideas = {
        "comfort": ["vegetable khichdi", "tomato soup with toast", "simple dal-rice"],
        "happy": ["paneer tikka", "veg pulao", "fruit yogurt bowl"],
        "sad": ["warm tomato soup", "comforting dal-rice", "hot chocolate with toast"],
        "stressed": ["quick vegetable pulao", "curd rice", "simple veggie sandwich"],
        "spicy": ["spicy paneer bhurji", "masala noodles", "chilli potato"],
        "tired": ["one-pot khichdi", "egg or paneer bhurji toast", "quick poha"],
        "healthy": ["vegetable dal", "sprout salad", "vegetable oats"],
        "celebratory": ["paneer tikka", "veg biryani", "homemade pizza"],
    }
    options = ideas.get(mood_key, ["vegetable pulao", "paneer bhurji", "simple dal-rice"])
    if available_ingredients.strip():
        ingredient_words = [x.strip() for x in ingredient_text.replace(" and ", ",").split(",") if x.strip()]
        matched = [dish for dish in options if any(word in dish.lower() for word in ingredient_words)]
        # Ingredient names do not map neatly to dish names; retain suggestions and show the ingredient context.
        if matched:
            options = matched + [dish for dish in options if dish not in matched]
    if dietary_preference.lower() in {"vegetarian", "vegan"}:
        options = [x for x in options if not any(word in x.lower() for word in ["egg", "chicken", "fish", "meat"])]
    return {
        "mood": mood,
        "available_ingredients": available_ingredients,
        "dietary_preference": dietary_preference,
        "max_cooking_minutes": max_cooking_minutes,
        "suggestions": options[:3],
        "note": "Prototype suggestions. Check ingredients, allergens, and cooking times before preparing.",
    }

@mcp.tool()
def get_daily_briefing() -> dict:
    """Summarize pending tasks, today's study sessions, pending reminders, and expiring inventory."""
    today = date.today().isoformat()
    now = datetime.now().isoformat(timespec="minutes")
    with get_db() as conn:
        tasks = rows_to_dicts(conn.execute(
            "SELECT id, title, subject, due_date, estimated_minutes FROM tasks WHERE status='pending' ORDER BY due_date LIMIT 10"
        ).fetchall())
        sessions = rows_to_dicts(conn.execute(
            "SELECT id, title, session_date, start_time, duration_minutes FROM study_sessions WHERE session_date = ? ORDER BY start_time",
            (today,),
        ).fetchall())
        reminders = rows_to_dicts(conn.execute(
            "SELECT id, title, remind_at FROM reminders WHERE status='pending' AND remind_at >= ? ORDER BY remind_at LIMIT 10",
            (now,),
        ).fetchall())
        items = rows_to_dicts(conn.execute(
            "SELECT id, item_name, quantity, unit, expiry_date FROM inventory WHERE expiry_date IS NOT NULL AND expiry_date <= ? ORDER BY expiry_date",
            ((date.today() + timedelta(days=7)).isoformat(),),
        ).fetchall())
    return {"date": today, "pending_tasks": tasks, "today_study_sessions": sessions,
            "upcoming_reminders": reminders, "expired_or_near_expiry_inventory": items}


# ------------------------- Secure password vault -------------------------
# This local prototype encrypts the entire entry (including title/site/username)
# with a key derived from the master password. The master password is never
# written to the database. The in-memory key auto-locks after 10 minutes idle.
def _derive_vault_key(master_password: str, salt: bytes) -> bytes:
    kdf = Scrypt(salt=salt, length=32, n=2**14, r=8, p=1)
    raw_key = kdf.derive(master_password.encode("utf-8"))
    return base64.urlsafe_b64encode(raw_key)


def _lock_vault_key() -> None:
    global _VAULT_KEY, _VAULT_LAST_ACTIVITY
    _VAULT_KEY = None
    _VAULT_LAST_ACTIVITY = 0.0


def _active_fernet() -> Fernet | None:
    global _VAULT_KEY, _VAULT_LAST_ACTIVITY
    if _VAULT_KEY is None:
        return None
    if time.monotonic() - _VAULT_LAST_ACTIVITY > _VAULT_IDLE_TIMEOUT_SECONDS:
        _lock_vault_key()
        return None
    _VAULT_LAST_ACTIVITY = time.monotonic()
    return Fernet(_VAULT_KEY)


def _vault_is_initialized() -> bool:
    with get_db() as conn:
        return conn.execute("SELECT 1 FROM password_vault_meta WHERE id = 1").fetchone() is not None


@mcp.tool()
def setup_password_vault(master_password: str) -> dict:
    """Initialize the encrypted password vault once. Use a demo-only master password during MCP Inspector testing; choose a long unique passphrase for real use."""
    if len(master_password) < 12:
        return {"ok": False, "message": "Master password must be at least 12 characters long."}
    if _vault_is_initialized():
        return {"ok": False, "message": "Vault is already initialized. Use unlock_password_vault instead."}
    salt = secrets.token_bytes(16)
    key = _derive_vault_key(master_password, salt)
    verifier_ciphertext = Fernet(key).encrypt(_VAULT_VERIFIER)
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        conn.execute(
            "INSERT INTO password_vault_meta(id, salt, verifier_ciphertext, created_at) VALUES (1, ?, ?, ?)",
            (salt, verifier_ciphertext, now),
        )
    global _VAULT_KEY, _VAULT_LAST_ACTIVITY
    _VAULT_KEY = key
    _VAULT_LAST_ACTIVITY = time.monotonic()
    return {"ok": True, "message": "Password vault initialized and unlocked. Store only demo credentials while testing in MCP Inspector."}


@mcp.tool()
def unlock_password_vault(master_password: str) -> dict:
    """Unlock the encrypted password vault in memory using its master password."""
    with get_db() as conn:
        meta = conn.execute("SELECT salt, verifier_ciphertext FROM password_vault_meta WHERE id = 1").fetchone()
    if meta is None:
        return {"ok": False, "message": "Vault is not initialized. Call setup_password_vault first."}
    key = _derive_vault_key(master_password, bytes(meta["salt"]))
    try:
        verified = Fernet(key).decrypt(bytes(meta["verifier_ciphertext"]))
    except InvalidToken:
        return {"ok": False, "message": "Incorrect master password."}
    if verified != _VAULT_VERIFIER:
        return {"ok": False, "message": "Vault verification failed."}
    global _VAULT_KEY, _VAULT_LAST_ACTIVITY
    _VAULT_KEY = key
    _VAULT_LAST_ACTIVITY = time.monotonic()
    return {"ok": True, "message": "Password vault unlocked. It will auto-lock after 10 minutes without activity."}


@mcp.tool()
def lock_password_vault() -> dict:
    """Lock the vault immediately and clear its encryption key from process memory."""
    _lock_vault_key()
    return {"ok": True, "message": "Password vault locked."}


@mcp.tool()
def save_password_entry(title: str, website: str, username: str, password: str, notes: str = "") -> dict:
    """Save a credential encrypted at rest. The vault must be unlocked. Never use real credentials in MCP Inspector demos."""
    fernet = _active_fernet()
    if fernet is None:
        return {"ok": False, "message": "Vault is locked. Call unlock_password_vault first."}
    if not title.strip() or not website.strip() or not username.strip() or not password:
        return {"ok": False, "message": "Title, website, username, and password are required."}
    payload = {
        "title": title.strip(), "website": website.strip(), "username": username.strip(),
        "password": password, "notes": notes.strip(),
    }
    ciphertext = fernet.encrypt(json.dumps(payload, ensure_ascii=False).encode("utf-8"))
    now = datetime.now().isoformat(timespec="seconds")
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO password_entries(ciphertext, created_at, updated_at) VALUES (?, ?, ?)",
            (ciphertext, now, now),
        )
    return {"ok": True, "entry_id": cur.lastrowid, "message": f"Encrypted password entry saved: {title.strip()}"}


@mcp.tool()
def list_password_entries(query: str = "") -> dict:
    """List password-vault entry metadata without revealing passwords. The vault must be unlocked."""
    fernet = _active_fernet()
    if fernet is None:
        return {"ok": False, "message": "Vault is locked. Call unlock_password_vault first."}
    with get_db() as conn:
        rows = conn.execute("SELECT id, ciphertext, created_at, updated_at FROM password_entries ORDER BY id DESC").fetchall()
    entries = []
    for row in rows:
        try:
            payload = json.loads(fernet.decrypt(bytes(row["ciphertext"])).decode("utf-8"))
        except (InvalidToken, ValueError, UnicodeDecodeError):
            return {"ok": False, "message": "Could not decrypt a vault entry. Check that the vault is unlocked with the correct master password."}
        if query.strip() and query.strip().casefold() not in " ".join(
            [payload.get("title", ""), payload.get("website", ""), payload.get("username", "")]
        ).casefold():
            continue
        entries.append({"entry_id": row["id"], "title": payload["title"], "website": payload["website"],
                        "username": payload["username"], "created_at": row["created_at"], "updated_at": row["updated_at"]})
    return {"ok": True, "count": len(entries), "entries": entries, "note": "Passwords are intentionally omitted. Use reveal_password_entry only when you need to view a specific secret."}


@mcp.tool()
def reveal_password_entry(entry_id: int) -> dict:
    """Decrypt and reveal one saved password entry. Use only when necessary; the returned secret may appear in MCP client history."""
    fernet = _active_fernet()
    if fernet is None:
        return {"ok": False, "message": "Vault is locked. Call unlock_password_vault first."}
    with get_db() as conn:
        row = conn.execute("SELECT ciphertext FROM password_entries WHERE id = ?", (entry_id,)).fetchone()
    if row is None:
        return {"ok": False, "message": "Password entry ID not found."}
    try:
        payload = json.loads(fernet.decrypt(bytes(row["ciphertext"])).decode("utf-8"))
    except (InvalidToken, ValueError, UnicodeDecodeError):
        return {"ok": False, "message": "Could not decrypt this entry. Check the vault master password."}
    return {"ok": True, "entry_id": entry_id, **payload,
            "warning": "This secret is now present in the tool result. Avoid sharing or exporting this Inspector session."}


@mcp.tool()
def delete_password_entry(entry_id: int) -> dict:
    """Permanently delete one encrypted password entry by ID. The vault must be unlocked."""
    if _active_fernet() is None:
        return {"ok": False, "message": "Vault is locked. Call unlock_password_vault first."}
    with get_db() as conn:
        cur = conn.execute("DELETE FROM password_entries WHERE id = ?", (entry_id,))
    return {"ok": cur.rowcount > 0, "message": "Password entry deleted." if cur.rowcount else "Password entry ID not found."}


if __name__ == "__main__":
    # The official MCP Python SDK supports Streamable HTTP transport via FastMCP.
    mcp.run(transport="streamable-http")
