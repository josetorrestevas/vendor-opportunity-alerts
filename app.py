"""
Vendor Opportunity Alerts — Backend Server
Runs on Railway.app (free tier).
Handles:
  - POST /signup       : receives landing page form submission
  - POST /webhook      : receives Stripe payment confirmation
  - GET  /health       : health check for Railway
"""

import os
import json
import hmac
import hashlib
import smtplib
import gspread
import stripe
from flask import Flask, request, jsonify
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from google.oauth2.service_account import Credentials

app = Flask(__name__)

# ── Environment variables (set in Railway dashboard) ─────────────────────────
STRIPE_SECRET_KEY        = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET    = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_MONTHLY_PRICE_ID  = os.environ.get("STRIPE_MONTHLY_PRICE_ID", "")
STRIPE_ANNUAL_PRICE_ID   = os.environ.get("STRIPE_ANNUAL_PRICE_ID", "")
SENDER_EMAIL             = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD          = os.environ.get("SENDER_PASSWORD", "")
ADMIN_EMAIL              = os.environ.get("ADMIN_EMAIL", "jose@josetorres.realtor")
GOOGLE_SHEET_ID          = os.environ.get("GOOGLE_SHEET_ID", "")
GOOGLE_SERVICE_ACCOUNT   = os.environ.get("GOOGLE_SERVICE_ACCOUNT", "")  # JSON string
# ─────────────────────────────────────────────────────────────────────────────

stripe.api_key = STRIPE_SECRET_KEY

# In-memory store for pending vendors (company, email, nigpClasses, notes)
# keyed by Stripe customer_id, written at checkout session creation,
# read when webhook confirms payment.
pending_vendors = {}


def get_sheet():
    creds_dict = json.loads(GOOGLE_SERVICE_ACCOUNT)
    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(GOOGLE_SHEET_ID).sheet1
    return sheet


STATUS_COL  = 9   # column I
SESSION_COL = 10  # column J


def _vendor_row(vendor, status, session_id=""):
    return [
        vendor.get("company", ""),
        vendor.get("contact", ""),
        vendor.get("email", ""),
        vendor.get("phone", ""),
        vendor.get("nigpClasses", ""),
        vendor.get("notes", ""),
        vendor.get("plan", ""),
        datetime.now().strftime("%Y-%m-%d %H:%M"),
        status,
        session_id,
    ]


def add_pending_lead(vendor, session_id):
    """Record everyone who submits the form, before they pay."""
    get_sheet().append_row(_vendor_row(vendor, "Pending", session_id))


def add_vendor_to_sheet(vendor, session_id=""):
    """Mark the pending row Active; append a new row if none is found."""
    sheet = get_sheet()
    if session_id:
        ids = sheet.col_values(SESSION_COL)
        if session_id in ids:
            row = ids.index(session_id) + 1
            sheet.update_cell(row, STATUS_COL, "Active")
            return
    sheet.append_row(_vendor_row(vendor, "Active", session_id))


def send_email(to, subject, html_body):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = to
    msg.attach(MIMEText(html_body, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, [to], msg.as_string())


def send_welcome_email(vendor):
    plan_label = "Annual ($240/year)" if "annual" in vendor.get("plan", "").lower() else "Monthly ($25/month)"
    html = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f5f5f5;padding:30px 0;">
<tr><td align="center">
<table width="620" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
<tr><td style="background:#0F1B2D;padding:28px 32px;">
<h1 style="color:#C8A951;margin:0;font-size:22px;">Welcome to Vendor Opportunity Alerts</h1>
<p style="color:#aaa;margin:6px 0 0;font-size:13px;">Century 21 Tevas Government Contracting Division</p>
</td></tr>
<tr><td style="padding:28px 32px;">
<p style="color:#333;font-size:15px;">Hi {contact},</p>
<p style="color:#333;font-size:14px;">
You're officially on the list. Every weekday morning, we scan the Texas Electronic
State Business Daily (ESBD) for newly Posted solicitations and match them against
your registered specialty. When something fits — it lands in your inbox the same morning.
</p>
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f8f6f0;border-radius:6px;padding:18px;margin:20px 0;">
<tr><td>
<p style="margin:0 0 6px;font-size:13px;color:#777;text-transform:uppercase;letter-spacing:0.06em;">Your account</p>
<p style="margin:0 0 4px;font-size:14px;color:#333;"><strong>Company:</strong> {company}</p>
<p style="margin:0 0 4px;font-size:14px;color:#333;"><strong>Plan:</strong> {plan}</p>
<p style="margin:0 0 4px;font-size:14px;color:#333;"><strong>Trial ends:</strong> 7 days from today — no charge until then</p>
<p style="margin:0;font-size:14px;color:#333;"><strong>Specialties:</strong> {classes}</p>
</td></tr>
</table>
<p style="color:#333;font-size:14px;">
<strong>Need help with a proposal?</strong> Reply to this email and our Government
Leasing Division can assist — from RFP breakdown to a complete submission package.
</p>
<p style="margin:28px 0 0;">
<a href="https://www.txsmartbuy.gov/esbd?page=1&status=1"
   style="background:#C8A951;color:#0F1B2D;padding:12px 24px;border-radius:4px;
          text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;">
View Current Posted Opportunities
</a>
</p>
</td></tr>
<tr><td style="background:#f0f0f0;padding:16px 32px;font-size:11px;color:#999;border-top:1px solid #e0e0e0;">
Century 21 Tevas · Pearland, Texas · SDVOSB · TREC #9012066<br>
To cancel your subscription, reply to this email or manage it via your Stripe customer portal.
</td></tr>
</table>
</td></tr>
</table>
</body></html>
""".format(
        contact=vendor.get("contact") or vendor.get("company") or "there",
        company=vendor.get("company", ""),
        plan=plan_label,
        classes=vendor.get("nigpClasses", "").replace(";", ", "),
    )
    send_email(vendor["email"], "You're on the list — Vendor Opportunity Alerts", html)


def send_admin_notification(vendor):
    plan_label = "Annual ($240/year)" if "annual" in vendor.get("plan", "").lower() else "Monthly ($25/month)"
    html = """
<!DOCTYPE html>
<html><head><meta charset="UTF-8"></head>
<body style="font-family:Arial,sans-serif;padding:24px;background:#f5f5f5;">
<table width="580" cellpadding="0" cellspacing="0" style="background:#fff;border-radius:8px;padding:28px;box-shadow:0 2px 8px rgba(0,0,0,0.1);">
<tr><td>
<h2 style="color:#0F1B2D;margin:0 0 16px;">New Vendor Signup 🎉</h2>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>Company:</strong> {company}</p>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>Contact:</strong> {contact}</p>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>Email:</strong> {email}</p>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>Phone:</strong> {phone}</p>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>Plan:</strong> {plan}</p>
<p style="color:#333;font-size:14px;margin:0 0 8px;"><strong>NIGP Classes:</strong> {classes}</p>
<p style="color:#333;font-size:14px;margin:0 0 16px;"><strong>Notes:</strong> {notes}</p>
<p style="color:#777;font-size:12px;margin:0;">Vendor has been automatically added to the Google Sheet as Active.</p>
</td></tr>
</table>
</body></html>
""".format(
        company=vendor.get("company", ""),
        contact=vendor.get("contact", ""),
        email=vendor.get("email", ""),
        phone=vendor.get("phone", ""),
        plan=plan_label,
        classes=vendor.get("nigpClasses", "").replace(";", ", "),
        notes=vendor.get("notes", ""),
    )
    send_email(
        ADMIN_EMAIL,
        "New Vendor Signup: {} — {}".format(vendor.get("company", ""), plan_label),
        html,
    )


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok", "time": datetime.now().isoformat()})


@app.route("/create-checkout-session", methods=["POST"])
def create_checkout_session():
    """
    Called by the landing page when a vendor clicks Subscribe.
    Creates a Stripe Checkout Session with a 7-day trial, returns the URL.
    """
    data = request.get_json()

    company    = data.get("companyName", "").strip()
    contact    = data.get("contactName", "").strip()
    email      = data.get("email", "").strip()
    phone      = data.get("phone", "").strip()
    classes    = data.get("nigpClasses", "").strip()
    notes      = data.get("notes", "").strip()
    plan       = data.get("plan", "monthly").lower()

    if not email or not company:
        return jsonify({"error": "Missing required fields"}), 400

    price_id = STRIPE_ANNUAL_PRICE_ID if plan == "annual" else STRIPE_MONTHLY_PRICE_ID

    session = stripe.checkout.Session.create(
        payment_method_types=["card"],
        mode="subscription",
        customer_email=email,
        subscription_data={
            "trial_period_days": 7,
            "metadata": {
                "company":    company,
                "contact":    contact,
                "phone":      phone,
                "nigpClasses": classes,
                "notes":      notes,
                "plan":       plan,
            },
        },
        line_items=[{"price": price_id, "quantity": 1}],
        success_url="https://josetorrestevas.github.io/vendor-opportunity-alerts/success.html",
        cancel_url="https://josetorrestevas.github.io/vendor-opportunity-alerts/",
    )

    # Store vendor info keyed by session id until webhook confirms
    pending_vendors[session.id] = {
        "company":    company,
        "contact":    contact,
        "email":      email,
        "phone":      phone,
        "nigpClasses": classes,
        "notes":      notes,
        "plan":       plan,
    }

    try:
        add_pending_lead(pending_vendors[session.id], session.id)
    except Exception as e:
        print("Could not log pending lead: {}".format(e))

    return jsonify({"url": session.url})


@app.route("/webhook", methods=["POST"])
def stripe_webhook():
    """
    Stripe calls this when a payment event occurs.
    We listen for checkout.session.completed to activate the vendor.
    """
    payload    = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        return jsonify({"error": str(e)}), 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        session_id = session.get("id")
        vendor = pending_vendors.pop(session_id, None)

        if not vendor:
            # Fallback: reconstruct from subscription metadata
            sub_id = session.get("subscription")
            if sub_id:
                sub = stripe.Subscription.retrieve(sub_id)
                meta = sub.get("metadata", {})
                vendor = {
                    "company":    meta.get("company", ""),
                    "contact":    meta.get("contact", ""),
                    "email":      session.get("customer_email", ""),
                    "phone":      meta.get("phone", ""),
                    "nigpClasses": meta.get("nigpClasses", ""),
                    "notes":      meta.get("notes", ""),
                    "plan":       meta.get("plan", "monthly"),
                }

        if vendor and vendor.get("email"):
            try:
                add_vendor_to_sheet(vendor, session_id)
                send_welcome_email(vendor)
                send_admin_notification(vendor)
                print("Activated vendor: {}".format(vendor.get("email")))
            except Exception as e:
                print("Error activating vendor: {}".format(e))

    return jsonify({"status": "ok"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
