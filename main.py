# Final Advanced Email Scheduler
# --------------------------------
# Required libraries:
# pip install flask sqlalchemy pandas openpyxl certifi

import os
import time
import heapq
import threading
import uuid
import imaplib
import email
import ssl
from email.header import decode_header, make_header
from email.utils import parsedate_to_datetime, formataddr
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta
from types import SimpleNamespace
from flask import Flask, request, redirect, url_for, flash, render_template_string
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, ForeignKey, func
from sqlalchemy.orm import sessionmaker, declarative_base, relationship, joinedload
import pandas as pd
import smtplib

# --- Basic Configuration ---
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
DB_PATH = os.path.join(BASE_DIR, "data", "email_scheduler.sqlite")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

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
    password = Column(String(1024), nullable=False) # App Password
    created_at = Column(DateTime, default=datetime.utcnow)

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

Account.tasks = relationship("Task", order_by=Task.id, back_populates="account", cascade="all, delete-orphan")
Base.metadata.create_all(engine)

# --- Flask App Initialization ---
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET", "super-secret-key-for-dev")

# --- Background Worker Setup ---
TASK_HEAP = []
HEAP_LOCK = threading.Lock()
STOP_EVENT = threading.Event()
WORKER_STARTED = threading.Event()
IMAP_STARTED = threading.Event()

# --- Helper Functions ---
def _create_unverified_ssl_context():
    """Creates an SSL context that does not verify certificates."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context

def _push_task_heap(send_at, task_id):
    with HEAP_LOCK:
        heapq.heappush(TASK_HEAP, (send_at.timestamp(), task_id))

def _send_via_smtp(account: Account, to_email: str, subject: str, body: str, in_reply_to=None):
    msg = MIMEMultipart()
    msg["From"] = formataddr((account.name, account.email))
    msg["To"] = to_email
    msg["Subject"] = subject
    msg["Message-ID"] = f"<{uuid.uuid4().hex}@{account.email.split('@')[-1]}>"
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    
    msg.attach(MIMEText(body, "html", "utf-8"))
    
    ssl_context = _create_unverified_ssl_context()

    server = smtplib.SMTP("smtp.gmail.com", 587, timeout=30)
    server.starttls(context=ssl_context)
    server.login(account.email, account.password)
    server.send_message(msg)
    server.quit()
    return msg["Message-ID"]

# --- Background Threads ---
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
                msgid = _send_via_smtp(task.account, task.receiver, task.subject, task.body)
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
                    
                    # Search for emails from the last 7 days for robustness
                    date_since = (datetime.now() - timedelta(days=7)).strftime("%d-%b-%Y")
                    search_criteria = f'(SENTSINCE "{date_since}")'
                    
                    result, data = imap.search(None, search_criteria)
                    if result != "OK": continue

                    print(f"Found {len(data[0].split())} recent messages for {account.email}")

                    for uid in data[0].split():
                        if not uid: continue
                        # Check if this message has already been processed
                        res, msg_data = imap.fetch(uid, "(BODY[HEADER.FIELDS (MESSAGE-ID)])")
                        if res != "OK": continue
                        header_data = msg_data[0][1].decode('utf-8')
                        current_message_id = email.message_from_string(header_data).get('Message-ID')
                        
                        if session.query(Inbox).filter_by(message_id=current_message_id).first():
                            continue # Skip already processed email

                        res, msg_data = imap.fetch(uid, "(RFC822)")
                        if res != "OK": continue
                        
                        msg = email.message_from_bytes(msg_data[0][1])
                        in_reply_to = msg.get("In-Reply-To")
                        if not in_reply_to: continue

                        print(f"  - Checking UID {uid.decode()}, In-Reply-To: {in_reply_to}")
                        task_match = session.query(Task).filter(Task.message_id == in_reply_to).first()
                        
                        if task_match:
                            print(f"  - Match found! Updating task {task_match.id} to replied.")
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

                            inbox_entry = Inbox(
                                account_id=account.id,
                                from_addr=from_addr,
                                subject=subject,
                                date=date,
                                body=body_text,
                                message_id=current_message_id,
                                in_reply_to=in_reply_to,
                                task_id=task_match.id
                            )
                            session.add(inbox_entry)
                            session.commit()
                    imap.logout()
                except Exception as e:
                    print(f"IMAP Error for {account.email}: {e}")
        STOP_EVENT.wait(120) # Poll every 2 minutes

# --- HTML Templates ---
def render_page(content_template, **kwargs):
    base_html = """
    <!doctype html>
    <html lang="en" data-bs-theme="light">
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <title>Gainers Automation </title>
        <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
        <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.min.css">
        <style>
            body { background-color: #f0f2f5; }
            .navbar { background-color: #ffffff; box-shadow: 0 2px 4px rgba(0,0,0,.08); }
            .card { border: none; border-radius: 0.5rem; box-shadow: 0 1px 3px rgba(0,0,0,.1); }
            .btn-primary { background-color: #0d6efd; border-color: #0d6efd; }
            .nav-link.active { font-weight: 600; color: #0d6efd !important; }
        </style>
    </head>
    <body>
        <nav class="navbar navbar-expand-lg sticky-top mb-4">
            <div class="container">
                <a class="navbar-brand fw-bold" href="{{ url_for('dashboard') }}">📧 Gainers Future Email Automation</a>
                <div class="collapse navbar-collapse">
                    <ul class="navbar-nav me-auto mb-2 mb-lg-0">
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'dashboard' %}active{% endif %}" href="{{ url_for('dashboard') }}">Dashboard</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'accounts' %}active{% endif %}" href="{{ url_for('accounts') }}">Accounts</a></li>
                        <li class="nav-item"><a class="nav-link {% if request.endpoint == 'inbox' %}active{% endif %}" href="{{ url_for('inbox') }}">Inbox</a></li>
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

DASHBOARD_PAGE = """
<div class="card">
    <div class="card-header bg-light"><h5><i class="bi bi-clock-history"></i> Scheduled & Sent Tasks</h5></div>
    <div class="card-body">
        <div class="table-responsive">
            <table class="table table-hover align-middle">
                <thead>
                    <tr>
                        <th>From</th><th>To</th><th>Subject</th><th>Schedule</th><th>Status</th><th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for t in tasks %}
                    <tr>
                        <td><small>{{ t.account.name }}<br>{{ t.account.email }}</small></td>
                        <td>{{ t.receiver }}</td>
                        <td>{{ t.subject }}</td>
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
                    <tr><td colspan="6" class="text-center text-muted">No tasks found.</td></tr>
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
                    <div>
                        <h5 class="card-title mb-0">{{ acc.name }}</h5>
                        <p class="card-text text-muted">{{ acc.email }}</p>
                    </div>
                    <form method="POST" action="{{ url_for('delete_account', account_id=acc.id) }}" onsubmit="return confirm('Delete this account and all its data?');">
                        <button type="submit" class="btn btn-sm btn-danger"><i class="bi bi-trash"></i> Delete</button>
                    </form>
                </div>
                <hr>
                <div class="row text-center">
                    <div class="col">
                        <h6>{{ acc.stats.total }}</h6><p class="text-muted mb-0">Scheduled</p>
                    </div>
                    <div class="col">
                        <h6 class="text-success">{{ acc.stats.sent }}</h6><p class="text-muted mb-0">Sent</p>
                    </div>
                    <div class="col">
                        <h6 class="text-info">{{ acc.stats.replied }}</h6><p class="text-muted mb-0">Replied</p>
                    </div>
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
        <h5><i class="bi bi-envelope-plus"></i> {{ 'Reply to Email' if reply_to else 'Compose & Schedule' }}</h5>
    </div>
    <div class="card-body">
        <form method="post">
            <div class="row">
                <div class="col-md-6 mb-3">
                    <label class="form-label">From Account</label>
                    <select name="account_id" class="form-select" required>
                        {% for a in accounts %}<option value="{{ a.id }}" {% if reply_to and reply_to.account_id == a.id %}selected{% endif %}>{{ a.name }} ({{ a.email }})</option>{% endfor %}
                    </select>
                </div>
                <div class="col-md-6 mb-3">
                    <label class="form-label">To</label>
                    <input name="receiver" type="email" class="form-control" value="{{ reply_to.receiver if reply_to else '' }}" required>
                </div>
            </div>
            <div class="mb-3">
                <label class="form-label">Subject</label>
                <input name="subject" type="text" class="form-control" value="{{ reply_to.subject if reply_to else '' }}" required>
            </div>
            <div class="mb-3">
                <label class="form-label">Body (HTML Supported)</label>
                <textarea name="body" rows="10" class="form-control" required>{{ reply_to.body | safe if reply_to else '' }}</textarea>
            </div>
            <div class="mb-3">
                <label class="form-label">Schedule Time</label>
                <input name="send_at" type="datetime-local" class="form-control" required>
            </div>
            <button class="btn btn-primary" type="submit"><i class="bi bi-send"></i> Schedule Email</button>
        </form>
    </div>
</div>
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
            <div class="p-2 bg-light border rounded mb-2" style="max-height: 100px; overflow-y: auto;">
                <small>{{ msg.body }}</small>
            </div>
            <a href="{{ url_for('compose', reply_to_inbox_id=msg.id) }}" class="btn btn-sm btn-outline-primary"><i class="bi bi-reply"></i> Reply to this Email</a>
        </div>
        {% else %}
        <p class="text-center text-muted">No replies found in inbox yet.</p>
        {% endfor %}
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

        reply_to_data = None
        reply_to_inbox_id = request.args.get('reply_to_inbox_id')
        if reply_to_inbox_id:
            inbox_msg = session.get(Inbox, reply_to_inbox_id)
            if inbox_msg:
                quoted_body = f"<br><br><hr>On {inbox_msg.date.strftime('%c')}, {inbox_msg.from_addr} wrote:<blockquote>{inbox_msg.body}</blockquote>"
                reply_to_data = SimpleNamespace(
                    account_id=inbox_msg.account_id,
                    receiver=inbox_msg.from_addr,
                    subject=f"Re: {inbox_msg.subject}",
                    body=quoted_body
                )

        if request.method == "POST":
            send_at_dt = datetime.strptime(request.form["send_at"], "%Y-%m-%dT%H:%M")
            task = Task(
                account_id=request.form["account_id"],
                receiver=request.form["receiver"],
                subject=request.form["subject"],
                body=request.form["body"],
                send_at=send_at_dt
            )
            session.add(task)
            session.commit()
            _push_task_heap(send_at_dt, task.id)
            flash("Email scheduled successfully!", "success")
            return redirect(url_for("dashboard"))

        return render_page(COMPOSE_PAGE, accounts=accounts, reply_to=reply_to_data)

@app.route("/inbox")
def inbox():
    with SessionLocal() as session:
        messages = session.query(Inbox).options(joinedload(Inbox.account)).order_by(Inbox.date.desc()).all()
        return render_page(INBOX_PAGE, messages=messages)

@app.route("/task/<int:task_id>/delete", methods=["POST"])
def delete_task(task_id):
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        if task:
            session.delete(task)
            session.commit()
            flash("Task deleted.", "success")
    return redirect(url_for("dashboard"))

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
