"""
pipeline.py — Full Automated Job Hunt Pipeline
================================================

DESIGN DECISIONS:
  - NO expensive ATS scoring on all 1200 jobs. Instead:
    1. Keyword pre-filter (instant, no API) eliminates irrelevant jobs
    2. Only generate resumes + apply for keyword-matched jobs
    3. ATS scoring runs AFTER apply (for display only, never blocks)

  - Max N applications per run (configurable, default 40)
  - Skip jobs already submitted/failed/manual
  - Submit in order: Easy Apply first (fastest), then public forms, then manual

Pipeline phases:
  SCRAPE → PRE-FILTER → RESUME → SUBMIT → DONE
"""

import os
import re
import sys
import time
import threading
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db

# ── State ────────────────────────────────────────────────────────────────────

_state: dict[str, dict] = {}


def _blank() -> dict:
    return {
        "running":        False,
        "phase":          "",
        "phase_label":    "Idle",
        "started_at":     None,
        "finished_at":    None,
        "jobs_scraped":   0,
        "jobs_new":       0,
        "jobs_skipped":   0,
        "jobs_eligible":  0,
        "jobs_resumed":   0,
        "jobs_submitted": 0,
        "jobs_manual":    0,
        "jobs_failed":    0,
        "current_job":    "",
        "log":            [],
        "error":          None,
    }


def get_state(u: str) -> dict:
    if u not in _state:
        _state[u] = _blank()
    return _state[u]


def is_running(u: str) -> bool:
    return _state.get(u, {}).get("running", False)


# ── Logging ───────────────────────────────────────────────────────────────────

def _log(u: str, msg: str, level: str = "info"):
    ts    = datetime.now().strftime("%H:%M:%S")
    entry = {"ts": ts, "msg": msg, "level": level}
    s     = get_state(u)
    s["log"].append(entry)
    if len(s["log"]) > 300:
        s["log"] = s["log"][-300:]
    db.log(u, msg)
    print(f"  [{ts}] {msg}")


def _phase(u: str, phase: str, label: str):
    s = get_state(u)
    s["phase"]       = phase
    s["phase_label"] = label


# ── Keyword pre-filter (no API, instant) ─────────────────────────────────────

def _keyword_match(job: dict, profile: dict) -> bool:
    """
    Fast keyword check — no API call, runs in milliseconds.

    Rules:
    1. ALL words in a target role must appear in the job title.
       "Data Engineer" needs both "data" AND "engineer" in the title.
       This prevents "Software Engineer" matching "Data Engineer" roles.
    2. If description is stub (<200 chars, e.g. LinkedIn HTML scrape),
       title match alone passes — no full JD to check skills against.
    3. Full description: also require ≥2 skill keyword hits.
    """
    title       = (job.get("title", "") or "").lower()
    description = (job.get("description", "") or "").lower()
    combined    = title + " " + description

    target_roles = profile.get("target_roles") or ["engineer"]
    role_match   = False

    for role in target_roles:
        words = [w.lower() for w in re.split(r"\W+", role) if len(w) > 2]
        if not words:
            continue
        # ALL words must appear in title (strict) OR all in combined (lenient)
        if all(w in title for w in words):
            role_match = True; break
        if all(w in combined for w in words):
            role_match = True; break

    if not role_match:
        return False

    # Stub description (LinkedIn HTML) — title match is sufficient
    if len(description.strip()) < 200:
        return True

    # Full description — require ≥2 skill keyword hits
    all_skills = (
        list(profile.get("skills", []))
        + list(profile.get("ml_skills", []))
        + list(profile.get("tools", []))
    )
    skill_hits = sum(
        1 for s in all_skills
        if s and len(s) > 2 and s.lower() in combined
    )
    return skill_hits >= 2


# ── The pipeline ──────────────────────────────────────────────────────────────

def _run(u: str, profile: dict, opts: dict):
    s        = get_state(u)
    max_apps = int(opts.get("max_apply", 40))
    roles    = opts.get("roles") or profile.get("target_roles", ["Software Engineer"])
    work     = opts.get("work_preference") or profile.get("work_preference", "Any")

    _log(u, f"━━ Pipeline start | roles={roles} | max={max_apps}")

    try:
        # ── 1. SCRAPE ─────────────────────────────────────────────────────────
        _phase(u, "scraping", "Scraping 12 job boards…")
        _log(u, "Scraping all sources for last 24h jobs…")

        import scraper
        raw_jobs = scraper.run(roles, work,
                emp_type=opts.get("employment_type_pref", "Any")
            )
        s["jobs_scraped"] = len(raw_jobs)
        _log(u, f"Scraped {len(raw_jobs)} jobs total")

        added, total = db.upsert_jobs(u, raw_jobs)
        s["jobs_new"] = added
        _log(u, f"{added} new, {total} total in DB")

        # ── 2. PRE-FILTER (keyword match, instant) ────────────────────────────
        _phase(u, "filtering", "Keyword filtering…")

        all_jobs = db.load_jobs(u)
        # Only look at jobs not yet processed
        unprocessed = [
            j for j in all_jobs
            if j.get("status") == "new"
        ]

        eligible = []
        skipped  = 0
        for job in unprocessed:
            if _keyword_match(job, profile):
                eligible.append(job)
            else:
                skipped += 1

        s["jobs_skipped"]  = skipped
        s["jobs_eligible"] = len(eligible)
        _log(u, f"Keyword filter: {len(eligible)} eligible, {skipped} skipped")

        if not eligible:
            _log(u, "No eligible jobs after keyword filter. Done.", "warn")
            _finish(u, s)
            return

        # Separate automatable vs manual-only
        # manual = HN (ycombinator.com), YC (workatastartup.com), or unknown
        def is_automatable(j):
            plat = (j.get("apply_platform") or "manual").lower()
            url  = (j.get("apply_url") or j.get("url") or "").lower()
            # Explicitly manual sources
            if "ycombinator.com" in url: return False
            if "workatastartup.com" in url: return False
            if "news.ycombinator" in url: return False
            # Known automatable platforms
            return plat in ("linkedin","greenhouse","lever","ashby",
                            "workable","smartrecruiters","indeed",
                            "icims","bamboohr","universal")

        def sort_key(j):
            plat = (j.get("apply_platform") or "").lower()
            if plat == "greenhouse": return 0  # API submit, no browser needed
            if plat == "lever":      return 1
            if plat == "ashby":      return 1
            if plat == "workable":   return 2
            if plat == "smartrecruiters": return 2
            if plat == "linkedin":   return 3  # needs session
            return 4

        auto_jobs   = [j for j in eligible if is_automatable(j)]
        manual_only = [j for j in eligible if not is_automatable(j)]

        # Debug: show platform breakdown of eligible jobs
        plat_counts = {}
        for j in eligible:
            p = j.get("apply_platform","manual")
            plat_counts[p] = plat_counts.get(p,0) + 1
        _log(u, f"Eligible by platform: {plat_counts}")
        _log(u, f"Automatable: {len(auto_jobs)} | Manual-only (HN/YC/unknown): {len(manual_only)}")

        # Mark manual-only jobs so they appear in Jobs list without wasting time
        for j in manual_only:
            db.update_job(u, j["id"], status="manual",
                          manual_reason="Source requires manual application (HN/YC/unknown ATS)",
                          manual_apply_url=j.get("url",""))
        s["jobs_manual"] += len(manual_only)

        auto_jobs.sort(key=sort_key)
        to_process = auto_jobs[:max_apps]
        _log(u, f"Will process top {len(to_process)} automatable jobs (capped at {max_apps})")

        # ── 3+4. RESUME + SUBMIT per job ──────────────────────────────────────
        import resume_generator as rg
        import auto_apply
        import shutil

        submitted = 0
        manual    = 0
        failed    = 0

        for idx, job in enumerate(to_process):
            if not s["running"]:
                _log(u, "Pipeline stopped by user.", "warn")
                break

            jid   = job["id"]
            label = f"{job.get('title','?')} @ {job.get('company','?')}"
            s["current_job"] = f"[{idx+1}/{len(to_process)}] {label}"

            # ── 3. Generate tailored resume ───────────────────────────────────
            _phase(u, "resuming", f"Resume {idx+1}/{len(to_process)}")
            _log(u, f"  [{idx+1}/{len(to_process)}] Generating resume → {label}")

            try:
                res = rg.generate(
                    profile,
                    job.get("description", ""),
                    job.get("title", "Role"),
                    job.get("company", "Company"),
                )
                if res.get("error"):
                    raise RuntimeError(res["error"])

                # Move to user-namespaced path
                src = Path(res["path"])
                dst = src.parent / f"{u}_{src.name}"
                if src.exists():
                    shutil.move(str(src), str(dst))
                    res["filename"] = dst.name
                    res["path"]     = str(dst)
                    res["url"]      = f"/api/resume/download/{dst.name}"

                db.update_job(u, jid,
                    resume_path     = res["path"],
                    resume_filename = res["filename"],
                    status          = "ready",
                    match_label     = res.get("match_label", ""),
                    match_reason    = res.get("match_reason", ""),
                    matched_keywords= res.get("matched_keywords", []),
                    missing_keywords= res.get("missing_keywords", []),
                )
                job["resume_path"] = res["path"]
                s["jobs_resumed"] += 1

            except Exception as exc:
                _log(u, f"  Resume error: {exc}", "error")
                db.update_job(u, jid, status="failed", apply_error=str(exc))
                failed += 1
                s["jobs_failed"] = failed
                continue

            # ── 4. Submit ─────────────────────────────────────────────────────
            _phase(u, "submitting", f"Submitting {idx+1}/{len(to_process)}")
            _log(u, f"  Submitting → {label}")

            try:
                result = auto_apply.apply_job(jid, profile, u)

                if result.get("success"):
                    _log(u, f"  ✓ Submitted: {label}")
                    submitted += 1
                    s["jobs_submitted"] = submitted

                elif result.get("manual") or result.get("pre_filled"):
                    reason = result.get("reason", "")[:80]
                    _log(u, f"  ✎ Manual needed: {label} — {reason}", "warn")
                    manual += 1
                    s["jobs_manual"] = manual

                else:
                    reason = result.get("reason", "")[:80]
                    _log(u, f"  ✗ Failed: {label} — {reason}", "error")
                    failed += 1
                    s["jobs_failed"] = failed

            except Exception as exc:
                _log(u, f"  Submit exception: {exc}", "error")
                db.update_job(u, jid, status="failed", apply_error=str(exc))
                failed += 1
                s["jobs_failed"] = failed

            # Polite delay — avoid triggering rate limits
            time.sleep(2)

        _log(u, (
            f"━━ Pipeline done | "
            f"scraped={s['jobs_scraped']} new={s['jobs_new']} "
            f"eligible={s['jobs_eligible']} "
            f"submitted={submitted} manual={manual} failed={failed}"
        ))

    except Exception as exc:
        import traceback
        traceback.print_exc()
        _log(u, f"Pipeline crashed: {exc}", "error")
        s["error"] = str(exc)

    finally:
        _finish(u, s)


def _finish(u: str, s: dict):
    s["running"]     = False
    s["phase"]       = "done"
    s["phase_label"] = "Done"
    s["finished_at"] = datetime.now().isoformat()
    s["current_job"] = ""


# ── Public API ────────────────────────────────────────────────────────────────

def start(u: str, profile: dict, opts: dict) -> bool:
    if is_running(u):
        return False
    s = get_state(u)
    s.update(_blank())
    s["running"]    = True
    s["started_at"] = datetime.now().isoformat()
    threading.Thread(target=_run, args=(u, profile, opts), daemon=True).start()
    return True


def stop(u: str):
    s = get_state(u)
    s["running"] = False


def status(u: str) -> dict:
    return dict(get_state(u))
