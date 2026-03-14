# Advanced Email Scheduler

This is a multi-account email scheduling application built with Flask. It allows users to schedule emails, manage multiple sender accounts, automatically track replies, and run email outreach with unsubscribe compliance controls.

## Features

- **Multi-Account Support:** Add and manage multiple Gmail accounts via the UI.
- **Email Scheduling:** Compose and schedule emails to be sent at a future time.
- **Reply Tracking:** Automatically detects and updates the status of emails that have been replied to.
- **Suppression List Management:** Every outgoing campaign includes an unsubscribe link; unsubscribed recipients are skipped in compose and bulk scheduling.
- **Bulk Upload with Safety Checks:** CSV/XLSX upload now validates account ownership and skips suppressed recipients.
- **Interactive UI:** A clean and modern user interface for managing accounts and tasks.
- **Health Check Endpoint:** `/healthz` for deployment and uptime probes.

## How to Run Locally

1.  **Clone the repository:**
    ```bash
    git clone <your-repository-url>
    cd <repository-folder>
    ```

2.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

3.  **Set optional environment variables:**
    ```bash
    export FLASK_SECRET="replace-with-a-strong-secret"
    export APP_BASE_URL="http://127.0.0.1:5000"
    ```

4.  **Run the application:**
    ```bash
    python main.py
    ```

5.  Open your browser and go to `http://127.0.0.1:5000`.
