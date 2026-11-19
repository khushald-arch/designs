# Zoom Webinar Attendee Report → Zoho Sheet

When a webinar ends, Zoom calls this webhook; ~30 min later (once Zoom's report is ready) the
attendee report (`/report/webinars/{uuid}/participants`) is appended to a Zoho Sheet.
A weekly sweep (Mon 06:00 by default) catches any webinar the webhook missed. No duplicates.

## 1. Zoom setup
1. marketplace.zoom.us → Develop → Build App → **Server-to-Server OAuth**.
2. Scopes: `report:read:admin` (or granular `report:read:list_webinar_participants:admin`),
   `webinar:read:admin`, `dashboard_webinars:read:admin` (for the weekly sweep). Requires a Pro+ plan with Webinar add-on.
3. Feature → **Event Subscriptions** → add `Webinar → Webinar has ended`,
   endpoint URL `https://<your-host>/zoom/webhook`. Copy the **Secret Token**.
4. Copy Account ID / Client ID / Client Secret.

## 2. Zoho Sheet setup
1. Create a sheet, tab named `Attendees`, row 1 headers exactly:
   `Webinar ID | Webinar UUID | Topic | Webinar Date | Name | Email | Join Time | Leave Time | Duration (min) | Attended`
2. Resource ID = the id in the URL `sheet.zoho.com/sheet/open/<RESOURCE_ID>`.
3. api-console.zoho.com → **Self Client** → generate code with scope
   `ZohoSheet.dataAPI.UPDATE,ZohoSheet.dataAPI.READ` → exchange for a refresh token:
   ```bash
   curl -X POST "https://accounts.zoho.com/oauth/v2/token?grant_type=authorization_code&client_id=ID&client_secret=SECRET&code=CODE"
   ```
   (Use `zoho.in` / `zoho.eu` if your account is in that data center, and set `ZOHO_DC`.)

## 3. Run / deploy
```bash
cp .env.example .env   # fill in values
pip install -r requirements.txt
set -a; . ./.env; set +a
gunicorn -w 1 -b 0.0.0.0:8080 app:app
```
Use **one worker** (scheduler + background threads). Deploy anywhere with a public HTTPS URL
(Render, Railway, Fly.io, a VM). Mount a persistent volume for `synced.json`.

Manual backfill of a specific webinar:
```bash
curl -X POST "https://<host>/sync/<webinar-uuid>?key=<ZOOM_WEBHOOK_SECRET_TOKEN>"
```
