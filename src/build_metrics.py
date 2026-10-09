#!/usr/bin/env python3
"""
Build video delivery metrics from Metabase model 1 and populate a Google Sheet.

Tabs populated (clear + full repopulate each run):
  - video_vin     : overall, one row per period bucket
  - video_region  : per region  x period bucket
  - video_rt      : per team_id x period bucket (+ sort_order, sort_date)

Metric definitions (all from model 1, card 11942; one row per ACTIVE video):
  QC  sla / p99 / p95   -> TAT = qc_updated_on (qc_done time) - Created_ON.
                           Rows without a qc_done time are skipped.
  tech sla / p99 / p95  -> TAT = Processing_Done_Time - Created_ON.
                           Rows without a processing done time are skipped.
  SLA met               -> TAT <= 6h (21600s), for both QC and tech.
  fulfillment_pct       -> verified / (verified + rejected + QC-pending) * 100
                           over all rows; QC-pending = no qc_done time yet.
                           Other verified_status values are ignored.
  Segments              -> Ent / Mid / Resellers / SMB, matched case-insensitively
                           on customer_segment.
  Period bucketing      -> by the active video's Created_ON, ISO week (Mon-Sun).
                           4 completed weekly + 4 monthly + 1 MTD = 9 buckets.
                           Empty period/segment cell -> 0.
"""

import os
import sys
import json
import argparse
from datetime import datetime, date, timedelta
from collections import defaultdict

import requests
from dateutil.relativedelta import relativedelta
from zoneinfo import ZoneInfo

# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------

SPREADSHEET_ID = "16vFElbOV8Awd63R6-WlsNdAScWNE18xGUPFkzE0GN7c"
MODEL1_CARD_ID = 11942

SLA_THRESHOLD_SECONDS = 6 * 3600  # fixed 6h SLA, QC and tech TAT

IST = ZoneInfo("Asia/Kolkata")

# customer_segment value -> column prefix
SEGMENTS = {
    "ent": "ent",
    "mid": "mid",
    "resellers": "resellers",
    "smb": "smb",
}

# Column order per tab (must match the sheet headers exactly).
# Per scope: QC sla/p99/p95, then tech sla/p99/p95.
TAT_METRICS = ["sla_pct", "p99_tat_hrs", "p95_tat_hrs",
               "tech_sla_pct", "tech_p99_tat_hrs", "tech_p95_tat_hrs"]
SEG_PREFIXES = ("ent", "mid", "resellers", "smb")

METRIC_HEADERS = (
    TAT_METRICS
    + [f"{seg}_{m}" for seg in SEG_PREFIXES for m in TAT_METRICS]
    + ["fulfillment_pct"]
    + [f"{seg}_fulfillment_pct" for seg in SEG_PREFIXES]
)

VIN_HEADERS = (["period_type", "period", "total_videos", "delivered_videos"]
               + METRIC_HEADERS + ["last_updated"])

REGION_HEADERS = (["period_type", "period", "region", "total_videos", "delivered_videos"]
                  + METRIC_HEADERS + ["last_updated"])

RT_HEADERS = (["sort_order", "sort_date", "period_type", "period",
               "total_videos", "delivered_videos"]
              + METRIC_HEADERS + ["last_updated"])

LOG_TAB = "run_log"
LOG_HEADERS = [
    "run_time", "status", "rows_fetched",
    "video_vin_rows", "video_region_rows", "video_rt_rows", "video_ff_rows",
    "duration_sec", "error",
]

# --- video_ff (failure / rejection reasons) ---
FF_HEADERS = ["period_type", "period", "reason", "count", "pct", "last_updated"]
FF_TOP_N = 3
FF_NO_REASON = "(no reason recorded)"

# These three raw reasons are merged into one bucket (matched case-insensitively,
# trimmed). Extend this set if more image-missing variants appear.
FF_MERGE_LABEL = "Interior and Exterior Images not available"
FF_MERGE_SOURCES = {
    "interior image missing",
    "exterior images missing",
    "exterior image missing",
    "interior and exterior images not available",
}

# ----------------------------------------------------------------------------
# Metabase
# ----------------------------------------------------------------------------

def metabase_session(base_url, user, password):
    """Log in with email/password and return a session token."""
    resp = requests.post(
        f"{base_url}/api/session",
        json={"username": user, "password": password},
        timeout=60,
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Metabase login failed ({resp.status_code}): {resp.text[:300]}"
        )
    return resp.json()["id"]


def fetch_card_rows(base_url, session_id, card_id, max_attempts=4):
    """
    Run a saved card and return list[dict] keyed by column display name.

    The card is heavy (~80k rows) and a gateway in front of Metabase can return
    a 504 Gateway Time-out when the DB is momentarily busy. Retry with backoff so
    a transient slow window doesn't fail the whole run.
    """
    import time as _time

    last_err = None
    for attempt in range(1, max_attempts + 1):
        try:
            resp = requests.post(
                f"{base_url}/api/card/{card_id}/query/json",
                headers={"X-Metabase-Session": session_id},
                timeout=600,
            )
        except requests.exceptions.RequestException as e:
            last_err = f"request error: {e}"
        else:
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except ValueError:
                    data = None
                if isinstance(data, list):
                    return data
                # Metabase can return 200 with an error object instead of rows
                # (e.g. the query timed out or failed in the DB) -> retry.
                if isinstance(data, dict):
                    detail = data.get("error") or data.get("message") or data.get("status")
                else:
                    detail = resp.text[:200]
                last_err = f"200 but no rows returned ({str(detail)[:300]})"
            else:
                last_err = f"{resp.status_code}: {resp.text[:200]}"
            # 502/503/504 are transient gateway/DB-busy errors -> retry.
            if resp.status_code not in (200, 429, 500, 502, 503, 504):
                # non-transient (e.g. 401/403/404) -> fail fast, no point retrying
                raise RuntimeError(f"Card {card_id} query failed ({last_err})")

        if attempt < max_attempts:
            wait = 30 * attempt  # 30s, 60s, 90s
            print(f"  fetch attempt {attempt} failed ({last_err}); "
                  f"retrying in {wait}s ...")
            _time.sleep(wait)

    raise RuntimeError(
        f"Card {card_id} query failed after {max_attempts} attempts ({last_err})"
    )


# ----------------------------------------------------------------------------
# Helpers: parsing, percentiles, period buckets
# ----------------------------------------------------------------------------

def parse_dt(value):
    """Parse 'YYYY-MM-DD HH:MM:SS' (or ISO) into naive datetime, else None."""
    if value is None:
        return None
    s = str(value).strip()
    if not s or s.startswith("1970-01-01"):
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(s[:19], fmt)
        except ValueError:
            continue
    # last resort: ISO with tz
    try:
        return datetime.fromisoformat(s).replace(tzinfo=None)
    except ValueError:
        return None


def percentile(sorted_vals, q):
    """Linear-interpolation percentile (q in 0..1) on a pre-sorted list."""
    if not sorted_vals:
        return 0
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    idx = q * (len(sorted_vals) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = idx - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def iso_week_bounds(d):
    """Return (monday, sunday) date bounds of the ISO week containing date d."""
    monday = d - timedelta(days=d.weekday())
    sunday = monday + timedelta(days=6)
    return monday, sunday


def build_periods(today_ist):
    """
    Build the 9 period buckets relative to today (IST date).

    Returns list of dicts:
      { key, period_type, period_label, sort_order, sort_date,
        start (datetime, inclusive), end (datetime, exclusive) }
    """
    periods = []
    today = today_ist

    # ---- 4 completed weekly buckets (exclude current partial week) ----
    # W-1 = most recent completed week, W-2 = the week before, etc.
    this_monday = today - timedelta(days=today.weekday())
    for i in range(1, 5):
        wk_monday = this_monday - timedelta(days=7 * i)
        wk_sunday = wk_monday + timedelta(days=6)
        periods.append({
            "period_type": "Week",
            "period_label": f"W-{i}",
            "sort_order": 1,
            "sort_date": wk_monday.isoformat(),
            "start": datetime.combine(wk_monday, datetime.min.time()),
            "end": datetime.combine(wk_sunday + timedelta(days=1), datetime.min.time()),
        })

    # ---- MTD (1st of current month .. today inclusive) ----
    # Placed between weeks and months to match the sheet layout.
    first_of_this_month = today.replace(day=1)
    mtd_start = first_of_this_month
    mtd_end = today + timedelta(days=1)  # include today fully
    periods.append({
        "period_type": "Month",
        "period_label": "MTD",
        "sort_order": 2,
        "sort_date": mtd_start.isoformat(),
        "start": datetime.combine(mtd_start, datetime.min.time()),
        "end": datetime.combine(mtd_end, datetime.min.time()),
    })

    # ---- 4 completed monthly buckets (exclude current partial month) ----
    # M-1 = most recent completed month, M-2 = the month before, etc.
    for i in range(1, 5):
        m_start = first_of_this_month - relativedelta(months=i)
        m_end = m_start + relativedelta(months=1)  # exclusive
        periods.append({
            "period_type": "Month",
            "period_label": f"M-{i}",
            "sort_order": 3,
            "sort_date": m_start.isoformat(),
            "start": datetime.combine(m_start, datetime.min.time()),
            "end": datetime.combine(m_end, datetime.min.time()),
        })

    return periods


# ----------------------------------------------------------------------------
# Core aggregation
# ----------------------------------------------------------------------------

def seg_key(row):
    seg = str(row.get("customer_segment") or "").strip().lower()
    return seg if seg in SEGMENTS else None


METRIC_KEYS = METRIC_HEADERS


def _sla_p99_p95(tats):
    """tats: TAT seconds. Return (sla, p99, p95) or (None, None, None) if empty."""
    tats = sorted(tats)
    if not tats:
        return None, None, None
    sla = sum(1 for t in tats if t <= SLA_THRESHOLD_SECONDS) / len(tats) * 100
    hrs = [t / 3600.0 for t in tats]
    return round(sla, 2), round(percentile(hrs, 0.99), 2), round(percentile(hrs, 0.95), 2)


def _fulfillment(rows):
    """
    verified / (verified + rejected + QC-pending) * 100, or None if the
    denominator is empty. QC-pending = no qc_done time yet.
    """
    delivered = denom = 0
    for r in rows:
        vs = str(r.get("verified_status") or "").strip().lower()
        if vs == "verified":
            delivered += 1
            denom += 1
        elif vs == "rejected" or r.get("qc_pending"):
            denom += 1
    if denom == 0:
        return None
    return round(delivered / denom * 100, 2)


def _tat_block(rows, prefix=""):
    """QC + tech sla/p99/p95 for these rows, keyed with an optional prefix."""
    out = {}
    qc = _sla_p99_p95([r["qc_tat"] for r in rows if r["qc_tat"] is not None])
    tech = _sla_p99_p95([r["tech_tat"] for r in rows if r["tech_tat"] is not None])
    for name, val in zip(TAT_METRICS, qc + tech):
        out[prefix + name] = val
    return out


def compute_group_metrics_raw(rows):
    """
    Compute all metrics for one group of active-video rows. Values are None
    where the group has no data for that metric ('no data' vs 0).
    """
    out = _tat_block(rows)
    out["fulfillment_pct"] = _fulfillment(rows)
    for seg in SEGMENTS.values():
        seg_rows = [r for r in rows if r["seg"] == seg]
        out.update(_tat_block(seg_rows, f"{seg}_"))
        out[f"{seg}_fulfillment_pct"] = _fulfillment(seg_rows)
    return out


def compute_group_metrics(rows):
    """Pooled metrics with None->0 for output (used by vin and region tabs)."""
    raw = compute_group_metrics_raw(rows)
    return {k: (0 if v is None else v) for k, v in raw.items()}


def compute_macro_avg_metrics(rows):
    """
    Rooftop macro-average (used by video_rt): compute each metric PER rooftop
    (team_id), then average across rooftops that have data for that metric.
    Rooftops with no data for a given metric are skipped from its average.
    """
    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append(r)

    per_team = [compute_group_metrics_raw(team_rows) for team_rows in by_team.values()]

    out = {}
    for key in METRIC_KEYS:
        vals = [t[key] for t in per_team if t.get(key) is not None]
        out[key] = round(sum(vals) / len(vals), 2) if vals else 0
    return out


def prepare_rows(raw_rows):
    """
    Normalize raw model-1 rows (one per active video) into a compact form.
    Each row carries: created, seg, region, team_id, team_name,
    verified_status, vin, rejected_reason, video_id, crm_status, and
      qc_tat   = qc_updated_on - Created_ON          (seconds, or None)
      tech_tat = Processing_Done_Time - Created_ON   (seconds, or None)
    Negative or missing TATs are None, so the row is skipped for that metric.
    """
    def getv(row, *names):
        """Return the first present, non-empty value among candidate key names."""
        for n in names:
            if n in row and row[n] not in (None, ""):
                return row[n]
        return None

    def tat(end, start):
        if end is None or start is None:
            return None
        secs = (end - start).total_seconds()
        return secs if secs >= 0 else None

    all_rows = []
    for r in raw_rows:
        # API may return display names ('Created On'); tolerate both forms.
        created = parse_dt(getv(r, "Created On", "Created_ON", "created_on"))
        qc_done = parse_dt(getv(r, "qc_updated_on", "Qc Updated On", "QC Updated On"))
        proc_done = parse_dt(getv(r, "Processing_Done_Time", "Processing Done Time",
                                  "processing_done_time"))
        all_rows.append({
            "created": created,
            "seg": seg_key(r),
            "region": (str(getv(r, "region", "Region")).strip()
                       if getv(r, "region", "Region") is not None else "Unknown"),
            "team_id": getv(r, "Team_ID", "team_id"),
            "team_name": getv(r, "Team_Name", "team_name"),
            "verified_status": getv(r, "verified_status", "Verified_Status"),
            "vin": (str(getv(r, "VIN", "vin")).strip()
                    if getv(r, "VIN", "vin") is not None else None),
            "rejected_reason": getv(r, "rejected_reason", "Rejected_Reason", "rejection_reason"),
            "video_id": getv(r, "Video_ID", "video_id"),
            "crm_status": getv(r, "CRM_Status", "crm_status"),
            "qc_tat": tat(qc_done, created),
            "qc_pending": qc_done is None,
            "tech_tat": tat(proc_done, created),
        })
    return all_rows


def build_tab_rows(raw_rows, periods, grain, last_updated):
    """
    grain in {"vin","region","rt"}.
    Returns list of output rows (list of cell values) matching that tab's headers.
    """
    all_rows_all = prepare_rows(raw_rows)

    def in_period(dt, p):
        return dt is not None and p["start"] <= dt < p["end"]

    out_rows = []

    for p in periods:
        # rows falling in this period (by the active video's Created_ON)
        all_p = [r for r in all_rows_all if in_period(r["created"], p)]

        if grain == "region":
            groups = defaultdict(list)
            for r in all_p:
                groups[r["region"]].append(r)
            groups = dict(sorted(groups.items()))
        else:  # vin / rt -- overall per period (rt is a rooftop macro-average)
            groups = {None: all_p}

        for gkey, all_g in groups.items():
            if grain == "rt":
                m = compute_macro_avg_metrics(all_g)
            else:
                m = compute_group_metrics(all_g)

            # distinct video counts for this group/period (not deduped to VIN)
            # total_videos = distinct video_ids whose crm_status is qc_done
            total_vids = len({r["video_id"] for r in all_g
                              if r.get("video_id") not in (None, "")
                              and str(r.get("crm_status") or "").strip().lower() == "qc_done"})
            delivered_vids = len({r["video_id"] for r in all_g
                                  if r.get("video_id") not in (None, "")
                                  and str(r.get("verified_status") or "").strip().lower() == "verified"})
            vid_counts = {"total_videos": total_vids,
                          "delivered_videos": delivered_vids}

            if grain == "vin":
                row = {
                    "period_type": p["period_type"],
                    "period": p["period_label"],
                    "last_updated": last_updated,
                }
                row.update(vid_counts)
                row.update(m)
                out_rows.append([row.get(h, 0) for h in VIN_HEADERS])

            elif grain == "region":
                row = {
                    "period_type": p["period_type"],
                    "period": p["period_label"],
                    "region": gkey,
                    "last_updated": last_updated,
                }
                row.update(vid_counts)
                row.update(m)
                out_rows.append([row.get(h, 0) for h in REGION_HEADERS])

            else:  # rt
                row = {
                    "sort_order": p["sort_order"],
                    "sort_date": p["sort_date"],
                    "period_type": p["period_type"],
                    "period": p["period_label"],
                    "last_updated": last_updated,
                }
                row.update(vid_counts)
                row.update(m)
                out_rows.append([row.get(h, 0) for h in RT_HEADERS])

    return out_rows


def _canon_reason(raw):
    """
    Normalize a rejected_reason: collapse whitespace, merge image-missing
    variants, blank -> no-reason. Returns (group_key, display_label); the key
    is lowercased so case/spacing variants of one reason land in one bucket.
    """
    s = " ".join(str(raw).split()) if raw not in (None, "") else ""
    if not s:
        return FF_NO_REASON.lower(), FF_NO_REASON
    key = s.lower()
    if key in FF_MERGE_SOURCES:
        return FF_MERGE_LABEL.lower(), FF_MERGE_LABEL
    return key, s


def build_ff_rows(raw_rows, periods, last_updated):
    """
    video_ff: rejection-reason breakdown per period.
    One row per (period, reason) for the TOP FF_TOP_N reasons in that period.
    count = rejected videos for that reason; pct = count / total created videos
    in the period * 100. Reasons are merged (image-missing group) before ranking.
    """
    all_rows_all = prepare_rows(raw_rows)

    def in_period(dt, p):
        return dt is not None and p["start"] <= dt < p["end"]

    out_rows = []
    for p in periods:
        in_rows = [r for r in all_rows_all if in_period(r["created"], p)]
        total_created = len(in_rows)  # denominator = all created videos in period

        counts = defaultdict(int)
        variants = defaultdict(lambda: defaultdict(int))  # key -> display -> n
        for r in in_rows:
            if str(r.get("verified_status") or "").strip().lower() == "rejected":
                key, label = _canon_reason(r.get("rejected_reason"))
                counts[key] += 1
                variants[key][label] += 1

        # display each bucket with its most common spelling
        labelled = {max(variants[k].items(), key=lambda kv: (kv[1], kv[0]))[0]: n
                    for k, n in counts.items()}
        ranked = sorted(labelled.items(), key=lambda kv: (-kv[1], kv[0]))[:FF_TOP_N]

        for reason, cnt in ranked:
            pct = round(cnt / total_created * 100, 2) if total_created else 0
            row = {
                "period_type": p["period_type"],
                "period": p["period_label"],
                "reason": reason,
                "count": cnt,
                "pct": pct,
                "last_updated": last_updated,
            }
            out_rows.append([row.get(h, "") for h in FF_HEADERS])

    return out_rows


# ----------------------------------------------------------------------------
# Google Sheets
# ----------------------------------------------------------------------------

def open_spreadsheet(spreadsheet_id, sa_info):
    """Authorize with the service account and return an open spreadsheet handle."""
    import gspread
    from google.oauth2.service_account import Credentials

    scopes = ["https://www.googleapis.com/auth/spreadsheets"]
    creds = Credentials.from_service_account_info(sa_info, scopes=scopes)
    gc = gspread.authorize(creds)
    return gc.open_by_key(spreadsheet_id)


def write_sheet(sh, tabs):
    """tabs: dict tab_name -> (headers, rows). Clear + repopulate each."""
    import gspread

    for tab_name, (headers, rows) in tabs.items():
        try:
            ws = sh.worksheet(tab_name)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=tab_name, rows=max(len(rows) + 10, 50),
                                  cols=len(headers) + 2)
        ws.clear()
        payload = [headers] + rows
        ws.update(range_name="A1", values=payload, value_input_option="RAW")
        print(f"  wrote {len(rows)} rows to '{tab_name}'")


def append_log(sh, log_row):
    """Append one row to the run_log tab (created with headers if missing)."""
    import gspread

    try:
        ws = sh.worksheet(LOG_TAB)
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title=LOG_TAB, rows=1000, cols=len(LOG_HEADERS) + 2)
        ws.update(range_name="A1", values=[LOG_HEADERS], value_input_option="RAW")
    # ensure a header row exists on an empty tab
    if not ws.get_all_values():
        ws.update(range_name="A1", values=[LOG_HEADERS], value_input_option="RAW")
    ws.append_row([log_row.get(h, "") for h in LOG_HEADERS],
                  value_input_option="RAW")
    print(f"  appended run_log row (status={log_row.get('status')})")


# ----------------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch + compute, print rows, do NOT write to the sheet.")
    args = ap.parse_args()

    import time
    start = time.monotonic()

    now_ist = datetime.now(IST)
    today_ist = now_ist.date()
    run_time = now_ist.strftime("%Y-%m-%d %H:%M:%S IST")
    last_updated = run_time

    log = {
        "run_time": run_time,
        "status": "",
        "rows_fetched": "",
        "video_vin_rows": "",
        "video_region_rows": "",
        "video_rt_rows": "",
        "duration_sec": "",
        "error": "",
    }

    tabs = None
    sh = None
    sa_info = None

    try:
        base_url = os.environ["METABASE_URL"].rstrip("/")
        mb_user = os.environ["METABASE_USER"]
        mb_pass = os.environ["METABASE_PASSWORD"]

        # Open the sheet early (so we can still log if data steps fail later).
        if not args.dry_run:
            sa_info = json.loads(os.environ["GOOGLE_SA_JSON"])
            sh = open_spreadsheet(SPREADSHEET_ID, sa_info)

        print(f"[{run_time}] logging into Metabase ...")
        session_id = metabase_session(base_url, mb_user, mb_pass)

        print(f"fetching model 1 (card {MODEL1_CARD_ID}) ...")
        raw_rows = fetch_card_rows(base_url, session_id, MODEL1_CARD_ID)
        log["rows_fetched"] = len(raw_rows)
        print(f"  {len(raw_rows)} rows")

        # --- DIAGNOSTIC: show the actual column names the API returned ---
        if raw_rows:
            print("COLUMN NAMES:", list(raw_rows[0].keys()))
            print("SAMPLE ROW:", raw_rows[0])
        # ----------------------------------------------------------------

        periods = build_periods(today_ist)
        print(f"periods: {[p['period_label'] for p in periods]}")

        vin_rows = build_tab_rows(raw_rows, periods, "vin", last_updated)
        region_rows = build_tab_rows(raw_rows, periods, "region", last_updated)
        rt_rows = build_tab_rows(raw_rows, periods, "rt", last_updated)
        ff_rows = build_ff_rows(raw_rows, periods, last_updated)

        log["video_vin_rows"] = len(vin_rows)
        log["video_region_rows"] = len(region_rows)
        log["video_rt_rows"] = len(rt_rows)
        log["video_ff_rows"] = len(ff_rows)

        tabs = {
            "video_vin": (VIN_HEADERS, vin_rows),
            "video_region": (REGION_HEADERS, region_rows),
            "video_rt": (RT_HEADERS, rt_rows),
            "video_ff": (FF_HEADERS, ff_rows),
        }

        if args.dry_run:
            for name, (headers, rows) in tabs.items():
                print(f"\n=== {name} ({len(rows)} rows) ===")
                print("\t".join(headers))
                for row in rows[:15]:
                    print("\t".join(str(c) for c in row))
                if len(rows) > 15:
                    print(f"... (+{len(rows) - 15} more)")
            log["status"] = "dry-run"
            log["duration_sec"] = round(time.monotonic() - start, 1)
            print(f"\n[dry-run] no sheet writes performed. log={log}")
            return

        print("writing to Google Sheet ...")
        write_sheet(sh, tabs)

        log["status"] = "success"
        log["duration_sec"] = round(time.monotonic() - start, 1)
        print("done.")

    except Exception as exc:
        log["status"] = "failure"
        log["error"] = f"{type(exc).__name__}: {exc}"[:1000]
        log["duration_sec"] = round(time.monotonic() - start, 1)
        print(f"ERROR: {log['error']}", file=sys.stderr)
        # best-effort: still try to record the failure in the sheet
        if not args.dry_run:
            try:
                if sh is None:
                    if sa_info is None:
                        sa_info = json.loads(os.environ["GOOGLE_SA_JSON"])
                    sh = open_spreadsheet(SPREADSHEET_ID, sa_info)
                append_log(sh, log)
            except Exception as log_exc:
                print(f"(also failed to write run_log: {log_exc})", file=sys.stderr)
        raise
    else:
        # success path: append the log row
        if not args.dry_run and sh is not None:
            try:
                append_log(sh, log)
            except Exception as log_exc:
                print(f"(failed to write run_log: {log_exc})", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
