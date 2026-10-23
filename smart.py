"""Smart voice/chat command engine for AlexaPlus.

Understands (English, Hindi and Hinglish):
  * reminders / timers / alarms with real times  ("remind me to take medicine after 2 min")
  * inventory with expiry dates + automatic "about to expire" reminders
  * "add this data to your database" (memory notes)
  * music ("play Kesariya", "play X on spotify", "stop the music") and "open youtube"
  * funny canned replies

Everything here is plain Python (no extra packages) so it is easy to test.
`handle()` returns a response dict for /api/chat, or None when the message is not a
smart command (the normal chat flow then continues).
"""
from __future__ import annotations

import json
import random
import re
import time as _time
import urllib.error
import urllib.request
from datetime import date, datetime, time, timedelta
from urllib.parse import quote_plus

import server as core

# ----------------------------------------------------------------------------
# Small helpers
# ----------------------------------------------------------------------------
_ONES = ("zero|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|thirteen|fourteen|"
         "fifteen|sixteen|seventeen|eighteen|nineteen")
_TENS = "twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety"
_HI = "ek|do|teen|char|chaar|paanch|panch|chhe|saat|aath|nau|das|gyarah|barah|pandrah|bees|tees|chalis|pachas"
_NUM_WORDS = {w: i for i, w in enumerate(_ONES.split("|"))}
_NUM_WORDS.update({"twenty": 20, "thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
                   "eighty": 80, "ninety": 90, "ek": 1, "do": 2, "teen": 3, "char": 4, "chaar": 4,
                   "paanch": 5, "panch": 5, "chhe": 6, "saat": 7, "aath": 8, "nau": 9, "das": 10,
                   "gyarah": 11, "barah": 12, "pandrah": 15, "bees": 20, "tees": 30, "chalis": 40,
                   "pachas": 50})
NUM = rf"(?:\d+(?:\.\d+)?|(?:{_TENS})(?:[\s-]+(?:{_ONES}))?|{_ONES}|{_HI}|half\s+an?|half|quarter|an?)"
UNIT = (r"(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|hr|ghant[ae]|ghanta|days?|din|weeks?|hafte?|"
        r"months?)")
PAIR = rf"{NUM}\s*{UNIT}\b"
MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7, "aug": 8, "sep": 9,
          "oct": 10, "nov": 11, "dec": 12}
MONTH_RE = (r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
            r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)")
WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
            "thursday": 3, "thu": 3, "thur": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5,
            "sat": 5, "sunday": 6, "sun": 6}
WEEKDAY_RE = r"(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday|mon|tues?|wed|thu(?:rs?)?|fri|sat|sun)"


def parse_number(token: str) -> float | None:
    t = token.strip().lower()
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        pass
    if t in {"a", "an"}:
        return 1.0
    if t.startswith("half"):
        return 0.5
    if t == "quarter":
        return 0.25
    parts = re.split(r"[\s-]+", t)
    total = 0
    for p in parts:
        if p not in _NUM_WORDS:
            return None
        total += _NUM_WORDS[p]
    return float(total)


_WAKE_RE = re.compile(
    r"\b(?:(?:hey|ok|okay|hi|hello)\s+)?(?:alexa\+?|alexa plus|alexia|alexsa|alexis|elexa|aleksa|alexxa|lexa)\b[,.!?:]*\s*",
    re.I)
_FILLER_RE = re.compile(
    r"^(?:please|pls|plz|kindly|can you|could you|would you|will you|can u|could u|i want you to|i need you to|"
    r"i want to|i would like you to|just|now|then|and|so|ok|okay|hey)\b[,\s]*", re.I)
_TRAIL_RE = re.compile(r"[,\s]*\b(?:please|pls|plz|for me|right now|now|thanks|thank you)\s*[.!?]*\s*$", re.I)


def clean_command(text: str) -> str:
    t = (text or "").strip()
    t = _WAKE_RE.sub(" ", t, count=1) if re.search(r"alex|lexa", t, re.I) else t
    t = t.strip(" ,.!?")
    for _ in range(4):
        new = _FILLER_RE.sub("", t).strip()
        if new == t:
            break
        t = new
    for _ in range(2):
        t = _TRAIL_RE.sub("", t).strip()
    return t.strip()


# ----------------------------------------------------------------------------
# Date / time parsing
# ----------------------------------------------------------------------------
def _cut(text: str, span: tuple[int, int]) -> str:
    return (text[:span[0]] + " " + text[span[1]:])


def _tidy(text: str) -> str:
    t = re.sub(r"\s+", " ", text).strip(" ,.-:;!?")
    for _ in range(3):
        t2 = re.sub(r"^(?:at|on|in|after|by|for|to|about|that|the)\s+", "", t, flags=re.I)
        t2 = re.sub(r"\s+(?:at|on|in|after|by|for|to|and|that|ko|ka|ki|ke|liye)$", "", t2, flags=re.I)
        if t2 == t:
            break
        t = t2.strip(" ,.-:;!?")
    return t


def find_duration(text: str, allow_for: bool = False) -> tuple[timedelta, tuple[int, int]] | None:
    """Find 'in/after 2 minutes', '1 hour 30 minutes', 'half an hour', '2 min baad'."""
    prefix = r"(?:(?:in|after|within" + (r"|for" if allow_for else "") + r"|baad)\s+)"
    rx = re.compile(
        rf"(?:{prefix})?(?P<body>{PAIR}(?:\s*(?:,|and|aur)?\s*{PAIR})*(?:\s+and\s+a\s+half)?)(?P<tail>\s+(?:baad|later|from now))?",
        re.I)
    best = None
    for m in rx.finditer(text):
        has_prefix = bool(re.match(r"\s*(?:in|after|within|for|baad)\s", m.group(0), re.I))
        has_tail = bool(m.group("tail"))
        # Bare "5 minutes" (no in/after/later) is not a duration unless nothing else gives a time.
        if not (has_prefix or has_tail):
            continue
        total = timedelta()
        for pm in re.finditer(rf"(?P<n>{NUM})\s*(?P<u>{UNIT})\b", m.group("body"), re.I):
            n = parse_number(pm.group("n"))
            if n is None:
                continue
            u = pm.group("u").lower()
            if u.startswith("sec"):
                total += timedelta(seconds=n)
            elif u.startswith("min"):
                total += timedelta(minutes=n)
            elif u.startswith(("hour", "hr", "ghant")):
                total += timedelta(hours=n)
            elif u.startswith(("day", "din")):
                total += timedelta(days=n)
            elif u.startswith(("week", "hafte")):
                total += timedelta(weeks=n)
            elif u.startswith("month"):
                total += timedelta(days=30 * n)
        if re.search(r"and\s+a\s+half", m.group("body"), re.I):
            total += total / 2 if total else timedelta()
        if total > timedelta():
            best = (total, m.span())
            break
    return best


def find_date(text: str, today: date | None = None) -> tuple[date, tuple[int, int]] | None:
    """Find the first date expression. Returns (date, span)."""
    today = today or date.today()
    low = text
    cands: list[tuple[int, date, tuple[int, int]]] = []

    def add(m, d):
        if d:
            cands.append((m.start(), d, m.span()))

    for m in re.finditer(r"\b(?:day after tomorrow|parso)\b", low, re.I):
        add(m, today + timedelta(days=2))
    for m in re.finditer(r"\b(?:tomorrow|tmrw|tommorow|tomorow|kal)\b", low, re.I):
        if not any(c[2][0] <= m.start() < c[2][1] for c in cands):
            add(m, today + timedelta(days=1))
    for m in re.finditer(r"\b(?:today|tonight|aaj)\b", low, re.I):
        add(m, today)
    for m in re.finditer(r"\b(?:ISO)\b", low):
        pass
    for m in re.finditer(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", low):
        try:
            add(m, date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    for m in re.finditer(r"\b(\d{1,2})[/.\-](\d{1,2})(?:[/.\-](\d{2,4}))?\b", low):
        # day-first (Indian format). Skip things like "5.30" (time) when there is no year and no slash/dash.
        sep_ok = any(s in m.group(0) for s in "/-") or m.group(3)
        if not sep_ok:
            continue
        d, mo = int(m.group(1)), int(m.group(2))
        y = int(m.group(3)) if m.group(3) else today.year
        if y < 100:
            y += 2000
        try:
            dt = date(y, mo, d)
            if not m.group(3) and dt < today:
                dt = date(y + 1, mo, d)
            add(m, dt)
        except ValueError:
            pass
    for m in re.finditer(rf"\b(?:on\s+|the\s+)?(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?({MONTH_RE})\b(?:[,\s]+(\d{{4}}))?", low, re.I):
        mo = MONTHS[m.group(2)[:3].lower()]
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            dt = date(y, mo, int(m.group(1)))
            if not m.group(3) and dt < today:
                dt = date(y + 1, mo, int(m.group(1)))
            add(m, dt)
        except ValueError:
            pass
    for m in re.finditer(rf"\b(?:on\s+)?({MONTH_RE})\s+(\d{{1,2}})(?:st|nd|rd|th)?\b(?:[,\s]+(\d{{4}}))?", low, re.I):
        mo = MONTHS[m.group(1)[:3].lower()]
        y = int(m.group(3)) if m.group(3) else today.year
        try:
            dt = date(y, mo, int(m.group(2)))
            if not m.group(3) and dt < today:
                dt = date(y + 1, mo, int(m.group(2)))
            add(m, dt)
        except ValueError:
            pass
    for m in re.finditer(r"\bnext\s+week\b", low, re.I):
        add(m, today + timedelta(days=7))
    for m in re.finditer(r"\bnext\s+month\b", low, re.I):
        add(m, today + timedelta(days=30))
    for m in re.finditer(rf"\b(?:(next|this|on)\s+)?({WEEKDAY_RE})\b", low, re.I):
        wd = WEEKDAYS[m.group(2).lower()]
        delta = (wd - today.weekday()) % 7
        if delta == 0 or (m.group(1) or "").lower() == "next" and delta < 7 and False:
            delta = 7
        add(m, today + timedelta(days=delta))
    for m in re.finditer(rf"\b(?:in|after|within)\s+(?P<n>{NUM})\s*(?P<u>days?|din|weeks?|hafte?|months?)\b", low, re.I):
        n = parse_number(m.group("n"))
        if n is None:
            continue
        u = m.group("u").lower()
        days = n * (7 if u.startswith(("week", "hafte")) else 30 if u.startswith("month") else 1)
        add(m, today + timedelta(days=int(days)))
    if not cands:
        return None
    cands.sort(key=lambda c: c[0])
    # prefer the longest span when two candidates overlap at the same start
    first = cands[0]
    same = [c for c in cands if c[0] == first[0]]
    best = max(same, key=lambda c: c[2][1] - c[2][0])
    return best[1], best[2]


_PERIOD = r"(?:morning|subah|afternoon|dopahar|evening|shaam|night|raat|tonight)"


def find_clock(text: str) -> tuple[int, int | None, str, tuple[int, int]] | None:
    """Find a clock time. Returns (hour, minute, ampm_or_period, span). ampm may be '' when unknown."""
    rx_list = [
        # 5 pm / 5:30 p.m. / at 5pm
        re.compile(r"\b(?:(?:at|by|around|@|on)\s*)?(?P<h>\d{1,2})(?:[:.](?P<m>\d{2}))?\s*(?P<ap>a\.?\s?m\.?|p\.?\s?m\.?)(?=\W|$)", re.I),
        # at 17:30 / at 5:30
        re.compile(r"\b(?:at|by|around|@)\s*(?P<h>\d{1,2})[:.](?P<m>\d{2})\b(?P<ap>)", re.I),
        # 8 baje / 8 o'clock / at 8 (in the morning)
        re.compile(rf"\b(?:(?P<p0>{_PERIOD})\s+)?(?:(?:at|by|around)\s+)?(?P<h>\d{{1,2}})\s*(?:o'?clock|baje)(?:\s+(?:in the\s+|at\s+)?(?P<p1>{_PERIOD}))?\b(?P<ap>)", re.I),
        re.compile(rf"\b(?:at|by|around)\s+(?P<h>\d{{1,2}})\b(?:\s+(?:in the\s+|at\s+)?(?P<p1>{_PERIOD}))?(?P<ap>)(?=\W|$)", re.I),
    ]
    for rx in rx_list:
        m = rx.search(text)
        if not m:
            continue
        h = int(m.group("h"))
        mi = m.groupdict().get("m")
        mi = int(mi) if mi else 0
        if h > 24 or mi > 59:
            continue
        ap = (m.groupdict().get("ap") or "").lower().replace(".", "").replace(" ", "")
        period = (m.groupdict().get("p0") or m.groupdict().get("p1") or "").lower()
        return h, mi, ap or period, m.span()
    m = re.search(r"\b(?:at\s+)?(noon|midday)\b", text, re.I)
    if m:
        return 12, 0, "pm", m.span()
    m = re.search(r"\b(?:at\s+)?midnight\b", text, re.I)
    if m:
        return 0, 0, "am", m.span()
    return None


def resolve_clock(h: int, mi: int, marker: str, base: date, now: datetime) -> datetime:
    if marker in {"am", "morning", "subah"}:
        hour = 0 if h == 12 else h
    elif marker in {"pm"}:
        hour = h if h >= 12 else h + 12
        if h == 12:
            hour = 12
    elif marker in {"afternoon", "dopahar", "evening", "shaam"}:
        hour = h + 12 if h < 12 else h
    elif marker in {"night", "raat", "tonight"}:
        hour = 0 if h == 12 else (h + 12 if 6 <= h < 12 else h)
    else:
        if h >= 13 or h == 0:
            hour = h % 24
        else:
            # ambiguous 1..12: choose the next occurrence when the date is today, else a sensible default
            opts = [h % 12, (h % 12) + 12]
            if base == now.date():
                future = [datetime.combine(base, time(o, mi)) for o in opts if datetime.combine(base, time(o, mi)) > now]
                if future:
                    return min(future)
                return datetime.combine(base, time(opts[0], mi))
            hour = h if 7 <= h <= 11 else (h % 12) + 12
    return datetime.combine(base, time(hour % 24, mi))


class When:
    def __init__(self, due: datetime | None, rest: str, relative: bool = False, has_clock: bool = False, has_date: bool = False):
        self.due, self.rest, self.relative, self.has_clock, self.has_date = due, rest, relative, has_clock, has_date


def parse_when(text: str, now: datetime | None = None, allow_for: bool = False) -> When:
    """Extract the time from a sentence. Returns When(due, text_without_time_phrase)."""
    now = now or datetime.now()
    dur = find_duration(text, allow_for=allow_for)
    if dur:
        delta, span = dur
        rest = _tidy(_cut(text, span))
        if delta >= timedelta(days=1) and not re.search(r"\d\s*(?:a|p)\.?m", text, re.I):
            due = datetime.combine((now + delta).date(), time(9, 0))
            clock = find_clock(rest)
            if clock:
                h, mi, mk, sp = clock
                due = resolve_clock(h, mi, mk, (now + delta).date(), now)
                rest = _tidy(_cut(rest, sp))
            return When(due, rest, relative=True, has_date=True)
        return When(now + delta, rest, relative=True)
    rest = text
    base = None
    d = find_date(rest, now.date())
    if d:
        base, span = d
        rest = _cut(rest, span)
    clock = find_clock(rest)
    due = None
    if clock:
        h, mi, mk, span = clock
        rest = _cut(rest, span)
        use_base = base or now.date()
        due = resolve_clock(h, mi, mk, use_base, now)
        if base is None and due <= now:
            due = resolve_clock(h, mi, mk, now.date() + timedelta(days=1), now)
    elif base is not None:
        due = datetime.combine(base, time(9, 0))
    return When(due, _tidy(rest), has_clock=bool(clock), has_date=base is not None)


def describe_when(due: datetime, now: datetime | None = None) -> str:
    now = now or datetime.now()
    secs = int((due - now).total_seconds())
    if secs < 90:
        return f"in {max(secs, 1)} seconds" if secs < 60 else "in about a minute"
    if secs < 3600:
        mins = round(secs / 60)
        return f"in {mins} minute{'s' if mins != 1 else ''}"
    clock = due.strftime("%I:%M %p").lstrip("0")
    if due.date() == now.date():
        return f"today at {clock}"
    if due.date() == now.date() + timedelta(days=1):
        return f"tomorrow at {clock}"
    return f"on {due.strftime('%a')} {due.day} {due.strftime('%b')} at {clock}"


def pretty_date(d: date, today: date | None = None) -> str:
    """'today' / 'tomorrow' / 'yesterday' / 'on 25 Oct 2026'."""
    today = today or date.today()
    diff = (d - today).days
    if diff == 0:
        return "today"
    if diff == 1:
        return "tomorrow"
    if diff == -1:
        return "yesterday"
    return "on " + d.strftime("%d %b %Y").lstrip("0")


def plural_unit(qty: float, unit: str) -> str:
    if qty == 1 or unit in {"item", "kg", "g", "l", "ml", "dozen"}:
        return unit
    if unit.endswith(("x", "s", "ch", "sh")):
        return unit + "es"
    if unit == "loaf":
        return "loaves"
    return unit + "s"


# ----------------------------------------------------------------------------
# Personality
# ----------------------------------------------------------------------------
def pick(options: list[str]) -> str:
    return random.choice(options)


REMINDER_TAILS = [
    "Your future self says thank you!",
    "No excuses, I'm watching. Lovingly.",
    "I would do it myself, but I have no hands.",
    "Go on, I'll wait. I'm very patient. I'm software.",
    "Chop chop!",
]
REMINDER_OPENERS = ["Ding ding!", "Psst!", "Hey you!", "Knock knock!", "Attention please!"]


def reminder_announcement(title: str, late_minutes: int = 0) -> str:
    t = title.strip().rstrip(".")
    if t.lower().startswith(("heads up", "⚠")):
        return t
    if late_minutes >= 10:
        return f"Oops, I'm late. Reminder: {t}. Better late than never, right?"
    return f"{pick(REMINDER_OPENERS)} Reminder: {t}. {pick(REMINDER_TAILS)}"


# ----------------------------------------------------------------------------
# Conversation state (single local user)
# ----------------------------------------------------------------------------
_PENDING: dict = {"kind": None, "data": {}, "ts": 0.0}
_LAST_ITEM: dict = {"id": None, "name": "", "ts": 0.0}
PENDING_TTL = 180


def set_pending(pending_kind: str, **data) -> None:
    _PENDING.update(kind=pending_kind, data=data, ts=_time.time())


def clear_pending() -> None:
    _PENDING.update(kind=None, data={}, ts=0.0)


def get_pending() -> tuple[str | None, dict]:
    if _PENDING["kind"] and _time.time() - _PENDING["ts"] > PENDING_TTL:
        clear_pending()
    return _PENDING["kind"], _PENDING["data"]


def _remember_item(item_id: int | None, name: str) -> None:
    _LAST_ITEM.update(id=item_id, name=name, ts=_time.time())


# ----------------------------------------------------------------------------
# Reminders
# ----------------------------------------------------------------------------
_REMIND_RE = re.compile(
    r"\b(?:remind\s+(?:me|us)|reminder|set\s+(?:an?\s+)?(?:alarm|timer)|alarm\b|timer\b|wake\s+me(?:\s+up)?|"
    r"yaad\s+dila\w*|yaad\s+dil\w*|alert\s+me|notify\s+me|ping\s+me)", re.I)
_LIST_REM_RE = re.compile(r"\b(?:show|list|what(?:'s| are| is)?|tell me|read|check|any|my)\b.*\b(?:reminders?|alarms?|timers?)\b|\b(?:reminders?|alarms?)\s+(?:list|kya hai)\b", re.I)
_CANCEL_REM_RE = re.compile(r"\b(?:cancel|delete|remove|clear|stop|dismiss)\b.*\b(?:reminders?|alarms?|timers?)\b", re.I)


def _create_reminder(title: str, due: datetime, notes: str = "") -> dict:
    return core.create_reminder(title, due.isoformat(timespec="seconds"), notes)


def _extract_reminder_title(cmd: str) -> tuple[str, str, str]:
    """Returns (title, connector, kind). kind in reminder|timer|alarm|wake."""
    kind = "reminder"
    low = cmd.lower()
    if re.search(r"\btimer\b", low):
        kind = "timer"
    elif re.search(r"\balarm\b", low):
        kind = "alarm"
    elif re.search(r"\bwake\s+me", low):
        kind = "wake"
    connector = "to"
    t = cmd
    m = re.search(r"remind\s+(?:me|us)\s+(to|about|that|of)\b", t, re.I)
    if m:
        connector = "about" if m.group(1).lower() in {"about", "of"} else "to"
    elif re.search(r"(?:set\s+(?:an?\s+)?reminder\s+(?:for|about))", t, re.I):
        connector = "about"
    t = re.sub(r"\b(?:remind\s+(?:me|us)|set\s+(?:an?\s+)?(?:reminder|alarm|timer)|add\s+(?:an?\s+)?reminder|wake\s+me\s+up|wake\s+me|alert\s+me|notify\s+me|ping\s+me)\b", " ", t, flags=re.I)
    t = re.sub(r"\byaad\s+dil\w*\b|\bmujhe\b|\bmujhko\b|\bhume\b", " ", t, flags=re.I)
    t = _tidy(t)
    t = re.sub(r"^(?:to|about|that|for|of)\s+", "", t, flags=re.I)
    t = re.sub(r"\b(?:ki|ke liye|ko)\b$", "", t, flags=re.I).strip(" ,.")
    t = re.sub(r"\bmy\b", "my", t, flags=re.I)
    return _tidy(t), connector, kind


def _reminder_confirmation(title: str, connector: str, due: datetime, kind: str) -> tuple[str, str]:
    when = describe_when(due)
    clock = due.strftime("%I:%M:%S %p").lstrip("0") if (due - datetime.now()).total_seconds() < 3600 else due.strftime("%I:%M %p").lstrip("0")
    if kind == "timer":
        reply = f"Timer set {when}! I'll shout when it's done. (That's {clock}.)"
    elif kind in {"alarm", "wake"}:
        reply = f"Alarm set for {due.strftime('%I:%M %p').lstrip('0')} ({when}). Rise and shine, sleepyhead!"
    else:
        phr = f"{connector} {title}" if title else ""
        reply = pick([
            f"Done! I'll remind you {phr} {when}. Consider it etched in my silicon brain.",
            f"Got it! Reminder set {when}: {title}. I never forget. It's my only talent.",
            f"Sure thing! I'll tap you on the shoulder {when} {connector} {title}.",
        ]).replace("  ", " ")
    return reply, reply


def _handle_reminder(cmd: str) -> dict | None:
    now = datetime.now()
    when = parse_when(cmd, now, allow_for=bool(re.search(r"\btimer\b", cmd, re.I)))
    title, connector, kind = _extract_reminder_title(when.rest if when.rest else cmd)
    # If title extraction ate everything from the 'rest', redo on the original with time removed.
    if not title:
        title2, connector, kind = _extract_reminder_title(when.rest)
        title = title2
    if kind == "timer" and not title:
        title = "Your timer is up"
    if kind in {"alarm"} and not title:
        title = "Alarm"
    if kind == "wake" and not title:
        title = "Wake up"
    if when.due is None:
        set_pending("reminder_time", title=title, connector=connector, kind=kind)
        if not title:
            set_pending("reminder_both", connector=connector, kind=kind)
            return {"ok": True, "reply": "Sure! What should I remind you about, and when?"}
        return {"ok": True, "reply": f"Sure! When should I remind you {connector} {title}? You can say things like “in 2 minutes”, “at 6 PM” or “tomorrow morning”."}
    if not title:
        set_pending("reminder_title", due=when.due.isoformat(), connector=connector, kind=kind)
        return {"ok": True, "reply": f"Okay, {describe_when(when.due)}. What should I remind you about?"}
    if when.due <= now:
        when.due = now + timedelta(seconds=5)
    res = _create_reminder(title, when.due, "Created by voice/chat command.")
    if not res.get("ok"):
        return {"ok": False, "reply": res.get("message", "I couldn't save that reminder.")}
    reply, speech = _reminder_confirmation(title, connector, when.due, kind)
    return {"ok": True, "reply": reply, "speech": speech, "data": res, "refresh": ["reminders"]}


def _list_reminders() -> dict:
    res = core.list_reminders("pending")
    if not res["count"]:
        return {"ok": True, "reply": "You have no pending reminders. Your schedule is as empty as my fridge at midnight."}
    lines = []
    for r in res["reminders"][:10]:
        try:
            due = datetime.fromisoformat(r["remind_at"])
            lines.append(f"• {r['title']} — {describe_when(due)}")
        except ValueError:
            lines.append(f"• {r['title']} — {r['remind_at']}")
    spoken = "You have " + str(res["count"]) + " pending reminder" + ("s" if res["count"] != 1 else "") + ". " + \
             ". ".join(l[2:].replace(" — ", ", ") for l in lines[:4])
    return {"ok": True, "reply": "Here are your pending reminders:\n" + "\n".join(lines), "speech": spoken}


def _cancel_reminders(cmd: str) -> dict:
    low = cmd.lower()
    with core.get_db() as conn:
        if re.search(r"\b(?:all|every|everything)\b", low):
            cur = conn.execute("UPDATE reminders SET status='completed' WHERE status='pending'")
            n = cur.rowcount
        else:
            rows = conn.execute("SELECT id,title FROM reminders WHERE status='pending' ORDER BY remind_at").fetchall()
            keyword = re.sub(r"\b(?:cancel|delete|remove|clear|stop|dismiss|my|the|a|an|reminders?|alarms?|timers?|about|for|of)\b", " ", low)
            keyword = keyword.strip(" ,.")
            hits = [r for r in rows if keyword and keyword in r["title"].lower()] if keyword else []
            if not hits and rows and not keyword:
                hits = [rows[0]]  # "cancel the reminder" -> the next one
            for r in hits:
                conn.execute("UPDATE reminders SET status='completed' WHERE id=?", (r["id"],))
            n = len(hits)
    if not n:
        return {"ok": True, "reply": "I couldn't find a matching pending reminder to cancel."}
    return {"ok": True, "reply": f"Cancelled {n} reminder{'s' if n != 1 else ''}. Poof! Gone, like my motivation on Mondays.", "refresh": ["reminders"]}


# ----------------------------------------------------------------------------
# Inventory + expiry
# ----------------------------------------------------------------------------
_UNITS = {
    "kg": "kg", "kgs": "kg", "kilo": "kg", "kilos": "kg", "kilogram": "kg", "kilograms": "kg",
    "g": "g", "gm": "g", "gms": "g", "gram": "g", "grams": "g",
    "l": "l", "ltr": "l", "litre": "l", "litres": "l", "liter": "l", "liters": "l",
    "ml": "ml", "packet": "packet", "packets": "packet", "pack": "pack", "packs": "pack",
    "piece": "piece", "pieces": "piece", "pcs": "piece", "pc": "piece", "dozen": "dozen",
    "bottle": "bottle", "bottles": "bottle", "box": "box", "boxes": "box", "can": "can", "cans": "can",
    "loaf": "loaf", "loaves": "loaf", "bag": "bag", "bags": "bag", "cup": "cup", "cups": "cup",
    "strip": "strip", "strips": "strip", "tablet": "tablet", "tablets": "tablet", "jar": "jar", "jars": "jar",
}
_UNIT_RE = "|".join(sorted(_UNITS, key=len, reverse=True))
_CATEGORIES = {
    "dairy": ["milk", "curd", "yogurt", "yoghurt", "cheese", "paneer", "butter", "ghee", "cream", "dahi"],
    "vegetable": ["tomato", "potato", "onion", "carrot", "spinach", "cabbage", "capsicum", "cucumber", "vegetable", "palak", "aloo", "pyaz"],
    "fruit": ["apple", "banana", "mango", "orange", "grapes", "fruit", "papaya", "kela", "aam"],
    "bakery": ["bread", "bun", "cake", "biscuit", "cookies", "pav"],
    "meat": ["chicken", "egg", "eggs", "fish", "mutton", "meat"],
    "medicine": ["medicine", "tablet", "tablets", "syrup", "capsule", "ointment", "paracetamol", "crocin", "dawai"],
    "grocery": ["rice", "dal", "atta", "flour", "sugar", "salt", "oil", "masala", "pasta", "noodles", "maggi"],
}


def guess_category(name: str) -> str:
    low = name.lower()
    for cat, words in _CATEGORIES.items():
        if any(re.search(rf"\b{re.escape(w)}s?\b", low) for w in words):
            return cat
    return "general"


_EXPIRY_KW = (r"(?:expir(?:y|es|ing|ed|ation)(?:\s+date)?|best\s+before|use\s+by|valid\s+(?:till|until)|"
              r"exp(?:\.|\s+date)?)")
_ADD_PREFIX = re.compile(
    r"^(?:please\s+)?(?:add|put|store|save|insert|enter|log|include|buy|purchase|i\s+(?:just\s+)?(?:bought|got|purchased)|bought|got|purchased)\b"
    r"(?:\s+(?:this|these|the following|new))?(?:\s+(?:data|item|items|stuff|product|details?|info|information))?\s*(?:[:\-]|that|called|named)?\s*", re.I)
_TARGET_SUFFIX = re.compile(
    r"\s*(?:\b(?:in|into|to|on)\b\s+(?:(?:your|ur|my|the)\s+)?(?:inventory|stock|pantry|fridge|kitchen|database|data\s*base|db|store|list)\b)\s*[,:\-]?\s*", re.I)
_LEAD_RE = re.compile(
    rf"(?:(?:and\s+)?(?:also\s+)?(?:remind|alert|notify)\s+me\s+)?(?:(?P<n>{NUM})\s+days?\s+before|(?:a\s+day|one\s+day)\s+before|before\s+(?:it\s+)?expir\w*|before\s+expiry)(?:\s+(?:it\s+)?(?:expires?|expiry|expiration))?", re.I)


def parse_inventory(cmd: str, today: date | None = None) -> dict:
    """Parse an 'add to inventory' sentence into name / qty / unit / expiry / lead_days."""
    today = today or date.today()
    text = cmd
    out = {"name": "", "quantity": 1.0, "unit": "item", "expiry": None, "lead_days": None, "category": "general", "no_expiry": False}
    text = _ADD_PREFIX.sub("", text, count=1)
    text = _TARGET_SUFFIX.sub(" ", text)
    # lead days: "remind me 3 days before"
    m = re.search(rf"(?:(?:and\s+)?(?:also\s+)?(?:please\s+)?(?:remind|alert|notify)\s+me\s+)(?:(?P<n>{NUM})\s+days?\s+before|a\s+day\s+before|before)[^,.;]*", text, re.I)
    if m:
        n = m.group("n")
        out["lead_days"] = int(parse_number(n)) if n and parse_number(n) else 1 if "a day" in m.group(0).lower() else None
        text = _cut(text, m.span())
    else:
        m2 = re.search(rf"(?P<n>{NUM})\s+days?\s+before", text, re.I)
        if m2 and parse_number(m2.group("n")):
            out["lead_days"] = int(parse_number(m2.group("n")))
            text = _cut(text, m2.span())
    # expiry
    km = re.search(_EXPIRY_KW, text, re.I)
    if km:
        tail = text[km.end():]
        tail_clean = re.sub(r"^\s*(?:date\s*)?(?:is|will be|on|by|at|in|:|=|-)?\s*", "", tail, flags=re.I)
        offset = len(tail) - len(tail_clean)
        # "expires in 5 days" -> keep the 'in'
        if re.match(rf"\s*(?:in|after)\s+{NUM}\s*(?:days?|weeks?|months?)", tail, re.I):
            tail_clean = tail.lstrip()
            offset = len(tail) - len(tail_clean)
        fd = find_date(tail_clean, today)
        if fd:
            out["expiry"] = fd[0]
            end = km.end() + offset + fd[1][1]
            text = text[:km.start()] + " " + text[end:]
        elif re.search(r"\b(?:no|none|nothing|never)\b", tail, re.I):
            out["no_expiry"] = True
            text = text[:km.start()] + " " + text[km.end():]
        else:
            text = text[:km.start()] + " " + text[km.end():]
    else:
        # allow "milk 2 litres, 25 october" only when explicitly flagged by 'expires' words; otherwise no date
        pass
    text = re.sub(r"[,\s]+(?:and|with|having|that|which|it|its|they|their|this|these|those|both|will|are|is)\s*$", "", text.strip(" ,.;:-"), flags=re.I).strip(" ,.;:-")
    text = re.sub(r"^(?:and|also|with)\s+", "", text, flags=re.I)
    # quantity + unit
    qm = re.match(rf"^(?P<q>{NUM})\s*(?P<u>{_UNIT_RE})?\b\s*(?:of\s+)?(?P<name>.+)$", text, re.I)
    if qm and parse_number(qm.group("q")) is not None and (qm.group("u") or re.match(r"\d", qm.group("q"))):
        out["quantity"] = parse_number(qm.group("q")) or 1.0
        if qm.group("u"):
            out["unit"] = _UNITS[qm.group("u").lower()]
        text = qm.group("name")
    else:
        qm2 = re.match(rf"^(?P<name>.+?)\s+(?P<q>\d+(?:\.\d+)?)\s*(?P<u>{_UNIT_RE})?$", text, re.I)
        if qm2:
            out["quantity"] = float(qm2.group("q"))
            if qm2.group("u"):
                out["unit"] = _UNITS[qm2.group("u").lower()]
            text = qm2.group("name")
    name = re.sub(r"^(?:a|an|the|some|my|one more)\s+", "", text.strip(" ,.;:-"), flags=re.I).strip(" ,.;:-")
    name = re.sub(r"\s+", " ", name)
    out["name"] = name[:1].upper() + name[1:] if name else ""
    out["category"] = guess_category(name)
    return out


def _item_lookup(name: str) -> dict | None:
    if not name:
        return None
    with core.get_db() as conn:
        row = conn.execute("SELECT * FROM inventory WHERE lower(item_name) = lower(?) ORDER BY id DESC LIMIT 1", (name,)).fetchone()
        if not row:
            row = conn.execute("SELECT * FROM inventory WHERE lower(item_name) LIKE lower(?) ORDER BY id DESC LIMIT 1", (f"%{name}%",)).fetchone()
    return dict(row) if row else None


def _delete_item_reminders(item_id: int) -> None:
    with core.get_db() as conn:
        conn.execute("DELETE FROM reminders WHERE status='pending' AND notes=?", (f"inventory:{item_id}",))


def schedule_expiry_reminders(item_id: int, name: str, expiry: date, lead_days: int | None = None, now: datetime | None = None) -> list[datetime]:
    """Create 'about to expire' reminders: N days before (default 2) at 9 AM + on the expiry day at 8 AM."""
    now = now or datetime.now()
    lead = 2 if lead_days is None else max(0, int(lead_days))
    _delete_item_reminders(item_id)
    when_list: list[tuple[datetime, str]] = []
    if lead > 0:
        when_list.append((datetime.combine(expiry - timedelta(days=lead), time(9, 0)),
                          f"Heads up! {name} expires in {lead} day{'s' if lead != 1 else ''} ({expiry.strftime('%d %b')})."))
    when_list.append((datetime.combine(expiry, time(8, 0)), f"Heads up! {name} expires today! Use it or lose it."))
    scheduled: list[datetime] = []
    for due, title in when_list:
        if due > now:
            core.create_reminder(title, due.isoformat(timespec="seconds"), f"inventory:{item_id}")
            scheduled.append(due)
    if not scheduled and expiry >= now.date():
        due = now + timedelta(seconds=30)
        days = (expiry - now.date()).days
        label = "today" if days == 0 else "tomorrow" if days == 1 else f"in {days} days"
        core.create_reminder(f"Heads up! {name} expires {label}.", due.isoformat(timespec="seconds"), f"inventory:{item_id}")
        scheduled.append(due)
    return scheduled


def _expiry_phrase(expiry: date, scheduled: list[datetime]) -> str:
    if expiry < date.today():
        return " Note: that date has already passed, so it's already expired."
    if not scheduled:
        return ""
    first = min(scheduled)
    more = f" (and {len(scheduled) - 1} more)" if len(scheduled) > 1 else ""
    return f" I'll remind you {describe_when(first)}{more}, before it goes bad."


def add_item_with_expiry(parsed: dict) -> dict:
    name = parsed["name"]
    if not name:
        return {"ok": False, "reply": "What item should I add?"}
    expiry = parsed["expiry"]
    res = core.add_inventory_item(name, parsed["quantity"], parsed["unit"], expiry.isoformat() if expiry else "", parsed["category"])
    if not res.get("ok"):
        return {"ok": False, "reply": res.get("message", "I couldn't add that item.")}
    item_id = res["item_id"]
    _remember_item(item_id, name)
    qty = parsed["quantity"]
    if parsed["unit"] == "item":
        qty_txt = f"{qty:g} " if qty != 1 else ""
    else:
        qty_txt = f"{qty:g} {plural_unit(qty, parsed['unit'])} of "
    if expiry:
        sched = schedule_expiry_reminders(item_id, name, expiry, parsed["lead_days"])
        extra = _expiry_phrase(expiry, sched)
        reply = pick([
            f"Added {qty_txt}{name} to your inventory, expiring {pretty_date(expiry)}.{extra}",
            f"Done! {qty_txt}{name} is now in your inventory, expiring {pretty_date(expiry)}.{extra} Food waste, you shall not pass!",
        ]).replace("  ", " ")
        return {"ok": True, "reply": reply, "data": res, "refresh": ["inventory", "reminders"]}
    set_pending("expiry_for_item", item_id=item_id, name=name, lead_days=parsed["lead_days"])
    return {"ok": True, "reply": f"Added {qty_txt}{name} to your inventory. When does it expire? Say a date like “25 October” or “in 5 days”, or say “no expiry”.",
            "data": res, "refresh": ["inventory"]}


def set_item_expiry(item: dict, expiry: date, lead_days: int | None = None) -> dict:
    with core.get_db() as conn:
        conn.execute("UPDATE inventory SET expiry_date=?, updated_at=? WHERE id=?",
                     (expiry.isoformat(), datetime.now().isoformat(timespec="seconds"), item["id"]))
    sched = schedule_expiry_reminders(item["id"], item["item_name"], expiry, lead_days)
    _remember_item(item["id"], item["item_name"])
    extra = _expiry_phrase(expiry, sched)
    return {"ok": True, "reply": f"Got it! {item['item_name']} expires {pretty_date(expiry)}.{extra}".replace("  ", " "),
            "refresh": ["inventory", "reminders"]}


_EXPIRY_UPDATE_RE = re.compile(
    rf"^(?:please\s+)?(?:add|set|update|change|put|enter|save|note|mark)\s+(?:the\s+|an?\s+|its\s+|his\s+|their\s+)?{_EXPIRY_KW}", re.I)


def _handle_expiry_update(cmd: str) -> dict | None:
    today = date.today()
    # "add the expiry for milk is 25 october" / "set expiry of this to tomorrow"
    km = re.search(_EXPIRY_KW, cmd, re.I)
    rest = cmd[km.end():] if km else cmd
    name = ""
    nm = re.match(r"\s*(?:date\s*)?(?:for|of)\s+(?P<name>.+?)\s+(?:is|to|on|will be|=|:|in|after|by)\b", rest, re.I)
    if nm:
        name = nm.group("name")
    name = re.sub(r"^(?:the|my|this|that|it)\b\s*", "", name, flags=re.I).strip()
    if name.lower() in {"this", "that", "it", "item", "this item", ""}:
        name = ""
    fd = find_date(rest, today)
    lead = None
    lm = re.search(rf"(?P<n>{NUM})\s+days?\s+before", cmd, re.I)
    if lm and parse_number(lm.group("n")):
        lead = int(parse_number(lm.group("n")))
    item = _item_lookup(name) if name else None
    if not item and not name and _LAST_ITEM["id"] and _time.time() - _LAST_ITEM["ts"] < 1800:
        with core.get_db() as conn:
            row = conn.execute("SELECT * FROM inventory WHERE id=?", (_LAST_ITEM["id"],)).fetchone()
        item = dict(row) if row else None
    if not fd:
        return {"ok": True, "reply": "I didn't catch the expiry date. Say something like “expiry is 25 October” or “expires in 5 days”."}
    if not item:
        if name:
            return {"ok": True, "reply": f"I couldn't find “{name}” in your inventory. Say “add {name} to inventory” first."}
        return {"ok": True, "reply": "Which item is this for? Say “expiry for milk is 25 October”, or add the item first."}
    return set_item_expiry(item, fd[0], lead)


def _handle_inventory_add(cmd: str) -> dict | None:
    parsed = parse_inventory(cmd)
    if not parsed["name"]:
        set_pending("inventory_name")
        return {"ok": True, "reply": "Sure! What item should I add, and when does it expire?"}
    return add_item_with_expiry(parsed)


def _handle_inventory_remove(cmd: str) -> dict | None:
    m = re.search(r"(?:remove|delete|take out|used up|finished|threw away|thrown)\s+(?:the\s+|my\s+)?(?P<name>.+?)(?:\s+(?:from|in|out of)\s+(?:my\s+|the\s+|your\s+)?(?:inventory|stock|pantry|fridge|list))?$", cmd, re.I)
    if not m:
        return None
    item = _item_lookup(m.group("name").strip(" .!"))
    if not item:
        return {"ok": True, "reply": f"I couldn't find “{m.group('name').strip()}” in your inventory."}
    with core.get_db() as conn:
        conn.execute("DELETE FROM inventory WHERE id=?", (item["id"],))
    _delete_item_reminders(item["id"])
    return {"ok": True, "reply": f"Removed {item['item_name']} from your inventory (and its expiry reminders).", "refresh": ["inventory", "reminders"]}


# ----------------------------------------------------------------------------
# "add this data to your database / memory"
# ----------------------------------------------------------------------------
_DB_RE = re.compile(
    r"^(?:please\s+)?(?:add|save|store|put|write|note|keep|enter|insert|record|log)\b(?P<mid>.*?)\b(?:in|into|to|on|inside)\s+(?:(?:your|ur|my|the)\s+)?(?:data\s*base|database|db|memory|memories|notes?|brain|records?)\b\s*[,:\-]?\s*(?P<content>.*)$", re.I)


def _looks_like_inventory(text: str) -> bool:
    return bool(re.search(_EXPIRY_KW, text, re.I) or re.search(r"\b(?:inventory|stock|pantry|fridge)\b", text, re.I))


def _save_note(content: str) -> dict:
    content = content.strip(" .")
    if not content:
        return {"ok": False, "reply": "I didn't catch what to save."}
    res = core.save_memory(content[:55], content, "voice-note")
    return {"ok": res.get("ok", True), "reply": pick([
        f"Saved! “{content[:80]}” is now in my memory. I'll guard it with my life (and my RAM).",
        f"Done, it's in the database: “{content[:80]}”. Nothing escapes my memory. Except where I put my keys. I have no keys.",
    ]), "data": res, "refresh": ["memories"]}


_SAVE_THIS_RE = re.compile(r"^(?:please\s+)?(?:save|store)\s+(?:this|that|it)\b\s*(?:data|info|information|note)?\s*[:,\-]?\s*(?P<content>.*)$", re.I)


def _handle_save_this(cmd: str) -> dict | None:
    """'save this: X' / 'store this X' -> always written to the local SQL database."""
    m = _SAVE_THIS_RE.match(cmd)
    if not m or re.search(r"\b(?:password|passcode|pin|otp|location|number)\b", cmd, re.I):
        return None
    content = m.group("content").strip()
    if content and _looks_like_inventory(content):
        return _handle_inventory_add(content)
    if content:
        return _save_note(content)
    set_pending("data_entry")
    return {"ok": True, "reply": "Sure, what should I store in the database?"}


def _handle_database(cmd: str) -> dict | None:
    m = _DB_RE.match(cmd)
    if not m:
        return None
    mid, content = m.group("mid").strip(), m.group("content").strip()
    content = re.sub(r"^(?:that|this|data|the following)\s*[:,\-]?\s*", "", content, flags=re.I)
    full = (mid + " " + content).strip()
    inventory_mid = bool(re.search(r"\b(?:inventory|stock|pantry|item|items)\b", mid, re.I))
    if (inventory_mid or re.search(_EXPIRY_KW, full, re.I)) and (content or re.search(_EXPIRY_KW, mid, re.I)):
        return _handle_inventory_add(content or mid)
    if content:
        return _save_note(content)
    set_pending("data_entry")
    return {"ok": True, "reply": "Ready! Tell me what to store: a note, or an item with its expiry date (like “milk, expires 30 October”)."}


# ----------------------------------------------------------------------------
# Music + open websites
# ----------------------------------------------------------------------------
SITES = {
    "youtube": "https://www.youtube.com/", "you tube": "https://www.youtube.com/",
    "spotify": "https://open.spotify.com/", "google": "https://www.google.com/",
    "gmail": "https://mail.google.com/", "whatsapp": "https://web.whatsapp.com/",
    "instagram": "https://www.instagram.com/", "facebook": "https://www.facebook.com/",
    "twitter": "https://x.com/", "x": "https://x.com/", "linkedin": "https://www.linkedin.com/",
    "github": "https://github.com/", "maps": "https://www.google.com/maps", "google maps": "https://www.google.com/maps",
    "netflix": "https://www.netflix.com/", "amazon": "https://www.amazon.in/", "flipkart": "https://www.flipkart.com/",
    "chatgpt": "https://chatgpt.com/", "claude": "https://claude.ai/", "gemini": "https://gemini.google.com/",
    "drive": "https://drive.google.com/", "google drive": "https://drive.google.com/",
    "calendar": "https://calendar.google.com/", "google calendar": "https://calendar.google.com/",
    "hotstar": "https://www.hotstar.com/", "prime video": "https://www.primevideo.com/",
    "wikipedia": "https://www.wikipedia.org/", "news": "https://news.google.com/",
    "youtube music": "https://music.youtube.com/", "gaana": "https://gaana.com/", "jiosaavn": "https://www.jiosaavn.com/",
}
SITES.update({
    "yt": "https://www.youtube.com/", "u tube": "https://www.youtube.com/", "utube": "https://www.youtube.com/", "yutube": "https://www.youtube.com/",
    "insta": "https://www.instagram.com/", "fb": "https://www.facebook.com/", "whats app": "https://web.whatsapp.com/",
    "mail": "https://mail.google.com/", "email": "https://mail.google.com/", "g mail": "https://mail.google.com/",
    "stack overflow": "https://stackoverflow.com/", "stackoverflow": "https://stackoverflow.com/", "reddit": "https://www.reddit.com/",
    "twitter x": "https://x.com/", "prime": "https://www.primevideo.com/", "disney hotstar": "https://www.hotstar.com/",
    "amazon prime": "https://www.primevideo.com/", "pinterest": "https://www.pinterest.com/", "zoom": "https://zoom.us/",
    "google meet": "https://meet.google.com/", "meet": "https://meet.google.com/", "google docs": "https://docs.google.com/",
    "docs": "https://docs.google.com/", "google sheets": "https://sheets.google.com/", "translate": "https://translate.google.com/",
    "google translate": "https://translate.google.com/", "outlook": "https://outlook.live.com/", "vit": "https://vit.ac.in/",
    "vtop": "https://vtop.vit.ac.in/", "leetcode": "https://leetcode.com/", "hackerrank": "https://www.hackerrank.com/",
    "geeksforgeeks": "https://www.geeksforgeeks.org/", "w3schools": "https://www.w3schools.com/", "bing": "https://www.bing.com/",
})
_PLATFORM_RE = r"(?:spotify|youtube\s*music|you\s*tube|youtube|yt|jio\s*saavn|saavn|gaana|wynk|apple\s*music|amazon\s*music)"
_YT_CACHE: dict[str, tuple[str, str]] = {}


def youtube_lookup(query: str, timeout: int = 8) -> tuple[str, str] | None:
    """Return (video_id, title) of the first YouTube search result, or None."""
    key = query.strip().lower()
    if key in _YT_CACHE:
        return _YT_CACHE[key]
    for params in ("&sp=EgIQAQ%3D%3D", ""):
        url = "https://www.youtube.com/results?search_query=" + quote_plus(query) + params
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9", "Cookie": "CONSENT=YES+1; SOCS=CAI"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                html = resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError):
            continue
        m = re.search(r'"videoRenderer":\{"videoId":"([A-Za-z0-9_-]{11})"', html)
        if not m:
            continue
        title = query
        tm = re.search(r'"title":\{"runs":\[\{"text":"((?:[^"\\]|\\.)*)"', html[m.end(): m.end() + 3000])
        if tm:
            try:
                title = json.loads('"' + tm.group(1) + '"')
            except ValueError:
                title = tm.group(1)
        _YT_CACHE[key] = (m.group(1), title)
        return _YT_CACHE[key]
    return None


_TLD_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\.(?:com|in|org|net|edu|io|co|ac\.in|gov|gov\.in|app|dev|ai|me|tv|info|xyz|us|uk)(?:/\S*)?$")


def resolve_site(spoken: str) -> tuple[str, str] | None:
    """Turn a spoken site name into (label, url). Known names, spoken domains ("github dot com"), else a lucky web search."""
    name = re.sub(r"\b(?:the|website|web\s*site|site|app|application|page|please|for me|in (?:a )?new tab|on (?:the )?(?:browser|chrome|web))\b", " ", spoken.lower())
    name = re.sub(r"\s+", " ", name).strip(" .,!?")
    name = re.sub(r"\s*\bdot\b\s*", ".", name)
    name = re.sub(r"^(?:https?://)?(?:www\.)?", "", name)
    if not name:
        return None
    if name in SITES:
        return name, SITES[name]
    squashed = name.replace(" ", "")
    if squashed in SITES:
        return name, SITES[squashed]
    if _TLD_RE.match(squashed):
        return squashed, "https://" + squashed
    if re.fullmatch(r"[a-z0-9]+", squashed) and len(name.split()) == 1:
        return name, "https://www." + squashed + ".com"
    return name, "https://duckduckgo.com/?q=" + quote_plus("!ducky " + name)


def _spotify_urls(q: str) -> tuple[str, str]:
    return f"spotify:search:{quote_plus(q).replace('+', '%20')}", f"https://open.spotify.com/search/{quote_plus(q).replace('+', '%20')}"


_PLAY_RE = re.compile(
    rf"^(?:play|put\s+on|start\s+playing|listen\s+to|sing|stream)\s+(?:me\s+|us\s+)?(?:the\s+|a\s+|some\s+)?(?:(?:song|songs|music|track|video|tune|gaana|gana)\s+)?(?P<q>.*)$", re.I)
_PLAY_HINDI_RE = re.compile(r"^(?P<q>.+?)\s+(?:play\s+karo|play\s+kar|chalao|bajao|baja\s+do|laga\s+do|lagao|chala\s+do|sunao|play\s+kardo)\b.*$", re.I)


def _handle_music(cmd: str) -> dict | None:
    low = cmd.lower().strip(" .!?")
    # controls
    if re.fullmatch(r"(?:stop|pause|mute|quit)(?:\s+(?:the|my|this|that|current))?\s*(?:music|song|songs|playing|playback|video|gaana|gana|it|playing music)?", low) \
            or re.fullmatch(r"(?:music|song|gaana|gana|video)\s+(?:band\s+karo|bandh\s+karo|stop|pause)", low) \
            or re.fullmatch(r"(?:stop|band\s+karo)\s+(?:the\s+)?(?:music|song|gaana)", low):
        verb = "pause" if re.match(r"pause", low) or low.endswith("pause") else "stop"
        return {"ok": True, "reply": pick(["Music off. The silence is deafening!", "Okay, shushing the music now.", "Done! Back to the sound of... nothing."]),
                "action": {"type": "music_" + verb}}
    if re.fullmatch(r"(?:resume|unpause|continue)(?:\s+(?:the|my))?(?:\s+(?:music|song|songs|playing|playback|video))?", low):
        return {"ok": True, "reply": "Resuming the music. Let the party continue!", "action": {"type": "music_resume"}}
    if re.search(r"\b(?:volume|louder|quieter|softer|turn it (?:up|down)|sound)\b", low) and re.search(r"\b(?:up|down|louder|quieter|softer|increase|decrease|raise|lower|max|full|low|mute)\b", low):
        if re.search(r"\b(?:down|quieter|softer|decrease|lower|low)\b", low):
            return {"ok": True, "reply": "Turning it down a notch.", "action": {"type": "music_volume", "delta": -25}}
        return {"ok": True, "reply": "Turning it up! Neighbours, brace yourselves.", "action": {"type": "music_volume", "delta": 25}}
    # "open youtube and play <song>"  /  "open yt and play <song>"
    cm = re.match(r"^(?:open|launch|go\s+to|start|kholo|khol\s+do)\s+(?:the\s+)?(?P<site>[a-z0-9 .]+?)\s+(?:and|then|aur|&)\s+(?P<rest>(?:play|search|sing|put\s+on|stream)\b.*)$", low)
    if cm and re.fullmatch(r"(?:you\s*tube|yt|u\s*tube|yutube|youtube\s+music)", cm.group("site").strip()):
        rest = cm.group("rest")
        if re.match(r"^search\b", rest):
            sq = re.sub(r"^search\s+(?:for\s+)?", "", rest).strip()
            url = "https://www.youtube.com/results?search_query=" + quote_plus(sq)
            return {"ok": True, "reply": f"Searching YouTube for “{sq}”.", "open_url": url, "action": {"type": "open_url", "url": url}}
        return _handle_music(rest if re.search(r"\byoutube\b|\byt\b", rest) else rest + " on youtube")
    # open <anything>: known sites, spoken domains ("github dot com"), or the best web match
    om = re.match(r"^(?:open|launch|go\s+to|visit|start|kholo|khol\s+do|open\s+up)\s+(?P<site>.+?)(?:\s+(?:please|kholo|khol\s+do))?$", low)
    if om:
        res = resolve_site(om.group("site"))
        if res:
            label, url = res
            return {"ok": True, "reply": pick([f"Opening {label}!", f"{label.title()}, coming right up!"]), "speech": f"Opening {label}.",
                    "open_url": url, "action": {"type": "open_url", "url": url}}
    if re.match(r"^(?:search|google|look\s+up)\b", low):
        sm = re.match(r"^(?:search|google|look\s+up)\s+(?:for\s+|on\s+google\s+for\s+)?(?:on\s+)?(?P<q>.+)$", cmd, re.I)
        if sm and sm.group("q").strip():
            q = sm.group("q").strip(" .?!")
            yt = re.match(r"^(?:on\s+)?youtube\s+(?:for\s+)?(?P<q>.+)$", q, re.I)
            url = ("https://www.youtube.com/results?search_query=" + quote_plus(yt.group("q"))) if yt else ("https://www.google.com/search?q=" + quote_plus(q))
            return {"ok": True, "reply": f"Searching for “{(yt.group('q') if yt else q)}”. Fingers crossed the internet knows!", "open_url": url, "action": {"type": "open_url", "url": url}}
    # play
    hm = _PLAY_HINDI_RE.match(cmd)
    pm = _PLAY_RE.match(cmd)
    q = None
    if pm:
        q = pm.group("q")
    elif hm:
        q = hm.group("q")
    elif re.match(r"^gaana\s+(?:bajao|chalao|lagao)$", low):
        q = ""
    if q is None:
        return None
    q = q.strip(" .!?")
    platform = ""
    plm = re.search(rf"\s*\b(?:on|in|using|via|with|through|par|pe)\s+(?P<p>{_PLATFORM_RE})\b\s*$", q, re.I)
    if plm:
        platform = re.sub(r"\s+", "", plm.group("p").lower())
        platform = "youtube" if platform in {"yt", "utube"} else platform
        q = q[:plm.start()].strip(" ,.")
    if re.match(r"^(?:spotify|youtube|you tube|youtube music)$", q, re.I) and not platform:
        platform, q = re.sub(r"\s+", "", q.lower()), ""
    q = re.sub(r"^(?:the\s+)?(?:song|songs|music|track|video)\s+", "", q, flags=re.I).strip()
    if re.fullmatch(r"(?:some\s+)?(?:music|song|songs|something|anything|gaana|a song)?", q, re.I):
        q = "top hit songs"
        generic = True
    else:
        generic = False
    if not q:
        q = "top hit songs"
    if platform == "spotify":
        uri, web = _spotify_urls(q)
        return {"ok": True, "reply": pick([f"Opening Spotify with “{q}”. Spotify doesn't let apps press play for you, so tap the green button and enjoy!",
                                           f"Spotify is on its way with “{q}”. One tap on play and you're dancing!"]),
                "open_url": uri, "fallback_url": web, "action": {"type": "open_url", "url": web}}
    if platform in {"saavn", "jiosaavn", "gaana", "wynk", "applemusic", "amazonmusic"}:
        urls = {"saavn": "https://www.jiosaavn.com/search/", "jiosaavn": "https://www.jiosaavn.com/search/", "gaana": "https://gaana.com/search/",
                "wynk": "https://wynk.in/music/search?q=", "applemusic": "https://music.apple.com/search?term=", "amazonmusic": "https://music.amazon.in/search/"}
        url = urls[platform] + quote_plus(q)
        return {"ok": True, "reply": f"Opening {platform} for “{q}”.", "open_url": url, "action": {"type": "open_url", "url": url}}
    # Default / YouTube: play inside the app
    found = youtube_lookup(q + (" official audio" if not generic and platform != "youtube" and len(q.split()) <= 6 and "official" not in q.lower() and "video" not in q.lower() else ""))
    if found:
        vid, title = found
        reply = pick([f"Playing “{title}”. Hit it, maestro!", f"Now playing: {title}. Sing along, I won't judge. Much.",
                      f"Cranking up “{title}”. Time to dance like nobody's watching!"])
        return {"ok": True, "reply": reply, "speech": f"Playing {title}. Enjoy!", "action": {"type": "play_youtube", "video_id": vid, "title": title, "query": q}}
    url = "https://www.youtube.com/results?search_query=" + quote_plus(q)
    return {"ok": True, "reply": f"I couldn't reach YouTube to start “{q}” directly, so I'm opening the search results for you. Just click the first video!",
            "open_url": url, "action": {"type": "open_url", "url": url}}


# ----------------------------------------------------------------------------
# Pending-conversation follow-ups
# ----------------------------------------------------------------------------
_CANCEL_WORDS = re.compile(r"^(?:cancel|never\s*mind|forget it|stop|no thanks?|leave it|skip)[.! ]*$", re.I)


def _handle_pending(cmd: str) -> dict | None:
    kind, data = get_pending()
    if not kind:
        return None
    if _CANCEL_WORDS.match(cmd.strip()) and kind != "expiry_for_item":
        clear_pending()
        return {"ok": True, "reply": "Okay, cancelled. No harm done!"}
    now = datetime.now()
    if kind == "reminder_time":
        w = parse_when(cmd, now)
        if w.due:
            clear_pending()
            title = data.get("title") or w.rest
            if not title:
                return None
            if w.due <= now:
                w.due = now + timedelta(seconds=5)
            res = _create_reminder(title, w.due, "Created by voice/chat command.")
            reply, speech = _reminder_confirmation(title, data.get("connector", "to"), w.due, data.get("kind", "reminder"))
            return {"ok": res.get("ok", False), "reply": reply, "speech": speech, "data": res, "refresh": ["reminders"]}
        clear_pending()
        return None
    if kind == "reminder_title":
        clear_pending()
        title = cmd.strip(" .")
        title = re.sub(r"^(?:to|about|that)\s+", "", title, flags=re.I)
        due = datetime.fromisoformat(data["due"])
        res = _create_reminder(title, due, "Created by voice/chat command.")
        reply, speech = _reminder_confirmation(title, data.get("connector", "to"), due, data.get("kind", "reminder"))
        return {"ok": res.get("ok", False), "reply": reply, "speech": speech, "data": res, "refresh": ["reminders"]}
    if kind == "reminder_both":
        w = parse_when(cmd, now)
        title, connector, k = _extract_reminder_title(w.rest or cmd)
        if w.due and title:
            clear_pending()
            res = _create_reminder(title, w.due, "Created by voice/chat command.")
            reply, speech = _reminder_confirmation(title, connector, w.due, data.get("kind", "reminder"))
            return {"ok": res.get("ok", False), "reply": reply, "speech": speech, "data": res, "refresh": ["reminders"]}
        clear_pending()
        return None
    if kind == "expiry_for_item":
        item_id = data["item_id"]
        if re.search(r"\b(?:no|none|nothing|never|skip|doesn'?t|does not)\b", cmd, re.I) and not find_date(cmd):
            clear_pending()
            return {"ok": True, "reply": f"No problem, {data['name']} has no expiry date."}
        fd = find_date(cmd, date.today())
        if fd:
            clear_pending()
            with core.get_db() as conn:
                row = conn.execute("SELECT * FROM inventory WHERE id=?", (item_id,)).fetchone()
            if row:
                lm = re.search(rf"(?P<n>{NUM})\s+days?\s+before", cmd, re.I)
                lead = int(parse_number(lm.group("n"))) if lm and parse_number(lm.group("n")) else data.get("lead_days")
                return set_item_expiry(dict(row), fd[0], lead)
        clear_pending()
        return None
    if kind == "inventory_name":
        clear_pending()
        return _handle_inventory_add(cmd)
    if kind == "data_entry":
        clear_pending()
        if _looks_like_inventory(cmd):
            return _handle_inventory_add(cmd)
        return _save_note(cmd)
    clear_pending()
    return None


def _handle_expiry_sentence(cmd: str, low: str) -> dict | None:
    """'milk expires on 5 november' -> update the item if it exists, else add it. 'add 2 bread expiring tomorrow' -> add."""
    parsed = parse_inventory(cmd)
    if not parsed["name"]:
        return None
    add_verb = bool(_ADD_PREFIX.match(cmd))
    existing = _item_lookup(parsed["name"])
    if existing and parsed["expiry"] and not add_verb:
        return set_item_expiry(existing, parsed["expiry"], parsed["lead_days"])
    if parsed["expiry"] or add_verb:
        return add_item_with_expiry(parsed)
    return None


# ----------------------------------------------------------------------------
# Public entry point
# ----------------------------------------------------------------------------
def handle(text: str) -> dict | None:
    """Return a chat response dict, or None if the sentence is not a smart command."""
    cmd = clean_command(text)
    if not cmd:
        return None
    low = cmd.lower()

    pending = _handle_pending(cmd)
    if pending is not None:
        return pending

    r = _handle_save_this(cmd)
    if r is not None:
        return r

    # --- music / sites
    if re.match(r"^(?:play|put on|start playing|listen to|sing|stream|stop|pause|resume|unpause|continue|mute|open|launch|go to|kholo|search|google|look up|volume|turn|gaana|music|song)\b", low) \
            or _PLAY_HINDI_RE.match(cmd) or re.search(r"\b(?:louder|quieter|volume (?:up|down))\b", low):
        r = _handle_music(cmd)
        if r is not None:
            return r

    if _EXPIRY_UPDATE_RE.match(cmd) or re.match(rf"^(?:the\s+)?{_EXPIRY_KW}\s+(?:for|of)\b", cmd, re.I):
        return _handle_expiry_update(cmd)
    starts_remind = bool(re.match(r"^(?:remind|set\s+(?:an?\s+)?(?:reminder|alarm|timer)|add\s+(?:an?\s+)?reminder|wake\s+me|yaad)", low))
    if not starts_remind and re.search(_EXPIRY_KW, low) and not _CANCEL_REM_RE.search(low) and not _LIST_REM_RE.search(low):
        inv = _handle_expiry_sentence(cmd, low)
        if inv is not None:
            return inv

    # --- reminders
    if _CANCEL_REM_RE.search(low):
        return _cancel_reminders(cmd)
    if _LIST_REM_RE.search(low) and not re.search(r"\bremind\s+me\b", low):
        return _list_reminders()
    if _REMIND_RE.search(low):
        r = _handle_reminder(cmd)
        if r is not None:
            return r

    # --- inventory / expiry
    if _DB_RE.match(cmd):
        r = _handle_database(cmd)
        if r is not None:
            return r
    if re.match(r"^(?:remove|delete|take out|used up|finished|threw away)\b", low) and re.search(r"\b(?:inventory|stock|pantry|fridge)\b", low):
        r = _handle_inventory_remove(cmd)
        if r is not None:
            return r
    if re.match(r"^(?:add|put|store|insert|log|i\s+(?:just\s+)?(?:bought|got|purchased)|bought|purchased)\b", low) and \
            re.search(r"\b(?:inventory|stock|pantry|fridge)\b|\bi\s+(?:just\s+)?(?:bought|got|purchased)\b", low):
        return _handle_inventory_add(cmd)
    return None
