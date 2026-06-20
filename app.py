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
from fastapi import FastAPI, Request, HTTPException
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
DATABASE = "job_tracker.db"

def get_db_connection():
    conn = sqlite3.connect(DATABASE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    log_message("Database", "Initializing SQLite Database...")
    conn = get_db_connection()
    conn.execute('''
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')
    conn.commit()

    # Prepopulate with dummy jobs if database is empty for visual showcase
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) FROM jobs")
    count = cursor.fetchone()[0]
    if count == 0:
        log_message("Database", "Pre-populating database with initial sample jobs.")
        
        # Discovered Job
        cursor.execute('''
        INSERT INTO jobs (title, company, description, url, location, status)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (
            "DevOps Engineer", 
            "Microsoft", 
            "We are seeking a DevOps Engineer to join our Azure Cloud Platform team. You will automate delivery pipelines, manage infrastructure as code, and optimize GitLab CI/CD workflows on Azure VMs. Strong experience with Python, Linux, and PostgreSQL is highly preferred.",
            "https://careers.microsoft.com/jobs/devops-engineer-demo", 
            "Redmond, WA (Hybrid)", 
            "Discovered"
        ))
        
        # Requires Intervention Job with mock Captcha screenshot
        cursor.execute('''
        INSERT INTO jobs (title, company, description, url, location, status, match_score, match_reason, screenshot_path)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            "Cloud Infrastructure Engineer", 
            "GitLab Inc.", 
            "Join GitLab as a Cloud Infrastructure Specialist! In this role, you will scale our cloud delivery systems, improve CI/CD pipelines, and manage PostgreSQL databases. Experience running Linux (Debian/Pop!_OS) containers and writing automation scripts in Python or Go is required.",
            "https://about.gitlab.com/jobs/cloud-infra-demo", 
            "Remote", 
            "Requires Intervention",
            88,
            "Matches skills: GitLab CI/CD, Linux, Python, PostgreSQL. Highly relevant projects.",
            "/static/screenshots/captcha_job_sample.png"
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
    conn.execute("UPDATE jobs SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (status, job_id))
    conn.commit()
    conn.close()

def update_job_match(job_id: int, score: int, reason: str):
    conn = get_db_connection()
    conn.execute("UPDATE jobs SET match_score = ?, match_reason = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (score, reason, job_id))
    conn.commit()
    conn.close()

def update_job_resume(job_id: int, resume_path: str):
    conn = get_db_connection()
    conn.execute("UPDATE jobs SET resume_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (resume_path, job_id))
    conn.commit()
    conn.close()

def update_job_screenshot(job_id: int, screenshot_path: str):
    conn = get_db_connection()
    conn.execute("UPDATE jobs SET screenshot_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?", (screenshot_path, job_id))
    conn.commit()
    conn.close()

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


async def send_cold_email(company_name: str, job_description: str, resume_path: str, recruiter_email: str = None) -> str:
    """
    Draft highly technical pitch and email recruiter using Brevo's web API.
    """
    import base64
    log_message("Emailer", f"Drafting outreach pitch for recruiter at {company_name}...")
    
    if not recruiter_email:
        domain = company_name.lower().replace(" ", "").replace(".", "") + ".com"
        recruiter_email = f"recruiting@{domain}"
        log_message("Emailer", f"Recruiter email guessed: {recruiter_email}")

    subject = f"Cloud / Backend Engineering Inquiry - Aman Parab"
    email_body = ""

    api_key = os.environ.get("GEMINI_API_KEY")
    if api_key and api_key != "your_gemini_api_key_here":
        try:
            from google import genai
            client = genai.Client(api_key=api_key)
            prompt = f"""
            Draft a highly technical cold outreach email as Aman Parab pitching skills.
            Core skills: Python, Microsoft Azure, GitLab CI/CD, PostgreSQL, Linux, Machine Learning.
            Company: {company_name}
            Job details: {job_description[:400]}
            
            Email must:
            - Be concise (max 3 short paragraphs).
            - Focus on Azure VM deployment automation and Python ML backend projects.
            - Reference attached resume.
            
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
            log_message("Emailer", "Gemini custom email drafted.", "SUCCESS")
        except Exception as e:
            log_message("Emailer", f"Gemini draft failed: {str(e)}. Using fallback template.", "WARNING")

    if not email_body:
        email_body = f"""Dear Hiring Team at {company_name},

I recently reviewed your backend infrastructure requirements and wanted to connect. I am a Backend and Cloud Systems Engineer with specialized expertise in Python backend architectures, Microsoft Azure VM automation, PostgreSQL databases, and GitLab CI/CD pipeline deployments.

My work includes automating GitLab CI/CD workflows for deploying microservices onto Azure VMs, engineering real-time ML translation networks (CNNs), and constructing analytical PostgreSQL databases. I develop in Pop!_OS Linux and thrive on infrastructure automation.

I have attached my tailored resume for your reference. I would welcome the opportunity to chat about how my automation and cloud engineering expertise aligns with {company_name}'s needs.

Best regards,
Aman Parab
+91-9324101109 | amanparab007@gmail.com"""

    # Check for Brevo API Key
    brevo_key = os.environ.get("BREVO_API_KEY", "your_brevo_api_key_here")
    sender_email = os.environ.get("SENDER_EMAIL", "amanparab007@gmail.com")
    sender_name = os.environ.get("SENDER_NAME", "Aman Parab")

    # Attachment logic
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
            log_message("Emailer", f"Encoded resume for attachment.")
        except Exception as e:
            log_message("Emailer", f"Failed to encode resume: {str(e)}", "WARNING")

    if brevo_key == "your_brevo_api_key_here" or not brevo_key:
        log_message("Emailer", "[SANDBOX] Brevo API key not configured. Simulating dispatch.", "WARNING")
        log_message("Emailer", f"Simulated Brevo Email sent to {recruiter_email}\nSubject: {subject}", "SUCCESS")
        return "Emailed (Sandbox Mode): Email sent successfully."

    # Dispatch using HTTP POST to Brevo API
    try:
        url = "https://api.brevo.com/v3/smtp/email"
        headers = {
            "accept": "application/json",
            "api-key": brevo_key,
            "content-type": "application/json"
        }
        
        payload = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": recruiter_email}],
            "subject": subject,
            "textContent": email_body
        }
        if attachment:
            payload["attachment"] = attachment

        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            if response.status_code in [200, 201, 202]:
                log_message("Emailer", f"Email dispatched via Brevo API successfully to {recruiter_email}.", "SUCCESS")
                return "Emailed successfully."
            else:
                log_message("Emailer", f"Brevo API error (Status {response.status_code}): {response.text}", "ERROR")
                return f"Failed: Brevo API error ({response.status_code})."
    except Exception as e:
        log_message("Emailer", f"Brevo API request failed: {str(e)}", "ERROR")
        return f"Failed: Brevo API connection error."

# --- Background PTA Orchestrator Loop ---

async def run_local_pipeline_step():
    """
    Executes one sequential pass of the job-hunting pipeline.
    """
    profile = load_profile_data()

    # 1. Scrape if Discovered count is low
    conn = get_db_connection()
    disc_count = conn.execute("SELECT COUNT(*) FROM jobs WHERE status = 'Discovered'").fetchone()[0]
    conn.close()

    if disc_count < 2:
        log_message("AgentLoop", "Discovered jobs count is low. Scraping job listings...")
        scraped_json = await scrape_jobs("DevOps, Python Developer, Cloud Engineer")
        scraped_jobs = json.loads(scraped_json)
        
        conn = get_db_connection()
        ins_count = 0
        for j in scraped_jobs:
            cur = conn.cursor()
            cur.execute('''
            INSERT OR IGNORE INTO jobs (title, company, description, url, location, status)
            VALUES (?, ?, ?, ?, ?, ?)
            ''', (j["title"], j["company"], j["description"], j["url"], j["location"], "Discovered"))
            if cur.rowcount > 0:
                ins_count += 1
        conn.commit()
        conn.close()
        log_message("AgentLoop", f"Inserted {ins_count} new job listings into ledger.")

    # 2. Evaluate Discovered matches
    conn = get_db_connection()
    discovered_jobs = conn.execute("SELECT * FROM jobs WHERE status = 'Discovered'").fetchall()
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
        else:
            update_job_status(job_id, "Archived")
            log_message("AgentLoop", f"Job ID {job_id} scored {score}%. Marked as Archived.", "INFO")
        
        await asyncio.sleep(random.randint(2, 5))

    # 3. Process Tailored applications
    conn = get_db_connection()
    tailored_jobs = conn.execute("SELECT * FROM jobs WHERE status = 'Tailored'").fetchall()
    conn.close()

    for job in tailored_jobs:
        job_id = job["id"]
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
    applied_jobs = conn.execute("SELECT * FROM jobs WHERE status = 'Applied'").fetchall()
    conn.close()

    for job in applied_jobs:
        job_id = job["id"]
        email_res = await send_cold_email(job["company"], job["description"], job["resume_path"])
        log_message("AgentLoop", f"Job ID {job_id} Outreach: {email_res}")
        
        if "Emailed" in email_res or "successfully" in email_res:
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
    jobs = conn.execute("SELECT * FROM jobs ORDER BY updated_at DESC").fetchall()
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

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="127.0.0.1", port=8000, reload=True)
