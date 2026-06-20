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
            "DevOps Engineer", 
            "Microsoft", 
            "We are seeking a DevOps Engineer to join our Azure Cloud Platform team. You will automate delivery pipelines, manage infrastructure as code, and optimize GitLab CI/CD workflows on Azure VMs. Strong experience with Python, Linux, and PostgreSQL is highly preferred.",
            "https://careers.microsoft.com/jobs/devops-engineer-demo", 
            "Redmond, WA (Hybrid)", 
            "Discovered"
        ))
        
        # Requires Intervention Job with mock Captcha screenshot and mock contacts
        db_execute(conn, '''
        INSERT INTO jobs (title, company, description, url, location, status, match_score, match_reason, screenshot_path, contacts)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) ON CONFLICT (url) DO NOTHING
        ''', (
            "Cloud Infrastructure Engineer", 
            "GitLab Inc.", 
            "Join GitLab as a Cloud Infrastructure Specialist! In this role, you will scale our cloud delivery systems, improve CI/CD pipelines, and manage PostgreSQL databases. Experience running Linux (Debian/Pop!_OS) containers and writing automation scripts in Python or Go is required.",
            "https://about.gitlab.com/jobs/cloud-infra-demo", 
            "Remote", 
            "Requires Intervention",
            88,
            "Matches skills: GitLab CI/CD, Linux, Python, PostgreSQL. Highly relevant projects.",
            "/static/screenshots/captcha_job_sample.png",
            json.dumps([
                {"name": "Sid Sijbrandij", "role": "Co-Founder & CEO", "email": "sid@gitlab.com", "pitch_type": "executive", "status": "pending"},
                {"name": "HR Recruitment", "role": "HR Director", "email": "hr@gitlab.com", "pitch_type": "hr", "status": "pending"}
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
                        
                        if any(w in position for w in ["founder", "co-founder", "ceo", "cto", "cio", "coo", "president"]):
                            pitch_type = "executive"
                            is_match = True
                        elif any(w in position for w in ["hr", "recruiter", "talent", "recruitment", "people", "hiring"]):
                            pitch_type = "hr"
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
        {"name": "HR Director", "role": "HR Director", "email": f"hr@{domain}", "pitch_type": "hr", "status": "pending"},
        {"name": "Recruitment Team", "role": "Recruitment Team", "email": f"recruiting@{domain}", "pitch_type": "hr", "status": "pending"}
    ]

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
    Search job boards for matching job listings using public APIs.
    """
    log_message("Scraper", f"Scraping job boards for: {keywords}")
    keywords_list = [k.strip() for k in keywords.split(",") if k.strip()]
    discovered_jobs = []

    # Attempt fetching from public Arbeitnow API (no key required)
    try:
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
                    
                    # Match keywords against title or description
                    match = False
                    for kw in keywords_list:
                        if kw.lower() in title.lower() or kw.lower() in description.lower():
                            match = True
                            break
                    if match:
                        discovered_jobs.append({
                            "title": title,
                            "company": company,
                            "description": description[:1200],  # Keep description sized appropriately
                            "url": url,
                            "location": location
                        })
    except Exception as e:
        log_message("Scraper", f"Arbeitnow API fetch failed: {str(e)}. Using mock fallback data.", "WARNING")

    # Fallback to realistic mock data if nothing found
    if not discovered_jobs:
        log_message("Scraper", "Using pre-configured high-quality job mockups.", "INFO")
        mock_jobs = [
            {
                "title": "DevOps Engineer",
                "company": "Microsoft",
                "description": "We are seeking a DevOps Engineer to join our Azure Cloud Platform team. You will automate delivery pipelines, manage infrastructure as code, and optimize GitLab CI/CD workflows on Azure VMs. Strong experience with Python, Linux, and PostgreSQL is highly preferred.",
                "url": "https://careers.microsoft.com/jobs/devops-engineer-demo",
                "location": "Redmond, WA (Hybrid)"
            },
            {
                "title": "Cloud Infrastructure Engineer",
                "company": "GitLab Inc.",
                "description": "Join GitLab as a Cloud Infrastructure Specialist! In this role, you will scale our cloud delivery systems, improve CI/CD pipelines, and manage PostgreSQL databases. Experience running Linux (Debian/Pop!_OS) containers and writing automation scripts in Python or Go is required.",
                "url": "https://about.gitlab.com/jobs/cloud-infra-demo",
                "location": "Remote"
            },
            {
                "title": "Backend Python Developer",
                "company": "Spotify",
                "description": "We are looking for a Python Developer to build and optimize backend services. You will design PostgreSQL schemas, write machine learning pipelines for search relevance, and deploy to Azure Cloud. Familiarity with GitLab CI/CD, Pop!_OS/Linux development, and CNN networks is a plus.",
                "url": "https://spotify.com/careers/python-developer-demo",
                "location": "New York, NY (Remote)"
            },
            {
                "title": "Machine Learning Engineer",
                "company": "Google",
                "description": "We are seeking an ML Engineer. The candidate will work on real-time neural networks (CNNs) for translation and accessibility. Requirements include deep knowledge of Python, PostgreSQL, and deploying services on cloud VMs (Google Cloud / Azure).",
                "url": "https://careers.google.com/jobs/ml-engineer-demo",
                "location": "Mountain View, CA"
            },
            {
                "title": "System Administrator & DevOps Lead",
                "company": "Red Hat",
                "description": "Lead the DevOps transformation. System administration on Pop!_OS and Linux environments, GitLab CI/CD scripting, automating PostgreSQL backups on cloud VMs. Python scripting is a must.",
                "url": "https://redhat.com/jobs/devops-lead-demo",
                "location": "Boston, MA (Hybrid)"
            }
        ]
        
        for job in mock_jobs:
            for kw in keywords_list:
                if kw.lower() in job["title"].lower() or kw.lower() in job["description"].lower():
                    discovered_jobs.append(job)
                    break

    log_message("Scraper", f"Successfully scraped {len(discovered_jobs)} matching job listings.", "SUCCESS")
    return json.dumps(discovered_jobs)


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
    """
    from playwright.async_api import async_playwright
    log_message("AutoApply", f"Launching playwright sandbox for Job ID {job_id}...")
    profile = json.loads(profile_data)

    pdf_abs_path = os.path.abspath(pdf_path.lstrip("/"))
    screenshot_name = f"captcha_job_{job_id}.png"
    screenshot_path = os.path.join("static", "screenshots", screenshot_name)

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = await context.new_page()

            log_message("AutoApply", f"Opening application URL: {job_url}")
            await page.goto(job_url, timeout=30000, wait_until="load")
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
                await browser.close()
                return "Requires Intervention: CAPTCHA detected."

            # Mock automation filling fields (for robustness in sandbox environments)
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
                await browser.close()
                return "Applied successfully."
            else:
                log_message("AutoApply", "Application layout complex. Marking for user intervention.", "WARNING")
                update_job_status(job_id, "Requires Intervention")
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
            return f"Requires Intervention: automation failure."


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
                    role_instructions = f"The recipient is a Founder/Co-Founder/Executive named {target_name}. Focus on high-level infrastructure scaling, cost optimization, and back-end efficiency. Make it brief and business-oriented."
                else:
                    role_instructions = f"The recipient is an HR Recruiter named {target_name}. Focus on skills alignment (Python, Azure, CI/CD, ML), project accomplishments, and prompt availability."
                
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

I wanted to reach out regarding backend engineering at {company_name}. I am a Cloud Systems and Backend Developer specializing in Python architectures, Microsoft Azure VM automation, PostgreSQL databases, and GitLab CI/CD pipelines.

I focus on infrastructure-as-code automation and robust backend services. I develop in Pop!_OS Linux and love building high-performance systems.

My resume is attached. I would appreciate 10 minutes to discuss how my automation and cloud scaling expertise can benefit {company_name}.

Best regards,
Aman Parab
+91-9324101109 | amanparab007@gmail.com"""
            else:
                email_body = f"""Dear {target_name},

I recently reviewed your backend engineering initiatives and wanted to connect. I am a Backend and Cloud Systems Engineer with specialized expertise in Python backend architectures, Microsoft Azure VM automation, PostgreSQL databases, and GitLab CI/CD pipeline deployments.

My work includes automating GitLab CI/CD workflows for deploying microservices onto Azure VMs, engineering real-time ML translation networks (CNNs), and constructing analytical PostgreSQL databases. 

I've attached my tailored resume for your reference. I would welcome the opportunity to chat about how my automation and cloud engineering credentials align with {company_name}'s open roles.

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
        scraped_json = await scrape_jobs("DevOps, Python Developer, Cloud Engineer")
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
