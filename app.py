"""
TalentFlow Flask API
All routes are prefixed /api/
Auth: simple username+password stored in local profiles.json
"""
import json, os, re, sys, threading, time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from flask import Flask, jsonify, request, send_file, abort, session
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")  # .env sits next to app.py at root

import db

# ── Rate limiting (in-memory, per user) ──────────────────────────────────────
_rate_limits: dict = {}
_rate_lock = threading.Lock()

def _check_rate(username: str, action: str, max_calls: int, window_secs: int) -> bool:
    now = time.time()
    with _rate_lock:
        user  = _rate_limits.setdefault(username, {})
        calls = [t for t in user.get(action, []) if now - t < window_secs]
        if len(calls) >= max_calls: return False
        calls.append(now); user[action] = calls; return True

TIERS = {
    "free":  {"scrapes_per_day": 3,  "applies_per_day": 5,  "resumes_per_day": 10},
    "pro":   {"scrapes_per_day": 20, "applies_per_day": 50, "resumes_per_day": 100},
    "agent": {"scrapes_per_day": 99, "applies_per_day": 999,"resumes_per_day": 999},
}

def get_tier(username: str) -> str:
    p = db.get_profile(username)
    return (p or {}).get("subscription_tier", "free")

def tier_limits(username: str) -> dict:
    return TIERS.get(get_tier(username), TIERS["free"])

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "talentflow-dev-change-in-prod")

# Secure session cookies for HTTPS in production
_is_prod = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
if _is_prod:
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

# Dynamic CORS — reads FRONTEND_URL from Railway/Render env vars
# CORS — allow frontend + all vercel.app preview URLs
# flask-cors supports regex strings in the origins list
_frontend = os.environ.get("FRONTEND_URL", "")
_origins = [
    "http://localhost:3000",
    "http://localhost:5173",
    "http://127.0.0.1:3000",
    r"https://.*\.vercel\.app",   # all Vercel deployments (regex)
    r"https://.*\.railway\.app",  # all Railway deployments (regex)
]
if _frontend and _frontend not in _origins:
    _origins.insert(0, _frontend)

CORS(app, supports_credentials=True,
     origins=_origins,
     allow_headers=["Content-Type", "Authorization", "X-Requested-With"],
     methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
     max_age=3600)

# Use same DATA_DIR as db.py to ensure consistency
# db.py handles the env var + flat/nested detection
DATA    = db.DATA_DIR
RESUMES = DATA / "resumes"
DATA.mkdir(parents=True, exist_ok=True)
RESUMES.mkdir(parents=True, exist_ok=True)

_bg_threads: dict[str, threading.Thread] = {}

# ── Token auth — persisted in db so tokens survive redeploys ─────────────────
import secrets as _secrets

def _new_token() -> str:
    return _secrets.token_urlsafe(32)

def bg(key: str, fn, *args, **kwargs):
    def _run():
        try: fn(*args, **kwargs)
        except Exception as e:
            print(f"bg[{key}] error: {e}")
    t = threading.Thread(target=_run, daemon=True)
    _bg_threads[key] = t
    t.start()
    return t

def current_user() -> str | None:
    # 1. Check Authorization: Bearer <token> header (production cross-origin)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        return db.get_token_user(auth[7:])
    # 2. Check ?token= query param (for direct file downloads)
    qs_tok = request.args.get("token", "")
    if qs_tok:
        return db.get_token_user(qs_tok)
    # 3. Fallback: session cookie (local development only)
    return session.get("username")

def require_auth():
    u = current_user()
    if not u: abort(401)
    return u


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "TalentFlow API", "version": "1.4.0",
                    "db_mode": "postgresql" if db.USE_POSTGRES else "json_files",
                    "db_persists": db.USE_POSTGRES,
                    "features": ["token-auth", "persistent-tokens", "extract-profile", "two-pass-tailor"]})


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
def auth_status():
    u = current_user()
    if not u:
        return jsonify({"logged_in": False,
                        "profiles_exist": db.profiles_exist()})
    profile = db.get_profile(u)
    return jsonify({"logged_in": True, "profile": profile})

@app.get("/api/auth/me")
def auth_me():
    u = current_user()
    if not u:
        return jsonify({"ok": False}), 401
    profile = db.get_profile(u)
    if not profile:
        return jsonify({"ok": False}), 401
    return jsonify({"ok": True, "profile": profile})

@app.post("/api/auth/register")
def register():
    data = request.json or {}
    if not data.get("username") or not data.get("password"):
        return jsonify({"error": "username and password required"}), 400
    result = db.create_profile(data)
    if result.get("error"):
        return jsonify(result), 400
    username = result["profile"]["username"]
    session["username"] = username
    token = _new_token()
    db.save_token(token, username)
    db.log(username, "Account created")
    result["token"] = token
    return jsonify(result)

@app.post("/api/auth/login")
def login():
    data = request.json or {}
    result = db.login_profile(data.get("username",""),
                              data.get("password",""))
    if result.get("error"):
        return jsonify(result), 401
    username = result["profile"]["username"]
    session["username"] = username
    token = _new_token()
    db.save_token(token, username)
    db.log(username, "Logged in")
    result["token"] = token
    return jsonify(result)

@app.post("/api/auth/logout")
def logout():
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        db.delete_token(auth[7:])
    session.clear()
    return jsonify({"ok": True})


# ── Profile ───────────────────────────────────────────────────────────────────

@app.get("/api/profile")
def get_profile():
    u = require_auth()
    return jsonify(db.get_profile(u))

@app.put("/api/profile")
def update_profile():
    u    = require_auth()
    data = request.json or {}
    res  = db.update_profile(u, data)
    if res.get("error"): return jsonify(res), 400
    return jsonify(res)

@app.post("/api/profile/upload-resume")
def upload_resume_to_profile():
    """
    Upload a base resume → extract ALL structured data with Claude → save to DB.
    This runs ONCE on upload. After this, generate() reads from profile DB directly.
    Never re-reads the PDF file.
    """
    u = require_auth()
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in (".pdf", ".docx", ".txt"):
        return jsonify({"error": "Use PDF, DOCX, or TXT"}), 400

    user_dir = DATA / u
    user_dir.mkdir(exist_ok=True)
    saved_path = user_dir / f"base_resume{ext}"
    f.save(str(saved_path))

    try:
        import resume_generator as rg
        extracted = rg.extract_profile_from_file(str(saved_path))
    except Exception as exc:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(exc)}), 500

    if extracted.get("error") and not extracted.get("raw_resume_text"):
        return jsonify(extracted), 400

    # Save every extracted field to the profile DB.
    # generate() will read experience/projects/skills from here — never from the PDF.
    ALL_MERGE_KEYS = [
        "name", "email", "phone", "linkedin", "github", "website",
        "location", "title", "summary", "years_experience", "current_company",
        "target_roles",
        "skills", "ml_skills", "tools",
        "experience",   # list of {title, company, location, dates, bullets}
        "education",    # list of {degree, school, location, dates, honors}
        "projects",     # list of {name, technologies, dates, url, bullets}
        "certifications",
        "awards",
        "raw_resume_text",
    ]
    updates = {k: extracted[k] for k in ALL_MERGE_KEYS if k in extracted}
    updates["base_resume_path"] = str(saved_path)

    # Log what we extracted so user can see it worked
    n_jobs  = len(updates.get("experience") or [])
    n_proj  = len(updates.get("projects") or [])
    n_certs = len(updates.get("certifications") or [])

    db.update_profile(u, updates)
    db.log(u, f"Resume parsed: {f.filename} → {n_jobs} jobs, {n_proj} projects, {n_certs} certs")

    return jsonify({
        "ok":       True,
        "summary": {
            "jobs":         n_jobs,
            "projects":     n_proj,
            "certifications": n_certs,
            "skills":       len(updates.get("skills") or []),
        },
        "extracted": updates,
    })


# ── Jobs ──────────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
def get_jobs():
    u      = require_auth()
    jobs   = db.load_jobs(u)
    status = request.args.get("status")
    src    = request.args.get("source")
    q      = request.args.get("q","").lower()
    if status: jobs = [j for j in jobs if j.get("status") == status]
    if src:    jobs = [j for j in jobs if j.get("source") == src]
    if q:      jobs = [j for j in jobs if q in (j.get("title","") + j.get("company","")).lower()]
    # Sort: new first, then by ats_score desc
    jobs.sort(key=lambda j: (-j.get("ats_score",0)))
    return jsonify(jobs)

@app.get("/api/jobs/<jid>")
def get_job(jid):
    u = require_auth()
    j = db.get_job(u, jid)
    return jsonify(j) if j else abort(404)

@app.patch("/api/jobs/<jid>/status")
def set_status(jid):
    u    = require_auth()
    body = request.json or {}
    new_status = body.get("status","")
    db.update_job(u, jid, status=new_status,
                  **({"submitted_at": datetime.now().isoformat()}
                     if new_status in ("submitted","applying") else {}),
                  notes=body.get("notes", db.get_job(u,jid) and db.get_job(u,jid).get("notes","") or ""))
    db.log(u, f"Job {jid} → {new_status}")
    return jsonify({"ok": True})

@app.patch("/api/jobs/<jid>/note")
def set_note(jid):
    u = require_auth()
    db.update_job(u, jid, notes=(request.json or {}).get("notes",""))
    return jsonify({"ok": True})

@app.get("/api/jobs/stats")
def job_stats():
    u    = require_auth()
    jobs = db.load_jobs(u)
    by_s, by_src = {}, {}
    for j in jobs:
        s = j.get("status","new")
        by_s[s] = by_s.get(s,0)+1
        src = j.get("source","?")
        by_src[src] = by_src.get(src,0)+1
    ls = DATA / f"last_scrape_{u}.txt"
    return jsonify({
        "total": len(jobs), "by_status": by_s, "by_source": by_src,
        "resumes_generated": len(list(RESUMES.glob(f"{u}_*.pdf"))),
        "last_scrape": ls.read_text().strip() if ls.exists() else None,
    })


# ── Scrape ────────────────────────────────────────────────────────────────────

# Track per-user scrape progress
_scrape_progress: dict[str, dict] = {}

@app.post("/api/scrape/start")
def start_scrape():
    u       = require_auth()
    profile = db.get_profile(u)
    body    = request.json or {}
    roles   = body.get("roles") or profile.get("target_roles", ["Software Engineer"])
    work    = body.get("work_preference") or profile.get("work_preference","Any")
    emp     = body.get("employment_type_pref") or profile.get("employment_type_pref","Any")

    if _scrape_progress.get(u,{}).get("running"):
        return jsonify({"error": "Scrape already running"}), 409
    lim = tier_limits(u)
    if not _check_rate(u, "scrape", lim["scrapes_per_day"], 86400):
        return jsonify({"error": f"Daily limit: {lim['scrapes_per_day']} scrapes/day. Upgrade to Pro for more."}), 429

    _scrape_progress[u] = {"running":True,"current_source":"","found":0,"done":False}
    db.log(u, f"Scrape started: {roles}")

    def _run():
        def progress(name, count):
            _scrape_progress[u]["current_source"] = name
            _scrape_progress[u]["found"]          = count

        try:
            import scraper
            new_jobs = scraper.run(roles, work, emp_type=emp, progress_cb=progress)

            # AI-score if API key set
            api_key = os.environ.get("ANTHROPIC_API_KEY","")
            if api_key and new_jobs:
                db.log(u, f"AI-scoring {min(len(new_jobs),50)} jobs …")
                import resume_generator as rg
                for i, job in enumerate(new_jobs[:50]):
                    try:
                        score = rg.ats_score_job(profile, job)
                        job.update(score)
                    except Exception: pass
                    time.sleep(0.2)

            added, total = db.upsert_jobs(u, new_jobs)
            (DATA / f"last_scrape_{u}.txt").write_text(datetime.utcnow().isoformat())
            db.log(u, f"Scrape done: {added} new, {total} total")
        except Exception as e:
            db.log(u, f"Scrape error: {e}")
        finally:
            _scrape_progress[u]["running"] = False
            _scrape_progress[u]["done"]    = True

    bg(f"scrape_{u}", _run)
    return jsonify({"ok":True,"message":"Scrape started"})

@app.get("/api/scrape/progress")
def scrape_progress():
    u = require_auth()
    return jsonify(_scrape_progress.get(u, {"running":False,"done":True,"found":0}))


# ── Resume generation ─────────────────────────────────────────────────────────

@app.post("/api/resume/generate")
def gen_resume():
    u       = require_auth()
    profile = db.get_profile(u)
    if not profile:
        return jsonify({"error":"No profile found"}), 400

    body   = request.json or {}
    jid    = body.get("job_id")
    jd     = body.get("job_description","")
    title  = body.get("job_title","Role")
    co     = body.get("company","Company")

    if jid:
        job = db.get_job(u, jid)
        if job:
            jd    = job.get("description","")
            title = job.get("title", title)
            co    = job.get("company", co)

    try:
        import resume_generator as rg
        result = rg.generate(profile, jd, title, co)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify({"error": str(e)}), 500

    if result.get("error"):
        return jsonify(result), 500

    # Move to user-namespaced path in app RESUMES dir
    user_filename = f"{u}_{result['filename']}"
    try:
        import shutil
        src_p = Path(result["path"])
        dst_p = RESUMES / user_filename
        RESUMES.mkdir(parents=True, exist_ok=True)
        if src_p.exists():
            shutil.move(str(src_p), str(dst_p))
        result["filename"] = user_filename
        result["path"]     = str(dst_p)
        result["url"]      = f"/api/resume/download/{user_filename}"
    except Exception as _mv_err:
        print(f"  Warning: could not move resume: {_mv_err}")

    if jid:
        db.update_job(u, jid,
            resume_path      = result["path"],
            resume_filename  = result["filename"],
            ats_score        = result.get("ats_score", 0),
            match_label      = result.get("match_label",""),
            match_reason     = result.get("match_reason",""),
            matched_keywords = result.get("matched_keywords",[]),
            missing_keywords = result.get("missing_keywords",[]),
            ats_tips         = result.get("ats_tips",[]),
            status           = "ready",
        )
    db.log(u, f"Resume generated: {result['filename']}")
    return jsonify(result)

@app.get("/api/resume/list")
def list_resumes():
    u = require_auth()
    files = sorted(RESUMES.glob(f"{u}_*.pdf"),
                   key=lambda f: f.stat().st_mtime, reverse=True)
    return jsonify([{
        "filename": f.name,
        "size_kb":  round(f.stat().st_size/1024,1),
        "created":  datetime.fromtimestamp(f.stat().st_mtime).isoformat(),
        "url":      f"/api/resume/download/{f.name}",
    } for f in files])

@app.get("/api/resume/download/<filename>")
def dl_resume(filename):
    # current_user() handles: Authorization header, ?token= param, session cookie
    require_auth()
    safe = re.sub(r"[^a-zA-Z0-9_\-\.]","",filename)
    p = RESUMES / safe
    if not p.exists(): return abort(404)
    return send_file(str(p), as_attachment=True, download_name=safe,
                     mimetype="application/pdf")


# ── Auto-apply ────────────────────────────────────────────────────────────────

@app.post("/api/apply/<jid>")
def apply_one(jid):
    u       = require_auth()
    profile = db.get_profile(u)
    job     = db.get_job(u, jid)
    if not job: return abort(404)
    db.update_job(u, jid, status="applying")

    def _run():
        try:
            import auto_apply
            auto_apply.apply_job(jid, profile, u)
        except Exception as e:
            db.log(u, f"Apply error {jid}: {e}")
    bg(f"apply_{u}_{jid}", _run)
    return jsonify({"ok":True})

@app.post("/api/apply/batch")
def apply_batch():
    u       = require_auth()
    profile = db.get_profile(u)
    def _run():
        try:
            import auto_apply
            auto_apply.apply_batch(profile, u)
        except Exception as e:
            db.log(u, f"Batch apply error: {e}")
    bg(f"batch_{u}", _run)
    return jsonify({"ok":True})



# ── Pipeline ───────────────────────────────────────────────────────────────────

@app.post("/api/pipeline/start")
def pipeline_start():
    u       = require_auth()
    profile = db.get_profile(u)
    if not profile:
        return jsonify({"error": "No profile found"}), 400

    import pipeline
    if pipeline.is_running(u):
        return jsonify({"error": "Pipeline already running"}), 409

    body    = request.json or {}
    options = {
        "score_threshold":    body.get("score_threshold", 60),
        "max_apply":          body.get("max_apply", 50),
        "roles":              body.get("roles") or profile.get("target_roles", []),
        "work_preference":    body.get("work_preference") or profile.get("work_preference", "Any"),
        "employment_type_pref": body.get("employment_type_pref") or profile.get("employment_type_pref", "Any"),
    }

    started = pipeline.start(u, profile, options)
    if not started:
        return jsonify({"error": "Could not start pipeline"}), 500

    db.log(u, f"Pipeline started (threshold={options['score_threshold']}, max={options['max_apply']})")
    return jsonify({"ok": True, "message": "Pipeline started"})


@app.get("/api/pipeline/status")
def pipeline_status():
    u = require_auth()
    import pipeline
    return jsonify(pipeline.status(u))


@app.post("/api/pipeline/stop")
def pipeline_stop():
    """
    There is no hard kill — the pipeline checks a flag between phases.
    We mark it stopped; it will finish the current job then exit.
    """
    u = require_auth()
    import pipeline
    s = pipeline.get_state(u)
    s["running"]     = False
    s["phase"]       = "done"
    s["phase_label"] = "Stopped by user"
    db.log(u, "Pipeline stopped by user")
    return jsonify({"ok": True})

# ── Apply from any URL ────────────────────────────────────────────────────────

@app.post("/api/apply/from-url")
def apply_from_url():
    """
    Paste any job URL → we fetch the JD, tailor a resume, and auto-submit.
    Works for Greenhouse, Lever, Ashby, Workable, LinkedIn, and any ATS.
    """
    u = require_auth()
    profile = db.get_profile(u)
    if not profile:
        return jsonify({"error": "No profile found. Complete your profile first."}), 400

    body    = request.json or {}
    url     = (body.get("url") or "").strip()
    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Normalise URL
    if not url.startswith("http"):
        url = "https://" + url

    db.log(u, f"Quick Apply: {url[:80]}")

    # 1. Detect ATS platform from URL
    import auto_apply as aa, scraper as sc, resume_generator as rg, shutil
    platform = aa.detect_ats(url)
    db.log(u, f"  Detected platform: {platform}")

    # 2. Fetch job description from the page
    jd_text = ""
    title   = "Role"
    company = "Company"

    try:
        import urllib.request
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
        })
        with urllib.request.urlopen(req, timeout=15) as r:
            html = r.read().decode("utf-8", errors="replace")

        # Strip HTML tags to get plain text
        import re
        clean = re.sub(r"<[^>]+>", " ", html)
        clean = re.sub(r"&[a-zA-Z#0-9]+;", " ", clean)
        clean = re.sub(r"\s+", " ", clean).strip()
        jd_text = clean[:5000]

        # Try to extract title from <title> or og:title
        title_m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
        if title_m:
            raw_t = title_m.group(1).strip()
            # Title is usually "Job Title - Company | Greenhouse" etc.
            parts = re.split(r"[|\-—–]", raw_t)
            if len(parts) >= 2:
                title   = parts[0].strip()[:60]
                company = parts[1].strip()[:40]
            else:
                title = raw_t[:60]

        # Try og:title and og:description as fallback
        og_title = re.search(r'og:title[^>]*content="([^"]+)"', html)
        if og_title and len(og_title.group(1)) > len(title):
            title = og_title.group(1)[:80]

    except Exception as e:
        db.log(u, f"  Could not fetch URL ({e}) — will apply with profile summary only")
        jd_text = ""

    # Override with user-provided values if given
    if body.get("title"):   title   = body["title"]
    if body.get("company"): company = body["company"]

    db.log(u, f"  Job: {title} @ {company}")

    # 3. Create a job record in DB
    import time as _time
    job_id  = f"url_{abs(hash(url))}_{int(_time.time())}"
    job_rec = {
        "id":             job_id,
        "title":          title,
        "company":        company,
        "location":       "USA",
        "work_type":      "On-site",
        "source":         "Manual URL",
        "url":            url,
        "apply_url":      url,
        "apply_platform": platform,
        "easy_apply":     False,
        "description":    jd_text,
        "status":         "new",
        "ats_score":      0,
        "salary":         "",
        "tags":           [],
        "posted":         datetime.utcnow().strftime("%Y-%m-%d"),
        "scraped_at":     datetime.utcnow().isoformat(),
        "updated_at":     datetime.utcnow().isoformat(),
        "notes":          f"Added via Quick Apply from URL: {url[:100]}",
        "resume_path":    None, "resume_filename": None,
        "matched_keywords": [], "missing_keywords": [], "ats_tips": [],
        "match_label": "", "match_reason": "",
        "manual_reason": "", "manual_apply_url": "",
        "apply_error": "", "submitted_at": None, "applied_at": None,
    }
    db.upsert_jobs(u, [job_rec])

    # 4. Generate tailored resume
    try:
        res = rg.generate(profile, jd_text, title, company)
        if res.get("error"):
            raise RuntimeError(res["error"])
        # Move to user-namespaced path
        src_path = Path(res["path"])
        dst_name = f"{u}_{src_path.name}"
        # Always move to the app's RESUMES dir so downloads work
        dst_path = RESUMES / dst_name
        RESUMES.mkdir(parents=True, exist_ok=True)
        if src_path.exists():
            shutil.move(str(src_path), str(dst_path))
        res["filename"] = dst_name
        res["path"]     = str(dst_path)
        res["url"]      = f"/api/resume/download/{dst_name}"
        db.update_job(u, job_id,
                      resume_path     = str(dst_path),
                      resume_filename = dst_name,
                      status          = "ready",
                      ats_score       = res.get("ats_score", 0),
                      match_label     = res.get("match_label", ""),
                      match_reason    = res.get("match_reason", ""),
                      matched_keywords= res.get("matched_keywords", []),
                      missing_keywords= res.get("missing_keywords", []))
        job_rec["resume_path"] = res["path"]
        db.log(u, f"  Resume: {res['filename']}")
    except Exception as e:
        db.log(u, f"  Resume error: {e}")
        return jsonify({"error": f"Resume generation failed: {e}",
                        "job_id": job_id, "platform": platform,
                        "title": title, "company": company}), 500

    # 5. Auto-submit
    db.log(u, f"  Submitting via [{platform}]…")
    try:
        result = aa.apply_job(job_id, profile, u)
    except Exception as e:
        result = {"success": False, "reason": str(e), "manual": True, "apply_url": url}

    return jsonify({
        "ok":              True,
        "job_id":          job_id,
        "title":           title,
        "company":         company,
        "platform":        platform,
        "resume_filename": res.get("filename",""),
        "ats_score":       res.get("ats_score", 0),
        "match_label":     res.get("match_label",""),
        "submitted":       result.get("success", False),
        "manual":          result.get("manual", False),
        "pre_filled":      result.get("pre_filled", False),
        "reason":          result.get("reason",""),
        "apply_url":       result.get("apply_url", url),
    })


# ── LinkedIn Session ─────────────────────────────────────────────────────────

@app.get("/api/linkedin/status")
def linkedin_status():
    """Check if LinkedIn session is saved and valid."""
    u = require_auth()
    import auto_apply as aa
    has_session  = aa.SESSION_FILE.exists()
    has_creds    = bool(os.environ.get("LINKEDIN_EMAIL") and os.environ.get("LINKEDIN_PASSWORD"))
    session_age  = None
    if has_session:
        import time
        age_secs   = time.time() - aa.SESSION_FILE.stat().st_mtime
        session_age = f"{int(age_secs/3600)}h ago"
    return jsonify({
        "has_session":  has_session,
        "has_creds":    has_creds,
        "session_age":  session_age,
        "ready":        has_session or has_creds,
        "session_path": str(aa.SESSION_FILE),
    })


@app.post("/api/linkedin/save-session")
def linkedin_save_session():
    """
    Upload LinkedIn session JSON (from browser export).
    The session JSON can be exported using the EditThisCookie extension:
    1. Log in to LinkedIn on your browser
    2. Export cookies as JSON via EditThisCookie / Cookie Editor extension
    3. POST that JSON here
    """
    u = require_auth()
    import auto_apply as aa
    body = request.json or {}
    session_data = body.get("session_data") or body.get("cookies")
    if not session_data:
        return jsonify({"error": "session_data or cookies field required"}), 400
    try:
        # Validate it looks like a Playwright storage state or cookie array
        if isinstance(session_data, list):
            # Cookie array format — convert to Playwright storage state
            storage = {
                "cookies": session_data,
                "origins": []
            }
        elif isinstance(session_data, dict):
            storage = session_data
        else:
            return jsonify({"error": "session_data must be JSON object or array"}), 400

        aa.SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        aa.SESSION_FILE.write_text(json.dumps(storage, indent=2))
        db.log(u, f"LinkedIn session saved ({len(str(storage))} bytes)")
        return jsonify({"ok": True, "message": "LinkedIn session saved — Easy Apply now active"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ── Stripe / Subscriptions ───────────────────────────────────────────────────

@app.post("/api/billing/create-checkout")
def create_checkout():
    u = require_auth()
    try:
        import stripe
        stripe.api_key = os.environ.get("STRIPE_SECRET_KEY","")
        if not stripe.api_key:
            return jsonify({"error":"Billing not configured — contact support"}), 503
        body     = request.json or {}
        plan     = body.get("plan","pro")
        prices   = {"pro": os.environ.get("STRIPE_PRICE_PRO",""),
                    "agent": os.environ.get("STRIPE_PRICE_AGENT","")}
        price_id = prices.get(plan)
        if not price_id:
            return jsonify({"error":"Invalid plan"}), 400
        profile  = db.get_profile(u) or {}
        frontend = os.environ.get("FRONTEND_URL","https://talentflow-frontend-ten.vercel.app")
        session_obj = stripe.checkout.Session.create(
            payment_method_types=["card"], mode="subscription",
            customer_email=profile.get("email",""),
            metadata={"username": u, "plan": plan},
            line_items=[{"price": price_id, "quantity": 1}],
            success_url=f"{frontend}?billing=success&plan={plan}",
            cancel_url=f"{frontend}?billing=cancelled",
        )
        return jsonify({"ok": True, "url": session_obj.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.post("/api/billing/webhook")
def stripe_webhook():
    try:
        import stripe
        stripe.api_key  = os.environ.get("STRIPE_SECRET_KEY","")
        webhook_secret  = os.environ.get("STRIPE_WEBHOOK_SECRET","")
        payload         = request.get_data()
        sig             = request.headers.get("Stripe-Signature","")
        if webhook_secret:
            try: event = stripe.Webhook.construct_event(payload, sig, webhook_secret)
            except Exception: return jsonify({"error":"Invalid signature"}), 400
        else:
            event = json.loads(payload)
        etype = event.get("type","")
        obj   = event.get("data",{}).get("object",{})
        if etype in ("checkout.session.completed","customer.subscription.updated"):
            meta = obj.get("metadata",{})
            username = meta.get("username",""); plan = meta.get("plan","pro")
            if username:
                db.update_profile(username, {"subscription_tier": plan,
                    "subscription_status": "active",
                    "subscription_updated": datetime.now().isoformat()})
                print(f"  [billing] {username} → {plan}")
        elif etype == "customer.subscription.deleted":
            meta = obj.get("metadata",{})
            username = meta.get("username","")
            if username:
                db.update_profile(username, {"subscription_tier": "free",
                    "subscription_status": "cancelled"})
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.get("/api/billing/status")
def billing_status():
    u = require_auth()
    profile = db.get_profile(u) or {}
    tier    = profile.get("subscription_tier","free")
    return jsonify({"tier": tier, "limits": tier_limits(u),
                    "status": profile.get("subscription_status","")})


# ── Fix apply_platform on existing jobs ───────────────────────────────────────

@app.post("/api/jobs/fix-platforms")
def fix_platforms():
    """Retroactively fix apply_platform for all jobs based on their URL."""
    u = require_auth()
    import scraper as sc
    jobs = db.load_jobs(u)
    fixed = 0
    for job in jobs:
        url  = job.get("apply_url") or job.get("url","")
        plat = sc.detect_platform(url, url)
        if plat != job.get("apply_platform"):
            db.update_job(u, job["id"], apply_platform=plat)
            fixed += 1
    db.log(u, f"Fixed apply_platform on {fixed}/{len(jobs)} jobs")
    return jsonify({"ok": True, "fixed": fixed, "total": len(jobs)})


# ── Pending Questions ("Ask User" flow) ───────────────────────────────────────

@app.get("/api/apply/pending")
def get_pending():
    """Poll for questions the bot couldn't answer — UI shows these to the user."""
    u = require_auth()
    pending = db.list_pending_questions(u)
    return jsonify({"pending": pending})

@app.post("/api/apply/pending/<job_id>/answer")
def answer_pending(job_id):
    """User submits answers — bot will resume and fill them in."""
    u = require_auth()
    body    = request.json or {}
    answers = body.get("answers", {})   # {q0: "Yes", q1: "No", ...}
    if not answers:
        return jsonify({"error": "answers dict required"}), 400
    ok = db.answer_pending_questions(u, job_id, answers)
    if not ok:
        return jsonify({"error": "No pending questions found for this job"}), 404
    db.log(u, f"User answered pending questions for job {job_id}")
    return jsonify({"ok": True, "message": "Answers saved — bot will resume shortly"})

@app.delete("/api/apply/pending/<job_id>")
def dismiss_pending(job_id):
    """Dismiss pending questions (skip / mark manual)."""
    u = require_auth()
    db.clear_pending_questions(u, job_id)
    return jsonify({"ok": True})


# ── Activity & health ─────────────────────────────────────────────────────────

@app.get("/api/activity")
def activity():
    u = require_auth()
    return jsonify([{"line":l} for l in db.get_activity(u)])

# health endpoint defined earlier in file
