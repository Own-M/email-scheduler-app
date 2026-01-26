# Production-Ready Email Automation Hub with Gemini AI & Advanced Analytics
# --------------------------------------------------------------------
# Updated & Improved
# Required libraries:
# pip install flask sqlalchemy pandas openpyxl certifi requests flask-login werkzeug matplotlib

import os
import time
import heapq
import threading
import uuid
import imaplib
import email
import ssl
import smtplib
import json
import requests
import base64
import logging
import pandas as pd
from io import BytesIO
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, formataddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from types import SimpleNamespace
from flask import Flask, request, redirect, url_for, flash, render_template_string, jsonify
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, func, Boolean
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, joinedload
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

# --- Basic Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "email_scheduler.sqlite")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Logging Setup ---
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Database Setup ---
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# --- Database Models ---
class User(Base, UserMixin):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(150), unique=True, nullable=False)
    password_hash = Column(String(256), nullable=False)
    # Security: Default key removed. User must set it in Settings.
    gemini_api_key = Column(String(256), nullable=True, default="")
    accounts = relationship("Account", back_populates="user", cascade="all, delete-orphan")
    contacts = relationship("Contact", back_populates="user", cascade="all, delete-orphan")
    templates = relationship("Template", back_populates="user", cascade="all, delete-orphan")

class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    email = Column(String(320), nullable=False)
    password = Column(String(1024), nullable=False)
    user = relationship("User", back_populates="accounts")

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    receiver = Column(String(320), nullable=False)
    subject = Column(String(998), nullable=False)
    body = Column(Text, nullable=False)
    send_at = Column(DateTime, nullable=False)
    status = Column(String(32), default="pending") # pending, sending, sent, failed, replied
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    message_id = Column(String(256), nullable=True, unique=True)
    attachment_path = Column(String(512), nullable=True)
    is_opened = Column(Boolean, default=False)
    opened_at = Column(DateTime, nullable=True)
    account = relationship("Account", back_populates="tasks")

class Inbox(Base):
    __tablename__ = "inbox"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    from_addr = Column(String(320), nullable=True)
    subject = Column(String(998), nullable=True)
    date = Column(DateTime, nullable=True)
    body = Column(Text, nullable=True)
    message_id = Column(String(256), nullable=True, unique=True)
    in_reply_to = Column(String(256), nullable=True)
    task_id = Column(Integer, ForeignKey("tasks.id"), nullable=True)
    sentiment = Column(String(50), nullable=True) # Positive, Negative, Neutral
    account = relationship("Account")
    task = relationship("Task")

class Template(Base):
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    body = Column(Text, nullable=False)
    user = relationship("User", back_populates="templates")

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False)
    name = Column(String(200), nullable=False)
    email = Column(String(320), nullable=False)
    user = relationship("User", back_populates="contacts")

Account.tasks = relationship("Task", order_by=Task.id, back_populates="account", cascade="all, delete-orphan")
Base.metadata.create_all(engine)

# --- Flask App and Login Manager Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "change-this-in-production-please")
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024 # 16MB max file size
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

@login_manager.user_loader
def load_user(user_id):
    with SessionLocal() as session:
        user = session.query(User).options(joinedload(User.accounts)).get(int(user_id))
        return user

# --- Background Worker Setup ---
TASK_HEAP = []
HEAP_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WORKER_STARTED = threading.Event()
IMAP_STARTED = threading.Event()

# --- Helper Functions ---
def _create_unverified_ssl_context():
    """Creates a relaxed SSL context for broad compatibility."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context

def _push_task_heap(send_at, task_id):
    with HEAP_LOCK:
        heapq.heappush(TASK_HEAP, (send_at.timestamp(), task_id))

def _send_via_smtp(account: Account, to_email: str, subject: str, body: str, attachment_path=None, in_reply_to=None):
    msg = MIMEMultipart()
    msg["From"] = formataddr((account.name, account.email))
    msg["To"] = to_email
    msg["Subject"] = subject
    # Unique Message-ID with domain
    domain = account.email.split('@')[-1]
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{domain}>"
    
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    
    msg.attach(MIMEText(body, "html", "utf-8"))

    if attachment_path and os.path.exists(attachment_path):
        try:
            with open(attachment_path, "rb") as attachment:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(attachment.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(attachment_path)}")
            msg.attach(part)
        except Exception as e:
            logger.error(f"Failed to attach file {attachment_path}: {e}")
    
    ssl_context = _create_unverified_ssl_context()
    # Try generic Gmail SMTP first
    try:
        server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
        server.starttls(context=ssl_context)
        server.login(account.email, account.password)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        logger.error(f"SMTP Error: {e}")
        raise e
        
    return msg["Message-ID"]

# --- Background Threads (Worker, IMAP Poller) ---
def _worker_loop():
    logger.info("Worker thread started.")
    while not STOP_EVENT.is_set():
        task_id_to_process = None
        with HEAP_LOCK:
            if TASK_HEAP and TASK_HEAP[0][0] <= time.time():
                _, task_id_to_process = heapq.heappop(TASK_HEAP)
        
        if task_id_to_process is None:
            time.sleep(1)
            continue

        with SessionLocal() as session:
            try:
                task = session.query(Task).options(joinedload(Task.account)).get(task_id_to_process)
                if not task:
                    continue
                
                # Double check status in DB to prevent duplicate sends if re-queued manually
                if task.status not in ("pending", "failed"):
                     continue

                logger.info(f"Processing task {task.id} for {task.receiver}")
                task.status = "sending"
                session.commit()
                
                msgid = _send_via_smtp(task.account, task.receiver, task.subject, task.body, task.attachment_path)
                task.message_id = msgid
                task.status = "sent"
                task.last_error = None
            except Exception as e:
                logger.error(f"Failed processing task {task_id_to_process}: {e}")
                if task:
                    task.status = "failed"
                    task.last_error = str(e)
            finally:
                if task:
                    task.attempts += 1
                    session.commit()

def _imap_poller_loop():
    logger.info("IMAP Poller thread started.")
    while not STOP_EVENT.is_set():
        with SessionLocal() as session:
            try:
                all_users = session.query(User).all()
                for user in all_users:
                    # Skip if no API key is set, but still fetch emails. 
                    # Just skip sentiment analysis if no key.
                    
                    accounts = session.query(Account).filter_by(user_id=user.id).all()
                    for account in accounts:
                        try:
                            ssl_context = _create_unverified_ssl_context()
                            imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl_context)
                            imap.login(account.email, account.password)
                            imap.select("INBOX")
                            
                            # Look back 3 days to reduce load
                            date_since = (datetime.now() - timedelta(days=3)).strftime("%d-%b-%Y")
                            search_criteria = f'(SENTSINCE "{date_since}")'
                            
                            result, data = imap.search(None, search_criteria)
                            if result != "OK": 
                                imap.logout()
                                continue

                            for uid in data[0].split():
                                if not uid: continue
                                res, msg_data = imap.fetch(uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
                                if res != "OK": continue
                                
                                header_data = msg_data[0][1].decode('utf-8')
                                current_message_id = email.message_from_string(header_data).get('Message-ID')
                                
                                # Skip if already processed
                                if session.query(Inbox).filter_by(message_id=current_message_id).first():
                                    continue

                                res, msg_data = imap.fetch(uid, "(RFC822)")
                                if res != "OK": continue
                                
                                msg = email.message_from_bytes(msg_data[0][1])
                                in_reply_to = msg.get("In-Reply-To")
                                
                                # We only care about replies to our tasks
                                if not in_reply_to: continue

                                task_match = session.query(Task).filter(Task.message_id == in_reply_to).first()
                                
                                if task_match:
                                    # Update task status
                                    task_match.status = "replied"
                                    
                                    from_addr = str(make_header(decode_header(msg.get("From", ""))))
                                    subject = str(make_header(decode_header(msg.get("Subject", ""))))
                                    date_tuple = email.utils.parsedate_tz(msg.get("Date"))
                                    if date_tuple:
                                        local_date = email.utils.mktime_tz(date_tuple)
                                        date_obj = datetime.fromtimestamp(local_date)
                                    else:
                                        date_obj = datetime.now()

                                    body_text = ""
                                    if msg.is_multipart():
                                        for part in msg.walk():
                                            if part.get_content_type() == "text/plain":
                                                body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                                                break
                                    else:
                                        body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")

                                    # AI Sentiment Analysis
                                    sentiment = "Neutral"
                                    if user.gemini_api_key:
                                        try:
                                            # Using newer Gemini 1.5 Flash
                                            api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={user.gemini_api_key}"
                                            prompt = f"Analyze the sentiment of this email reply. Classify as 'Positive', 'Negative', or 'Neutral'. Return ONLY the word.\n\nEmail: '{body_text[:1000]}'"
                                            payload = {"contents": [{"parts": [{"text": prompt}]}]}
                                            response = requests.post(api_url, json=payload, timeout=10)
                                            if response.ok:
                                                result = response.json()
                                                cand_text = result['candidates'][0]['content']['parts'][0]['text'].strip()
                                                # Basic cleanup
                                                if "positive" in cand_text.lower(): sentiment = "Positive"
                                                elif "negative" in cand_text.lower(): sentiment = "Negative"
                                        except Exception as ai_e:
                                            logger.warning(f"AI Sentiment Analysis Failed: {ai_e}")

                                    inbox_entry = Inbox(
                                        account_id=account.id, 
                                        from_addr=from_addr, 
                                        subject=subject, 
                                        date=date_obj, 
                                        body=body_text, 
                                        message_id=current_message_id, 
                                        in_reply_to=in_reply_to, 
                                        task_id=task_match.id, 
                                        sentiment=sentiment
                                    )
                                    session.add(inbox_entry)
                                    session.commit()
                                    logger.info(f"Processed reply from {from_addr}")

                            imap.logout()
                        except Exception as e:
                            logger.error(f"IMAP Error for {account.email}: {e}")
            except Exception as e:
                logger.error(f"Critical error in IMAP loop: {e}")
        
        # Poll every 2 minutes
        STOP_EVENT.wait(120)

# --- HTML Templates ---
def render_page(content_template, **kwargs):
    base_html = """
    <!doctype html>
    <html lang="en" data-bs-theme="light">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Email Automation Hub</title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
        <style>
            body { background-color: #f8f9fa; font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; }
            .navbar { background-color: #ffffff; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.1); }
            .card { border: none; border-radius: 0.75rem; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 1.5rem; }
            .card-header { background-color: transparent; border-bottom: 1px solid #f0f0f0; padding: 1rem 1.25rem; }
            .nav-link { color: #495057; font-weight: 500; }
            .nav-link:hover, .nav-link.active { color: #0d6efd !important; }
            .landing-hero { padding: 6rem 0; background: linear-gradient(135deg, #0d6efd 0%, #6610f2 100%); color: white; border-radius: 0 0 2rem 2rem; margin-bottom: 3rem; }
            .status-badge { min-width: 80px; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-expand-lg sticky-top">
            <div class="container">
                <a class="navbar-brand fw-bold text-primary" href="{{ url_for('landing_page') }}"><i class="bi bi-send-fill"></i> AutoMail</a>
                <button class="navbar-toggler" type="button" data-bs-toggle="collapse" data-bs-target="#navbarContent">
                    <span class="navbar-toggler-icon"></span>
                </button>
                <div class="collapse navbar-collapse" id="navbarContent">
                    <ul class="navbar-nav me-auto mb-2 mb-lg-0">
                        {% if current_user.is_authenticated %}
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">Dashboard</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'accounts' %}active{% endif %}" href="{{ url_for('accounts') }}">Accounts</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'inbox' %}active{% endif %}" href="{{ url_for('inbox') }}">Inbox <span class="badge bg-danger rounded-pill ms-1" style="font-size: 0.6em;">AI</span></a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'contacts' %}active{% endif %}" href="{{ url_for('contacts') }}">Contacts</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'templates' %}active{% endif %}" href="{{ url_for('templates') }}">Templates</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'bulk_upload' %}active{% endif %}" href="{{ url_for('bulk_upload') }}">Bulk Upload</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'analytics' %}active{% endif %}" href="{{ url_for('analytics') }}">Analytics</a></li>
                        {% endif %}
                    </ul>
                    {% if current_user.is_authenticated %}
                    <div class="dropdown">
                      <button class="btn btn-light dropdown-toggle border" type="button" data-bs-toggle="dropdown">
                        <i class="bi bi-person-circle text-secondary"></i> {{ current_user.username }}
                      </button>
                      <ul class="dropdown-menu dropdown-menu-end shadow">
                        <li><a class="dropdown-item" href="{{ url_for('settings') }}"><i class="bi bi-gear me-2"></i> Settings</a></li>
                        <li><hr class="dropdown-divider"></li>
                        <li><a class="dropdown-item text-danger" href="{{ url_for('logout') }}"><i class="bi bi-box-arrow-right me-2"></i> Logout</a></li>
                      </ul>
                    </div>
                    {% else %}
                    <div class="d-flex gap-2">
                        <a href="{{ url_for('login') }}" class="btn btn-outline-light">Login</a>
                        <a href="{{ url_for('register') }}" class="btn btn-light text-primary fw-bold">Register</a>
                    </div>
                    {% endif %}
                </div>
            </div>
        </nav>
        <div class="container mt-4">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }} alert-dismissible fade show shadow-sm" role="alert">
                            {{ message }}
                            <button type="button" class="btn-close" data-bs-dismiss="alert" aria-label="Close"></button>
                        </div>
                    {% endfor %}
                {% endif %}
            {% endwith %}
            {{ content|safe }}
        </div>
        <script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
    </body>
    </html>
    """
    content = render_template_string(content_template, **kwargs)
    return render_template_string(base_html, content=content)

# --- All Page Templates ---
LANDING_PAGE = """
<div class="landing-hero text-center">
    <div class="container">
        <h1 class="display-3 fw-bold mb-3">Intelligent Email Automation</h1>
        <p class="lead col-lg-8 mx-auto mb-4">Schedule personalized emails, track engagement, and analyze sentiment with the power of Gemini 1.5 AI. The ultimate tool for modern outreach.</p>
        <a href="{{ url_for('register') }}" class="btn btn-light btn-lg px-5 py-3 fw-bold text-primary shadow">Get Started Now</a>
    </div>
</div>
<div class="row text-center gy-4">
    <div class="col-lg-4">
        <div class="p-4 h-100 bg-white rounded shadow-sm">
            <div class="display-5 text-primary mb-3"><i class="bi bi-stars"></i></div>
            <h3>Gemini 1.5 AI</h3>
            <p class="text-muted">Compose emails effortlessly and analyze incoming replies for sentiment (Positive/Negative) automatically.</p>
        </div>
    </div>
    <div class="col-lg-4">
        <div class="p-4 h-100 bg-white rounded shadow-sm">
            <div class="display-5 text-success mb-3"><i class="bi bi-graph-up-arrow"></i></div>
            <h3>Real-time Analytics</h3>
            <p class="text-muted">Visualise your campaign performance with intuitive charts. Track sent, failed, and replied statuses instantly.</p>
        </div>
    </div>
    <div class="col-lg-4">
        <div class="p-4 h-100 bg-white rounded shadow-sm">
            <div class="display-5 text-warning mb-3"><i class="bi bi-hdd-network"></i></div>
            <h3>Bulk Operations</h3>
            <p class="text-muted">Upload CSV/Excel files to schedule hundreds of personalized emails in seconds. Manage contacts and templates seamlessly.</p>
        </div>
    </div>
</div>
"""

AUTH_PAGE_TEMPLATE = """
<div class="row justify-content-center mt-5">
    <div class="col-md-5 col-lg-4">
        <div class="card shadow">
            <div class="card-body p-4">
                <h3 class="card-title text-center mb-4 fw-bold">{{ title }}</h3>
                <form method="post">
                    <div class="mb-3">
                        <label for="username" class="form-label text-muted">Username</label>
                        <div class="input-group">
                            <span class="input-group-text bg-light"><i class="bi bi-person"></i></span>
                            <input type="text" name="username" class="form-control" required autofocus>
                        </div>
                    </div>
                    <div class="mb-4">
                        <label for="password" class="form-label text-muted">Password</label>
                        <div class="input-group">
                            <span class="input-group-text bg-light"><i class="bi bi-lock"></i></span>
                            <input type="password" name="password" class="form-control" required>
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary w-100 py-2">{{ button_text }}</button>
                </form>
                <div class="text-center mt-4">
                    <small class="text-muted">{{ footer_text|safe }}</small>
                </div>
            </div>
        </div>
    </div>
</div>
"""

DASHBOARD_PAGE = """
<div class="d-flex justify-content-between align-items-center mb-3">
    <h4 class="mb-0"><i class="bi bi-speedometer2"></i> Dashboard</h4>
    <div>
        <a href="{{ url_for('compose') }}" class="btn btn-primary"><i class="bi bi-plus-lg"></i> Compose New</a>
    </div>
</div>

<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h6 class="mb-0 text-muted">Recent Tasks</h6>
        <div class="btn-group btn-group-sm">
             <a href="?filter=all" class="btn btn-outline-secondary {% if not filter_status or filter_status == 'all' %}active{% endif %}">All</a>
             <a href="?filter=pending" class="btn btn-outline-secondary {% if filter_status == 'pending' %}active{% endif %}">Pending</a>
             <a href="?filter=sent" class="btn btn-outline-secondary {% if filter_status == 'sent' %}active{% endif %}">Sent</a>
             <a href="?filter=failed" class="btn btn-outline-secondary {% if filter_status == 'failed' %}active{% endif %}">Failed</a>
        </div>
    </div>
    <div class="card-body p-0">
        <div class="table-responsive">
            <table class="table table-hover align-middle mb-0">
                <thead class="bg-light text-secondary">
                    <tr>
                        <th class="ps-3">Recipient</th>
                        <th>Subject</th>
                        <th>Schedule</th>
                        <th>Status</th>
                        <th class="text-end pe-3">Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for t in tasks %}
                    <tr>
                        <td class="ps-3">
                            <div class="fw-bold">{{ t.receiver }}</div>
                            <small class="text-muted">via {{ t.account.email }}</small>
                        </td>
                        <td style="max-width: 250px;" class="text-truncate" title="{{ t.subject }}">
                            {{ t.subject }}
                            {% if t.attachment_path %}<i class="bi bi-paperclip text-muted ms-1"></i>{% endif %}
                        </td>
                        <td>
                            <div class="small">{{ t.send_at.strftime('%Y-%m-%d') }}</div>
                            <div class="small text-muted">{{ t.send_at.strftime('%H:%M') }}</div>
                        </td>
                        <td>
                            {% if t.status == 'sent' %}<span class="badge text-bg-success status-badge">Sent</span>
                            {% elif t.status == 'sending' %}<span class="badge text-bg-warning status-badge">Sending...</span>
                            {% elif t.status == 'failed' %}<span class="badge text-bg-danger status-badge" title="{{ t.last_error }}">Failed</span>
                            {% elif t.status == 'replied' %}<span class="badge text-bg-info status-badge">Replied</span>
                            {% else %}<span class="badge text-bg-secondary status-badge">Pending</span>
                            {% endif %}
                        </td>
                        <td class="text-end pe-3">
                            <div class="btn-group">
                                {% if t.status == 'failed' %}
                                <form method="POST" action="{{ url_for('retry_task', task_id=t.id) }}" class="d-inline">
                                    <button type="submit" class="btn btn-sm btn-outline-warning" title="Retry"><i class="bi bi-arrow-clockwise"></i></button>
                                </form>
                                {% endif %}
                                <form method="POST" action="{{ url_for('delete_task', task_id=t.id) }}" onsubmit="return confirm('Delete this task?');" class="d-inline">
                                    <button type="submit" class="btn btn-sm btn-outline-danger ms-1" title="Delete"><i class="bi bi-trash"></i></button>
                                </form>
                            </div>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="5" class="text-center py-5 text-muted">No tasks found for this filter.</td></tr>
                    {% endfor %}
                </tbody>
            </table>
        </div>
    </div>
</div>
"""

ACCOUNTS_PAGE = """
<div class="row">
    <div class="col-lg-4 mb-4">
        <div class="card h-100">
            <div class="card-header"><h5><i class="bi bi-person-plus"></i> Add Account</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3">
                        <label class="form-label">Account Name</label>
                        <input name="name" class="form-control" placeholder="e.g. Work Email" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Gmail Address</label>
                        <input name="email" type="email" class="form-control" placeholder="user@gmail.com" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">App Password</label>
                        <input name="password" type="password" class="form-control" placeholder="16-char App Password" required>
                        <div class="form-text small">Enable 2FA on Google and generate an 'App Password'.</div>
                    </div>
                    <button class="btn btn-primary w-100" type="submit">Connect Account</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-8">
        <h5 class="mb-3"><i class="bi bi-shield-lock"></i> Connected Accounts</h5>
        {% for acc in accounts %}
        <div class="card mb-3">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-start">
                    <div>
                        <h5 class="card-title mb-1">{{ acc.name }}</h5>
                        <div class="text-muted"><i class="bi bi-envelope"></i> {{ acc.email }}</div>
                    </div>
                    <form method="POST" action="{{ url_for('delete_account', account_id=acc.id) }}" onsubmit="return confirm('Delete account and ALL related history?');">
                        <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i> Remove</button>
                    </form>
                </div>
                <hr class="my-3">
                <div class="row text-center g-2">
                    <div class="col-4 border-end">
                        <div class="h5 mb-0">{{ acc.stats.total }}</div>
                        <small class="text-muted">Total Scheduled</small>
                    </div>
                    <div class="col-4 border-end">
                        <div class="h5 mb-0 text-success">{{ acc.stats.sent }}</div>
                        <small class="text-muted">Successfully Sent</small>
                    </div>
                    <div class="col-4">
                        <div class="h5 mb-0 text-info">{{ acc.stats.replied }}</div>
                        <small class="text-muted">Replies Received</small>
                    </div>
                </div>
            </div>
        </div>
        {% else %}
        <div class="alert alert-info">No accounts connected yet. Add one to start sending!</div>
        {% endfor %}
    </div>
</div>
"""

COMPOSE_PAGE = """
<div class="row justify-content-center">
<div class="col-lg-10">
<div class="card">
    <div class="card-header bg-white py-3">
        <h5 class="mb-0"><i class="bi bi-envelope-plus"></i> {{ 'Broadcast Campaign' if broadcast else 'Compose Email' }}</h5>
    </div>
    <div class="card-body">
        <form method="post" enctype="multipart/form-data">
            <div class="row g-3">
                <div class="col-md-6">
                    <label class="form-label fw-bold">From Account</label>
                    <select name="account_id" class="form-select" required>
                        {% for a in accounts %}<option value="{{ a.id }}">{{ a.name }} &lt;{{ a.email }}&gt;</option>{% endfor %}
                    </select>
                </div>
                <div class="col-md-6">
                    <label class="form-label fw-bold">Recipient</label>
                    {% if broadcast %}
                    <div class="input-group">
                        <span class="input-group-text"><i class="bi bi-people"></i></span>
                        <input type="text" class="form-control" value="All {{ contact_count }} Saved Contacts" readonly style="background-color: #e9ecef;">
                    </div>
                    <div class="form-text text-primary">This will schedule individual emails to everyone in your contacts list.</div>
                    {% else %}
                    <input name="receiver" type="email" class="form-control" list="contact-list" placeholder="Enter email address" required>
                    <datalist id="contact-list">
                        {% for c in contacts %}<option value="{{ c.email }}">{{ c.name }}</option>{% endfor %}
                    </datalist>
                    {% endif %}
                </div>
                
                <div class="col-12">
                    <label class="form-label fw-bold">Load Template (Optional)</label>
                    <select id="template-select" class="form-select form-select-sm border-0 bg-light">
                        <option value="">-- Select a template to populate body --</option>
                        {% for t in templates %}<option value="{{ t.id }}">{{ t.name }}</option>{% endfor %}
                    </select>
                </div>

                <div class="col-12">
                    <label class="form-label fw-bold">Subject Line</label>
                    <div class="input-group">
                        <input name="subject" id="subject-input" type="text" class="form-control" placeholder="e.g. Follow up regarding..." required>
                        <button class="btn btn-outline-primary" type="button" id="suggest-subjects-btn" title="AI Suggestions">
                            <i class="bi bi-magic"></i> Suggest
                        </button>
                    </div>
                    <div id="subject-suggestions" class="mt-2 d-flex flex-wrap gap-2"></div>
                </div>

                <div class="col-12">
                    <div class="d-flex justify-content-between mb-1">
                         <label class="form-label fw-bold">Message Body</label>
                         <button type="button" class="btn btn-sm btn-link text-decoration-none" data-bs-toggle="modal" data-bs-target="#ai-compose-modal">
                            <i class="bi bi-stars"></i> Write with AI
                        </button>
                    </div>
                    <textarea id="body-textarea" name="body" rows="12" class="form-control font-monospace" required></textarea>
                    <div class="form-text">HTML formatting is supported.</div>
                </div>

                <div class="col-md-6">
                    <label class="form-label fw-bold">Attachment</label>
                    <input name="attachment" type="file" class="form-control">
                </div>
                <div class="col-md-6">
                    <label class="form-label fw-bold">Schedule For</label>
                    <input name="send_at" type="datetime-local" class="form-control" required value="{{ default_time }}">
                </div>
            </div>
            
            <hr class="my-4">
            
            <div class="d-flex justify-content-end gap-2">
                <a href="{{ url_for('dashboard') }}" class="btn btn-light">Cancel</a>
                <button class="btn btn-primary px-4" type="submit"><i class="bi bi-send"></i> Schedule {{ 'Broadcast' if broadcast else 'Email' }}</button>
            </div>
        </form>
    </div>
</div>
</div>
</div>

<!-- AI Compose Modal -->
<div class="modal fade" id="ai-compose-modal" tabindex="-1">
  <div class="modal-dialog modal-dialog-centered">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title"><i class="bi bi-robot text-primary"></i> AI Email Assistant</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <label class="form-label">What should this email be about?</label>
        <textarea id="ai-prompt" class="form-control" rows="4" placeholder="e.g. Write a polite follow-up to a client who hasn't replied to my quote sent last week..."></textarea>
        <div id="ai-spinner" class="d-none text-center mt-3">
            <div class="spinner-border text-primary" role="status"></div>
            <div class="small mt-1 text-muted">Gemini is writing...</div>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
        <button type="button" class="btn btn-primary" id="generate-email-btn">Generate Draft</button>
      </div>
    </div>
  </div>
</div>

<script>
    const templates = {{ templates|tojson }};
    document.getElementById('template-select').addEventListener('change', function() {
        const templateId = this.value;
        const bodyTextarea = document.getElementById('body-textarea');
        if (templateId) {
            const selectedTemplate = templates.find(t => t.id == templateId);
            if(selectedTemplate) bodyTextarea.value = selectedTemplate.body;
        }
    });

    document.getElementById('generate-email-btn').addEventListener('click', async function() {
        const prompt = document.getElementById('ai-prompt').value;
        if(!prompt.trim()) return;
        
        const spinner = document.getElementById('ai-spinner');
        spinner.classList.remove('d-none'); 
        this.disabled = true;

        try {
            const response = await fetch('/generate-email-body', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ prompt: prompt })
            });
            const data = await response.json();
            if (data.text) {
                document.getElementById('body-textarea').value = data.text;
                document.querySelector('#ai-compose-modal .btn-close').click();
            } else { alert('Error: ' + (data.error || 'Unknown error')); }
        } catch (error) { alert('An error occurred: ' + error); } finally {
            spinner.classList.add('d-none'); this.disabled = false;
        }
    });

    document.getElementById('suggest-subjects-btn').addEventListener('click', async function() {
        const body = document.getElementById('body-textarea').value;
        if (!body.trim()) { alert('Please write the email body first so AI can understand the context.'); return; }
        
        const originalText = this.innerHTML;
        this.disabled = true; 
        this.innerHTML = '<span class="spinner-border spinner-border-sm"></span> Thinking...';
        
        try {
            const response = await fetch('/generate-subject', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ email_body: body })
            });
            const data = await response.json();
            const suggestionsDiv = document.getElementById('subject-suggestions');
            suggestionsDiv.innerHTML = '';
            
            if (data.subjects && data.subjects.length > 0) {
                data.subjects.forEach(subject => {
                    const btn = document.createElement('button');
                    btn.className = 'btn btn-outline-secondary btn-sm rounded-pill';
                    btn.textContent = subject;
                    btn.type = 'button';
                    btn.onclick = () => {
                        document.getElementById('subject-input').value = subject;
                    };
                    suggestionsDiv.appendChild(btn);
                });
            } else { 
                alert('Could not generate suggestions. Please ensure your API key is correct.'); 
            }
        } catch (error) { alert('An error occurred: ' + error); } finally {
            this.disabled = false; this.innerHTML = originalText;
        }
    });
</script>
"""

INBOX_PAGE = """
<div class="card">
    <div class="card-header d-flex justify-content-between align-items-center">
        <h5 class="mb-0"><i class="bi bi-inbox-fill"></i> Incoming Replies</h5>
        <span class="badge bg-light text-dark border">{{ messages|length }} Messages</span>
    </div>
    <div class="list-group list-group-flush">
        {% for msg in messages %}
        <div class="list-group-item p-3">
            <div class="d-flex w-100 justify-content-between mb-2">
                <h6 class="mb-0 text-primary">{{ msg.from_addr }}</h6>
                <small class="text-muted">{{ msg.date.strftime('%Y-%m-%d %H:%M') }}</small>
            </div>
            <div class="d-flex align-items-center mb-2">
                <span class="fw-bold me-2">{{ msg.subject }}</span>
                {% if msg.sentiment == 'Positive' %}<span class="badge bg-success-subtle text-success border border-success">Positive</span>
                {% elif msg.sentiment == 'Negative' %}<span class="badge bg-danger-subtle text-danger border border-danger">Negative</span>
                {% else %}<span class="badge bg-secondary-subtle text-secondary border">Neutral</span>
                {% endif %}
            </div>
            <div class="bg-light p-3 rounded text-muted small mb-2 border">
                {{ msg.body }}
            </div>
            <div class="d-flex justify-content-between align-items-center">
                <small class="text-muted">Received via: {{ msg.account.email }}</small>
                <a href="{{ url_for('compose', reply_to_inbox_id=msg.id) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-reply-fill"></i> Reply</a>
            </div>
        </div>
        {% else %}
        <div class="text-center py-5">
            <div class="display-1 text-muted opacity-25"><i class="bi bi-inbox"></i></div>
            <p class="text-muted mt-2">No replies found yet. The system checks periodically.</p>
        </div>
        {% endfor %}
    </div>
</div>
"""

TEMPLATES_PAGE = """
<div class="row">
    <div class="col-lg-5 mb-4">
        <div class="card h-100">
            <div class="card-header"><h5><i class="bi bi-file-earmark-plus"></i> Create Template</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3">
                        <label class="form-label">Template Name</label>
                        <input name="name" class="form-control" placeholder="e.g. Sales Outreach #1" required>
                    </div>
                    <div class="mb-3">
                        <label class="form-label">Email Body</label>
                        <textarea name="body" rows="10" class="form-control font-monospace" placeholder="HTML or Plain Text..." required></textarea>
                    </div>
                    <button class="btn btn-primary w-100" type="submit">Save Template</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-7">
        <div class="card">
            <div class="card-header"><h5><i class="bi bi-collection"></i> Library</h5></div>
            <div class="list-group list-group-flush">
                {% for t in templates %}
                <div class="list-group-item d-flex justify-content-between align-items-center">
                    <div>
                        <div class="fw-bold">{{ t.name }}</div>
                        <small class="text-muted">ID: {{ t.id }}</small>
                    </div>
                    <form method="POST" action="{{ url_for('delete_template', template_id=t.id) }}" onsubmit="return confirm('Delete this template?');">
                        <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button>
                    </form>
                </div>
                {% else %}
                <div class="p-4 text-center text-muted">No templates saved.</div>
                {% endfor %}
            </div>
        </div>
    </div>
</div>
"""

CONTACTS_PAGE = """
<div class="row">
    <div class="col-lg-4 mb-4">
        <div class="card">
            <div class="card-header"><h5><i class="bi bi-person-plus"></i> Add Contact</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3"><input name="name" class="form-control" placeholder="Full Name" required></div>
                    <div class="mb-3"><input name="email" type="email" class="form-control" placeholder="Email Address" required></div>
                    <button class="btn btn-primary w-100" type="submit">Save Contact</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-8">
        <div class="card">
            <div class="card-header d-flex justify-content-between align-items-center">
                <h5 class="mb-0"><i class="bi bi-people"></i> Directory</h5>
                <a href="{{ url_for('compose', broadcast=True) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-megaphone"></i> Email All Contacts</a>
            </div>
            <ul class="list-group list-group-flush">
                {% for c in contacts %}
                <li class="list-group-item d-flex justify-content-between align-items-center">
                    <div>
                        <div class="fw-bold">{{ c.name }}</div>
                        <div class="text-muted small">{{ c.email }}</div>
                    </div>
                    <form method="POST" action="{{ url_for('delete_contact', contact_id=c.id) }}" onsubmit="return confirm('Delete this contact?');">
                        <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button>
                    </form>
                </li>
                {% else %}
                <li class="list-group-item text-center text-muted p-4">No contacts saved yet.</li>
                {% endfor %}
            </ul>
        </div>
    </div>
</div>
"""

BULK_UPLOAD_PAGE = """
<div class="row justify-content-center">
    <div class="col-md-8">
        <div class="card shadow-sm">
            <div class="card-header bg-light"><h5><i class="bi bi-file-earmark-spreadsheet"></i> Bulk Scheduler</h5></div>
            <div class="card-body">
                <p class="text-muted">Upload a <code>.csv</code> or <code>.xlsx</code> file to schedule multiple emails at once.</p>
                <div class="alert alert-secondary">
                    <strong>Expected Columns:</strong><br>
                    <code>Receiver</code>, <code>Subject</code>, <code>Body</code>, <code>Schedule</code> (Format: YYYY-MM-DD HH:MM:SS)
                </div>
                
                <form method="post" enctype="multipart/form-data">
                    <div class="mb-4">
                        <label class="form-label fw-bold">Sending Account</label>
                        <select name="account_id" class="form-select" required>
                            {% for a in accounts %}<option value="{{ a.id }}">{{ a.name }} ({{ a.email }})</option>{% endfor %}
                        </select>
                    </div>
                    <div class="mb-4">
                        <label class="form-label fw-bold">Select File</label>
                        <input name="file" type="file" class="form-control" required accept=".csv,.xlsx">
                    </div>
                    <button class="btn btn-primary w-100 py-2" type="submit"><i class="bi bi-cloud-upload"></i> Process & Schedule</button>
                </form>
            </div>
        </div>
    </div>
</div>
"""

SETTINGS_PAGE = """
<div class="row justify-content-center">
    <div class="col-lg-6">
        <div class="card">
            <div class="card-header"><h5><i class="bi bi-sliders"></i> System Settings</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3">
                        <label for="gemini_api_key" class="form-label fw-bold">Gemini API Key</label>
                        <div class="input-group">
                            <span class="input-group-text"><i class="bi bi-key"></i></span>
                            <input type="password" name="gemini_api_key" class="form-control" value="{{ current_user.gemini_api_key or '' }}" placeholder="Paste API Key here">
                        </div>
                        <div class="form-text">
                            Required for AI composing and sentiment analysis. 
                            <a href="https://aistudio.google.com/app/apikey" target="_blank">Get a key here</a>.
                        </div>
                    </div>
                    <button type="submit" class="btn btn-primary">Save Changes</button>
                </form>
            </div>
        </div>
    </div>
</div>
"""

ANALYTICS_PAGE = """
<div class="row">
    <div class="col-12">
        <div class="card">
            <div class="card-header"><h5><i class="bi bi-pie-chart-fill"></i> Performance Overview</h5></div>
            <div class="card-body">
                <div class="row text-center mb-5">
                    <div class="col-md-3 border-end">
                        <h2 class="fw-bold">{{ stats.total_sent }}</h2>
                        <span class="badge bg-success">Total Sent</span>
                    </div>
                    <div class="col-md-3 border-end">
                        <h2 class="fw-bold">{{ stats.total_replied }}</h2>
                        <span class="badge bg-info">Replies</span>
                    </div>
                    <div class="col-md-3 border-end">
                        <h2 class="fw-bold">{{ '%.1f'|format(stats.reply_rate) }}%</h2>
                        <span class="badge bg-secondary">Reply Rate</span>
                    </div>
                    <div class="col-md-3">
                        <h2 class="fw-bold text-success">{{ stats.positive_replies }}</h2>
                        <span class="badge bg-success-subtle text-success border border-success">Positive Sentiment</span>
                    </div>
                </div>
                <div class="row justify-content-center">
                    <div class="col-md-8 text-center">
                        {% if stats.total_sent > 0 or stats.total_failed > 0 %}
                            <img src="data:image/png;base64,{{ stats_chart }}" class="img-fluid rounded border p-2 bg-light" alt="Stats Chart">
                        {% else %}
                            <div class="alert alert-light border">No data available for charts yet. Send some emails!</div>
                        {% endif %}
                    </div>
                </div>
            </div>
        </div>
    </div>
</div>
"""

# --- Flask Routes ---
@app.route("/")
def landing_page():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    return render_page(LANDING_PAGE)

@app.route("/dashboard")
@login_required
def dashboard():
    filter_status = request.args.get('filter', 'all')
    with SessionLocal() as session:
        user_accounts_ids = [acc.id for acc in current_user.accounts]
        query = session.query(Task).options(joinedload(Task.account)).filter(Task.account_id.in_(user_accounts_ids))
        
        if filter_status != 'all':
            query = query.filter(Task.status == filter_status)
            
        tasks = query.order_by(Task.send_at.desc()).all()
        return render_page(DASHBOARD_PAGE, tasks=tasks, filter_status=filter_status)

@app.route("/accounts", methods=["GET", "POST"])
@login_required
def accounts():
    with SessionLocal() as session:
        if request.method == "POST":
            # Basic validation
            if not request.form["email"] or not request.form["password"]:
                 flash("Email and Password are required.", "danger")
                 return redirect(url_for("accounts"))

            acc = Account(name=request.form["name"], email=request.form["email"], password=request.form["password"], user_id=current_user.id)
            session.add(acc)
            session.commit()
            flash("Account added successfully!", "success")
            return redirect(url_for("accounts"))
        
        accounts_list = session.query(Account).filter_by(user_id=current_user.id).all()
        # Attach temporary stats
        for acc in accounts_list:
            acc.stats = {
                'total': session.query(func.count(Task.id)).filter(Task.account_id == acc.id).scalar(),
                'sent': session.query(func.count(Task.id)).filter(Task.account_id == acc.id, Task.status == 'sent').scalar(),
                'replied': session.query(func.count(Task.id)).filter(Task.account_id == acc.id, Task.status == 'replied').scalar()
            }
        return render_page(ACCOUNTS_PAGE, accounts=accounts_list)

@app.route("/account/<int:account_id>/delete", methods=["POST"])
@login_required
def delete_account(account_id):
    with SessionLocal() as session:
        account = session.query(Account).filter_by(id=account_id, user_id=current_user.id).first()
        if account:
            session.delete(account)
            session.commit()
            flash("Account deleted.", "success")
    return redirect(url_for("accounts"))

@app.route("/compose", methods=["GET", "POST"])
@login_required
def compose():
    with SessionLocal() as session:
        accounts = session.query(Account).filter_by(user_id=current_user.id).all()
        if not accounts:
            flash("Please connect an email account first.", "warning")
            return redirect(url_for("accounts"))

        templates_query = session.query(Template).filter_by(user_id=current_user.id).all()
        templates_for_js = [{"id": t.id, "name": t.name, "body": t.body} for t in templates_query]
        
        contacts = session.query(Contact).filter_by(user_id=current_user.id).all()
        broadcast = request.args.get('broadcast', type=bool)
        
        # Pre-fill body if replying
        reply_id = request.args.get('reply_to_inbox_id')
        initial_body = ""
        initial_subject = ""
        if reply_id:
            msg = session.get(Inbox, reply_id)
            if msg:
                initial_subject = f"Re: {msg.subject}" if not msg.subject.startswith("Re:") else msg.subject
                initial_body = f"\n\n\n--- On {msg.date}, {msg.from_addr} wrote ---\n{msg.body[:200]}..."

        if request.method == "POST":
            account_id = request.form["account_id"]
            subject = request.form["subject"]
            body = request.form["body"]
            try:
                send_at_dt = datetime.strptime(request.form["send_at"], "%Y-%m-%dT%H:%M")
            except ValueError:
                flash("Invalid date format.", "danger")
                return redirect(request.url)

            attachment_path = None
            if 'attachment' in request.files:
                file = request.files['attachment']
                if file.filename != '':
                    filename = secure_filename(f"{uuid.uuid4().hex[:8]}_{file.filename}")
                    attachment_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(attachment_path)

            if broadcast:
                if not contacts:
                    flash("No contacts to broadcast to.", "warning")
                else:
                    count = 0
                    for contact in contacts:
                        task = Task(account_id=account_id, receiver=contact.email, subject=subject, body=body, send_at=send_at_dt, attachment_path=attachment_path)
                        session.add(task)
                        # Slightly stagger sends to avoid blocking
                        send_at_dt += timedelta(seconds=2)
                        session.flush()
                        _push_task_heap(send_at_dt, task.id)
                        count += 1
                    flash(f"Broadcast scheduled for {count} contacts!", "success")
            else:
                rcpt = request.form.get("receiver")
                if not rcpt:
                    flash("Receiver email required.", "danger")
                    return redirect(request.url)
                    
                task = Task(account_id=account_id, receiver=rcpt, subject=subject, body=body, send_at=send_at_dt, attachment_path=attachment_path)
                session.add(task)
                session.flush()
                _push_task_heap(send_at_dt, task.id)
                flash("Email scheduled successfully!", "success")
            
            session.commit()
            return redirect(url_for("dashboard"))

        default_time = (datetime.now() + timedelta(minutes=2)).strftime("%Y-%m-%dT%H:%M")
        return render_page(COMPOSE_PAGE, accounts=accounts, templates=templates_for_js, contacts=contacts, broadcast=broadcast, contact_count=len(contacts), default_time=default_time)

@app.route("/inbox")
@login_required
def inbox():
    with SessionLocal() as session:
        user_accounts_ids = [acc.id for acc in current_user.accounts]
        messages = session.query(Inbox).options(joinedload(Inbox.account)).filter(Inbox.account_id.in_(user_accounts_ids)).order_by(Inbox.date.desc()).all()
        return render_page(INBOX_PAGE, messages=messages)

@app.route("/contacts", methods=["GET", "POST"])
@login_required
def contacts():
    with SessionLocal() as session:
        if request.method == "POST":
            contact = Contact(name=request.form["name"], email=request.form["email"], user_id=current_user.id)
            session.add(contact)
            session.commit()
            flash("Contact added.", "success")
            return redirect(url_for("contacts"))
        contacts = session.query(Contact).filter_by(user_id=current_user.id).order_by(Contact.name).all()
        return render_page(CONTACTS_PAGE, contacts=contacts)

@app.route("/contact/<int:contact_id>/delete", methods=["POST"])
@login_required
def delete_contact(contact_id):
    with SessionLocal() as session:
        contact = session.query(Contact).filter_by(id=contact_id, user_id=current_user.id).first()
        if contact:
            session.delete(contact)
            session.commit()
            flash("Contact deleted.", "success")
    return redirect(url_for("contacts"))

@app.route("/templates", methods=["GET", "POST"])
@login_required
def templates():
    with SessionLocal() as session:
        if request.method == "POST":
            template_name = request.form["name"].strip()
            if not template_name:
                flash("Template name cannot be empty.", "warning")
            else:
                existing = session.query(Template).filter_by(name=template_name, user_id=current_user.id).first()
                if existing:
                    flash(f"A template with the name '{template_name}' already exists.", "danger")
                else:
                    template = Template(name=template_name, body=request.form["body"], user_id=current_user.id)
                    session.add(template)
                    session.commit()
                    flash("Template saved.", "success")
            return redirect(url_for("templates"))
        templates = session.query(Template).filter_by(user_id=current_user.id).order_by(Template.name).all()
        return render_page(TEMPLATES_PAGE, templates=templates)

@app.route("/template/<int:template_id>/delete", methods=["POST"])
@login_required
def delete_template(template_id):
    with SessionLocal() as session:
        template = session.query(Template).filter_by(id=template_id, user_id=current_user.id).first()
        if template:
            session.delete(template)
            session.commit()
            flash("Template deleted.", "success")
    return redirect(url_for("templates"))

@app.route("/task/<int:task_id>/delete", methods=["POST"])
@login_required
def delete_task(task_id):
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task and task.account.user_id == current_user.id:
            session.delete(task)
            session.commit()
            flash("Task deleted.", "success")
    return redirect(url_for("dashboard"))

@app.route("/task/<int:task_id>/retry", methods=["POST"])
@login_required
def retry_task(task_id):
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task and task.account.user_id == current_user.id:
            # Reset status and schedule for "now" (plus 5 seconds)
            task.status = "pending"
            task.send_at = datetime.now() + timedelta(seconds=5)
            task.last_error = None
            session.commit()
            _push_task_heap(task.send_at, task.id)
            flash("Task queued for retry.", "success")
        else:
            flash("Task not found or permission denied.", "danger")
    return redirect(url_for("dashboard"))

@app.route("/bulk-upload", methods=["GET", "POST"])
@login_required
def bulk_upload():
    with SessionLocal() as session:
        accounts = session.query(Account).filter_by(user_id=current_user.id).all()
        if not accounts:
            flash("Please add a sender account first.", "warning")
            return redirect(url_for("accounts"))
        
        if request.method == "POST":
            file = request.files.get('file')
            if not file or file.filename == '':
                flash("No file selected.", "warning")
                return redirect(url_for("bulk_upload"))
            
            try:
                if file.filename.endswith('.csv'):
                    df = pd.read_csv(file)
                elif file.filename.endswith('.xlsx'):
                    df = pd.read_excel(file)
                else:
                    flash("Unsupported file type. Use CSV or XLSX.", "danger")
                    return redirect(url_for("bulk_upload"))

                required_cols = ['Receiver', 'Subject', 'Body', 'Schedule']
                if not all(col in df.columns for col in required_cols):
                     flash(f"Missing required columns: {required_cols}", "danger")
                     return redirect(url_for("bulk_upload"))

                account_id = request.form["account_id"]
                count = 0
                for _, row in df.iterrows():
                    try:
                        send_at_dt = pd.to_datetime(row['Schedule']).to_pydatetime()
                    except:
                        send_at_dt = datetime.now() + timedelta(minutes=10) # Default if parse fails
                        
                    task = Task(account_id=account_id, receiver=row['Receiver'], subject=row['Subject'], body=row['Body'], send_at=send_at_dt)
                    session.add(task)
                    session.flush()
                    _push_task_heap(send_at_dt, task.id)
                    count += 1
                session.commit()
                flash(f"Successfully scheduled {count} emails from file.", "success")
                return redirect(url_for("dashboard"))
            except Exception as e:
                flash(f"Error processing file: {e}", "danger")
                return redirect(url_for("bulk_upload"))

        return render_page(BULK_UPLOAD_PAGE, accounts=accounts)

# --- Auth Routes ---
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        with SessionLocal() as session:
            user = session.query(User).filter_by(username=request.form['username']).first()
            if user and check_password_hash(user.password_hash, request.form['password']):
                login_user(user)
                return redirect(url_for('dashboard'))
            else:
                flash('Invalid username or password.', 'danger')
    return render_page(AUTH_PAGE_TEMPLATE, title="Welcome Back", button_text="Login to Hub", footer_text='Don\'t have an account? <a href="/register">Register here</a>')

@app.route('/register', methods=['GET', 'POST'])
def register():
    if current_user.is_authenticated:
        return redirect(url_for('dashboard'))
    if request.method == 'POST':
        with SessionLocal() as session:
            existing = session.query(User).filter_by(username=request.form['username']).first()
            if existing:
                flash('Username already taken.', 'warning')
            else:
                hashed_password = generate_password_hash(request.form['password'], method='pbkdf2:sha256')
                new_user = User(username=request.form['username'], password_hash=hashed_password)
                session.add(new_user)
                session.commit()
                flash('Registration successful! Please login.', 'success')
                return redirect(url_for('login'))
    return render_page(AUTH_PAGE_TEMPLATE, title="Create Account", button_text="Register", footer_text='Already have an account? <a href="/login">Login here</a>')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

@app.route('/settings', methods=['GET', 'POST'])
@login_required
def settings():
    if request.method == 'POST':
        with SessionLocal() as session:
            user = session.get(User, current_user.id)
            user.gemini_api_key = request.form['gemini_api_key']
            session.commit()
            flash('Settings updated successfully!', 'success')
            return redirect(url_for('settings'))
    return render_page(SETTINGS_PAGE)

# --- Gemini AI Routes ---
@app.route('/generate-email-body', methods=['POST'])
@login_required
def generate_email_body():
    if not current_user.gemini_api_key:
        return jsonify({'error': 'Gemini API key not found. Go to Settings to add it.'}), 400
    try:
        user_prompt = request.json['prompt']
        full_prompt = f"Write a professional email body based on this request: '{user_prompt}'. Do NOT include a subject line. Do NOT include placeholders like [Your Name] if possible, just keep it generic or use 'The Team'. Output only the body."
        
        # Using standard 1.5 Flash model
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={current_user.gemini_api_key}"
        payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
        
        response = requests.post(api_url, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        
        # Handle safety ratings or empty content
        if 'candidates' not in result or not result['candidates']:
             return jsonify({'error': 'AI declined to generate content (Safety Filter).'}), 400
             
        text = result['candidates'][0]['content']['parts'][0]['text']
        return jsonify({'text': text})
    except Exception as e:
        logger.error(f"AI Generation Error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/generate-subject', methods=['POST'])
@login_required
def generate_subject():
    if not current_user.gemini_api_key:
        return jsonify({'error': 'Gemini API key not found.'}), 400
    try:
        email_body = request.json['email_body']
        full_prompt = f"Generate 3 catchy, professional subject lines for this email body. Return ONLY a JSON array of strings (e.g. [\"Subject 1\", \"Subject 2\"]).\n\nEmail Body: '{email_body[:2000]}'"
        
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-1.5-flash:generateContent?key={current_user.gemini_api_key}"
        # Request JSON response schema
        payload = {
            "contents": [{"parts": [{"text": full_prompt}]}], 
            "generationConfig": { "responseMimeType": "application/json" }
        }
        
        response = requests.post(api_url, json=payload, timeout=30)
        response.raise_for_status()
        result = response.json()
        subjects_text = result['candidates'][0]['content']['parts'][0]['text']
        subjects = json.loads(subjects_text)
        return jsonify({'subjects': subjects})
    except Exception as e:
        logger.error(f"AI Subject Error: {e}")
        return jsonify({'error': str(e)}), 500

# --- Analytics Route ---
@app.route('/analytics')
@login_required
def analytics():
    with SessionLocal() as session:
        user_accounts_ids = [acc.id for acc in current_user.accounts]
        
        total_sent = session.query(func.count(Task.id)).filter(Task.account_id.in_(user_accounts_ids), Task.status == 'sent').scalar()
        total_failed = session.query(func.count(Task.id)).filter(Task.account_id.in_(user_accounts_ids), Task.status == 'failed').scalar()
        total_replied = session.query(func.count(Task.id)).filter(Task.account_id.in_(user_accounts_ids), Task.status == 'replied').scalar()
        
        stats = {
            'total_sent': total_sent,
            'total_failed': total_failed,
            'total_replied': total_replied,
            'reply_rate': (total_replied / total_sent * 100) if total_sent > 0 else 0,
            'positive_replies': session.query(func.count(Inbox.id)).filter(Inbox.account_id.in_(user_accounts_ids), Inbox.sentiment == 'Positive').scalar()
        }

        # Generate Chart
        labels = ['Sent', 'Replied', 'Failed']
        sizes = [total_sent, total_replied, total_failed]
        
        # Avoid plotting empty charts
        if sum(sizes) > 0:
            colors = ['#198754', '#0dcaf0', '#dc3545'] # Bootstrap Success, Info, Danger
            
            fig, ax = plt.subplots(figsize=(6, 6))
            ax.pie(sizes, labels=labels, colors=colors, autopct='%1.1f%%', startangle=140, wedgeprops=dict(width=0.4))
            ax.set_title('Campaign Performance')
            
            buf = BytesIO()
            fig.savefig(buf, format='png', transparent=True, bbox_inches='tight')
            chart_image = base64.b64encode(buf.getvalue()).decode('utf-8')
            plt.close(fig)
            stats['stats_chart'] = chart_image
        else:
            stats['stats_chart'] = ""
        
        return render_page(ANALYTICS_PAGE, stats=stats)


# --- Initializer Function ---
def initialize_app():
    # Only load tasks if this is the main thread to prevent duplication in some WSGI servers
    # though strictly for this script it's just 'main'
    with SessionLocal() as session:
        # Load pending tasks into memory heap on restart
        tasks = session.query(Task).filter(Task.status.in_(["pending"])).all()
        logger.info(f"Re-queueing {len(tasks)} pending tasks...")
        for task in tasks:
            if task.send_at:
                 _push_task_heap(task.send_at, task.id)

    if not WORKER_STARTED.is_set():
        t = threading.Thread(target=_worker_loop, daemon=True)
        t.start()
        WORKER_STARTED.set()
        
    if not IMAP_STARTED.is_set():
        t = threading.Thread(target=_imap_poller_loop, daemon=True)
        t.start()
        IMAP_STARTED.set()

# --- Main Execution ---
if __name__ == "__main__":
    try:
        initialize_app()
        # Changed port to 5001 to avoid conflict with AirPlay on macOS
        port = 5001
        print("\n--- Email Automation Hub Started ---")
        print(f"Access the dashboard at: http://localhost:{port}")
        app.run(host="0.0.0.0", port=port, debug=True, use_reloader=False)
    except Exception as e:
        print(f"Failed to start app: {e}")
