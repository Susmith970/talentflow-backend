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

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET", "talentflow-dev-change-in-prod")

# Secure session cookies for HTTPS in production
_is_prod = bool(os.environ.get("RAILWAY_ENVIRONMENT") or os.environ.get("RENDER"))
if _is_prod:
    app.config["SESSION_COOKIE_SECURE"]   = True
    app.config["SESSION_COOKIE_SAMESITE"] = "None"
    app.config["SESSION_COOKIE_HTTPONLY"] = True

# Dynamic CORS — reads FRONTEND_URL from Railway/Render env vars
_frontend = os.environ.get("FRONTEND_URL", "http://localhost:3000")
CORS(app, supports_credentials=True,
     origins=[_frontend, "http://localhost:3000", "http://localhost:5173"],
     allow_headers=["Content-Type","Authorization"],
     methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS"])

# Use same DATA_DIR as db.py to ensure consistency
# db.py handles the env var + flat/nested detection
DATA    = db.DATA_DIR
RESUMES = DATA / "resumes"
DATA.mkdir(parents=True, exist_ok=True)
RESUMES.mkdir(parents=True, exist_ok=True)

_bg_threads: dict[str, threading.Thread] = {}

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
    return session.get("username")

def require_auth():
    u = current_user()
    if not u: abort(401)
    return u


@app.get("/api/health")
def health():
    return jsonify({"ok": True, "service": "TalentFlow API", "version": "1.0.0"})


# ── Auth ──────────────────────────────────────────────────────────────────────

@app.get("/api/auth/status")
def auth_status():
    u = current_user()
    if not u:
        return jsonify({"logged_in": False,
                        "profiles_exist": db.profiles_exist()})
    profile = db.get_profile(u)
    return jsonify({"logged_in": True, "profile": profile})

@app.post("/api/auth/register")
def register():
    data = request.json or {}
    if not data.get("username") or not data.get("password"):
        return jsonify({"error": "username and password required"}), 400
    result = db.create_profile(data)
    if result.get("error"):
        return jsonify(result), 400
    session["username"] = result["profile"]["username"]
    db.log(result["profile"]["username"], "Account created")
    return jsonify(result)

@app.post("/api/auth/login")
def login():
    data = request.json or {}
    result = db.login_profile(data.get("username",""),
                              data.get("password",""))
    if result.get("error"):
        return jsonify(result), 401
    session["username"] = result["profile"]["username"]
    db.log(result["profile"]["username"], "Logged in")
    return jsonify(result)

@app.post("/api/auth/logout")
def logout():
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
    u = require_auth()
    if "file" not in request.files:
        return jsonify({"error": "No file"}), 400
    f   = request.files["file"]
    ext = Path(f.filename).suffix.lower()
    if ext not in (".pdf",".docx",".txt"):
        return jsonify({"error": "Use PDF, DOCX, or TXT"}), 400

    user_dir = DATA / u
    user_dir.mkdir(exist_ok=True)
    tmp = user_dir / f"base_resume{ext}"
    f.save(str(tmp))

    try:
        import resume_generator as rg
        extracted = rg.extract_profile_from_file(str(tmp))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    # Merge extracted fields into profile (don't overwrite auth/meta)
    merge_keys = ["name","email","phone","linkedin","github","website",
                  "location","title","summary","years_experience",
                  "current_company","skills","ml_skills","tools",
                  "experience","education","certifications",
                  "publications","awards","languages","target_roles",
                  "raw_resume_text","layout"]
    updates = {k: extracted[k] for k in merge_keys if k in extracted}
    updates["base_resume_path"] = str(tmp)
    db.update_profile(u, updates)
    db.log(u, f"Resume uploaded: {f.filename}")
    return jsonify({"ok": True, "extracted": updates})


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

    if _scrape_progress.get(u,{}).get("running"):
        return jsonify({"error": "Scrape already running"}), 409

    _scrape_progress[u] = {"running":True,"current_source":"","found":0,"done":False}
    db.log(u, f"Scrape started: {roles}")

    def _run():
        def progress(name, count):
            _scrape_progress[u]["current_source"] = name
            _scrape_progress[u]["found"]          = count

        try:
            import scraper
            new_jobs = scraper.run(roles, work, progress_cb=progress)

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

    # Tag resume filename with username prefix
    user_filename = f"{u}_{result['filename']}"
    try:
        import shutil
        src = Path(result["path"])
        dst = RESUMES / user_filename
        shutil.move(str(src), str(dst))
        result["filename"] = user_filename
        result["path"]     = str(dst)
        result["url"]      = f"/api/resume/download/{user_filename}"
    except Exception: pass

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
    require_auth()
    safe = re.sub(r"[^a-zA-Z0-9_\-\.]","",filename)
    p = RESUMES / safe
    if not p.exists(): return abort(404)
    return send_file(str(p), as_attachment=True, download_name=safe)


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
        "score_threshold": body.get("score_threshold", 60),
        "max_apply":       body.get("max_apply", 50),
        "roles":           body.get("roles") or profile.get("target_roles", []),
        "work_preference": body.get("work_preference") or profile.get("work_preference", "Any"),
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
        dst_path = src_path.parent / dst_name
        if src_path.exists():
            shutil.move(str(src_path), str(dst_path))
            res["filename"] = dst_name
            res["path"]     = str(dst_path)
        db.update_job(u, job_id,
                      resume_path     = res["path"],
                      resume_filename = res["filename"],
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


# ── Activity & health ─────────────────────────────────────────────────────────

@app.get("/api/activity")
def activity():
    u = require_auth()
    return jsonify([{"line":l} for l in db.get_activity(u)])

# health endpoint defined earlier in file
