"""
whoop_fetch.py (CI version) — runs in GitHub Actions.

Reads credentials from env vars, fetches WHOOP data,
and writes a self-contained index.html dashboard.

Required secrets (set in repo Settings -> Secrets and variables -> Actions):
  WHOOP_CLIENT_ID
  WHOOP_CLIENT_SECRET
  WHOOP_REFRESH_TOKEN
"""

import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Run:  pip install requests")
    sys.exit(1)


# -- CONSTANTS -----------------------------------------------------------------
BASE_URL  = "https://api.prod.whoop.com/developer"
TOKEN_URL = "https://api.prod.whoop.com/oauth/oauth2/token"
DAYS_BACK = int(os.environ.get("WHOOP_DAYS_BACK", "400"))
OUTPUT    = Path("index.html")
# ------------------------------------------------------------------------------


def ci_mode():
    """Return a fresh access token by refreshing the stored refresh token."""
    client_id     = os.environ["WHOOP_CLIENT_ID"]
    client_secret = os.environ["WHOOP_CLIENT_SECRET"]
    refresh       = os.environ["WHOOP_REFRESH_TOKEN"]

    resp = requests.post(TOKEN_URL, data={
        "grant_type":    "refresh_token",
        "refresh_token": refresh,
        "client_id":     client_id,
        "client_secret": client_secret,
    })
    if resp.status_code != 200:
        print(f"Token refresh failed: {resp.status_code} {resp.text}")
        sys.exit(1)
    token = resp.json()
    print("Token refreshed successfully.")
    return token


def api_get(path, token, params=None):
    """Paginated GET from the WHOOP API."""
    headers = {"Authorization": f"Bearer {token['access_token']}"}
    records = []
    next_token = None

    while True:
        p = dict(params or {}, limit=25)
        if next_token:
            p["nextToken"] = next_token
        resp = requests.get(BASE_URL + path, headers=headers, params=p)
        if resp.status_code == 429:
            print("  Rate limited, waiting 15s...")
            time.sleep(15)
            continue
        resp.raise_for_status()
        data = resp.json()
        records.extend(data.get("records", [data]))
        next_token = data.get("next_token")
        if not next_token:
            break

    return records


def ms_to_hours(ms):
    return round(ms / 3_600_000, 4) if ms else None


def fetch_all(token):
    """Fetch recovery, sleep, and cycle data."""
    params = {}
    if DAYS_BACK:
        start_dt = datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)
        params["start"] = start_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    print(f"Fetching last {DAYS_BACK} days of data...")

    print("  -> recovery...")
    recoveries = api_get("/v2/recovery", token, params)
    print(f"     {len(recoveries)} records")

    print("  -> sleep...")
    sleeps = api_get("/v2/activity/sleep", token, params)
    print(f"     {len(sleeps)} records")

    rec_by_cycle   = {r["cycle_id"]: r for r in recoveries if r.get("score_state") == "SCORED"}
    sleep_by_cycle = {s["cycle_id"]: s for s in sleeps if not s.get("nap")}

    rows = []
    for cycle_id, rec in rec_by_cycle.items():
        score = rec.get("score", {})
        sleep = sleep_by_cycle.get(cycle_id)

        dt = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00"))
        date_str = dt.strftime("%Y-%m-%d")

        hrv   = score.get("hrv_rmssd_milli")
        rhr   = score.get("resting_heart_rate")
        rec_s = score.get("recovery_score")

        sleep_hours = deep_hours = rem_hours = light_hours = awake_hours = wakeups = None

        if sleep and sleep.get("score_state") == "SCORED":
            ss = sleep["score"].get("stage_summary", {})
            total_ms = ss.get("total_in_bed_time_milli", 0) - ss.get("total_awake_time_milli", 0)
            sleep_hours = ms_to_hours(total_ms)
            deep_hours  = ms_to_hours(ss.get("total_slow_wave_sleep_time_milli"))
            rem_hours   = ms_to_hours(ss.get("total_rem_sleep_time_milli"))
            light_hours = ms_to_hours(ss.get("total_light_sleep_time_milli"))
            awake_hours = ms_to_hours(ss.get("total_awake_time_milli"))
            wakeups     = ss.get("disturbance_count")

        rows.append({
            "date":     date_str,
            "hrv":      hrv,
            "rhr":      rhr,
            "recovery": rec_s,
            "sleep":    sleep_hours,
            "deep":     deep_hours,
            "rem":      rem_hours,
            "light":    light_hours,
            "awake":    awake_hours,
            "wakeups":  wakeups,
        })

    rows.sort(key=lambda r: r["date"])
    return rows


def rolling_avg(values, n=7):
    """Compute rolling average, returning None for windows with no data."""
    result = []
    for i in range(len(values)):
        window = [v for v in values[max(0, i - n + 1):i + 1] if v is not None]
        result.append(round(sum(window) / len(window), 2) if window else None)
    return result


def build_html(rows):
    """Generate a self-contained HTML dashboard with Chart.js."""
    dates    = [r["date"]     for r in rows]
    hrv      = [r["hrv"]      for r in rows]
    rhr      = [r["rhr"]      for r in rows]
    recovery = [r["recovery"] for r in rows]
    sleep    = [r["sleep"]    for r in rows]

    hrv_avg      = rolling_avg(hrv)
    rhr_avg      = rolling_avg(rhr)
    recovery_avg = rolling_avg(recovery)
    sleep_avg    = rolling_avg(sleep)

    updated = datetime.now(timezone.utc).strftime("%B %d, %Y at %H:%M UTC")
    n = len(rows)

    def safe_avg(lst):
        vals = [v for v in lst if v is not None]
        return round(sum(vals) / len(vals), 1) if vals else "N/A"

    recent = rows[-30:] if len(rows) >= 30 else rows
    avg_hrv = safe_avg([r["hrv"] for r in recent])
    avg_rhr = safe_avg([r["rhr"] for r in recent])
    avg_rec = safe_avg([r["recovery"] for r in recent])
    avg_slp = safe_avg([r["sleep"] for r in recent])

    data_json = json.dumps({
        "dates": dates,
        "hrv": hrv, "hrv_avg": hrv_avg,
        "rhr": rhr, "rhr_avg": rhr_avg,
        "recovery": recovery, "recovery_avg": recovery_avg,
        "sleep": sleep, "sleep_avg": sleep_avg,
    })

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Keith's WHOOP Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
          background: #f8fafc; color: #1e293b; padding: 24px; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 4px; }}
  .subtitle {{ color: #64748b; font-size: 0.9rem; margin-bottom: 28px; }}
  .stats {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
  .stat {{ background: white; border-radius: 12px; padding: 20px 24px;
           box-shadow: 0 1px 3px rgba(0,0,0,.08); min-width: 150px; flex: 1; }}
  .stat-label {{ font-size: 0.75rem; text-transform: uppercase; letter-spacing: .05em;
                 color: #64748b; margin-bottom: 6px; }}
  .stat-value {{ font-size: 2rem; font-weight: 700; }}
  .stat-sub {{ font-size: 0.75rem; color: #94a3b8; margin-top: 2px; }}
  .chart-card {{ background: white; border-radius: 12px; padding: 20px 24px;
                 box-shadow: 0 1px 3px rgba(0,0,0,.08); margin-bottom: 20px; }}
  .chart-card h2 {{ font-size: 1rem; font-weight: 600; margin-bottom: 16px; color: #334155; }}
  canvas {{ width: 100% !important; height: 200px !important; }}
  .grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; }}
  @media (max-width: 700px) {{ .grid {{ grid-template-columns: 1fr; }} }}
</style>
</head>
<body>
<h1>Keith's WHOOP Dashboard</h1>
<p class="subtitle">Updated {updated} &nbsp;&middot;&nbsp; {n} days of data</p>

<div class="stats">
  <div class="stat">
    <div class="stat-label">Avg HRV</div>
    <div class="stat-value" style="color:#6366f1">{avg_hrv}</div>
    <div class="stat-sub">ms &nbsp;&middot;&nbsp; last 30 days</div>
  </div>
  <div class="stat">
    <div class="stat-label">Avg RHR</div>
    <div class="stat-value" style="color:#ef4444">{avg_rhr}</div>
    <div class="stat-sub">bpm &nbsp;&middot;&nbsp; last 30 days</div>
  </div>
  <div class="stat">
    <div class="stat-label">Avg Recovery</div>
    <div class="stat-value" style="color:#22c55e">{avg_rec}</div>
    <div class="stat-sub">% &nbsp;&middot;&nbsp; last 30 days</div>
  </div>
  <div class="stat">
    <div class="stat-label">Avg Sleep</div>
    <div class="stat-value" style="color:#3b82f6">{avg_slp}</div>
    <div class="stat-sub">hrs &nbsp;&middot;&nbsp; last 30 days</div>
  </div>
</div>

<div class="chart-card">
  <h2>HRV <span style="color:#94a3b8;font-weight:400;font-size:.85rem">(+ 7-day avg)</span></h2>
  <canvas id="hrv"></canvas>
</div>
<div class="grid">
  <div class="chart-card"><h2>Resting Heart Rate</h2><canvas id="rhr"></canvas></div>
  <div class="chart-card"><h2>Recovery Score</h2><canvas id="rec"></canvas></div>
</div>
<div class="chart-card">
  <h2>Sleep Duration <span style="color:#94a3b8;font-weight:400;font-size:.85rem">(+ 7-day avg)</span></h2>
  <canvas id="slp"></canvas>
</div>

<script>
const D = {data_json};
const N = 365;
const sl = arr => arr.slice(-N);
const baseOpts = {{
  responsive: true, maintainAspectRatio: false,
  plugins: {{ legend: {{ display: false }}, tooltip: {{ mode: "index", intersect: false }} }},
  elements: {{ point: {{ radius: 0, hoverRadius: 4 }} }},
  scales: {{
    x: {{ ticks: {{ maxTicksLimit: 8, maxRotation: 0 }}, grid: {{ display: false }} }},
    y: {{ grid: {{ color: "#f1f5f9" }} }}
  }}
}};
new Chart(document.getElementById("hrv"), {{ type:"line", data:{{ labels:sl(D.dates),
  datasets:[
    {{ label:"HRV", data:sl(D.hrv), borderColor:"#a5b4fc", borderWidth:1, backgroundColor:"rgba(99,102,241,.08)", fill:true, tension:0.3 }},
    {{ label:"7-day avg", data:sl(D.hrv_avg), borderColor:"#6366f1", borderWidth:2.5, tension:0.4 }}
  ]}}, options:baseOpts }});
new Chart(document.getElementById("rhr"), {{ type:"line", data:{{ labels:sl(D.dates),
  datasets:[
    {{ label:"RHR", data:sl(D.rhr), borderColor:"#fca5a5", borderWidth:1, backgroundColor:"rgba(239,68,68,.08)", fill:true, tension:0.3 }},
    {{ label:"7-day avg", data:sl(D.rhr_avg), borderColor:"#ef4444", borderWidth:2.5, tension:0.4 }}
  ]}}, options:baseOpts }});
new Chart(document.getElementById("rec"), {{ type:"line", data:{{ labels:sl(D.dates),
  datasets:[
    {{ label:"Recovery", data:sl(D.recovery), borderColor:"#86efac", borderWidth:1, backgroundColor:"rgba(34,197,94,.08)", fill:true, tension:0.3 }},
    {{ label:"7-day avg", data:sl(D.recovery_avg), borderColor:"#22c55e", borderWidth:2.5, tension:0.4 }}
  ]}}, options:{{ ...baseOpts, scales:{{ ...baseOpts.scales, y:{{ min:0, max:100 }} }} }} }});
new Chart(document.getElementById("slp"), {{ type:"line", data:{{ labels:sl(D.dates),
  datasets:[
    {{ label:"Sleep", data:sl(D.sleep), borderColor:"#93c5fd", borderWidth:1, backgroundColor:"rgba(59,130,246,.08)", fill:true, tension:0.3 }},
    {{ label:"7-day avg", data:sl(D.sleep_avg), borderColor:"#3b82f6", borderWidth:2.5, tension:0.4 }}
  ]}}, options:baseOpts }});
</script>
</body>
</html>"""
    return html


def main():
    token = ci_mode()
    rows  = fetch_all(token)
    html  = build_html(rows)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"\nWrote {OUTPUT} ({len(rows)} days, {len(html):,} bytes)")


if __name__ == "__main__":
    main()
