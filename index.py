"""
Vercel serverless dashboard for Keith's training + recovery data.
Live-fetches WHOOP + Strava + TrainingPeaks on each load (15-min cache),
owns the rotating WHOOP refresh token in Upstash KV, and returns an
interactive HTML dashboard. Password protection is handled by Vercel Pro.
"""
import json, os, time, urllib.parse, hashlib
from datetime import datetime, timedelta, timezone

import requests

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
        date = datetime.fromisoformat(rec["created_at"].replace("Z", "+00:00")).strftime("%Y-%m-%d")
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
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out = [e for e in sorted(events, key=lambda x: x.get("date", ""))
           if e.get("date", "") >= today and e.get("summary")]
    return out[:12]

# ---- payload assembly with cache ----
def build_payload():
    wt = refresh_whoop()
    st = refresh_strava()
    whoop = fetch_whoop(wt)
    strava = fetch_strava(st)
    tp = fetch_tp()
    now = datetime.now(timezone.utc)
    days = []
    for i in range(DAYS - 1, -1, -1):
        d = now - timedelta(days=i)
        k = d.strftime("%Y-%m-%d")
        w = whoop.get(k, {})
        s = strava.get(k, {})
        days.append({
            "date": k, "label": d.strftime("%b ") + str(d.day),
            "hrv": w.get("hrv"), "rhr": w.get("rhr"),
            "recovery": w.get("recovery"), "sleep": w.get("sleep"),
            "effort": s.get("effort", 0), "kj": s.get("kj", 0),
            "secs": s.get("secs", 0), "rides": s.get("rides", []),
        })
    return {"generated": now.strftime("%B %d, %Y %H:%M UTC"), "days": days, "tp": tp}

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
    "Keep it under ~120 words, plain text, no preamble or sign-off."
)
CHAT_SYSTEM = (
    "You are Keith's knowledgeable, supportive cycling coach. Answer his questions about training, "
    "recovery, pacing, and fueling using the current athlete data provided. Be concise, specific, and "
    "practical - reference his actual numbers (readiness, form/TSB, FTP watts, planned workouts) when "
    "relevant. Respect the readiness signal and never prescribe hard efforts on a low-recovery day. "
    "You are not a doctor or dietitian: for pain, illness, or medical questions, recommend rest and a "
    "professional. Keep answers short (a few sentences) unless he asks for more detail."
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
    if ctx.get("coachNote"):
        L.append("Coach's note: %s" % ctx.get("coachNote"))
    if ctx.get("yourNote"):
        L.append("Athlete's note today: %s" % ctx.get("yourNote"))
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

    qs = environ.get("QUERY_STRING", "") or ""
    params = urllib.parse.parse_qs(qs)
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
