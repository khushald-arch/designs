"""
Zoom Webinar -> Zoho Sheet sync.

Flow:
  1. Zoom sends a `webinar.ended` webhook to POST /zoom/webhook.
  2. We wait REPORT_DELAY_MINUTES (Zoom reports are not ready instantly),
     then pull the attendee report: GET /report/webinars/{uuid}/participants.
  3. Rows are appended to a Zoho Sheet worksheet via the Zoho Sheet API.

A weekly fallback job (APScheduler) also sweeps the last 7 days of past
webinars so nothing is missed if a webhook is dropped. Already-synced
webinar UUIDs are stored in synced.json to avoid duplicates.
"""
import hashlib, hmac, json, logging, os, threading, time
from datetime import date, timedelta
from urllib.parse import quote

import requests
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sync")

env = os.environ.get
ZOOM_ACCOUNT_ID = env("ZOOM_ACCOUNT_ID")
ZOOM_CLIENT_ID = env("ZOOM_CLIENT_ID")
ZOOM_CLIENT_SECRET = env("ZOOM_CLIENT_SECRET")
ZOOM_WEBHOOK_SECRET = env("ZOOM_WEBHOOK_SECRET_TOKEN", "")
ZOOM_USER_ID = env("ZOOM_USER_ID", "me")  # host whose webinars are swept weekly

ZOHO_CLIENT_ID = env("ZOHO_CLIENT_ID")
ZOHO_CLIENT_SECRET = env("ZOHO_CLIENT_SECRET")
ZOHO_REFRESH_TOKEN = env("ZOHO_REFRESH_TOKEN")
ZOHO_DC = env("ZOHO_DC", "com")  # com, in, eu, com.au ...
ZOHO_SHEET_RESOURCE_ID = env("ZOHO_SHEET_RESOURCE_ID")  # id from the sheet URL
ZOHO_WORKSHEET_NAME = env("ZOHO_WORKSHEET_NAME", "Attendees")

REPORT_DELAY_MINUTES = int(env("REPORT_DELAY_MINUTES", "30"))
STATE_FILE = env("STATE_FILE", "synced.json")

COLUMNS = ["Webinar ID", "Webinar UUID", "Topic", "Webinar Date", "Name", "Email",
           "Join Time", "Leave Time", "Duration (min)", "Attended"]

app = Flask(__name__)
_lock = threading.Lock()


# ---------- state ----------
def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except FileNotFoundError:
        return set()


def mark_synced(uuid):
    with _lock:
        s = load_state(); s.add(uuid)
        with open(STATE_FILE, "w") as f:
            json.dump(sorted(s), f)


# ---------- Zoom ----------
def zoom_token():
    r = requests.post("https://zoom.us/oauth/token",
                      params={"grant_type": "account_credentials", "account_id": ZOOM_ACCOUNT_ID},
                      auth=(ZOOM_CLIENT_ID, ZOOM_CLIENT_SECRET), timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def zoom_get(path, token, params=None):
    r = requests.get(f"https://api.zoom.us/v2{path}",
                     headers={"Authorization": f"Bearer {token}"}, params=params, timeout=60)
    r.raise_for_status()
    return r.json()


def encode_uuid(uuid):
    # Zoom requires double-encoding UUIDs that start with '/' or contain '//'
    return quote(quote(uuid, safe=""), safe="") if uuid.startswith("/") or "//" in uuid else quote(uuid, safe="")


def fetch_attendees(uuid, token):
    rows, next_token = [], ""
    while True:
        data = zoom_get(f"/report/webinars/{encode_uuid(uuid)}/participants", token,
                        {"page_size": 300, "next_page_token": next_token})
        rows += data.get("participants", [])
        next_token = data.get("next_page_token")
        if not next_token:
            return rows


# ---------- Zoho ----------
def zoho_token():
    r = requests.post(f"https://accounts.zoho.{ZOHO_DC}/oauth/v2/token", params={
        "refresh_token": ZOHO_REFRESH_TOKEN, "client_id": ZOHO_CLIENT_ID,
        "client_secret": ZOHO_CLIENT_SECRET, "grant_type": "refresh_token"}, timeout=30)
    r.raise_for_status()
    return r.json()["access_token"]


def zoho_append(records):
    """Appends records (list of dicts keyed by COLUMNS). Worksheet row 1 must hold COLUMNS as headers."""
    token = zoho_token()
    url = f"https://sheet.zoho.{ZOHO_DC}/api/v2/{ZOHO_SHEET_RESOURCE_ID}"
    for i in range(0, len(records), 500):
        r = requests.post(url, headers={"Authorization": f"Zoho-oauthtoken {token}"}, data={
            "method": "worksheet.records.add",
            "worksheet_name": ZOHO_WORKSHEET_NAME,
            "header_row": 1,
            "json_data": json.dumps(records[i:i + 500]),
        }, timeout=60)
        r.raise_for_status()
        body = r.json()
        if body.get("status") != "success":
            raise RuntimeError(f"Zoho Sheet error: {body}")


# ---------- sync ----------
def sync_webinar(uuid, webinar_id=None, topic="", start_time=""):
    if uuid in load_state():
        log.info("Webinar %s already synced, skipping", uuid); return 0
    token = zoom_token()
    if not topic or not webinar_id:
        meta = zoom_get(f"/past_webinars/{encode_uuid(uuid)}", token)
        topic, webinar_id, start_time = meta.get("topic", ""), meta.get("id", webinar_id), meta.get("start_time", start_time)
    attendees = fetch_attendees(uuid, token)
    records = [{
        "Webinar ID": str(webinar_id), "Webinar UUID": uuid, "Topic": topic,
        "Webinar Date": (start_time or "")[:10],
        "Name": p.get("name", ""), "Email": p.get("user_email", ""),
        "Join Time": p.get("join_time", ""), "Leave Time": p.get("leave_time", ""),
        "Duration (min)": round(p.get("duration", 0) / 60, 1),
        "Attended": "Yes",
    } for p in attendees]
    if records:
        zoho_append(records)
    mark_synced(uuid)
    log.info("Synced %d attendee rows for webinar %s (%s)", len(records), webinar_id, topic)
    return len(records)


def delayed_sync(*args):
    def run():
        time.sleep(REPORT_DELAY_MINUTES * 60)
        for attempt in range(5):
            try:
                sync_webinar(*args); return
            except Exception as e:
                log.warning("Sync attempt %d failed: %s", attempt + 1, e)
                time.sleep(600)
        log.error("Giving up on webinar %s; weekly sweep will retry", args[0])
    threading.Thread(target=run, daemon=True).start()


def weekly_sweep():
    """Fallback: pull every webinar that ended in the last 7 days and sync any missed ones."""
    try:
        token = zoom_token()
        frm, to = (date.today() - timedelta(days=7)).isoformat(), date.today().isoformat()
        data = zoom_get("/metrics/webinars", token,
                        {"type": "past", "from": frm, "to": to, "page_size": 300})
        for w in data.get("webinars", []):
            try:
                sync_webinar(w["uuid"], w.get("id"), w.get("topic", ""), w.get("start_time", ""))
            except Exception as e:
                log.error("Weekly sweep failed for %s: %s", w.get("uuid"), e)
    except Exception as e:
        log.error("Weekly sweep error: %s", e)


# ---------- routes ----------
def verify_zoom(req):
    ts = req.headers.get("x-zm-request-timestamp", "")
    msg = f"v0:{ts}:{req.get_data(as_text=True)}"
    expected = "v0=" + hmac.new(ZOOM_WEBHOOK_SECRET.encode(), msg.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, req.headers.get("x-zm-signature", ""))


@app.post("/zoom/webhook")
def zoom_webhook():
    body = request.get_json(force=True)
    event = body.get("event")

    if event == "endpoint.url_validation":  # Zoom's one-time endpoint check
        plain = body["payload"]["plainToken"]
        enc = hmac.new(ZOOM_WEBHOOK_SECRET.encode(), plain.encode(), hashlib.sha256).hexdigest()
        return jsonify(plainToken=plain, encryptedToken=enc)

    if not verify_zoom(request):
        return "invalid signature", 401

    if event == "webinar.ended":
        obj = body["payload"]["object"]
        log.info("Webinar ended: %s (%s) — syncing in %d min", obj.get("topic"), obj.get("id"), REPORT_DELAY_MINUTES)
        delayed_sync(obj["uuid"], obj.get("id"), obj.get("topic", ""), obj.get("start_time", ""))
    return "", 200


@app.post("/sync/<path:uuid>")
def manual_sync(uuid):
    """Manual trigger: curl -X POST https://host/sync/<webinar-uuid>?key=..."""
    if request.args.get("key") != ZOOM_WEBHOOK_SECRET:
        return "forbidden", 403
    return jsonify(rows=sync_webinar(uuid))


@app.get("/health")
def health():
    return "ok"


scheduler = BackgroundScheduler(timezone=env("TZ", "UTC"))
scheduler.add_job(weekly_sweep, "cron", day_of_week=env("SWEEP_DAY", "mon"), hour=int(env("SWEEP_HOUR", "6")))
scheduler.start()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "8080")))
