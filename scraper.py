"""
Job Tracker Scraper
Reads company configs + global titles from Firebase, scrapes career pages,
detects new jobs matching title keywords, sends email alerts.

Supported platforms:
- Eightfold AI (e.g. Liberty Mutual)
- Workday (e.g. Nationwide, Travelers, Allstate, CNA, Markel)
- iCIMS (e.g. Mercury Insurance)
- Oracle HCM (e.g. Chubb)
- Generic fallback (link parsing)
"""

import json
import os
import re
import hashlib
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from urllib.parse import urljoin

import firebase_admin
from firebase_admin import credentials, firestore
from playwright.sync_api import sync_playwright

# ─── Firebase Init ───
firebase_creds = json.loads(os.environ.get("FIREBASE_CREDENTIALS", "{}"))
if firebase_creds:
    cred = credentials.Certificate(firebase_creds)
else:
    cred = credentials.Certificate("serviceAccountKey.json")

firebase_admin.initialize_app(cred)
db = firestore.client()

# ─── Email Config ───
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")


def get_config():
    """Read companies, global titles, and notification email from Firebase."""
    companies = []
    docs = db.collection("companies").stream()
    for d in docs:
        data = d.to_dict()
        companies.append({
            "id": d.id,
            "name": data.get("name", ""),
            "url": data.get("url", ""),
        })

    # Global titles
    titles_doc = db.collection("settings").document("titles").get()
    titles = []
    if titles_doc.exists:
        titles = titles_doc.to_dict().get("list", [])

    # Notification email
    settings_doc = db.collection("settings").document("notification").get()
    email = ""
    if settings_doc.exists:
        email = settings_doc.to_dict().get("email", "")

    return companies, titles, email


def get_seen_jobs():
    doc = db.collection("settings").document("seen_jobs").get()
    if doc.exists:
        return set(doc.to_dict().get("job_ids", []))
    return set()


def save_seen_jobs(seen_ids):
    db.collection("settings").document("seen_jobs").set({
        "job_ids": list(seen_ids),
        "updated_at": datetime.utcnow().isoformat(),
    })


def detect_platform(url):
    """Detect which ATS platform a URL belongs to."""
    url_lower = url.lower()
    if "eightfold" in url_lower or "searchjobs." in url_lower:
        return "eightfold"
    if "myworkdayjobs.com" in url_lower or ".wd1." in url_lower or ".wd5." in url_lower or ".wd12." in url_lower:
        return "workday"
    if "icims.com" in url_lower:
        return "icims"
    if "oraclecloud.com" in url_lower:
        return "oracle"
    if "jobs.ajg.com" in url_lower or "jobs.statefarm.com" in url_lower:
        return "generic_js"
    return "generic"


def scrape_eightfold(page, url):
    """Extract jobs from Eightfold AI career sites."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Error loading: {e}")
        return []

    try:
        jobs = page.evaluate("""
            () => {
                const scripts = document.querySelectorAll('script');
                for (const script of scripts) {
                    const text = script.textContent || '';
                    if (text.includes('"positions"')) {
                        const match = text.match(/"positions"\\s*:\\s*(\\[.*?\\])\\s*[,}]/s);
                        if (match) {
                            try {
                                const positions = JSON.parse(match[1]);
                                return positions.map(p => ({
                                    title: p.name || p.posting_name || '',
                                    link: p.canonicalPositionUrl || '',
                                    id: String(p.id || '')
                                }));
                            } catch(e) {}
                        }
                    }
                }
                return [];
            }
        """)
        print(f"  Eightfold: {len(jobs)} positions found")
        return jobs or []
    except Exception as e:
        print(f"  Eightfold parse error: {e}")
        return []


def scrape_workday(page, url):
    """Extract jobs from Workday career sites."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Error loading: {e}")
        return []

    try:
        jobs = page.evaluate("""
            () => {
                const results = [];
                // Workday renders job cards as <a> tags with data-automation-id
                const jobLinks = document.querySelectorAll('a[data-automation-id="jobTitle"]');
                if (jobLinks.length > 0) {
                    for (const link of jobLinks) {
                        results.push({
                            title: (link.textContent || '').trim(),
                            link: link.href || ''
                        });
                    }
                    return results;
                }
                // Fallback: look for job listing sections
                const sections = document.querySelectorAll('[data-automation-id="jobResults"] li, .css-19uc56f, [class*="jobTitle"]');
                for (const el of sections) {
                    const a = el.querySelector('a') || el;
                    const text = (a.textContent || '').trim();
                    const href = a.href || '';
                    if (text && text.length > 3 && text.length < 200) {
                        results.push({ title: text, link: href });
                    }
                }
                return results;
            }
        """)
        print(f"  Workday: {len(jobs)} positions found")
        return jobs or []
    except Exception as e:
        print(f"  Workday parse error: {e}")
        return []


def scrape_icims(page, url):
    """Extract jobs from iCIMS career sites."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Error loading: {e}")
        return []

    try:
        jobs = page.evaluate("""
            () => {
                const results = [];
                const links = document.querySelectorAll('.iCIMS_JobsTable a.iCIMS_Anchor, .title a, [class*="JobTitle"] a, .iCIMS_JobTitle a');
                for (const link of links) {
                    const text = (link.textContent || '').trim();
                    if (text && text.length > 3) {
                        results.push({ title: text, link: link.href || '' });
                    }
                }
                if (results.length === 0) {
                    const allLinks = document.querySelectorAll('a');
                    for (const link of allLinks) {
                        const href = link.href || '';
                        const text = (link.textContent || '').trim();
                        if (href.includes('/jobs/') && text.length > 5 && text.length < 200) {
                            results.push({ title: text, link: href });
                        }
                    }
                }
                return results;
            }
        """)
        print(f"  iCIMS: {len(jobs)} positions found")
        return jobs or []
    except Exception as e:
        print(f"  iCIMS parse error: {e}")
        return []


def scrape_oracle(page, url):
    """Extract jobs from Oracle HCM / OHCM career sites."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)  # Oracle is slow
    except Exception as e:
        print(f"  Error loading: {e}")
        return []

    try:
        jobs = page.evaluate("""
            () => {
                const results = [];
                // Oracle HCM uses various selectors
                const rows = document.querySelectorAll('[role="row"] a, .job-list-item a, [class*="JobTitle"], [class*="job-title"]');
                for (const el of rows) {
                    const text = (el.textContent || '').trim();
                    const href = el.href || '';
                    if (text && text.length > 3 && text.length < 200) {
                        results.push({ title: text, link: href });
                    }
                }
                if (results.length === 0) {
                    const allLinks = document.querySelectorAll('a');
                    for (const link of allLinks) {
                        const href = link.href || '';
                        const text = (link.textContent || '').trim();
                        if ((href.includes('/job/') || href.includes('/requisition/')) && text.length > 5 && text.length < 200) {
                            results.push({ title: text, link: href });
                        }
                    }
                }
                return results;
            }
        """)
        print(f"  Oracle HCM: {len(jobs)} positions found")
        return jobs or []
    except Exception as e:
        print(f"  Oracle parse error: {e}")
        return []


def scrape_generic(page, url):
    """Generic fallback: parse all links that look like job postings."""
    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
    except Exception as e:
        print(f"  Error loading: {e}")
        return []

    jobs = []
    try:
        links = page.query_selector_all("a")
        for link in links:
            try:
                text = (link.inner_text() or "").strip()
                href = link.get_attribute("href") or ""
                if not text or len(text) < 5 or len(text) > 200:
                    continue
                skip_words = ["sign in", "log in", "privacy", "cookie", "terms",
                              "contact", "about us", "home", "menu", "close",
                              "search", "filter", "clear", "reset", "back",
                              "next", "previous", "show more"]
                if any(sw in text.lower() for sw in skip_words):
                    continue
                if href.startswith("/"):
                    href = urljoin(url, href)
                jobs.append({"title": text, "link": href})
            except Exception:
                continue
    except Exception as e:
        print(f"  Generic parse error: {e}")

    print(f"  Generic: {len(jobs)} links found")
    return jobs


def scrape_jobs(page, url):
    """Route to the correct scraper based on URL."""
    platform = detect_platform(url)
    print(f"  Platform: {platform}")

    if platform == "eightfold":
        return scrape_eightfold(page, url)
    elif platform == "workday":
        return scrape_workday(page, url)
    elif platform == "icims":
        return scrape_icims(page, url)
    elif platform == "oracle":
        return scrape_oracle(page, url)
    else:
        return scrape_generic(page, url)


def matches_titles(job_title, target_titles):
    """Check if job title matches any of the target title keywords."""
    job_lower = job_title.lower()
    for target in target_titles:
        words = target.strip().split()
        if all(word in job_lower for word in words):
            return True
    return False


def make_job_id(company_name, job_title, job_link):
    raw = f"{company_name}|{job_title}|{job_link}"
    return hashlib.md5(raw.encode()).hexdigest()


def send_email(to_email, new_jobs):
    if not SMTP_USER or not SMTP_PASS:
        print("  SMTP not configured. Skipping email.")
        for job in new_jobs:
            print(f"    [{job['company']}] {job['title']} -> {job['link']}")
        return

    subject = f"🔔 {len(new_jobs)} New Job(s) Found!"

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

    companies, titles, notify_email = get_config()
    if not companies:
        print("No companies configured. Add them via the frontend.")
        return
    if not titles:
        print("No job titles configured. Add them via the frontend.")
        return
    if not notify_email:
        print("No notification email set. Add it via the frontend.")
        return

    print(f"Tracking {len(companies)} companies with {len(titles)} title keywords")
    print(f"Titles: {', '.join(titles)}\n")

    seen_jobs = get_seen_jobs()
    new_jobs = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        for company in companies:
            print(f"Scraping: {company['name']} ({company['url']})")

            jobs = scrape_jobs(page, company["url"])

            matched = 0
            for job in jobs:
                if not matches_titles(job["title"], titles):
                    continue

                job_id = make_job_id(company["name"], job["title"], job.get("link", ""))
                if job_id not in seen_jobs:
                    new_jobs.append({
                        "company": company["name"],
                        "title": job["title"],
                        "link": job.get("link", ""),
                    })
                    seen_jobs.add(job_id)
                    matched += 1

            print(f"  Matched: {matched} new job(s)\n")

        browser.close()

    print(f"Total new matching jobs: {len(new_jobs)}")

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
