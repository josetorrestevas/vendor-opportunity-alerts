"""
Vendor Opportunity Alerts - Standalone Monitor
Scrapes txsmartbuy.gov and emails matched vendors based on NIGP specialty.
"""

import json, os, smtplib, hashlib, time, csv, io, urllib.request
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


def load_seen():
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE) as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(SEEN_FILE, "w") as f:
        json.dump(list(seen), f)


def load_vendors():
    if not VENDOR_SHEET_CSV_URL:
        print("  [INFO] No VENDOR_SHEET_CSV_URL set.")
        return []
    try:
        with urllib.request.urlopen(VENDOR_SHEET_CSV_URL, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:
        print("  [WARN] Could not fetch vendor sheet: {}".format(e))
        return []
    reader = csv.DictReader(io.StringIO(raw))
    vendors = []
    for row in reader:
        status = (row.get("Status") or "").strip().lower()
        email  = (row.get("Email") or "").strip()
        if not email or status == "inactive":
            continue
        classes_raw = row.get("NIGP Classes") or ""
        notes_raw   = row.get("Specialization Notes") or ""
        keywords = []
        for part in classes_raw.split(";"):
            desc = part.strip().split("-", 1)[-1].strip().lower()
            if desc:
                keywords.append(desc)
        for word in notes_raw.lower().replace(",", " ").split():
            if len(word) > 3:
                keywords.append(word)
        vendors.append({"company": row.get("Company Name", "").strip(), "contact": row.get("Contact Name", "").strip(), "email": email, "keywords": keywords})
    print("  Loaded {} active vendors.".format(len(vendors)))
    return vendors


def match_opportunities(opps, vendor):
    matched = []
    for opp in opps:
        haystack = (opp["title"] + " " + " ".join(opp["columns"]) + " " + " ".join(opp["secondary"])).lower()
        for kw in vendor["keywords"]:
            if kw and kw in haystack:
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
    return ("<!DOCTYPE html><html><head><meta charset='UTF-8'></head><body style='font-family:Arial,sans-serif;background:#f5f5f5;margin:0;padding:0;'><table width='100%' cellpadding='0' cellspacing='0' style='background:#f5f5f5;padding:30px 0;'><tr><td align='center'><table width='660' cellpadding='0' cellspacing='0' style='background:#fff;border-radius:8px;overflow:hidden;'><tr><td style='background:#0F1B2D;padding:24px 30px;'><h1 style='color:#C8A951;margin:0;font-size:20px;'>TX SmartBuy ESBD - {}</h1><p style='color:#aaa;margin:6px 0 0;font-size:13px;'>{} | Century 21 Tevas Government Leasing Division</p></td></tr><tr><td style='padding:20px 30px 10px;'><p style='margin:0;color:#333;font-size:14px;'>{}</p></td></tr><tr><td style='padding:0 30px 20px;'><table width='100%' cellpadding='0' cellspacing='0' style='border-collapse:collapse;border:1px solid #e8e8e8;'>{}</table></td></tr><tr><td style='padding:0 30px 24px;'><a href='https://www.txsmartbuy.gov/esbd?page=1&status=1' style='background:#C8A951;color:#0F1B2D;padding:11px 22px;border-radius:4px;text-decoration:none;font-weight:bold;font-size:13px;display:inline-block;'>View All Opportunities</a></td></tr><tr><td style='background:#f0f0f0;padding:14px 30px;font-size:11px;color:#999;'>Automated alert - Century 21 Tevas - Reply STOP to unsubscribe.</td></tr></table></td></tr></table></body></html>").format(heading, today, intro, rows_html)


def send_email(to, subject, html):
    if isinstance(to, str):
        to = [a.strip() for a in to.split(",") if a.strip()]
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = SENDER_EMAIL
    msg["To"]      = ", ".join(to)
    msg.attach(MIMEText(html, "html"))
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(SENDER_EMAIL, SENDER_PASSWORD)
        server.sendmail(SENDER_EMAIL, to, msg.as_string())
    print("  Email sent to {}".format(", ".join(to)))


def main():
    print("\n" + "="*55)
    print("  Vendor Opportunity Alerts - {}".format(datetime.now().strftime("%Y-%m-%d %H:%M")))
    print("="*55)
    seen = load_seen()
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
    new_opps = [o for o in all_opps if o["id"] not in seen]
    print("\n  Found {} total, {} new.".format(len(all_opps), len(new_opps)))
    matched_count = 0
    if new_opps:
        vendors = load_vendors()
        for vendor in vendors:
            matched = match_opportunities(new_opps, vendor)
            if matched:
                matched_count += 1
                intro = "<strong>{} new opportunit{}</strong> matched your specialty. Need proposal help? Reply to this email.".format(len(matched), "ies" if len(matched) != 1 else "y")
                html  = build_email(matched, "Matched For You", intro)
                try:
                    send_email(vendor["email"], "New Texas Bid Opportunities - {}".format(datetime.now().strftime("%b %d, %Y")), html)
                except Exception as e:
                    print("  [WARN] Failed to email {}: {}".format(vendor["email"], e))
    else:
        print("  No new opportunities - no vendor emails sent.")
    if ADMIN_EMAIL and new_opps:
        summary = "<html><body style='font-family:Arial,sans-serif;padding:24px;'><h3>Vendor Alert Run Summary</h3><p>New opps found: {}<br>Vendors emailed: {}</p></body></html>".format(len(new_opps), matched_count)
        try:
            send_email(ADMIN_EMAIL, "Vendor Alert Summary - {}".format(datetime.now().strftime("%b %d, %Y")), summary)
        except Exception as e:
            print("  [WARN] Admin summary failed: {}".format(e))
    if new_opps:
        for o in new_opps:
            seen.add(o["id"])
    save_seen(seen)
    print("="*55 + "\n")


if __name__ == "__main__":
    main()
