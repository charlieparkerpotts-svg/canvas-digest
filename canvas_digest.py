#!/usr/bin/env python3
"""
Canvas Digest — emails you what's due today and tomorrow across all your
active Canvas courses, pulled from Canvas's official REST API (not scraped
HTML). Optionally dumps each course's syllabus to a local folder for
reference with --sync-syllabi.

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

REQUIRED_ENV = ["CANVAS_BASE_URL", "CANVAS_API_TOKEN", "EMAIL_FROM", "GMAIL_APP_PASSWORD"]


def load_config():
    missing = [name for name in REQUIRED_ENV if not os.environ.get(name)]
    if missing:
        sys.exit(f"Missing required environment variables: {', '.join(missing)}")

    return {
        "base_url": os.environ["CANVAS_BASE_URL"].rstrip("/"),
        "token": os.environ["CANVAS_API_TOKEN"],
        "email_from": os.environ["EMAIL_FROM"],
        "email_to": os.environ.get("EMAIL_TO", os.environ["EMAIL_FROM"]),
        "gmail_app_password": os.environ["GMAIL_APP_PASSWORD"],
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


def collect_due_assignments(session, config):
    """Return (today_items, tomorrow_items), each a list of
    (course_name, assignment_title, local_due_dt, html_url)."""
    tz = config["tz"]
    now_local = datetime.now(tz)
    today = now_local.date()
    tomorrow = today + timedelta(days=1)

    today_items, tomorrow_items = [], []

    courses = get_active_courses(session, config["base_url"])
    for course in courses:
        assignments = get_course_assignments(session, config["base_url"], course["id"])
        for a in assignments:
            due_at = a.get("due_at")
            if not due_at:
                continue
            due_local = parse_due_at(due_at).astimezone(tz)
            item = (course["name"], a["name"], due_local, a.get("html_url", ""))
            if due_local.date() == today:
                today_items.append(item)
            elif due_local.date() == tomorrow:
                tomorrow_items.append(item)

    today_items.sort(key=lambda x: x[2])
    tomorrow_items.sort(key=lambda x: x[2])
    return today_items, tomorrow_items


def format_section(title, items):
    if not items:
        return f"{title}\n  Nothing due.\n"
    lines = [title]
    for course_name, assignment_name, due_local, url in items:
        lines.append(f"  [{due_local.strftime('%I:%M %p').lstrip('0')}] {course_name} — {assignment_name}")
        if url:
            lines.append(f"      {url}")
    return "\n".join(lines) + "\n"


def build_email_body(today_items, tomorrow_items):
    parts = [
        format_section("DUE TODAY", today_items),
        format_section("DUE TOMORROW", tomorrow_items),
    ]
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

    config = load_config()
    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {config['token']}"})

    if args.sync_syllabi:
        sync_syllabi(session, config)

    today_items, tomorrow_items = collect_due_assignments(session, config)
    body = build_email_body(today_items, tomorrow_items)
    subject = f"Canvas Digest — {datetime.now(config['tz']).strftime('%A, %B %d')}"

    if args.dry_run:
        print(subject)
        print(body)
        return

    if not today_items and not tomorrow_items and not config["send_if_empty"]:
        print("Nothing due today or tomorrow; SEND_IF_EMPTY=false, skipping email.")
        return

    send_email(config, subject, body)
    print("Digest email sent.")


if __name__ == "__main__":
    main()
