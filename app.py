#!/usr/bin/env python3
"""Hybrid meeting slot booker → Yandex Calendar (CalDAV + optional iCal busy)."""
from __future__ import annotations

import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta, time, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from icalendar import Calendar, Event, vCalAddress, vText
from pydantic import BaseModel, EmailStr, Field

TZ = ZoneInfo("Europe/Moscow")
DAY_START = time(15, 0)
DAY_END = time(19, 0)
SLOT_MIN = 30
MAX_AHEAD_DAYS = 13
OWNER_EMAIL = os.environ.get("YANDEX_EMAIL", "d.cheklov@hybrid.ru")
OWNER_NAME = os.environ.get("OWNER_NAME", "Дмитрий Чеклов")
CALDAV_URL = os.environ.get("YANDEX_CALDAV_URL", "https://caldav.yandex.ru/").rstrip("/") + "/"
CALDAV_USER = os.environ.get("YANDEX_CALDAV_USER", OWNER_EMAIL)
CALDAV_PASSWORD = os.environ.get("YANDEX_CALDAV_PASSWORD", "")
ICAL_URL = os.environ.get("YANDEX_ICAL_URL", "").strip()
CALDAV_COLLECTION = os.environ.get(
    "YANDEX_CALDAV_COLLECTION", "events-37973416"
).strip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "bookings.sqlite"
STATIC = Path(__file__).parent / "static"

app = FastAPI(title="Hybrid meet")
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS bookings (
            slot_start TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            uid TEXT NOT NULL,
            created_at TEXT NOT NULL
        )"""
    )
    conn.commit()
    return conn


if os.environ.get("RESET_BOOKINGS") == "1":
    _c = db()
    _c.execute("DELETE FROM bookings")
    _c.commit()
    _c.close()


def today_local() -> datetime:
    d = datetime.now(TZ).date()
    return datetime.combine(d, time(0, 0), tzinfo=TZ)


def _load_dates_file(path: Path) -> set[str]:
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text())
    except Exception:
        return set()
    if not isinstance(data, list):
        return set()
    out = set()
    for x in data:
        s = str(x).strip()
        try:
            datetime.strptime(s, "%Y-%m-%d")
            out.add(s)
        except ValueError:
            continue
    return out


def open_dates() -> set[str]:
    dates: set[str] = set()
    env = os.environ.get("OPEN_DATES", "")
    for part in env.split(","):
        s = part.strip()
        try:
            datetime.strptime(s, "%Y-%m-%d")
            dates.add(s)
        except ValueError:
            continue
    dates |= _load_dates_file(Path(__file__).parent / "open_dates.json")
    dates |= _load_dates_file(DATA_DIR / "open_dates.json")
    return dates


def parse_day(date_str: str | None) -> datetime:
    today = today_local()
    if not date_str:
        return today
    try:
        d = datetime.strptime(date_str.strip(), "%Y-%m-%d").date()
    except ValueError:
        return today
    return datetime.combine(d, time(0, 0), tzinfo=TZ)


def day_is_open(day: datetime) -> bool:
    d = day.date()
    if d < datetime.now(TZ).date():
        return False
    return d.isoformat() in open_dates()


def slot_starts(day: datetime | None = None) -> list[datetime]:
    day = day or today_local()
    start = datetime.combine(day.date(), DAY_START, tzinfo=TZ)
    end = datetime.combine(day.date(), DAY_END, tzinfo=TZ)
    out = []
    t = start
    while t + timedelta(minutes=SLOT_MIN) <= end:
        out.append(t)
        t += timedelta(minutes=SLOT_MIN)
    return out


def date_label(d) -> str:
    if d == datetime.now(TZ).date():
        return "Сегодня"
    return d.strftime("%d.%m.%Y")


def parse_ics_busy(raw: bytes, window_start: datetime, window_end: datetime) -> set[str]:
    busy: set[str] = set()
    try:
        cal = Calendar.from_ical(raw)
    except Exception:
        return busy
    for component in cal.walk("VEVENT"):
        dtstart = component.get("dtstart")
        dtend = component.get("dtend")
        if not dtstart:
            continue
        s = dtstart.dt
        if hasattr(s, "hour"):
            if s.tzinfo is None:
                s = s.replace(tzinfo=TZ)
            else:
                s = s.astimezone(TZ)
        else:
            continue
        if dtend and hasattr(dtend.dt, "hour"):
            e = dtend.dt
            if e.tzinfo is None:
                e = e.replace(tzinfo=TZ)
            else:
                e = e.astimezone(TZ)
        else:
            e = s + timedelta(hours=1)
        if e <= window_start or s >= window_end:
            continue
        t = window_start
        while t < window_end:
            se = t + timedelta(minutes=SLOT_MIN)
            if t < e and se > s:
                busy.add(t.isoformat())
            t += timedelta(minutes=SLOT_MIN)
    return busy


def ical_busy(day: datetime | None = None) -> set[str]:
    if not ICAL_URL:
        return set()
    slots = slot_starts(day)
    if not slots:
        return set()
    ws, we = slots[0], slots[-1] + timedelta(minutes=SLOT_MIN)
    try:
        r = httpx.get(ICAL_URL, timeout=20.0, follow_redirects=True)
        r.raise_for_status()
        return parse_ics_busy(r.content, ws, we)
    except Exception:
        return set()


def sqlite_busy() -> set[str]:
    conn = db()
    rows = conn.execute("SELECT slot_start FROM bookings").fetchall()
    conn.close()
    return {r[0] for r in rows}


def make_ics(uid: str, start: datetime, name: str, email: str) -> bytes:
    end = start + timedelta(minutes=SLOT_MIN)
    cal = Calendar()
    cal.add("prodid", "-//Hybrid//Meet//RU")
    cal.add("version", "2.0")
    cal.add("method", "REQUEST")
    ev = Event()
    ev.add("uid", uid)
    ev.add("dtstamp", datetime.now(timezone.utc))
    ev.add("dtstart", start)
    ev.add("dtend", end)
    ev.add("summary", f"Встреча: {name} × {OWNER_NAME}")
    ev.add("description", f"Слот с лендинга Hybrid. Гость: {name} <{email}>")
    ev.add("location", "Яндекс Телемост / Hybrid")
    organizer = vCalAddress(f"MAILTO:{OWNER_EMAIL}")
    organizer.params["cn"] = vText(OWNER_NAME)
    ev.add("organizer", organizer)
    attendee = vCalAddress(f"MAILTO:{email}")
    attendee.params["cn"] = vText(name)
    attendee.params["partstat"] = vText("ACCEPTED")
    ev.add("attendee", attendee)
    cal.add_component(ev)
    return cal.to_ical()


def caldav_put(uid: str, ics: bytes) -> tuple[bool, str]:
    if not CALDAV_PASSWORD:
        return False, "no_password"
    from urllib.parse import quote
    user = quote(CALDAV_USER, safe="")
    paths = [
        f"{CALDAV_URL}calendars/{user}/{CALDAV_COLLECTION}/{uid}.ics",
        f"{CALDAV_URL}calendars/{CALDAV_USER}/{CALDAV_COLLECTION}/{uid}.ics",
    ]
    auth = (CALDAV_USER, CALDAV_PASSWORD)
    last = "no_attempt"
    for url in paths:
        try:
            r = httpx.put(
                url,
                content=ics,
                auth=auth,
                headers={"Content-Type": "text/calendar; charset=utf-8"},
                timeout=20.0,
            )
            last = f"{r.status_code}"
            if r.status_code in (200, 201, 204, 409):
                return True, last
        except Exception as e:
            last = type(e).__name__
    return False, last


class BookIn(BaseModel):
    slot: str = Field(..., description="ISO datetime Europe/Moscow")
    name: str = Field(..., min_length=1, max_length=120)
    email: EmailStr


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/book/slots")
def api_slots(date: str | None = Query(None)):
    day = parse_day(date)
    d = day.date()
    today = datetime.now(TZ).date()
    opened = day_is_open(day)
    now = datetime.now(TZ)
    items = []
    show_grid = opened or d == today
    if show_grid:
        busy = sqlite_busy() | ical_busy(day) if opened else set()
        for s in slot_starts(day):
            key = s.isoformat()
            items.append(
                {
                    "start": key,
                    "label": s.strftime("%H:%M"),
                    "end_label": (s + timedelta(minutes=SLOT_MIN)).strftime("%H:%M"),
                    "taken": (not opened) or key in busy or s < now,
                }
            )
    return {
        "date": d.isoformat(),
        "date_label": date_label(d),
        "open": opened,
        "min_date": today.isoformat(),
        "max_date": (today + timedelta(days=MAX_AHEAD_DAYS)).isoformat(),
        "tz": "Europe/Moscow",
        "owner": OWNER_NAME,
        "slots": items,
    }


@app.post("/book")
def api_book(body: BookIn):
    try:
        start = datetime.fromisoformat(body.slot)
        if start.tzinfo is None:
            start = start.replace(tzinfo=TZ)
        else:
            start = start.astimezone(TZ)
    except ValueError:
        raise HTTPException(400, "bad slot")
    day = parse_day(start.date().isoformat())
    if not day_is_open(day):
        raise HTTPException(400, "slot not offered")
    allowed = {s.isoformat(): s for s in slot_starts(day)}
    if body.slot not in allowed and start.isoformat() not in allowed:
        raise HTTPException(400, "slot not offered")
    start = allowed.get(body.slot) or allowed[start.isoformat()]
    key = start.isoformat()
    if start < datetime.now(TZ):
        raise HTTPException(400, "slot in the past")
    if key in ical_busy(day) or key in sqlite_busy():
        raise HTTPException(409, "slot taken")
    uid = str(uuid.uuid4())
    ics = make_ics(uid, start, body.name.strip(), str(body.email))
    (DATA_DIR / f"{uid}.ics").write_bytes(ics)
    conn = db()
    try:
        conn.execute(
            "INSERT INTO bookings(slot_start, name, email, uid, created_at) VALUES (?,?,?,?,?)",
            (key, body.name.strip(), str(body.email), uid, datetime.now(TZ).isoformat()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        conn.close()
        raise HTTPException(409, "slot taken")
    conn.close()
    ok, detail = caldav_put(uid, ics)
    return {
        "ok": True,
        "start": key,
        "calendar": "yandex" if ok else "queued",
        "caldav": detail,
    }


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")
