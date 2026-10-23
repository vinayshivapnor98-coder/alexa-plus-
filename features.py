"""Personal-context features for Alexa+: everything here is stored per person (see server.CURRENT_PERSON).

  * study plan built from the person's own history (when they study, how long, which subjects lag)
  * live location (sent by the browser) + "open map", "how far is X", directions
  * ride booking shortcuts (Uber / Ola / Rapido deep links - the person confirms in the app)
  * WhatsApp call / message shortcuts using saved contacts
  * order tracking for Amazon / Flipkart (orders the person tells us about + a shortcut to the retailer's page)
  * "serious" notifications (overdue work, expiring food, urgent visitors, high alerts, deliveries today)
  * switching person by voice ("switch to Rahul")

Limits (by design - no public API exists): apps cannot press "Confirm ride", start a WhatsApp call, or read
Amazon/Flipkart accounts. We open the right screen and keep our own record so nothing starts from zero.
"""
from __future__ import annotations

import json
import math
import re
import sqlite3
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import date, datetime, timedelta

import os
import server as core
import smart

UA = "AlexaPlusPersonalAgent/1.0 (local hackathon prototype)"
DEFAULT_CC = (os.getenv("DEFAULT_COUNTRY_CODE") or "91").strip().lstrip("+")


# ----------------------------------------------------------------------------
# storage
# ----------------------------------------------------------------------------
def _ensure(path) -> None:
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS locations (
            id INTEGER PRIMARY KEY AUTOINCREMENT, lat REAL NOT NULL, lng REAL NOT NULL,
            accuracy REAL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS places (
            name TEXT PRIMARY KEY, lat REAL NOT NULL, lng REAL NOT NULL, label TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS contacts (
            name TEXT PRIMARY KEY, phone TEXT NOT NULL, created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS notified (
            key TEXT PRIMARY KEY, created_at TEXT NOT NULL
        );
        """)


core.register_init_hook(_ensure)
_ensure(core.person_db_path())


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _action_result(reply: str, speech: str | None = None, **extra) -> dict:
    out = {"ok": True, "reply": reply}
    if speech:
        out["speech"] = speech
    out.update(extra)
    return out


# ----------------------------------------------------------------------------
# location
# ----------------------------------------------------------------------------
def save_location(lat: float, lng: float, accuracy: float | None = None) -> None:
    with core.get_db() as db:
        db.execute("INSERT INTO locations(lat,lng,accuracy,created_at) VALUES(?,?,?,?)", (lat, lng, accuracy, _now()))
        db.execute("DELETE FROM locations WHERE id NOT IN (SELECT id FROM locations ORDER BY id DESC LIMIT 300)")


def last_location() -> dict | None:
    with core.get_db() as db:
        row = db.execute("SELECT * FROM locations ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    d = dict(row)
    try:
        d["age_minutes"] = (datetime.now() - datetime.fromisoformat(d["created_at"])).total_seconds() / 60
    except ValueError:
        d["age_minutes"] = 0
    return d


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    r = 6371.0
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dphi, dl = p2 - p1, math.radians(b[1] - a[1])
    h = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(h))


def _http_json(url: str, timeout: int = 8):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return None


_GEO_CACHE: dict[str, dict | None] = {}


def geocode(query: str, near: tuple[float, float] | None = None) -> dict | None:
    key = (query.strip().lower(), tuple(round(x, 1) for x in near) if near else None)
    if key in _GEO_CACHE:
        return _GEO_CACHE[key]
    url = "https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&q=" + urllib.parse.quote_plus(query)
    if near:
        lat, lng = near
        url += f"&viewbox={lng-0.6},{lat+0.6},{lng+0.6},{lat-0.6}"
    data = _http_json(url)
    res = None
    if data:
        res = {"lat": float(data[0]["lat"]), "lng": float(data[0]["lon"]), "label": data[0].get("display_name", query)}
    _GEO_CACHE[key] = res
    return res


def reverse_geocode(lat: float, lng: float) -> str:
    data = _http_json(f"https://nominatim.openstreetmap.org/reverse?format=jsonv2&zoom=16&lat={lat}&lon={lng}")
    if data and data.get("display_name"):
        return ", ".join(data["display_name"].split(", ")[:3])
    return f"{lat:.4f}, {lng:.4f}"


def route(a: tuple[float, float], b: tuple[float, float]) -> dict:
    """Road distance + time via OSRM; falls back to straight-line distance."""
    data = _http_json(f"https://router.project-osrm.org/route/v1/driving/{a[1]},{a[0]};{b[1]},{b[0]}?overview=false")
    try:
        r = data["routes"][0]
        return {"km": r["distance"] / 1000, "minutes": r["duration"] / 60, "road": True}
    except (TypeError, KeyError, IndexError):
        km = haversine_km(a, b)
        return {"km": km, "minutes": km / 25 * 60, "road": False}


def _fmt_km(km: float) -> str:
    return f"{km * 1000:.0f} metres" if km < 1 else f"{km:.1f} km"


def _fmt_min(mins: float) -> str:
    mins = int(round(mins))
    if mins < 60:
        return f"{max(mins, 1)} minutes"
    h, m = divmod(mins, 60)
    return f"{h} hour{'s' if h != 1 else ''}" + (f" {m} minutes" if m else "")


def _get_place(name: str) -> dict | None:
    with core.get_db() as db:
        row = db.execute("SELECT * FROM places WHERE name = ?", (name.strip().lower(),)).fetchone()
    return dict(row) if row else None


def _set_place(name: str, lat: float, lng: float, label: str = "") -> None:
    with core.get_db() as db:
        db.execute("INSERT OR REPLACE INTO places(name,lat,lng,label,created_at) VALUES(?,?,?,?,?)", (name.strip().lower(), lat, lng, label, _now()))


_NO_LOC = ("I can't see your location yet. Allow location access in your browser (click the lock icon next to the address bar), "
           "keep this tab open, and ask again.")


def _resolve_destination(text: str, here: dict) -> dict | None:
    t = text.strip(" .?!").lower()
    if t in {"this", "that", "it", "there", "this place", "that place", "here"}:
        p = _get_place("_last")
        return p
    saved = _get_place(t) or _get_place(re.sub(r"^(?:my|the)\s+", "", t))
    if saved:
        return saved
    g = geocode(text, near=(here["lat"], here["lng"]))
    if g:
        _set_place("_last", g["lat"], g["lng"], text.strip(" .?!"))
        g["label"] = text.strip(" .?!")
    return g


def _maps_dir_url(here: dict, dest: dict) -> str:
    return ("https://www.google.com/maps/dir/?api=1&origin=" + f"{here['lat']},{here['lng']}" +
            "&destination=" + f"{dest['lat']},{dest['lng']}" + "&travelmode=driving")


def _distance_answer(dest_text: str, open_map: bool, here: dict | None = None) -> dict:
    here = here or last_location()
    if not here:
        return _action_result(_NO_LOC, action={"type": "request_location"})
    dest = _resolve_destination(dest_text, here)
    if not dest:
        if dest_text.strip(" .?!").lower() in {"this", "that", "it", "there", "this place", "that place"}:
            return _action_result("Which place do you mean? Say, for example, “how far is Phoenix Mall”.")
        return _action_result(f"I couldn't find “{dest_text.strip()}” on the map. Try adding the city, like “{dest_text.strip()} Pune”.")
    r = route((here["lat"], here["lng"]), (dest["lat"], dest["lng"]))
    name = dest.get("label") or dest_text
    how = "by road" if r["road"] else "in a straight line (road routing is offline)"
    reply = f"{name} is {_fmt_km(r['km'])} away {how}, about {_fmt_min(r['minutes'])} by car."
    out = _action_result(reply, speech=reply)
    if open_map:
        url = _maps_dir_url(here, dest)
        out.update({"open_url": url, "action": {"type": "open_url", "url": url}})
        out["reply"] = reply + " Opening the map."
    return out


def _handle_location(cmd: str, low: str) -> dict | None:
    # save a place
    m = re.match(r"^(?:save|set|remember|mark)\s+(?:my\s+|this\s+)?(?:current\s+)?(?:location|place|position|spot|here)?\s*(?:as|is)\s+(?:my\s+)?(?P<n>[a-z][a-z0-9 ]{1,30})$", low) \
        or re.match(r"^(?:this\s+is|i\s+am\s+at|i'm\s+at)\s+(?:my\s+)?(?P<n>home|hostel|college|office|work|gym|library|university)$", low) \
        or re.match(r"^(?:my\s+)?(?P<n>home|hostel|college|office|work)\s+is\s+here$", low)
    if m:
        here = last_location()
        if not here:
            return _action_result(_NO_LOC, action={"type": "request_location"})
        name = m.group("n").strip()
        _set_place(name, here["lat"], here["lng"], name)
        return _action_result(f"Saved your current location as “{name}”. Ask “how far is {name}” any time.")
    if re.match(r"^(?:where\s+am\s+i|what(?:'s|\s+is)\s+my\s+(?:current\s+)?location|my\s+location|share\s+my\s+location)$", low):
        here = last_location()
        if not here:
            return _action_result(_NO_LOC, action={"type": "request_location"})
        addr = reverse_geocode(here["lat"], here["lng"])
        return _action_result(f"You're near {addr}.", speech=f"You are near {addr}.")
    # open map [for X]
    m = re.match(r"^(?:open|show|launch|start)\s+(?:the\s+|google\s+)*(?:maps?|navigation)(?:\s+(?:for|to|of|towards)\s+(?P<d>.+))?$", low) \
        or re.match(r"^(?:navigate|directions?|route|take\s+me|guide\s+me)\s+(?:me\s+)?(?:to|till|towards)\s+(?P<d>.+)$", low) \
        or re.match(r"^(?:give\s+me\s+|show\s+me\s+)(?:the\s+)?(?:directions?|route|way)\s+(?:to|for)\s+(?P<d>.+)$", low)
    if m:
        d = (m.groupdict().get("d") or "").strip()
        if d:
            return _distance_answer(d, open_map=True)
        here = last_location()
        if not here:
            url = "https://www.google.com/maps"
            return _action_result("Opening the map. " + _NO_LOC, open_url=url, action={"type": "open_url", "url": url})
        url = f"https://www.google.com/maps/search/?api=1&query={here['lat']},{here['lng']}"
        addr = reverse_geocode(here["lat"], here["lng"])
        return _action_result(f"Opening the map at your location, near {addr}. Tell me a place and I'll say how far it is.",
                              speech=f"Opening the map. You are near {addr}. Tell me a place and I'll tell you how far it is.",
                              open_url=url, action={"type": "open_url", "url": url})
    # how far / distance
    m = re.match(r"^(?:how\s+far(?:\s+is|\s+are)?|how\s+long\s+(?:to\s+(?:reach|get\s+to|go\s+to)|will\s+it\s+take\s+to\s+(?:reach|get\s+to))|distance\s+(?:to|of|between\s+me\s+and)|how\s+much\s+time\s+(?:to\s+reach|for)|eta\s+(?:to|for)|time\s+to\s+reach|kitni\s+door\s+hai)\s*(?P<d>.+)$", low) \
        or re.match(r"^(?P<d>.+?)\s+(?:kitni\s+door\s+hai|kitna\s+door\s+hai|how\s+far)$", low)
    if m:
        d = re.sub(r"^(?:is|the|from here to|from here)\s+", "", m.group("d").strip()).strip()
        d = re.sub(r"\s+(?:from here|from me|from my location)$", "", d)
        return _distance_answer(d, open_map=False)
    return None


# ----------------------------------------------------------------------------
# rides
# ----------------------------------------------------------------------------
def _handle_ride(cmd: str, low: str) -> dict | None:
    if not re.search(r"\b(?:ride|cab|taxi|uber|ola|rapido|auto\s*rickshaw|auto|bike\s+taxi)\b", low):
        return None
    if not re.match(r"^(?:please\s+)?(?:book|get|order|call|arrange|hail|find|need|want|i\s+need|i\s+want|can\s+you\s+book|call\s+me|send|open|launch|start|select|set|use)\b", low):
        return None
    provider = "uber"
    if "ola" in re.findall(r"[a-z]+", low):
        provider = "ola"
    elif "rapido" in low:
        provider = "rapido"
    m = re.search(r"\bdrop(?:[\s\-]*(?:off|up|point))?(?:\s+location)?\s*(?:is|at|to|as|:|=)?\s+(?P<d>.+)$", low) \
        or re.search(r"\b(?:to|for|towards|till)\s+(?P<d>.+)$", low)
    dest_text = m.group("d").strip() if m else ""
    dest_text = re.sub(r"^(?:the\s+|at\s+|is\s+)+", "", dest_text)
    dest_text = re.sub(r"\s+(?:on|using|via|with)\s+(?:uber|ola|rapido)$", "", dest_text).strip()
    dest_text = re.sub(r"\s+(?:please|now)$", "", dest_text)
    here = last_location()
    if not dest_text:
        urls = {"uber": "https://m.uber.com/", "ola": "https://book.olacabs.com/", "rapido": "https://www.rapido.bike/"}
        return _action_result(f"Opening {provider.title()}. Tell me where to — say “book a ride to <place>” — and I'll set the drop-off for you.",
                              open_url=urls[provider], action={"type": "open_url", "url": urls[provider]})
    if not here:
        return _action_result("I need your live location as the pickup point. " + _NO_LOC, action={"type": "request_location"})
    dest = _resolve_destination(dest_text, here)
    if not dest:
        return _action_result(f"I couldn't find “{dest_text}”. Try the full place name with the city.")
    q = urllib.parse.quote
    nick = q(dest.get("label") or dest_text)
    if provider == "uber":
        url = (f"https://m.uber.com/ul/?action=setPickup&pickup=my_location&dropoff[latitude]={dest['lat']}"
               f"&dropoff[longitude]={dest['lng']}&dropoff[nickname]={nick}")
    elif provider == "ola":
        url = (f"https://book.olacabs.com/?lat={here['lat']}&lng={here['lng']}&drop_lat={dest['lat']}&drop_lng={dest['lng']}"
               f"&drop_name={nick}&pickup_name=My%20location")
    else:
        url = "https://www.rapido.bike/"
    r = route((here["lat"], here["lng"]), (dest["lat"], dest["lng"]))
    info = f"It's about {_fmt_km(r['km'])}, around {_fmt_min(r['minutes'])}."
    reply = (f"Opening {provider.title()} with pickup at your location and drop at {dest.get('label') or dest_text}. {info} "
             f"For safety and payment, you confirm the fare and tap “Book” in the app yourself.")
    return _action_result(reply, speech=f"Opening {provider.title()} to {dest.get('label') or dest_text}. {info} Please confirm the booking in the app.",
                          open_url=url, action={"type": "open_url", "url": url})


# ----------------------------------------------------------------------------
# contacts + WhatsApp
# ----------------------------------------------------------------------------
_PHONE_RE = re.compile(r"(\+?\d[\d \-]{7,15}\d)")


def _norm_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if raw.strip().startswith("+"):
        return digits
    digits = digits.lstrip("0")
    return digits if len(digits) > 10 else DEFAULT_CC + digits


def _find_contact(name: str) -> tuple[str, str] | None:
    n = re.sub(r"[^a-z0-9 ]", "", name.lower()).strip()
    n = re.sub(r"^(?:my|the)\s+", "", n)
    if not n:
        return None
    with core.get_db() as db:
        rows = db.execute("SELECT name, phone FROM contacts").fetchall()
    for r in rows:
        if r["name"] == n:
            return r["name"], r["phone"]
    for r in rows:
        if n in r["name"] or r["name"] in n:
            return r["name"], r["phone"]
    return None


def _handle_contacts(cmd: str, low: str) -> dict | None:
    if re.match(r"^(?:save|add|store|remember|update)\b", low) and _PHONE_RE.search(cmd) and re.search(r"\b(?:number|contact|phone|mobile|no)\b", low):
        pm = _PHONE_RE.search(cmd)
        phone = _norm_phone(pm.group(1))
        name = (cmd[:pm.start()] + " " + cmd[pm.end():]).lower()
        name = re.sub(r"\b(?:save|add|store|remember|update|the|contact|number|phone|mobile|no|of|for|as|is|in|my|to|whatsapp|whats app|name|named|called|\'s)\b", " ", name)
        name = re.sub(r"[^a-z0-9 ]", " ", name)
        name = re.sub(r"\s+", " ", name).strip()
        if not name:
            return _action_result("Whose number is it? Say, for example, “save Rahul's number 98765 43210”.")
        with core.get_db() as db:
            db.execute("INSERT OR REPLACE INTO contacts(name,phone,created_at) VALUES(?,?,?)", (name, phone, _now()))
        return _action_result(f"Saved {name.title()}'s number. Now you can say “call {name} on WhatsApp”.")
    if re.match(r"^(?:list|show|what are)\s+(?:my\s+)?contacts$", low):
        with core.get_db() as db:
            rows = db.execute("SELECT name, phone FROM contacts ORDER BY name").fetchall()
        if not rows:
            return _action_result("You have no saved contacts yet. Say “save Rahul's number 98765 43210”.")
        return _action_result("Your contacts:\n" + "\n".join(f"• {r['name'].title()}: +{r['phone']}" for r in rows),
                              speech=f"You have {len(rows)} saved contacts.")
    return None


def _handle_whatsapp(cmd: str, low: str) -> dict | None:
    m = re.match(r"^(?:open\s+whats\s*app\s+(?:and|then)\s+)?(?:(?:voice\s+|video\s+)?(?:call|ring|dial|phone)|(?:message|text|msg|send\s+(?:a\s+)?(?:whats\s*app\s+)?(?:message\s+)?to|whats\s*app))\s+(?P<rest>.+)$", low)
    if not m:
        return None
    verb_call = bool(re.match(r"^(?:open\s+whats\s*app\s+(?:and|then)\s+)?(?:(?:voice\s+|video\s+)?(?:call|ring|dial|phone))\b", low))
    rest = m.group("rest")
    video = "video" in low.split(" call")[0] if verb_call else False
    text = ""
    tm = re.search(r"\s+(?:saying|say|that|with message|message|:)\s+(?P<t>.+)$", rest)
    if tm and not verb_call:
        text = tm.group("t").strip()
        rest = rest[:tm.start()]
    elif not verb_call and re.search(r"\s", rest.strip()):
        first, _, tail = rest.strip().partition(" ")
        if _find_contact(first):
            rest, text = first, tail
    name = re.sub(r"\s+(?:on|in|via|using|through)\s+whats\s*app$", "", rest.strip()).strip(" .?!")
    name = re.sub(r"^(?:to\s+)?(?:my\s+)?", "", name)
    if not name:
        return None
    # a spoken phone number works too
    pm = _PHONE_RE.search(name)
    if pm:
        found = (name, _norm_phone(pm.group(1)))
    else:
        found = _find_contact(name)
    if not found:
        if not verb_call and not re.search(r"whats\s*app", low):
            return None
        return _action_result(f"I don't have a number for {name.title()}. Say “save {name}'s number 98765 43210” first, then ask me again.")
    cname, phone = found
    q = urllib.parse.quote(text)
    web = f"https://wa.me/{phone}" + (f"?text={q}" if text else "")
    app_url = f"whatsapp://send?phone={phone}" + (f"&text={q}" if text else "")
    if verb_call:
        reply = (f"Opening WhatsApp with {cname.title()}. WhatsApp doesn't let apps start a call by themselves, "
                 f"so tap the {'video ' if video else ''}call icon at the top of the chat.")
        speech = f"Opening WhatsApp with {cname}. Tap the call button to ring them."
    else:
        reply = f"Opening WhatsApp to {cname.title()}" + (f" with your message “{text}”. Press send." if text else ".")
        speech = reply
    return _action_result(reply, speech=speech, open_url=app_url, fallback_url=web, action={"type": "open_url", "url": web})


# ----------------------------------------------------------------------------
# orders (Amazon / Flipkart ...)
# ----------------------------------------------------------------------------
ORDER_PAGES = {
    "amazon": "https://www.amazon.in/gp/css/order-history", "flipkart": "https://www.flipkart.com/account/orders",
    "myntra": "https://www.myntra.com/my/orders", "meesho": "https://www.meesho.com/", "ajio": "https://www.ajio.com/my-account/order-history",
}
_STATUS_WORDS = {"delivered": "Delivered", "out for delivery": "Out for delivery", "shipped": "Shipped", "dispatched": "Shipped",
                 "in transit": "In transit", "delayed": "Delayed", "cancelled": "Cancelled", "canceled": "Cancelled", "returned": "Returned"}
_OPEN_STATUSES = ("Delivered", "Cancelled", "Returned")


def _eta_from(text: str) -> str:
    try:
        w = smart.parse_when(text)
        if w and w.due and w.has_date:
            return w.due.date().isoformat()
        if w and w.due and w.relative:
            return w.due.date().isoformat()
    except Exception:  # noqa: BLE001
        pass
    return ""


def _orders(open_only: bool = True) -> list[dict]:
    with core.get_db() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM deliveries ORDER BY id DESC").fetchall()]
    return [r for r in rows if not open_only or r["status"] not in _OPEN_STATUSES]


def _handle_orders(cmd: str, low: str) -> dict | None:
    # "I ordered AirPods on Amazon, arriving Friday"
    m = re.match(r"^(?:i\s+)?(?:have\s+)?(?:ordered|bought|purchased|placed\s+an?\s+order\s+for|add\s+(?:an?\s+)?order|save\s+(?:my\s+)?order|track\s+(?:my\s+)?order\s+for)\s+(?P<item>.+?)\s+(?:on|from|via|at)\s+(?P<ret>amazon|flipkart|myntra|meesho|ajio)\b(?P<rest>.*)$", low)
    if m:
        item = m.group("item").strip(" ,.")
        item = re.sub(r"^(?:a|an|the|my)\s+", "", item)
        ret = m.group("ret").title()
        eta = _eta_from(m.group("rest"))
        now = _now()
        with core.get_db() as db:
            db.execute("INSERT INTO deliveries(item_name,retailer,courier,tracking_number,status,estimated_date,recipient,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (item.title()[:160], ret, "", "", "Order confirmed", eta, "", "Added by voice/chat", now, now))
        when = f", expected {eta}" if eta else ""
        return _action_result(f"Saved your {ret} order “{item}”{when}. I'll remember it and alert you when it's due. Tell me when it ships or arrives.",
                              refresh=["deliveries"])
    # "my airpods order is out for delivery / delivered"
    m = re.match(r"^(?:my\s+)?(?P<item>.+?)\s+(?:order\s+|package\s+|parcel\s+)?(?:is|has\s+been|was|got|has)\s+(?P<st>delivered|out\s+for\s+delivery|shipped|dispatched|in\s+transit|delayed|cancelled|canceled|returned|arrived)$", low)
    if m:
        item = re.sub(r"\b(?:order|package|parcel|amazon|flipkart)\b", "", m.group("item")).strip()
        st = "Delivered" if m.group("st") == "arrived" else _STATUS_WORDS.get(re.sub(r"\s+", " ", m.group("st")))
        orders = _orders(open_only=False)
        hit = next((o for o in orders if item and (item in o["item_name"].lower() or o["item_name"].lower() in item)), None)
        if hit and st:
            with core.get_db() as db:
                db.execute("UPDATE deliveries SET status=?, updated_at=? WHERE id=?", (st, _now(), hit["id"]))
            return _action_result(f"Updated “{hit['item_name']}” to {st}.", refresh=["deliveries"])
    # "track my amazon order", "where is my order"
    if re.search(r"\b(?:track|tracking|status|where(?:'s|\s+is)|check|update on|kahan hai)\b", low) and re.search(r"\b(?:order|orders|package|parcel|delivery|deliveries|amazon|flipkart)\b", low) \
            and not re.search(r"\b(?:remind|reminder)\b", low):
        ret = next((k for k in ORDER_PAGES if k in low), "")
        orders = _orders()
        if ret:
            orders = [o for o in orders if ret in o["retailer"].lower()] or orders
        page = ORDER_PAGES.get(ret or (orders[0]["retailer"].lower() if orders and orders[0]["retailer"].lower() in ORDER_PAGES else "amazon"))
        if orders:
            lines = [f"• {o['item_name']} ({o['retailer'] or 'order'}): {o['status']}" + (f", expected {o['estimated_date']}" if o["estimated_date"] else "") for o in orders[:6]]
            reply = "Here are the orders I'm tracking:\n" + "\n".join(lines) + "\nOpening the live order page so you can see the courier's latest scan."
            speech = f"You have {len(orders)} open order{'s' if len(orders) != 1 else ''}. " + "; ".join(f"{o['item_name']} is {o['status'].lower()}" for o in orders[:3]) + "."
        else:
            reply = ("I'm not tracking any open orders yet. Tell me like this: “I ordered headphones on Amazon, arriving Friday”. "
                     "Opening your orders page for now.")
            speech = "I'm not tracking any orders yet. Opening your orders page."
        return _action_result(reply, speech=speech, open_url=page, action={"type": "open_url", "url": page})
    return None


# ----------------------------------------------------------------------------
# study plan from history
# ----------------------------------------------------------------------------
def _mode(values: list[int], default: int) -> int:
    return Counter(values).most_common(1)[0][0] if values else default


def study_insights() -> dict:
    with core.get_db() as db:
        tasks = [dict(r) for r in db.execute("SELECT * FROM tasks").fetchall()]
        sessions = [dict(r) for r in db.execute("SELECT * FROM study_sessions").fetchall()]
    today = date.today().isoformat()
    done = [t for t in tasks if t["status"] == "completed"]
    pending = [t for t in tasks if t["status"] != "completed"]
    hours = []
    for t in done:
        if t.get("completed_at"):
            try:
                hours.append(datetime.fromisoformat(t["completed_at"]).hour)
            except ValueError:
                pass
    for s in sessions:
        if s["completed"]:
            try:
                hours.append(int(str(s["start_time"]).split(":")[0]))
            except ValueError:
                pass
    per_day: dict[str, int] = {}
    for s in sessions:
        if s["completed"]:
            per_day[s["session_date"]] = per_day.get(s["session_date"], 0) + int(s["duration_minutes"])
    for t in done:
        if t.get("completed_at"):
            d = t["completed_at"][:10]
            per_day[d] = per_day.get(d, 0) + int(t["estimated_minutes"])
    subj_total, subj_done = Counter(), Counter()
    for t in tasks:
        s = (t["subject"] or "General").strip() or "General"
        subj_total[s] += 1
        if t["status"] == "completed":
            subj_done[s] += 1
    weak = [s for s, n in subj_total.items() if n >= 2 and subj_done[s] / n < 0.5]
    overdue = [t for t in pending if t["due_date"] and t["due_date"] < today]
    avg_task = round(sum(t["estimated_minutes"] for t in done) / len(done)) if done else 0
    return {
        "has_history": bool(done or any(s["completed"] for s in sessions)),
        "best_hour": _mode(hours, 18),
        "budget": max(45, min(240, round(sum(per_day.values()) / len(per_day)))) if per_day else 90,
        "chunk": max(25, min(90, avg_task or 45)),
        "weak": weak, "overdue": overdue, "pending": pending,
        "done_count": len(done), "total_count": len(tasks),
        "subj_total": subj_total, "subj_done": subj_done,
    }


def build_study_plan(days: int = 7, start_offset: int = 0, save: bool = True) -> dict:
    ins = study_insights()
    today = date.today()
    now = datetime.now()
    start_day = today + timedelta(days=start_offset)
    pending = sorted(ins["pending"], key=lambda t: (
        t["due_date"] or "9999-12-31",
        0 if ((t["subject"] or "General") in ins["weak"]) else 1, t["id"]))
    remaining = {t["id"]: int(t["estimated_minutes"]) for t in pending}
    plan: list[dict] = []
    for i in range(days):
        d = start_day + timedelta(days=i)
        cap = ins["budget"]
        hour, minute = ins["best_hour"], 0
        if d == today and (now.hour > hour or (now.hour == hour and now.minute > 0)):
            hour, minute = min(now.hour + 1, 22), 0
        t_cursor = datetime.combine(d, datetime.min.time()).replace(hour=hour, minute=minute)
        for t in pending:
            if cap <= 0:
                break
            if remaining[t["id"]] <= 0:
                continue
            # due earlier than this day (and not overdue-from-start): skip, it should have been done already
            if t["due_date"] and t["due_date"] < d.isoformat() and i > 0:
                remaining[t["id"]] = max(remaining[t["id"]], 0)
            chunk = min(ins["chunk"], remaining[t["id"]], cap)
            if chunk < 10:
                continue
            plan.append({"date": d.isoformat(), "start": t_cursor.strftime("%H:%M"), "minutes": chunk,
                         "title": t["title"], "subject": t["subject"], "task_id": t["id"], "due": t["due_date"]})
            remaining[t["id"]] -= chunk
            cap -= chunk
            t_cursor += timedelta(minutes=chunk + 10)
    # nothing pending: suggest revision for the weakest subject so the day isn't empty
    if not plan and ins["weak"]:
        w = ins["weak"][0]
        plan.append({"date": start_day.isoformat(), "start": f"{ins['best_hour']:02d}:00", "minutes": ins["chunk"],
                     "title": f"Revise {w}", "subject": w, "task_id": None, "due": None})
    if save and plan:
        now_s = _now()
        with core.get_db() as db:
            db.execute("DELETE FROM study_sessions WHERE title LIKE '[Plan]%' AND completed = 0 AND session_date >= ?", (start_day.isoformat(),))
            db.executemany("INSERT INTO study_sessions(title, session_date, start_time, duration_minutes, completed, created_at) VALUES(?,?,?,?,0,?)",
                           [(f"[Plan] {(p['subject'] + ': ') if p['subject'] else ''}{p['title']}", p["date"], p["start"], p["minutes"], now_s) for p in plan])
    return {"plan": plan, "insights": ins}


def _ampm(hhmm: str) -> str:
    h, m = int(hhmm[:2]), hhmm[3:]
    return f"{(h % 12) or 12}:{m} {'AM' if h < 12 else 'PM'}"


def _handle_study(cmd: str, low: str) -> dict | None:
    wants_plan = re.search(r"\b(?:study\s+(?:plan|schedule|timetable|routine)|plan\s+my\s+(?:study|studies|week|day|revision)|my\s+study\s+(?:plan|schedule)|what\s+should\s+i\s+study)\b", low) \
        and re.match(r"^(?:make|create|generate|build|prepare|give|show|suggest|plan|update|what|my|study|schedule|redo|refresh|replan|please|i\s+need|i\s+want)\b", low)
    wants_report = re.search(r"\b(?:how\s+(?:am\s+i|have\s+i\s+been)\s+(?:doing|studying)|my\s+study\s+(?:report|progress|history|habits?)|study\s+(?:progress|report|habits?))\b", low)
    if not (wants_plan or wants_report):
        return None
    days = 1 if re.search(r"\b(?:today|aaj)\b", low) and not re.search(r"\bweek\b", low) else 7
    off = 1 if re.search(r"\btomorrow\b", low) else 0
    dm = re.search(r"\b(\d{1,2})\s*days?\b", low)
    if dm:
        days = max(1, min(14, int(dm.group(1))))
    if off:
        days = 1
    res = build_study_plan(days=days, start_offset=off, save=bool(wants_plan))
    ins, plan = res["insights"], res["plan"]
    lines = []
    if ins["has_history"]:
        best = _ampm("%02d:00" % ins["best_hour"])
        lines.append(f"From your history: you usually study around {best}, about {ins['budget']} minutes a day, "
                     f"and you've finished {ins['done_count']} of {ins['total_count']} tasks.")
    else:
        lines.append("I don't have much study history for you yet, so I'm using a gentle default (about 90 minutes from 6 PM). "
                     "Mark tasks complete and I'll learn your real rhythm.")
    if ins["weak"]:
        lines.append("Needs attention: " + ", ".join(f"{s} ({ins['subj_done'][s]}/{ins['subj_total'][s]} done)" for s in ins["weak"]) + " — I put these first.")
    if ins["overdue"]:
        lines.append(f"{len(ins['overdue'])} task(s) are overdue: " + ", ".join(t["title"] for t in ins["overdue"][:3]) + ".")
    if wants_report and not wants_plan:
        return _action_result("\n".join(lines), speech=" ".join(lines))
    if not plan:
        lines.append("You have no pending tasks. Add some, like “add a study task SQL revision for DBMS due Friday”, and I'll plan around them.")
        return _action_result("\n".join(lines), speech=lines[-1])
    by_day: dict[str, list[dict]] = {}
    for p in plan:
        by_day.setdefault(p["date"], []).append(p)
    for d, items in by_day.items():
        label = "Today" if d == date.today().isoformat() else datetime.fromisoformat(d).strftime("%a %d %b")
        lines.append(f"{label}: " + "; ".join(f"{_ampm(p['start'])} {p['title']} ({p['minutes']} min)" for p in items))
    first = plan[0]
    speech = (f"Your plan is ready. Next up: {first['title']} at {_ampm(first['start'])} for {first['minutes']} minutes. "
              f"I've added {len(plan)} study sessions to your planner.")
    return _action_result("\n".join(lines), speech=speech, refresh=["study"])


# ----------------------------------------------------------------------------
# serious notifications
# ----------------------------------------------------------------------------
def collect_serious() -> list[dict]:
    """Everything worth interrupting the person for right now (per current person). Each item has a stable key."""
    items: list[dict] = []
    today = date.today()
    tom = today + timedelta(days=1)
    with core.get_db() as db:
        for t in db.execute("SELECT id,title,subject,due_date FROM tasks WHERE status != 'completed' AND due_date IS NOT NULL AND due_date <= ?", (today.isoformat(),)):
            overdue = t["due_date"] < today.isoformat()
            items.append({"key": f"task:{t['id']}:{today.isoformat()}", "severity": "serious",
                          "title": ("Overdue: " if overdue else "Due today: ") + t["title"],
                          "body": (t["subject"] + " · " if t["subject"] else "") + f"due {t['due_date']}"})
        for r in db.execute("SELECT id,item_name,expiry_date FROM inventory WHERE expiry_date IS NOT NULL AND expiry_date <= ?", (tom.isoformat(),)):
            gone = r["expiry_date"] < today.isoformat()
            items.append({"key": f"inv:{r['id']}:{r['expiry_date']}", "severity": "serious" if gone or r["expiry_date"] == today.isoformat() else "warning",
                          "title": f"{r['item_name']} {'has expired' if gone else 'expires ' + ('today' if r['expiry_date'] == today.isoformat() else 'tomorrow')}",
                          "body": "Use it or toss it to stay safe."})
        for v in db.execute("SELECT id,visitor_name,message FROM visitor_messages WHERE urgency='urgent' AND status='unread'"):
            items.append({"key": f"visitor:{v['id']}", "severity": "serious", "title": f"Urgent visitor message from {v['visitor_name']}", "body": v["message"][:140]})
        for a in db.execute("SELECT id,severity,title,details FROM local_alerts WHERE lower(severity) IN ('high','critical','severe','emergency','serious')"):
            items.append({"key": f"alert:{a['id']}", "severity": "serious", "title": a["title"], "body": a["details"][:160]})
        for d in db.execute("SELECT id,item_name,retailer,status,estimated_date FROM deliveries WHERE status NOT IN ('Delivered','Cancelled','Returned')"):
            if d["status"] == "Out for delivery":
                items.append({"key": f"del-ofd:{d['id']}", "severity": "info", "title": f"{d['item_name']} is out for delivery", "body": d["retailer"] or "Keep your phone handy."})
            elif d["estimated_date"] and d["estimated_date"] <= today.isoformat():
                items.append({"key": f"del-due:{d['id']}:{d['estimated_date']}", "severity": "info", "title": f"{d['item_name']} was due {'today' if d['estimated_date'] == today.isoformat() else d['estimated_date']}",
                              "body": "Check the order status. Tell me “my order is delivered” if it arrived."})
        for r in db.execute("SELECT id,title,remind_at FROM reminders WHERE status='pending' AND (lower(title) LIKE '%urgent%' OR lower(title) LIKE '%emergency%' OR lower(title) LIKE '%important%')"):
            try:
                due = datetime.fromisoformat(r["remind_at"])
            except ValueError:
                continue
            if due - datetime.now() <= timedelta(minutes=30):
                items.append({"key": f"rem:{r['id']}:{r['remind_at']}", "severity": "serious", "title": r["title"], "body": f"Due {due.strftime('%I:%M %p').lstrip('0')}"})
    return items


def new_notifications(mark: bool = True) -> list[dict]:
    items = collect_serious()
    if not items:
        return []
    with core.get_db() as db:
        seen = {r["key"] for r in db.execute("SELECT key FROM notified")}
        fresh = [i for i in items if i["key"] not in seen]
        if mark and fresh:
            db.executemany("INSERT OR IGNORE INTO notified(key, created_at) VALUES(?,?)", [(i["key"], _now()) for i in fresh])
    return fresh


def _handle_serious(cmd: str, low: str) -> dict | None:
    if not re.match(r"^(?:is\s+there\s+|are\s+there\s+|do\s+i\s+have\s+|any\s+|check\s+|show\s+|what(?:'s|\s+is)\s+|tell\s+me\s+)?(?:anything\s+|any\s+|something\s+|my\s+)?(?:urgent|serious|important|alerts?|notifications?|emergenc(?:y|ies))\b", low) \
            and not re.search(r"\bwhat(?:'s|\s+is)\s+urgent\b|\banything\s+urgent\b", low):
        return None
    items = collect_serious()
    if not items:
        return _action_result("Nothing serious right now. You're all clear!", speech="Nothing serious right now. You're all clear.")
    lines = [f"• {i['title']}" + (f" — {i['body']}" if i["body"] else "") for i in items[:8]]
    return _action_result("Here's what needs attention:\n" + "\n".join(lines),
                          speech=f"{len(items)} thing{'s' if len(items) != 1 else ''} need attention. " + "; ".join(i["title"] for i in items[:3]) + ".")


# ----------------------------------------------------------------------------
# people (profiles)
# ----------------------------------------------------------------------------
def ensure_person(name: str) -> str:
    slug = core.slugify_person(name)
    tok = core.CURRENT_PERSON.set(slug)
    try:
        core.get_db().close()   # creates + initialises that person's database
    finally:
        core.CURRENT_PERSON.reset(tok)
    return slug


def _handle_person(cmd: str, low: str) -> dict | None:
    if re.match(r"^(?:who\s+am\s+i|who\s+is\s+(?:this|using\s+(?:this|you))|which\s+(?:profile|user)(?:\s+is\s+this)?|current\s+(?:user|profile))$", low):
        who = core.CURRENT_PERSON.get()
        return _action_result(f"You're using the {'main' if who == 'default' else who.title()} profile. Everything I save goes into this profile.")
    m = re.match(r"^(?:switch|change|swap)\s+(?:the\s+)?(?:(?:user|profile|person|account)\s+)?to\s+(?P<n>[a-z][a-z ]{0,30}?)(?:\s+(?:profile|account))?$", low) \
        or re.match(r"^(?:log\s*in|login|sign\s+in|use\s+profile)\s+(?:as\s+)?(?P<n>[a-z][a-z ]{0,30})$", low) \
        or re.match(r"^(?:create|add|make|new)\s+(?:a\s+)?(?:new\s+)?(?:profile|user|person|account)\s+(?:for|called|named)?\s*(?P<n>[a-z][a-z ]{0,30})$", low)
    if not m:
        m2 = re.match(r"^(?:i\s+am|i'm|this\s+is|it'?s)\s+(?P<n>[a-z]{2,20})$", low)
        if m2 and core.slugify_person(m2.group("n")) in core.list_people():
            m = m2
    if not m:
        return None
    name = m.group("n").strip()
    if name in {"me", "main", "default", "home"}:
        name = "default"
    slug = ensure_person(name)
    label = "main" if slug == "default" else slug.title()
    return _action_result(f"Switching to {label}'s profile. Your notes, tasks, history and location stay separate.",
                          speech=f"Hi {label}! Switching to your profile.", action={"type": "switch_person", "name": slug})


# ----------------------------------------------------------------------------
# entry point
# ----------------------------------------------------------------------------
def handle(text: str) -> dict | None:
    cmd = smart.clean_command(text)
    if not cmd:
        return None
    low = cmd.lower().strip(" .!?")
    for fn in (_handle_person, _handle_contacts, _handle_whatsapp, _handle_ride, _handle_location, _handle_orders, _handle_study, _handle_serious):
        r = fn(cmd, low)
        if r is not None:
            return r
    return None
