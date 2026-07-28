"""
Vercel serverless dashboard for Keith's training + recovery data.
Live-fetches WHOOP + Strava + TrainingPeaks on each load (15-min cache),
owns the rotating WHOOP refresh token in Upstash KV, and returns an
interactive HTML dashboard. Password protection is handled by Vercel Pro.
"""
import json, os, time, urllib.parse, hashlib
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

# ---- token management ----
def whoop_refresh_token():
    return kv_get("whoop_refresh") or _env("WHOOP_REFRESH_TOKEN")

def refresh_whoop():
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
_PAGE_CACHE = {"html": None}

def get_page():
    if _PAGE_CACHE["html"]:
        return _PAGE_CACHE["html"]
    r = requests.get(PAGE_URL, timeout=20)
    r.raise_for_status()
    _PAGE_CACHE["html"] = r.text
    return r.text

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
        "FTP %sW, body weight %s lb" % (ctx.get("ftp"), ctx.get("weightLb")),
    ]
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
    L.append("Next planned workout: %s" % (ctx.get("nextWorkout") or "none scheduled"))
    up = ctx.get("upcoming") or []
    if isinstance(up, list) and up:
        L.append("Upcoming: " + "; ".join(
            ("%s %s" % (u.get("date", ""), u.get("summary", ""))).strip() for u in up[:5] if isinstance(u, dict)))
    return "\n".join(str(x) for x in L)

def anthropic_call(system, messages, max_tokens=700):
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
        }), timeout=45)
    except Exception as e:
        return None, "Coach request failed: " + str(e)[:120]
    if r.status_code != 200:
        try:
            em = r.json().get("error", {}).get("message", "")
        except Exception:
            em = r.text[:160]
        return None, "Coach error (%s): %s" % (r.status_code, em[:170])
    try:
        parts = r.json().get("content", [])
        txt = "".join(p.get("text", "") for p in parts if p.get("type") == "text").strip()
        return (txt or "(no reply)"), None
    except Exception:
        return None, "Coach parse error"

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
    text, err = anthropic_call(CHAT_SYSTEM + "\n\nCurrent athlete data:\n" + ctxt, messages, 700)
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
