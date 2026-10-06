"""
TX Smart Alerts (TXSmartAlerts.com) - Daily Monitor
Scrapes txsmartbuy.gov ESBD and emails each active subscriber the new
postings that match the trades they picked on the sign-up form.
"""

import json, os, re, sys, smtplib, hashlib, time, csv, io, urllib.request, urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
from playwright.sync_api import sync_playwright

SENDER_EMAIL         = os.environ.get("SENDER_EMAIL", "")
SENDER_PASSWORD      = os.environ.get("SENDER_PASSWORD", "")
SEEN_FILE            = "seen_opportunities.json"
BASE_URL             = "https://www.txsmartbuy.gov/esbd"
MAX_PAGES            = 5
VENDOR_SHEET_CSV_URL = os.environ.get("VENDOR_SHEET_CSV_URL", "")
ADMIN_EMAIL          = os.environ.get("ADMIN_EMAIL", "")
BRAND                = "TX Smart Alerts"
SITE                 = "TXSmartAlerts.com"
PORTAL_URL           = "https://billing.stripe.com/p/login/00w7sKfEu9n40Zs6z99EI00"

# Sending: SendGrid HTTP API first (no Google password to get revoked),
# Gmail SMTP second as a fallback.
SENDGRID_API_KEY     = os.environ.get("SENDGRID_API_KEY", "")
FROM_EMAIL           = os.environ.get("FROM_EMAIL", "alerts@txsmartalerts.com")
REPLY_TO             = os.environ.get("REPLY_TO", "txsmartbuyalerts@gmail.com")

# Matched bids that could not be delivered are kept per vendor and retried on
# the next run. Anything older than this is dropped (deadlines will be close).
PENDING_MAX_DAYS     = 10


# Keywords matched against each posting's title (start-of-word, case-insensitive).
# Keys are the trade names used on the TXSmartAlerts.com sign-up form.
TRADE_KEYWORDS = {
    "general construction / new buildings": ["construction", "new building", "building addition", "facility", "facilities", "improvements", "expansion"],
    "heavy / civil (roads, bridges, utilities)": ["road", "roadway", "street", "bridge", "paving", "pavement", "highway", "culvert", "crack seal", "overlay", "sidewalk", "intersection", "excavation", "earthwork", "water line", "sewer", "utility", "utilities"],
    "specialty trade on new construction": ["electrical", "plumbing", "mechanical", "hvac", "concrete", "masonry", "steel", "framing", "drywall", "glazing"],
    "renovation / remodeling": ["renovation", "renovate", "remodel", "refresh", "alteration", "rehabilitation", "restoration", "build-out", "buildout", "finish-out"],
    "public works (water, sewer, drainage)": ["water line", "waterline", "wastewater", "sewer", "drainage", "storm sewer", "stormwater", "lift station", "storage tank", "water tank", "water treatment", "water plant", "utility", "utilities"],
    "hvac": ["hvac", "heating", "air condition", "chiller", "boiler", "ventilation", "cooling tower", "mechanical"],
    "roofing & gutters": ["roof", "gutter"],
    "electrical": ["electrical", "electric", "lighting", "generator", "wiring"],
    "plumbing": ["plumbing", "water heater", "backflow", "fixture"],
    "painting": ["paint", "coating"],
    "flooring & carpet": ["floor", "carpet", "tile"],
    "general building repair": ["building maintenance", "facility maintenance", "facilities maintenance", "building repair", "facility repair", "handyman", "door", "window", "locksmith", "lock"],
    "janitorial / custodial": ["janitorial", "custodial", "cleaning"],
    "pest control": ["pest", "termite", "rodent", "extermina"],
    "asbestos / lead abatement": ["asbestos", "abatement", "lead-based", "lead paint", "mold"],
    "landscaping / mowing / grounds": ["mowing", "landscap", "grounds", "lawn", "tree", "vegetation", "irrigation", "right of way", "right-of-way", "litter"],
    "security guards": ["security guard", "security services", "security officer", "armed", "unarmed", "guard"],
    "fire alarm & safety": ["fire alarm", "fire sprinkler", "sprinkler", "fire suppression", "fire protection", "life safety"],
    "environmental services": ["environmental", "remediation", "hazardous", "waste", "debris", "disposal", "recycling"],
    "architecture": ["architect", "a/e ", "design services"],
    "engineering": ["engineering", "engineer"],
    "land surveying": ["survey"],
    "consulting": ["consult", "advisory"],
    "management services": ["management services", "program management", "project management", "construction management", "property management"],
    "it / software / data": ["software", "it services", "network", "computer", "data", "technology", "cyber", "phone system", "telecom", "server"],
    "training & education": ["training", "education", "instructor", "curriculum"],
    "printing": ["printing", "print"],
    "real property lease / rental": ["lease", "leasing", "office space", "space rental", "real property", "space for"],
    "real estate appraisal": ["appraisal"],
    "title & escrow": ["title", "escrow", "abstract", "closing services"],
}

# Words from "What specifically do you do?" that are too common to match on.
STOP_WORDS = {
    "services", "service", "commercial", "residential", "general", "texas", "state",
    "contract", "contractor", "contractors", "company", "work", "works", "projects",
    "project", "other", "also", "with", "that", "from", "this", "have", "your", "into",
    "government", "public", "small", "business", "local", "annual", "various",
}


def trade_keywords(part):
    """Turn one sign-up selection into keywords.
    New form format:  'HVAC [NIGP 910-36]'
    Old form format:  '912 - Architectural Services'"""
    part = part.strip()
    if not part:
        return []
    name = re.sub(r"\s*\[NIGP[^\]]*\]\s*$", "", part).strip()
    if name != part or not re.match(r"^\d{3}\s*-", part):
        kws = TRADE_KEYWORDS.get(name.lower())
        if kws:
            return list(kws)
        return [w for w in re.findall(r"[a-z][a-z/-]{3,}", name.lower()) if w not in STOP_WORDS]
    desc = part.split("-", 1)[1].strip().lower()   # old '912 - Architectural Services'
    return [desc] if desc else []


def keyword_hit(kw, text):
    return re.search(r"(?<![a-z0-9])" + re.escape(kw), text) is not None


def get_page(page, page_num):
    url = "{}?page={}&status=1".format(BASE_URL, page_num)
    print("  Loading {}".format(url))
    page.goto(url, wait_until="networkidle", timeout=45000)
    page.wait_for_timeout(2000)
    rows = page.query_selector_all(".esbd-result-row")
    print("  Page {}: found {} result rows".format(page_num, len(rows)))
    opportunities = []
    for row in rows:
        title_el   = row.query_selector(".esbd-result-title")
        title      = title_el.inner_text().strip() if title_el else "N/A"
        columns    = [c.inner_text().strip() for c in row.query_selector_all(".esbd-result-column")]
        secondary  = [s.inner_text().strip() for s in row.query_selector_all(".esbd-result-body-secondary")]
        link_el    = row.query_selector("a")
        detail_url = ""
        if link_el:
            href = link_el.get_attribute("href")
            if href:
                detail_url = href if href.startswith("http") else "https://www.txsmartbuy.gov" + href
        opp_id = hashlib.md5(title.encode()).hexdigest()[:12]
        opportunities.append({"id": opp_id, "title": title, "columns": columns, "secondary": secondary, "detail_url": detail_url, "found_date": datetime.now().strftime("%Y-%m-%d")})
    return opportunities


def load_state():
    """Returns (seen_ids, pending). Reads the old format (a plain list of IDs) too."""
    if not os.path.exists(SEEN_FILE):
        return set(), {}
    with open(SEEN_FILE) as f:
        data = json.load(f)
    if isinstance(data, list):
        return set(data), {}
    return set(data.get("seen", [])), data.get("pending", {})


def save_state(seen, pending):
    with open(SEEN_FILE, "w") as f:
        json.dump({"seen": sorted(seen), "pending": pending}, f)


def prune_pending(pending):
    today = datetime.now().date()
    out = {}
    for email, opps in pending.items():
        keep = []
        for o in opps:
            try:
                age = (today - datetime.strptime(o.get("found_date", ""), "%Y-%m-%d").date()).days
            except ValueError:
                age = 0
            if age <= PENDING_MAX_DAYS:
                keep.append(o)
        if keep:
            out[email] = keep
    return out


def load_vendors():
    """Returns the list of Active vendors. Raises if the sheet can't be read."""
    if not VENDOR_SHEET_CSV_URL:
        raise RuntimeError("VENDOR_SHEET_CSV_URL secret is not set")
    raw = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(VENDOR_SHEET_CSV_URL, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
            break
        except Exception as e:
            print("  [WARN] Vendor sheet fetch attempt {} failed: {}".format(attempt + 1, e))
            if attempt < 2:
                time.sleep(10)
    if raw is None:
        raise RuntimeError("could not download the vendor sheet after 3 tries")
    reader = csv.DictReader(io.StringIO(raw))
    vendors = []
    for row in reader:
        status = (row.get("Status") or "").strip().lower()
        email  = (row.get("Email") or "").strip()
        # Only Active subscribers (in trial or paying). Pending = never finished checkout.
        if not email or status != "active":
            continue
        classes_raw = row.get("NIGP Classes") or ""
        notes_raw   = row.get("Specialization Notes") or ""
        keywords = []
        for part in classes_raw.split(";"):
            keywords.extend(trade_keywords(part))
        for word in re.findall(r"[a-z][a-z/-]+", notes_raw.lower()):
            if len(word) > 3 and word not in STOP_WORDS:
                keywords.append(word)
        keywords = list(dict.fromkeys(k for k in keywords if k))
        vendors.append({"company": row.get("Company Name", "").strip(), "contact": row.get("Contact Name", "").strip(), "email": email, "keywords": keywords})
    print("  Loaded {} active vendors.".format(len(vendors)))
    return vendors


def match_opportunities(opps, vendor):
    matched = []
    for opp in opps:
        # Match on the posting title only; the other columns are dates/IDs/status
        # text that every posting shares.
        haystack = opp["title"].lower()
        for kw in vendor["keywords"]:
            if kw and keyword_hit(kw, haystack):
                matched.append(opp)
                break
    return matched


def build_email(opps, heading, intro):
    today = datetime.now().strftime("%B %d, %Y")
    rows_html = ""
    for opp in opps:
        col_text  = " | ".join(opp["columns"]) if opp["columns"] else ""
        sec_text  = " | ".join(opp["secondary"]) if opp["secondary"] else ""
        view_link = "<a href='{}' style='color:#C8A951;font-weight:bold;text-decoration:none;'>View</a>".format(opp["detail_url"]) if opp["detail_url"] else ""
        rows_html += "<tr><td style='padding:14px 16px;border-bottom:1px solid #e8e8e8;vertical-align:top;'><div style='font-weight:bold;color:#1A1A2E;font-size:14px;margin-bottom:4px;'>{}</div><div style='color:#555;font-size:12px;margin-bottom:4px;'>{}</div><div style='color:#777;font-size:11px;margin-bottom:6px;'>{}</div><div style='font-size:11px;color:#999;'>Found: {} | {}</div></td></tr>".format(opp["title"], col_text, sec_text, opp["found_date"], view_link)
    return ("<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body style='font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;'><table width='100%' cellpadding='0' cellspacing='0' style='background:#f5f5f5;padding:30px 0;'><tr><td align='center'><table width='660' cellpadding='0' cellspacing='0' style='background:#fff;border-radius:8px;overflow:hidden;'><tr><td style='background:#0F1B2D;padding:24px 30px;'><h1 style='color:#C8A951;margin:0;font-size:20px;'>TXSmartAlerts.com - {}</h1><p style='color:#aaa;margin:6px 0 0;font-size:13px;'>{} | TXSmartAlerts.com</p></td></tr><tr><td style='padding:20px 30px 10px;'><p style='margin:0;color:#333;font-size:14px;'>{}</p></td></tr><tr><td style='padding:0 30px 20px;'><table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e8e8e8;'>{}</table></td></tr><tr><td style='padding:0 30px 24px;'><a href='https://www.txsmartbuy.gov/esbd?page=1&status=1' style='background:#C8A951;color:#0F1B2D;padding:11px 22px;border-radius:4px;text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;'>View All Opportunities</a></td></tr><tr><td style='background:#f0f0f0;padding:14px 30px;font-size:11px;color:#999;'>TXSmartAlerts.com &middot; Pearland, Texas &middot; Service-Disabled Veteran-Owned<br>Manage or cancel your subscription in your <a href='https://billing.stripe.com/p/login/00w7sKfEu9n40Zs6z99EI00' style='color:#C8A951;'>customer portal</a>, or reply STOP to unsubscribe.</td></tr></table></td></tr></table></body></html>").format(heading, today, intro, rows_html)


def html_to_text(html):
    text = re.sub(r"(?i)<br\s*/?>|</p>|</tr>|</h\d>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&middot;", "-", text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n+", "\n\n", text).strip()


def send_via_sendgrid(to, subject, html):
    payload = {
        "personalizations": [{"to": [{"email": a} for a in to]}],
        "from": {"email": FROM_EMAIL, "name": BRAND},
        "reply_to": {"email": REPLY_TO},
        "subject": subject,
        "content": [{"type": "text/plain", "value": html_to_text(html)},
                    {"type": "text/html", "value": html}],
        "tracking_settings": {"click_tracking": {"enable": False, "enable_text": False}},
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": "Bearer " + SENDGRID_API_KEY, "Content-Type": "application/json"},
        method="POST")
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                if resp.status in (200, 202):
                    return
                raise RuntimeError("SendGrid HTTP {}".format(resp.status))
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:300]
            if e.code in (401, 403) or (400 <= e.code < 500 and e.code != 429):
                raise RuntimeError("SendGrid HTTP {}: {}".format(e.code, body))   # retrying won't help
            err = "SendGrid HTTP {}: {}".format(e.code, body)
        except Exception as e:
            err = "SendGrid: {}".format(e)
        print("  [WARN] {} (attempt {})".format(err, attempt + 1))
        if attempt < 2:
            time.sleep(5 * (attempt + 1) ** 2)
    raise RuntimeError(err)


def send_via_gmail(to, subject, html):
    msg = MIMEMultipart("alternative")
    msg["Subject"]  = subject
    msg["From"]     = "{} <{}>".format(BRAND, SENDER_EMAIL)
    msg["To"]       = ", ".join(to)
    msg["Reply-To"] = REPLY_TO
    msg.attach(MIMEText(html_to_text(html), "plain"))
    msg.attach(MIMEText(html, "html"))
    err = None
    for attempt in range(2):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as server:
                server.login(SENDER_EMAIL, SENDER_PASSWORD)
                server.sendmail(SENDER_EMAIL, to, msg.as_string())
            return
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError("Gmail login rejected (app password revoked?): {}".format(e))
        except Exception as e:
            err = "Gmail: {}".format(e)
            print("  [WARN] {} (attempt {})".format(err, attempt + 1))
            if attempt < 1:
                time.sleep(10)
    raise RuntimeError(err)


def send_email(to, subject, html):
    """Tries SendGrid, then Gmail. Returns the channel used; raises if both fail."""
    if isinstance(to, str):
        to = [a.strip() for a in to.split(",") if a.strip()]
    errors = []
    if SENDGRID_API_KEY:
        try:
            send_via_sendgrid(to, subject, html)
            print("  Email sent to {} (SendGrid)".format(", ".join(to)))
            return "SendGrid"
        except Exception as e:
            errors.append(str(e))
            print("  [WARN] SendGrid failed for {}: {}".format(", ".join(to), e))
    if SENDER_EMAIL and SENDER_PASSWORD:
        try:
            send_via_gmail(to, subject, html)
            print("  Email sent to {} (Gmail fallback)".format(", ".join(to)))
            return "Gmail"
        except Exception as e:
            errors.append(str(e))
    if not errors:
        errors.append("no sending method configured")
    raise RuntimeError(" | ".join(errors))


def scrape_all():
    all_opps = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
        for page_num in range(1, MAX_PAGES + 1):
            try:
                opps = get_page(page, page_num)
            except Exception as e:
                print("  [WARN] Page {} failed: {}".format(page_num, e))
                break
            if not opps:
                print("  No results on page {}, stopping.".format(page_num))
                break
            all_opps.extend(opps)
            time.sleep(1.5)
        browser.close()
    return all_opps


def main():
    today_label = datetime.now().strftime("%b %d, %Y")
    print("\n" + "="*55)
    print("  TX Smart Alerts monitor - {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("="*55)
    problems = []          # anything here makes the run fail (red) and flags the summary
    channels = set()
    seen, pending = load_state()
    pending = prune_pending(pending)

    all_opps = scrape_all()
    if not all_opps:
        problems.append("ESBD scrape returned 0 postings (site down or page layout changed)")
    new_opps = [o for o in all_opps if o["id"] not in seen]
    print("\n  Found {} total, {} new.".format(len(all_opps), len(new_opps)))

    vendors, sheet_ok = [], True
    try:
        vendors = load_vendors()
    except Exception as e:
        sheet_ok = False
        problems.append("Vendor sheet: {}".format(e))
        print("  [ERROR] {}".format(e))

    emailed, failed = [], []
    if sheet_ok:
        active = {v["email"].lower() for v in vendors}
        pending = {k: v for k, v in pending.items() if k in active}   # drop cancelled vendors
        for vendor in vendors:
            key = vendor["email"].lower()
            queue = pending.get(key, [])
            queued_ids = {o["id"] for o in queue}
            queue += [o for o in match_opportunities(new_opps, vendor) if o["id"] not in queued_ids]
            if not queue:
                continue
            intro = "<strong>{} new opportunit{}</strong> matched your specialty. Need proposal help? Reply to this email.".format(len(queue), "ies" if len(queue) != 1 else "y")
            html  = build_email(queue, "Matched For You", intro)
            try:
                channels.add(send_email(vendor["email"], "TX Smart Alerts: New Texas Bids for You - {}".format(today_label), html))
                emailed.append("{} ({})".format(vendor["email"], len(queue)))
                pending.pop(key, None)
            except Exception as e:
                pending[key] = queue          # retried automatically next run
                failed.append("{}: {}".format(vendor["email"], e))
                print("  [ERROR] Could not email {} - kept {} bids for the next run".format(vendor["email"], len(queue)))
        # Only mark postings seen once we know who they matched.
        seen.update(o["id"] for o in new_opps)
    if failed:
        problems.append("{} vendor email(s) not delivered - bids saved and will be retried next run".format(len(failed)))

    save_state(seen, pending)   # save before the summary so nothing is lost if it fails

    if ADMIN_EMAIL:
        status = "ACTION NEEDED" if problems else "OK"
        lines = ["Status: <strong>{}</strong>".format(status),
                 "New opps found: {}".format(len(new_opps)),
                 "Active vendors: {}".format(len(vendors) if sheet_ok else "unknown (sheet not read)"),
                 "Vendors emailed: {}".format(len(emailed)),
                 "Sent via: {}".format(", ".join(sorted(channels)) or "-")]
        if emailed:
            lines.append("<br><strong>Delivered:</strong><br>" + "<br>".join(emailed))
        if failed:
            lines.append("<br><strong>Not delivered (will retry next run):</strong><br>" + "<br>".join(failed))
        if problems:
            lines.append("<br><strong>Problems:</strong><br>" + "<br>".join(problems))
        if "Gmail" in channels:
            lines.append("<br>Note: SendGrid failed or isn't set up, so the Gmail fallback was used.")
        summary = "<html><body style='font-family:Arial,sans-serif;padding:24px;'><h3>Vendor Alert Run Summary</h3><p>{}</p></body></html>".format("<br>".join(lines))
        subject = "TX Smart Alerts Run Summary - {}".format(today_label)
        if problems:
            subject = "ACTION NEEDED: " + subject
        try:
            send_email(ADMIN_EMAIL, subject, summary)
        except Exception as e:
            problems.append("Admin summary not sent: {}".format(e))

    print("="*55)
    if problems:
        print("  RUN FAILED:")
        for p in problems:
            print("   - " + p)
        print("="*55 + "\n")
        sys.exit(1)     # red run -> GitHub emails a failure notice
    print("  Run OK.\n" + "="*55 + "\n")


if __name__ == "__main__":
    main()
