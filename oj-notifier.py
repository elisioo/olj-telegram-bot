"""OnlineJobs.ph -> Telegram job notifier with interactive slash searches."""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
SEARCH_URL = os.environ.get("OJ_SEARCH_URL", "https://www.onlinejobs.ph/jobseekers/jobsearch")
KEYWORDS = [key.strip().lower() for key in os.environ.get("OJ_KEYWORDS", "").split(",") if key.strip()]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "600"))
SEEN_FILE = Path(__file__).parent / "seen_jobs.json"
PROFILE_IMAGE = Path(__file__).parent / "asset" / "OLJ_BOT.jpg"
MAX_SEARCH_RESULTS = 5
MAX_RECENT_JOBS = 5

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("oj-notifier")


def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


def fetch_jobs() -> list[dict]:
    """Fetch job listing cards, including their short description and tags."""
    response = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    jobs = {}
    for anchor in soup.find_all("a", href=re.compile(r"^/jobseekers/job/")):
        # OnlineJobs occasionally returns malformed anchor tags with no attrs.
        # Ignore those rather than letting one bad tag stop the notifier.
        href = (anchor.attrs or {}).get("href")
        if not href:
            continue
        text = anchor.get_text(" ", strip=True)
        if "Posted on" not in text:
            continue
        job_id_match = re.search(r"-(\d+)$", href)
        if not job_id_match:
            continue

        card = anchor.select_one(".jobpost-cat-box")
        title, work_type, posted, salary = text, "", "", ""
        overview, tags = "", []
        if card:
            # Extract each field from its dedicated element.  Parsing the full
            # card text makes the title accidentally include every detail.
            title_element = card.select_one("h4")
            if title_element:
                work_badge = title_element.select_one(".badge")
                if work_badge:
                    work_type = work_badge.get_text(" ", strip=True)
                    work_badge.decompose()
                title = title_element.get_text(" ", strip=True)
            posted_element = card.select_one("em")
            if posted_element:
                posted = re.sub(r"^Posted on\s*", "", posted_element.get_text(" ", strip=True)).strip()
            salary_element = card.select_one("dl dd")
            if salary_element:
                salary = salary_element.get_text(" ", strip=True)
            description = card.select_one(".desc")
            if description:
                see_more = description.find("a")
                if see_more:
                    see_more.decompose()
                overview = description.get_text(" ", strip=True)
            tags = [tag.get_text(" ", strip=True) for tag in card.select(".job-tag .badge")]

        # Preserve a reasonable result if OnlineJobs changes its card markup.
        if not card or not work_type:
            match = re.match(r"(.*?)\s+(Full Time|Part Time|Gig|Any)\s+\*?Posted on ([\d\-: ]+)\*?\s*(.*)", text)
            if match:
                fallback_title, fallback_type, fallback_posted, fallback_salary = match.groups()
                title = title if card and title else fallback_title
                work_type = work_type or fallback_type
                posted = posted or fallback_posted
                salary = salary or fallback_salary

        job_id = job_id_match.group(1)
        jobs[job_id] = {
            "id": job_id,
            "title": title.strip(),
            "type": work_type.strip(),
            "posted": posted.strip(),
            "salary": salary.strip(),
            "overview": overview,
            "tags": tags,
            "url": "https://www.onlinejobs.ph" + href,
        }
    return list(jobs.values())


def fetch_job_details(job: dict) -> dict:
    """Add data shown only on the individual posting page."""
    try:
        response = requests.get(job["url"], headers=HEADERS, timeout=20)
        response.raise_for_status()
    except requests.RequestException as exc:
        log.warning("Could not load details for %s: %s", job["id"], exc)
        return job

    soup = BeautifulSoup(response.text, "html.parser")
    fields = {}
    for heading in soup.select("h3"):
        value = heading.find_next_sibling("p")
        if value:
            fields[heading.get_text(" ", strip=True).upper()] = value.get_text(" ", strip=True)
    description = soup.select_one("#job-description")
    if description:
        job["overview"] = description.get_text(" ", strip=True)
    job["type"] = fields.get("TYPE OF WORK", job.get("type", ""))
    job["salary"] = fields.get("WAGE / SALARY", job.get("salary", ""))
    job["hours"] = fields.get("HOURS PER WEEK", job.get("hours", ""))
    job["updated"] = fields.get("DATE UPDATED", job.get("updated", ""))
    return job


def matches_keywords(job: dict) -> bool:
    if not KEYWORDS:
        return True
    searchable = " ".join([job["title"], job.get("overview", ""), " ".join(job.get("tags", []))]).lower()
    return any(keyword in searchable for keyword in KEYWORDS)


def format_message(job: dict) -> str:
    """Render the requested Telegram job-card layout."""
    overview = job.get("overview") or "Not provided."
    if len(overview) > 700:
        overview = overview[:697].rsplit(" ", 1)[0] + "..."
    return "\n".join([
        f"\U0001f4cc <b>{escape(job['title'])}</b>",
        f"\U0001f4bc <b>Type of work:</b> {escape(job.get('type') or 'Not provided')}",
        f"\U0001f4b0 <b>Wage/salary:</b> {escape(job.get('salary') or 'Not provided')}",
        f"\u23f1\ufe0f <b>Hours per week:</b> {escape(job.get('hours') or 'Not provided')}",
        f"\U0001f4c5 <b>Date updated:</b> {escape(job.get('updated') or 'Not provided')}",
        f"\U0001f552 <b>Time uploaded:</b> {escape(job.get('posted') or 'Not provided')}",
        "",
        f"\U0001f4cb <b>Job overview</b>\n{escape(overview)}",
        "",
        f"\U0001f517 <b>Job link:</b> <a href=\"{escape(job['url'], quote=True)}\">Open job posting</a>",
    ])


def send_telegram_message_to(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    response = requests.post(url, data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"}, timeout=15)
    if not response.ok:
        log.error("Telegram send failed: %s", response.text)


def send_telegram_message(text: str):
    send_telegram_message_to(CHAT_ID, text)


def set_bot_profile_photo():
    """Upload the bundled OLJ BOT image as the Telegram bot's profile photo."""
    if not PROFILE_IMAGE.exists():
        raise SystemExit(f"Profile image not found: {PROFILE_IMAGE}")
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/setMyProfilePhoto"
    with PROFILE_IMAGE.open("rb") as image:
        response = requests.post(
            url,
            data={"photo": json.dumps({"type": "static", "photo": "attach://profile_photo"})},
            files={"profile_photo": (PROFILE_IMAGE.name, image, "image/jpeg")},
            timeout=30,
        )
    if not response.ok:
        raise SystemExit(f"Telegram profile-photo upload failed: {response.text}")
    log.info("Bot profile photo updated using %s", PROFILE_IMAGE.name)


def welcome_message() -> str:
    """Explain the bot and its commands for first-time users."""
    return "\n".join([
        "\U0001f44b <b>Welcome to the OnlineJobs.ph Notifier!</b>",
        "",
        "Created by @dobidapda, this bot monitors OnlineJobs.ph for new job posts that match your configured keywords.",
        "",
        "I watch OnlineJobs.ph and send you matching new job posts with the job type, pay, hours, overview, and a direct link.",
        "",
        "<b>Commands</b>",
        "\U0001f504 <b>/refresh</b> - Check right now for new job posts.",
        "\U0001f550 <b>/recent</b> - View the five most recent job posts.",
        "\U0001f4c5 <b>/view_now</b> - View every job updated today.",
        "\U0001f50e <b>/keyword</b> - Search current jobs by keyword.",
        "\U0001f4ac <b>/help</b> - Show this guide again.",
        "",
        "<b>How to search</b>",
        "Send <b>/video_editor</b> for one-word searches, or <b>/search video editor</b> for phrases.",
        "",
        "\U0001f4a1 Example: <b>/virtual_assistant</b>",
    ])


def search_jobs(keyword: str) -> list[dict]:
    """Search title, listing overview, and tags for a slash-command keyword."""
    terms = keyword.replace("_", " ").strip().lower().split()
    if not terms:
        return []
    matches = []
    for job in fetch_jobs():
        searchable = " ".join([job["title"], job.get("overview", ""), " ".join(job.get("tags", []))]).lower()
        if all(term in searchable for term in terms):
            matches.append(job)
    return matches[:MAX_SEARCH_RESULTS]


def send_search_results(chat_id: str, keyword: str):
    try:
        jobs = search_jobs(keyword)
    except requests.RequestException as exc:
        log.error("Search failed: %s", exc)
        send_telegram_message_to(chat_id, "\u26a0\ufe0f I couldn't search OnlineJobs right now. Please try again shortly.")
        return
    if not jobs:
        send_telegram_message_to(chat_id, f"\U0001f50e No current jobs found for <b>{escape(keyword)}</b>.")
        return
    send_telegram_message_to(chat_id, f"\U0001f50e Found <b>{len(jobs)}</b> job(s) for <b>{escape(keyword)}</b>:")
    for job in jobs:
        send_telegram_message_to(chat_id, format_message(fetch_job_details(job)))


def refresh_jobs(seen: set, chat_id: str) -> set:
    """Check now for unseen jobs and report the result to the requesting chat."""
    try:
        jobs = fetch_jobs()
    except requests.RequestException as exc:
        log.error("Refresh failed: %s", exc)
        send_telegram_message_to(chat_id, "\u26a0\ufe0f I couldn't refresh OnlineJobs right now. Please try again shortly.")
        return seen

    new_jobs = [job for job in jobs if job["id"] not in seen and matches_keywords(job)]
    if not new_jobs:
        send_telegram_message_to(chat_id, "\U0001f60a Nothing's new still you :> Want me to show the latest job for today? Just type the command /view_now")
    else:
        for job in reversed(new_jobs):
            send_telegram_message_to(chat_id, format_message(fetch_job_details(job)))
            time.sleep(1)
    # Mark every listing as seen, including jobs excluded by OJ_KEYWORDS.
    seen.update(job["id"] for job in jobs)
    save_seen(seen)
    return seen


def send_recent_jobs(chat_id: str):
    """Send the five newest listings without changing notification state."""
    try:
        jobs = fetch_jobs()[:MAX_RECENT_JOBS]
    except requests.RequestException as exc:
        log.error("Recent-jobs request failed: %s", exc)
        send_telegram_message_to(chat_id, "\u26a0\ufe0f I couldn't load recent jobs right now. Please try again shortly.")
        return
    if not jobs:
        send_telegram_message_to(chat_id, "\U0001f4ed No recent jobs found right now.")
        return
    send_telegram_message_to(chat_id, f"\U0001f550 <b>{len(jobs)} most recent job posts:</b>")
    for job in jobs:
        send_telegram_message_to(chat_id, format_message(fetch_job_details(job)))
        time.sleep(1)


def send_jobs_updated_today(chat_id: str):
    """Send all current listings updated on OnlineJobs' newest displayed date."""
    try:
        jobs = fetch_jobs()
    except requests.RequestException as exc:
        log.error("View-now request failed: %s", exc)
        send_telegram_message_to(chat_id, "\u26a0\ufe0f I couldn't load today's updated jobs right now. Please try again shortly.")
        return

    detailed_jobs = [fetch_job_details(job) for job in jobs]

    def update_date(job: dict) -> datetime:
        try:
            return datetime.strptime(job.get("updated", ""), "%b %d, %Y")
        except ValueError:
            return datetime.min

    newest_date = max((update_date(job) for job in detailed_jobs), default=datetime.min)
    if newest_date == datetime.min:
        send_telegram_message_to(chat_id, "\U0001f4ed No updated job posts found right now.")
        return
    updated_today = [job for job in detailed_jobs if update_date(job).date() == newest_date.date()]
    date_label = next(job["updated"] for job in updated_today if job.get("updated"))
    send_telegram_message_to(chat_id, f"\U0001f4c5 <b>{len(updated_today)} job post(s) updated on {escape(date_label)}:</b>")
    for job in updated_today:
        send_telegram_message_to(chat_id, format_message(job))
        time.sleep(1)


def process_commands(offset: int | None, seen: set) -> tuple[int | None, set]:
    """Handle /keyword, /refresh, /recent, and /view_now while running."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        response = requests.get(url, params={"offset": offset, "timeout": 1}, timeout=5)
        response.raise_for_status()
        updates = response.json().get("result", [])
    except requests.RequestException as exc:
        log.warning("Could not check Telegram commands: %s", exc)
        return offset, seen
    for update in updates:
        offset = update["update_id"] + 1
        message = update.get("message", {})
        text = message.get("text", "").strip()
        chat_id = str(message.get("chat", {}).get("id", ""))
        # Keep interactive search private to the configured recipient.
        if not text.startswith("/") or chat_id != CHAT_ID:
            continue
        command = text.split()[0].split("@", 1)[0].lower()
        if command in {"/start", "/help"}:
            send_telegram_message_to(chat_id, welcome_message())
        elif command == "/refresh":
            seen = refresh_jobs(seen, chat_id)
        elif command == "/recent":
            send_recent_jobs(chat_id)
        elif command == "/view_now":
            send_jobs_updated_today(chat_id)
        elif command == "/search" and len(text.split(maxsplit=1)) == 2:
            send_search_results(chat_id, text.split(maxsplit=1)[1])
        else:
            send_search_results(chat_id, command[1:])
    return offset, seen


def run_once(seen: set) -> set:
    try:
        jobs = fetch_jobs()
    except requests.RequestException as exc:
        log.error("Fetch failed: %s", exc)
        return seen
    if not jobs:
        log.warning("No jobs parsed - the site's HTML may have changed.")
        return seen
    new_jobs = [job for job in jobs if job["id"] not in seen and matches_keywords(job)]
    for job in reversed(new_jobs):
        log.info("New job: %s", job["title"])
        send_telegram_message(format_message(fetch_job_details(job)))
        time.sleep(1)
    seen.update(job["id"] for job in jobs)
    return seen


def sanity_check_chat_id():
    bot_id = BOT_TOKEN.split(":")[0] if ":" in BOT_TOKEN else None
    if bot_id and CHAT_ID.strip() == bot_id:
        raise SystemExit("TELEGRAM_CHAT_ID is set to the bot's own id, not yours.")


def main():
    if not BOT_TOKEN or not CHAT_ID:
        raise SystemExit("Missing config. Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID environment variables.")
    sanity_check_chat_id()
    if "--set-profile-photo" in sys.argv:
        set_bot_profile_photo()
        return
    seen = load_seen()
    command_offset = None
    log.info("Starting OnlineJobs.ph watcher. %d jobs already known.", len(seen))
    send_telegram_message(
        f"\u2705 <b>OnlineJobs.ph notifier started.</b> Watching {len(seen)} known jobs.\n\n{welcome_message()}"
    )
    while True:
        seen = run_once(seen)
        save_seen(seen)
        log.info("Sleeping %ds...", CHECK_INTERVAL)
        for _ in range(CHECK_INTERVAL):
            command_offset, seen = process_commands(command_offset, seen)
            time.sleep(1)


if __name__ == "__main__":
    main()
