#!/usr/bin/env python3
"""Kleiner lokaler KI-Assistent für macOS.

Läuft komplett lokal: Sprachmodell über Ollama, Werkzeuge über
AppleScript (Kalender, Erinnerungen) und Spotlight (Dateisuche).
Nur Python-Standardbibliothek, keine zusätzlichen Pakete.
"""

import datetime as dt
import json
import os
import re
import subprocess
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.environ.get("ASSISTENT_MODEL", "qwen2.5:3b")
PORT = int(os.environ.get("ASSISTENT_PORT", "8765"))
CALENDAR_NAME = os.environ.get("ASSISTENT_KALENDER", "")  # leer = erster beschreibbarer Kalender

def _gpu_option():
    # In virtuellen Maschinen ist die virtuelle GPU ~10x langsamer als die CPU.
    setting = os.environ.get("ASSISTENT_GPU", "auto")
    if setting == "auto":
        cpu = subprocess.run(["sysctl", "-n", "machdep.cpu.brand_string"],
                             capture_output=True, text=True).stdout
        setting = "aus" if "Virtual" in cpu else "an"
    return {"num_gpu": 0} if setting in ("aus", "0", "off") else {}


GPU_OPTION = _gpu_option()
HERE = Path(__file__).resolve().parent
HOME = Path.home()

WEEKDAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
          "August", "September", "Oktober", "November", "Dezember"]


# ---------------------------------------------------------------- Hilfsfunktionen

def weekday_index(name):
    if not name:
        return None
    # "Di", "Dienstag", "dienstags" ... – die ersten zwei Buchstaben sind eindeutig
    prefix = name.strip().lower()[:2]
    for i, w in enumerate(WEEKDAYS):
        if len(prefix) == 2 and w.lower().startswith(prefix):
            return i
    return None


def resolve_date(date=None, day=None, month=None, weekday=None, today=None):
    """Macht aus losen Angaben ein konkretes Datum.

    "Dienstag 23." -> nächster Tag, der ein 23. UND ein Dienstag ist.
    """
    today = today or dt.date.today()
    if date:
        return dt.date.fromisoformat(str(date)[:10])
    wd = weekday_index(weekday)
    day = int(day) if day not in (None, "") else None
    month = int(month) if month not in (None, "") else None
    if day is None and wd is None:
        raise ValueError("Kein Datum angegeben.")
    d = today
    for _ in range(366 * 3):
        if ((day is None or d.day == day)
                and (month is None or d.month == month)
                and (wd is None or d.weekday() == wd)):
            if day and wd is not None and month is None and (d - today).days > 45:
                # Wahrscheinlich ein Versprecher – lieber nachfragen als falsch eintragen.
                by_day = resolve_date(day=day, today=today)
                by_wd = resolve_date(weekday=weekday, today=today)
                raise ValueError(
                    "Wochentag und Datum passen nicht zusammen. Bitte den Nutzer fragen: "
                    "Meinst du %s oder %s?" % (fmt_date(by_day), fmt_date(by_wd)))
            return d
        d += dt.timedelta(days=1)
    raise ValueError("Kein passendes Datum gefunden (Wochentag und Tag passen nie zusammen?).")


def parse_time(t, fallback=None):
    if t in (None, ""):
        return fallback
    m = re.match(r"^\s*(\d{1,2})(?:[:.](\d{2}))?", str(t))
    if not m:
        raise ValueError("Uhrzeit nicht verstanden: %s" % t)
    return dt.time(int(m.group(1)) % 24, int(m.group(2) or 0))


def fmt_date(d):
    return "%s, %d. %s %d" % (WEEKDAYS[d.weekday()], d.day, MONTHS[d.month - 1], d.year)


def as_string(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def as_date(var, d):
    # Locale-unabhängig: Datum Stück für Stück setzen.
    return (
        "set {v} to current date\n"
        "set day of {v} to 1\n"
        "set year of {v} to {y}\n"
        "set month of {v} to {mo}\n"
        "set day of {v} to {d}\n"
        "set time of {v} to {s}\n"
    ).format(v=var, y=d.year, mo=d.month, d=d.day, s=d.hour * 3600 + d.minute * 60)


def osascript(script):
    r = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "AppleScript-Fehler")
    return r.stdout.strip()


# ---------------------------------------------------------------- Werkzeuge

def create_calendar_event(title, start_time, end_time=None, date=None, day=None, month=None,
                          weekday=None, reminder_days_before=None, reminder_minutes_before=None,
                          location=None, notes=None):
    d = resolve_date(date, day, month, weekday)
    st = parse_time(start_time)
    if st is None:
        raise ValueError("Startzeit fehlt.")
    start = dt.datetime.combine(d, st)
    et = parse_time(end_time)
    end = dt.datetime.combine(d, et) if et else start + dt.timedelta(hours=1)
    if end <= start:
        end += dt.timedelta(days=1)

    alarm_min = 0
    if reminder_days_before:
        alarm_min += int(float(reminder_days_before) * 24 * 60)
    if reminder_minutes_before:
        alarm_min += int(float(reminder_minutes_before))

    cal = ('calendar %s' % as_string(CALENDAR_NAME)) if CALENDAR_NAME \
        else '(first calendar whose writable is true)'
    props = "summary:%s, start date:startD, end date:endD" % as_string(title)
    if location:
        props += ", location:%s" % as_string(location)
    if notes:
        props += ", description:%s" % as_string(notes)
    script = as_date("startD", start) + as_date("endD", end) + (
        'tell application "Calendar"\n'
        "  set cal to %s\n"
        "  set ev to make new event at end of events of cal with properties {%s}\n"
        "%s"
        "  return name of cal\n"
        "end tell\n"
    ) % (cal, props,
         ("  tell ev to make new display alarm at end of display alarms "
          "with properties {trigger interval:-%d}\n" % alarm_min) if alarm_min else "")
    cal_name = osascript(script)

    msg = "Termin „%s“ eingetragen: %s, %s–%s Uhr (Kalender: %s)." % (
        title, fmt_date(d), start.strftime("%H:%M"), end.strftime("%H:%M"), cal_name)
    if alarm_min:
        when = start - dt.timedelta(minutes=alarm_min)
        msg += " Erinnerung am %s um %s Uhr." % (fmt_date(when.date()), when.strftime("%H:%M"))
    return msg


def create_reminder(title, date=None, day=None, month=None, weekday=None, time=None, notes=None):
    script = ""
    props = "name:%s" % as_string(title)
    when = None
    if date or day or weekday:
        d = resolve_date(date, day, month, weekday)
        when = dt.datetime.combine(d, parse_time(time, dt.time(9, 0)))
        script += as_date("remD", when)
        props += ", remind me date:remD"
    if notes:
        props += ", body:%s" % as_string(notes)
    script += (
        'tell application "Reminders"\n'
        "  make new reminder at end of reminders of default list with properties {%s}\n"
        "end tell\n"
    ) % props
    osascript(script)
    if when:
        return "Erinnerung „%s“ für %s um %s Uhr angelegt." % (
            title, fmt_date(when.date()), when.strftime("%H:%M"))
    return "Erinnerung „%s“ angelegt (ohne Termin)." % title


FILE_TYPES = {
    "praesentation": ['kMDItemContentTypeTree == "public.presentation"',
                      'kMDItemContentType == "com.apple.keynote.key"'],
    "pdf": ['kMDItemContentType == "com.adobe.pdf"'],
    "dokument": ['kMDItemContentTypeTree == "public.composite-content"',
                 'kMDItemContentTypeTree == "public.text"'],
    "tabelle": ['kMDItemContentTypeTree == "public.spreadsheet"'],
    "bild": ['kMDItemContentTypeTree == "public.image"'],
    "video": ['kMDItemContentTypeTree == "public.movie"'],
    "audio": ['kMDItemContentTypeTree == "public.audio"'],
}
FOLDERS = {
    "downloads": [HOME / "Downloads"],
    "dokumente": [HOME / "Documents"],
    "schreibtisch": [HOME / "Desktop"],
    "alle": [HOME],
}


def _mdfind(query, folders, limit):
    found = []
    for f in folders:
        if not f.exists():
            continue
        r = subprocess.run(["mdfind", "-onlyin", str(f), query],
                           capture_output=True, text=True, timeout=30)
        found += [p for p in r.stdout.splitlines()
                  if p and "/Library/" not in p and "/." not in p]
    uniq = list(dict.fromkeys(found))
    uniq.sort(key=lambda p: os.path.getmtime(p) if os.path.exists(p) else 0, reverse=True)
    return uniq[:limit]


def search_files(topic=None, file_type=None, days_back=None, folder="downloads"):
    folders = FOLDERS.get((folder or "downloads").lower(), FOLDERS["downloads"])
    parts = []
    ft = (file_type or "").lower().replace("ä", "ae")
    if ft.startswith("pr"):
        ft = "praesentation"
    if ft in FILE_TYPES:
        parts.append("(" + " || ".join(FILE_TYPES[ft]) + ")")
    if days_back not in (None, "", 0, "0"):
        n = int(days_back)
        parts.append("(kMDItemDateAdded >= $time.today(-%d) || kMDItemFSCreationDate >= $time.today(-%d))"
                     % (n, n))
    words = [w for w in re.split(r"[\s,;]+", topic or "") if len(w) > 1]
    for w in words:
        w = w.replace('"', "")
        parts.append('(kMDItemDisplayName == "*%s*"cd || kMDItemTextContent == "%s*"cdw)' % (w, w))
    if not parts:
        parts.append('kMDItemContentTypeTree == "public.item"')

    results = _mdfind(" && ".join(parts), folders, 10)
    note = ""
    if not results and words and len(parts) > len(words):
        # Zweiter Versuch: nur nach Thema, ohne Datum/Typ
        results = _mdfind(" && ".join(parts[-len(words):]), folders, 10)
        if results:
            note = "(Nichts mit allen Filtern gefunden – das hier passt nur zum Thema.)\n"
    if not results:
        return "Keine passenden Dateien gefunden."
    lines = []
    for p in results:
        t = dt.datetime.fromtimestamp(os.path.getmtime(p))
        lines.append("- %s  (%s)  PFAD: %s" % (os.path.basename(p), t.strftime("%d.%m.%Y %H:%M"), p))
    return note + "\n".join(lines)


def open_file(path, reveal_in_finder=False):
    p = Path(os.path.expanduser(path)).resolve()
    if not str(p).startswith(str(HOME)) or not p.exists():
        raise ValueError("Datei nicht gefunden: %s" % path)
    subprocess.run(["open", "-R", str(p)] if reveal_in_finder else ["open", str(p)], check=True)
    return ("Im Finder gezeigt: " if reveal_in_finder else "Geöffnet: ") + p.name


TOOLS = {
    "create_calendar_event": create_calendar_event,
    "create_reminder": create_reminder,
    "search_files": search_files,
    "open_file": open_file,
}

DATE_PROPS = {
    "date": {"type": "string", "description": "Exaktes Datum YYYY-MM-DD, nur wenn eindeutig bekannt"},
    "day": {"type": "integer", "description": "Tag im Monat, z.B. 23 bei 'Dienstag 23.'"},
    "month": {"type": "integer", "description": "Monat 1-12, nur wenn genannt"},
    "weekday": {"type": "string", "description": "Wochentag auf Deutsch, z.B. 'Dienstag', wenn genannt"},
}

TOOL_SPECS = [
    {"type": "function", "function": {
        "name": "create_calendar_event",
        "description": "Trägt einen Termin in den macOS-Kalender ein, optional mit Erinnerung.",
        "parameters": {"type": "object", "required": ["title", "start_time"], "properties": dict(DATE_PROPS, **{
            "title": {"type": "string", "description": "Kurzer Titel des Termins"},
            "start_time": {"type": "string", "description": "Startzeit HH:MM (24h)"},
            "end_time": {"type": "string", "description": "Endzeit HH:MM (24h)"},
            "reminder_days_before": {"type": "number", "description": "Erinnerung so viele Tage vorher"},
            "reminder_minutes_before": {"type": "number", "description": "Erinnerung so viele Minuten vorher"},
            "location": {"type": "string"},
            "notes": {"type": "string"},
        })}}},
    {"type": "function", "function": {
        "name": "create_reminder",
        "description": "Legt eine Aufgabe/Erinnerung in der App 'Erinnerungen' an.",
        "parameters": {"type": "object", "required": ["title"], "properties": dict(DATE_PROPS, **{
            "title": {"type": "string"},
            "time": {"type": "string", "description": "Uhrzeit HH:MM"},
            "notes": {"type": "string"},
        })}}},
    {"type": "function", "function": {
        "name": "search_files",
        "description": "Sucht Dateien auf dem Mac (Spotlight) nach Thema, Typ und Zeitraum.",
        "parameters": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "Suchbegriffe/Thema, z.B. 'Marketing'"},
            "file_type": {"type": "string",
                          "enum": ["praesentation", "pdf", "dokument", "tabelle", "bild", "video", "audio", "alle"]},
            "days_back": {"type": "integer",
                          "description": "Nur Dateien der letzten N Tage. gestern=2, heute=1, letzte Woche=7"},
            "folder": {"type": "string", "enum": ["downloads", "dokumente", "schreibtisch", "alle"],
                       "description": "Standard: downloads, wenn 'heruntergeladen' gesagt wird; sonst alle"},
        }}}},
    {"type": "function", "function": {
        "name": "open_file",
        "description": "Öffnet eine Datei oder zeigt sie im Finder.",
        "parameters": {"type": "object", "required": ["path"], "properties": {
            "path": {"type": "string", "description": "Vollständiger PFAD aus den Suchergebnissen"},
            "reveal_in_finder": {"type": "boolean"},
        }}}},
]


def system_prompt():
    today = dt.date.today()
    days = ", ".join("%s %s" % (WEEKDAYS[(today + dt.timedelta(i)).weekday()][:2],
                                (today + dt.timedelta(i)).strftime("%d.%m."))
                     for i in range(14))
    return (
        "Du bist ein hilfsbereiter, knapper Assistent auf dem Mac des Nutzers. Antworte auf Deutsch.\n"
        "Heute ist %s, %s Uhr. Die nächsten Tage: %s.\n"
        "Regeln:\n"
        "- Für Termine, Erinnerungen und Dateisuche IMMER die Werkzeuge benutzen, nichts erfinden.\n"
        "- Datum: Gib bei Terminen 'day' und 'weekday' so an, wie der Nutzer sie nennt; "
        "das Programm rechnet das Datum selbst aus.\n"
        "- Uhrzeiten von 1 bis 7 ohne 'morgens' sind Nachmittag (2 Uhr -> 14:00, 5 Uhr -> 17:00).\n"
        "- 'gestern heruntergeladen' -> search_files mit folder=downloads, days_back=2.\n"
        "- Nach einer Dateisuche: Liste die Treffer kurz auf (Name + Datum). Frag, ob du eine öffnen sollst.\n"
        "- Bestätige ausgeführte Aktionen mit dem Ergebnis des Werkzeugs."
    ) % (fmt_date(today), dt.datetime.now().strftime("%H:%M"), days)


# ---------------------------------------------------------------- Ollama

def ollama_chat(messages):
    body = json.dumps({
        "model": MODEL, "messages": messages, "tools": TOOL_SPECS, "stream": False,
        "keep_alive": "5m",  # Modell nach 5 Min. Leerlauf aus dem RAM entladen
        "options": dict({"temperature": 0.2, "num_ctx": 4096}, **GPU_OPTION),
    }).encode()
    req = urllib.request.Request(OLLAMA_URL + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())["message"]


def extract_tool_calls(msg):
    calls = msg.get("tool_calls") or []
    if calls:
        return [(c["function"]["name"], c["function"].get("arguments") or {}) for c in calls]
    # Kleine Modelle schreiben den Aufruf manchmal als JSON in den Text.
    text = (msg.get("content") or "").strip()
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if obj.get("name") in TOOLS:
                return [(obj["name"], obj.get("arguments") or obj.get("parameters") or {})]
        except ValueError:
            pass
    return []


NUMBERS = {"ein": 1, "eine": 1, "einen": 1, "einem": 1, "zwei": 2, "drei": 3, "vier": 4,
           "fünf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10}
UNIT_MIN = {"minute": 1, "stunde": 60, "tag": 1440, "woche": 10080}
WD_RE = "(montag|dienstag|mittwoch|donnerstag|freitag|samstag|sonntag)s?"
MONTH_RE = "(" + "|".join(m.lower() for m in MONTHS) + ")"


def _pm(h, text):
    # 1–7 Uhr ohne "morgens/früh/nachts" ist fast immer nachmittags gemeint
    if 1 <= h <= 7 and not re.search(r"morgens|früh|nachts|vormittag", text):
        return h + 12
    return h


def extract_hints(text, today=None):
    """Liest Datum, Uhrzeit, Erinnerung und Suchfilter regelbasiert aus dem Text.

    Kleine Modelle verrechnen sich hier oft – was wir sicher erkennen, gewinnt.
    """
    today = today or dt.date.today()
    t = text.lower()
    h = {}

    # Erinnerung: "4 tage vorher", "eine stunde davor"
    m = re.search(r"(\d+|%s)\s*(minute|stunde|tag|woche)\w*\s*(vorher|davor|zuvor|früher)"
                  % "|".join(NUMBERS), t)
    if m:
        n = int(m.group(1)) if m.group(1).isdigit() else NUMBERS[m.group(1)]
        h["reminder_minutes"] = n * UNIT_MIN[m.group(2)]
        t = t.replace(m.group(0), " ")

    # Uhrzeiten: "von 2-5 uhr", "14:00 bis 15:30", "um 9"
    m = re.search(r"(?:von\s*)?(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr)?\s*(?:-|–|bis)\s*"
                  r"(\d{1,2})(?:[:.](\d{2}))?\s*uhr", t) \
        or re.search(r"von\s*(\d{1,2})(?:[:.](\d{2}))?\s*(?:uhr)?\s*(?:-|–|bis)\s*"
                     r"(\d{1,2})(?:[:.](\d{2}))?", t)
    if m:
        sh, eh = int(m.group(1)), _pm(int(m.group(3)), text.lower())
        if 1 <= sh <= 7 and sh + 12 < eh:
            sh += 12
        h["start_time"] = "%02d:%s" % (sh, m.group(2) or "00")
        h["end_time"] = "%02d:%s" % (eh, m.group(4) or "00")
        t = t.replace(m.group(0), " ")
    else:
        m = re.search(r"\bum\s*(\d{1,2})(?:[:.](\d{2}))?", t) \
            or re.search(r"(\d{1,2})(?:[:.](\d{2}))?\s*uhr", t)
        if m:
            h["start_time"] = "%02d:%s" % (_pm(int(m.group(1)), text.lower()), m.group(2) or "00")
            t = t.replace(m.group(0), " ")

    # Datum
    if re.search(r"\bübermorgen\b", t):
        h["date"] = (today + dt.timedelta(2)).isoformat()
    elif re.search(r"\bmorgen\b", t):
        h["date"] = (today + dt.timedelta(1)).isoformat()
    elif re.search(r"\bheute\b", t):
        h["date"] = today.isoformat()
    m = re.search(r"\b(\d{1,2})\.\s*(?:(\d{1,2})\.|%s)?" % MONTH_RE, t)
    if m and "date" not in h:
        h["day"] = int(m.group(1))
        if m.group(2):
            h["month"] = int(m.group(2))
        elif m.group(3):
            h["month"] = [x.lower() for x in MONTHS].index(m.group(3)) + 1
    m = re.search(r"\b" + WD_RE + r"\b", t)
    if m and "date" not in h:
        h["weekday"] = m.group(1)

    # Dateisuche
    if re.search(r"vorgestern", t):
        h["days_back"] = 3
    elif re.search(r"gestern", t):
        h["days_back"] = 2
    elif re.search(r"heute", t):
        h["days_back"] = 1
    elif re.search(r"(letzte|diese)[nr]? woche|letzten tagen|paar tagen", t):
        h["days_back"] = 7
    elif re.search(r"letzte[nr]? monat", t):
        h["days_back"] = 31
    if re.search(r"herunterg|download", t):
        h["folder"] = "downloads"
    elif re.search(r"schreibtisch|desktop", t):
        h["folder"] = "schreibtisch"
    for pat, ft in [(r"präsentation|praesentation|pptx?|powerpoint|keynote|folien", "praesentation"),
                    (r"\bpdf", "pdf"), (r"tabelle|excel|xlsx", "tabelle"),
                    (r"\bbild|foto|screenshot", "bild"), (r"video", "video"),
                    (r"\bword\b|docx", "dokument")]:
        if re.search(pat, t):
            h["file_type"] = ft
            break
    return h


def apply_hints(name, args, hints):
    if not hints:
        return args
    args = dict(args)
    date_keys = ("date", "day", "month", "weekday")
    if name in ("create_calendar_event", "create_reminder"):
        if any(k in hints for k in date_keys):
            for k in date_keys:
                args.pop(k, None)
                if k in hints:
                    args[k] = hints[k]
        time_key = "start_time" if name == "create_calendar_event" else "time"
        if "start_time" in hints:
            args[time_key] = hints["start_time"]
        if name == "create_calendar_event":
            if "end_time" in hints:
                args["end_time"] = hints["end_time"]
            if "reminder_minutes" in hints:
                args.pop("reminder_days_before", None)
                args["reminder_minutes_before"] = hints["reminder_minutes"]
    elif name == "search_files":
        for k in ("days_back", "folder", "file_type"):
            if k in hints:
                args[k] = hints[k]
        if args.get("folder") not in FOLDERS:
            args["folder"] = "alle"
    return args


def run_tool(name, args):
    if isinstance(args, str):
        args = json.loads(args or "{}")
    fn = TOOLS.get(name)
    if not fn:
        return "Unbekanntes Werkzeug: %s" % name
    args = {k: v for k, v in args.items() if v not in (None, "")}
    try:
        return fn(**args)
    except TypeError as e:
        return "Fehler: falsche Parameter (%s)" % e
    except Exception as e:  # noqa: BLE001 – Fehler geht ans Modell zurück
        return "Fehler: %s" % e


class Chat:
    def __init__(self):
        self.history = []

    def reply(self, user_text):
        self.history.append({"role": "user", "content": user_text})
        self.history = self.history[-20:]
        messages = [{"role": "system", "content": system_prompt()}] + self.history
        hints = extract_hints(user_text)
        actions = []
        for _ in range(5):
            msg = ollama_chat(messages)
            calls = extract_tool_calls(msg)
            if not calls:
                answer = (msg.get("content") or "").strip()
                self.history.append({"role": "assistant", "content": answer})
                return {"answer": answer, "actions": actions}
            messages.append({"role": "assistant", "content": msg.get("content", ""),
                             "tool_calls": msg.get("tool_calls") or []})
            for name, args in calls:
                args = apply_hints(name, args if isinstance(args, dict) else {}, hints)
                result = run_tool(name, args)
                actions.append({"tool": name, "args": args, "result": result})
                messages.append({"role": "tool", "tool_name": name, "content": result})
        return {"answer": "Das hat zu viele Schritte gebraucht – bitte genauer formulieren.",
                "actions": actions}


CHAT = Chat()


# ---------------------------------------------------------------- Webserver

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            self._send(200, (HERE / "index.html").read_bytes(), "text/html")
        else:
            self._send(404, {"error": "not found"})

    def do_POST(self):
        # Nur Anfragen von der eigenen Seite annehmen.
        origin = self.headers.get("Origin", "")
        if origin and origin not in ("http://127.0.0.1:%d" % PORT, "http://localhost:%d" % PORT):
            return self._send(403, {"error": "forbidden"})
        length = int(self.headers.get("Content-Length", 0))
        data = json.loads(self.rfile.read(length) or b"{}")
        try:
            if self.path == "/chat":
                self._send(200, CHAT.reply(data.get("message", "")))
            elif self.path == "/reset":
                CHAT.history.clear()
                self._send(200, {"ok": True})
            elif self.path == "/open":
                self._send(200, {"result": run_tool("open_file", data)})
            else:
                self._send(404, {"error": "not found"})
        except urllib.error.URLError:
            self._send(503, {"error": "Ollama läuft nicht. Starte es mit: ollama serve"})
        except Exception as e:  # noqa: BLE001
            self._send(500, {"error": str(e)})


if __name__ == "__main__":
    print("Assistent läuft auf http://127.0.0.1:%d  (Modell: %s)" % (PORT, MODEL))
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()
