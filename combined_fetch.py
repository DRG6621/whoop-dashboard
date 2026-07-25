"""
combined_fetch.py â runs in GitHub Actions.
Fetches WHOOP recovery + Strava activity data and writes a combined index.html dashboard.

Required secrets (repo Settings â Secrets and variables â Actions):
  WHOOP_CLIENT_ID, WHOOP_CLIENT_SECRET, WHOOP_REFRESH_TOKEN
  STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, STRAVA_REFRESH_TOKEN
"""

import json, os, sys, time
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("Run: pip install requests")
    sys.exit(1)

WHOOP_BASE   = "https://api.prod.whoop.com/developer"
WHOOP_TOKEN  = "https://api.prod.whoop.com/oauth/oauth2/token"
STRAVA_BASE  = "https://www.strava.com/api/v3"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
DAYS_BACK    = int(os.environ.get("DAYS_BACK", "30"))
FTP          = 290
OUTPUT       = Path("index.html")


# ââ TOKEN REFRESH âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def refresh_whoop():
    r = requests.post(WHOOP_TOKEN, data={
        "grant_type":    "refresh_token",
        "refresh_token": os.environ["WHOOP_REFRESH_TOKEN"],
        "client_id":     os.environ["WHOOP_CLIENT_ID"],
        "client_secret": os.environ["WHOOP_CLIENT_SECRET"],
    })
    if r.status_code != 200:
        print(f"WHOOP token refresh failed: {r.status_code} {r.text}")
        sys.exit(1)
    print("â WHOOP token refreshed")
    return r.json()

def refresh_strava():
    r = requests.post(STRAVA_TOKEN, data={
        "grant_type":    "refresh_token",
        "refresh_token": os.environ["STRAVA_REFRESH_TOKEN"],
        "client_id":     os.environ["STRAVA_CLIENT_ID"],
        "client_secret": os.environ["STRAVA_CLIENT_SECRET"],
    })
    if r.status_code != 200:
        print(f"Strava token refresh failed: {r.status_code} {r.text}")
        sys.exit(1)
    print("â Strava token refreshed")
    return r.json()


# ââ API HELPERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def whoop_get(path, token, params=None):
    h = {"Authorization": f"Bearer {token['access_token']}"}
    records, nxt = [], None
    while True:
        p = dict(params or {}, limit=25)
        if nxt:
            p["nextToken"] = nxt
        r = requests.get(WHOOP_BASE + path, headers=h, params=p)
        if r.status_code == 429:
            time.sleep(15)
            continue
        r.raise_for_status()
        d = r.json()
        records.extend(d.get("records", [d]))
        nxt = d.get("next_token")
        if not nxt:
            break
    return records

def strava_get(path, token, params=None):
    h = {"Authorization": f"Bearer {token['access_token']}"}
    records, page = [], 1
    while True:
        p = dict(params or {}, per_page=200, page=page)
        r = requests.get(STRAVA_BASE + path, headers=h, params=p)
        if r.status_code == 429:
            time.sleep(15)
            continue
        r.raise_for_status()
        d = r.json()
        if not d:
            break
        records.extend(d)
        if len(d) < 200:
            break
        page += 1
    return records


# ââ DATA FETCHERS âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def fetch_whoop(token):
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = {"start": start}
    recs   = whoop_get("/v2/recovery", token, params)
    sleeps = whoop_get("/v2/activity/sleep", token, params)
    print(f"  WHOOP: {len(recs)} recovery, {len(sleeps)} sleep records")

    sleep_map = {s["cycle_id"]: s for s in sleeps if not s.get("nap")}
    out = {}
    for rec in recs:
        if rec.get("score_state") != "SCORED":
            continue
        sc   = rec.get("score", {})
        slp  = sleep_map.get(rec["cycle_id"])
        date = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
        hrs  = None
        if slp and slp.get("score_state") == "SCORED":
            ss  = slp["score"].get("stage_summary", {})
            ms  = ss.get("total_in_bed_time_milli", 0) - ss.get("total_awake_time_milli", 0)
            hrs = round(ms / 3_600_000, 2) if ms else None
        out[date] = {
            "hrv":      sc.get("hrv_rmssd_milli"),
            "rhr":      sc.get("resting_heart_rate"),
            "recovery": sc.get("recovery_score"),
            "sleep":    hrs,
        }
    return out

def fetch_strava(token):
    after  = int((datetime.now(timezone.utc) - timedelta(days=DAYS_BACK)).timestamp())
    acts   = strava_get("/athlete/activities", token, {"after": after})
    print(f"  Strava: {len(acts)} activities")
    by_day = {}
    for a in acts:
        k = a["start_date_local"][:10]
        d = by_day.setdefault(k, {"effort": 0, "kj": 0, "secs": 0})
        d["effort"] += (a.get("suffer_score") or 0)
        d["kj"]     += (a.get("kilojoules")   or 0)
        d["secs"]   += (a.get("moving_time")  or 0)
    for d in by_day.values():
        d["cal"] = round(d["kj"])   # kJ â kcal for cyclists
        d["hrs"] = round(d["secs"] / 3600, 2)
    return by_day


# ââ STAT HELPERS ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def rolling7(vals):
    out = []
    for i in range(len(vals)):
        w = [v for v in vals[max(0, i - 6):i + 1] if v is not None]
        out.append(round(sum(w) / len(w), 1) if w else None)
    return out

def safe_avg(lst, p=1):
    v = [x for x in lst if x is not None]
    return round(sum(v) / len(v), p) if v else None


# ââ HTML BUILDER ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ

def build_html(whoop, strava):
    now = datetime.now(timezone.utc)

    # Build day-by-day list
    days = []
    for i in range(DAYS_BACK - 1, -1, -1):
        d  = now - timedelta(days=i)
        k  = d.strftime("%Y-%m-%d")
        # Use %d and strip leading zero manually for cross-platform safety
        lb = d.strftime("%b") + " " + str(d.day)
        w  = whoop.get(k, {})
        s  = strava.get(k, {})
        days.append({
            "date":     k,
            "label":    lb,
            "hrv":      w.get("hrv"),
            "rhr":      w.get("rhr"),
            "recovery": w.get("recovery"),
            "sleep":    w.get("sleep"),
            "effort":   s.get("effort", 0),
            "cal":      s.get("cal", 0),
            "hrs":      s.get("hrs", 0),
        })

    # Aggregates
    avg_hrv  = safe_avg([d["hrv"]      for d in days])
    avg_rhr  = safe_avg([d["rhr"]      for d in days])
    avg_rec  = safe_avg([d["recovery"] for d in days])
    avg_slp  = safe_avg([d["sleep"]    for d in days])

    w7         = days[-7:]
    w7_effort  = sum(d["effort"] for d in w7)
    w7_hrs     = round(sum(d["hrs"] for d in w7), 1)

    today_d  = days[-1]
    t_rec    = today_d.get("recovery")
    t_hrv    = today_d.get("hrv")
    t_rhr    = today_d.get("rhr")
    has_w    = bool(t_rec)

    # Strava fatigue (0â100)
    sf = (10 if w7_effort < 200 else
          28 if w7_effort < 350 else
          55 if w7_effort < 500 else
          72 if w7_effort < 650 else 88)
    fat_label = ("Low"      if sf < 25 else
                 "Moderate" if sf < 50 else
                 "High"     if sf < 75 else "Very High")
    fat_color = ("#22c55e" if sf < 25 else
                 "#f59e0b" if sf < 50 else
                 "#ef4444" if sf < 75 else "#dc2626")

    # Combined readiness
    if has_w:
        readiness = round(t_rec * 0.60 + (100 - sf) * 0.40)
        if t_hrv and avg_hrv and t_hrv < avg_hrv * 0.90:
            readiness = round(readiness * 0.88)
    else:
        readiness = None

    # Recommendation card
    if readiness is not None:
        if readiness >= 72:
            rc_bg = "#f0fdf4"; rc_br = "#86efac"; rc_icon = "ð´"
            rc_title = "Go Time â Readiness " + str(readiness) + "%"
            rc_body  = ("WHOOP " + str(round(t_rec)) + "% recovery with " + str(w7_effort) +
                        " RE this week. Body is ready â consider threshold (275â305W) or sweet spot (246â275W).")
        elif readiness >= 50:
            rc_bg = "#fefce8"; rc_br = "#fde047"; rc_icon = "ð´"
            rc_title = "Moderate Readiness (" + str(readiness) + "%) â Stay Controlled"
            rc_body  = ("Recovery " + str(round(t_rec)) + "% + " + str(w7_effort) +
                        " RE this week. Zone 2 only today (160â218W, HR 121â150). Save intensity for when WHOOP shows green.")
        else:
            rc_bg = "#fef2f2"; rc_br = "#fca5a5"; rc_icon = "ð"
            rc_title = "Rest Day â Readiness " + str(readiness) + "%"
            rc_body  = ("WHOOP " + str(round(t_rec)) + "% recovery signals the body needs it. " +
                        "Hard training today builds no fitness â rest does. Target 8+ hrs sleep.")
    elif sf >= 72:
        rc_bg = "#fef2f2"; rc_br = "#fca5a5"; rc_icon = "ð"
        rc_title = "High Training Load â Consider Rest"
        rc_body  = str(w7_effort) + " RE over 7 days. WHOOP data not available today â go by feel."
    else:
        rc_bg = "#f0fdf4"; rc_br = "#86efac"; rc_icon = "ð´"
        rc_title = "Training Load Manageable"
        rc_body  = str(w7_effort) + " RE this week â capacity looks good. WHOOP score not available today."

    bar_c = ("#22c55e" if readiness is None or readiness >= 72 else
             "#f59e0b" if readiness >= 50 else "#ef4444")

    if readiness is not None:
        rp = min(readiness, 100)
        readiness_bar = (
            '<div class="readiness-wrap">'
            '<div class="readiness-label"><span>Combined Readiness</span><span>' + str(readiness) + '%</span></div>'
            '<div class="readiness-bar"><div class="readiness-fill" style="width:' + str(rp) + '%;background:' + bar_c + '"></div></div>'
            '</div>'
        )
    else:
        readiness_bar = ""

    # Divergence banner
    banner = ""
    if has_w:
        if t_rec >= 70 and sf >= 55:
            banner = ('<div class="banner banner-purple"><span>ð</span><span>'
                      'Body says go (' + str(round(t_rec)) + '% recovery) but load is piling up (' + str(w7_effort) +
                      ' RE / 7 days). Ride Zone 2 today (160â218W) â this is how aerobic base compounds without digging a hole.'
                      '</span></div>')
        elif t_rec < 45 and sf < 30:
            banner = ('<div class="banner banner-amber"><span>â ï¸</span><span>'
                      'Load is light but recovery is low (' + str(round(t_rec)) + '%). '
                      "Something outside cycling is draining you â don't add load just because Strava looks fresh."
                      '</span></div>')
        elif t_hrv and avg_hrv and t_hrv < avg_hrv * 0.88 and t_rec >= 50:
            pct = round((1 - t_hrv / avg_hrv) * 100)
            banner = ('<div class="banner banner-blue"><span>ð¡</span><span>'
                      'HRV (' + str(round(t_hrv)) + 'ms) is ' + str(pct) + '% below your ' +
                      str(avg_hrv) + 'ms baseline even though recovery % looks OK. '
                      'HRV is the earlier warning signal â keep intensity sub-threshold today.'
                      '</span></div>')

    # Stat display helpers
    def s_rec():
        if has_w:        return (str(round(t_rec)) + "%", "tag-live", "today")
        if avg_rec:      return (str(avg_rec) + "%",       "tag-avg",  "30-day avg")
        return ("â", "tag-avg", "â")

    def s_hrv():
        if t_hrv:        return (str(round(t_hrv)),        "tag-live", "today")
        if avg_hrv:      return (str(avg_hrv),             "tag-avg",  "30-day avg")
        return ("â", "tag-avg", "â")

    def s_rhr():
        if t_rhr:        return (str(int(t_rhr)),          "tag-live", "today")
        if avg_rhr:      return (str(avg_rhr),             "tag-avg",  "30-day avg")
        return ("â", "tag-avg", "â")

    rv, rc, rt = s_rec()
    hv, hc, ht = s_hrv()
    rv2, rc2, rt2 = s_rhr()
    sv = str(avg_slp) if avg_slp else "â"

    # Guidance text
    if readiness is not None and readiness < 50:
        floor = round(avg_hrv * 0.90) if avg_hrv else 84
        g_rec = ("WHOOP says rest â aim for 8.5â9 hrs tonight. Elevate legs 20 min. "
                 "If HRV is still below " + str(floor) + "ms tomorrow, take another easy day rather than forcing intervals.")
    elif t_hrv and avg_hrv and t_hrv < avg_hrv * 0.90:
        g_rec = ("HRV suppression (" + str(round(t_hrv)) + "ms vs " + str(avg_hrv) + "ms baseline) is a nervous system signal. "
                 "Extra sleep beats extra training now. 8+ hrs, no caffeine after noon.")
    elif sf >= 55:
        g_rec = ("High load week â cold/warm contrast showers flush metabolites. Aim for 8 hrs. "
                 "Check tomorrow's WHOOP: if recovery < 65%, hold any high-intensity plan.")
    else:
        g_rec = ("You're well-recovered. Standard 7.5â8 hrs protects your HRV baseline. "
                 "Light foam rolling (hips, calves, hamstrings) 10 min tonight maintains mobility.")

    t_cal = today_d.get("cal") or 0
    if t_cal > 1400:
        g_nut = ("Post-ride (" + str(t_cal) + " kcal burned): replenish within 30 min â "
                 "85â100g carbs + 25â30g protein. Keep dinner carb-forward. Tomorrow pre-ride: 60â80g carbs 2 hrs out.")
    elif readiness is not None and readiness < 50:
        g_nut = ("Rest day: reduce carbs slightly (200â250g total) but keep protein high â 1.7g/kg = 140g for you. "
                 "Leucine-rich foods (eggs, Greek yogurt, chicken) drive muscle repair.")
    elif readiness is not None and readiness >= 72:
        g_nut = ("Pre-session: 60â80g easy carbs 2 hrs out. At FTP " + str(FTP) + "W, hard zones burn 900â1,000 kcal/hr. "
                 "During 90+ min rides: 60â90g carbs/hr on the bike.")
    else:
        g_nut = ("Moderate-load day. Target 5â7g carbs/kg (420â590g). "
                 "Post-ride recovery window (first 30 min) is most critical â protein + carb combo immediately after.")

    if readiness is not None and readiness >= 72 and sf < 55:
        g_nxt = ("Excellent readiness, manageable load â ideal for quality. "
                 "2Ã20 min sweet spot (246â275W, 85â95% FTP) or 3Ã12 min threshold (275â305W). Full 20-min Z2 warmup first.")
    elif readiness is not None and readiness >= 72:
        g_nxt = ("Good recovery but load is high â best next session is 90-min Zone 2 endurance (160â218W). "
                 "Reinforces aerobic adaptation without adding fatigue.")
    else:
        g_nxt = ("Let the body recover. Next quality session: when WHOOP shows â¥70%, "
                 "open with a 45-min activation (Z2 + 3Ã2 min fast pedalling) before returning to structured intervals.")

    # Source pill
    pill_bg    = "#ede9fe" if has_w else "#fff7ed"
    pill_color = "#7c3aed" if has_w else "#c2410c"
    pill_text  = "ð¢ WHOOP + Strava" if has_w else "ð  Strava + WHOOP (no score today)"

    # Chart data
    chart_json = json.dumps({
        "labels":   [d["label"]    for d in days],
        "effort":   [d["effort"]   for d in days],
        "recovery": [d["recovery"] for d in days],
        "hrv":      [d["hrv"]      for d in days],
        "hrv_avg":  rolling7([d["hrv"]      for d in days]),
        "rec_avg":  rolling7([d["recovery"] for d in days]),
        "sleep":    [d["sleep"]    for d in days],
    })

    updated = now.strftime("%B %-d, %Y %H:%M UTC")

    # JavaScript (no f-string needed â embed chart_json directly)
    js = """
const D = """ + chart_json + """;
const base = {
  responsive: true, maintainAspectRatio: false,
  plugins: { legend: {display:false}, tooltip: {mode:'index',intersect:false} },
  elements: { point: {radius:0, hoverRadius:4} },
  scales: {
    x: { grid: {display:false}, ticks: {maxTicksLimit:8, maxRotation:0} },
    y: { grid: {color:'#f1f5f9'} }
  }
};
const barC = D.effort.map(e =>
  e===0?'#e2e8f0':e<=60?'#86efac':e<=110?'#fde047':e<=160?'#fb923c':'#f87171');

new Chart(document.getElementById('dual').getContext('2d'), {
  type:'bar', data:{ labels:D.labels, datasets:[
    { label:'Load (RE)', data:D.effort, backgroundColor:barC, borderRadius:3, yAxisID:'y1', order:2 },
    { label:'Recovery %', data:D.recovery, type:'line', borderColor:'#22c55e',
      backgroundColor:'rgba(34,197,94,.1)', fill:true, tension:0.4,
      pointRadius:3, pointBackgroundColor:'#22c55e', pointBorderColor:'white', pointBorderWidth:1.5,
      yAxisID:'y2', order:1, spanGaps:false }
  ]},
  options:{
    responsive:true, maintainAspectRatio:false,
    plugins:{legend:{display:false},tooltip:{mode:'index',intersect:false}},
    scales:{
      x:{grid:{display:false},ticks:{maxTicksLimit:8,maxRotation:0}},
      y1:{type:'linear',position;'left',beginAtZero:true,grid:{color:'#f1f5f9'},
          title:{display:true,text:'Relative Effort',font:{size:10},color:'#94a3b8'}},
      y2:{type:'linear',position;'right',min:0,max:100,grid:{drawOnChartArea:false},
          title:{display:true,text:'Recovery %',font:{size:10},color:'#22c55e'},
          ticks:{color:'#22c55e'}}
    }
  }
});
new Chart(document.getElementById('hrvc').getContext('2d'),{type:'line',data:{labels:D.labels,datasets:[
  {label:'HRV',data:D.hrv,borderColor:'#a5b4fc',borderWidth:1.5,backgroundColor:'rgba(99,102,241,.08)',fill:true,tension:0.3,pointRadius:0},
  {label:'7d avg',data:D.hrv_avg,borderColor:'#6366f1',borderWidth:2.5,tension:0.4,pointRadius:0}
]},options:Object.assign({},base)});
new Chart(document.getElementById('recc').getContext('2d'),{type:'line',data:{labels:D.labels,datasets:[
  {label:'Recovery',data:D.recovery,borderColor:'#86efac',borderWidth:1.5,backgroundColor:'rgba(34,197,94,.08)',fill:true,tension:0.3,pointRadius:0},
  {label:'7d avg',data:D.rec_avg,borderColor:'#22c55e',borderWidth:2.5,tension:0.4,pointRadius:0}
]},options:Object.assign({},base,{scales:Object.assign({},base.scales,{y:{grid:{color:'#f1f5f9'},min:0,max:100}})})});
new Chart(document.getElementById('slpc').getContext('2d'),{type:'line',data:{labels:D.labels,datasets:[
  {label:'Sleep',data:D.sleep,borderColor:'#93c5fd',borderWidth:1.5,backgroundColor:'rgba(59,130,246,.08)',fill:true,tension:0.3,pointRadius:0}
]},options:Object.assign({},base)});
"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Keith's Training + Recovery Dashboard</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;background:#f8fafc;color:#1e293b;padding:24px;max-width:980px;margin:0 auto}}
h1{{font-size:1.5rem;font-weight:700;margin-bottom:4px}}
.sub{{color:#64748b;font-size:.85rem;margin-bottom:18px;display:flex;justify-content:space-between;align-items:center}}
.pill{{font-size:.72rem;padding:3px 9px;border-radius:20px;font-weight:600;background:{pill_bg};color:{pill_color}}}
.rec{{border-radius:12px;padding:20px 24px;margin-bottom:14px;display:flex;gap:16px;align-items:flex-start;background:{rc_bg};border:2px solid {rc_br}}}
.rec-icon{{font-size:2.2rem;line-height:1;flex-shrink:0}}
.rec-title{{font-size:1.1rem;font-weight:700}}
.rec-body{{font-size:.85rem;color:#475569;margin-top:5px;line-height:1.5}}
.readiness-wrap{{margin-top:10px}}
.readiness-label{{font-size:.72rem;color:#64748b;display:flex;justify-content:space-between;margin-bottom:4px}}
.readiness-bar{{height:8px;background:#e2e8f0;border-radius:4px;overflow:hidden}}
.readiness-fill{{height:100%;border-radius:4px}}
.banner{{display:flex;gap:10px;align-items:flex-start;border-radius:8px;padding:10px 14px;font-size:.82rem;margin-bottom:14px;line-height:1.5}}
.banner-purple{{background:#f5f3ff;border-left:3px solid #7c3aed;color:#4c1d95}}
.banner-blue{{background:#eff6ff;border-left:3px solid #3b82f6;color:#1e3a8a}}
.banner-amber{{background:#fffbeb;border-left:3px solid #f59e0b;color:#78350f}}
.stats{{display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:14px}}
.stat{{background:white;border-radius:10px;padding:14px 16px;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.stat-label{{font-size:.68rem;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8}}
.stat-val{{font-size:1.4rem;font-weight:700;margin:4px 0 2px}}
.stat-sub{{font-size:.68rem;color:#94a3b8}}
.tag{{font-size:.62rem;padding:1px 5px;border-radius:4px;display:inline-block;margin-top:3px}}
.tag-live{{background:#f0fdf4;color:#16a34a}}
.tag-avg{{background:#f1f5f9;color:#64748b}}
.card{{background:white;border-radius:12px;padding:20px 22px;box-shadow:0 1px 3px rgba(0,0,0,.07);margin-bottom:14px}}
.card h2{{font-size:.78rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#94a3b8;margin-bottom:14px}}
.ch-tall{{height:200px}}.ch-sm{{height:155px}}
.grid2{{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:14px}}
.grid3{{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin-bottom:14px}}
.g-card{{background:white;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(0,0,0,.07)}}
.g-icon{{font-size:1.3rem;margin-bottom:6px}}
.g-label{{font-size:.72rem;font-weight:600;text-transform:uppercase;letter-spacing:.04em;color:#64748b;margin-bottom:6px}}
.g-text{{font-size:.82rem;line-height:1.55;color:#334155}}
@media(max-width:700px){{.stats{{grid-template-columns:repeat(3,1fr)}}.grid2,.grid3{{grid-template-columns:1fr}}}}
@media(max-width:420px){{.stats{{grid-template-columns:repeat(2,1fr)}}}}
</style>
</head>
<body>

<h1>&#9889; Keith's Training + Recovery Dashboard</h1>
<div class="sub">
  <span>Updated {updated} &nbsp;&middot;&nbsp; {DAYS_BACK} days</span>
  <span class="pill">{pill_text}</span>
</div>

<div class="rec">
  <div class="rec-icon">{rc_icon}</div>
  <div style="flex:1">
    <div class="rec-title">{rc_title}</div>
    <div class="rec-body">{rc_body}</div>
    {readiness_bar}
  </div>
</div>

{banner}

<div class="stats">
  <div class="stat">
    <div class="stat-label">Recovery</div>
    <div class="stat-val" style="color:#22c55e">{rv}</div>
    <div class="stat-sub">WHOOP %</div>
    <span class="tag {rc}">{rt}</span>
  </div>
  <div class="stat">
    <div class="stat-label">HRV</div>
    <div class="stat-val" style="color:#6366f1">{hv}</div>
    <div class="stat-sub">ms</div>
    <span class="tag {hc}">{ht}</span>
  </div>
  <div class="stat">
    <div class="stat-label">RHR</div>
    <div class="stat-val" style="color:#ef4444">{rv2}</div>
    <div class="stat-sub">bpm</div>
    <span class="tag {rc2}">{rt2}</span>
  </div>
  <div class="stat">
    <div class="stat-label">7-Day Load</div>
    <div class="stat-val" style="color:#f59e0b">{w7_effort}</div>
    <div class="stat-sub">relative effort</div>
  </div>
  <div class="stat">
    <div class="stat-label">7-Day Hours</div>
    <div class="stat-val" style="color:#3b82f6">{w7_hrs}</div>
    <div class="stat-sub">hrs riding</div>
  </div>
  <div class="stat">
    <div class="stat-label">Avg Sleep</div>
    <div class="stat-val" style="color:#8b5cf6">{sv}</div>
    <div class="stat-sub">hrs &middot; 30-day</div>
  </div>
</div>

<div class="card">
  <h2>30-Day Training Load vs. WHOOP Recovery &nbsp;<span style="font-weight:400">(Strava bars &middot; WHOOP recovery line)</span></h2>
  <div class="ch-tall"><canvas id="dual"></canvas></div>
</div>

<div class="grid2">
  <div class="card"><h2>HRV (+ 7-day avg)</h2><div class="ch-sm"><canvas id="hrvc"></canvas></div></div>
  <div class="card"><h2>Recovery Score (+ 7-day avg)</h2><div class="ch-sm"><canvas id="recc"></canvas></div></div>
</div>

<div class="card"><h2>Sleep Duration</h2><div class="ch-sm"><canvas id="slpc"></canvas></div></div>

<div class="grid3">
  <div class="g-card"><div class="g-icon">&#128564;</div><div class="g-label">Recovery</div><div class="g-text">{g_rec}</div></div>
  <div class="g-card"><div class="g-icon">&#129361;</div><div class="g-label">Nutrition</div><div class="g-text">{g_nut}</div></div>
  <div class="g-card"><div class="g-icon">&#127919;</div><div class="g-label">Next Session</div><div class="g-text">{g_nxt}</div></div>
</div>

<script>{js}</script>
</body>
</html>"""


def main():
    print("=== Keith's Combined Dashboard Builder ===\n")
    wt = refresh_whoop()
    st = refresh_strava()
    print()
    whoop  = fetch_whoop(wt)
    strava = fetch_strava(st)
    print(f"\nBuilding HTML ({DAYS_BACK} days)...")
    html = build_html(whoop, strava)
    OUTPUT.write_text(html, encoding="utf-8")
    print(f"â Wrote {OUTPUT} ({len(html):,} bytes)")


if __name__ == "__main__":
    main()
