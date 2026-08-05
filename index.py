"""
Vercel serverless dashboard for Keith's training + recovery data.
Live-fetches WHOOP + Strava + TrainingPeaks on each load (15-min cache),
owns the rotating WHOOP refresh token in Upstash KV, and returns an
interactive HTML dashboard. Login security: set env APP_PASSWORD to require
a password (signed cookie session ~60 days, brute-force lockout via KV).
"""
import json, os, time, urllib.parse, hashlib, hmac
from datetime import datetime, timedelta, timezone

import requests

# Athlete's local timezone — day buckets use this so "today" matches Keith's day,
# not UTC (which was making WHOOP look a day behind in the evening).
try:
    from zoneinfo import ZoneInfo
    ET = ZoneInfo(os.environ.get("APP_TIMEZONE", "America/New_York"))
except Exception:
    ET = timezone.utc

def _env(k, d=""):
    return (os.environ.get(k, d) or "").strip()

WHOOP_BASE   = "https://api.prod.whoop.com/developer"
WHOOP_TOKEN  = "https://api.prod.whoop.com/oauth/oauth2/token"
STRAVA_BASE  = "https://www.strava.com/api/v3"
STRAVA_TOKEN = "https://www.strava.com/oauth/token"
DAYS         = 90
CACHE_TTL    = 900  # 15 minutes

# ---- Upstash KV (persistent store for the rotating WHOOP token + cache) ----
def _kv_cfg():
    url = _env("KV_REST_API_URL") or _env("UPSTASH_REDIS_REST_URL")
    tok = _env("KV_REST_API_TOKEN") or _env("UPSTASH_REDIS_REST_TOKEN")
    return url, tok

def kv_get(key):
    url, tok = _kv_cfg()
    if not url:
        return None
    r = requests.get(f"{url}/get/{key}", headers={"Authorization": f"Bearer {tok}"}, timeout=15)
    if r.status_code != 200:
        return None
    return r.json().get("result")

def kv_set(key, value):
    url, tok = _kv_cfg()
    if not url:
        return
    requests.post(f"{url}/set/{key}", headers={"Authorization": f"Bearer {tok}"},
                  data=value.encode("utf-8"), timeout=15)

# ---- login security ----
# Set env APP_PASSWORD in Vercel to lock the dashboard. Sessions are HMAC-signed
# cookies derived from the password (changing the password logs everyone out).
SESSION_DAYS = 60
LOCKOUT_FAILS = 8       # wrong guesses allowed per window
LOCKOUT_WINDOW = 900    # 15 minutes

def _app_password():
    return _env("APP_PASSWORD")

def _coach_password():
    return _env("COACH_PASSWORD")  # optional second login for Coach Jeremiah Bishop

def _auth_secret():
    # Derived from BOTH passwords: changing either logs everyone out.
    return hashlib.sha256(
        ("kd-auth-v1::" + _app_password() + "::" + _coach_password()).encode("utf-8")).digest()

def make_session_token(role="athlete"):
    payload = str(int(time.time()) + SESSION_DAYS * 86400) + ":" + role
    sig = hmac.new(_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload + "." + sig

def session_role(tok):
    """Return 'athlete' / 'coach' if the cookie is valid, else None."""
    try:
        payload, sig = tok.split(".", 1)
        exp, _, role = payload.partition(":")
        if time.time() > float(exp):
            return None
        good = hmac.new(_auth_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        return (role or "athlete") if hmac.compare_digest(sig, good) else None
    except Exception:
        return None

def is_authed(environ):
    if not _app_password():
        return True  # no password configured -> open (pre-setup behavior)
    for part in (environ.get("HTTP_COOKIE", "") or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == "kd_auth" and session_role(v.strip()):
            return True
    return False

def _login_fails():
    try:
        o = json.loads(kv_get("login_fails") or "{}")
        if time.time() - float(o.get("ts", 0)) > LOCKOUT_WINDOW:
            return {"count": 0, "ts": time.time()}
        return o
    except Exception:
        return {"count": 0, "ts": time.time()}

def login_locked():
    return _login_fails().get("count", 0) >= LOCKOUT_FAILS

def record_login_fail():
    o = _login_fails()
    kv_set("login_fails", json.dumps({"count": int(o.get("count", 0)) + 1, "ts": time.time()}))

def clear_login_fails():
    kv_set("login_fails", json.dumps({"count": 0, "ts": 0}))

def _pw_eq(a, b):
    return bool(b) and hmac.compare_digest(
        hashlib.sha256(a.encode("utf-8")).digest(),
        hashlib.sha256(b.encode("utf-8")).digest())

def check_password(pw):
    """Return the role for a correct password, else None."""
    if _pw_eq(pw, _app_password()):
        return "athlete"
    if _pw_eq(pw, _coach_password()):
        return "coach"
    return None

LOGIN_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Keith's Performance HQ — Sign in</title>
<link href="https://fonts.googleapis.com/css2?family=Sora:wght@600;800&family=Inter:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Inter',sans-serif;min-height:100vh;display:flex;align-items:center;justify-content:center;padding:24px;
background:#070b1d;background-image:radial-gradient(900px 420px at 20% -5%,rgba(99,102,241,.32),transparent 60%),
radial-gradient(700px 380px at 85% 10%,rgba(217,70,239,.20),transparent 55%),linear-gradient(180deg,#070b1d,#0b1130 50%,#070b1d)}
.box{width:100%;max-width:380px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.12);border-radius:20px;
padding:34px 30px;backdrop-filter:blur(12px);box-shadow:0 20px 60px rgba(0,0,0,.5);text-align:center}
.bolt{font-size:2.2rem;margin-bottom:10px}
h1{font-family:'Sora',sans-serif;font-size:1.25rem;font-weight:800;color:#f8fafc;margin-bottom:4px}
h1 span{background:linear-gradient(92deg,#818cf8,#e879f9,#fbbf24);-webkit-background-clip:text;background-clip:text;color:transparent}
.sub{font-size:.78rem;color:#94a3b8;margin-bottom:22px}
input{width:100%;padding:12px 14px;border-radius:12px;border:1.5px solid rgba(255,255,255,.16);background:rgba(255,255,255,.08);
color:#f8fafc;font-size:1rem;outline:none;margin-bottom:12px;transition:border .15s}
input:focus{border-color:#a855f7}
button{width:100%;padding:12px;border:none;border-radius:12px;font-size:.95rem;font-weight:700;cursor:pointer;color:#fff;
background:linear-gradient(92deg,#6366f1,#a855f7);box-shadow:0 6px 22px rgba(139,92,246,.45)}
button:hover{filter:brightness(1.1)}button:disabled{opacity:.6;cursor:wait}
#err{font-size:.78rem;color:#f87171;min-height:18px;margin-top:10px;font-weight:600}
</style></head><body>
<div class="box">
  <div class="bolt">&#9889;</div>
  <h1>KEITH'S <span>PERFORMANCE HQ</span></h1>
  <div class="sub">Private dashboard &mdash; enter your athlete or coach password.</div>
  <input id="pw" type="password" placeholder="Password" autofocus autocomplete="current-password">
  <button id="go">Unlock</button>
  <div id="err"></div>
</div>
<script>
const pw=document.getElementById('pw'),go=document.getElementById('go'),err=document.getElementById('err');
async function login(){
  if(!pw.value){pw.focus();return}
  go.disabled=true;err.textContent='';
  try{
    const r=await fetch('/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:pw.value})});
    const j=await r.json();
    if(j.ok){location.reload()}else{err.textContent=j.error||'Wrong password.';go.disabled=false;pw.select()}
  }catch(e){err.textContent='Network error - try again.';go.disabled=false}
}
go.onclick=login;pw.addEventListener('keydown',e=>{if(e.key==='Enter')login()});
</script></body></html>"""

# ---- token management ----
def whoop_refresh_token():
    return kv_get("whoop_refresh") or _env("WHOOP_REFRESH_TOKEN")

def refresh_whoop():
    # Reuse a cached access token (~50 min) so we don't rotate the single-use
    # refresh token on every page load — concurrent loads were racing the
    # rotation and causing transient 401s mid-request.
    try:
        at = kv_get("whoop_access")
        exp = kv_get("whoop_access_exp")
        if at and exp and time.time() < float(exp) - 120:
            return {"access_token": at}
    except Exception:
        pass
    rt = whoop_refresh_token()
    r = requests.post(WHOOP_TOKEN, data={
        "grant_type": "refresh_token", "refresh_token": rt,
        "client_id": _env("WHOOP_CLIENT_ID"),
        "client_secret": _env("WHOOP_CLIENT_SECRET"),
    }, timeout=30)
    r.raise_for_status()
    data = r.json()
    new = data.get("refresh_token", "")
    if new:
        kv_set("whoop_refresh", new)
    try:
        kv_set("whoop_access", data.get("access_token", ""))
        kv_set("whoop_access_exp", str(time.time() + float(data.get("expires_in", 3600))))
    except Exception:
        pass
    return data

def exchange_auth(code):
    """One-time: exchange a WHOOP authorization code for a fresh refresh token, store in KV."""
    r = requests.post(WHOOP_TOKEN, data={
        "grant_type": "authorization_code",
        "code": code,
        "client_id": _env("WHOOP_CLIENT_ID"),
        "client_secret": _env("WHOOP_CLIENT_SECRET"),
        "redirect_uri": "http://localhost:8080/callback",
    }, timeout=30)
    if r.status_code != 200:
        return None, (str(r.status_code) + " " + r.text)
    rt = r.json().get("refresh_token", "")
    if rt:
        kv_set("whoop_refresh", rt)
    return rt, None

def refresh_strava():
    r = requests.post(STRAVA_TOKEN, data={
        "grant_type": "refresh_token",
        "refresh_token": _env("STRAVA_REFRESH_TOKEN"),
        "client_id": _env("STRAVA_CLIENT_ID"),
        "client_secret": _env("STRAVA_CLIENT_SECRET"),
    }, timeout=30)
    r.raise_for_status()
    return r.json()

# ---- data fetchers ----
def whoop_get(path, token, params=None):
    h = {"Authorization": f"Bearer {token['access_token']}"}
    out, nxt = [], None
    while True:
        p = dict(params or {}, limit=25)
        if nxt:
            p["nextToken"] = nxt
        r = requests.get(WHOOP_BASE + path, headers=h, params=p, timeout=30)
        if r.status_code == 429:
            time.sleep(3); continue
        r.raise_for_status()
        d = r.json()
        out.extend(d.get("records", []))
        nxt = d.get("next_token")
        if not nxt:
            break
    return out

def fetch_whoop(token):
    start = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recs = whoop_get("/v2/recovery", token, {"start": start})
    sleeps = whoop_get("/v2/activity/sleep", token, {"start": start})
    smap = {s["cycle_id"]: s for s in sleeps if not s.get("nap")}
    out = {}
    for rec in recs:
        if rec.get("score_state") != "SCORED":
            continue
        sc = rec.get("score", {})
        slp = smap.get(rec["cycle_id"])
        date = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00")).astimezone(ET).strftime("%Y-%m-%d")
        hrs = None
        if slp and slp.get("score_state") == "SCORED":
            ss = slp["score"].get("stage_summary", {})
            ms = ss.get("total_in_bed_time_milli", 0) - ss.get("total_awake_time_milli", 0)
            hrs = round(ms / 3_600_000, 2) if ms else None
        out[date] = {"hrv": sc.get("hrv_rmssd_milli"), "rhr": sc.get("resting_heart_rate"),
                     "recovery": sc.get("recovery_score"), "sleep": hrs}
    return out

def fetch_strava(token):
    after = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp())
    h = {"Authorization": f"Bearer {token['access_token']}"}
    acts, page = [], 1
    while True:
        r = requests.get(STRAVA_BASE + "/athlete/activities", headers=h,
                         params={"after": after, "per_page": 200, "page": page}, timeout=30)
        if r.status_code == 429:
            time.sleep(3); continue
        r.raise_for_status()
        d = r.json()
        if not d:
            break
        acts.extend(d)
        if len(d) < 200:
            break
        page += 1
    by_day = {}
    for a in acts:
        k = a["start_date_local"][:10]
        d = by_day.setdefault(k, {"effort": 0, "kj": 0, "secs": 0, "rides": []})
        d["effort"] += (a.get("suffer_score") or 0)
        d["kj"] += (a.get("kilojoules") or 0)
        d["secs"] += (a.get("moving_time") or 0)
        w = a.get("weighted_average_watts") or a.get("average_watts") or 0
        mt = a.get("moving_time") or 0
        if w and mt:
            d["rides"].append({"mt": mt, "w": w})
    return by_day

def fetch_tp():
    url = _env("TP_ICAL_URL")
    if not url:
        return []
    try:
        r = requests.get(url, timeout=30); r.raise_for_status()
    except Exception:
        return []
    raw = r.text.replace("\r\n", "\n")
    lines = []
    for ln in raw.split("\n"):
        if ln.startswith(" ") and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    events, cur = [], None
    for ln in lines:
        if ln.startswith("BEGIN:VEVENT"):
            cur = {}
        elif ln.startswith("END:VEVENT"):
            if cur is not None:
                events.append(cur)
            cur = None
        elif cur is not None:
            if ln.startswith("DTSTART"):
                cur["date"] = ln.split(":", 1)[-1].strip()[:8]
            elif ln.startswith("SUMMARY"):
                cur["summary"] = ln.split(":", 1)[-1].strip()
            elif ln.startswith("DESCRIPTION"):
                dd = ln.split(":", 1)[-1].strip()
                cur["desc"] = dd.replace("\\n", " ").replace("\\,", ",")[:160]
    today = datetime.now(ET).strftime("%Y%m%d")
    out = [e for e in sorted(events, key=lambda x: x.get("date", ""))
           if e.get("date", "") >= today and e.get("summary")]
    return out[:12]

# ---- Withings (weight / body comp, blood pressure, steps) ----
WITHINGS_AUTH     = "https://account.withings.com/oauth2_user/authorize2"
WITHINGS_TOKEN    = "https://wbsapi.withings.net/v2/oauth2"
WITHINGS_MEASURE  = "https://wbsapi.withings.net/measure"
WITHINGS_ACTIVITY = "https://wbsapi.withings.net/v2/measure"
WITHINGS_SCOPE    = "user.metrics,user.activity"

def withings_configured():
    return bool(_env("WITHINGS_CLIENT_ID") and _env("WITHINGS_CLIENT_SECRET"))

def withings_authorize_url(redirect):
    return (WITHINGS_AUTH + "?response_type=code"
            "&client_id=" + urllib.parse.quote(_env("WITHINGS_CLIENT_ID")) +
            "&scope=" + urllib.parse.quote(WITHINGS_SCOPE) +
            "&redirect_uri=" + urllib.parse.quote(redirect) +
            "&state=withings")

def exchange_withings_auth(code, redirect):
    try:
        r = requests.post(WITHINGS_TOKEN, data={
            "action": "requesttoken", "grant_type": "authorization_code", "code": code,
            "client_id": _env("WITHINGS_CLIENT_ID"), "client_secret": _env("WITHINGS_CLIENT_SECRET"),
            "redirect_uri": redirect,
        }, timeout=30)
        j = r.json()
    except Exception as e:
        return False, str(e)[:160]
    if j.get("status") != 0:
        return False, json.dumps(j)[:200]
    b = j.get("body", {})
    if b.get("refresh_token"):
        kv_set("withings_refresh", b["refresh_token"])
    if b.get("userid") is not None:
        kv_set("withings_userid", str(b.get("userid")))
    return True, None

def refresh_withings():
    rt = kv_get("withings_refresh") or _env("WITHINGS_REFRESH_TOKEN")
    if not rt or not withings_configured():
        return None
    try:
        r = requests.post(WITHINGS_TOKEN, data={
            "action": "requesttoken", "grant_type": "refresh_token", "refresh_token": rt,
            "client_id": _env("WITHINGS_CLIENT_ID"), "client_secret": _env("WITHINGS_CLIENT_SECRET"),
        }, timeout=30)
        j = r.json()
    except Exception:
        return None
    if j.get("status") != 0:
        return None
    b = j.get("body", {})
    if b.get("refresh_token"):
        kv_set("withings_refresh", b["refresh_token"])
    return b.get("access_token")

def fetch_withings(access_token):
    """Returns {date: {weight, fat, systolic, diastolic, bp_hr, bp_ts, steps}}."""
    out = {}
    if not access_token:
        return out
    hdr = {"Authorization": "Bearer " + access_token}
    start = int((datetime.now(timezone.utc) - timedelta(days=DAYS)).timestamp())
    end = int(datetime.now(timezone.utc).timestamp())
    # measures: weight(1), lean/fat-free(5), fat%(6), fat mass(8), diastolic(9),
    # systolic(10), hr(11), muscle(76), hydration/water(77), bone(88), PWV(91), visceral(170)
    LB = 2.2046226
    try:
        r = requests.post(WITHINGS_MEASURE, headers=hdr, data={
            "action": "getmeas", "meastypes": "1,5,6,8,9,10,11,76,77,88,91,170", "category": "1",
            "startdate": start, "enddate": end,
        }, timeout=30)
        for g in r.json().get("body", {}).get("measuregrps", []):
            ts = g.get("date", 0)
            dkey = datetime.fromtimestamp(ts, ET).strftime("%Y-%m-%d")
            rec = out.setdefault(dkey, {})
            for m in g.get("measures", []):
                val = m["value"] * (10 ** m["unit"])
                t = m["type"]
                if t == 1:
                    rec["weight"] = round(val * LB, 1)
                elif t == 5:
                    rec["leanMass"] = round(val * LB, 1)
                elif t == 6:
                    rec["fat"] = round(val, 1)
                elif t == 8:
                    rec["fatMass"] = round(val * LB, 1)
                elif t == 76:
                    rec["muscle"] = round(val * LB, 1)
                elif t == 77:
                    rec["water"] = round(val * LB, 1)
                elif t == 88:
                    rec["bone"] = round(val * LB, 1)
                elif t == 91:
                    rec["pwv"] = round(val, 1)
                elif t == 170:
                    rec["visceral"] = round(val, 1)
                elif t == 10:
                    rec["systolic"] = round(val); rec["bp_ts"] = ts
                elif t == 9:
                    rec["diastolic"] = round(val); rec["bp_ts"] = ts
                elif t == 11:
                    rec["bp_hr"] = round(val)
    except Exception:
        pass
    # activity: steps
    try:
        sday = (datetime.now(timezone.utc) - timedelta(days=DAYS)).strftime("%Y-%m-%d")
        eday = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        r = requests.post(WITHINGS_ACTIVITY, headers=hdr, data={
            "action": "getactivity", "startdateymd": sday, "enddateymd": eday,
            "data_fields": "steps,distance,calories",
        }, timeout=30)
        for a in r.json().get("body", {}).get("activities", []):
            dkey = a.get("date")
            if dkey and a.get("steps") is not None:
                out.setdefault(dkey, {})["steps"] = a.get("steps")
    except Exception:
        pass
    return out

# ---- payload assembly with cache ----
def build_payload():
    wt = refresh_whoop()
    st = refresh_strava()
    whoop = fetch_whoop(wt)
    strava = fetch_strava(st)
    tp = fetch_tp()
    withings = {}
    try:
        wa = refresh_withings()
        if wa:
            withings = fetch_withings(wa)
    except Exception:
        withings = {}
    now = datetime.now(ET)
    days = []
    for i in range(DAYS - 1, -1, -1):
        d = now - timedelta(days=i)
        k = d.strftime("%Y-%m-%d")
        w = whoop.get(k, {})
        s = strava.get(k, {})
        wi = withings.get(k, {})
        days.append({
            "date": k, "label": d.strftime("%b ") + str(d.day),
            "hrv": w.get("hrv"), "rhr": w.get("rhr"),
            "recovery": w.get("recovery"), "sleep": w.get("sleep"),
            "effort": s.get("effort", 0), "kj": s.get("kj", 0),
            "secs": s.get("secs", 0), "rides": s.get("rides", []),
            "weight": wi.get("weight"), "fat": wi.get("fat"), "steps": wi.get("steps"),
            "leanMass": wi.get("leanMass"), "fatMass": wi.get("fatMass"), "muscle": wi.get("muscle"),
            "water": wi.get("water"), "bone": wi.get("bone"), "pwv": wi.get("pwv"), "visceral": wi.get("visceral"),
            "systolic": wi.get("systolic"), "diastolic": wi.get("diastolic"),
            "bp_hr": wi.get("bp_hr"), "bp_ts": wi.get("bp_ts"),
        })
    return {"generated": now.strftime("%B %d, %Y %I:%M %p ET").lstrip("0"), "days": days, "tp": tp,
            "withings": withings_configured() and bool(kv_get("withings_refresh") or _env("WITHINGS_REFRESH_TOKEN"))}

def get_payload(fresh):
    if not fresh:
        cached = kv_get("payload")
        if cached:
            try:
                obj = json.loads(cached)
                if time.time() - obj.get("ts", 0) < CACHE_TTL:
                    return obj["data"], True
            except Exception:
                pass
    data = build_payload()
    try:
        kv_set("payload", json.dumps({"ts": time.time(), "data": data}))
    except Exception:
        pass
    return data, False

# ---- HTML ----
PAGE_URL = "https://raw.githubusercontent.com/DRG6621/whoop-dashboard/main/page.html"
_PAGE_CACHE = {"html": None, "ts": 0}

def get_page():
    if _PAGE_CACHE["html"] and (time.time() - _PAGE_CACHE["ts"] < 300):
        return _PAGE_CACHE["html"]
    try:
        r = requests.get(PAGE_URL + "?cb=" + str(int(time.time() // 60)), timeout=20)
        r.raise_for_status()
        _PAGE_CACHE["html"] = r.text
        _PAGE_CACHE["ts"] = time.time()
    except Exception:
        if not _PAGE_CACHE["html"]:
            raise
    return _PAGE_CACHE["html"]

def render(data, cached):
    page = get_page()
    payload = json.dumps(data)
    return page.replace("__PAYLOAD__", payload).replace("__CACHED__", "cached" if cached else "fresh")

# ---- AI coach (Anthropic Messages API via requests; no SDK needed) ----
AI_ENDPOINT = "https://api.anthropic.com/v1/messages"

DAILY_SYSTEM = (
    "You are Keith's experienced, supportive cycling coach. Using the athlete data provided, "
    "write today's coaching in 3-5 tight sentences: (1) the headline call for today "
    "(train hard / ride steady / recover) grounded in the readiness and form (TSB) numbers; "
    "(2) the specific session, tied to his FTP watts/zones and any planned workout; "
    "(3) one fueling or recovery cue. Be concrete and encouraging. Respect the readiness signal: "
    "if readiness is low or he notes illness or heavy fatigue, protect recovery and do NOT prescribe "
    "hard intervals. Never give medical advice; for pain or illness advise easy days and a professional. "
    "If an OFF-PLAN FLAG is present, acknowledge he's going off-script and coach the deviation: keep it "
    "aligned with today's readiness (don't let an off-plan ride turn a needed recovery day into a hard one); "
    "if he's set on it, give a specific intensity/duration cap and note the trade-off for the plan. "
    "Keep it under ~120 words, plain text, no preamble or sign-off."
)
CHAT_SYSTEM = (
    "You are Keith's knowledgeable, supportive cycling coach. Answer his questions about training, "
    "recovery, pacing, and fueling using the current athlete data provided. Be concise, specific, and "
    "practical - reference his actual numbers (readiness, form/TSB, FTP watts, planned workouts) when "
    "relevant. Respect the readiness signal and never prescribe hard efforts on a low-recovery day. "
    "You are not a doctor or dietitian: for pain, illness, or medical questions, recommend rest and a "
    "professional. If an OFF-PLAN FLAG is present, coach the deviation honestly - help him make the off-plan "
    "choice fit his readiness and flag the cost to his plan if it's a bad idea. "
    "You may discuss general timing of common training supplements (caffeine, creatine, protein, carbs, "
    "beta-alanine, electrolytes) relative to his sessions, but do not give medical dosing advice or treat "
    "health conditions - defer those to a doctor or dietitian. "
    "Keep answers short (a few sentences) unless he asks for more detail."
)

def _ctx_text(ctx):
    if not isinstance(ctx, dict):
        return ""
    L = [
        "Date: %s" % ctx.get("date"),
        "WHOOP recovery: %s%% | HRV: %s (baseline ~%s) | RHR: %s | last sleep: %sh (avg %sh)" % (
            ctx.get("recovery"), ctx.get("hrv"), ctx.get("hrvBaseline"), ctx.get("rhr"),
            ctx.get("sleepH"), ctx.get("avgSleep")),
        "Combined readiness: %s/100 (base %s from WHOOP+Strava)" % (ctx.get("readiness"), ctx.get("baseReadiness")),
        "7-day load: %s relative-effort over %sh riding" % (ctx.get("sevenDayLoad"), ctx.get("sevenDayHours")),
        "PMC - Fitness(CTL) %s, Fatigue(ATL) %s, Form(TSB) %s, ramp %s CTL/wk" % (
            ctx.get("ctl"), ctx.get("atl"), ctx.get("tsb"), ctx.get("rampPerWeek")),
        "FTP %sW, body weight %s lb%s" % (
            ctx.get("ftp"), ctx.get("weightLb"),
            (" (target weight %s lb)" % ctx.get("targetWeight")) if ctx.get("targetWeight") else ""),
    ]
    if ctx.get("eventName") or ctx.get("goalDate"):
        L.append("LONG-TERM GOAL: %s%s%s -- all coaching and nutrition should build toward this." % (
            ("key event '%s' on %s" % (ctx.get("eventName"), ctx.get("eventDate"))) if ctx.get("eventName") else "",
            (" (%s days out)" % ctx.get("daysToEvent")) if ctx.get("daysToEvent") is not None else "",
            ("; weight-goal date %s" % ctx.get("goalDate")) if ctx.get("goalDate") else ""))
    if ctx.get("stepsToday") is not None:
        L.append("Steps today: %s (7-day avg %s)" % (ctx.get("stepsToday"), ctx.get("steps7avg")))
    if ctx.get("bpSys"):
        L.append("Latest blood pressure: %s/%s mmHg%s%s" % (
            ctx.get("bpSys"), ctx.get("bpDia"),
            (" HR %s" % ctx.get("bpHr")) if ctx.get("bpHr") else "",
            (" measured %s" % ctx.get("bpWhen")) if ctx.get("bpWhen") else ""))
    bc = []
    for lbl, key, unit in (("body fat", "fatPct", "%"), ("fat mass", "fatMass", " lb"),
                           ("muscle", "muscle", " lb"), ("water", "water", " lb"),
                           ("bone", "bone", " lb"), ("visceral fat", "visceral", "")):
        v = ctx.get(key)
        if v is not None and v != "":
            bc.append("%s %s%s" % (lbl, v, unit))
    if bc:
        L.append("Body composition (Withings scale): " + ", ".join(bc))
    ci = []
    for lbl, key in (("energy", "energy"), ("soreness", "soreness"), ("fatigue", "fatigue"), ("sleep quality", "sleepQuality"), ("motivation", "motivation")):
        v = ctx.get(key)
        if v:
            ci.append("%s %s/5" % (lbl, v))
    if ci:
        L.append("Today's check-in: " + ", ".join(ci))
    if ctx.get("illness"):
        L.append("*** ILLNESS SYMPTOMS reported today -- prioritize recovery, no hard training. ***")
    bw = (ctx.get("bloodwork") or "").strip()
    if bw:
        L.append("Recent bloodwork (%s): %s" % (ctx.get("bloodworkDate") or "recent", bw.replace("\n", "; ")))
    s = ctx.get("supplements") or {}
    if isinstance(s, dict):
        sparts = []
        for lbl, key in (("morning", "morning"), ("pre-workout", "pre"), ("post-workout", "post"), ("night", "night")):
            v = (s.get(key) or "").strip()
            if v:
                sparts.append("%s: %s" % (lbl, v.replace("\n", ", ")))
        if sparts:
            L.append("Supplement stack -- " + " | ".join(sparts))
    if ctx.get("coachNote"):
        L.append("Coach's note: %s" % ctx.get("coachNote"))
    if ctx.get("yourNote"):
        L.append("Athlete's note today: %s" % ctx.get("yourNote"))
    if ctx.get("offPlan"):
        note = ctx.get("offPlanNote") or ""
        L.append("*** OFF-PLAN FLAG: the athlete is deviating from the scheduled workout today.%s ***" % (
            (" What they're doing instead: %s." % note) if note else ""))
    if ctx.get("extraWorkouts"):
        L.append("Extra/weekend workouts not in TrainingPeaks: %s" % str(ctx.get("extraWorkouts"))[:300])
    if ctx.get("otherActivities"):
        L.append("Other non-bike activity today: %s" % str(ctx.get("otherActivities"))[:300])
    L.append("Next planned workout: %s" % (ctx.get("nextWorkout") or "none scheduled"))
    up = ctx.get("upcoming") or []
    if isinstance(up, list) and up:
        L.append("Upcoming: " + "; ".join(
            ("%s %s" % (u.get("date", ""), u.get("summary", ""))).strip() for u in up[:5] if isinstance(u, dict)))
    return "\n".join(str(x) for x in L)

def anthropic_call(system, messages, max_tokens=700, timeout=45):
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        return None, ("AI coaching isn't set up yet. Add an ANTHROPIC_API_KEY in this project's "
                      "Vercel Environment Variables, redeploy, then reload.")
    model = _env("AI_MODEL") or "claude-sonnet-5"
    try:
        r = requests.post(AI_ENDPOINT, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, data=json.dumps({
            "model": model, "max_tokens": max_tokens, "system": system, "messages": messages,
        }), timeout=timeout)
    except Exception as e:
        return None, "Coach request failed: " + str(e)[:120]
    if r.status_code != 200:
        try:
            em = r.json().get("error", {}).get("message", "")
        except Exception:
            em = r.text[:160]
        return None, "Coach error (%s): %s" % (r.status_code, em[:170])
    try:
        j = r.json()
        parts = j.get("content", [])
        txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        if not txt:
            return None, ("The AI ran out of room before writing its answer (stop: %s) - hit the button again."
                          % j.get("stop_reason"))
        return txt, None
    except Exception:
        return None, "Coach parse error"

NUTRI_CHAT_SYSTEM = (
    "You are Keith's dedicated sports NUTRITIONIST (separate from his training coach). Your job: help him hit "
    "his target weight WITHOUT losing cycling power, using HIS coach's diet system below as the source of truth. "
    "Answer questions about meal swaps, portions, food choices, fueling timing around rides, grocery/prep, and "
    "weight-goal pacing - always concrete with grams/amounts, preferring his own recipes. Never under-fuel "
    "training; deficits belong on rest/easy days. You are not a doctor or dietitian - medical questions, "
    "big calorie changes, or supplement dosing go to his doctor and coach Jeremiah Bishop. "
    "Keep answers short and practical unless he asks for detail."
)

def handle_coach(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        n = 0
    raw = environ["wsgi.input"].read(n) if n > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        req = {}
    ctxt = _ctx_text(req.get("context", {}))
    if req.get("type") == "daily":
        h = hashlib.sha256(ctxt.encode("utf-8")).hexdigest()[:16]
        ck = "coach_daily:" + h
        if not req.get("force"):
            c = kv_get(ck)
            if c:
                return {"text": c, "cached": True}
        text, err = anthropic_call(DAILY_SYSTEM, [
            {"role": "user", "content": ctxt + "\n\nGive me today's coaching now."}], 700)
        if err:
            return {"error": err}
        try:
            kv_set(ck, text)
        except Exception:
            pass
        return {"text": text}
    # chat
    msg = (req.get("message") or "").strip()[:1200]
    if not msg:
        return {"error": "Say something to your coach."}
    messages = []
    for m in (req.get("history") or [])[-8:]:
        role = m.get("role") if isinstance(m, dict) else None
        content = (m.get("content") or "")[:2000] if isinstance(m, dict) else ""
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})
    messages.append({"role": "user", "content": msg})
    if req.get("type") == "nutrition":
        sys = NUTRI_CHAT_SYSTEM + "\n\n" + DIET_KNOWLEDGE + "\n\nCurrent athlete data:\n" + ctxt
        plan = (req.get("planExcerpt") or "").strip()
        if plan:
            sys += "\n\nHis current weekly meal plan (excerpt):\n" + plan[:1800]
    else:
        sys = CHAT_SYSTEM + "\n\nCurrent athlete data:\n" + ctxt
    text, err = anthropic_call(sys, messages, 700)
    if err:
        return {"error": err}
    return {"text": text}

# ---- Nutrition Consultant (built from Keith's coach-written diet plans) ----
DIET_KNOWLEDGE = """KEITH'S DIET SYSTEM (from his coach's Fat Loss & Maintenance plans - follow this structure exactly):
Every meal: 25g protein from lean sources + 2 cups veggies. Bedtime: 25g casein protein in water + 7g fat.
Carbs scale with ride duration. Meal templates (training day, eat ~2h before riding):
- PRE-RIDE meal: 7g fat + carbs: 75g (1hr ride), 90g (2hr), 100g (3-4hr).
- DURING ride (workout carbs: sports drink/gels): 20g (1hr), 150g (2hr), 300g (3hr), 400g (4hr).
- IMMEDIATELY POST-RIDE: protein + veggies + carbs matching pre-ride (75-100g). Never skip or delay.
- Later meals: 40-70g carbs each (higher after longer rides), 3-4h apart.
FAT LOSS mode: fat only at pre-ride + bedtime; later meals lean; REST DAY = 5 meals, 25g carbs each, fat at meals 1/4/bedtime; suggested 15-20 min easy walk.
MAINTENANCE mode: 7g fat at nearly every meal; REST DAY = 5 meals, 40g carbs each.
Food lists - Protein: chicken/turkey breast, tuna, salmon/fish, lean beef/steak, shrimp, ground turkey, egg whites, fat-free Greek yogurt/cottage cheese, tofu, skim milk.
Veggies: broccoli, spinach, lettuce, onions, tomatoes, peppers, asparagus, zucchini, cauliflower, celery, cucumbers.
Fats: nuts (cashews, almonds, walnuts, pistachios), nut butters, avocado, olive/canola/flaxseed oil.
Carbs: rice, oatmeal (steel cut), sweet potatoes, quinoa, beans/lentils, whole grain bread/pasta/wraps, corn, fruit.
Workout carbs: Gatorade/Powerade, fruit juice, coconut water, Vitargo-type products.
Rules: wait 1h+ after eating before training; sip workout carbs during the ride; eat post-ride meal immediately.
KEITH'S OWN RECIPES (from his nutrition plan - PREFER these in daily menus, scale carbs up/down to hit the template):
1. Overnight Berry Oats [446kcal P35 C43 F10]: oats 45g, protein powder 1 scoop, almond milk 1 cup, blueberries 120g, cinnamon, chia 1 tbsp.
2. Grilled Turkey + Quinoa & Mixed Veg [595kcal P54 C40 F20]: turkey breast 190g, quinoa 55g dry, mixed veg 95g, avocado 50g, olive oil 1 tsp.
3. Greek Yogurt Protein Pudding [308kcal P41 C14 F10]: protein powder 1 scoop, Greek yogurt 180g, honey 1 tsp.
4. Pan-Seared Fish & Green Veg [506kcal P57 C47 F9]: white fish 250g, brown rice 50g dry, broccoli 90g, green beans 95g, asparagus 120g, olive oil 1 tsp.
5. High-Protein Omelette & Veg [376kcal P36 C28 F12]: 2 eggs + 115g egg whites, spinach, mushrooms 120g, tomato, onion, 2 slices whole wheat bread.
6. Tuna Salad Bowl [520kcal P44 C59 F11]: canned tuna, whole wheat pasta 80g dry, light mayo 2 tbsp, cucumber, tomato, lime.
7. Grilled Steak & Sweet Potato Mash [626kcal P56 C50 F19]: sirloin 170g, sweet potato 250g, carrots 75g, red onion, olive oil 1 tsp.
8. Protein Shake & Fruit [330kcal P53 C16 F5]: protein powder 2 scoops, almond milk 1.5 cups, watermelon 200g."""

MEALPLAN_SYSTEM = (
    "You are Keith's nutrition consultant, building his week from HIS coach's actual diet system (provided below). "
    "Goal: hit his target weight WITHOUT losing cycling power - never under-fuel training days; create any deficit "
    "on rest/easy days, keep protein high, always fuel the work and the recovery. "
    "Using his upcoming TrainingPeaks week, map each day to the right meal template by ride duration "
    "(rest-day template on days with no ride). Output EXACTLY this structure in markdown:\n"
    "**WEEK AT A GLANCE** - one line per day: day, workout, template used, ~carb total.\n"
    "**DAILY PLANS** - for each day: each meal with time, what to eat as a concrete sample meal with amounts "
    "(e.g. '6oz grilled chicken, 2 cups broccoli, 1.5 cups cooked rice'), matching the template's protein/veggie/fat/carb targets. "
    "END EVERY MEAL LINE with its macro breakdown in brackets: [P 25g / C 75g / F 7g / ~450 kcal]. "
    "On training days include a 'During ride' entry showing workout carbs: total grams, grams per hour, and ~kcal "
    "(e.g. 'During ride (2h): 150g workout carbs (~75g/hr, ~600 kcal) - sports drink + gels, sip every 15-20 min'). "
    "End each day with a DAY TOTAL macro line.\n"
    "**3 RECIPES** - three simple recipes for the week using the plan's foods, with ingredients + steps (5-8 steps max).\n"
    "**MEAL PREP** - a short Sunday/midweek prep strategy (batch cooking, portions).\n"
    "**SHOPPING LIST** - consolidated by category (protein/produce/carbs/fats/workout fuel) with rough quantities for the week.\n"
    "Be concrete and practical. No medical advice; note he should confirm weight-loss pace with his coach/doctor if asked.\n"
    "STEERING RULE: the weight you receive is a multi-day smoothed average - day-to-day scale readings swing "
    "with water/glycogen and mean little. Adjust the plan like steering a big ship: small, gradual changes only "
    "(never shift daily carbs/deficit more than ~10% vs the prior week because of weight movement), and never "
    "cut fueling on training days to chase the scale.\n\n"
    + DIET_KNOWLEDGE
)

def handle_meal_plan(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        n = 0
    raw = environ["wsgi.input"].read(n) if n > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        req = {}
    ctx = req.get("context") or {}
    mode = "fat loss" if (req.get("mode") or "") == "fatloss" else "maintenance"
    tw = req.get("targetWeight")
    up = ctx.get("upcoming") or []
    week = "; ".join(("%s %s" % (u.get("date", ""), u.get("summary", ""))).strip()
                     for u in up[:7] if isinstance(u, dict)) or "no planned workouts found"
    extra = (ctx.get("extraWorkouts") or "").strip()
    if extra:
        week += ". ADDITIONAL workouts not in TrainingPeaks (treat as training days, fuel accordingly): " + extra[:400]
    other = (ctx.get("otherActivities") or "").strip()
    if other:
        week += ". Other non-bike activity (hiking/walking/strength/yard work - adds energy demand): " + other[:300]
    goal_bits = ""
    if ctx.get("eventName"):
        goal_bits += " KEY EVENT: %s on %s (%s days out) - periodize nutrition toward being light AND powerful for it." % (
            ctx.get("eventName"), ctx.get("eventDate"), ctx.get("daysToEvent"))
    if ctx.get("goalDate"):
        goal_bits += " Weight-goal deadline: %s - pace the deficit to land the target by then without under-fueling." % ctx.get("goalDate")
    wt = ctx.get("smoothWeightLb") or ctx.get("weightLb")
    user = (
        "Mode: %s. Current weight: %s lb (7-day smoothed average; body fat %s%%, muscle %s lb). Target weight: %s lb. FTP %sW.%s\n"
        "Upcoming TrainingPeaks week: %s\n"
        "Build my week now." % (
            mode, wt, ctx.get("fatPct"), ctx.get("muscle"),
            tw or "not set", ctx.get("ftp"), goal_bits, week)
    )
    h = hashlib.sha256((mode + str(tw) + week + str(wt) + goal_bits).encode("utf-8")).hexdigest()[:16]
    ck = "meal_plan_v3:" + h
    if not req.get("force"):
        c = kv_get(ck)
        if c and len(c) > 400:
            return {"text": c, "cached": True}
    text, err = anthropic_call(MEALPLAN_SYSTEM, [{"role": "user", "content": user}], 24000, timeout=240)
    if err:
        return {"error": err}
    if len(text) > 400:
        try:
            kv_set(ck, text)
        except Exception:
            pass
    return {"text": text}

# ---- Ride analysis: latest Strava ride streams -> climbs/VAM/decoupling/bests ----
def _rollmax(vals, win):
    n = len(vals)
    if n < win:
        return None
    s = sum(vals[:win]); best = s
    for i in range(win, n):
        s += vals[i] - vals[i - win]
        if s > best:
            best = s
    return round(best / win)

def analyze_streams(t, w, hr, alt, dist, ftp, weight_lb):
    out = {}
    n = len(t) if t else 0
    if n < 120:
        return None
    w = [(x or 0) for x in (w or [0] * n)]
    hr = [(x or 0) for x in (hr or [0] * n)]
    alt = alt or [0] * n
    dist = dist or [0] * n
    # normalized power (30s rolling)
    if any(w):
        roll = []
        s = 0.0
        from collections import deque
        q = deque()
        for x in w:
            q.append(x); s += x
            if len(q) > 30:
                s -= q.popleft()
            roll.append(s / len(q))
        np_ = (sum(r ** 4 for r in roll) / len(roll)) ** 0.25
        out["np"] = round(np_)
        out["avgW"] = round(sum(w) / n)
        out["if"] = round(np_ / ftp, 2) if ftp else None
        dur_h = (t[-1] - t[0]) / 3600.0
        out["tss"] = round((dur_h * np_ * (np_ / ftp)) / ftp * 100) if ftp else None
        out["kj"] = round(sum(w) / 1000.0 * ((t[-1] - t[0]) / n))
        out["bests"] = {"1min": _rollmax(w, 60), "5min": _rollmax(w, 300), "20min": _rollmax(w, 1200)}
    hrs = [x for x in hr if x > 0]
    if hrs:
        out["avgHR"] = round(sum(hrs) / len(hrs)); out["maxHR"] = max(hrs)
    # decoupling (Pw:HR drift): halves of the working portion
    idx = [i for i in range(n) if w[i] > 50 and hr[i] > 90]
    if len(idx) > 600:
        half = len(idx) // 2
        a, b = idx[:half], idx[half:]
        def ratio(ix):
            mw = sum(w[i] for i in ix) / len(ix); mh = sum(hr[i] for i in ix) / len(ix)
            return (mh / mw) if mw else None
        r1, r2 = ratio(a), ratio(b)
        if r1 and r2:
            out["decouplingPct"] = round((r2 / r1 - 1) * 100, 1)
    # climbs: smoothed altitude, sustained grade
    sm = []
    for i in range(n):
        lo = max(0, i - 7); hi = min(n, i + 8)
        sm.append(sum(alt[lo:hi]) / (hi - lo))
    climbs = []
    i = 0
    while i < n - 30:
        j = i + 15
        dd = dist[j] - dist[i]; da = sm[j] - sm[i]
        grade = (da / dd * 100) if dd > 8 else 0
        if grade > 2.5:
            start = i
            k = j
            while k < n - 15:
                dd2 = dist[k + 15] - dist[k]
                g2 = ((sm[k + 15] - sm[k]) / dd2 * 100) if dd2 > 8 else 0
                if g2 < 1.0:
                    break
                k += 15
            gain = sm[k] - sm[start]
            if gain >= 25:
                sec = t[k] - t[start]
                dkm = (dist[k] - dist[start]) / 1000.0
                seg_w = [w[x] for x in range(start, k) if w[x] > 0]
                seg_h = [hr[x] for x in range(start, k) if hr[x] > 0]
                climbs.append({
                    "startMin": round(t[start] / 60), "min": round(sec / 60.0, 1),
                    "km": round(dkm, 2), "gainFt": round(gain * 3.281),
                    "grade": round((gain / (dkm * 1000) * 100), 1) if dkm > 0 else None,
                    "vam": round(gain / (sec / 3600.0)) if sec > 60 else None,
                    "avgW": round(sum(seg_w) / len(seg_w)) if seg_w else None,
                    "wkg": round((sum(seg_w) / len(seg_w)) / (weight_lb / 2.205), 2) if seg_w and weight_lb else None,
                    "avgHR": round(sum(seg_h) / len(seg_h)) if seg_h else None,
                })
            i = k + 30
        else:
            i += 15
    climbs.sort(key=lambda c: -(c["gainFt"] or 0))
    out["climbs"] = climbs[:6]
    return out

RIDE_INSIGHT_SYSTEM = (
    "You are Keith's race engineer reviewing his latest ride file. Given the computed metrics, give 3-4 short, "
    "punchy insights (one line each, start each with an emoji): what stood out (power bests vs FTP, VAM on "
    "climbs, pacing), what the Pw:HR decoupling says about aerobic durability/fueling (under ~5% = strong, "
    "5-8% = fatigue or under-fueling creeping in, >8% = faded hard), and one thing to do next time. "
    "Concrete numbers, zero fluff, coach-to-racer voice. Plain text lines only."
)

def handle_ride_analysis(environ):
    try:
        nlen = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        nlen = 0
    raw = environ["wsgi.input"].read(nlen) if nlen > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        req = {}
    ctx = req.get("context") or {}
    ftp = float(ctx.get("ftp") or 300)
    weight = float(ctx.get("weightLb") or 178)
    st = refresh_strava()
    h = {"Authorization": "Bearer " + st["access_token"]}
    r = requests.get(STRAVA_BASE + "/athlete/activities", headers=h, params={"per_page": 10}, timeout=30)
    r.raise_for_status()
    act = None
    for a in r.json():
        if a.get("moving_time", 0) > 1200 and "Ride" in (a.get("type") or ""):
            act = a; break
    if not act:
        return {"error": "No recent ride found on Strava."}
    aid = act["id"]
    ck = "ride_analysis_v1:%s" % aid
    if not req.get("force"):
        c = kv_get(ck)
        if c:
            try:
                return json.loads(c)
            except Exception:
                pass
    r = requests.get(STRAVA_BASE + "/activities/%s/streams" % aid, headers=h,
                     params={"keys": "time,watts,heartrate,altitude,distance", "key_by_type": "true"}, timeout=45)
    if r.status_code != 200:
        return {"error": "Could not fetch ride streams (%s)." % r.status_code}
    s = r.json()
    def g(k):
        return (s.get(k) or {}).get("data")
    m = analyze_streams(g("time"), g("watts"), g("heartrate"), g("altitude"), g("distance"), ftp, weight)
    if not m:
        return {"error": "Ride too short or missing data to analyze."}
    result = {
        "name": act.get("name"), "date": (act.get("start_date_local") or "")[:10],
        "distMi": round((act.get("distance") or 0) / 1609.34, 1),
        "movingMin": round((act.get("moving_time") or 0) / 60),
        "elevFt": round((act.get("total_elevation_gain") or 0) * 3.281),
        "metrics": m, "ftp": ftp,
    }
    # AI race-engineer insights
    try:
        summary = json.dumps(result)[:3000]
        text, err = anthropic_call(RIDE_INSIGHT_SYSTEM, [
            {"role": "user", "content": "FTP %sW, weight %s lb. Ride data: %s\n\nGive me the debrief." % (ftp, weight, summary)}], 2000, timeout=90)
        if text:
            result["insight"] = text
    except Exception:
        pass
    try:
        kv_set(ck, json.dumps(result))
    except Exception:
        pass
    return result

# ---- Race Planner: GPX course + rival intel -> AI race strategy ----
RIDE_TYPES = {
    # Crr rolling resistance, CdA drag, bike+kit lb, descent speed cap km/h
    "road":   {"crr": 0.0045, "cda": 0.32, "bike_lb": 19, "vmax": 58},
    "gravel": {"crr": 0.0085, "cda": 0.36, "bike_lb": 21, "vmax": 46},
    "mtb":    {"crr": 0.0140, "cda": 0.42, "bike_lb": 26, "vmax": 38},
    "cx":     {"crr": 0.0110, "cda": 0.38, "bike_lb": 18, "vmax": 40},
}

def parse_gpx(txt):
    """Return list of (lat, lon, ele_m). Handles any GPX namespace."""
    import xml.etree.ElementTree as ET
    root = ET.fromstring(txt)
    pts = []
    for el in root.iter():
        if el.tag.split("}")[-1] in ("trkpt", "rtept"):
            try:
                lat = float(el.get("lat")); lon = float(el.get("lon"))
            except Exception:
                continue
            ele = None
            for ch in el:
                if ch.tag.split("}")[-1] == "ele":
                    try:
                        ele = float(ch.text)
                    except Exception:
                        pass
            if ele is None:
                ele = pts[-1][2] if pts else 0.0
            pts.append((lat, lon, ele))
    return pts

def parse_fit(data):
    """Minimal FIT course parser: extract (lat, lon, ele_m) from record messages (global msg 20).
    Handles definition/data/compressed-timestamp records and developer fields."""
    import struct
    if len(data) < 14 or data[8:12] != b".FIT":
        raise ValueError("not a FIT file")
    hlen = data[0]
    pos = hlen
    end = len(data) - 2  # trailing CRC
    defs = {}
    pts = []
    last_lat = last_lon = None
    while pos < end:
        hdr = data[pos]; pos += 1
        if hdr & 0x80:  # compressed timestamp data message
            local = (hdr >> 5) & 0x3
            is_def = False
        else:
            local = hdr & 0x0F
            is_def = bool(hdr & 0x40)
        if is_def:
            arch = data[pos + 1]
            fmt = "<H" if arch == 0 else ">H"
            gmsg = struct.unpack_from(fmt, data, pos + 2)[0]
            nf = data[pos + 4]
            pos += 5
            fields = []
            for _ in range(nf):
                fields.append((data[pos], data[pos + 1], data[pos + 2]))
                pos += 3
            if hdr & 0x20:  # developer fields
                nd = data[pos]; pos += 1
                pos += 3 * nd
            defs[local] = (gmsg, arch, fields)
        else:
            d = defs.get(local)
            if d is None:
                raise ValueError("corrupt FIT (data before definition)")
            gmsg, arch, fields = d
            lat = lon = ele = None
            for fnum, fsize, ftype in fields:
                raw = data[pos:pos + fsize]; pos += fsize
                if gmsg != 20:
                    continue
                bo = "little" if arch == 0 else "big"
                if fnum == 0 and fsize == 4:
                    v = int.from_bytes(raw, bo, signed=True)
                    if v != 0x7FFFFFFF:
                        lat = v * (180.0 / 2147483648.0)
                elif fnum == 1 and fsize == 4:
                    v = int.from_bytes(raw, bo, signed=True)
                    if v != 0x7FFFFFFF:
                        lon = v * (180.0 / 2147483648.0)
                elif fnum == 2 and fsize == 2:  # altitude uint16 /5 - 500
                    v = int.from_bytes(raw, bo)
                    if v != 0xFFFF:
                        ele = v / 5.0 - 500.0
                elif fnum == 78 and fsize == 4:  # enhanced_altitude uint32 /5 - 500
                    v = int.from_bytes(raw, bo)
                    if v != 0xFFFFFFFF:
                        ele = v / 5.0 - 500.0
            if gmsg == 20 and lat is not None and lon is not None:
                if ele is None:
                    ele = pts[-1][2] if pts else 0.0
                pts.append((lat, lon, ele))
                last_lat, last_lon = lat, lon
    return pts

def _hav_m(a, b):
    import math
    R = 6371000.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    x = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 2 * R * math.asin(min(1, x ** 0.5))

def _solve_speed(power, grade, mass, crr, cda, vmax_kmh):
    """Solve P = v*(m g (grade+crr)) + 0.5 rho CdA v^3 (drivetrain ~3%) for v.
    Bisection on the single negative->positive sign change (Newton diverges on descents)."""
    g = 9.81; rho = 1.20; p = power * 0.97
    cap = vmax_kmh / 3.6
    def f(v):
        return v * mass * g * (grade + crr) + 0.5 * rho * cda * v ** 3 - p
    if f(cap) <= 0:
        return cap  # equilibrium speed above the cap (steep descent) -> ride the cap
    lo, hi = 0.3, cap
    for _ in range(40):
        mid = (lo + hi) / 2
        if f(mid) > 0:
            hi = mid
        else:
            lo = mid
    return max(0.5, (lo + hi) / 2)

def analyze_course(pts, ftp, rider_lb, ride_type):
    """Course metrics + climb list + iterated time/power estimate."""
    cfg = RIDE_TYPES.get(ride_type, RIDE_TYPES["gravel"])
    # downsample to <= 3000 points
    if len(pts) > 3000:
        step = len(pts) / 3000.0
        pts = [pts[int(i * step)] for i in range(3000)]
    n = len(pts)
    if n < 50:
        return None
    dist = [0.0]
    for i in range(1, n):
        dist.append(dist[-1] + _hav_m(pts[i - 1], pts[i]))
    total_km = dist[-1] / 1000.0
    if total_km < 1:
        return None
    # smooth elevation
    ele = [p[2] for p in pts]
    sm = []
    for i in range(n):
        lo = max(0, i - 5); hi = min(n, i + 6)
        sm.append(sum(ele[lo:hi]) / (hi - lo))
    gain = sum(max(0, sm[i] - sm[i - 1]) for i in range(1, n))
    mass = (rider_lb + cfg["bike_lb"]) / 2.205
    # climbs: sustained grade > 3%, gain >= 20 m
    climbs = []
    i = 0
    while i < n - 5:
        j = i
        while j < n - 1:
            dd = dist[j + 1] - dist[j]
            g2 = (sm[j + 1] - sm[j]) / dd if dd > 3 else 0
            if g2 < 0.015:
                break
            j += 1
        seg_d = dist[j] - dist[i]; seg_g = sm[j] - sm[i]
        if seg_g >= 20 and seg_d > 100 and seg_g / seg_d > 0.03:
            climbs.append({"i": i, "j": j, "atKm": round(dist[i] / 1000, 1),
                           "km": round(seg_d / 1000, 2), "gainFt": round(seg_g * 3.281),
                           "grade": round(seg_g / seg_d * 100, 1)})
            i = j + 3
        else:
            i += 1
    # time estimate: 1 km pieces, iterate IF by duration twice
    est_h = total_km / 25.0
    target_if = 0.8
    for _ in range(3):
        target_if = 0.85 if est_h < 2 else (0.78 if est_h < 4 else 0.72)
        p_target = ftp * target_if
        tsec = 0.0
        k0 = 0
        while k0 < n - 1:
            k1 = k0
            while k1 < n - 1 and dist[k1] - dist[k0] < 1000:
                k1 += 1
            dd = dist[k1] - dist[k0]
            if dd <= 0:
                break
            gr = (sm[k1] - sm[k0]) / dd
            pw = p_target * (1.12 if gr > 0.03 else (0.55 if gr < -0.02 else 1.0))
            v = _solve_speed(pw, gr, mass, cfg["crr"], cfg["cda"], cfg["vmax"])
            tsec += dd / v
            k0 = k1
        # technical-surface fudge
        tsec *= {"road": 1.0, "gravel": 1.04, "mtb": 1.12, "cx": 1.10}.get(ride_type, 1.05)
        est_h = tsec / 3600.0
    p_target = round(ftp * target_if)
    # per-climb targets/times
    for c in climbs:
        gr = c["grade"] / 100.0
        pw = ftp * min(0.95, target_if + 0.12)
        v = _solve_speed(pw, gr, mass, cfg["crr"], cfg["cda"], cfg["vmax"])
        c["estMin"] = round((c["km"] * 1000 / v) / 60.0, 1)
        c["targetW"] = round(pw)
        del c["i"]; del c["j"]
    climbs_sorted = sorted(climbs, key=lambda c: -c["gainFt"])[:8]
    # elevation profile for a mini chart (120 points)
    prof = []
    for k in range(120):
        idx = min(n - 1, int(k * n / 120))
        prof.append([round(dist[idx] / 1000.0, 1), round(sm[idx])])
    return {
        "km": round(total_km, 1), "mi": round(total_km / 1.609, 1),
        "gainFt": round(gain * 3.281), "gainM": round(gain),
        "estHours": round(est_h, 1), "targetIF": target_if, "targetW": p_target,
        "climbs": climbs_sorted, "nClimbs": len(climbs), "profile": prof,
    }

RACE_STRATEGY_SYSTEM = (
    "You are Keith's race tactician and directeur sportif preparing him for a specific race. You get: the course "
    "breakdown parsed from the official GPX (distance, climbing, every key climb with grade/length/estimated time), "
    "the surface type, days until race day, Keith's REAL measured power profile and fitness data, and his own "
    "scouting notes on rivals. Write a race plan with these sections, plain text, ALL-CAPS section titles:\n"
    "WHERE THIS RACE IS DECIDED - the 2-3 course features that will make the selection, with km marks.\n"
    "YOUR WEAPONS - what in Keith's actual numbers he can leverage (be specific: watts, W/kg, durability).\n"
    "SECTION-BY-SECTION PLAN - pacing through the course in order with power targets in watts; when to sit in, "
    "when to push, descent/technical notes for the surface type.\n"
    "RIVAL PLAYBOOK - for each rival named in his notes: their likely strength, where they'll attack, exactly how "
    "to counter or exploit them. If no rival intel given, give tactics for racing unknowns at his level.\n"
    "FUELING SCHEDULE - carbs/hr and when, matched to the estimated duration and his coach's fueling system "
    "(60-90g carbs/hr for races over 2h; bottles + solids early, gels late).\n"
    "FINAL 72 HOURS - taper, openers, kit/tire notes for the surface.\n"
    "Be concrete, numbers everywhere, coach-to-racer voice, no fluff. If his fitness data suggests a weakness for "
    "this course, say it straight and give the workaround."
)

def handle_race_plan(environ):
    try:
        nlen = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        nlen = 0
    raw = environ["wsgi.input"].read(nlen) if nlen > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        return {"error": "Bad request."}
    gpx = req.get("gpx") or ""
    fit_b64 = req.get("fitB64") or ""
    if not gpx.strip() and not fit_b64:
        return {"error": "No course file received."}
    ctx = req.get("context") or {}
    ftp = float(ctx.get("ftp") or 300)
    weight = float(ctx.get("weightLb") or ctx.get("smoothWeightLb") or 178)
    rtype = (req.get("rideType") or "gravel").lower()
    try:
        if fit_b64:
            import base64
            pts = parse_fit(base64.b64decode(fit_b64))
        else:
            pts = parse_gpx(gpx)
    except Exception as e:
        return {"error": "Could not read that course file: " + str(e)[:100]}
    course = analyze_course(pts, ftp, weight, rtype)
    if not course:
        return {"error": "GPX parsed but the course is too short/empty to analyze."}
    rivals = (req.get("rivals") or "").strip()[:2500]
    rname = (req.get("raceName") or "").strip()[:120]
    rdate = (req.get("raceDate") or "").strip()[:20]
    ck = "race_plan_v1:" + hashlib.sha256(
        (json.dumps(course, sort_keys=True) + rivals + rtype + rname + str(ftp)).encode("utf-8")).hexdigest()[:16]
    if not req.get("force"):
        c = kv_get(ck)
        if c:
            try:
                out = json.loads(c)
                out["cached"] = True
                return out
            except Exception:
                pass
    ctxt = _ctx_text(ctx)
    ride_json = (req.get("lastRide") or "")[:2000]
    user = ("RACE: %s%s | surface: %s\nCOURSE (from GPX): %s\n\nMY CURRENT DATA:\n%s\n" %
            (rname or "unnamed race", (" on " + rdate) if rdate else "", rtype,
             json.dumps(course)[:2600], ctxt))
    if ride_json:
        user += "\nMY LATEST ANALYZED RIDE (real measured power): " + ride_json
    user += ("\nRIVAL SCOUTING NOTES (from me):\n" + (rivals or "(none given)") +
             "\n\nBuild my race plan now.")
    text, err = anthropic_call(RACE_STRATEGY_SYSTEM, [{"role": "user", "content": user}], 8000, timeout=200)
    if err:
        return {"error": err, "course": course}
    result = {"course": course, "plan": text, "raceName": rname, "raceDate": rdate, "rideType": rtype}
    try:
        if len(text) > 400:
            kv_set(ck, json.dumps(result))
    except Exception:
        pass
    return result

REPORT_SYSTEM = (
    "Write a concise athlete status report FROM Keith TO his cycling coach for review. Professional, "
    "data-first, plain text (no markdown symbols except simple dashes), ready to paste into an email. "
    "Structure: SUBJECT line; snapshot of current numbers (recovery, HRV, readiness, CTL/ATL/TSB, 7-day load, "
    "weight/body comp, blood pressure); how training has gone recently and how he's feeling (check-in, notes, "
    "any illness/off-plan flags); nutrition status (mode, target weight, adherence context); supplement stack; "
    "recent bloodwork if present; and 2-4 specific questions for the coach to weigh in on. Under 350 words. "
    "Use only the data provided - no invention. This is Keith reporting to his own coach."
)

def handle_coach_report(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        n = 0
    raw = environ["wsgi.input"].read(n) if n > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        req = {}
    ctxt = _ctx_text(req.get("context", {}))
    text, err = anthropic_call(REPORT_SYSTEM, [
        {"role": "user", "content": ctxt + "\n\nWrite my coach report now."}], 6000, timeout=120)
    if err:
        return {"error": err}
    return {"text": text}

LABS_PROMPT = (
    "This is a blood/lab test report. Extract EVERY lab result. Output one result per line as "
    "'Test Name Value Unit' (for example: 'LDL 95 mg/dL'). If you can find the collection/draw date, "
    "put it on the very first line as 'DATE: YYYY-MM-DD'. Output nothing else — no reference ranges, "
    "no commentary, no headers. Just the optional DATE line then the results, one per line."
)

def handle_parse_labs(environ):
    try:
        n = int(environ.get("CONTENT_LENGTH") or 0)
    except Exception:
        n = 0
    raw = environ["wsgi.input"].read(n) if n > 0 else b""
    try:
        req = json.loads((raw or b"{}").decode("utf-8"))
    except Exception:
        req = {}
    b64 = req.get("pdf") or ""
    if not b64:
        return {"error": "No PDF received."}
    key = _env("ANTHROPIC_API_KEY")
    if not key:
        return {"error": "AI isn't configured (add ANTHROPIC_API_KEY)."}
    body = {
        "model": _env("AI_MODEL") or "claude-sonnet-5",
        "max_tokens": 1600,
        "messages": [{"role": "user", "content": [
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": b64}},
            {"type": "text", "text": LABS_PROMPT},
        ]}],
    }
    try:
        r = requests.post(AI_ENDPOINT, headers={
            "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json",
        }, data=json.dumps(body), timeout=60)
    except Exception as e:
        return {"error": "Reading the PDF failed: " + str(e)[:120]}
    if r.status_code != 200:
        try:
            em = r.json().get("error", {}).get("message", "")
        except Exception:
            em = r.text[:160]
        return {"error": "Lab reader error (%s): %s" % (r.status_code, em[:170])}
    try:
        parts = r.json().get("content", [])
        txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return {"text": txt or "(nothing found)"}
    except Exception:
        return {"error": "Could not parse the lab reader response."}

def app(environ, start_response):
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "") or ""

    # ---- login security gate (active once APP_PASSWORD env is set) ----
    if method == "POST" and path.rstrip("/").endswith("login"):
        try:
            n = int(environ.get("CONTENT_LENGTH") or 0)
            raw = environ["wsgi.input"].read(n) if n > 0 else b""
            pw = json.loads((raw or b"{}").decode("utf-8")).get("password", "")
        except Exception:
            pw = ""
        if login_locked():
            out, cookie = {"error": "Too many attempts - locked for 15 minutes."}, None
        else:
            role = check_password(pw)
            if role:
                clear_login_fails()
                out, cookie = {"ok": True, "role": role}, make_session_token(role)
            else:
                record_login_fail()
                out, cookie = {"error": "Wrong password."}, None
        headers = [("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")]
        if cookie:
            headers.append(("Set-Cookie",
                "kd_auth=%s; Max-Age=%d; Path=/; HttpOnly; Secure; SameSite=Lax"
                % (cookie, SESSION_DAYS * 86400)))
        start_response("200 OK", headers)
        return [json.dumps(out).encode("utf-8")]

    if path.rstrip("/").endswith("logout"):
        start_response("302 Found", [
            ("Location", "/"),
            ("Set-Cookie", "kd_auth=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax"),
        ])
        return [b""]

    if not is_authed(environ):
        if method == "POST":
            start_response("401 Unauthorized", [
                ("Content-Type", "application/json; charset=utf-8"),
                ("Cache-Control", "no-store"),
            ])
            return [json.dumps({"error": "Not signed in - reload the page."}).encode("utf-8")]
        start_response("401 Unauthorized", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [LOGIN_HTML.encode("utf-8")]

    if method == "POST" and path.rstrip("/").endswith("coach"):
        try:
            out = handle_coach(environ)
        except Exception as e:
            out = {"error": "Coach failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if method == "POST" and path.rstrip("/").endswith("ride-analysis"):
        try:
            out = handle_ride_analysis(environ)
        except Exception as e:
            out = {"error": "Ride analysis failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if method == "POST" and path.rstrip("/").endswith("race-plan"):
        try:
            out = handle_race_plan(environ)
        except Exception as e:
            out = {"error": "Race plan failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if method == "POST" and path.rstrip("/").endswith("coach-report"):
        try:
            out = handle_coach_report(environ)
        except Exception as e:
            out = {"error": "Report failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if method == "POST" and path.rstrip("/").endswith("meal-plan"):
        try:
            out = handle_meal_plan(environ)
        except Exception as e:
            out = {"error": "Meal plan failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    if method == "POST" and path.rstrip("/").endswith("parse-labs"):
        try:
            out = handle_parse_labs(environ)
        except Exception as e:
            out = {"error": "Lab parse failed: " + str(e)[:140]}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    # Cross-device state sync (supplements, daily inputs, off-plan, FTP) in KV.
    if path.rstrip("/").endswith("state"):
        if method == "POST":
            try:
                n = int(environ.get("CONTENT_LENGTH") or 0)
            except Exception:
                n = 0
            raw = environ["wsgi.input"].read(n) if n > 0 else b""
            try:
                obj = json.loads((raw or b"{}").decode("utf-8"))
                if isinstance(obj, dict):
                    kv_set("user_state", json.dumps(obj))
            except Exception:
                pass
            out = {"ok": True}
        else:
            try:
                s = kv_get("user_state")
                out = json.loads(s) if s else {}
            except Exception:
                out = {}
        body = json.dumps(out).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]

    qs = environ.get("QUERY_STRING", "") or ""
    params = urllib.parse.parse_qs(qs)

    # Withings OAuth — redirect URI is this app's root.
    host = environ.get("HTTP_HOST", "")
    redirect_uri = ("https://" + host + "/") if host else _env("WITHINGS_REDIRECT")
    if params.get("wauth"):
        if not withings_configured():
            start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
            return [b"<h2>Add WITHINGS_CLIENT_ID and WITHINGS_CLIENT_SECRET in Vercel first, then reload this link.</h2>"]
        start_response("302 Found", [("Location", withings_authorize_url(redirect_uri))])
        return [b""]
    if params.get("state", [""])[0] == "withings" and params.get("code", [""])[0]:
        ok, err = exchange_withings_auth(params.get("code", [""])[0], redirect_uri)
        msg = ("Withings connected — weight, blood pressure &amp; steps will sync on the next refresh. "
               "<a href='/?fresh=1'>Open your dashboard</a>." if ok
               else ("Withings connect failed: " + (err or "unknown")))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [("<h2>" + msg + "</h2>").encode("utf-8")]

    seed = params.get("seed", [""])[0]
    if seed:
        rt, err = exchange_auth(seed)
        msg = ("WHOOP re-auth OK \u2014 fresh token stored. You can remove ?seed and reload." if rt
               else ("WHOOP re-auth FAILED: " + (err or "no token")))
        start_response("200 OK", [("Content-Type", "text/html; charset=utf-8")])
        return [("<h2>" + msg + "</h2>").encode("utf-8")]
    fresh = "fresh=1" in qs
    try:
        data, cached = get_payload(fresh)
        body = render(data, cached).encode("utf-8")
        start_response("200 OK", [
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])
        return [body]
    except Exception as e:
        body = ("<h1>Dashboard error</h1><pre>" + str(e) + "</pre>").encode("utf-8")
        start_response("500 Internal Server Error", [("Content-Type", "text/html; charset=utf-8")])
        return [body]
