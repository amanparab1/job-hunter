import os
import json
import sqlite3
import time
import random
import asyncio
import threading
import smtplib
import httpx
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.application import MIMEApplication
from typing import List, Dict, Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, Request, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# Load environment variables
load_dotenv()

# Initialize directories
os.makedirs("static/resumes", exist_ok=True)
os.makedirs("static/screenshots", exist_ok=True)
os.makedirs("templates", exist_ok=True)

# Global Logs Store
LOGS = []
logs_lock = threading.Lock()

def log_message(module: str, message: str, level: str = "INFO"):
    """
    Thread-safe logger appending to global state.
    """
    timestamp = time.time()
    with logs_lock:
        LOGS.append({
            "timestamp": timestamp,
            "module": module,
            "message": message,
            "level": level
        })
        # Keep logs bounded
        if len(LOGS) > 1000:
            LOGS.pop(0)
    print(f"[{module}] [{level}] {message}")

# --- Database Setup & Helpers ---
USE_POSTGRES = os.getenv("USE_POSTGRES", "true").lower() == "true"
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = os.getenv("DB_PORT", "5432")
DB_NAME = os.getenv("DB_NAME", "job_hunter")
DB_USER = os.getenv("DB_USER", "postgres")
DB_PASSWORD = os.getenv("DB_PASSWORD", "postgres")

def get_db_connection():
    if USE_POSTGRES:
        try:
            import psycopg2
            import psycopg2.extras
            conn = psycopg2.connect(
                host=DB_HOST,
                port=DB_PORT,
                database=DB_NAME,
                user=DB_USER,
                password=DB_PASSWORD
            )
            return conn
        except Exception as e:
            # If the database does not exist, we try to create it automatically
            if "database" in str(e) and DB_NAME in str(e):
                log_message("Database", f"PostgreSQL database '{DB_NAME}' not found. Attempting to create it...", "WARNING")
                try:
                    import psycopg2
                    temp_conn = psycopg2.connect(
                        host=DB_HOST,
                        port=DB_PORT,
                        database="postgres",
                        user=DB_USER,
                        password=DB_PASSWORD
                    )
                    temp_conn.autocommit = True
                    with temp_conn.cursor() as cursor:
                        cursor.execute(f"CREATE DATABASE {DB_NAME}")
                    temp_conn.close()
                    log_message("Database", f"Created database '{DB_NAME}' in PostgreSQL.", "SUCCESS")
                    
                    conn = psycopg2.connect(
                        host=DB_HOST,
                        port=DB_PORT,
                        database=DB_NAME,
                        user=DB_USER,
                        password=DB_PASSWORD
                    )
                    return conn
                except Exception as ex:
                    log_message("Database", f"Failed to create postgres database '{DB_NAME}': {str(ex)}. Falling back to SQLite.", "ERROR")
            else:
                log_message("Database", f"PostgreSQL connection failed: {str(e)}. Falling back to SQLite.", "ERROR")
            
    # SQLite Fallback
    import sqlite3
    conn = sqlite3.connect("job_tracker.db")
    conn.row_factory = sqlite3.Row
    return conn

def is_postgres(conn):
    return type(conn).__module__.startswith("psycopg2")

def db_execute(conn, query, params=()):
    """
    Executes a query supporting both SQLite and PostgreSQL.
    Converts %s placeholders to ? placeholders if connection is SQLite.
    """
    import sqlite3
    if not is_postgres(conn):
        query = query.replace("%s", "?")
        # For INSERT OR IGNORE in SQLite
        if "ON CONFLICT" in query:
            import re
            query = re.sub(r"ON CONFLICT\s*\(.*?\)\s*DO\s*NOTHING", "", query, flags=re.IGNORECASE)
            query = re.sub(r"INSERT\s+INTO", "INSERT OR IGNORE INTO", query, flags=re.IGNORECASE)
            
    if is_postgres(conn):
        import psycopg2.extras
        cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    else:
        cur = conn.cursor()
        
    cur.execute(query, params)
    return cur

def init_db():
    conn = get_db_connection()
    if is_postgres(conn):
        log_message("Database", "Initializing PostgreSQL Database...")
        db_execute(conn, '''
        CREATE TABLE IF NOT EXISTS jobs (
            id SERIAL PRIMARY KEY,
            title VARCHAR(255) NOT NULL,
            company VARCHAR(255) NOT NULL,
            description TEXT,
            url TEXT UNIQUE,
            location VARCHAR(255),
            match_score INTEGER,
            match_reason TEXT,
            resume_path TEXT,
            status VARCHAR(50) NOT NULL,
            screenshot_path TEXT,
            contacts TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
    else:
        log_message("Database", "Initializing SQLite Database...")
        db_execute(conn, '''
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT NOT NULL,
            company TEXT NOT NULL,
            description TEXT,
            url TEXT UNIQUE,
            location TEXT,
            match_score INTEGER,
            match_reason TEXT,
            resume_path TEXT,
            status TEXT NOT NULL,
            screenshot_path TEXT,
            contacts TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        ''')
    conn.commit()

    # Migration: Add contacts column if it doesn't exist (for existing databases)
    try:
        cur = db_execute(conn, "SELECT * FROM jobs LIMIT 1")
        colnames = [desc[0] for desc in cur.description]
        if 'contacts' not in colnames:
            log_message("Database", "Migrating database: adding 'contacts' column to jobs table...")
            db_execute(conn, "ALTER TABLE jobs ADD COLUMN contacts TEXT")
            conn.commit()
    except Exception as e:
        log_message("Database", f"Database migration error: {str(e)}", "ERROR")

    # Prepopulate with dummy jobs if database is empty for visual showcase
    cur = db_execute(conn, "SELECT COUNT(*) FROM jobs")
    count = cur.fetchone()[0]
    if count == 0:
        log_message("Database", "Pre-populating database with initial sample jobs.")
        
        # Discovered Job
        db_execute(conn, '''
        INSERT INTO jobs (title, company, description, url, location, status)
        VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING
        ''', (
            "Python Developer Fresher", 
            "Reliance Jio Infocomm", 
            "We are seeking a Python Developer Fresher to join our core backend team in Mumbai. You will write high-performance APIs using FastAPI, assist in PostgreSQL schema optimization, and script CI/CD pipelines. This is a local fresher position in Mumbai, India.",
            "https://careers.jio.com/jobs/python-developer-mumbai", 
            "Mumbai, Maharashtra (On-site)", 
            "Discovered"
        ))
        
        # Requires Intervention Job with mock Captcha screenshot and mock contacts
        db_execute(conn, '''
        INSERT INTO jobs (title, company, description, url, location, status, match_score, match_reason, screenshot_path, contacts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING
        ''', (
            "Software Engineer Intern", 
            "WebEngage", 
            "Join WebEngage in Mumbai as a Backend Software Engineer Intern! Help scale our user engagement and marketing automation platforms. You will write code in Python, debug PostgreSQL databases, and configure microservices deployments on Linux (Pop!_OS/Debian).",
            "https://webengage.com/careers/se-intern-mumbai", 
            "Mumbai, India (Hybrid)", 
            "Requires Intervention",
            92,
            "Matches Xavier Institute IT background. Relates to Python, PostgreSQL, and Linux scripting skills.",
            "/static/screenshots/captcha_job_sample.png",
            json.dumps([
                {"name": "Avlesh Singh", "role": "Co-Founder & CEO", "email": "avlesh@webengage.com", "pitch_type": "executive", "status": "pending"},
                {"name": "Ankit Utreja", "role": "CTO", "email": "ankit@webengage.com", "pitch_type": "technical", "status": "pending"},
                {"name": "HR Recruitment", "role": "HR Director", "email": "hr@webengage.com", "pitch_type": "hr", "status": "pending"}
            ])
        ))
        
        # Create a mock CAPTCHA image using PIL
        try:
            from PIL import Image, ImageDraw
            img = Image.new('RGB', (400, 250), color='#1e293b')
            d = ImageDraw.Draw(img)
            d.text((50, 100), "CAPTCHA CHALLENGE DETECTED", fill='#ef4444')
            d.text((50, 130), "Please resolve this challenge manually.", fill='#94a3b8')
            img.save("static/screenshots/captcha_job_sample.png")
        except Exception as e:
            # Fallback, write empty file
            with open("static/screenshots/captcha_job_sample.png", "w") as f:
                f.write("")
        
        conn.commit()
    conn.close()

def update_job_status(job_id: int, status: str):
    conn = get_db_connection()
    db_execute(conn, "UPDATE jobs SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (status, job_id))
    conn.commit()
    conn.close()

def update_job_match(job_id: int, score: int, reason: str):
    conn = get_db_connection()
    db_execute(conn, "UPDATE jobs SET match_score = %s, match_reason = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (score, reason, job_id))
    conn.commit()
    conn.close()

def update_job_resume(job_id: int, resume_path: str):
    conn = get_db_connection()
    db_execute(conn, "UPDATE jobs SET resume_path = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (resume_path, job_id))
    conn.commit()
    conn.close()

def update_job_screenshot(job_id: int, screenshot_path: str):
    conn = get_db_connection()
    db_execute(conn, "UPDATE jobs SET screenshot_path = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (screenshot_path, job_id))
    conn.commit()
    conn.close()

# --- Snov.io Contact Enrichment & Guessed Target Generation ---

_SNOV_TOKEN = None
_SNOV_TOKEN_EXPIRES_AT = 0

def get_snov_access_token():
    global _SNOV_TOKEN, _SNOV_TOKEN_EXPIRES_AT
    current_time = time.time()
    if _SNOV_TOKEN and current_time < _SNOV_TOKEN_EXPIRES_AT - 60:
        return _SNOV_TOKEN
        
    user_id = os.environ.get("SNOV_USER_ID")
    secret = os.environ.get("SNOV_SECRET")
    
    if not user_id or not secret or user_id == "your_snov_user_id_here" or secret == "your_snov_secret_here":
        return None
        
    url = "https://api.snov.io/v2/oauth/access_token"
    payload = {
        "grant_type": "client_credentials",
        "client_id": user_id,
        "client_secret": secret
    }
    
    try:
        response = httpx.post(url, data=payload, timeout=15.0)
        if response.status_code == 200:
            data = response.json()
            _SNOV_TOKEN = data.get("access_token")
            expires_in = data.get("expires_in", 3600)
            _SNOV_TOKEN_EXPIRES_AT = current_time + expires_in
            log_message("Enrichment", "Successfully authenticated with Snov.io API and received new access token.", "SUCCESS")
            return _SNOV_TOKEN
    except Exception as e:
        log_message("Enrichment", f"Snov.io Authentication failed: {str(e)}", "WARNING")
    return None

async def find_snov_contacts(company_name: str) -> list:
    token = get_snov_access_token()
    if not token:
        return []
        
    clean_company = company_name.lower().replace(" ", "").replace(".", "").replace("inc", "").replace("ltd", "").strip()
    domain = f"{clean_company}.com"
    
    headers = {"Authorization": f"Bearer {token}"}
    start_url = "https://api.snov.io/v2/domain-search/start/"
    payload = {"domain": domain}
    
    log_message("Enrichment", f"Initiating Snov.io Domain Search task for domain: '{domain}'...")
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(start_url, headers=headers, data=payload)
            if response.status_code != 200:
                log_message("Enrichment", f"Snov.io Domain Search start failed: Status {response.status_code}", "WARNING")
                return []
            start_data = response.json()
            task_hash = start_data.get("task_hash")
            if not task_hash:
                log_message("Enrichment", f"Snov.io did not return task hash for domain: '{domain}'", "WARNING")
                return []
                
            result_url = f"https://api.snov.io/v2/domain-search/result/{task_hash}"
            max_attempts = 5
            for attempt in range(max_attempts):
                log_message("Enrichment", f"Retrieving Snov.io results (attempt {attempt + 1}/{max_attempts})...")
                res = await client.get(result_url, headers=headers)
                if res.status_code == 200:
                    result_data = res.json()
                    if result_data.get("status") == "processing":
                        await asyncio.sleep(2)
                        continue
                    
                    contacts_data = result_data.get("contacts", {})
                    prospects = contacts_data.get("prospects", [])
                    
                    found_targets = []
                    for p in prospects:
                        position = p.get("position", "").lower()
                        email = p.get("email")
                        name = p.get("name", "Unknown Contact")
                        if not email:
                            continue
                        
                        role = p.get("position", "Executive")
                        pitch_type = "hr"
                        is_match = False
                        
                        if any(w in position for w in ["founder", "co-founder", "ceo", "president"]):
                            pitch_type = "executive"
                            is_match = True
                        elif any(w in position for w in ["cto", "tech lead", "architect", "lead developer", "engineering manager", "technical manager", "it lead"]):
                            pitch_type = "technical"
                            is_match = True
                        elif any(w in position for w in ["hr", "recruiter", "talent", "recruitment", "people", "hiring"]):
                            pitch_type = "hr"
                            is_match = True
                        elif any(w in position for w in ["developer", "engineer", "programmer", "it"]):
                            pitch_type = "technical"
                            is_match = True
                            
                        if is_match:
                            found_targets.append({
                                "name": name,
                                "role": role,
                                "email": email,
                                "pitch_type": pitch_type,
                                "status": "pending"
                            })
                    return found_targets
    except Exception as e:
        log_message("Enrichment", f"Unexpected error during Snov.io Domain Search: {str(e)}", "WARNING")
    return []

async def get_or_generate_contacts(company_name: str, job_description: str) -> list:
    # 1. Attempt Snov.io search
    contacts = await find_snov_contacts(company_name)
    if contacts:
        log_message("Enrichment", f"Discovered {len(contacts)} contacts via Snov.io for {company_name}.", "SUCCESS")
        return contacts
        
    # 2. Fallback to standard guessed addresses
    clean_company = company_name.lower().replace(" ", "").replace(".", "").replace("inc", "").replace("ltd", "").strip()
    domain = f"{clean_company}.com"
    log_message("Enrichment", f"Snov.io not active or returned empty. Pre-populating guessed contacts for {domain}.")
    return [
        {"name": "Founder", "role": "Founder", "email": f"founder@{domain}", "pitch_type": "executive", "status": "pending"},
        {"name": "Co-Founder", "role": "Co-Founder", "email": f"cofounder@{domain}", "pitch_type": "executive", "status": "pending"},
        {"name": "CTO", "role": "CTO", "email": f"cto@{domain}", "pitch_type": "technical", "status": "pending"},
        {"name": "Tech Lead", "role": "Tech Lead", "email": f"techlead@{domain}", "pitch_type": "technical", "status": "pending"},
        {"name": "HR Director", "role": "HR Director", "email": f"hr@{domain}", "pitch_type": "hr", "status": "pending"},
        {"name": "Recruitment Team", "role": "Recruitment Team", "email": f"recruiting@{domain}", "pitch_type": "hr", "status": "pending"}
    ]

# --- Platform Credentials & Playwright Persistent Browser Session Helpers ---

from playwright.async_api import async_playwright

# Global locks and flags for cross-thread coordination
browser_in_use = False
browser_in_use_lock = threading.Lock()

auth_lock = threading.Lock()
platform_auth_status = {
    "linkedin": "unknown",
    "naukri": "unknown",
    "indeed": "unknown"
}

active_2fa = {
    "platform": None,         # "linkedin", "naukri", "indeed"
    "screenshot_path": None,  # relative path for web view
    "code": None,             # user submitted 2FA code
    "status": "idle"          # "idle", "waiting", "submitted"
}

async def acquire_browser():
    """Asynchronously acquires exclusive access to the browser session directory without blocking FastAPI's thread."""
    while True:
        with browser_in_use_lock:
            global browser_in_use
            if not browser_in_use:
                browser_in_use = True
                log_message("BrowserMgr", "Acquired persistent browser session lock.")
                return True
        await asyncio.sleep(1)

def release_browser():
    """Releases the browser session directory lock."""
    with browser_in_use_lock:
        global browser_in_use
        browser_in_use = False
        log_message("BrowserMgr", "Released persistent browser session lock.")

async def get_persistent_context(p, headless=True):
    """Launches Chromium with a persistent user data directory and anti-bot evasions."""
    user_data_dir = os.path.abspath("playwright_session")
    os.makedirs(user_data_dir, exist_ok=True)
    
    # Launch persistent context
    context = await p.chromium.launch_persistent_context(
        user_data_dir=user_data_dir,
        headless=headless,
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-infobars"
        ]
    )
    
    # Inject evasion script to hide navigator.webdriver
    await context.add_init_script("""
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined
        });
    """)
    return context

async def handle_2fa_verification(page, platform: str) -> Optional[str]:
    """Captures a screenshot, sets 2FA waiting state, and polls until user submits a code or cancels."""
    global active_2fa
    screenshot_name = f"2fa_{platform}.png"
    screenshot_path = os.path.join("static", "screenshots", screenshot_name)
    
    log_message("Auth", f"{platform.capitalize()} login requires 2FA or security challenge. Capturing screenshot...", "WARNING")
    await page.screenshot(path=screenshot_path)
    
    with auth_lock:
        active_2fa["platform"] = platform
        active_2fa["screenshot_path"] = f"/static/screenshots/{screenshot_name}"
        active_2fa["status"] = "waiting"
        active_2fa["code"] = None
        
    # Poll until status changes
    while True:
        with auth_lock:
            if active_2fa["status"] == "submitted":
                code = active_2fa["code"]
                # reset
                active_2fa["status"] = "idle"
                active_2fa["platform"] = None
                active_2fa["screenshot_path"] = None
                active_2fa["code"] = None
                log_message("Auth", f"Received 2FA code from dashboard: {code}", "INFO")
                return code
            elif active_2fa["status"] == "idle":
                # Aborted by user
                log_message("Auth", f"2FA verification aborted for {platform.capitalize()}.", "WARNING")
                return None
        await asyncio.sleep(1)

async def check_platform_login_status(platform: str) -> str:
    """Checks if currently logged in to a platform by visiting its main feed or dashboard."""
    await acquire_browser()
    status = "requires_login"
    async with async_playwright() as p:
        try:
            context = await get_persistent_context(p, headless=True)
            page = await context.new_page()
            
            if platform == "linkedin":
                await page.goto("https://www.linkedin.com/feed", timeout=20000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if "/feed" in page.url:
                    status = "logged_in"
            elif platform == "naukri":
                await page.goto("https://www.naukri.com/mnjuser/homepage", timeout=20000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if "homepage" in page.url or "mnjuser" in page.url:
                    status = "logged_in"
            elif platform == "indeed":
                await page.goto("https://profile.indeed.com/", timeout=20000, wait_until="domcontentloaded")
                await asyncio.sleep(2)
                if "secure.indeed.com" not in page.url:
                    status = "logged_in"
            
            await context.close()
        except Exception as e:
            log_message("AuthStatus", f"Error checking {platform} status: {str(e)}", "WARNING")
        finally:
            release_browser()
            
    with auth_lock:
        platform_auth_status[platform] = status
    return status

async def execute_platform_login(platform: str) -> bool:
    """Automates the platform login steps. Prompts for 2FA if triggered."""
    username = os.environ.get(f"{platform.upper()}_USERNAME")
    password = os.environ.get(f"{platform.upper()}_PASSWORD")
    
    if not username or not password or "your_" in username:
        log_message("Auth", f"No credentials configured in .env for {platform.capitalize()}.", "ERROR")
        with auth_lock:
            platform_auth_status[platform] = "requires_login"
        return False
        
    await acquire_browser()
    success = False
    
    async with async_playwright() as p:
        try:
            context = await get_persistent_context(p, headless=True)
            page = await context.new_page()
            
            log_message("Auth", f"Attempting login to {platform.capitalize()} for {username}...")
            
            if platform == "linkedin":
                await page.goto("https://www.linkedin.com/login", timeout=30000)
                await page.fill("input#username", username)
                await page.fill("input#password", password)
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)
                
                # Check for 2FA challenge page
                if "/feed" not in page.url:
                    html_content = await page.content()
                    if "/checkpoint/challenge/" in page.url or "pin" in html_content.lower() or "verification" in html_content.lower():
                        code = await handle_2fa_verification(page, "linkedin")
                        if code:
                            pin_input = await page.query_selector("input[name='pin'], input[id*='pin' i], input[type='text']")
                            if pin_input:
                                await pin_input.fill(code)
                                await page.click("button[type='submit'], #email-pin-submit-button")
                                await page.wait_for_load_state("networkidle")
                                await asyncio.sleep(5)
                
                if "/feed" in page.url:
                    log_message("Auth", "LinkedIn authentication successful!", "SUCCESS")
                    success = True
                    
            elif platform == "naukri":
                await page.goto("https://www.naukri.com/nlogin/login", timeout=30000)
                await page.fill("#usernameField", username)
                await page.fill("#passwordField", password)
                await page.click("button[type='submit']")
                await page.wait_for_load_state("networkidle")
                await asyncio.sleep(3)
                
                html_content = await page.content()
                if "otp" in html_content.lower() or "verify" in html_content.lower() or "verification" in html_content.lower():
                    code = await handle_2fa_verification(page, "naukri")
                    if code:
                        otp_input = await page.query_selector("input[placeholder*='OTP' i], input[id*='otp' i], input[name*='otp' i]")
                        if otp_input:
                            await otp_input.fill(code)
                            submit_btn = await page.query_selector("button:has-text('Verify'), button:has-text('Submit'), button[type='submit']")
                            if submit_btn:
                                await submit_btn.click()
                                await page.wait_for_load_state("networkidle")
                                await asyncio.sleep(5)
                
                if "homepage" in page.url or "mnjuser" in page.url:
                    log_message("Auth", "Naukri authentication successful!", "SUCCESS")
                    success = True
                    
            elif platform == "indeed":
                await page.goto("https://secure.indeed.com/account/login", timeout=30000)
                email_input = await page.query_selector("input[type='email'], input[name='__email']")
                if email_input:
                    await email_input.fill(username)
                    await page.click("button[type='submit']")
                    await asyncio.sleep(2)
                
                pass_input = await page.query_selector("input[type='password'], input[name='__password']")
                if pass_input:
                    await pass_input.fill(password)
                    await page.click("button[type='submit']")
                    await page.wait_for_load_state("networkidle")
                    await asyncio.sleep(3)
                
                html_content = await page.content()
                if "code" in html_content.lower() or "verify" in html_content.lower() or "verification" in html_content.lower():
                    code = await handle_2fa_verification(page, "indeed")
                    if code:
                        code_input = await page.query_selector("input[id*='code' i], input[name*='code' i], input[type='text']")
                        if code_input:
                            await code_input.fill(code)
                            await page.click("button[type='submit']")
                            await page.wait_for_load_state("networkidle")
                            await asyncio.sleep(5)
                            
                await page.goto("https://profile.indeed.com/", timeout=20000)
                await asyncio.sleep(2)
                if "secure.indeed.com" not in page.url:
                    log_message("Auth", "Indeed authentication successful!", "SUCCESS")
                    success = True
            
            await context.close()
        except Exception as e:
            log_message("Auth", f"Authentication failed for {platform.capitalize()}: {str(e)}", "ERROR")
        finally:
            release_browser()
            
    with auth_lock:
        platform_auth_status[platform] = "logged_in" if success else "requires_login"
    return success

# --- Profile Load/Save Helpers ---
PROFILE_FILE = "master_profile.json"

def load_profile_data() -> dict:
    if not os.path.exists(PROFILE_FILE):
        return {}
    with open(PROFILE_FILE, "r") as f:
        return json.load(f)

def save_profile_data(data: dict):
    with open(PROFILE_FILE, "w") as f:
        json.dump(data, f, indent=2)

# --- Antigravity SDK & Agentic Tools ---

async def scrape_jobs(keywords: str) -> str:
    """
    Search job boards for matching job listings using public APIs and authenticated platform queries.
    """
    log_message("Scraper", f"Scraping job boards for: {keywords}")
    keywords_list = [k.strip() for k in keywords.split(",") if k.strip()]
    discovered_jobs = []

    # 1. Fetch from Arbeitnow API
    try:
        log_message("Scraper", "Fetching jobs from Arbeitnow...")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://www.arbeitnow.com/api/job-board-api")
            if response.status_code == 200:
                data = response.json()
                items = data.get("data", [])
                for item in items:
                    title = item.get("title", "")
                    company = item.get("company_name", "")
                    description = item.get("description", "")
                    url = item.get("url", "")
                    location = item.get("location", "Remote")
                    
                    match = False
                    for kw in keywords_list:
                        if kw.lower() in title.lower() or kw.lower() in description.lower():
                            match = True
                            break
                    if match:
                        discovered_jobs.append({
                            "title": title,
                            "company": company,
                            "description": description[:1500],
                            "url": url,
                            "location": location
                        })
    except Exception as e:
        log_message("Scraper", f"Arbeitnow fetch error: {str(e)}", "WARNING")

    # 2. Fetch from Remotive API
    try:
        log_message("Scraper", "Fetching jobs from Remotive...")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://remotive.com/api/remote-jobs?category=software-dev")
            if response.status_code == 200:
                data = response.json()
                items = data.get("jobs", [])
                for item in items:
                    title = item.get("title", "")
                    company = item.get("company_name", "")
                    description = item.get("description", "")
                    url = item.get("url", "")
                    location = item.get("candidate_required_location", "Remote")
                    
                    match = False
                    for kw in keywords_list:
                        if kw.lower() in title.lower() or kw.lower() in description.lower():
                            match = True
                            break
                    if match:
                        discovered_jobs.append({
                            "title": title,
                            "company": company,
                            "description": description[:1500],
                            "url": url,
                            "location": location
                        })
    except Exception as e:
        log_message("Scraper", f"Remotive fetch error: {str(e)}", "WARNING")

    # 3. Fetch from The Muse API
    try:
        log_message("Scraper", "Fetching jobs from The Muse...")
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get("https://www.themuse.com/api/public/jobs?page=1&category=Software+Engineering")
            if response.status_code == 200:
                data = response.json()
                items = data.get("results", [])
                for item in items:
                    title = item.get("name", "")
                    company_data = item.get("company", {})
                    company = company_data.get("name", "")
                    description = item.get("contents", "")
                    refs = item.get("refs", {})
                    url = refs.get("landing_page", "")
                    
                    locations_data = item.get("locations", [])
                    location = locations_data[0].get("name", "Remote") if locations_data else "Remote"
                    
                    match = False
                    for kw in keywords_list:
                        if kw.lower() in title.lower() or kw.lower() in description.lower():
                            match = True
                            break
                    if match:
                        discovered_jobs.append({
                            "title": title,
                            "company": company,
                            "description": description[:1500],
                            "url": url,
                            "location": location
                        })
    except Exception as e:
        log_message("Scraper", f"The Muse fetch error: {str(e)}", "WARNING")

    # 4. Scrape from LinkedIn if logged in
    if platform_auth_status.get("linkedin") == "logged_in":
        try:
            log_message("Scraper", "Fetching jobs from LinkedIn search (authenticated)...")
            await acquire_browser()
            async with async_playwright() as p:
                context = await get_persistent_context(p, headless=True)
                page = await context.new_page()
                for query_kw in keywords_list[:2]: # Query first two keywords to prevent excessive searches
                    kw_encoded = query_kw.replace(" ", "%20")
                    await page.goto(f"https://www.linkedin.com/jobs/search/?keywords={kw_encoded}&location=Mumbai&f_TPR=r86400", timeout=20000)
                    await asyncio.sleep(4)
                    
                    cards = await page.query_selector_all(".jobs-search-results-list__list-item, .job-card-container, .base-card")
                    log_message("Scraper", f"Found {len(cards)} raw cards on LinkedIn for '{query_kw}'.")
                    for card in cards[:8]:
                        try:
                            title_el = await card.query_selector(".disabled.list-style-none a, .job-card-list__title, a.job-card-container__link")
                            if not title_el:
                                continue
                            title = (await title_el.inner_text()).strip()
                            url = (await title_el.get_attribute("href") or "").split("?")[0]
                            if not url.startswith("http"):
                                url = "https://www.linkedin.com" + url
                                
                            comp_el = await card.query_selector(".job-card-container__company-name, .job-card-list__company-name, .base-card__subtitle")
                            company = (await comp_el.inner_text()).strip() if comp_el else "Unknown Company"
                            
                            loc_el = await card.query_selector(".job-card-container__metadata-item, .job-card-list__metadata-item, .base-card__metadata")
                            location = (await loc_el.inner_text()).strip() if loc_el else "Mumbai"
                            
                            discovered_jobs.append({
                                "title": title,
                                "company": company,
                                "description": f"Position: {title} at {company}. Apply directly via LinkedIn Easy Apply.",
                                "url": url,
                                "location": location
                            })
                        except Exception as ce:
                            continue
                await context.close()
        except Exception as e:
            log_message("Scraper", f"LinkedIn scraping error: {str(e)}", "WARNING")
        finally:
            release_browser()

    # 5. Scrape from Indeed if logged in
    if platform_auth_status.get("indeed") == "logged_in":
        try:
            log_message("Scraper", "Fetching jobs from Indeed search (authenticated)...")
            await acquire_browser()
            async with async_playwright() as p:
                context = await get_persistent_context(p, headless=True)
                page = await context.new_page()
                for query_kw in keywords_list[:2]:
                    kw_encoded = query_kw.replace(" ", "+")
                    await page.goto(f"https://in.indeed.com/jobs?q={kw_encoded}&l=Mumbai&fromage=1", timeout=20000)
                    await asyncio.sleep(4)
                    
                    cards = await page.query_selector_all(".job_seen_beacon, td.resultContent")
                    log_message("Scraper", f"Found {len(cards)} raw cards on Indeed for '{query_kw}'.")
                    for card in cards[:8]:
                        try:
                            title_el = await card.query_selector("h2.jobTitle a, a[id*='job_' i]")
                            if not title_el:
                                continue
                            title = (await title_el.inner_text()).strip()
                            jk = await title_el.get_attribute("data-jk")
                            if jk:
                                url = f"https://in.indeed.com/viewjob?jk={jk}"
                            else:
                                url = (await title_el.get_attribute("href") or "")
                                if url and not url.startswith("http"):
                                    url = "https://in.indeed.com" + url
                                    
                            comp_el = await card.query_selector("[data-testid='company-name'], .companyName")
                            company = (await comp_el.inner_text()).strip() if comp_el else "Unknown Company"
                            
                            loc_el = await card.query_selector("[data-testid='text-location'], .companyLocation")
                            location = (await loc_el.inner_text()).strip() if loc_el else "Mumbai"
                            
                            discovered_jobs.append({
                                "title": title,
                                "company": company,
                                "description": f"Position: {title} at {company}. Apply directly on Indeed.",
                                "url": url,
                                "location": location
                            })
                        except Exception as ce:
                            continue
                await context.close()
        except Exception as e:
            log_message("Scraper", f"Indeed scraping error: {str(e)}", "WARNING")
        finally:
            release_browser()

    # 6. Scrape from Naukri if logged in
    if platform_auth_status.get("naukri") == "logged_in":
        try:
            log_message("Scraper", "Fetching jobs from Naukri search (authenticated)...")
            await acquire_browser()
            async with async_playwright() as p:
                context = await get_persistent_context(p, headless=True)
                page = await context.new_page()
                for query_kw in keywords_list[:2]:
                    kw_encoded = query_kw.lower().replace(" ", "-")
                    await page.goto(f"https://www.naukri.com/{kw_encoded}-jobs-in-mumbai?src=jobsearchDesk&xp=1", timeout=20000)
                    await asyncio.sleep(4)
                    
                    cards = await page.query_selector_all(".jobTuple, .cust-job-tuple, [data-job-id]")
                    log_message("Scraper", f"Found {len(cards)} raw cards on Naukri for '{query_kw}'.")
                    for card in cards[:8]:
                        try:
                            title_el = await card.query_selector("a.title, a.cust-job-title")
                            if not title_el:
                                continue
                            title = (await title_el.inner_text()).strip()
                            url = (await title_el.get_attribute("href") or "").split("?")[0]
                            
                            comp_el = await card.query_selector("a.comp-name, a.companyName")
                            company = (await comp_el.inner_text()).strip() if comp_el else "Unknown Company"
                            
                            loc_el = await card.query_selector(".loc-wrap, .location")
                            location = (await loc_el.inner_text()).strip() if loc_el else "Mumbai"
                            
                            discovered_jobs.append({
                                "title": title,
                                "company": company,
                                "description": f"Position: {title} at {company}. Apply directly on Naukri.",
                                "url": url,
                                "location": location
                            })
                        except Exception as ce:
                            continue
                await context.close()
        except Exception as e:
            log_message("Scraper", f"Naukri scraping error: {str(e)}", "WARNING")
        finally:
            release_browser()

    # Deduplicate results by URL
    seen_urls = set()
    deduped_jobs = []
    for job in discovered_jobs:
        if job["url"] not in seen_urls:
            seen_urls.add(job["url"])
            deduped_jobs.append(job)

    # Fallback to realistic mock data if nothing found
    if not deduped_jobs:
        log_message("Scraper", "No active listings found online. Using pre-configured fresher mockups.", "INFO")
        mock_jobs = [
            {
                "title": "Python Backend Intern",
                "company": "Microsoft",
                "description": "We are seeking a Python Backend Intern to join our Cloud Engineering team. You will write backend services, help automate deployment pipelines, and work with PostgreSQL databases. Training on Azure and GitLab CI/CD will be provided. Perfect for freshers or students.",
                "url": "https://careers.microsoft.com/jobs/devops-engineer-demo",
                "location": "Redmond, WA (Hybrid)"
            },
            {
                "title": "Junior Cloud Engineer",
                "company": "GitLab Inc.",
                "description": "Join GitLab as a Junior Cloud Infrastructure Specialist! This entry-level role focuses on supporting our CI/CD pipelines, automating Linux environments, and deploying services. Python scripting and basic PostgreSQL familiarity is required.",
                "url": "https://about.gitlab.com/jobs/cloud-infra-demo",
                "location": "Remote"
            },
            {
                "title": "Software Engineer Intern (Python/Django)",
                "company": "Spotify",
                "description": "We are looking for a Python Developer Intern to build and optimize backend services. You will work on database queries, write scripts in Python, and help configure GitLab CI/CD. Familiarity with Linux development is a plus. Great learning opportunity.",
                "url": "https://spotify.com/careers/python-developer-demo",
                "location": "New York, NY (Remote)"
            },
            {
                "title": "Graduate Cloud Engineer (Azure/Python)",
                "company": "Google",
                "description": "We are seeking a Graduate Software Engineer. The candidate will work on cloud automation scripts, system scaling, and backend architectures. Requirements include basic knowledge of Python, PostgreSQL, and deploying services on cloud VMs (Google Cloud / Azure).",
                "url": "https://careers.google.com/jobs/ml-engineer-demo",
                "location": "Mountain View, CA"
            },
            {
                "title": "IT Support & DevOps Intern",
                "company": "Red Hat",
                "description": "DevOps intern wanted. Help script GitLab CI/CD workflows, configure Pop!_OS and Linux environments, and automate PostgreSQL backups on cloud VMs. Python scripting is a must.",
                "url": "https://redhat.com/jobs/devops-lead-demo",
                "location": "Boston, MA (Hybrid)"
            }
        ]
        for job in mock_jobs:
            for kw in keywords_list:
                if kw.lower() in job["title"].lower() or kw.lower() in job["description"].lower():
                    deduped_jobs.append(job)
                    break

    log_message("Scraper", f"Successfully scraped {len(deduped_jobs)} matching job listings.", "SUCCESS")
    return json.dumps(deduped_jobs)


async def evaluate_match(job_description: str, master_profile: str) -> str:
    """
    Use an LLM call to return a job compatibility match score (0-100) and rationale.
    """
    log_message("Evaluator", "Starting match evaluation...")
    profile = json.loads(master_profile)

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key != "your_gemini_api_key_here":
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = f"""
            Compare the candidate's profile with the job description.
            
            Profile:
            {json.dumps(profile)}
            
            Job Description:
            {job_description}
            
            Return a JSON object containing:
            1. "score": An integer from 0 to 100 representing how well the candidate fits the role based on skills, education, and projects.
            2. "reason": A brief 1-2 sentence explanation of the score.
            
            Do not include markdown selectors or other wrappers. Output ONLY valid JSON.
            """
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            resp_text = response.text.strip()
            if resp_text.startswith("```"):
                resp_text = resp_text.split("```")[1]
                if resp_text.startswith("json"):
                    resp_text = resp_text[4:]
            
            result = json.loads(resp_text.strip())
            log_message("Evaluator", f"Gemini Evaluation - Score: {result['score']}", "SUCCESS")
            return json.dumps(result)
        except Exception as e:
            log_message("Evaluator", f"Gemini API failure: {str(e)}. Falling back to keyword logic.", "WARNING")

    # Fallback to local keyword evaluation
    skills = profile.get("skills", [])
    matched_skills = []
    for skill in skills:
        if skill.lower() in job_description.lower():
            matched_skills.append(skill)

    score = 50 + (len(matched_skills) * 8)
    if score > 100:
        score = 100
    reason = f"Keyword fallback match: Found overlapping core skills ({', '.join(matched_skills)})."
    
    result = {"score": score, "reason": reason}
    log_message("Evaluator", f"Fallback Evaluation - Score: {score}", "SUCCESS")
    return json.dumps(result)


async def generate_tailored_pdf(job_description: str, master_profile: str, job_id: int) -> str:
    """
    Select the most relevant projects based on job description and compile a clean resume PDF locally.
    """
    log_message("ResumeBuilder", f"Tailoring resume PDF for Job ID {job_id}...")
    profile = json.loads(master_profile)
    projects = profile.get("projects", [])
    selected_projects = []

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key != "your_gemini_api_key_here":
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = f"""
            Analyze the job description and pick the top 2 most relevant projects for the candidate.
            
            Job:
            {job_description}
            
            Candidate Projects:
            {json.dumps(projects)}
            
            Return a JSON array of the indexes of the selected projects (e.g. [0, 2]). Only return raw JSON.
            """
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            resp_text = response.text.strip()
            if resp_text.startswith("```"):
                resp_text = resp_text.split("```")[1]
                if resp_text.startswith("json"):
                    resp_text = resp_text[4:]
            
            indexes = json.loads(resp_text.strip())
            for idx in indexes:
                if 0 <= idx < len(projects):
                    selected_projects.append(projects[idx])
        except Exception as e:
            log_message("ResumeBuilder", f"Gemini project selection failed: {str(e)}. Using keyword overlaps.", "WARNING")

    # Fallback to keyword heuristics if LLM failed
    if len(selected_projects) < 2:
        selected_projects = []
        project_scores = []
        for p in projects:
            p_score = 0
            keywords = p.get("technologies", []) + p.get("title", "").split()
            for kw in keywords:
                if kw.lower() in job_description.lower():
                    p_score += 10
            project_scores.append(p_score)
        
        top_indexes = sorted(range(len(project_scores)), key=lambda i: project_scores[i], reverse=True)[:2]
        for idx in top_indexes:
            selected_projects.append(projects[idx])

    # ReportLab PDF compile
    from reportlab.lib.pagesizes import letter
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.lib import colors

    pdf_filename = f"resume_job_{job_id}.pdf"
    pdf_path = os.path.join("static", "resumes", pdf_filename)

    try:
        doc = SimpleDocTemplate(pdf_path, pagesize=letter, rightMargin=54, leftMargin=54, topMargin=54, bottomMargin=54)
        story = []
        styles = getSampleStyleSheet()

        # Styles
        title_style = ParagraphStyle(
            'ResumeTitle', parent=styles['Heading1'], fontName='Helvetica-Bold', fontSize=22,
            leading=26, textColor=colors.HexColor("#1e293b"), spaceAfter=3
        )
        subtitle_style = ParagraphStyle(
            'ResumeSubtitle', parent=styles['Normal'], fontName='Helvetica', fontSize=9,
            leading=13, textColor=colors.HexColor("#64748b"), spaceAfter=12
        )
        section_heading = ParagraphStyle(
            'SectionHeading', parent=styles['Heading2'], fontName='Helvetica-Bold', fontSize=12,
            leading=16, textColor=colors.HexColor("#0f172a"), spaceBefore=10, spaceAfter=5, keepWithNext=True
        )
        body_style = ParagraphStyle(
            'ResumeBody', parent=styles['BodyText'], fontName='Helvetica', fontSize=9.5,
            leading=13, textColor=colors.HexColor("#334155"), spaceAfter=5
        )
        bullet_style = ParagraphStyle(
            'ResumeBullet', parent=body_style, leftIndent=12, firstLineIndent=-8, spaceAfter=3
        )

        # Header details
        story.append(Paragraph(profile.get("name", "Aman Parab"), title_style))
        contact = profile.get("contact", {})
        edu = profile.get("education", {})
        header_text = f"Email: {contact.get('email')} | Phone: {contact.get('phone')} | Degree: {edu.get('degree')} (CGPA: {edu.get('cgpa')})"
        story.append(Paragraph(header_text, subtitle_style))

        # Thin rule divider
        story.append(Table([[""]], colWidths=[504], rowHeights=[1], style=TableStyle([
            ('LINEABOVE', (0,0), (-1,-1), 0.5, colors.HexColor("#cbd5e1")),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
            ('TOPPADDING', (0,0), (-1,-1), 0)
        ])))
        story.append(Spacer(1, 8))

        # Education
        story.append(Paragraph("Education", section_heading))
        edu_line = f"<b>{edu.get('degree')}</b> - {edu.get('institution')} (CGPA: {edu.get('cgpa')})"
        story.append(Paragraph(edu_line, body_style))

        # Core Skills
        story.append(Paragraph("Core Skills", section_heading))
        story.append(Paragraph(", ".join(profile.get("skills", [])), body_style))

        # Selected Tailored Projects
        story.append(Paragraph("Tailored Projects Highlight", section_heading))
        for p in selected_projects:
            story.append(Paragraph(f"<b>{p.get('title')}</b> <i>({', '.join(p.get('technologies', []))})</i>", body_style))
            story.append(Paragraph(f"• {p.get('details')}", bullet_style))
            story.append(Spacer(1, 3))

        doc.build(story)
        log_message("ResumeBuilder", f"Resume tailormade and built: {pdf_path}", "SUCCESS")
        return f"/static/resumes/{pdf_filename}"
    except Exception as e:
        log_message("ResumeBuilder", f"PDF build crash: {str(e)}", "ERROR")
        return ""


async def auto_apply(job_url: str, pdf_path: str, profile_data: str, job_id: int) -> str:
    """
    Automate field-mapping, form uploads, and submissions via Playwright. Capture CAPTCHA fallbacks.
    Supports platform-specific logic for LinkedIn, Indeed, and Naukri.
    """
    from playwright.async_api import async_playwright
    log_message("AutoApply", f"Launching playwright sandbox for Job ID {job_id}...")
    profile = json.loads(profile_data)

    pdf_abs_path = os.path.abspath(pdf_path.lstrip("/"))
    screenshot_name = f"captcha_job_{job_id}.png"
    screenshot_path = os.path.join("static", "screenshots", screenshot_name)

    is_platform = any(k in job_url.lower() for k in ["linkedin.com", "indeed.com", "naukri.com"])
    
    if is_platform:
        await acquire_browser()

    async with async_playwright() as p:
        try:
            if is_platform:
                context = await get_persistent_context(p, headless=True)
            else:
                browser = await p.chromium.launch(headless=True)
                context = await browser.new_context(
                    viewport={"width": 1280, "height": 800},
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
                )
            
            page = await context.new_page()
            log_message("AutoApply", f"Opening application URL: {job_url}")
            await page.goto(job_url, timeout=40000, wait_until="load")
            await asyncio.sleep(4)

            # Check for CAPTCHA elements
            html_content = await page.content()
            captcha_words = ["g-recaptcha", "recaptcha", "hcaptcha", "cloudflare", "turnstile", "cf-challenge", "please verify you are human"]
            captcha_hit = False
            for word in captcha_words:
                if word in html_content.lower():
                    captcha_hit = True
                    break
            
            # Check frames for CAPTCHAs
            for frame in page.frames:
                if any(k in (frame.url or "").lower() for k in ["recaptcha", "hcaptcha", "turnstile", "cloudflare"]):
                    captcha_hit = True
                    break

            if captcha_hit:
                log_message("AutoApply", "Security block / CAPTCHA page detected.", "WARNING")
                await page.screenshot(path=screenshot_path)
                update_job_screenshot(job_id, f"/static/screenshots/{screenshot_name}")
                update_job_status(job_id, "Requires Intervention")
                await context.close()
                if not is_platform:
                    await browser.close()
                return "Requires Intervention: CAPTCHA detected."

            # --- Platform-Specific Form Handling ---
            
            if "linkedin.com" in job_url.lower():
                log_message("AutoApply", "LinkedIn URL detected. Searching for Easy Apply...")
                easy_apply_btn = await page.query_selector("button.jobs-apply-button, button[aria-label*='Easy Apply' i], button:has-text('Easy Apply')")
                
                if not easy_apply_btn:
                    # Check if already applied
                    already_applied = await page.query_selector(".jobs-s-apply__applied-date, .artdeco-inline-feedback--success")
                    if already_applied:
                        log_message("AutoApply", "LinkedIn job already applied to.", "SUCCESS")
                        update_job_status(job_id, "Applied")
                        await context.close()
                        return "Applied successfully."
                        
                    # Standard Apply redirects to external site
                    apply_btn = await page.query_selector("button.jobs-apply-button")
                    if apply_btn:
                        log_message("AutoApply", "LinkedIn job has external apply. Clicking redirect...")
                        async with page.context.expect_page() as new_page_info:
                            await apply_btn.click()
                        page = await new_page_info.value
                        await page.wait_for_load_state("load")
                        await asyncio.sleep(4)
                        # We continue below as generic external form
                    else:
                        log_message("AutoApply", "No apply button found on LinkedIn page.", "WARNING")
                        update_job_status(job_id, "Requires Intervention")
                        await context.close()
                        return "Requires Intervention: Apply button missing."
                
                else:
                    log_message("AutoApply", "Found Easy Apply. Opening LinkedIn application modal...")
                    await easy_apply_btn.click()
                    await asyncio.sleep(3)
                    
                    # Navigate Easy Apply multi-step modal
                    step = 0
                    applied = False
                    while step < 10:
                        step += 1
                        await page.screenshot(path=screenshot_path)
                        update_job_screenshot(job_id, f"/static/screenshots/{screenshot_name}")
                        
                        # Check if any input fields need filling
                        inputs = await page.query_selector_all("input[type='text'], input[type='tel'], input[type='email']")
                        for inp in inputs:
                            val = await inp.input_value()
                            if not val:
                                name_attr = await inp.get_attribute("name") or ""
                                id_attr = await inp.get_attribute("id") or ""
                                placeholder = await inp.get_attribute("placeholder") or ""
                                combined = (name_attr + id_attr + placeholder).lower()
                                
                                if "phone" in combined or "mobile" in combined or "tel" in combined:
                                    await inp.fill(profile.get("contact", {}).get("phone", ""))
                                elif "email" in combined:
                                    await inp.fill(profile.get("contact", {}).get("email", ""))
                                elif "name" in combined:
                                    await inp.fill(profile.get("name", ""))
                                    
                        # Check if upload file exists
                        file_input = await page.query_selector("input[type='file']")
                        if file_input:
                            await file_input.set_input_files(pdf_abs_path)
                            await asyncio.sleep(2)
                            
                        # Handle screening questions (radio/checkboxes/text)
                        # Find text area or text input screening questions
                        text_questions = await page.query_selector_all("input[type='text'], textarea")
                        for tq in text_questions:
                            val = await tq.input_value()
                            if not val:
                                label_el = await page.query_selector(f"label[for='{await tq.get_attribute('id')}']")
                                label_text = await label_el.inner_text() if label_el else ""
                                if any(w in label_text.lower() for w in ["experience", "years"]):
                                    await tq.fill("1") # Assume 1 year for fresher
                                elif "gpa" in label_text.lower() or "grade" in label_text.lower():
                                    await tq.fill("7.88")
                                else:
                                    await tq.fill("Yes")
                                    
                        # Find radio questions (typically yes/no)
                        radio_groups = await page.query_selector_all("fieldset")
                        for rg in radio_groups:
                            # If no radio is checked, check the 'Yes' option
                            checked = await rg.query_selector("input[type='radio']:checked")
                            if not checked:
                                yes_opt = await rg.query_selector("input[type='radio'][value*='yes' i], label:has-text('Yes')")
                                if yes_opt:
                                    await yes_opt.click()
                                    
                        # Locate navigation buttons
                        next_btn = await page.query_selector("button:has-text('Next'), button:has-text('Continue'), button:has-text('Review')")
                        submit_btn = await page.query_selector("button:has-text('Submit application'), button:aria-label*='Submit application' i")
                        
                        if submit_btn:
                            log_message("AutoApply", "Submitting LinkedIn Easy Apply application...")
                            await submit_btn.click()
                            await asyncio.sleep(4)
                            applied = True
                            break
                        elif next_btn:
                            await next_btn.click()
                            await asyncio.sleep(2)
                        else:
                            # Stuck or finished
                            break
                            
                    if applied:
                        log_message("AutoApply", "LinkedIn Easy Apply application submitted successfully!", "SUCCESS")
                        update_job_status(job_id, "Applied")
                        await context.close()
                        return "Applied successfully."
                    else:
                        log_message("AutoApply", "Easy Apply modal got stuck or had complex custom questions. Marking for review.", "WARNING")
                        update_job_status(job_id, "Requires Intervention")
                        await context.close()
                        return "Requires Intervention: Easy Apply stuck."
                        
            elif "indeed.com" in job_url.lower():
                log_message("AutoApply", "Indeed URL detected. Searching for Indeed Apply...")
                indeed_apply_btn = await page.query_selector("button#indeedApplyButton, .indeed-apply-button, button:has-text('Apply Now')")
                
                if indeed_apply_btn:
                    log_message("AutoApply", "Clicking Indeed Apply button...")
                    await indeed_apply_btn.click()
                    await asyncio.sleep(4)
                    await page.screenshot(path=screenshot_path)
                    update_job_screenshot(job_id, f"/static/screenshots/{screenshot_name}")
                    log_message("AutoApply", "Indeed Apply modal opened. Marking for user verification.", "WARNING")
                    update_job_status(job_id, "Requires Intervention")
                    await context.close()
                    return "Requires Intervention: Indeed Apply verification needed."
                else:
                    log_message("AutoApply", "Indeed job is external or redirect. Running external autofill...")
                    
            elif "naukri.com" in job_url.lower():
                log_message("AutoApply", "Naukri URL detected. Attempting direct apply...")
                apply_btn = await page.query_selector("button:has-text('Apply'), .apply-button")
                if apply_btn:
                    await apply_btn.click()
                    await asyncio.sleep(4)
                    log_message("AutoApply", "Clicked Naukri Apply button.", "SUCCESS")
                    update_job_status(job_id, "Applied")
                    await context.close()
                    return "Applied successfully."
                else:
                    log_message("AutoApply", "Naukri apply button not found.", "WARNING")
                    update_job_status(job_id, "Requires Intervention")
                    await context.close()
                    return "Requires Intervention: Naukri apply button missing."

            # --- Generic / External Form Autofill ---
            log_message("AutoApply", "Running generic form filler on page...")
            name_filled = False
            email_filled = False
            phone_filled = False
            file_uploaded = False

            # Selectors
            name_selectors = ["input[name*='name' i]", "input[id*='name' i]", "input[placeholder*='name' i]"]
            for sel in name_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(profile.get("name", ""))
                        name_filled = True
                        break
                except:
                    continue

            email_selectors = ["input[type='email']", "input[name*='email' i]", "input[placeholder*='email' i]"]
            for sel in email_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(profile.get("contact", {}).get("email", ""))
                        email_filled = True
                        break
                except:
                    continue

            phone_selectors = ["input[type='tel']", "input[name*='phone' i]", "input[placeholder*='phone' i]"]
            for sel in phone_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        await el.fill(profile.get("contact", {}).get("phone", ""))
                        phone_filled = True
                        break
                except:
                    continue

            file_selectors = ["input[type='file']", "input[name*='resume' i]"]
            for sel in file_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.set_input_files(pdf_abs_path)
                        file_uploaded = True
                        break
                except:
                    continue

            await page.screenshot(path=screenshot_path)
            update_job_screenshot(job_id, f"/static/screenshots/{screenshot_name}")

            if name_filled and email_filled and file_uploaded:
                log_message("AutoApply", "Application form details mapped. Submitting form...")
                submit_selectors = ["button[type='submit']", "input[type='submit']", "button:has-text('Submit')"]
                for sel in submit_selectors:
                    try:
                        btn = await page.query_selector(sel)
                        if btn and await btn.is_visible():
                            await btn.click()
                            await asyncio.sleep(4)
                            break
                    except:
                        continue
                
                log_message("AutoApply", "Application submitted successfully!", "SUCCESS")
                update_job_status(job_id, "Applied")
                await context.close()
                if not is_platform:
                    await browser.close()
                return "Applied successfully."
            else:
                log_message("AutoApply", "Application layout complex. Marking for user intervention.", "WARNING")
                update_job_status(job_id, "Requires Intervention")
                await context.close()
                if not is_platform:
                    await browser.close()
                return "Requires Intervention: Custom layout."

        except Exception as e:
            log_message("AutoApply", f"Automation crash: {str(e)}", "ERROR")
            try:
                await page.screenshot(path=screenshot_path)
                update_job_screenshot(job_id, f"/static/screenshots/{screenshot_name}")
            except:
                pass
            update_job_status(job_id, "Requires Intervention")
            try:
                await context.close()
                if not is_platform:
                    await browser.close()
            except:
                pass
            return f"Requires Intervention: automation failure."
        finally:
            if is_platform:
                release_browser()


async def send_cold_email(company_name: str, job_description: str, resume_path: str, recruiter_email: str = None, job_id: int = None) -> str:
    """
    Draft highly technical pitches and email recruiters, founders, and co-founders using Brevo's web API.
    """
    import base64
    log_message("Emailer", f"Analyzing target contacts for {company_name}...")
    
    # 1. Retrieve contacts from database if job_id is provided
    targets = []
    if job_id:
        conn = get_db_connection()
        row = db_execute(conn, "SELECT contacts FROM jobs WHERE id = %s", (job_id,)).fetchone()
        conn.close()
        if row and row["contacts"]:
            try:
                targets = json.loads(row["contacts"])
            except Exception as e:
                log_message("Emailer", f"Failed to parse contacts JSON: {str(e)}", "WARNING")
                
    if not targets:
        if recruiter_email:
            targets = [{"name": "Recruiter", "role": "Recruiter/Override", "email": recruiter_email, "pitch_type": "hr", "status": "pending"}]
        else:
            clean_company = company_name.lower().replace(" ", "").replace(".", "").replace("inc", "").replace("ltd", "").strip()
            domain = f"{clean_company}.com"
            targets = [
                {"name": "Founder", "role": "Founder", "email": f"founder@{domain}", "pitch_type": "executive", "status": "pending"},
                {"name": "Co-Founder", "role": "Co-Founder", "email": f"cofounder@{domain}", "pitch_type": "executive", "status": "pending"},
                {"name": "HR Director", "role": "HR Director", "email": f"hr@{domain}", "pitch_type": "hr", "status": "pending"},
                {"name": "Recruitment Team", "role": "Recruitment Team", "email": f"recruiting@{domain}", "pitch_type": "hr", "status": "pending"}
            ]

    # Check for Brevo configuration
    brevo_key = os.environ.get("BREVO_API_KEY", "your_brevo_api_key_here")
    sender_email = os.environ.get("SENDER_EMAIL", "amanparab007@gmail.com")
    sender_name = os.environ.get("SENDER_NAME", "Aman Parab")

    # Encode resume PDF
    attachment = []
    pdf_abs_path = os.path.abspath(resume_path.lstrip("/"))
    if os.path.exists(pdf_abs_path):
        try:
            with open(pdf_abs_path, "rb") as f:
                pdf_b64 = base64.b64encode(f.read()).decode("utf-8")
            attachment.append({
                "content": pdf_b64,
                "name": os.path.basename(pdf_abs_path)
            })
            log_message("Emailer", f"Encoded tailored resume PDF successfully.")
        except Exception as e:
            log_message("Emailer", f"Failed to encode resume: {str(e)}", "WARNING")
    else:
        log_message("Emailer", f"Resume PDF not found at {pdf_abs_path}", "WARNING")

    api_key = os.environ.get("GEMINI_API_KEY")
    dispatched_log = []
    updated_targets = []

    # Send personalized emails to each target contact
    for target in targets:
        # Skip if already sent successfully
        if target.get("status") == "sent":
            updated_targets.append(target)
            dispatched_log.append(f"{target['role']} ({target['email']}) [Already Sent]")
            continue
            
        target_email = target["email"]
        target_role = target["role"]
        pitch_type = target.get("pitch_type", "hr")
        target_name = target.get("name", target_role)
        
        subject = f"Cloud / Backend Engineering Inquiry - Aman Parab"
        email_body = ""
        
        # 3. Draft customized emails based on target role
        if api_key and api_key != "your_gemini_api_key_here":
            try:
                from google import genai
                client = genai.Client(api_key=api_key)
                
                role_instructions = ""
                if pitch_type == "executive":
                    role_instructions = f"The recipient is a Founder/Co-Founder/Executive named {target_name}. Focus on business value, backend scalability, and infrastructure cost/efficiency. Make it brief."
                elif pitch_type == "technical":
                    role_instructions = f"The recipient is a CTO, Tech Lead, or Engineer named {target_name}. Focus on specific technical projects, coding practices (Python, PostgreSQL, GitLab CI/CD pipelines, Pop!_OS Linux), and how your engineering background fits their stack. Mention you are seeking a fresher/internship opportunity."
                else:
                    role_instructions = f"The recipient is an HR Recruiter/Director named {target_name}. Focus on skills alignment (Python, Azure, CI/CD, ML), Xavier Institute education (7.88 CGPA), and prompt availability for fresher/internship roles."
                
                prompt = f"""
                You are Aman Parab. Write a brief technical cold email to the {target_role} ({target_name}) of {company_name}.
                Core skills: Python, Microsoft Azure, GitLab CI/CD, PostgreSQL, Linux, Machine Learning.
                Job description context: {job_description[:400]}
                
                Instructions:
                - {role_instructions}
                - Refer to the attached resume.
                - Max 3 short paragraphs.
                
                Return ONLY the subject line starting with 'Subject: ' on line 1, and the body starting on line 3.
                """
                response = client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=prompt
                )
                lines = response.text.strip().split("\n")
                if lines[0].startswith("Subject:"):
                    subject = lines[0].replace("Subject:", "").strip()
                    email_body = "\n".join(lines[2:]).strip()
                else:
                    email_body = response.text.strip()
            except Exception as e:
                log_message("Emailer", f"Gemini draft failed for {target_role}: {str(e)}. Using standard fallback.", "WARNING")

        if not email_body:
            # Fallbacks
            if pitch_type == "executive":
                email_body = f"""Dear {target_name},

I wanted to reach out regarding engineering efficiency and infrastructure scaling at {company_name}. I am a Cloud Systems and Backend Developer specializing in Python architectures, Microsoft Azure VM automation, PostgreSQL databases, and GitLab CI/CD pipelines.

I focus on infrastructure-as-code automation and backend scalability. I am looking for a tech fresher or internship role where I can help automate delivery flows and reduce server overhead.

My resume is attached. I would appreciate 10 minutes to discuss how my automation skills can benefit {company_name}.

Best regards,
Aman Parab
+91-9324101109 | amanparab007@gmail.com"""
            elif pitch_type == "technical":
                email_body = f"""Dear {target_name},

I wanted to reach out to a fellow engineer regarding backend and cloud initiatives at {company_name}. I am a recent B.E. IT graduate from Xavier Institute of Engineering (7.88 CGPA) looking for a Python Backend or Cloud Engineering Fresher / Intern position.

I have hands-on experience developing Python architectures, scripting GitLab CI/CD pipelines to automate microservices deployment on Azure VMs, and structuring PostgreSQL databases. I develop in Pop!_OS Linux and love solving algorithmic challenges.

I've attached my resume. I'd love to chat briefly about how my training and automated cloud skills could help speed up developments in your engineering team.

Best regards,
Aman Parab
+91-9324101109 | amanparab007@gmail.com"""
            else:
                email_body = f"""Dear {target_name},

I recently reviewed your backend engineering initiatives and wanted to connect. I am a recent B.E. IT graduate (Xavier Institute of Engineering, CGPA: 7.88) seeking a Backend / Cloud Systems Engineer Fresher or Intern position.

My credentials include automating GitLab CI/CD workflows for deploying microservices onto Azure VMs, engineering real-time ML translation networks (CNNs), and constructing analytical PostgreSQL databases.

I've attached my tailored resume for your reference. I would welcome the opportunity to chat about how my qualifications align with {company_name}'s open fresher or intern roles.

Best regards,
Aman Parab
+91-9324101109 | amanparab007@gmail.com"""

        # 4. Dispatch
        if brevo_key == "your_brevo_api_key_here" or not brevo_key:
            log_message("Emailer", f"[SANDBOX] Simulating Brevo email dispatch to {target_role} ({target_email})...")
            log_message("Emailer", f"Mock Sent -> To: {target_email} | Subject: {subject}", "SUCCESS")
            target["status"] = "sent"
            dispatched_log.append(f"{target_role} ({target_email})")
            updated_targets.append(target)
            continue

        try:
            url = "https://api.brevo.com/v3/smtp/email"
            headers = {
                "accept": "application/json",
                "api-key": brevo_key,
                "content-type": "application/json"
            }
            
            payload = {
                "sender": {"name": sender_name, "email": sender_email},
                "to": [{"email": target_email}],
                "subject": subject,
                "textContent": email_body
            }
            if attachment:
                payload["attachment"] = attachment

            async with httpx.AsyncClient(timeout=15.0) as client:
                response = await client.post(url, headers=headers, json=payload)
                if response.status_code in [200, 201, 202]:
                    log_message("Emailer", f"Email successfully sent to {target_role} ({target_email}) via Brevo API.", "SUCCESS")
                    target["status"] = "sent"
                    dispatched_log.append(f"{target_role} ({target_email})")
                else:
                    log_message("Emailer", f"Brevo API error for {target_role} (Status {response.status_code}): {response.text}", "ERROR")
                    target["status"] = "failed"
        except Exception as e:
            log_message("Emailer", f"Brevo API request failed for {target_role}: {str(e)}", "ERROR")
            target["status"] = "failed"
            
        updated_targets.append(target)

    # 5. Write updated contacts back to database if job_id is provided
    if job_id:
        conn = get_db_connection()
        db_execute(conn, "UPDATE jobs SET contacts = %s WHERE id = %s", (json.dumps(updated_targets), job_id))
        conn.commit()
        conn.close()

    if dispatched_log:
        return f"Outreach successfully sent to: {', '.join(dispatched_log)}"
    else:
        return "Failed: No outreach emails could be successfully sent."

# --- Background PTA Orchestrator Loop ---

async def run_local_pipeline_step():
    """
    Executes one sequential pass of the job-hunting pipeline.
    """
    profile = load_profile_data()

    # 1. Scrape if Discovered count is low
    conn = get_db_connection()
    disc_count = db_execute(conn, "SELECT COUNT(*) FROM jobs WHERE status = 'Discovered'").fetchone()[0]
    conn.close()

    if disc_count < 2:
        log_message("AgentLoop", "Discovered jobs count is low. Scraping job listings...")
        scraped_json = await scrape_jobs("Python Fresher Mumbai, Software Engineer Intern Mumbai, Web Developer Fresher Mumbai, DevOps Intern Mumbai, Graduate Engineer Mumbai")
        scraped_jobs = json.loads(scraped_json)
        
        conn = get_db_connection()
        ins_count = 0
        for j in scraped_jobs:
            cur = db_execute(conn, '''
            INSERT INTO jobs (title, company, description, url, location, status)
            VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING
            ''', (j["title"], j["company"], j["description"], j["url"], j["location"], "Discovered"))
            if cur.rowcount > 0:
                ins_count += 1
        conn.commit()
        conn.close()
        log_message("AgentLoop", f"Inserted {ins_count} new job listings into ledger.")

    # 2. Evaluate Discovered matches
    conn = get_db_connection()
    discovered_jobs = db_execute(conn, "SELECT * FROM jobs WHERE status = 'Discovered'").fetchall()
    conn.close()

    for job in discovered_jobs:
        job_id = job["id"]
        log_message("AgentLoop", f"Evaluating match for Job ID {job_id} ({job['title']} at {job['company']})...")
        eval_json = await evaluate_match(job["description"], json.dumps(profile))
        eval_data = json.loads(eval_json)
        
        score = eval_data.get("score", 50)
        reason = eval_data.get("reason", "Keyword matching completed.")
        
        update_job_match(job_id, score, reason)
        if score >= 70:
            update_job_status(job_id, "Tailored")
            log_message("AgentLoop", f"Job ID {job_id} scored {score}% and was promoted to 'Tailored'.", "SUCCESS")
            try:
                contacts = await get_or_generate_contacts(job["company"], job["description"])
                conn = get_db_connection()
                db_execute(conn, "UPDATE jobs SET contacts = %s WHERE id = %s", (json.dumps(contacts), job_id))
                conn.commit()
                conn.close()
            except Exception as e:
                log_message("AgentLoop", f"Failed to populate contacts for Job ID {job_id}: {str(e)}", "WARNING")
        else:
            update_job_status(job_id, "Archived")
            log_message("AgentLoop", f"Job ID {job_id} scored {score}%. Marked as Archived.", "INFO")
        
        await asyncio.sleep(random.randint(2, 5))

    # 3. Process Tailored applications
    conn = get_db_connection()
    tailored_jobs = db_execute(conn, "SELECT * FROM jobs WHERE status = 'Tailored'").fetchall()
    conn.close()

    for job in tailored_jobs:
        job_id = job["id"]
        # Check if a custom uploaded resume exists
        default_resume = "static/default_resume.pdf"
        if os.path.exists(default_resume):
            pdf_path = "/static/default_resume.pdf"
            log_message("AgentLoop", f"Found custom default resume. Using it for Job ID {job_id}.")
            update_job_resume(job_id, pdf_path)
            # Submit application
            apply_res = await auto_apply(job["url"], pdf_path, json.dumps(profile), job_id)
            log_message("AgentLoop", f"Job ID {job_id} application: {apply_res}")
        else:
            # Generate resume
            pdf_path = await generate_tailored_pdf(job["description"], json.dumps(profile), job_id)
            if pdf_path:
                update_job_resume(job_id, pdf_path)
                # Submit application
                apply_res = await auto_apply(job["url"], pdf_path, json.dumps(profile), job_id)
                log_message("AgentLoop", f"Job ID {job_id} application: {apply_res}")
            else:
                log_message("AgentLoop", f"Resume tailoring failed for Job ID {job_id}", "ERROR")

        # Rate-limiting delay
        delay = random.randint(60, 180)
        log_message("AgentLoop", f"Rate-limit pause: sleeping for {delay} seconds...")
        await asyncio.sleep(delay)

    # 4. Dispatch cold outreach for Applied jobs
    conn = get_db_connection()
    applied_jobs = db_execute(conn, "SELECT * FROM jobs WHERE status = 'Applied'").fetchall()
    conn.close()

    for job in applied_jobs:
        job_id = job["id"]
        email_res = await send_cold_email(job["company"], job["description"], job["resume_path"], job_id=job_id)
        log_message("AgentLoop", f"Job ID {job_id} Outreach: {email_res}")
        
        if "successfully" in email_res or "Sent" in email_res:
            update_job_status(job_id, "Emailed")
            
        # Rate-limiting delay
        delay = random.randint(60, 180)
        log_message("AgentLoop", f"Rate-limit pause: sleeping for {delay} seconds...")
        await asyncio.sleep(delay)

async def agent_execution_loop():
    """
    Main loop boots google-antigravity runtime context if available, otherwise fallback.
    """
    log_message("AgentLoop", "Initializing Google Antigravity Agent Runtime...")
    
    try:
        from google.antigravity import Agent, LocalAgentConfig
        
        config = LocalAgentConfig(
            tools=[scrape_jobs, evaluate_match, generate_tailored_pdf, auto_apply, send_cold_email],
            system_instructions=(
                "You are an autonomous job-hunting agent automation. "
                "Monitor the SQLite ledger, fetch, score, tailor, apply, and outreach. "
                "Call tools sequentially to progress the pipeline states."
            )
        )
        
        async with Agent(config) as agent:
            log_message("AgentLoop", "Antigravity Agent active and registered.", "SUCCESS")
            while True:
                try:
                    prompt = (
                        "Analyze the SQLite job_tracker.db job entries. "
                        "Determine if any jobs need scraping, evaluation, resume tailoring, auto-applying, "
                        "or emailing, and invoke the appropriate tools. Update status after execution."
                    )
                    log_message("AgentLoop", "Polling agent prompt chat turn...")
                    response = await agent.chat(prompt)
                    log_message("AgentLoop", f"Agent turn completed: {await response.text()}")
                except Exception as ex:
                    log_message("AgentLoop", f"Agent session turn failed: {str(ex)}. Executing local helper pass.", "WARNING")
                    await run_local_pipeline_step()
                
                delay = random.randint(60, 180)
                log_message("AgentLoop", f"Rate limiting loop pause: sleeping {delay}s...")
                await asyncio.sleep(delay)
                
    except Exception as e:
        log_message("AgentLoop", f"Could not boot Antigravity Agent SDK: {str(e)}. Defaulting to Local Orchestration.", "WARNING")
        while True:
            try:
                await run_local_pipeline_step()
            except Exception as ex:
                log_message("AgentLoop", f"Error in local pipeline pass: {str(ex)}", "ERROR")
            
            # Idle delay before next check
            await asyncio.sleep(45)

def start_agent_thread():
    """
    Helper to launch async agent loop on a persistent background thread.
    """
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_until_complete, args=(agent_execution_loop(),), daemon=True)
    t.start()
    log_message("System", "Background agent thread spawned successfully.")

# --- FastAPI Web Server Interface ---

app = FastAPI(title="Job Hunt Automation Dashboard")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

@app.on_event("startup")
def startup_event():
    init_db()
    # Check login status in background tasks
    for platform in ["linkedin", "naukri", "indeed"]:
        asyncio.create_task(check_platform_login_status(platform))
    start_agent_thread()

@app.get("/", response_class=HTMLResponse)
async def get_dashboard(request: Request):
    return templates.TemplateResponse(request=request, name="index.html")

@app.get("/api/jobs")
async def get_jobs():
    conn = get_db_connection()
    cur = db_execute(conn, "SELECT * FROM jobs ORDER BY updated_at DESC")
    jobs = cur.fetchall()
    conn.close()
    return [dict(j) for j in jobs]

class StatusUpdate(BaseModel):
    status: str

@app.patch("/api/jobs/{job_id}/status")
async def patch_job_status(job_id: int, data: StatusUpdate):
    valid_statuses = ["Discovered", "Tailored", "Applied", "Emailed", "Requires Intervention", "Archived"]
    if data.status not in valid_statuses:
        raise HTTPException(status_code=400, detail="Invalid status column value")
    
    update_job_status(job_id, data.status)
    log_message("WebAPI", f"Job ID {job_id} status updated to '{data.status}' manually.")
    return {"status": "success", "message": f"Updated status of Job {job_id}"}

@app.get("/api/jobs/{job_id}/contacts")
async def get_job_contacts(job_id: int):
    conn = get_db_connection()
    row = db_execute(conn, "SELECT contacts, company, description FROM jobs WHERE id = %s", (job_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(status_code=404, detail="Job not found")
        
    contacts_str = row["contacts"]
    if not contacts_str:
        contacts = await get_or_generate_contacts(row["company"], row["description"])
        conn = get_db_connection()
        db_execute(conn, "UPDATE jobs SET contacts = %s WHERE id = %s", (json.dumps(contacts), job_id))
        conn.commit()
        conn.close()
        return contacts
        
    try:
        return json.loads(contacts_str)
    except Exception as e:
        return []

class ContactsUpdate(BaseModel):
    contacts: list

@app.post("/api/jobs/{job_id}/contacts")
async def update_job_contacts(job_id: int, data: ContactsUpdate):
    conn = get_db_connection()
    db_execute(conn, "UPDATE jobs SET contacts = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s", (json.dumps(data.contacts), job_id))
    conn.commit()
    conn.close()
    log_message("WebAPI", f"Contacts updated for Job ID {job_id} manually.")
    return {"status": "success", "message": "Contacts updated successfully"}

@app.post("/api/jobs/{job_id}/outreach")
async def trigger_outreach(job_id: int):
    conn = get_db_connection()
    job = db_execute(conn, "SELECT * FROM jobs WHERE id = %s", (job_id,)).fetchone()
    conn.close()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
        
    if not job["resume_path"]:
        raise HTTPException(status_code=400, detail="Tailored resume does not exist. Please generate/upload one first.")
        
    log_message("WebAPI", f"Manually triggering cold outreach for Job ID {job_id} ({job['company']})...")
    email_res = await send_cold_email(job["company"], job["description"], job["resume_path"], job_id=job_id)
    
    if "successfully" in email_res or "Sent" in email_res:
        update_job_status(job_id, "Emailed")
        return {"status": "success", "message": email_res}
    else:
        return {"status": "error", "message": email_res}
class CodeSubmit(BaseModel):
    code: str

@app.get("/api/auth/status")
async def get_auth_status():
    global platform_auth_status, active_2fa
    with auth_lock:
        return {
            "statuses": platform_auth_status,
            "active_2fa": {
                "platform": active_2fa["platform"],
                "screenshot_path": active_2fa["screenshot_path"],
                "status": active_2fa["status"]
            }
        }

@app.post("/api/auth/login/{platform}")
async def trigger_platform_login(platform: str):
    if platform not in ["linkedin", "naukri", "indeed"]:
        raise HTTPException(status_code=400, detail="Invalid platform name.")
    
    # Trigger login in a background task
    asyncio.create_task(execute_platform_login(platform))
    return {"status": "success", "message": f"Login process triggered for {platform.capitalize()}."}

@app.post("/api/auth/submit-code")
async def submit_2fa_code(data: CodeSubmit):
    global active_2fa
    with auth_lock:
        if active_2fa["status"] != "waiting":
            raise HTTPException(status_code=400, detail="No active 2FA verification challenge found.")
        active_2fa["code"] = data.code
        active_2fa["status"] = "submitted"
    return {"status": "success", "message": "Verification code submitted. Processing..."}

@app.post("/api/auth/cancel-2fa")
async def cancel_2fa():
    global active_2fa
    with auth_lock:
        active_2fa["status"] = "idle"
        active_2fa["platform"] = None
        active_2fa["screenshot_path"] = None
        active_2fa["code"] = None
    return {"status": "success", "message": "2FA verification cancelled."}

@app.get("/api/profile")
async def get_profile():
    return load_profile_data()

class ProfilePrompt(BaseModel):
    prompt: str

@app.post("/api/profile/update")
async def update_profile(data: ProfilePrompt):
    prompt_text = data.prompt.strip()
    if not prompt_text:
        raise HTTPException(status_code=400, detail="Prompt cannot be empty")

    log_message("Omnibar", f"Profile update requested: {prompt_text}")
    profile = load_profile_data()
    success = False
    message = ""

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key != "your_gemini_api_key_here":
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = f"""
            You are a system profile assistant. Parse the text and update the profile JSON database.
            
            Current JSON:
            {json.dumps(profile)}
            
            User Update command:
            {prompt_text}
            
            Incorporate the new details (new skill, project description, or contact information).
            Return ONLY the updated profile JSON. Do not write markdown tags.
            """
            response = client.models.generate_content(
                model="gemini-2.5-flash",
                contents=prompt
            )
            resp_text = response.text.strip()
            if resp_text.startswith("```"):
                resp_text = resp_text.split("```")[1]
                if resp_text.startswith("json"):
                    resp_text = resp_text[4:]
            
            updated_profile = json.loads(resp_text.strip())
            save_profile_data(updated_profile)
            success = True
            message = "Profile synced using Gemini parsing."
        except Exception as e:
            log_message("Omnibar", f"Gemini profile parse crash: {str(e)}. Using fallback regex.", "WARNING")

    if not success:
        # Heuristics parsing
        lower_prompt = prompt_text.lower()
        if "skill" in lower_prompt:
            # Parse skill
            parts = prompt_text.split(":")
            new_skill = parts[1].strip() if len(parts) > 1 else prompt_text.split("skill")[-1].strip()
            # Clean words
            for w in ["add", "to", "my", "list", "is", "skills"]:
                if new_skill.lower().startswith(w):
                    new_skill = new_skill[len(w):].strip()
            
            new_skills = [s.strip() for s in new_skill.split(",") if s.strip()]
            for s in new_skills:
                if s not in profile["skills"]:
                    profile["skills"].append(s)
            save_profile_data(profile)
            message = f"Skills added (regex fallback): {', '.join(new_skills)}"
        elif "project" in lower_prompt:
            # Parse project
            import re
            tech_match = re.search(r'\((.*?)\)', prompt_text)
            techs = []
            if tech_match:
                tech_str = tech_match.group(1)
                techs = [t.strip() for t in re.split(r'[,|-]', tech_str) if t.strip()]
                details = prompt_text.replace(tech_match.group(0), "").strip()
            else:
                details = prompt_text
                
            title_part = prompt_text.split("(")[0]
            for w in ["add project", "project", "add"]:
                if title_part.lower().startswith(w):
                    title_part = title_part[len(w):].strip()
            title = title_part if title_part else "New Project"
            
            new_proj = {
                "title": title,
                "technologies": techs,
                "details": details
            }
            profile["projects"].append(new_proj)
            save_profile_data(profile)
            message = f"Project added (regex fallback): {title}"
        else:
            profile["skills"].append(prompt_text)
            save_profile_data(profile)
            message = f"Added as general skill: {prompt_text}"

    log_message("Omnibar", f"Profile successfully updated: {message}", "SUCCESS")
    return {"status": "success", "message": message}

@app.get("/api/logs")
async def get_logs(since: float = 0):
    with logs_lock:
        filtered_logs = [log for log in LOGS if log["timestamp"] > since]
    return filtered_logs

@app.post("/api/resume/upload")
async def upload_resume(file: UploadFile = File(...)):
    if not file.filename.endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are allowed")
    
    file_path = "static/default_resume.pdf"
    with open(file_path, "wb") as f:
        f.write(await file.read())
        
    log_message("System", "Uploaded a fresh static resume to use as default.")
    return {"status": "success", "message": "Default resume uploaded successfully."}

@app.get("/api/resume/status")
async def get_resume_status():
    exists = os.path.exists("static/default_resume.pdf")
    return {
        "has_default": exists,
        "mode": "Static Resume (default_resume.pdf)" if exists else "Dynamic Tailored Resumes"
    }

@app.delete("/api/resume")
async def delete_resume():
    file_path = "static/default_resume.pdf"
    if os.path.exists(file_path):
        os.remove(file_path)
        log_message("System", "Deleted static resume. Reverted to Dynamic Tailored Resumes.")
        return {"status": "success", "message": "Reverted to dynamic tailoring."}
    return {"status": "success", "message": "No static resume to delete."}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
