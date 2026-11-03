# Video Delivery Metrics → Google Sheet

Fetches **model 1** (Metabase card `11942`) each morning, computes delivery
metrics, and repopulates three tabs in the target Google Sheet.

Runs daily at **06:00 IST** via GitHub Actions (and on-demand via *Run workflow*).

## What it computes

**Population:** videos from **Live rooftops only**: rooftop Live, active and non-test, inside a Live, active, non-test enterprise. This is the same rule as the Images cards. Card 11942 filters enterprises but not rooftops, so the job looks up the Live rooftops (`LIVE_ROOFTOPS_SQL`, through Metabase) and drops other rows. If that lookup fails, the run fails.

All metrics come from model 1 only (model 2 is not used in this version).

| Metric | Definition |
|---|---|
| `sla_pct` | Share of **delivered (verified)** videos with QC TAT ≤ **6h**. SLA total = TAT total. |
| `p99_tat_hrs`, `p95_tat_hrs` | 99th / 95th percentile of QC TAT over **delivered (verified)** videos. |
| `fulfillment_pct` | `verified / (verified + rejected + QC-pending-past-6h) × 100`. A QC-pending video within 6h of clock start is **in-flight** — excluded from both numerator and denominator. Once 6h have passed, it's a real shortfall and joins the denominator. `total_videos` / `delivered_videos` are that denominator / numerator. |
| `sla_checked`, `sla_breached`, `tat_measured`, `tat_over_sla` | Counts behind SLA / TAT for the dashboard tooltips. All four count the delivered-with-TAT population; `sla_breached` / `tat_over_sla` are the ones over 6h. On `video_rt` they count **rooftops**. Segment-prefixed copies too. |
| `ent_* / mid_* / resellers_* / smb_*` | Same metrics sliced by `customer_segment` (`Ent`, `Mid`, `Resellers`, `SMB`, case-insensitive). |

- **TAT** = QC done time (`qc_updated_on`) − clock start (`Last_Reprocessed_ON`
  when present, else `Created_ON`), in hours.
- QC-pending within 6h: in-flight, counted nowhere. QC-pending past 6h: counted
  in the fulfilment denominator and in `video_ff` as "QC pending (SLA breached)".
  Neither contributes to p99/p95 (no TAT yet).
- Empty period/segment cells are written as `0`.

## Period buckets (by `Created_ON`, ISO week Mon–Sun)

Each tab holds **9** buckets per grain: 4 completed weekly + 4 completed monthly
(both exclude the current partial period) + 1 MTD.

## Tabs

| Tab | Grain | Notes |
|---|---|---|
| `video_vin` | overall | 9 rows |
| `video_region` | per `region` | 9 × regions |
| `video_rt` | per `Team_ID` | 9 × teams, plus `sort_order` (weekly=1, monthly=2, mtd=3) and `sort_date` (period start) |
| `video_ff` | per `region` | One row per (period, region, reason): `region` is `"All"` for the pooled, all-regions scope, or one actual region name. Each row carries `count`/`pct`/`total` **and** `ent_count`/`ent_pct`/`ent_total`, `mid_*`, `resellers_*`, `smb_*` (the dashboard's segment chip picks a different column trio) - `total` is that scope's own denominator (all created videos in it), written out so the dashboard can sum `count`+`total` across whichever regions are selected and recompute an exact `pct` itself, instead of needing a precomputed row for every region combination. There is no `video_ff_region` / `video_ff_byregion` any more - both folded into this one tab; a leftover copy of either is deleted the first run after upgrade. |
| `video_dump` | per (`region`, `enterprise`) | One row per (period, region, enterprise, reason). `reason_count`/`reason_pct` are that reason's share of this one enterprise (same definition as `video_ff`, scoped to one enterprise) — the dashboard's Fulfilment Gap Reasons drill-down filters period+region+`customer_segment`+reason and lists what's left by `enterprise`. `total_videos`/`delivered_videos`/`qced_videos`/`rejected_videos` and `fulfillment_pct`/`sla_pct`/`sla_checked`/`sla_breached`/`p99_tat_hrs`/`p95_tat_hrs` are the enterprise's own metrics for that period+region (`sla_checked`/`sla_breached` are the exact counts behind `sla_pct` — delivered videos with a QC TAT, and of those, over 6h; "not delivered" for Fulfilment is `total_videos - delivered_videos`) and repeat across every reason row of that enterprise, so a Fulfilment / SLA / P99 / P95 drill-down is the same filter with reason left out (or `reason=""`, the sentinel for an enterprise with nothing to break down — zero rejections and zero breached-pending in that period+region). `customer_segment` is the enterprise's own tier, carried as an attribute (an enterprise is in exactly one segment), not a separate grouping axis. `region` is per-rooftop, so an enterprise with Live rooftops in more than one region gets one row per region it actually has videos in — there's no `region_set` slice: combining regions can sum the count columns, but P99/P95 can't be recombined from per-region percentiles, so a multi-region drill-down should stay to one region at a time. `enterprise` is model 1's own `Ent_Name` column. |

Each metric tab is **cleared and fully repopulated** every run.

### `video_vin_byregion` / `video_rt_byregion`
`video_vin` / `video_rt` pooled over every combination of regions except all of them: 5 regions, so 30 sets per period. They have the same columns plus `region_set`, the sorted region names, e.g. `AMER,EMEA`. The dashboard uses them so the region chips filter the VIN and Rooftop cards exactly.

### `run_log` tab
Every run appends one row (created automatically if missing, never cleared):
`run_time` (IST), `status` (success / failure / dry-run), `rows_fetched`,
`video_vin_rows`, `video_region_rows`, `video_rt_rows`, `video_ff_rows`,
`video_dump_rows`, `duration_sec`, `error`.
Failures are logged too (with the error message) and still mark the GitHub
Action as failed.

---

## One-time setup

### 1. Google service account (already done if the sheet shows it as Editor)
1. Google Cloud Console → create/select a project.
2. APIs & Services → Library → enable **Google Sheets API**.
3. Credentials → Create Credentials → **Service account** → create.
4. Service account → Keys → Add key → **JSON** → download.
5. Share the target spreadsheet with the service account's `client_email` as **Editor**.
   - Also set the sheet's *General access* to **Restricted** (the service
     account share is enough; don't leave it open to "anyone with the link").

### 2. Metabase login
Uses email + password to create a session each run (`/api/session`).
The account needs view access to card **11942**.
> If the account uses SSO/2FA, password login may be blocked — use a
> dedicated non-SSO reporting account instead.

### 3. GitHub secrets
Repo → Settings → Secrets and variables → Actions → **New repository secret**:

| Secret | Value |
|---|---|
| `METABASE_URL` | `https://metabase.spyne.ai` |
| `METABASE_USER` | Metabase login email |
| `METABASE_PASSWORD` | Metabase password |
| `GOOGLE_SA_JSON` | **entire contents** of the downloaded service-account JSON |

---

## Local test (no sheet writes)

```bash
pip install -r requirements.txt
export METABASE_URL="https://metabase.spyne.ai"
export METABASE_USER="you@spyne.ai"
export METABASE_PASSWORD="********"
python src/build_metrics.py --dry-run   # prints computed rows, writes nothing
```

Remove `--dry-run` (and set `GOOGLE_SA_JSON`) to actually write to the sheet.

## Config

Edit constants at the top of `src/build_metrics.py`:
- `SPREADSHEET_ID` — target sheet (`1UdtvpQre__qVTifIYnIWH_D1IKrvV86eLDxkGNyarFM`)
- `MODEL1_CARD_ID` — `11942`
- `SLA_THRESHOLD_SECONDS` — `6 * 3600`

## Notes / things to verify on first run
- The tab headers in the script must match the sheet's header row exactly
  (they were built from the headers you provided).
- If a known-processed VIN shows unexpected 0s, check that `Created_ON` /
  `First_QC_Done_Time` come back as `YYYY-MM-DD HH:MM:SS` strings from the API.
- Cron is UTC and can be delayed a few minutes under load; if you need a hard
  06:00 IST, an Apps Script time trigger is more precise.
