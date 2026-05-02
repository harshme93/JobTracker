"""
Job Tracker Scraper
Reads company configs from Firebase, scrapes career pages,
detects new jobs matching your title keywords, sends email alerts.
"""

import json
import os
import re
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

import firebase_admin
from firebase_admin import credentials, firestore
from playwright.sync_api import sync_playwright

# ─── Firebase Init ───
# Option 1: Service account JSON file (local dev)
# cred = credentials.Certificate("serviceAccountKey.json")

# Option 2: Environment variable (GitHub Actions)
firebase_creds = json.loads(os.environ.get("FIREBASE_CREDENTIALS", "{}"))
if firebase_creds:
    cred = credentials.Certificate(firebase_creds)
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ─── Email Config (via environment variables) ───
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")  # App password, not your real password


def get_config():
    """Read companies and notification email from Firebase."""
    companies = []
    docs = db.collection("companies").stream()
    for doc in docs:
        data = doc.to_dict()
        companies.append({
            "id": doc.id,
            "name": data.get("name", ""),
            "url": data.get("url", ""),
            "titles": data.get("titles", []),
        })

    # Get notification email
    settings_doc = db.collection("settings").document("notification").get()
    email = ""
    if settings_doc.exists:
        email = settings_doc.to_dict().get("email", "")

    return companies, email


def get_seen_jobs():
    """Get set of already-seen job IDs from Firebase."""
    doc = db.collection("settings").document("seen_jobs").get()
    if doc.exists:
        return set(doc.to_dict().get("job_ids", []))
    return set()


def save_seen_jobs(seen_ids):
    """Save seen job IDs to Firebase."""
    # Firestore has a 1MB doc limit; if this grows huge, split into chunks
    db.collection("settings").document("seen_jobs").set({
        "job_ids": list(seen_ids),
        "updated_at": datetime.utcnow().isoformat(),
    })


def scrape_jobs(page, url):
    """
    Scrape job listings from a career page.
    Returns list of {"title": ..., "link": ...}

    NOTE: This is a generic scraper. It works for many sites but you may
    need to customize selectors per company. Common patterns:
    - Workday: .css-19uc56f, [data-automation-id="jobTitle"]
    - Greenhouse: .opening a
    - Lever: .posting-title a
    - iCIMS: .iCIMS_JobsTable .Title a
    """
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)  # Extra wait for JS rendering
    except Exception as e:
        print(f"  Error loading {url}: {e}")
        return []

    # Generic approach: find all links that look like job postings
    jobs = []
    links = page.query_selector_all("a")

    for link in links:
        try:
            text = (link.inner_text() or "").strip()
            href = link.get_attribute("href") or ""

            # Filter: must have text, href must look like a job link
            if not text or len(text) < 5 or len(text) > 200:
                continue

            # Skip navigation/footer links
            skip_words = ["sign in", "log in", "privacy", "cookie", "terms",
                          "contact", "about us", "home", "menu", "close",
                          "search", "filter", "clear", "reset", "back"]
            if any(sw in text.lower() for sw in skip_words):
                continue

            # Normalize URL
            if href.startswith("/"):
                from urllib.parse import urljoin
                href = urljoin(url, href)

            jobs.append({"title": text, "link": href})
        except Exception:
            continue

    return jobs


def matches_titles(job_title, target_titles):
    """Check if job title matches any of the target title keywords."""
    job_lower = job_title.lower()
    for target in target_titles:
        words = target.strip().split()
        if all(word in job_lower for word in words):
            return True
    return False


def make_job_id(company_name, job_title, job_link):
    """Create a unique ID for a job posting."""
    raw = f"{company_name}|{job_title}|{job_link}"
    return hashlib.md5(raw.encode()).hexdigest()


def send_email(to_email, new_jobs):
    """Send email notification with new job listings."""
    if not SMTP_USER or not SMTP_PASS:
        print("  SMTP not configured. Skipping email.")
        print("  Would have sent:")
        for job in new_jobs:
            print(f"    [{job['company']}] {job['title']} → {job['link']}")
        return

    subject = f"🔔 {len(new_jobs)} New Job(s) Found!"

    # Build HTML email
    rows = ""
    for job in new_jobs:
        rows += f"""
        <tr>
            <td style="padding:8px 12px;border-bottom:1px solid #eee;font-weight:600">{job['company']}</td>
            <td style="padding:8px 12px;border-bottom:1px solid #eee">
                <a href="{job['link']}" style="color:#2563eb;text-decoration:none">{job['title']}</a>
            </td>
        </tr>"""

    html = f"""
    <div style="font-family:sans-serif;max-width:600px;margin:0 auto">
        <h2 style="margin-bottom:4px">New Jobs Found</h2>
        <p style="color:#666;margin-top:0">{datetime.utcnow().strftime('%B %d, %Y at %H:%M UTC')}</p>
        <table style="width:100%;border-collapse:collapse;margin-top:12px">
            <tr style="background:#f5f5f5">
                <th style="padding:8px 12px;text-align:left">Company</th>
                <th style="padding:8px 12px;text-align:left">Position</th>
            </tr>
            {rows}
        </table>
        <p style="margin-top:16px;font-size:13px;color:#999">— Job Tracker Bot</p>
    </div>
    """

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_USER
    msg["To"] = to_email
    msg.attach(MIMEText(html, "html"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, to_email, msg.as_string())
        print(f"  Email sent to {to_email}")
    except Exception as e:
        print(f"  Email failed: {e}")


def main():
    print(f"=== Job Tracker Run: {datetime.utcnow().isoformat()} ===\n")

    companies, notify_email = get_config()
    if not companies:
        print("No companies configured. Add them via the frontend.")
        return
    if not notify_email:
        print("No notification email set. Add it via the frontend.")
        return

    seen_jobs = get_seen_jobs()
    new_jobs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for company in companies:
            print(f"Scraping: {company['name']} ({company['url']})")

            # Build search URLs from titles
            for title_keyword in company["titles"]:
                search_url = company["url"] + title_keyword.replace(" ", "+")
                print(f"  Searching: {title_keyword}")

                jobs = scrape_jobs(page, search_url)
                print(f"  Found {len(jobs)} links")

                for job in jobs:
                    if not matches_titles(job["title"], [title_keyword]):
                        continue

                    job_id = make_job_id(company["name"], job["title"], job["link"])
                    if job_id not in seen_jobs:
                        new_jobs.append({
                            "company": company["name"],
                            "title": job["title"],
                            "link": job["link"],
                        })
                        seen_jobs.add(job_id)

        browser.close()

    print(f"\nNew matching jobs: {len(new_jobs)}")

    if new_jobs:
        send_email(notify_email, new_jobs)
        save_seen_jobs(seen_jobs)
    else:
        print("No new jobs. No notification sent.")

    # Save last run timestamp
    db.collection("settings").document("last_run").set({
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "jobs_found": len(new_jobs),
    })

    print("\nDone.")


if __name__ == "__main__":
    main()
