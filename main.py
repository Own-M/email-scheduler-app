# Final Advanced Email Scheduler with Webhooks and Bulk Upload
# --------------------------------------------------------------------
# Required libraries:
# pip install flask sqlalchemy pandas openpyxl certifi requests

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
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, formataddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from datetime import datetime, timedelta
from types import SimpleNamespace
from flask import Flask, request, redirect, url_for, flash, render_template_string, jsonify
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, joinedload
from werkzeug.utils import secure_filename

# --- Basic Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "email_scheduler.sqlite")
UPLOAD_FOLDER = os.path.join(BASE_DIR, "uploads")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# --- Database Setup ---
engine = create_engine(f"sqlite:///{DB_PATH}", connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)
Base = declarative_base()

# --- Database Models ---
class Account(Base):
    __tablename__ = "accounts"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(320), nullable=False, unique=True)
    password = Column(String(1024), nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

class Task(Base):
    __tablename__ = "tasks"
    id = Column(Integer, primary_key=True)
    account_id = Column(Integer, ForeignKey("accounts.id"), nullable=False)
    receiver = Column(String(320), nullable=False)
    subject = Column(String(998), nullable=False)
    body = Column(Text, nullable=False)
    send_at = Column(DateTime, nullable=False)
    status = Column(String(32), default="pending")
    attempts = Column(Integer, default=0)
    last_error = Column(Text, nullable=True)
    message_id = Column(String(256), nullable=True, unique=True)
    attachment_path = Column(String(512), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
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
    account = relationship("Account")
    task = relationship("Task")

class Template(Base):
    __tablename__ = "templates"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, unique=True)
    body = Column(Text, nullable=False)

class Contact(Base):
    __tablename__ = "contacts"
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    email = Column(String(320), nullable=False, unique=True)

Account.tasks = relationship("Task", order_by=Task.id, back_populates="account", cascade="all, delete-orphan")
Base.metadata.create_all(engine)

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "super-secret-key-for-dev")
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER

# --- Background Worker Setup ---
TASK_HEAP = []
HEAP_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WORKER_STARTED = threading.Event()
IMAP_STARTED = threading.Event()

# --- Helper Functions ---
def _create_unverified_ssl_context():
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
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{account.email.split('@')[-1]}>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    
    msg.attach(MIMEText(body, "html", "utf-8"))

    if attachment_path and os.path.exists(attachment_path):
        with open(attachment_path, "rb") as attachment:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(attachment.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", f"attachment; filename= {os.path.basename(attachment_path)}")
        msg.attach(part)
    
    ssl_context = _create_unverified_ssl_context()
    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    server.starttls(context=ssl_context)
    server.login(account.email, account.password)
    server.send_message(msg)
    server.quit()
    return msg["Message-ID"]

# --- Background Threads (Worker, IMAP Poller) ---
def _worker_loop():
    while not STOP_EVENT.is_set():
        task_id_to_process = None
        with HEAP_LOCK:
            if TASK_HEAP and TASK_HEAP[0][0] <= time.time():
                _, task_id_to_process = heapq.heappop(TASK_HEAP)
        
        if task_id_to_process is None:
            time.sleep(1)
            continue

        with SessionLocal() as session:
            task = session.query(Task).options(joinedload(Task.account)).get(task_id_to_process)
            if not task or task.status not in ("pending", "failed"):
                continue
            
            try:
                task.status = "sending"
                session.commit()
                msgid = _send_via_smtp(task.account, task.receiver, task.subject, task.body, task.attachment_path)
                task.message_id = msgid
                task.status = "sent"
                task.last_error = None
            except Exception as e:
                task.status = "failed"
                task.last_error = str(e)
                print(f"SMTP Error for task {task.id}: {e}")
            finally:
                task.attempts += 1
                session.commit()

def _imap_poller_loop():
    while not STOP_EVENT.is_set():
        with SessionLocal() as session:
            accounts = session.query(Account).all()
            for account in accounts:
                try:
                    ssl_context = _create_unverified_ssl_context()
                    imap = imaplib.IMAP4_SSL("imap.gmail.com", 993, ssl_context=ssl_context)
                    imap.login(account.email, account.password)
                    imap.select("INBOX")
                    
                    date_since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
                    search_criteria = f'(SENTSINCE "{date_since}")'
                    
                    result, data = imap.search(None, search_criteria)
                    if result != "OK": continue

                    for uid in data[0].split():
                        if not uid: continue
                        res, msg_data = imap.fetch(uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
                        if res != "OK": continue
                        header_data = msg_data[0][1].decode('utf-8')
                        current_message_id = email.message_from_string(header_data).get('Message-ID')
                        
                        if session.query(Inbox).filter_by(message_id=current_message_id).first():
                            continue

                        res, msg_data = imap.fetch(uid, "(RFC822)")
                        if res != "OK": continue
                        
                        msg = email.message_from_bytes(msg_data[0][1])
                        in_reply_to = msg.get("In-Reply-To")
                        if not in_reply_to: continue

                        task_match = session.query(Task).filter(Task.message_id == in_reply_to).first()
                        
                        if task_match:
                            task_match.status = "replied"
                            from_addr = str(make_header(decode_header(msg.get("From", ""))))
                            subject = str(make_header(decode_header(msg.get("Subject", ""))))
                            date = parsedate_to_datetime(msg.get("Date", ""))
                            body_text = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() == "text/plain":
                                        body_text = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "ignore")
                                        break
                            else:
                                body_text = msg.get_payload(decode=True).decode(msg.get_content_charset() or "utf-8", "ignore")

                            inbox_entry = Inbox(account_id=account.id, from_addr=from_addr, subject=subject, date=date, body=body_text, message_id=current_message_id, in_reply_to=in_reply_to, task_id=task_match.id)
                            session.add(inbox_entry)
                            session.commit()
                    imap.logout()
                except Exception as e:
                    print(f"IMAP Error for {account.email}: {e}")
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
            body { background-color: #f0f2f5; }
            .navbar { background-color: #ffffff; box-shadow: 0 2px 4px rgba(0,0,0,.08); }
            .card { border: none; border-radius: 0.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
            .nav-link.active { font-weight: 600; color: #0d6efd !important; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-expand-lg sticky-top mb-4">
            <div class="container">
                <a class="navbar-brand fw-bold" href="{{ url_for('dashboard') }}">📧 Automation Hub</a>
                <div class="collapse navbar-collapse">
                    <ul class="navbar-nav me-auto mb-2 mb-lg-0">
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">Dashboard</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'accounts' %}active{% endif %}" href="{{ url_for('accounts') }}">Accounts</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'inbox' %}active{% endif %}" href="{{ url_for('inbox') }}">Inbox</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'contacts' %}active{% endif %}" href="{{ url_for('contacts') }}">Contacts</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'templates' %}active{% endif %}" href="{{ url_for('templates') }}">Templates</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'bulk_upload' %}active{% endif %}" href="{{ url_for('bulk_upload') }}">Bulk Upload</a></li>
                    </ul>
                    <a href="{{ url_for('compose') }}" class="btn btn-primary"><i class="bi bi-pencil-square"></i> Compose</a>
                </div>
            </div>
        </nav>
        <div class="container">
            {% with messages = get_flashed_messages(with_categories=true) %}
                {% if messages %}
                    {% for category, message in messages %}
                        <div class="alert alert-{{ category }} alert-dismissible fade show" role="alert">
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
DASHBOARD_PAGE = """
<div class="card">
    <div class="card-header bg-light"><h5><i class="bi bi-clock-history"></i> Scheduled & Sent Tasks</h5></div>
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-hover align-middle">
                <thead><tr><th>From</th><th>To</th><th>Subject</th><th>Attachment</th><th>Schedule</th><th>Status</th><th>Actions</th></tr></thead>
                <tbody>
                    {% for t in tasks %}
                    <tr>
                        <td><small>{{ t.account.name }}<br>{{ t.account.email }}</small></td>
                        <td>{{ t.receiver }}</td>
                        <td>{{ t.subject }}</td>
                        <td>{% if t.attachment_path %}<i class="bi bi-paperclip"></i>{% endif %}</td>
                        <td>{{ t.send_at.strftime('%Y-%m-%d %H:%M') }}</td>
                        <td>
                            {% if t.status == 'sent' %}<span class="badge text-bg-success">Sent</span>
                            {% elif t.status == 'failed' %}<span class="badge text-bg-danger" title="{{ t.last_error }}">Failed</span>
                            {% elif t.status == 'replied' %}<span class="badge text-bg-info">Replied</span>
                            {% else %}<span class="badge text-bg-secondary">{{ t.status|capitalize }}</span>
                            {% endif %}
                        </td>
                        <td>
                            <form method="POST" action="{{ url_for('delete_task', task_id=t.id) }}" onsubmit="return confirm('Delete this task?');">
                                <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button>
                            </form>
                        </td>
                    </tr>
                    {% else %}
                    <tr><td colspan="7" class="text-center text-muted">No tasks found.</td></tr>
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
        <div class="card">
            <div class="card-header bg-light"><h5><i class="bi bi-person-plus"></i> Add New Account</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3"><input name="name" class="form-control" placeholder="Account Name (e.g., Work)" required></div>
                    <div class="mb-3"><input name="email" type="email" class="form-control" placeholder="your-email@gmail.com" required></div>
                    <div class="mb-3"><input name="password" type="password" class="form-control" placeholder="Google App Password" required></div>
                    <button class="btn btn-primary w-100" type="submit">Add Account</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-8">
        <h5><i class="bi bi-person-lines-fill"></i> Account Overview</h5>
        {% for acc in accounts %}
        <div class="card mb-3">
            <div class="card-body">
                <div class="d-flex justify-content-between align-items-center">
                    <div><h5 class="card-title mb-0">{{ acc.name }}</h5><p class="card-text text-muted">{{ acc.email }}</p></div>
                    <form method="POST" action="{{ url_for('delete_account', account_id=acc.id) }}" onsubmit="return confirm('Delete this account and all its data?');">
                        <button type="submit" class="btn btn-sm btn-danger"><i class="bi bi-trash"></i> Delete</button>
                    </form>
                </div>
                <hr>
                <div class="row text-center">
                    <div class="col"><h6>{{ acc.stats.total }}</h6><p class="text-muted mb-0">Scheduled</p></div>
                    <div class="col"><h6 class="text-success">{{ acc.stats.sent }}</h6><p class="text-muted mb-0">Sent</p></div>
                    <div class="col"><h6 class="text-info">{{ acc.stats.replied }}</h6><p class="text-muted mb-0">Replied</p></div>
                </div>
            </div>
        </div>
        {% else %}
        <div class="card"><div class="card-body text-center text-muted">No accounts added yet.</div></div>
        {% endfor %}
    </div>
</div>
"""

COMPOSE_PAGE = """
<div class="card">
    <div class="card-header bg-light">
        <h5><i class="bi bi-envelope-plus"></i> {{ 'Broadcast to All Contacts' if broadcast else 'Compose & Schedule' }}</h5>
    </div>
    <div class="card-body">
        <form method="post" enctype="multipart/form-data">
            <div class="row">
                <div class="col-md-6 mb-3">
                    <label class="form-label">From Account</label>
                    <select name="account_id" class="form-select" required>
                        {% for a in accounts %}<option value="{{ a.id }}">{{ a.name }} ({{ a.email }})</option>{% endfor %}
                    </select>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">To</label>
                    {% if broadcast %}
                    <input type="text" class="form-control" value="All Contacts ({{ contact_count }})" readonly>
                    {% else %}
                    <input name="receiver" type="email" class="form-control" list="contact-list" placeholder="Type or select a contact">
                    <datalist id="contact-list">
                        {% for c in contacts %}<option value="{{ c.email }}">{{ c.name }}</option>{% endfor %}
                    </datalist>
                    {% endif %}
                </div>
            </div>
            <div class="mb-3">
                <label class="form-label">Template</label>
                <select id="template-select" class="form-select">
                    <option value="">No Template</option>
                    {% for t in templates %}<option value="{{ t.id }}">{{ t.name }}</option>{% endfor %}
                </select>
            </div>
            <div class="mb-3">
                <label class="form-label">Subject</label>
                <div class="input-group">
                    <input name="subject" id="subject-input" type="text" class="form-control" required>
                    <button class="btn btn-outline-secondary" type="button" id="suggest-subjects-btn" title="Suggest Subjects with AI">Suggest Subjects ✨</button>
                </div>
                 <div id="subject-suggestions" class="mt-2"></div>
            </div>
            <div class="mb-3">
                <label class="form-label">Body (HTML Supported)</label>
                <textarea id="body-textarea" name="body" rows="10" class="form-control" required></textarea>
                <button type="button" class="btn btn-outline-primary btn-sm mt-2" data-bs-toggle="modal" data-bs-target="#ai-compose-modal">
                    Generate with AI ✨
                </button>
            </div>
            <div class="row">
                <div class="col-md-6 mb-3">
                    <label class="form-label">Attachment (Optional)</label>
                    <input name="attachment" type="file" class="form-control">
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">Schedule Time</label>
                    <input name="send_at" type="datetime-local" class="form-control" required>
                </div>
            </div>
            <button class="btn btn-primary" type="submit"><i class="bi bi-send"></i> Schedule Email</button>
        </form>
    </div>
</div>

<!-- AI Compose Modal -->
<div class="modal fade" id="ai-compose-modal" tabindex="-1">
  <div class="modal-dialog">
    <div class="modal-content">
      <div class="modal-header">
        <h5 class="modal-title">AI Email Assistant</h5>
        <button type="button" class="btn-close" data-bs-dismiss="modal"></button>
      </div>
      <div class="modal-body">
        <label for="ai-prompt" class="form-label">Enter your prompt:</label>
        <textarea id="ai-prompt" class="form-control" rows="3" placeholder="e.g., Write a follow-up email for a client..."></textarea>
        <div id="ai-spinner" class="d-none spinner-border spinner-border-sm mt-2" role="status">
            <span class="visually-hidden">Loading...</span>
        </div>
      </div>
      <div class="modal-footer">
        <button type="button" class="btn btn-secondary" data-bs-dismiss="modal">Close</button>
        <button type="button" class="btn btn-primary" id="generate-email-btn">Generate</button>
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
            bodyTextarea.value = selectedTemplate.body;
        } else { bodyTextarea.value = ''; }
    });
    document.getElementById('generate-email-btn').addEventListener('click', async function() {
        const prompt = document.getElementById('ai-prompt').value;
        const spinner = document.getElementById('ai-spinner');
        spinner.classList.remove('d-none'); this.disabled = true;
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
            } else { alert('Failed to generate email body. ' + (data.error || '')); }
        } catch (error) { alert('An error occurred: ' + error); } finally {
            spinner.classList.add('d-none'); this.disabled = false;
        }
    });
    document.getElementById('suggest-subjects-btn').addEventListener('click', async function() {
        const body = document.getElementById('body-textarea').value;
        if (!body.trim()) { alert('Please write the email body first.'); return; }
        this.disabled = true; this.innerHTML = '<span class="spinner-border spinner-border-sm" role="status"></span>';
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
                    btn.className = 'btn btn-outline-secondary btn-sm me-2 mb-2';
                    btn.textContent = subject;
                    btn.onclick = () => {
                        document.getElementById('subject-input').value = subject;
                        suggestionsDiv.innerHTML = '';
                    };
                    suggestionsDiv.appendChild(btn);
                });
            } else { suggestionsDiv.textContent = 'Could not generate suggestions.'; }
        } catch (error) { alert('An error occurred: ' + error); } finally {
            this.disabled = false; this.innerHTML = 'Suggest Subjects ✨';
        }
    });
</script>
"""

INBOX_PAGE = """
<div class="card">
    <div class="card-header bg-light"><h5><i class="bi bi-inbox"></i> Inbox Replies</h5></div>
    <div class="card-body">
        {% for msg in messages %}
        <div class="border-bottom pb-3 mb-3">
            <p class="mb-1"><strong>From:</strong> {{ msg.from_addr }}</p>
            <p class="mb-1"><strong>Subject:</strong> {{ msg.subject }}</p>
            <p class="text-muted mb-2"><small>Received: {{ msg.date.strftime('%Y-%m-%d %H:%M') }} via {{ msg.account.email }}</small></p>
            <div class="p-2 bg-light border rounded mb-2" style="max-height: 100px; overflow-y: auto;"><small>{{ msg.body }}</small></div>
            <a href="{{ url_for('compose', reply_to_inbox_id=msg.id) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-reply"></i> Reply</a>
        </div>
        {% else %}
        <p class="text-center text-muted">No replies found in inbox yet.</p>
        {% endfor %}
    </div>
</div>
"""

TEMPLATES_PAGE = """
<div class="row">
    <div class="col-lg-4 mb-4">
        <div class="card">
            <div class="card-header bg-light"><h5><i class="bi bi-plus-square"></i> Add New Template</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3"><input name="name" class="form-control" placeholder="Template Name" required></div>
                    <div class="mb-3"><textarea name="body" rows="8" class="form-control" placeholder="Template Body (HTML supported)" required></textarea></div>
                    <button class="btn btn-primary w-100" type="submit">Save Template</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-8">
        <div class="card">
            <div class="card-header bg-light"><h5><i class="bi bi-list-task"></i> Saved Templates</h5></div>
            <div class="card-body">
                <ul class="list-group">
                    {% for t in templates %}
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        {{ t.name }}
                        <form method="POST" action="{{ url_for('delete_template', template_id=t.id) }}" onsubmit="return confirm('Delete this template?');">
                            <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button>
                        </form>
                    </li>
                    {% else %}
                    <li class="list-group-item text-center text-muted">No templates saved yet.</li>
                    {% endfor %}
                </ul>
            </div>
        </div>
    </div>
</div>
"""

CONTACTS_PAGE = """
<div class="row">
    <div class="col-lg-4 mb-4">
        <div class="card">
            <div class="card-header bg-light"><h5><i class="bi bi-person-plus"></i> Add New Contact</h5></div>
            <div class="card-body">
                <form method="post">
                    <div class="mb-3"><input name="name" class="form-control" placeholder="Contact Name" required></div>
                    <div class="mb-3"><input name="email" type="email" class="form-control" placeholder="contact@email.com" required></div>
                    <button class="btn btn-primary w-100" type="submit">Save Contact</button>
                </form>
            </div>
        </div>
    </div>
    <div class="col-lg-8">
        <div class="card">
            <div class="card-header bg-light d-flex justify-content-between align-items-center">
                <h5><i class="bi bi-people"></i> Saved Contacts</h5>
                <a href="{{ url_for('compose', broadcast=True) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-broadcast"></i> Compose to All</a>
            </div>
            <div class="card-body">
                <ul class="list-group">
                    {% for c in contacts %}
                    <li class="list-group-item d-flex justify-content-between align-items-center">
                        <div>{{ c.name }} <small class="text-muted">({{ c.email }})</small></div>
                        <form method="POST" action="{{ url_for('delete_contact', contact_id=c.id) }}" onsubmit="return confirm('Delete this contact?');">
                            <button type="submit" class="btn btn-sm btn-outline-danger"><i class="bi bi-trash"></i></button>
                        </form>
                    </li>
                    {% else %}
                    <li class="list-group-item text-center text-muted">No contacts saved yet.</li>
                    {% endfor %}
                </ul>
            </div>
        </div>
    </div>
</div>
"""

BULK_UPLOAD_PAGE = """
<div class="card">
    <div class="card-header bg-light"><h5><i class="bi bi-upload"></i> Bulk Schedule Emails</h5></div>
    <div class="card-body">
        <form method="post" enctype="multipart/form-data">
            <div class="mb-3">
                <label class="form-label">From Account</label>
                <select name="account_id" class="form-select" required>
                    {% for a in accounts %}<option value="{{ a.id }}">{{ a.name }} ({{ a.email }})</option>{% endfor %}
                </select>
            </div>
            <div class="mb-3">
                <label class="form-label">Upload CSV or XLSX File</label>
                <input name="file" type="file" class="form-control" required accept=".csv,.xlsx">
                <div class="form-text">Required columns: <strong>Receiver, Subject, Body, Schedule</strong>. Schedule format: YYYY-MM-DD HH:MM:SS</div>
            </div>
            <button class="btn btn-primary" type="submit"><i class="bi bi-cloud-upload"></i> Upload and Schedule</button>
        </form>
    </div>
</div>
"""

# --- Flask Routes ---
@app.route("/")
def dashboard():
    with SessionLocal() as session:
        tasks = session.query(Task).options(joinedload(Task.account)).order_by(Task.send_at.desc()).all()
        return render_page(DASHBOARD_PAGE, tasks=tasks)

@app.route("/accounts", methods=["GET", "POST"])
def accounts():
    with SessionLocal() as session:
        if request.method == "POST":
            acc = Account(name=request.form["name"], email=request.form["email"], password=request.form["password"])
            session.add(acc)
            session.commit()
            flash("Account added successfully!", "success")
            return redirect(url_for("accounts"))
        accounts_list = session.query(Account).all()
        for acc in accounts_list:
            acc.stats = {
                'total': session.query(func.count(Task.id)).filter(Task.account_id == acc.id).scalar(),
                'sent': session.query(func.count(Task.id)).filter(Task.account_id == acc.id, Task.status == 'sent').scalar(),
                'replied': session.query(func.count(Task.id)).filter(Task.account_id == acc.id, Task.status == 'replied').scalar()
            }
        return render_page(ACCOUNTS_PAGE, accounts=accounts_list)

@app.route("/account/<int:account_id>/delete", methods=["POST"])
def delete_account(account_id):
    with SessionLocal() as session:
        account = session.get(Account, account_id)
        if account:
            session.delete(account)
            session.commit()
            flash("Account and all related data deleted.", "success")
    return redirect(url_for("accounts"))

@app.route("/compose", methods=["GET", "POST"])
def compose():
    with SessionLocal() as session:
        accounts = session.query(Account).all()
        if not accounts:
            flash("Please add an account first.", "warning")
            return redirect(url_for("accounts"))

        templates_query = session.query(Template).all()
        templates_for_js = [{"id": t.id, "name": t.name, "body": t.body} for t in templates_query]
        
        contacts = session.query(Contact).all()
        broadcast = request.args.get('broadcast', type=bool)
        
        if request.method == "POST":
            account_id = request.form["account_id"]
            subject = request.form["subject"]
            body = request.form["body"]
            send_at_dt = datetime.strptime(request.form["send_at"], "%Y-%m-%dT%H:%M")
            attachment_path = None
            
            if 'attachment' in request.files:
                file = request.files['attachment']
                if file.filename != '':
                    filename = secure_filename(file.filename)
                    attachment_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
                    file.save(attachment_path)

            if broadcast:
                all_contacts = session.query(Contact).all()
                for contact in all_contacts:
                    task = Task(account_id=account_id, receiver=contact.email, subject=subject, body=body, send_at=send_at_dt, attachment_path=attachment_path)
                    session.add(task)
                    session.flush()
                    _push_task_heap(send_at_dt, task.id)
                flash(f"Broadcast scheduled for {len(all_contacts)} contacts!", "success")
            else:
                task = Task(account_id=account_id, receiver=request.form["receiver"], subject=subject, body=body, send_at=send_at_dt, attachment_path=attachment_path)
                session.add(task)
                session.flush()
                _push_task_heap(send_at_dt, task.id)
                flash("Email scheduled successfully!", "success")
            
            session.commit()
            return redirect(url_for("dashboard"))

        return render_page(COMPOSE_PAGE, accounts=accounts, templates=templates_for_js, contacts=contacts, broadcast=broadcast, contact_count=len(contacts))

@app.route("/inbox")
def inbox():
    with SessionLocal() as session:
        messages = session.query(Inbox).options(joinedload(Inbox.account)).order_by(Inbox.date.desc()).all()
        return render_page(INBOX_PAGE, messages=messages)

@app.route("/contacts", methods=["GET", "POST"])
def contacts():
    with SessionLocal() as session:
        if request.method == "POST":
            contact = Contact(name=request.form["name"], email=request.form["email"])
            session.add(contact)
            session.commit()
            flash("Contact added.", "success")
            return redirect(url_for("contacts"))
        contacts = session.query(Contact).order_by(Contact.name).all()
        return render_page(CONTACTS_PAGE, contacts=contacts)

@app.route("/contact/<int:contact_id>/delete", methods=["POST"])
def delete_contact(contact_id):
    with SessionLocal() as session:
        contact = session.get(Contact, contact_id)
        if contact:
            session.delete(contact)
            session.commit()
            flash("Contact deleted.", "success")
    return redirect(url_for("contacts"))

@app.route("/templates", methods=["GET", "POST"])
def templates():
    with SessionLocal() as session:
        if request.method == "POST":
            template_name = request.form["name"].strip()
            if not template_name:
                flash("Template name cannot be empty.", "warning")
            else:
                existing = session.query(Template).filter_by(name=template_name).first()
                if existing:
                    flash(f"A template with the name '{template_name}' already exists.", "danger")
                else:
                    template = Template(name=template_name, body=request.form["body"])
                    session.add(template)
                    session.commit()
                    flash("Template saved.", "success")
            return redirect(url_for("templates"))
        templates = session.query(Template).order_by(Template.name).all()
        return render_page(TEMPLATES_PAGE, templates=templates)

@app.route("/template/<int:template_id>/delete", methods=["POST"])
def delete_template(template_id):
    with SessionLocal() as session:
        template = session.get(Template, template_id)
        if template:
            session.delete(template)
            session.commit()
            flash("Template deleted.", "success")
    return redirect(url_for("templates"))

@app.route("/task/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            session.delete(task)
            session.commit()
            flash("Task deleted.", "success")
    return redirect(url_for("dashboard"))

@app.route("/bulk-upload", methods=["GET", "POST"])
def bulk_upload():
    with SessionLocal() as session:
        accounts = session.query(Account).all()
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
                    flash("Unsupported file type. Please use CSV or XLSX.", "danger")
                    return redirect(url_for("bulk_upload"))

                account_id = request.form["account_id"]
                count = 0
                for _, row in df.iterrows():
                    send_at_dt = pd.to_datetime(row['Schedule']).to_pydatetime()
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


# --- Webhook Route ---
@app.route('/webhook/add-contact', methods=['POST'])
def webhook_add_contact():
    data = request.json
    if not data or 'email' not in data or 'name' not in data:
        return jsonify({'status': 'error', 'message': 'Missing name or email'}), 400
    
    with SessionLocal() as session:
        existing = session.query(Contact).filter_by(email=data['email']).first()
        if existing:
            return jsonify({'status': 'error', 'message': 'Contact already exists'}), 409
        
        contact = Contact(name=data['name'], email=data['email'])
        session.add(contact)
        session.commit()
        
        # Optional: Schedule a welcome email
        # You can configure this part as needed
        # For example, send from the first account using the first template
        first_account = session.query(Account).first()
        welcome_template = session.query(Template).first()
        if first_account and welcome_template:
            send_at = datetime.now() + timedelta(minutes=5)
            task = Task(
                account_id=first_account.id,
                receiver=contact.email,
                subject="Welcome!",
                body=welcome_template.body,
                send_at=send_at
            )
            session.add(task)
            session.commit()
            _push_task_heap(send_at, task.id)

    return jsonify({'status': 'success', 'message': 'Contact added'}), 201

# --- Gemini AI Routes ---
@app.route('/generate-email-body', methods=['POST'])
def generate_email_body():
    try:
        user_prompt = request.json['prompt']
        full_prompt = f"Write a professional and clear email body based on the following instruction. Do not include a subject line. The email should be ready to send. Instruction: '{user_prompt}'"
        
        api_key = "AIzaSyAdK1X_ImqFwnURtekBVOjs6FODvD7t8ps" # This will be handled by the environment.
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={api_key}"
        
        payload = {"contents": [{"parts": [{"text": full_prompt}]}]}
        response = requests.post(api_url, json=payload, timeout=30)
        response.raise_for_status()
        
        result = response.json()
        text = result['candidates'][0]['content']['parts'][0]['text']
        return jsonify({'text': text})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/generate-subject', methods=['POST'])
def generate_subject():
    try:
        email_body = request.json['email_body']
        full_prompt = f"Generate 3 short, catchy, and professional subject line options for the following email body. Return them as a JSON array of strings, like [\"Subject 1\", \"Subject 2\", \"Subject 3\"]. Email Body: '{email_body}'"
        
        api_key = "" # This will be handled by the environment.
        api_url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash-preview-05-20:generateContent?key={api_key}"

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
        return jsonify({'error': str(e)}), 500

# --- Initializer Function ---
def initialize_app():
    with SessionLocal() as session:
        tasks = session.query(Task).filter(Task.status.in_(["pending", "failed"])).all()
        for task in tasks:
            if task.send_at > datetime.now():
                _push_task_heap(task.send_at, task.id)

    if not WORKER_STARTED.is_set():
        threading.Thread(target=_worker_loop, daemon=True).start()
        WORKER_STARTED.set()
    if not IMAP_STARTED.is_set():
        threading.Thread(target=_imap_poller_loop, daemon=True).start()
        IMAP_STARTED.set()

# --- Main Execution ---
if __name__ == "__main__":
    initialize_app()
    app.run(host="0.0.0.0", port=5000, debug=True, use_reloader=False)
