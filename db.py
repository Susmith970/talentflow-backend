"""
db.py — Dual-mode database layer
  - Production (Railway): PostgreSQL via DATABASE_URL env var — persists forever
  - Local dev / fallback:  JSON files in ./data/

Zero changes required to any other file.
"""
import json, threading, time, hashlib, os
from pathlib import Path
from datetime import datetime

# ── Path setup (JSON fallback) ─────────────────────────────────────────────────
_here     = Path(__file__).parent
_data_env = os.environ.get("DATA_DIR", "")
if _data_env:
    DATA_DIR = Path(_data_env) / "data"
elif (_here / "data").exists() or not (_here.parent / "data").exists():
    DATA_DIR = _here / "data"
else:
    DATA_DIR = _here.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
ROOT = DATA_DIR.parent

# ── Postgres detection ─────────────────────────────────────────────────────────
# Railway injects PG* vars automatically when a Postgres service is linked.
# DATABASE_URL may need manual linking but PG* vars are always present.
DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    _pg_host = os.environ.get("PGHOST", "")
    _pg_port = os.environ.get("PGPORT", "5432")
    _pg_user = os.environ.get("PGUSER", "")
    _pg_pass = os.environ.get("PGPASSWORD", "")
    _pg_db   = os.environ.get("PGDATABASE", "")
    if _pg_host and _pg_user and _pg_db:
        DATABASE_URL = f"postgresql://{_pg_user}:{_pg_pass}@{_pg_host}:{_pg_port}/{_pg_db}"
        print(f"  [db] Built DATABASE_URL from PG* env vars (host={_pg_host})")
USE_POSTGRES = bool(DATABASE_URL)

_pg_lock = threading.Lock()
_pg_conn  = None

def _pg():
    global _pg_conn
    with _pg_lock:
        try:
            if _pg_conn and not _pg_conn.closed:
                _pg_conn.cursor().execute("SELECT 1")
                return _pg_conn
        except Exception:
            pass
        import psycopg2
        url = DATABASE_URL.replace("postgres://", "postgresql://", 1)
        _pg_conn = psycopg2.connect(url, sslmode="require", connect_timeout=10,
                                    keepalives=1, keepalives_idle=30)
        _pg_conn.autocommit = True
        return _pg_conn

def _init_pg():
    conn = _pg()
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS profiles (
                username   TEXT PRIMARY KEY,
                data       JSONB NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS jobs (
                username   TEXT NOT NULL,
                job_id     TEXT NOT NULL,
                data       JSONB NOT NULL,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (username, job_id)
            );
            CREATE TABLE IF NOT EXISTS tokens (
                token      TEXT PRIMARY KEY,
                username   TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE TABLE IF NOT EXISTS activity (
                id         BIGSERIAL PRIMARY KEY,
                username   TEXT NOT NULL,
                line       TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            );
            CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(username);
            CREATE INDEX IF NOT EXISTS idx_act_user  ON activity(username);
        """)
    print("  [db] PostgreSQL tables ready")

if USE_POSTGRES:
    try:
        _init_pg()
        print("  [db] Connected to PostgreSQL — profile + jobs persist across redeploys")
    except Exception as _pg_err:
        print(f"  [db] PostgreSQL FAILED: {_pg_err}")
        print(f"  [db] DATABASE_URL present: {bool(DATABASE_URL)}")
        print(f"  [db] Falling back to JSON files at {DATA_DIR}")
        USE_POSTGRES = False
else:
    print(f"  [db] No DATABASE_URL or PG* vars found — using JSON files at {DATA_DIR}")
    print(f"  [db] Set DATABASE_URL in Railway to enable persistence")

# ── JSON file helpers ──────────────────────────────────────────────────────────
_flocks: dict[str, threading.Lock] = {}
def _fl(name): 
    if name not in _flocks: _flocks[name] = threading.Lock()
    return _flocks[name]

def _fread(filename, default=None):
    path = DATA_DIR / filename
    with _fl(filename):
        if not path.exists(): return default if default is not None else {}
        try: return json.loads(path.read_text(encoding="utf-8"))
        except: return default if default is not None else {}

def _fwrite(filename, data):
    path = DATA_DIR / filename
    with _fl(filename):
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

# ── Password ──────────────────────────────────────────────────────────────────
def hash_password(pw):
    try:
        import bcrypt; return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()
    except ImportError: return hashlib.sha256(pw.encode()).hexdigest()

def check_password(pw, hashed):
    try:
        import bcrypt; return bcrypt.checkpw(pw.encode(), hashed.encode())
    except ImportError: return hashlib.sha256(pw.encode()).hexdigest() == hashed

# ── Profile helpers ────────────────────────────────────────────────────────────
def _build_profile(data):
    g = lambda k, d="": data.get(k, d)
    return {
        "id": f"p_{int(time.time()*1000)}", "username": g("username","").strip().lower(),
        "password_hash": hash_password(data["password"]),
        "name": g("name"), "email": g("email"), "phone": g("phone"),
        "linkedin": g("linkedin"), "github": g("github"), "website": g("website"),
        "location": g("location"), "title": g("title"), "summary": g("summary"),
        "years_experience": int(g("years_experience",0) or 0),
        "target_roles": g("target_roles",[]), "work_preference": g("work_preference","Any"),
        "skills": g("skills",[]), "ml_skills": g("ml_skills",[]), "tools": g("tools",[]),
        "experience": g("experience",[]), "education": g("education",[]),
        "projects": g("projects",[]), "certifications": g("certifications",[]),
        "awards": g("awards",[]), "base_resume_path": g("base_resume_path"),
        "raw_resume_text": g("raw_resume_text"),
        "current_company": g("current_company"), "middle_name": g("middle_name"),
        "address_line1": g("address_line1"), "address_city": g("address_city"),
        "address_state": g("address_state"), "address_zip": g("address_zip"),
        "address_country": g("address_country","United States"),
        "work_authorized": g("work_authorized","Yes"),
        "requires_sponsorship": g("requires_sponsorship","No"),
        "citizenship_status": g("citizenship_status","U.S. Citizen"),
        "visa_type": g("visa_type"),
        "salary_expectation": g("salary_expectation"), "salary_min": g("salary_min"),
        "salary_max": g("salary_max"), "willing_to_relocate": g("willing_to_relocate","Yes"),
        "remote_preference": g("remote_preference","Open to both"),
        "start_date": g("start_date","2 weeks"), "notice_period": g("notice_period","2 weeks"),
        "employment_type": g("employment_type","Full-time"),
        "highest_degree": g("highest_degree","Master's Degree"),
        "degree_major": g("degree_major","Data Engineering"),
        "graduation_year": g("graduation_year"),
        "veteran_status": g("veteran_status","I am not a veteran"),
        "disability_status": g("disability_status","I do not have a disability"),
        "gender": g("gender","Prefer not to say"), "race_ethnicity": g("race_ethnicity","Prefer not to say"),
        "pronouns": g("pronouns"), "referral_source": g("referral_source","LinkedIn"),
        "cover_letter_default": g("cover_letter_default"), "portfolio_url": g("portfolio_url"),
        "willing_background_check": g("willing_background_check","Yes"),
        "willing_drug_test": g("willing_drug_test","Yes"),
        "custom_answers": g("custom_answers",[]),
        "created_at": datetime.now().isoformat(), "updated_at": datetime.now().isoformat(),
    }

def _safe(p): return {k:v for k,v in p.items() if k != "password_hash"}
def _from_pg(row): return row[0] if isinstance(row[0], dict) else json.loads(row[0])

# ═══════════════════════════════════════════════════════════════════════════════
# PROFILE API
# ═══════════════════════════════════════════════════════════════════════════════

def create_profile(data):
    u = data.get("username","").strip().lower()
    if not u or not data.get("password"): return {"error": "username and password required"}
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT 1 FROM profiles WHERE username=%s", (u,))
                if cur.fetchone(): return {"error": "Username already exists"}
                p = _build_profile(data)
                cur.execute("INSERT INTO profiles(username,data) VALUES(%s,%s)",
                            (u, json.dumps(p)))
            return {"ok": True, "profile": _safe(p)}
        except Exception as e: return {"error": str(e)}
    else:
        profiles = _fread("profiles.json", [])
        if any(p["username"]==u for p in profiles): return {"error": "Username already exists"}
        p = _build_profile(data)
        profiles.append(p); _fwrite("profiles.json", profiles)
        return {"ok": True, "profile": _safe(p)}

def login_profile(username, password):
    u = username.strip().lower()
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM profiles WHERE username=%s", (u,))
                row = cur.fetchone()
            if not row: return {"error": "User not found"}
            p = _from_pg(row)
            if not check_password(password, p.get("password_hash","")): return {"error": "Incorrect password"}
            return {"ok": True, "profile": _safe(p)}
        except Exception as e: return {"error": str(e)}
    else:
        profiles = _fread("profiles.json", [])
        p = next((p for p in profiles if p["username"]==u), None)
        if not p: return {"error": "User not found"}
        if not check_password(password, p.get("password_hash","")): return {"error": "Incorrect password"}
        return {"ok": True, "profile": _safe(p)}

def get_profile(username):
    u = username.strip().lower()
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM profiles WHERE username=%s", (u,))
                row = cur.fetchone()
            return _safe(_from_pg(row)) if row else None
        except: return None
    else:
        p = next((p for p in _fread("profiles.json",[]) if p["username"]==u), None)
        return _safe(p) if p else None

def update_profile(username, updates):
    u = username.strip().lower()
    IMMUTABLE = {"id","username","password_hash","created_at"}
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM profiles WHERE username=%s", (u,))
                row = cur.fetchone()
                if not row: return {"error": "User not found"}
                p = _from_pg(row)
                for k,v in updates.items():
                    if k not in IMMUTABLE: p[k] = v
                p["updated_at"] = datetime.now().isoformat()
                cur.execute("UPDATE profiles SET data=%s,updated_at=NOW() WHERE username=%s",
                            (json.dumps(p), u))
            return {"ok": True, "profile": _safe(p)}
        except Exception as e: return {"error": str(e)}
    else:
        profiles = _fread("profiles.json",[])
        for p in profiles:
            if p["username"]==u:
                for k,v in updates.items():
                    if k not in IMMUTABLE: p[k]=v
                p["updated_at"]=datetime.now().isoformat()
                _fwrite("profiles.json",profiles)
                return {"ok":True,"profile":_safe(p)}
        return {"error":"User not found"}

def profiles_exist():
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT 1 FROM profiles LIMIT 1")
                return cur.fetchone() is not None
        except: return False
    return len(_fread("profiles.json",[])) > 0

# ═══════════════════════════════════════════════════════════════════════════════
# TOKENS
# ═══════════════════════════════════════════════════════════════════════════════

def save_token(token, username):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("INSERT INTO tokens(token,username) VALUES(%s,%s) ON CONFLICT(token) DO UPDATE SET username=EXCLUDED.username",
                            (token, username))
        except: pass
    else:
        t = _fread("tokens.json",{}); t[token]={"username":username,"created":datetime.now().isoformat()}
        _fwrite("tokens.json",t)

def get_token_user(token):
    if not token: return None
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT username FROM tokens WHERE token=%s",(token,))
                row=cur.fetchone()
            return row[0] if row else None
        except: return None
    else:
        t=_fread("tokens.json",{}); e=t.get(token)
        return e.get("username") if isinstance(e,dict) else None

def delete_token(token):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur: cur.execute("DELETE FROM tokens WHERE token=%s",(token,))
        except: pass
    else:
        t=_fread("tokens.json",{}); t.pop(token,None); _fwrite("tokens.json",t)

# ═══════════════════════════════════════════════════════════════════════════════
# JOBS
# ═══════════════════════════════════════════════════════════════════════════════

def load_jobs(username):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM jobs WHERE username=%s ORDER BY updated_at DESC",(username,))
                return [_from_pg(r) for r in cur.fetchall()]
        except: return []
    return _fread(f"jobs_{username}.json",[])

def save_jobs(username, jobs):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                for j in jobs:
                    cur.execute("INSERT INTO jobs(username,job_id,data) VALUES(%s,%s,%s) ON CONFLICT(username,job_id) DO UPDATE SET data=EXCLUDED.data,updated_at=NOW()",
                                (username,j["id"],json.dumps(j)))
        except: pass
    else:
        _fwrite(f"jobs_{username}.json",jobs)

def upsert_jobs(username, new_jobs):
    if USE_POSTGRES:
        try:
            added=0
            with _pg().cursor() as cur:
                for j in new_jobs:
                    cur.execute("SELECT 1 FROM jobs WHERE username=%s AND job_id=%s",(username,j["id"]))
                    if not cur.fetchone():
                        cur.execute("INSERT INTO jobs(username,job_id,data) VALUES(%s,%s,%s)",(username,j["id"],json.dumps(j)))
                        added+=1
                cur.execute("SELECT COUNT(*) FROM jobs WHERE username=%s",(username,))
                total=cur.fetchone()[0]
            return added,total
        except: return 0,0
    else:
        ex=load_jobs(username); ids={j["id"] for j in ex}
        added=[j for j in new_jobs if j["id"] not in ids]
        merged=ex+added; save_jobs(username,merged)
        return len(added),len(merged)

def update_job(username, job_id, **fields):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM jobs WHERE username=%s AND job_id=%s",(username,job_id))
                row=cur.fetchone()
                if not row: return
                j=_from_pg(row); j.update(fields); j["updated_at"]=datetime.now().isoformat()
                cur.execute("UPDATE jobs SET data=%s,updated_at=NOW() WHERE username=%s AND job_id=%s",
                            (json.dumps(j),username,job_id))
        except: pass
    else:
        jobs=load_jobs(username)
        for j in jobs:
            if j["id"]==job_id: j.update(fields); j["updated_at"]=datetime.now().isoformat()
        save_jobs(username,jobs)

def get_job(username, job_id):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT data FROM jobs WHERE username=%s AND job_id=%s",(username,job_id))
                row=cur.fetchone()
            return _from_pg(row) if row else None
        except: return None
    return next((j for j in load_jobs(username) if j["id"]==job_id),None)

# ═══════════════════════════════════════════════════════════════════════════════
# ACTIVITY LOG
# ═══════════════════════════════════════════════════════════════════════════════

def log(username, message):
    ts=datetime.now().strftime("%Y-%m-%d %H:%M:%S"); line=f"[{ts}] {message}"
    print(line)
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur: cur.execute("INSERT INTO activity(username,line) VALUES(%s,%s)",(username,line))
        except: pass
    else:
        path=DATA_DIR/f"activity_{username}.log"
        try:
            with open(path,"a",encoding="utf-8") as f: f.write(line+"\n")
        except: pass

def get_activity(username, n=60):
    if USE_POSTGRES:
        try:
            with _pg().cursor() as cur:
                cur.execute("SELECT line FROM activity WHERE username=%s ORDER BY created_at DESC LIMIT %s",(username,n))
                return [r[0] for r in cur.fetchall()]
        except: return []
    path=DATA_DIR/f"activity_{username}.log"
    if not path.exists(): return []
    lines=path.read_text(encoding="utf-8").strip().split("\n")
    return [l for l in reversed(lines[-n:]) if l.strip()]
