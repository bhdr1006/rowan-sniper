"""
Rowan Course Sniper - checker

Pulls every section of a term from Rowan's Section Tally, compares it with the
previous snapshot, and sends a push notification (via ntfy) to everyone who
subscribed to a section the moment a seat opens up.

Each section has its own ntfy topic:  <TOPIC_PREFIX>-<TERM_CODE>-<CRN>
Anyone subscribed to that topic in the ntfy app gets the alert.

Only uses the Python standard library.
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SECTION_TALLY = "https://sectiontally.rowan.edu"
SECTIONS_URL = SECTION_TALLY + "/section-tally/get-sections/"
REGISTER_URL = "https://ssb.rowan.edu/"

TERM_CODE = os.environ.get("TERM_CODE", "202720")          # 202720 = Spring 2027
TOPIC_PREFIX = os.environ.get("TOPIC_PREFIX", "rowan-sniper")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "0"))     # 0 = check once and exit
INTERVAL_SECONDS = int(os.environ.get("INTERVAL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "data/sections.json")
PAGE_SIZE = 500

# Safety limits: if the data looks broken, don't spam everyone.
MAX_ALERTS_PER_CHECK = 150
MIN_ROWS_RATIO = 0.5

# Column list copied from Section Tally's own table, so our request looks like
# the one their page sends.
COLUMNS = [
    "", "crn", "subject", "course_number", "section_sequence", "module",
    "session_type", "course_title", "", "formatted_meeting_times", "campus",
    "credit_hours", "available", "enrolled", "max_seats", "wait_available",
    "wait_count", "wait_capacity", "max_reserved", "left_reserved",
    "room_capacity", "cancelled_indicator", "term_code",
]

KEEP_FIELDS = [
    "crn", "subject", "course_number", "section_sequence", "course_title",
    "primary_professor", "formatted_meeting_times", "campus", "session_type",
    "available", "enrolled", "max_seats", "wait_available", "wait_count",
    "wait_capacity", "cancelled_indicator",
]


def log(msg):
    print(f"[{datetime.now(timezone.utc):%H:%M:%S}] {msg}", flush=True)


def to_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_params(start):
    params = [
        ("draw", "1"),
        ("start", str(start)),
        ("length", str(PAGE_SIZE)),
        ("search[value]", ""),
        ("search[regex]", "false"),
        ("order[0][column]", "2"),
        ("order[0][dir]", "asc"),
        ("term_code", TERM_CODE),
    ]
    for i, name in enumerate(COLUMNS):
        params += [
            (f"columns[{i}][data]", name),
            (f"columns[{i}][name]", "instructor" if i == 8 else ""),
            (f"columns[{i}][searchable]", "true"),
            (f"columns[{i}][orderable]", "true"),
            (f"columns[{i}][search][value]", ""),
            (f"columns[{i}][search][regex]", "false"),
        ]
    return params


def get_json(url):
    req = urllib.request.Request(url, headers={
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"{SECTION_TALLY}/sections?term_code={TERM_CODE}",
        "User-Agent": "Mozilla/5.0 (RowanCourseSniper; student project)",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_all_sections():
    rows, start = [], 0
    while True:
        url = SECTIONS_URL + "?" + urllib.parse.urlencode(build_params(start))
        payload = get_json(url)
        page = payload.get("data") or []
        rows.extend(page)
        total = payload.get("recordsFiltered") or payload.get("recordsTotal") or 0
        if not page or len(rows) >= to_int(total):
            break
        start += PAGE_SIZE
        time.sleep(1)  # be gentle with Rowan's server
    sections = {}
    for row in rows:
        crn = str(row.get("crn", "")).strip()
        if crn:
            sections[crn] = {k: row.get(k) for k in KEEP_FIELDS}
    return sections


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("term_code") == TERM_CODE:
            return data.get("sections", {})
    except (OSError, ValueError):
        pass
    return {}


def save_state(sections):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    data = {
        "term_code": TERM_CODE,
        "topic_prefix": TOPIC_PREFIX,
        "ntfy_server": NTFY_SERVER,
        "updated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "sections": sections,
    }
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, separators=(",", ":"))
    os.replace(tmp, STATE_FILE)


def topic_for(crn):
    return f"{TOPIC_PREFIX}-{TERM_CODE}-{crn}"


def ascii_only(text):
    return str(text).encode("ascii", "ignore").decode("ascii")


def find_alerts(old, new):
    """Return a list of (kind, section) where kind is 'seat' or 'waitlist'."""
    alerts = []
    for crn, sec in new.items():
        before = old.get(crn)
        if not before or sec.get("cancelled_indicator") is True:
            continue
        was_open = to_int(before.get("available")) > 0
        now_open = to_int(sec.get("available")) > 0
        if now_open and not was_open:
            alerts.append(("seat", sec))
            continue
        wl_was = to_int(before.get("wait_available")) > 0
        wl_now = to_int(sec.get("wait_available")) > 0
        if not now_open and wl_now and not wl_was:
            alerts.append(("waitlist", sec))
    return alerts


def send_alert(kind, sec):
    crn = sec["crn"]
    name = f"{sec.get('subject')} {sec.get('course_number')}-{sec.get('section_sequence')}"
    if kind == "seat":
        title = f"SEAT OPEN: {name} (CRN {crn})"
        body = (f"{sec.get('course_title')}\n"
                f"{to_int(sec.get('available'))} of {to_int(sec.get('max_seats'))} seats open right now.\n"
                f"Register fast in Self-Service Banner with CRN {crn}.")
        priority, tags = "urgent", "rotating_light"
    else:
        title = f"Waitlist spot open: {name} (CRN {crn})"
        body = (f"{sec.get('course_title')}\n"
                f"Class is still full, but {to_int(sec.get('wait_available'))} waitlist spot(s) opened.\n"
                f"Join the waitlist in Self-Service Banner with CRN {crn}.")
        priority, tags = "high", "hourglass"
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{topic_for(crn)}",
        data=body.encode("utf-8"),
        method="POST",
        headers={
            "Title": ascii_only(title),
            "Priority": priority,
            "Tags": tags,
            "Click": REGISTER_URL,
            "Actions": f"view, Open Banner, {REGISTER_URL}",
        },
    )
    try:
        urllib.request.urlopen(req, timeout=20).read()
        log(f"  sent {kind} alert -> {topic_for(crn)}")
    except urllib.error.URLError as e:
        log(f"  could not send alert for {crn}: {e}")


def check_once(old):
    new = fetch_all_sections()
    if not new:
        raise RuntimeError("Section Tally returned no sections")
    if old and len(new) < len(old) * MIN_ROWS_RATIO:
        raise RuntimeError(f"Only got {len(new)} sections (had {len(old)}); skipping this round")
    alerts = find_alerts(old, new) if old else []
    log(f"{len(new)} sections, {len(alerts)} openings")
    if len(alerts) > MAX_ALERTS_PER_CHECK:
        log(f"  {len(alerts)} openings at once looks like a data glitch - not sending")
    else:
        for kind, sec in alerts:
            send_alert(kind, sec)
    save_state(new)
    return new


def main():
    state = load_state()
    log(f"term {TERM_CODE}, {len(state)} sections in previous snapshot")
    deadline = time.time() + RUN_MINUTES * 60
    failures = 0
    while True:
        try:
            state = check_once(state)
            failures = 0
        except Exception as e:  # keep going on network hiccups
            failures += 1
            log(f"check failed ({failures}): {e}")
            if failures >= 5 and RUN_MINUTES == 0:
                sys.exit(1)
        if time.time() + INTERVAL_SECONDS >= deadline:
            break
        time.sleep(INTERVAL_SECONDS)
    if not state:
        sys.exit(1)


if __name__ == "__main__":
    main()
