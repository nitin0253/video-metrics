Video Delivery Metrics → Google Sheet

Fetches model 1 (Metabase card 11942) each morning, computes delivery metrics, and repopulates three tabs in the target Google Sheet.

Runs daily at 06:00 IST via GitHub Actions (and on-demand via Run workflow).

What it computes

All metrics come from model 1 only (model 2 is not used in this version).

Metric	Definition
sla_pct	Over one row per VIN (the first video on that VIN): share where TAT ≤ 6h.
p99_tat_hrs, p95_tat_hrs	99th / 95th percentile of TAT over the same one-row-per-VIN population.
fulfillment_pct	Over all video rows in the period: verified / (verified + rejected) × 100. Other verified_status values ignored.
ent_* / mid_* / resellers_* / smb_*	Same metrics sliced by customer_segment (Ent, Mid, Resellers, SMB, case-insensitive).
TAT = First_QC_Done_Time − Created_ON (hours). First_QC_Done_Time is per-VIN, so metrics use only the first video per VIN — this is non-negative by construction.
VINs with no First_QC_Done_Time are skipped from SLA/p99/p95 (still pending QC) but still count toward fulfillment.
Empty period/segment cells are written as 0.
Period buckets (by Created_ON, ISO week Mon–Sun)

Each tab holds 9 buckets per grain: 4 completed weekly + 4 completed monthly (both exclude the current partial period) + 1 MTD.

Tabs
Tab	Grain	Notes
video_vin	overall	9 rows
video_region	per region	9 × regions
video_rt	per Team_ID	9 × teams, plus sort_order (weekly=1, monthly=2, mtd=3) and sort_date (period start)

Each metric tab is cleared and fully repopulated every run.

run_log tab

Every run appends one row (created automatically if missing, never cleared): run_time (IST), status (success / failure / dry-run), rows_fetched, video_vin_rows, video_region_rows, video_rt_rows, duration_sec, error. Failures are logged too (with the error message) and still mark the GitHub Action as failed.

One-time setup
1. Google service account (already done if the sheet shows it as Editor)
Google Cloud Console → create/select a project.
APIs & Services → Library → enable Google Sheets API.
Credentials → Create Credentials → Service account → create.
Service account → Keys → Add key → JSON → download.
Share the target spreadsheet with the service account's client_email as Editor.
Also set the sheet's General access to Restricted (the service account share is enough; don't leave it open to "anyone with the link").
2. Metabase login

Uses email + password to create a session each run (/api/session). The account needs view access to card 11942.

If the account uses SSO/2FA, password login may be blocked — use a dedicated non-SSO reporting account instead.

3. GitHub secrets

Repo → Settings → Secrets and variables → Actions → New repository secret:

Secret	Value
METABASE_URL	https://metabase.spyne.ai
METABASE_USER	Metabase login email
METABASE_PASSWORD	Metabase password
GOOGLE_SA_JSON	entire contents of the downloaded service-account JSON
Local test (no sheet writes)
bash
pip install -r requirements.txt
export METABASE_URL="https://metabase.spyne.ai"
export METABASE_USER="you@spyne.ai"
export METABASE_PASSWORD="********"
python src/build_metrics.py --dry-run   # prints computed rows, writes nothing

Remove --dry-run (and set GOOGLE_SA_JSON) to actually write to the sheet.

Config

Edit constants at the top of src/build_metrics.py:

SPREADSHEET_ID — target sheet (1UdtvpQre__qVTifIYnIWH_D1IKrvV86eLDxkGNyarFM)
MODEL1_CARD_ID — 11942
SLA_THRESHOLD_SECONDS — 6 * 3600
Notes / things to verify on first run
The tab headers in the script must match the sheet's header row exactly (they were built from the headers you provided).
If a known-processed VIN shows unexpected 0s, check that Created_ON / First_QC_Done_Time come back as YYYY-MM-DD HH:MM:SS strings from the API.
Cron is UTC and can be delayed a few minutes under load; if you need a hard 06:00 IST, an Apps Script time trigger is more precise.
