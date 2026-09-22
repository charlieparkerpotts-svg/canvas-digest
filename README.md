# Canvas Digest

Emails you what's due **today** and **tomorrow** across all your active Canvas
courses, every morning, automatically — plus a heads-up on bigger assignments
several days before they're due. Uses Canvas's official REST API (not HTML
scraping), so it keeps working even if Canvas changes its page layout.

No Claude/AI subscription needed to run this — it's a plain Python script.

## What it does

- Pulls your active courses and their assignments from the Canvas API
- Filters to anything due today or tomorrow (in your timezone)
- Estimates how long each assignment will take (from its points value plus
  keyword hints like "project" or "quiz" in the title/description), and if
  that estimate is big enough, surfaces it in a **"heads up"** section days
  before it's actually due — so a paper doesn't blindside you the night
  before. This is a rough heuristic, not a real time estimate; tune the
  constants near the top of `canvas_digest.py` (`POINTS_PER_HOUR`,
  `HEAVY_KEYWORDS`, `LEAD_TIME_BREAKPOINTS`, etc.) if it's off for your courses.
- Emails you a short digest, grouped into Due Today / Due Tomorrow / Heads Up
- Optional: `--sync-syllabi` saves each course's syllabus HTML locally for
  reference (syllabus text is freeform, so it's not parsed for dates — the
  Assignments API is the reliable source for those)

## 1. Get a Canvas API token

1. Log into Canvas → **Account** → **Settings**
2. Scroll to **Approved Integrations** → **+ New Access Token**
3. Give it a name, leave expiry blank (or set one — you'll just need to
   regenerate it later), click **Generate Token**
4. Copy it now — Canvas only shows it once

Also note your Canvas URL, e.g. `https://yourschool.instructure.com`.

## 2. Get a Gmail App Password

You need a Gmail account with 2-Step Verification turned on.

1. Go to https://myaccount.google.com/apppasswords
2. Create an app password (name it "canvas-digest")
3. Copy the 16-character password

This lets the script send mail *as* that Gmail account without your real
login password.

## 3. Run it locally (optional, to test)

```bash
git clone <this-repo-url>
cd canvas-digest
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env
# edit .env with your values
```

Then, to load `.env` and do a dry run (prints instead of emailing):

```bash
export $(grep -v '^#' .env | xargs)   # Windows: use a tool like dotenv or set vars manually
python canvas_digest.py --dry-run
```

Once that looks right, drop `--dry-run` to actually send the email.

## 4. Automate it with GitHub Actions (free)

This repo includes `.github/workflows/daily-digest.yml`, which runs the
script every day on GitHub's free schedule runner — no server needed.

1. Fork this repo (or push your own copy to GitHub)
2. In your repo: **Settings → Secrets and variables → Actions → New repository secret**
   Add each of these:
   - `CANVAS_BASE_URL`
   - `CANVAS_API_TOKEN`
   - `EMAIL_FROM`
   - `EMAIL_TO` (can be the same as `EMAIL_FROM`, or a different address)
   - `GMAIL_APP_PASSWORD`
   - `TIMEZONE` (e.g. `America/New_York`)
3. That's it — it'll run daily at the time set in the workflow file. Adjust
   the `cron:` line in `.github/workflows/daily-digest.yml` if you want a
   different time (cron times are UTC).
4. To test immediately instead of waiting: go to the **Actions** tab →
   **Canvas Daily Digest** → **Run workflow**.

## Sharing this with friends

Anyone can use this without any Claude/AI subscription — they just need:
- Their own Canvas API token
- A Gmail account (or their own) with an app password
- To fork this repo and set their own GitHub Actions secrets (step 4 above)

Nobody's credentials are shared or stored centrally — each fork only holds
that person's own secrets, private to their GitHub account.

## Notes / limitations

- Only shows assignments that have a due date set in Canvas.
- Ungraded discussions/pages without a due date won't appear (Canvas doesn't
  expose a due date for those via the Assignments API).
- Syllabus sync (`--sync-syllabi`) is reference-only; it does not feed into
  the due-date digest.
