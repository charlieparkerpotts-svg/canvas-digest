#!/usr/bin/env python3
"""
Canvas Digest — emails you what's due today and tomorrow across all your
active Canvas courses, pulled from Canvas's official REST API (not scraped
HTML). Bigger assignments (estimated by points + keyword heuristics) also
get surfaced several days early in a "heads up" section. Optionally dumps
each course's syllabus to a local folder for reference with --sync-syllabi.

Setup: copy .env.example to .env and fill in your values, or set the same
names as real environment variables / GitHub Actions secrets.
"""

import argparse
import os
import smtplib
import sys
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
from zoneinfo import ZoneInfo

import requests

REQUIRED_ENV = ["CANVAS_BASE_URL", "CANVAS_API_TOKEN"]
REQUIRED_ENV_FOR_EMAIL = ["EMAIL_FROM", "GMAIL_APP_PASSWORD"]

# --- Effort estimate heuristic ---------------------------------------------
# Canvas has no "how long will this take" field, so this is a rough guess
# built from points_possible plus keyword hints in the title/description.
# It's meant to flag things worth starting early, not to be precise. Tune
# these constants directly if the estimates feel off for your courses.
POINTS_PER_HOUR = 10  # e.g. a 50-point assignment ≈ 5 hours baseline
MIN_HOURS = 0.25
HEAVY_KEYWORDS = [
    "project", "paper", "essay", "presentation", "research", "portfolio",
    "case study", "report", "final exam", "midterm", "capstone", "proposal",
]
HEAVY_MULTIPLIER = 1.5
HEAVY_MIN_HOURS = 3  # a "project"/"paper" is assumed at least this long even at 0 points
LIGHT_KEYWORDS = ["quiz", "discussion", "reading response", "worksheet", "reflection"]
LIGHT_CAP_HOURS = 1.5  # quizzes/discussions rarely take longer than this

# Maps estimated hours -> how many days before the due date to start
# surfacing the assignment in a "heads up" section (beyond the normal
# today/tomorrow window).
LEAD_TIME_BREAKPOINTS = [
    (2, 0),           # < 2h: no extra advance notice
    (4, 2),           # 2-4h: mention starting 2 days out
    (8, 4),           # 4-8h: mention starting 4 days out
    (float("inf"), 7),  # 8h+: mention starting a week out
]

# "DUE NEXT WEEK" section covers everything due from the day after tomorrow
# through this many days out.
NEXT_WEEK_WINDOW_DAYS = 8


def estimate_hours(assignment):
    points = assignment.get("points_possible") or 0
    text = f"{assignment.get('name') or ''} {assignment.get('description') or ''}".lower()

    hours = points / POINTS_PER_HOUR
    if any(k in text for k in HEAVY_KEYWORDS):
        hours = max(hours, HEAVY_MIN_HOURS) * HEAVY_MULTIPLIER
    if any(k in text for k in LIGHT_KEYWORDS):
        hours = min(hours, LIGHT_CAP_HOURS)

    return round(max(hours, MIN_HOURS), 1)


def lead_days_for(hours):
    for threshold, days in LEAD_TIME_BREAKPOINTS:
        if hours < threshold:
            return days
    return LEAD_TIME_BREAKPOINTS[-1][1]


def load_config(require_email=True):
    required = REQUIRED_ENV + (REQUIRED_ENV_FOR_EMAIL if require_email else [])
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")

    email_from = os.environ.get("EMAIL_FROM", "")
    return {
        "base_url": os.environ["CANVAS_BASE_URL"].rstrip("/"),
        "token": os.environ["CANVAS_API_TOKEN"],
        "email_from": email_from,
        "email_to": os.environ.get("EMAIL_TO", email_from),
        "gmail_app_password": os.environ.get("GMAIL_APP_PASSWORD", ""),
        "tz": ZoneInfo(os.environ.get("TIMEZONE", "America/New_York")),
        "send_if_empty": os.environ.get("SEND_IF_EMPTY", "true").lower() != "false",
    }


def canvas_get_all(session, base_url, path, params=None):
    """GET a Canvas API path, following Link-header pagination, and return
    the combined list of results."""
    url = f"{base_url}/api/v1{path}"
    results = []
    while url:
        resp = session.get(url, params=params, timeout=30)
        resp.raise_for_status()
        results.extend(resp.json())
        params = None  # only needed on the first request; 'next' url has them baked in
        url = resp.links.get("next", {}).get("url")
    return results


def get_active_courses(session, base_url):
    courses = canvas_get_all(
        session,
        base_url,
        "/courses",
        params={"enrollment_state": "active", "per_page": 100},
    )
    return [c for c in courses if c.get("name")]


def get_course_assignments(session, base_url, course_id):
    return canvas_get_all(
        session,
        base_url,
        f"/courses/{course_id}/assignments",
        params={"per_page": 100, "order_by": "due_at"},
    )


def parse_due_at(due_at_str):
    # Canvas returns ISO8601 UTC, e.g. "2026-09-22T03:59:00Z"
    return datetime.fromisoformat(due_at_str.replace("Z", "+00:00"))


def collect_digest_items(session, config):
    """Return (today_items, tomorrow_items, next_week_items, upcoming_items).

    today_items / tomorrow_items: (course_name, assignment_name, due_local, url, hours)
    next_week_items: same plus days_until — everything due from the day after
    tomorrow through NEXT_WEEK_WINDOW_DAYS out, ranked by importance.
    upcoming_items: same shape as next_week_items — bigger assignments (by
    estimated hours) that fall inside their own lead-time window but aren't
    due today/tomorrow yet."""
    tz = config["tz"]
    today = datetime.now(tz).date()

    today_items, tomorrow_items, next_week_items, upcoming_items = [], [], [], []

    courses = get_active_courses(session, config["base_url"])
    for course in courses:
        assignments = get_course_assignments(session, config["base_url"], course["id"])
        for a in assignments:
            due_at = a.get("due_at")
            if not due_at:
                continue
            due_local = parse_due_at(due_at).astimezone(tz)
            days_until = (due_local.date() - today).days
            if days_until < 0:
                continue

            hours = estimate_hours(a)
            url = a.get("html_url", "")

            if days_until == 0:
                today_items.append((course["name"], a["name"], due_local, url, hours))
            elif days_until == 1:
                tomorrow_items.append((course["name"], a["name"], due_local, url, hours))
            elif days_until <= lead_days_for(hours):
                upcoming_items.append((course["name"], a["name"], due_local, url, hours, days_until))

            if 2 <= days_until <= NEXT_WEEK_WINDOW_DAYS:
                next_week_items.append((course["name"], a["name"], due_local, url, hours, days_until))

    today_items.sort(key=lambda x: x[2])
    tomorrow_items.sort(key=lambda x: x[2])
    next_week_items.sort(key=lambda x: (-x[4], x[2]))  # most estimated hours first, then soonest due
    upcoming_items.sort(key=lambda x: x[2])
    return today_items, tomorrow_items, next_week_items, upcoming_items


def format_section(title, items):
    if not items:
        return f"{title}\n  Nothing due.\n"
    lines = [title]
    for course_name, assignment_name, due_local, url, hours in items:
        time_str = due_local.strftime("%I:%M %p").lstrip("0")
        lines.append(f"  [{time_str}] {course_name} — {assignment_name} (~{hours}h)")
        if url:
            lines.append(f"      {url}")
    return "\n".join(lines) + "\n"


def format_next_week_section(items):
    if not items:
        return "DUE NEXT WEEK\n  Nothing due.\n"
    lines = ["DUE NEXT WEEK (ranked by importance)"]
    for i, (course_name, assignment_name, due_local, url, hours, days_until) in enumerate(items, start=1):
        day_str = due_local.strftime("%a %b %d")
        lines.append(f"  {i}. {course_name} — {assignment_name} (~{hours}h, due {day_str})")
        if url:
            lines.append(f"      {url}")
    return "\n".join(lines) + "\n"


def format_upcoming_section(items):
    if not items:
        return ""
    lines = ["HEADS UP — BIGGER THINGS COMING"]
    for course_name, assignment_name, due_local, url, hours, days_until in items:
        day_str = due_local.strftime("%a %b %d")
        lines.append(
            f"  Due in {days_until} days ({day_str}): {course_name} — {assignment_name} (~{hours}h)"
        )
        if url:
            lines.append(f"      {url}")
    return "\n".join(lines) + "\n"


def build_email_body(today_items, tomorrow_items, next_week_items, upcoming_items):
    parts = [
        format_section("DUE TODAY", today_items),
        format_section("DUE TOMORROW", tomorrow_items),
        format_next_week_section(next_week_items),
    ]
    upcoming_section = format_upcoming_section(upcoming_items)
    if upcoming_section:
        parts.append(upcoming_section)
    return "\n".join(parts)


def send_email(config, subject, body):
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = config["email_from"]
    msg["To"] = config["email_to"]

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(config["email_from"], config["gmail_app_password"])
        server.sendmail(config["email_from"], config["email_to"].split(","), msg.as_string())


def sync_syllabi(session, config, out_dir="syllabi"):
    """Best-effort: dump each active course's syllabus HTML to a local file
    for reference. Syllabus text is freeform, so it is NOT parsed for due
    dates — the Assignments API above is the reliable source for those."""
    os.makedirs(out_dir, exist_ok=True)
    courses = canvas_get_all(
        session,
        config["base_url"],
        "/courses",
        params={"enrollment_state": "active", "per_page": 100, "include[]": "syllabus_body"},
    )
    for course in courses:
        body = course.get("syllabus_body") or "(no syllabus posted)"
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in course["name"])
        path = os.path.join(out_dir, f"{safe_name}.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        print(f"Saved syllabus: {path}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sync-syllabi",
        action="store_true",
        help="Also dump each course's syllabus HTML to a local ./syllabi folder",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the digest instead of emailing it",
    )
    args = parser.parse_args()

    config = load_config(require_email=not args.dry_run)
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {config['token']}"})

    if args.sync_syllabi:
        sync_syllabi(session, config)

    today_items, tomorrow_items, next_week_items, upcoming_items = collect_digest_items(session, config)
    body = build_email_body(today_items, tomorrow_items, next_week_items, upcoming_items)
    subject = f"Canvas Digest — {datetime.now(config['tz']).strftime('%A, %B %d')}"

    if args.dry_run:
        print(subject)
        print(body)
        return

    if (
        not today_items
        and not tomorrow_items
        and not next_week_items
        and not upcoming_items
        and not config["send_if_empty"]
    ):
        print("Nothing due today or tomorrow; SEND_IF_EMPTY=false, skipping email.")
        return

    send_email(config, subject, body)
    print("Digest email sent.")


if __name__ == "__main__":
    main()
