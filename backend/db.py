"""
db.py — Local file-based database
All data lives in ./data/  as JSON files.
Thread-safe with a per-file lock.
"""
import json, threading, time, hashlib, os
from pathlib import Path
from datetime import datetime

ROOT     = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

_locks: dict[str, threading.Lock] = {}

def _lock(name: str) -> threading.Lock:
    if name not in _locks:
        _locks[name] = threading.Lock()
    return _locks[name]

# ── Generic read / write ───────────────────────────────────────────────────────

def read(filename: str, default=None):
    path = DATA_DIR / filename
    with _lock(filename):
        if not path.exists():
            return default if default is not None else {}
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return default if default is not None else {}

def write(filename: str, data):
    path = DATA_DIR / filename
    with _lock(filename):
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False),
                        encoding="utf-8")

def append_list(filename: str, item: dict):
    """Append one item to a JSON array file."""
    arr = read(filename, [])
    arr.append(item)
    write(filename, arr)

# ── Profile ────────────────────────────────────────────────────────────────────

PROFILES_FILE = "profiles.json"

def hash_password(pw: str) -> str:
    try:
        import bcrypt
        return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    except ImportError:
        return hashlib.sha256(pw.encode()).hexdigest()

def check_password(pw: str, hashed: str) -> bool:
    try:
        import bcrypt
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ImportError:
        return hashlib.sha256(pw.encode()).hexdigest() == hashed

def create_profile(data: dict) -> dict:
    profiles = read(PROFILES_FILE, [])
    username = data.get("username", "").strip().lower()
    if any(p["username"] == username for p in profiles):
        return {"error": "Username already exists"}
    profile = {
        "id":               f"p_{int(time.time()*1000)}",
        "username":         username,
        "password_hash":    hash_password(data["password"]),
        "name":             data.get("name", ""),
        "email":            data.get("email", ""),
        "phone":            data.get("phone", ""),
        "linkedin":         data.get("linkedin", ""),
        "github":           data.get("github", ""),
        "website":          data.get("website", ""),
        "location":         data.get("location", ""),
        "title":            data.get("title", ""),
        "summary":          data.get("summary", ""),
        "years_experience": int(data.get("years_experience", 0)),
        "target_roles":     data.get("target_roles", []),
        "work_preference":  data.get("work_preference", "Any"),
        "skills":           data.get("skills", []),
        "ml_skills":        data.get("ml_skills", []),
        "tools":            data.get("tools", []),
        "experience":       data.get("experience", []),
        "education":        data.get("education", []),
        "projects":         data.get("projects", []),
        "certifications":   data.get("certifications", []),
        "awards":           data.get("awards", []),
        "base_resume_path": data.get("base_resume_path", ""),
        # ── Application form fields ──────────────────────────────────────
        # Contact & Identity
        "current_company":       data.get("current_company", ""),
        "middle_name":           data.get("middle_name", ""),
        "address_line1":         data.get("address_line1", ""),
        "address_city":          data.get("address_city", ""),
        "address_state":         data.get("address_state", ""),
        "address_zip":           data.get("address_zip", ""),
        "address_country":       data.get("address_country", "United States"),
        # Work eligibility
        "work_authorized":       data.get("work_authorized", "Yes"),
        "requires_sponsorship":  data.get("requires_sponsorship", "No"),
        "citizenship_status":    data.get("citizenship_status", "U.S. Citizen"),
        "visa_type":             data.get("visa_type", ""),
        # Compensation & logistics
        "salary_expectation":    data.get("salary_expectation", ""),
        "salary_min":            data.get("salary_min", ""),
        "salary_max":            data.get("salary_max", ""),
        "willing_to_relocate":   data.get("willing_to_relocate", "Yes"),
        "remote_preference":     data.get("remote_preference", "Open to both"),
        "start_date":            data.get("start_date", "2 weeks"),
        "notice_period":         data.get("notice_period", "2 weeks"),
        "employment_type":       data.get("employment_type", "Full-time"),
        # Education details
        "highest_degree":        data.get("highest_degree", "Master's Degree"),
        "degree_major":          data.get("degree_major", "Data Engineering"),
        "graduation_year":       data.get("graduation_year", ""),
        # EEO / Demographic
        "veteran_status":        data.get("veteran_status", "I am not a veteran"),
        "disability_status":     data.get("disability_status", "I do not have a disability"),
        "gender":                data.get("gender", "Prefer not to say"),
        "race_ethnicity":        data.get("race_ethnicity", "Prefer not to say"),
        "pronouns":              data.get("pronouns", ""),
        # Application preferences
        "referral_source":       data.get("referral_source", "LinkedIn"),
        "cover_letter_default":  data.get("cover_letter_default", ""),
        "portfolio_url":         data.get("portfolio_url", ""),
        "willing_background_check": data.get("willing_background_check", "Yes"),
        "willing_drug_test":     data.get("willing_drug_test", "Yes"),
        # Custom Q&A — anything the autofill doesn't handle automatically
        # Format: [{"question": "...", "answer": "..."}]
        "custom_answers":        data.get("custom_answers", []),
        "created_at":       datetime.now().isoformat(),
        "updated_at":       datetime.now().isoformat(),
    }
    profiles.append(profile)
    write(PROFILES_FILE, profiles)
    safe = {k: v for k, v in profile.items() if k != "password_hash"}
    return {"ok": True, "profile": safe}

def login_profile(username: str, password: str) -> dict:
    profiles = read(PROFILES_FILE, [])
    username = username.strip().lower()
    p = next((p for p in profiles if p["username"] == username), None)
    if not p:
        return {"error": "User not found"}
    if not check_password(password, p["password_hash"]):
        return {"error": "Incorrect password"}
    safe = {k: v for k, v in p.items() if k != "password_hash"}
    return {"ok": True, "profile": safe}

def get_profile(username: str) -> dict | None:
    profiles = read(PROFILES_FILE, [])
    p = next((p for p in profiles if p["username"] == username.lower()), None)
    if not p:
        return None
    return {k: v for k, v in p.items() if k != "password_hash"}

def update_profile(username: str, updates: dict) -> dict:
    profiles = read(PROFILES_FILE, [])
    username = username.strip().lower()
    for p in profiles:
        if p["username"] == username:
            # Never overwrite auth fields via update
            for k, v in updates.items():
                if k not in ("id", "username", "password_hash", "created_at"):
                    p[k] = v
            p["updated_at"] = datetime.now().isoformat()
            write(PROFILES_FILE, profiles)
            return {"ok": True, "profile": {k: v for k, v in p.items() if k != "password_hash"}}
    return {"error": "User not found"}

def profiles_exist() -> bool:
    profiles = read(PROFILES_FILE, [])
    return len(profiles) > 0

# ── Jobs ───────────────────────────────────────────────────────────────────────

def jobs_file(username: str) -> str:
    return f"jobs_{username}.json"

def load_jobs(username: str) -> list:
    return read(jobs_file(username), [])

def save_jobs(username: str, jobs: list):
    write(jobs_file(username), jobs)

def upsert_jobs(username: str, new_jobs: list) -> tuple[int, int]:
    """Merge new_jobs into existing, skipping duplicates. Returns (added, total)."""
    existing   = load_jobs(username)
    existing_ids = {j["id"] for j in existing}
    added = []
    for j in new_jobs:
        if j["id"] not in existing_ids:
            added.append(j)
    merged = existing + added
    save_jobs(username, merged)
    return len(added), len(merged)

def update_job(username: str, job_id: str, **fields):
    jobs = load_jobs(username)
    for j in jobs:
        if j["id"] == job_id:
            j.update(fields)
            j["updated_at"] = datetime.now().isoformat()
    save_jobs(username, jobs)

def get_job(username: str, job_id: str) -> dict | None:
    return next((j for j in load_jobs(username) if j["id"] == job_id), None)

# ── Activity log ───────────────────────────────────────────────────────────────

def log(username: str, message: str):
    ts   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {message}"
    print(line)
    path = DATA_DIR / f"activity_{username}.log"
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def get_activity(username: str, n: int = 60) -> list[str]:
    path = DATA_DIR / f"activity_{username}.log"
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8").strip().split("\n")
    return [l for l in reversed(lines[-n:]) if l.strip()]
