#!/usr/bin/env python3
"""
Build video delivery metrics from Metabase model 1 and populate a Google Sheet.

Tabs populated (clear + full repopulate each run):
  - video_vin           : overall, one row per period bucket
  - video_region        : per region  x period bucket
  - video_rt            : per team_id x period bucket (+ sort_order, sort_date)
  - video_ff            : rejection-reason breakdown per period, with
                          segment-prefixed count / pct columns for the
                          dashboard's segment chips.
  - video_ff_region     : video_ff sliced per region x period x reason.
  - video_ff_byregion   : video_ff sliced per region_set x period x reason,
                          same key the dashboard builds from its region chips.

Metric definitions (all from model 1, card 11942; one row per ACTIVE video, Live rooftops
only - rows from rooftops that aren't Live are dropped, see LIVE_ROOFTOPS_SQL):
  Population            -> verified + rejected + QC-pending videos (QC-pending =
                           no qc_done time yet). total_videos and fulfillment_pct use it.
  QC  sla               -> share of delivered (verified) videos with QC TAT <= 6h:
                           the same videos as QC p99 / p95, so SLA total = TAT total.
  QC  p99 / p95         -> TAT = qc_updated_on (qc_done time) - clock start, over
                           delivered (verified) videos only.
  tech sla / p99 / p95  -> TAT = Processing_Done_Time - Created_ON.
                           Rows without a processing done time are skipped.
  SLA met               -> TAT <= 6h (21600s), for both QC and tech.
  fulfillment_pct       -> verified / (verified + rejected + QC-pending) * 100
                           over all rows; QC-pending = no qc_done time yet.
                           Other verified_status values are ignored.
  Segments              -> Ent / Mid / Resellers / SMB, matched case-insensitively
                           on customer_segment.
  p99 / p95 per tab     -> video_vin: pooled over all videos in the period.
                           video_region / video_rt: per rooftop (team_id), then
                           a video-weighted average across rooftops (within the
                           region for video_region).
  Period bucketing      -> by the active video's Created_ON, ISO week (Mon-Sun).
                           7 completed daily + 4 weekly + 4 monthly + 1 MTD
                           = 16 buckets. D-1 = yesterday, D-7 = 7 days ago.
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

# Metabase database id of the ClickHouse warehouse, used for the Live-rooftop lookup below.
CLICKHOUSE_DB_ID = int(os.environ.get("METABASE_CLICKHOUSE_DB_ID", "350"))

# Population (same rule as the Images cards): only videos from Live, active, non-test
# rooftops inside Live, active, non-test enterprises. Card 11942 filters enterprises but
# not rooftops, so its rows are filtered here against this list.
LIVE_ROOFTOPS_SQL = """
SELECT t.team_id AS team_id
FROM eventila.enterprise_team_details AS t FINAL
INNER JOIN (
    SELECT enterprise_id FROM eventila.enterprise_details FINAL
    WHERE _peerdb_is_deleted = 0 AND stage = 'Live' AND is_active AND ifNull(is_test_account, 0) = 0
) AS e ON e.enterprise_id = t.enterprise_id
WHERE t._peerdb_is_deleted = 0 AND t.stage = 'Live' AND t.is_active AND ifNull(t.is_test_account, 0) = 0
"""

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

# Counts behind the QC SLA / TAT numbers, shown in the dashboard tooltips
# (same column names as the images tabs). On video_vin / video_region they count
# videos; on video_rt they count rooftops.
#   sla_checked  = in the SLA population (delivered, with a QC TAT)
#   sla_breached = of those, QC TAT > 6h
#   tat_measured = in the TAT (P95/P99) population (same rows as sla_checked)
#   tat_over_sla = of those, QC TAT > 6h
COUNT_METRICS = ["sla_checked", "sla_breached", "tat_measured", "tat_over_sla"]
COUNT_HEADERS = (COUNT_METRICS
                 + [f"{seg}_{m}" for seg in SEG_PREFIXES for m in COUNT_METRICS])
# Per-segment Total / Delivered behind fulfillment_pct, so the dashboard's fulfilment
# tooltip follows the segment chips (same names as the images tabs: ent_total_videos ...)
SEG_VID_HEADERS = [f"{seg}_{m}" for seg in SEG_PREFIXES for m in ("total_videos", "delivered_videos")]

VIN_HEADERS = (["period_type", "period", "total_videos", "delivered_videos"]
               + METRIC_HEADERS + COUNT_HEADERS + SEG_VID_HEADERS + ["last_updated"])

REGION_HEADERS = (["period_type", "period", "region", "total_videos", "delivered_videos"]
                  + METRIC_HEADERS + COUNT_HEADERS + SEG_VID_HEADERS + ["last_updated"])

RT_HEADERS = (["sort_order", "sort_date", "period_type", "period",
               "total_videos", "delivered_videos"]
              + METRIC_HEADERS + COUNT_HEADERS + SEG_VID_HEADERS + ["last_updated"])

LOG_TAB = "run_log"
LOG_HEADERS = [
    "run_time", "status", "rows_fetched",
    "video_vin_rows", "video_region_rows", "video_rt_rows", "video_ff_rows",
    "duration_sec", "error",
]

# --- video_ff (failure / rejection reasons) ---
# count / pct are the overall scope. Segment-prefixed count / pct let the
# dashboard's segment chips rescope the breakdown without recomputing: pick
# ent_count / ent_pct instead of count / pct, and the Fulfilment-failure-reasons
# card shows the Ent-only breakdown. Same denominator rule as before, scoped per
# segment (ent_pct = ent_rejected / ent_rows * 100).
FF_SEG_METRICS = ["count", "pct"]
FF_BASE_COLS = ["reason", "count", "pct"] + [f"{seg}_{m}" for seg in SEG_PREFIXES
                                              for m in FF_SEG_METRICS]
FF_HEADERS = ["period_type", "period"] + FF_BASE_COLS + ["last_updated"]
# Region slices for the region chip, same shape as video_region.
FF_REGION_HEADERS = ["period_type", "period", "region"] + FF_BASE_COLS + ["last_updated"]
# region_set slices for multi-region chip combos, same shape as video_vin_byregion.
FF_BYREGION_HEADERS = ["period_type", "period", "region_set"] + FF_BASE_COLS + ["last_updated"]
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


def fetch_live_rooftops(base_url, session_id):
    """
    Return the set of Live rooftop (team) ids.

    Uses Metabase's export endpoint (/api/dataset/json): /api/dataset caps results at
    2,000 rows, and there are already ~1,720 Live rooftops. The query must be sent
    trimmed: Metabase's ClickHouse driver fails a native query that starts with a
    newline ("Select statement did not produce a ResultSet").
    Fails the run rather than silently counting every rooftop.
    """
    query = {"database": CLICKHOUSE_DB_ID, "type": "native",
             "native": {"query": LIVE_ROOFTOPS_SQL.strip()}}
    resp = requests.post(
        f"{base_url}/api/dataset/json",
        headers={"X-Metabase-Session": session_id},
        data={"query": json.dumps(query)},
        timeout=300,
    )
    try:
        rows = resp.json()
    except ValueError:
        rows = None
    # the export endpoint returns a plain list of row objects on success (status 200 or 202)
    if resp.status_code not in (200, 202) or not isinstance(rows, list) or not rows:
        raise RuntimeError(f"Live rooftop lookup failed ({resp.status_code}): {resp.text[:2000]}")
    return {str(r["team_id"]) for r in rows if r.get("team_id")}


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

    # ---- 7 completed daily buckets (exclude today) ----
    # D-1 = yesterday, D-2 = day before, ..., D-7 = 7 days ago.
    for i in range(1, 8):
        d = today - timedelta(days=i)
        periods.append({
            "period_type": "Day",
            "period_label": f"D-{i}",
            "sort_order": 0,
            "sort_date": d.isoformat(),
            "start": datetime.combine(d, datetime.min.time()),
            "end": datetime.combine(d + timedelta(days=1), datetime.min.time()),
        })

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


def _qc_sla(rows):
    """
    QC SLA % over delivered videos (see prepare_rows): videos whose QC took <= 6h,
    out of all sla_eligible videos (= the TAT population). None if nothing is eligible.
    """
    elig = [r for r in rows if r["sla_eligible"]]
    if not elig:
        return None
    return round((len(elig) - sum(1 for r in elig if r["sla_breach"])) / len(elig) * 100, 2)


def _tat_block(rows, prefix=""):
    """
    QC + tech sla/p99/p95 for these rows, keyed with an optional prefix.
    QC sla uses the shared population; QC p99/p95 use delivered (verified)
    videos only, so the TAT population equals delivered_videos. Tech is unchanged.
    """
    out = {}
    _, qp99, qp95 = _sla_p99_p95([r["qc_tat_del"] for r in rows if r["qc_tat_del"] is not None])
    qc = (_qc_sla(rows), qp99, qp95)
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


def _is_percentile_key(key):
    return key.endswith("p99_tat_hrs") or key.endswith("p95_tat_hrs")


def rooftop_weighted_percentiles(rows):
    """
    p99 / p95 TAT (QC and tech, overall and per segment) computed PER rooftop
    (team_id), then averaged across rooftops weighted by each rooftop's number
    of videos with that TAT. Rooftops with no TAT for a metric are skipped.
    Returns {percentile_key: value (0 when no data)}.
    """
    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append(r)

    scopes = [("", None)] + [(f"{seg}_", seg) for seg in SEGMENTS.values()]
    kinds = [("", "qc_tat_del"), ("tech_", "tech_tat")]   # QC: delivered videos only

    out = {}
    for prefix, seg in scopes:
        for kind, field in kinds:
            sums = {"p99": 0.0, "p95": 0.0}
            total_n = 0
            for team_rows in by_team.values():
                tats = [r[field] for r in team_rows
                        if r[field] is not None and (seg is None or r["seg"] == seg)]
                if not tats:
                    continue
                _, p99, p95 = _sla_p99_p95(tats)
                sums["p99"] += p99 * len(tats)
                sums["p95"] += p95 * len(tats)
                total_n += len(tats)
            for q in ("p99", "p95"):
                key = f"{prefix}{kind}{q}_tat_hrs"
                out[key] = round(sums[q] / total_n, 2) if total_n else 0
    return out


def compute_macro_avg_metrics(rows):
    """
    Metrics for video_rt:
      - SLA and fulfillment: rooftop macro-average -- compute PER rooftop
        (team_id), then average across rooftops that have data for that
        metric (rooftops without data are skipped, not counted as 0).
      - p99 / p95 TAT (QC and tech, overall and per segment): per rooftop,
        then a video-weighted average across rooftops.
    """
    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append(r)

    per_team = [compute_group_metrics_raw(team_rows) for team_rows in by_team.values()]
    weighted = rooftop_weighted_percentiles(rows)

    out = {}
    for key in METRIC_KEYS:
        if _is_percentile_key(key):
            out[key] = weighted[key]
            continue
        vals = [t[key] for t in per_team if t.get(key) is not None]
        out[key] = round(sum(vals) / len(vals), 2) if vals else 0
    return out


def _count_block(rows, prefix=""):
    """
    Video counts behind QC sla / p99 / p95:
      sla_checked  = sla_eligible videos (= tat_measured)
      sla_breached = of those, QC over 6h
      tat_measured = delivered videos with a QC TAT (= delivered_videos)
      tat_over_sla = of those, QC TAT over 6h
    """
    tats = [r["qc_tat_del"] for r in rows if r["qc_tat_del"] is not None]
    return {f"{prefix}sla_checked": sum(1 for r in rows if r["sla_eligible"]),
            f"{prefix}sla_breached": sum(1 for r in rows if r["sla_breach"]),
            f"{prefix}tat_measured": len(tats),
            f"{prefix}tat_over_sla": sum(1 for x in tats if x > SLA_THRESHOLD_SECONDS)}


def compute_counts(rows):
    """Tooltip counts in videos (video_vin / video_region), overall and per segment."""
    out = _count_block(rows)
    for seg in SEGMENTS.values():
        out.update(_count_block([r for r in rows if r["seg"] == seg], f"{seg}_"))
    return out


def compute_rooftop_counts(rows):
    """
    Tooltip counts in rooftops (video_rt), overall and per segment:
    sla_checked / sla_breached  = rooftops with an SLA-eligible video / with a breach;
    tat_measured / tat_over_sla = rooftops with a delivered video's TAT / with one over 6h.
    """
    by_team = defaultdict(list)
    for r in rows:
        by_team[r["team_id"]].append(r)
    out = {}
    for prefix, seg in [("", None)] + [(f"{s}_", s) for s in SEGMENTS.values()]:
        n = {"sla_checked": 0, "sla_breached": 0, "tat_measured": 0, "tat_over_sla": 0}
        for team_rows in by_team.values():
            c = _count_block([r for r in team_rows if seg is None or r["seg"] == seg])
            for k in n:
                n[k] += 1 if c[k] else 0
        out.update({prefix + k: v for k, v in n.items()})
    return out


def prepare_rows(raw_rows, now=None):
    """
    Normalize raw model-1 rows (one per active video) into a compact form.
    Each row carries: created, seg, region, team_id, team_name,
    verified_status, vin, rejected_reason, video_id, crm_status, and
      qc_tat   = qc_updated_on - effective_created   (seconds, or None)
      tech_tat = Processing_Done_Time - effective_created (seconds, or None)
    effective_created = Last_Reprocessed_ON when present, else Created_ON —
    so a video reprocessed due to a price change has its TAT clock restart
    at the reprocess time and lands in the period bucket of its reprocess
    date. Negative or missing TATs are None, so the row is skipped for that
    metric.
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

    # "now" on the same naive IST clock as the period buckets (build_periods uses the IST date)
    now = now or datetime.now(IST).replace(tzinfo=None)

    all_rows = []
    for r in raw_rows:
        # API may return display names ('Created On'); tolerate both forms.
        created_raw = parse_dt(getv(r, "Created On", "Created_ON", "created_on"))
        last_reprocessed = parse_dt(getv(
            r, "Last_Reprocessed_ON", "Last Reprocessed ON",
            "Last_Reprocessed_On", "last_reprocessed_on",
        ))
        created = last_reprocessed or created_raw
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
        # Populations (same rule as the image cards):
        #   in_pop       = verified + rejected + QC-pending  (the fulfillment_pct denominator)
        #   qc_tat_del   = QC TAT of delivered (verified) videos only -> the P95/P99 population
        #   sla_eligible = the same videos as qc_tat_del (SLA total = TAT total)
        #   sla_breach   = of those, QC took over 6h
        row = all_rows[-1]
        vs = str(row["verified_status"] or "").strip().lower()
        row["delivered"] = vs == "verified"
        row["in_pop"] = row["delivered"] or vs == "rejected" or row["qc_pending"]
        row["qc_tat_del"] = row["qc_tat"] if row["delivered"] else None
        row["sla_eligible"] = row["qc_tat_del"] is not None
        row["sla_breach"] = row["sla_eligible"] and row["qc_tat_del"] > SLA_THRESHOLD_SECONDS
    return all_rows


def build_tab_rows(raw_rows, periods, grain, last_updated, prepared=None):
    """
    grain in {"vin","region","rt"}.
    Returns list of output rows (list of cell values) matching that tab's headers.
    prepared: already-prepared rows (prepare_rows output) to use instead of raw_rows.
    """
    all_rows_all = prepared if prepared is not None else prepare_rows(raw_rows)

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
        else:  # vin / rt -- overall per period (rt: SLA/fulfillment rooftop macro-avg)
            groups = {None: all_p}

        for gkey, all_g in groups.items():
            if grain == "rt":
                m = compute_macro_avg_metrics(all_g)
            else:
                m = compute_group_metrics(all_g)
                if grain == "region":
                    # p99/p95: per rooftop within the region, video-weighted
                    m.update(rooftop_weighted_percentiles(all_g))
            m.update(compute_rooftop_counts(all_g) if grain == "rt" else compute_counts(all_g))

            # total_videos / delivered_videos = the fulfillment_pct denominator / numerator
            # (verified + rejected + QC-pending / verified), so the tooltip's Total and
            # Delivered explain the fulfilment % and line up with the SLA / TAT counts.
            total_vids = sum(1 for r in all_g if r["in_pop"])
            delivered_vids = sum(1 for r in all_g if r["delivered"])
            vid_counts = {"total_videos": total_vids,
                          "delivered_videos": delivered_vids}
            for seg in SEG_PREFIXES:
                seg_g = [r for r in all_g if r["seg"] == seg]
                vid_counts[f"{seg}_total_videos"] = sum(1 for r in seg_g if r["in_pop"])
                vid_counts[f"{seg}_delivered_videos"] = sum(1 for r in seg_g if r["delivered"])

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


def _with_region_set(headers):
    i = headers.index("period") + 1
    return headers[:i] + ["region_set"] + headers[i:]


def build_byregion_rows(raw_rows, periods, grain, last_updated):
    """
    video_vin / video_rt pooled over every combination of regions except "all of them"
    (that is the plain tab): 5 regions -> 30 sets per period. region_set lists the sorted
    region names, e.g. 'AMER,EMEA'. The dashboard builds the same key from its region chips,
    so the VIN and Rooftop cards follow the region filter exactly (P95/P99 included).
    Returns (headers, rows).
    """
    base_headers = VIN_HEADERS if grain == "vin" else RT_HEADERS
    headers = _with_region_set(base_headers)
    pos = headers.index("region_set")
    prepared = prepare_rows(raw_rows)
    regions = sorted({r["region"] for r in prepared})
    out = []
    for mask in range(1, (1 << len(regions)) - 1):
        subset = [reg for i, reg in enumerate(regions) if mask & (1 << i)]
        key = ",".join(subset)
        rows = [r for r in prepared if r["region"] in subset]
        for row in build_tab_rows(None, periods, grain, last_updated, prepared=rows):
            out.append(row[:pos] + [key] + row[pos:])
    return headers, out


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


def _ff_scope(rows):
    """
    Count rejection reasons on one scope's rows.
    Returns (counts {key -> n}, variants {key -> {label -> n}}, denominator).
    denominator = len(rows), i.e. all created videos in that scope - same rule
    the overall video_ff has used since the top-3 cap was lifted.
    """
    counts = defaultdict(int)
    variants = defaultdict(lambda: defaultdict(int))
    for r in rows:
        if str(r.get("verified_status") or "").strip().lower() == "rejected":
            key, label = _canon_reason(r.get("rejected_reason"))
            counts[key] += 1
            variants[key][label] += 1
    return counts, variants, len(rows)


def _ff_label_for(key, scopes):
    """Pick a display label for one reason key: use the overall scope's most
    common spelling; fall back to any segment / region scope that saw it."""
    for _, variants, _ in scopes:
        v = variants.get(key)
        if v:
            return max(v.items(), key=lambda kv: (kv[1], kv[0]))[0]
    return key


def _ff_period_rows(scope_rows, p, extra_cols, last_updated, headers):
    """
    Build one block of video_ff rows for a single (period, scope).
    scope_rows: rows already filtered to the scope (overall / region / region_set).
    extra_cols: columns that locate the scope in the sheet, e.g. {'region': 'EMEA'}.
    headers: the tab's header list (FF_HEADERS / FF_REGION_HEADERS / FF_BYREGION_HEADERS).

    One row per reason seen in the overall scope OR any segment scope within
    scope_rows - so a reason that only shows up for one segment still gets a row
    (its overall count is 0, its segment count isn't). Overall count ranks the
    rows; segment pcts are each over that segment's own denominator so the chips
    don't need to recompute anything.
    """
    overall = _ff_scope(scope_rows)
    seg_scopes = {seg: _ff_scope([r for r in scope_rows if r["seg"] == seg])
                  for seg in SEG_PREFIXES}

    ov_counts, _, ov_total = overall
    all_keys = set(ov_counts)
    for seg in SEG_PREFIXES:
        all_keys |= set(seg_scopes[seg][0])

    scopes_for_label = [overall] + [seg_scopes[seg] for seg in SEG_PREFIXES]

    def rank_key(k):
        return (-ov_counts.get(k, 0), _ff_label_for(k, scopes_for_label).lower())

    out = []
    for key in sorted(all_keys, key=rank_key):
        label = _ff_label_for(key, scopes_for_label)
        cnt = ov_counts.get(key, 0)
        row = {
            "period_type": p["period_type"],
            "period": p["period_label"],
            "reason": label,
            "count": cnt,
            "pct": round(cnt / ov_total * 100, 2) if ov_total else 0,
            "last_updated": last_updated,
        }
        row.update(extra_cols)
        for seg in SEG_PREFIXES:
            seg_cnt, _, seg_total = seg_scopes[seg]
            c = seg_cnt.get(key, 0)
            row[f"{seg}_count"] = c
            row[f"{seg}_pct"] = round(c / seg_total * 100, 2) if seg_total else 0
        out.append([row.get(h, 0) for h in headers])
    return out


def build_ff_rows(raw_rows, periods, last_updated, prepared=None):
    """
    video_ff: rejection-reason breakdown per period.
    One row per (period, reason) across the overall scope + every segment it
    appears in, ranked by overall count desc. Segment-prefixed columns let the
    dashboard rescope the card without recomputing (ent_pct is Ent-only).
    Reasons are merged (image-missing group) before counting.
    """
    all_rows_all = prepared if prepared is not None else prepare_rows(raw_rows)

    def in_period(dt, p):
        return dt is not None and p["start"] <= dt < p["end"]

    out_rows = []
    for p in periods:
        in_rows = [r for r in all_rows_all if in_period(r["created"], p)]
        out_rows.extend(_ff_period_rows(in_rows, p, {}, last_updated, FF_HEADERS))
    return out_rows


def build_ff_region_rows(raw_rows, periods, last_updated, prepared=None):
    """
    video_ff_region: one row per (period, region, reason). Same column shape as
    video_ff plus a region column, so the dashboard's region chip can look up a
    region-scoped reason breakdown.
    """
    all_rows_all = prepared if prepared is not None else prepare_rows(raw_rows)

    def in_period(dt, p):
        return dt is not None and p["start"] <= dt < p["end"]

    regions = sorted({r["region"] for r in all_rows_all})
    out_rows = []
    for p in periods:
        in_rows = [r for r in all_rows_all if in_period(r["created"], p)]
        for region in regions:
            region_rows = [r for r in in_rows if r["region"] == region]
            out_rows.extend(_ff_period_rows(region_rows, p, {"region": region},
                                            last_updated, FF_REGION_HEADERS))
    return out_rows


def build_ff_byregion_rows(raw_rows, periods, last_updated, prepared=None):
    """
    video_ff_byregion: one row per (period, region_set, reason). region_set is
    the sorted region names of a non-trivial subset (not empty, not all), same
    key the dashboard builds from its region chips - matches video_vin_byregion /
    video_rt_byregion so the Fulfilment-failure-reasons card follows the chip
    selection exactly.
    """
    all_rows_all = prepared if prepared is not None else prepare_rows(raw_rows)

    def in_period(dt, p):
        return dt is not None and p["start"] <= dt < p["end"]

    regions = sorted({r["region"] for r in all_rows_all})
    out_rows = []
    for p in periods:
        in_rows = [r for r in all_rows_all if in_period(r["created"], p)]
        for mask in range(1, (1 << len(regions)) - 1):
            subset = [reg for i, reg in enumerate(regions) if mask & (1 << i)]
            key = ",".join(subset)
            scope_rows = [r for r in in_rows if r["region"] in subset]
            out_rows.extend(_ff_period_rows(scope_rows, p, {"region_set": key},
                                            last_updated, FF_BYREGION_HEADERS))
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
        print(f"  {len(raw_rows)} rows")

        # Live rooftops only (see LIVE_ROOFTOPS_SQL)
        live_rooftops = fetch_live_rooftops(base_url, session_id)
        before = len(raw_rows)
        raw_rows = [r for r in raw_rows
                    if str(r.get("Team_ID") or r.get("team_id") or "") in live_rooftops]
        print(f"  {len(live_rooftops)} Live rooftops; kept {len(raw_rows)} of {before} rows")
        log["rows_fetched"] = len(raw_rows)

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
        ff_region_rows = build_ff_region_rows(raw_rows, periods, last_updated)
        ff_br_rows = build_ff_byregion_rows(raw_rows, periods, last_updated)
        vin_br_headers, vin_br_rows = build_byregion_rows(raw_rows, periods, "vin", last_updated)
        rt_br_headers, rt_br_rows = build_byregion_rows(raw_rows, periods, "rt", last_updated)

        log["video_vin_rows"] = len(vin_rows)
        log["video_region_rows"] = len(region_rows)
        log["video_rt_rows"] = len(rt_rows)
        log["video_ff_rows"] = len(ff_rows)

        tabs = {
            "video_vin": (VIN_HEADERS, vin_rows),
            "video_region": (REGION_HEADERS, region_rows),
            "video_rt": (RT_HEADERS, rt_rows),
            "video_ff": (FF_HEADERS, ff_rows),
            "video_ff_region": (FF_REGION_HEADERS, ff_region_rows),
            "video_ff_byregion": (FF_BYREGION_HEADERS, ff_br_rows),
            "video_vin_byregion": (vin_br_headers, vin_br_rows),
            "video_rt_byregion": (rt_br_headers, rt_br_rows),
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
