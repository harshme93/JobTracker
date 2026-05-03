"""
Job Tracker Scraper
Reads company configs + global titles from Firebase, scrapes career pages,
detects new jobs matching title keywords, sends email alerts.

Supported platforms:
- Eightfold AI (e.g. Liberty Mutual) — searches per keyword
- Workday (e.g. Nationwide, Travelers, Allstate, CNA, Markel) — searches per keyword
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
from urllib.parse import urljoin, urlencode, urlparse, parse_qs

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
    companies = []
    docs = db.collection("companies").stream()
    for d in docs:
        data = d.to_dict()
        companies.append({
            "id": d.id,
            "name": data.get("name", ""),
            "url": data.get("url", ""),
        })

    titles_doc = db.collection("settings").document("titles").get()
    titles = []
    if titles_doc.exists:
        titles = titles_doc.to_dict().get("list", [])

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
    url_lower = url.lower()
    if "eightfold" in url_lower or "searchjobs." in url_lower:
        return "eightfold"
    if "myworkdayjobs.com" in url_lower or ".wd1." in url_lower or ".wd5." in url_lower or ".wd12." in url_lower:
        return "workday"
    if "icims.com" in url_lower:
        return "icims"
    if "oraclecloud.com" in url_lower:
        return "oracle"
    return "generic"


# ─────────────────────────────────────────────
# EIGHTFOLD — searches per keyword via URL
# ─────────────────────────────────────────────
def scrape_eightfold(page, base_url, titles):
    """Eightfold: append each keyword to the URL and extract positions."""
    all_jobs = []
    seen_titles = set()

    for keyword in titles:
        search_url = base_url + keyword.replace(" ", "+")
        print(f"    Searching: {keyword}")
        try:
            page.goto(search_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"    Error loading: {e}")
            continue

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
                                        link: p.canonicalPositionUrl || ''
                                    }));
                                } catch(e) {}
                            }
                        }
                    }
                    return [];
                }
            """)
            for job in (jobs or []):
                key = job["title"] + "|" + job.get("link", "")
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_jobs.append(job)
            print(f"    Found {len(jobs or [])} positions")
        except Exception as e:
            print(f"    Parse error: {e}")

    print(f"  Eightfold total: {len(all_jobs)} unique positions")
    return all_jobs


# ─────────────────────────────────────────────
# WORKDAY — searches per keyword using search input
# ─────────────────────────────────────────────
def scrape_workday(page, base_url, titles):
    """Workday: use the search box to search each keyword."""
    all_jobs = []
    seen_titles = set()

    for keyword in titles:
        print(f"    Searching: {keyword}")
        try:
            page.goto(base_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(2000)

            # Find and fill search input
            search_input = page.query_selector('input[data-automation-id="searchBox"]')
            if not search_input:
                search_input = page.query_selector('input[aria-label*="Search"]')
            if not search_input:
                search_input = page.query_selector('input[type="text"]')

            if search_input:
                search_input.click()
                search_input.fill("")
                search_input.fill(keyword)
                page.keyboard.press("Enter")
                page.wait_for_timeout(3000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass
            else:
                print(f"    No search input found, trying URL param")
                # Try appending search query to URL
                if "?" in base_url:
                    search_url = base_url + "&q=" + keyword.replace(" ", "+")
                else:
                    search_url = base_url + "?q=" + keyword.replace(" ", "+")
                page.goto(search_url, wait_until="networkidle", timeout=30000)
                page.wait_for_timeout(3000)

        except Exception as e:
            print(f"    Error: {e}")
            continue

        # Extract job listings
        try:
            jobs = page.evaluate("""
                () => {
                    const results = [];
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
                    // Broader fallback
                    const allLinks = document.querySelectorAll('a');
                    for (const link of allLinks) {
                        const href = link.href || '';
                        const text = (link.textContent || '').trim();
                        if (href.includes('/job/') && text.length > 5 && text.length < 200) {
                            results.push({ title: text, link: href });
                        }
                    }
                    return results;
                }
            """)
            for job in (jobs or []):
                key = job["title"] + "|" + job.get("link", "")
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_jobs.append(job)
            print(f"    Found {len(jobs or [])} positions")
        except Exception as e:
            print(f"    Parse error: {e}")

    print(f"  Workday total: {len(all_jobs)} unique positions")
    return all_jobs


# ─────────────────────────────────────────────
# iCIMS
# ─────────────────────────────────────────────
def scrape_icims(page, url, titles):
    """iCIMS: search per keyword using URL params."""
    all_jobs = []
    seen_titles = set()

    for keyword in titles:
        print(f"    Searching: {keyword}")
        search_url = url
        if "?" in url:
            search_url = url + "&ss=" + keyword.replace(" ", "+")
        else:
            search_url = url + "?ss=" + keyword.replace(" ", "+")

        try:
            page.goto(search_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)
        except Exception as e:
            print(f"    Error loading: {e}")
            continue

        try:
            jobs = page.evaluate("""
                () => {
                    const results = [];
                    // iCIMS job title links
                    const selectors = [
                        '.iCIMS_JobsTable a.iCIMS_Anchor',
                        '.title a',
                        '[class*="JobTitle"] a',
                        'a[class*="job"]',
                        'h2 a', 'h3 a'
                    ];
                    for (const sel of selectors) {
                        const links = document.querySelectorAll(sel);
                        for (const link of links) {
                            const text = (link.textContent || '').trim();
                            const href = link.href || '';
                            if (text && text.length > 3 && text.length < 200 && href.includes('/jobs/')) {
                                results.push({ title: text, link: href });
                            }
                        }
                        if (results.length > 0) return results;
                    }
                    return results;
                }
            """)
            for job in (jobs or []):
                key = job["title"] + "|" + job.get("link", "")
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_jobs.append(job)
            print(f"    Found {len(jobs or [])} positions")
        except Exception as e:
            print(f"    Parse error: {e}")

    print(f"  iCIMS total: {len(all_jobs)} unique positions")
    return all_jobs


# ─────────────────────────────────────────────
# ORACLE HCM
# ─────────────────────────────────────────────
def scrape_oracle(page, url, titles):
    """Oracle HCM: search using the search box or URL."""
    all_jobs = []
    seen_titles = set()

    for keyword in titles:
        print(f"    Searching: {keyword}")
        try:
            page.goto(url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(5000)

            # Try to find search input
            search_input = page.query_selector('input[type="search"]')
            if not search_input:
                search_input = page.query_selector('input[placeholder*="Search"]')
            if not search_input:
                search_input = page.query_selector('input[aria-label*="Search"]')
            if not search_input:
                search_input = page.query_selector('input[type="text"]')

            if search_input:
                search_input.click()
                search_input.fill("")
                search_input.fill(keyword)
                page.keyboard.press("Enter")
                page.wait_for_timeout(5000)
                try:
                    page.wait_for_load_state("networkidle", timeout=10000)
                except:
                    pass

        except Exception as e:
            print(f"    Error: {e}")
            continue

        try:
            jobs = page.evaluate("""
                () => {
                    const results = [];
                    // Oracle uses various structures
                    const selectors = [
                        '[role="row"] a',
                        '.job-list-item a',
                        'a[class*="job"]',
                        'a[href*="/job/"]',
                        'a[href*="/requisition/"]',
                        'h2 a', 'h3 a',
                        'li a'
                    ];
                    const seen = new Set();
                    for (const sel of selectors) {
                        const els = document.querySelectorAll(sel);
                        for (const el of els) {
                            const text = (el.textContent || '').trim();
                            const href = el.href || '';
                            if (text && text.length > 5 && text.length < 200 && !seen.has(text)) {
                                seen.add(text);
                                results.push({ title: text, link: href });
                            }
                        }
                        if (results.length > 0) break;
                    }
                    return results;
                }
            """)
            for job in (jobs or []):
                key = job["title"] + "|" + job.get("link", "")
                if key not in seen_titles:
                    seen_titles.add(key)
                    all_jobs.append(job)
            print(f"    Found {len(jobs or [])} positions")
        except Exception as e:
            print(f"    Parse error: {e}")

    print(f"  Oracle total: {len(all_jobs)} unique positions")
    return all_jobs


# ─────────────────────────────────────────────
# GENERIC — for custom career sites
# ─────────────────────────────────────────────
def scrape_generic(page, url, titles):
    """Generic: load the page and parse all links."""
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
                              "next", "previous", "show more", "load more"]
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


# ─────────────────────────────────────────────
# ROUTER
# ─────────────────────────────────────────────
def scrape_jobs(page, url, titles):
    """Route to the correct scraper based on URL."""
    platform = detect_platform(url)
    print(f"  Platform: {platform}")

    if platform == "eightfold":
        return scrape_eightfold(page, url, titles)
    elif platform == "workday":
        return scrape_workday(page, url, titles)
    elif platform == "icims":
        return scrape_icims(page, url, titles)
    elif platform == "oracle":
        return scrape_oracle(page, url, titles)
    else:
        return scrape_generic(page, url, titles)


def matches_titles(job_title, target_titles):
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

    subject = f"\U0001f514 {len(new_jobs)} New Job(s) Found!"

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

            # Pass titles to scraper — platform-aware scrapers search per keyword
            jobs = scrape_jobs(page, company["url"], titles)

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

    db.collection("settings").document("last_run").set({
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "jobs_found": len(new_jobs),
    })

    print("\nDone.")


if __name__ == "__main__":
    main()
