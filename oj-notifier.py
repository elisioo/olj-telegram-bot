"""OnlineJobs.ph -> Telegram job notifier with interactive slash searches
and Groq-powered resume matching.

New in this version:
  /resume            - send a PDF/DOCX/TXT file to save it as your resume
  /match              - score current listings against your saved resume
  /automatch on|off   - when on, background job alerts are AI-filtered by
                         your resume instead of (or in addition to) keywords

Requires GROQ_API_KEY (get one free at https://console.groq.com) for the
AI features. Everything else works without it.
"""

import io
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timedelta
from html import escape
from pathlib import Path

import requests
from bs4 import BeautifulSoup

# ---------- Configuration ----------
BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
OWNER_TG_CONTACT = "@dobidapda"
SEARCH_URL = os.environ.get("OJ_SEARCH_URL", "https://www.onlinejobs.ph/jobseekers/jobsearch")
KEYWORDS = [key.strip().lower() for key in os.environ.get("OJ_KEYWORDS", "").split(",") if key.strip()]
CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SECONDS", "60"))
BASE_DIR = Path(__file__).parent
SEEN_FILE = BASE_DIR / "seen_jobs.json"
SETTINGS_FILE = BASE_DIR / "settings.json"
RESUME_DIR = BASE_DIR / "resumes"
RESUME_DIR.mkdir(exist_ok=True)
PROFILE_IMAGE = BASE_DIR / "asset" / "OLJ_BOT.jpg"
MAX_SEARCH_RESULTS = 5
MAX_RECENT_JOBS = 5
# /match deliberately considers only newly posted listings.  This is configurable
# for deployments that want a shorter or longer matching window.
MATCH_LOOKBACK_DAYS = int(os.environ.get("MATCH_LOOKBACK_DAYS", "2"))

# ---------- Groq (AI resume matching) ----------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
MIN_MATCH_SCORE = int(os.environ.get("MIN_MATCH_SCORE", "45"))
STRONG_MATCH_SCORE = int(os.environ.get("STRONG_MATCH_SCORE", "60"))

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
}

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("oj-notifier")


# ---------- Seen-jobs persistence ----------
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(sorted(seen)))


# ---------- Per-chat settings (automatch toggle) ----------
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings))


# ---------- Resume storage ----------
def resume_path(chat_id: str) -> Path:
    return RESUME_DIR / f"{chat_id}.txt"


def has_resume(chat_id: str) -> bool:
    return resume_path(chat_id).exists()


def load_resume_text(chat_id: str) -> str:
    path = resume_path(chat_id)
    return path.read_text(encoding="utf-8") if path.exists() else ""


def save_resume_text(chat_id: str, text: str):
    resume_path(chat_id).write_text(text, encoding="utf-8")


# ---------- Match-history tracking (new vs. returning matches) ----------
MATCH_SEEN_DIR = BASE_DIR / "match_seen"
MATCH_SEEN_DIR.mkdir(exist_ok=True)


def match_seen_path(chat_id: str) -> Path:
    return MATCH_SEEN_DIR / f"{chat_id}.json"


def load_match_seen(chat_id: str) -> set:
    path = match_seen_path(chat_id)
    if path.exists():
        try:
            return set(json.loads(path.read_text()))
        except json.JSONDecodeError:
            return set()
    return set()


def save_match_seen(chat_id: str, seen_ids: set):
    match_seen_path(chat_id).write_text(json.dumps(sorted(seen_ids)))

def download_telegram_file(file_id: str) -> bytes:
    """Download a file the user sent to the bot."""
    info_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getFile"
    info = requests.get(info_url, params={"file_id": file_id}, timeout=15).json()
    if not info.get("ok"):
        raise RuntimeError(f"getFile failed: {info}")
    file_path = info["result"]["file_path"]
    download_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
    resp = requests.get(download_url, timeout=30)
    resp.raise_for_status()
    return resp.content


def extract_resume_text(content: bytes, filename: str) -> str:
    """Pull plain text out of an uploaded resume file (pdf, docx, or txt)."""
    suffix = Path(filename).suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(io.BytesIO(content))
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    elif suffix == ".docx":
        from docx import Document
        doc = Document(io.BytesIO(content))
        return "\n".join(p.text for p in doc.paragraphs)
    else:
        return content.decode("utf-8", errors="ignore")


def handle_resume_upload(chat_id: str, document: dict):
    filename = document.get("file_name", "resume")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".docx", ".txt"):
        send_telegram_message_to(chat_id, "\u26a0\ufe0f Please send your resume as a .pdf, .docx, or .txt file.")
        return
    try:
        content = download_telegram_file(document["file_id"])
        text = extract_resume_text(content, filename)
    except Exception as exc:
        log.error("Resume processing failed: %s", exc)
        send_telegram_message_to(chat_id, "\u26a0\ufe0f I couldn't read that file. Try a different PDF, DOCX, or TXT.")
        return
    if not text.strip():
        send_telegram_message_to(
            chat_id,
            "\u26a0\ufe0f I couldn't extract any text from that file — it might be a scanned "
            "image. Try a text-based PDF or DOCX instead.",
        )
        return
    save_resume_text(chat_id, text)
    send_telegram_message_to(
        chat_id,
        "\u2705 Resume saved! I'll use it to score how well jobs match your background.\n\n"
        "Send /match to see your best-fit jobs right now, or /automatch on to get only "
        "AI-matched jobs in your automatic alerts.",
    )


# ---------- Groq AI matching ----------
def call_groq(system_prompt: str, user_prompt: str) -> str:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY is not set.")
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_API_KEY}", "Content-Type": "application/json"},
        json={
            "model": GROQ_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.2,
            "response_format": {"type": "json_object"},
        },
        timeout=60,
    )
    resp.raise_for_status()
    data = resp.json()
    return data["choices"][0]["message"]["content"]


def ai_match_jobs(resume_text: str, jobs: list[dict]) -> list[dict]:
    """Score each job's fit against the resume via Groq.

    Returns jobs annotated with match_score/match_reason, sorted best-first.
    """
    if not jobs or not resume_text.strip():
        return []

    trimmed_resume = resume_text[:6000]
    job_lines = []
    for j in jobs:
        overview = (j.get("overview") or "")[:300]
        job_lines.append(
            f"id={j['id']} | title={j['title']} | type={j.get('type', '')} | "
            f"tags={','.join(j.get('tags', []))} | overview={overview}"
        )

    system_prompt = (
        "You are a job-matching assistant. Given a candidate's resume and a list of "
        "job postings, score how well each job fits the candidate's skills and "
        "experience from 0-100. Respond ONLY with a JSON object of the form "
        '{"matches": [{"id": "<job id>", "score": <0-100 integer>, "reason": "<one short sentence>"}]}. '
        "Include every job id given, even low scores."
    )
    user_prompt = f"RESUME:\n{trimmed_resume}\n\nJOB POSTINGS:\n" + "\n".join(job_lines)

    try:
        raw = call_groq(system_prompt, user_prompt)
        parsed = json.loads(raw)
    except (requests.RequestException, ValueError, KeyError, RuntimeError) as exc:
        log.error("Groq matching failed: %s", exc)
        return []

    scores = {str(m["id"]): m for m in parsed.get("matches", []) if "id" in m}
    annotated = []
    for j in jobs:
        m = scores.get(str(j["id"]))
        if not m:
            continue
        score = m.get("score", 0)
        job = dict(j)
        job["match_score"] = score
        job["match_reason"] = m.get("reason", "")
        annotated.append(job)
    annotated.sort(key=lambda j: j["match_score"], reverse=True)
    if not annotated and parsed.get("matches"):
        log.warning(
            "Groq returned %d match entries but none matched a job id. "
            "Sample returned id: %r, sample job id: %r",
            len(parsed["matches"]),
            parsed["matches"][0].get("id"),
            jobs[0]["id"],
        )
    return annotated


def posted_date(posted: str, today: datetime | None = None):
    """Return the calendar date from an OnlineJobs ``Posted on`` value."""
    value = re.sub(r"\s+", " ", (posted or "").replace("*", "").strip())
    value = re.sub(r"^posted on\s*", "", value, flags=re.IGNORECASE).strip()
    if not value:
        return None

    current = today or datetime.now()
    lowered = value.lower()
    if lowered in {"today", "just now"}:
        return current.date()
    if lowered == "yesterday":
        return (current - timedelta(days=1)).date()
    relative = re.fullmatch(r"(\d+)\s+(?:minute|hour|day|week)s?\s+ago", lowered)
    if relative:
        amount = int(relative.group(1))
        unit = relative.group(0).split()[1]
        return (current - timedelta(**{unit + "s": amount})).date()

    cleaned = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", value, flags=re.IGNORECASE)
    try:
        return datetime.fromisoformat(cleaned.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for date_format in ("%b %d, %Y", "%B %d, %Y", "%m/%d/%Y", "%m-%d-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, date_format).date()
        except ValueError:
            continue
    return None


def jobs_in_match_window(jobs: list[dict], now: datetime | None = None) -> list[dict]:
    """Keep listings posted today or in the preceding MATCH_LOOKBACK_DAYS days."""
    current = now or datetime.now()
    cutoff = (current - timedelta(days=MATCH_LOOKBACK_DAYS)).date()
    recent_jobs = []
    for job in jobs:
        date_posted = posted_date(job.get("posted", ""), current)
        if date_posted and cutoff <= date_posted <= current.date():
            recent_jobs.append(job)
    return recent_jobs


def send_ai_matches(chat_id: str):
    if not GROQ_API_KEY:
        send_telegram_message_to(
            chat_id, "⚠️ AI matching isn't set up yet — the bot owner needs to set GROQ_API_KEY."
        )
        return
    if not has_resume(chat_id):
        send_telegram_message_to(chat_id, "I don't have your resume yet. Send it to me as a file first (PDF, DOCX, or TXT).")
        return
    try:
        jobs = fetch_jobs()
    except requests.RequestException as exc:
        log.error("Match fetch failed: %s", exc)
        send_telegram_message_to(chat_id, "⚠️ I couldn't load current jobs right now. Please try again shortly.")
        return
    recent_jobs = jobs_in_match_window(jobs)
    if not recent_jobs:
        send_telegram_message_to(
            chat_id,
            f"😔 I couldn't find job posts from the last {MATCH_LOOKBACK_DAYS} days to compare right now. Try again later.",
        )
        return

    send_telegram_message_to(
        chat_id,
        f"🧠 Comparing your resume against {len(recent_jobs)} job post(s) from the last {MATCH_LOOKBACK_DAYS} days...",
    )
    ranked_jobs = ai_match_jobs(load_resume_text(chat_id), recent_jobs)
    strong_matches = [job for job in ranked_jobs if job.get("match_score", 0) >= STRONG_MATCH_SCORE]
    good_fit_matches = [job for job in ranked_jobs if job.get("match_score", 0) >= MIN_MATCH_SCORE]

    already_sent = load_match_seen(chat_id)

    def label_job(job: dict) -> dict:
        job = dict(job)
        if job["id"] in already_sent:
            job["match_label"] = "🔁 <i>Already sent to you before</i>"
        else:
            job["match_label"] = "🆕 <i>New match</i>"
        return job

    if strong_matches:
        extra_good_fits = [job for job in good_fit_matches if job["id"] not in {match["id"] for match in strong_matches}]
        if extra_good_fits:
            summary = (
                f"🧠 Found <b>{len(strong_matches)}</b> strong match(es) "
                f"and <b>{len(extra_good_fits)}</b> other good-fit job(s) for your resume:"
            )
        else:
            summary = f"🧠 Found <b>{len(strong_matches)}</b> strong match(es) for your resume:"
        send_telegram_message_to(chat_id, summary)
        sent_this_run = set()
        for job in strong_matches:
            send_telegram_message_to(chat_id, format_message(fetch_job_details(label_job(job))))
            sent_this_run.add(job["id"])
            time.sleep(1)
        for job in extra_good_fits:
            send_telegram_message_to(chat_id, format_message(fetch_job_details(label_job(job))))
            sent_this_run.add(job["id"])
            time.sleep(1)
        save_match_seen(chat_id, already_sent | sent_this_run)
        return
    elif good_fit_matches:
        matches = good_fit_matches
        send_telegram_message_to(chat_id, f"🧠 Found <b>{len(matches)}</b> good-fit job(s) for your resume:")
    elif ranked_jobs:
        matches = ranked_jobs[:MAX_SEARCH_RESULTS]
        send_telegram_message_to(
            chat_id,
            "🧠 I couldn't find any good-fit matches, so here are the closest jobs I found right now:",
        )
    else:
        log.error("AI matching returned no scores for %d recent jobs.", len(recent_jobs))
        send_telegram_message_to(
            chat_id,
            "⚠️ I found recent jobs, but couldn't score them against your resume right now. Please try again shortly.",
        )
        return
    sent_this_run = set()
    for job in matches:
        send_telegram_message_to(chat_id, format_message(fetch_job_details(label_job(job))))
        sent_this_run.add(job["id"])
        time.sleep(1)
    save_match_seen(chat_id, already_sent | sent_this_run)

# ---------- Scraping ----------
def fetch_jobs() -> list[dict]:
    """Fetch job listing cards, including their short description and tags."""
    response = requests.get(SEARCH_URL, headers=HEADERS, timeout=20)
    response.raise_for_status()
    soup = BeautifulSoup(response.text, "html.parser")

    jobs = {}
    for anchor in soup.find_all("a", href=re.compile(r"^/jobseekers/job/")):
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
    """Render the Telegram job-card layout, with an AI match line when present."""
    overview = job.get("overview") or "Not provided."
    if len(overview) > 700:
        overview = overview[:697].rsplit(" ", 1)[0] + "..."

    lines = [f"\U0001f4cc <b>{escape(job['title'])}</b>"]
    if "match_score" in job:
        lines.append(f"\U0001f9e0 <b>AI match:</b> {job['match_score']}/100 — {escape(job.get('match_reason', ''))}")
    if job.get("match_label"):
        lines.append(job["match_label"])
    lines += [
        f"\U0001f4bc <b>Type of work:</b> {escape(job.get('type') or 'Not provided')}",
        f"\U0001f4b0 <b>Wage/salary:</b> {escape(job.get('salary') or 'Not provided')}",
        f"\u23f1\ufe0f <b>Hours per week:</b> {escape(job.get('hours') or 'Not provided')}",
        f"\U0001f4c5 <b>Date updated:</b> {escape(job.get('updated') or 'Not provided')}",
        f"\U0001f552 <b>Time uploaded:</b> {escape(job.get('posted') or 'Not provided')}",
        "",
        f"\U0001f4cb <b>Job overview</b>\n{escape(overview)}",
        "",
        f"\U0001f517 <b>Job link:</b> <a href=\"{escape(job['url'], quote=True)}\">Open job posting</a>",
    ]
    return "\n".join(lines)


def send_telegram_message_to(chat_id: str, text: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    last_error = None
    for attempt in range(2):
        try:
            response = requests.post(url, data=payload, timeout=(10, 30))
            if not response.ok:
                log.error("Telegram send failed: %s", response.text)
            return
        except requests.RequestException as exc:
            last_error = exc
            log.warning("Telegram send attempt %d failed: %s", attempt + 1, exc)
    log.error("Telegram send failed after retry: %s", last_error)


def send_telegram_message(text: str):
    send_telegram_message_to(CHAT_ID, text)


def set_bot_profile_photo():
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
    lines = [
        "\U0001f44b <b>Welcome to the OnlineJobs.ph Notifier!</b>",
        "",
        "I watch OnlineJobs.ph and send you matching new job posts with the job type, pay, hours, overview, and a direct link.",
        "",
        "<b>Commands</b>",
        "\U0001f504 <b>/refresh</b> - Check right now for new job posts.",
        "\U0001f550 <b>/recent</b> - View the five most recent job posts.",
        "\U0001f4c5 <b>/view_now</b> - View every job updated today.",
        "\U0001f50e <b>/keyword</b> - Search current jobs by keyword.",
        "\U0001f4c4 <b>/resume</b> - Upload your resume (PDF/DOCX/TXT) for AI matching.",
        "\U0001f9e0 <b>/match</b> - See jobs that best fit your uploaded resume.",
        "\u2699\ufe0f <b>/automatch on|off</b> - Filter automatic alerts by resume fit.",
        "\U0001f4ac <b>/help</b> - Show this guide again.",
        "",
        "<b>How to search</b>",
        "Send <b>/video_editor</b> for one-word searches, or <b>/search video editor</b> for phrases.",
        "",
        "\U0001f4a1 Example: <b>/virtual_assistant</b>",
    ]
    if OWNER_TG_CONTACT:
        lines.extend([
            "",
            f"\U0001f4e8 For errors, questions, or recommendations, message the creator directly at {escape(OWNER_TG_CONTACT)}.",
        ])
    return "\n".join(lines)


def search_jobs(keyword: str) -> list[dict]:
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
    seen.update(job["id"] for job in jobs)
    save_seen(seen)
    return seen


def send_recent_jobs(chat_id: str):
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


def process_commands(offset, seen: set):
    """Handle /keyword, /refresh, /recent, /view_now, /resume, /match, /automatch."""
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
        chat_id = str(message.get("chat", {}).get("id", ""))
        if chat_id != CHAT_ID:
            continue

        # A file attachment (no leading "/" needed) is treated as a resume upload.
        document = message.get("document")
        if document:
            handle_resume_upload(chat_id, document)
            continue

        text = message.get("text", "").strip()
        if not text.startswith("/"):
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
        elif command == "/resume":
            send_telegram_message_to(
                chat_id,
                "\U0001f4c4 Send your resume now as a file attachment (paperclip icon) — PDF, DOCX, or TXT.",
            )
        elif command == "/match":
            send_ai_matches(chat_id)
        elif command == "/automatch":
            parts = text.split(maxsplit=1)
            arg = parts[1].strip().lower() if len(parts) == 2 else ""
            settings = load_settings()
            chat_settings = settings.setdefault(chat_id, {})
            if arg == "on":
                if not has_resume(chat_id):
                    send_telegram_message_to(chat_id, "Upload a resume first with /resume, then turn automatch on.")
                else:
                    chat_settings["automatch"] = True
                    save_settings(settings)
                    send_telegram_message_to(chat_id, "\U0001f9e0 AI resume matching is ON for automatic job alerts.")
            elif arg == "off":
                chat_settings["automatch"] = False
                save_settings(settings)
                send_telegram_message_to(chat_id, "AI resume matching is OFF. Back to keyword filtering.")
            else:
                state = "ON" if chat_settings.get("automatch") else "OFF"
                send_telegram_message_to(chat_id, f"AI resume matching is currently {state}. Use /automatch on or /automatch off.")
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

    candidate_jobs = [job for job in jobs if job["id"] not in seen and matches_keywords(job)]

    settings = load_settings()
    automatch_on = settings.get(CHAT_ID, {}).get("automatch", False)
    if automatch_on and has_resume(CHAT_ID) and candidate_jobs:
        candidate_jobs = ai_match_jobs(load_resume_text(CHAT_ID), candidate_jobs)

    for job in reversed(candidate_jobs):
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
