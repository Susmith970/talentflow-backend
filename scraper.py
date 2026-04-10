"""
TalentFlow Scraper — Maximum Coverage
Sources:
  1. LinkedIn       — public HTML search (multiple queries, pagination)
  2. LinkedIn RSS   — job alert RSS feeds (free, reliable)
  3. We Work Remotely — official RSS (4 categories)
  4. RemoteOK       — free JSON API
  5. Jobright.ai    — JSON-LD structured data
  6. Arbeitnow      — free REST API
  7. Jobicy         — free REST API
  8. HN Who's Hiring — Algolia API
  9. YC Jobs        — public JSON
 10. Greenhouse API — public board listings
 11. Lever          — public job board API
 12. Remotive       — free API

All sources deduplicated, filtered to last 24 hours, role-matched.
"""
import json, os, re, sys, time, urllib.request, urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from email.utils import parsedate_to_datetime

sys.path.insert(0, str(Path(__file__).parent))

ROOT         = Path(__file__).parent.parent
WINDOW_HOURS = int(os.environ.get("SCRAPE_WINDOW_HOURS", "24"))
UA_DESKTOP   = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE    = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
                "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")

# ─── Utilities ────────────────────────────────────────────────────────────────

def fetch(url, timeout=18, ua=UA_DESKTOP, extra=None, retries=2):
    for attempt in range(retries + 1):
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if extra:
            headers.update(extra)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                enc = r.headers.get("Content-Encoding", "")
                if "gzip" in enc:
                    import gzip
                    raw = gzip.decompress(raw)
                return raw.decode("utf-8", errors="replace")
        except Exception as e:
            if attempt < retries:
                time.sleep(1.5 * (attempt + 1))
            else:
                print(f"    ⚠  {url[:65]}: {e}")
    return ""


def clean(text):
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&[#\w]+;", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_recent(date_str, hours=None):
    if not date_str:
        return True
    cutoff = datetime.utcnow() - timedelta(hours=hours or WINDOW_HOURS)
    for fmt in ("%a, %d %b %Y %H:%M:%S %z", "%Y-%m-%dT%H:%M:%S%z",
                "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(date_str.strip()[:25], fmt).replace(tzinfo=None)
            return dt >= cutoff
        except Exception:
            pass
    try:
        return parsedate_to_datetime(date_str).replace(tzinfo=None) >= cutoff
    except Exception:
        return True


def work_type(loc):
    l = (loc or "").lower()
    return "Remote" if "remote" in l else "Hybrid" if "hybrid" in l else "On-site"


def job_category(title):
    ml = ["machine learning", "ml engineer", "data scientist", "deep learning",
          "nlp", "computer vision", "ai engineer", "llm", "applied scientist",
          "research scientist", "data engineer", "analytics engineer",
          "generative ai", "mlops", "genai", "artificial intelligence"]
    return "ML / Data Science" if any(k in title.lower() for k in ml) else "Software Engineering"


def detect_platform(url, apply_url=""):
    for u in [url or "", apply_url or ""]:
        u = u.lower()
        if "linkedin.com"          in u: return "linkedin"
        if "indeed.com"            in u: return "indeed"
        if "greenhouse.io"         in u: return "greenhouse"
        if "boards-api.greenhouse" in u: return "greenhouse"
        if "jobs.lever.co"         in u: return "lever"
        if "lever.co"              in u: return "lever"
        if "ashbyhq.com"           in u: return "ashby"
        if "workable.com"          in u: return "workable"
        if "smartrecruiters.com"   in u: return "smartrecruiters"
        if "myworkdayjobs.com"     in u: return "workday"
        if "icims.com"             in u: return "icims"
        if "bamboohr.com"          in u: return "bamboohr"
        if "taleo.net"             in u: return "manual"
        # News/community sites are NOT job application forms
        if "ycombinator.com"       in u: return "manual"
        if "news.ycombinator"      in u: return "manual"
        if "workatastartup.com"    in u: return "manual"
    return "manual"


def role_matches(title, desc, roles):
    combined = (title + " " + desc[:300]).lower()
    for role in roles:
        rl = role.lower().strip()
        if rl in combined:
            return True
        words = [w for w in re.split(r"\W+", rl) if len(w) > 2]
        if len(words) >= 2 and sum(1 for w in words if w in combined) >= max(1, len(words) - 1):
            return True
    return False


# detect_platform MUST be defined before make_job (it is — see above)
# US state names and common US location keywords for filtering
US_KEYWORDS = {
    "united states","usa","u.s.a","u.s.","remote","us remote","remote us",
    "anywhere","north america",
    # States
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    # Abbreviations
    " al "," ak "," az "," ar "," ca "," co "," ct "," de "," fl "," ga ",
    " hi "," id "," il "," in "," ia "," ks "," ky "," la "," me "," md ",
    " ma "," mi "," mn "," ms "," mo "," mt "," ne "," nv "," nh "," nj ",
    " nm "," ny "," nc "," nd "," oh "," ok "," or "," pa "," ri "," sc ",
    " sd "," tn "," tx "," ut "," vt "," va "," wa "," wv "," wi "," wy ",
    # Major cities
    "new york","los angeles","chicago","houston","phoenix","philadelphia",
    "san antonio","san diego","dallas","san jose","austin","jacksonville",
    "san francisco","columbus","charlotte","seattle","denver","boston",
    "washington dc","nashville","baltimore","memphis","louisville","portland",
    "atlanta","miami","minneapolis","raleigh","richmond","virginia beach",
}

def is_us_location(loc: str) -> bool:
    """Return True if the location is in the US or remote/unspecified."""
    if not loc or not loc.strip():
        return True   # no location = include (often remote)
    l = (" " + loc.lower() + " ")
    # Explicit non-US regions
    non_us = ["india","uk ","united kingdom","canada","australia","germany",
               "france","spain","italy","netherlands","brazil","mexico",
               "singapore","japan","china","europe","latam","apac","emea",
               "bangalore","mumbai","hyderabad","delhi","london","berlin",
               "paris","toronto","sydney","amsterdam","dublin","stockholm"]
    for region in non_us:
        if region in l:
            return False
    for kw in US_KEYWORDS:
        if kw in l:
            return True
    # Default include if ambiguous (e.g. "Remote" without country)
    if "remote" in l:
        return True
    return False


def make_job(uid, title, company, loc, source, url, posted, desc,
             salary="", tags=None, easy_apply=False, apply_url=None):
    _au = apply_url or url
    return {
        "id": uid, "title": title.strip(), "company": company.strip(),
        "location": (loc or "").strip(), "work_type": work_type(loc),
        "source": source, "url": url, "apply_url": _au,
        "posted": (posted or "Today")[:20],
        "description": clean(desc)[:2000],
        "salary": salary or "", "tags": (tags or [])[:10],
        "category": job_category(title),
        "easy_apply": easy_apply,
        "apply_platform": detect_platform(url, _au),
        # tracking
        "status": "new",
        "ats_score": 0, "match_label": "", "match_reason": "",
        "matched_keywords": [], "missing_keywords": [], "ats_tips": [],
        "resume_path": None, "resume_filename": None,
        "applied_at": None, "submitted_at": None, "notes": "",
        "scraped_at": datetime.utcnow().isoformat(),
        "updated_at": datetime.utcnow().isoformat(),
    }


def rss_field(item, tag):
    m = re.search(rf"<{tag}[^>]*><!\[CDATA\[(.*?)\]\]></{tag}>", item, re.DOTALL)
    if m: return m.group(1).strip()
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", item, re.DOTALL)
    return clean(m.group(1)) if m else ""


# ─── Source 1: LinkedIn HTML search (multiple pages) ─────────────────────────

def scrape_linkedin_html(roles):
    print("  [LinkedIn HTML] scraping …")
    jobs, seen = [], set()
    for role in roles[:8]:
        for start in (0, 25, 50):          # 3 pages per role = up to 75 results each
            q    = urllib.parse.quote(role)
            url  = (f"https://www.linkedin.com/jobs/search/"
                    f"?keywords={q}&f_TPR=r86400&sortBy=DD&start={start}")
            html = fetch(url, timeout=20)
            if not html:
                break

            ids    = re.findall(r'data-entity-urn="urn:li:jobPosting:(\d+)"', html)
            titles = re.findall(r'class="base-search-card__title"[^>]*>\s*(.*?)\s*</h3>',
                                html, re.DOTALL)
            comps  = re.findall(
                r'class="base-search-card__subtitle"[^>]*>.*?<a[^>]*>\s*(.*?)\s*</a>',
                html, re.DOTALL)
            locs   = re.findall(
                r'class="job-search-card__location"[^>]*>\s*(.*?)\s*</span>',
                html, re.DOTALL)
            dates  = re.findall(r'<time[^>]*datetime="([^"]+)"', html)

            if not ids:
                break  # no more pages

            for i, jid in enumerate(ids):
                if jid in seen: continue
                seen.add(jid)
                t = clean(titles[i]) if i < len(titles) else role
                c = clean(comps[i])  if i < len(comps)  else "Unknown"
                l = clean(locs[i])   if i < len(locs)   else ""
                d = dates[i]         if i < len(dates)  else ""
                if not t or not role_matches(t, "", roles): continue
                if not is_recent(d): continue
                job_url = f"https://www.linkedin.com/jobs/view/{jid}"
                _mj = make_job(
                    f"li_{jid}", t, c, l, "LinkedIn", job_url, d,
                    f"{t} at {c}. Open LinkedIn for full description.",
                    tags=[role.title()], easy_apply=False,
                    apply_url=job_url,   # real apply URL detected at apply time
                )
                if _mj: jobs.append(_mj)
            time.sleep(2.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 2: LinkedIn RSS (public job alert feeds) ─────────────────────────

def scrape_linkedin_rss(roles):
    print("  [LinkedIn RSS] scraping …")
    jobs, seen = [], set()
    for role in roles[:6]:
        q   = urllib.parse.quote(role)
        url = f"https://www.linkedin.com/jobs/search/?keywords={q}&f_TPR=r86400&sortBy=DD"
        xml = fetch(url)
        if not xml: continue
        for item in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL):
            title = rss_field(item, "title")
            link  = rss_field(item, "link") or rss_field(item, "guid")
            pub   = rss_field(item, "pubDate")
            desc  = rss_field(item, "description")[:800]
            if not link or link in seen or not is_recent(pub): continue
            if not role_matches(title, desc, roles): continue
            seen.add(link)
            # extract job ID from URL if possible
            jid_m = re.search(r"/(\d{8,})", link)
            uid   = f"lirss_{jid_m.group(1)}" if jid_m else f"lirss_{abs(hash(link))}"
            if uid in seen: continue
            seen.add(uid)
            _mj = make_job(uid, title, "Unknown", "", "LinkedIn RSS", link, pub, desc,
                                 easy_apply=False, apply_url=link)
            if _mj: jobs.append(_mj)
        time.sleep(1.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 3: We Work Remotely RSS ──────────────────────────────────────────

def scrape_weworkremotely(roles):
    print("  [We Work Remotely] scraping …")
    feeds = [
        "https://weworkremotely.com/categories/remote-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-data-science-jobs.rss",
        "https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss",
        "https://weworkremotely.com/categories/remote-full-stack-programming-jobs.rss",
        "https://weworkremotely.com/categories/remote-back-end-programming-jobs.rss",
    ]
    jobs, seen = [], set()
    for feed_url in feeds:
        xml = fetch(feed_url)
        if not xml: continue
        for item in re.findall(r"<item>(.*?)</item>", xml, re.DOTALL):
            raw  = rss_field(item, "title")
            link = rss_field(item, "link") or rss_field(item, "guid")
            pub  = rss_field(item, "pubDate")
            desc = rss_field(item, "description")[:800]
            comp = ""
            if ": " in raw: comp, raw = raw.split(": ", 1)
            if not link or link in seen or not is_recent(pub): continue
            if not role_matches(raw, desc, roles): continue
            seen.add(link)
            _mj = make_job(f"wwr_{abs(hash(link))}", raw,
                                 comp or "Unknown", "Remote",
                                 "We Work Remotely", link, pub, desc)
            if _mj: jobs.append(_mj)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 4: RemoteOK JSON API ─────────────────────────────────────────────

def scrape_remoteok(roles):
    print("  [RemoteOK] scraping …")
    raw = fetch("https://remoteok.com/api", extra={"Accept": "application/json"})
    if not raw: return []
    try: data = json.loads(raw)
    except Exception: return []
    cutoff = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
    jobs = []
    for j in data:
        if not isinstance(j, dict) or "position" not in j: continue
        ep = j.get("epoch", 0)
        if ep and datetime.utcfromtimestamp(ep) < cutoff: continue
        title = j.get("position", "")
        tags_raw = j.get("tags") or []
        tags_str  = " ".join(str(t) for t in tags_raw if t)
        if not role_matches(title, tags_str, roles): continue
        _mj = make_job(
            f"rok_{j.get('id','')}",
            title, j.get("company","Unknown"), "Remote", "RemoteOK",
            j.get("url", f"https://remoteok.com/remote-jobs/{j.get('id','')}"),
            datetime.utcfromtimestamp(ep).strftime("%Y-%m-%d") if ep else "Today",
            j.get("description",""),
            salary=j.get("salary",""), tags=[str(t) for t in (j.get("tags") or []) if t][:6],
        )
        if _mj: jobs.append(_mj)
    print(f"     ✓ {min(len(jobs),40)}")
    return jobs[:40]


# ─── Source 5: Jobright.ai JSON-LD ───────────────────────────────────────────

def scrape_jobright(roles):
    print("  [Jobright.ai] scraping …")
    jobs, seen = [], set()
    for role in roles[:6]:
        html = fetch(f"https://jobright.ai/jobs?query={urllib.parse.quote(role)}&datePosted=today")
        if not html: continue
        for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                                html, re.DOTALL):
            try:
                data = json.loads(block)
                if isinstance(data, dict) and data.get("@type") == "JobPosting":
                    items = [data]
                elif isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get("itemListElement", [])
                else:
                    continue

                for item in items:
                    if isinstance(item, dict) and item.get("item"):
                        item = item["item"]
                    if not isinstance(item, dict) or item.get("@type") != "JobPosting":
                        continue
                    title = item.get("title", "")
                    if not role_matches(title, item.get("description","")[:300], roles): continue
                    uid = str(item.get("identifier",{}).get("value", abs(hash(title))))
                    if uid in seen: continue
                    seen.add(uid)
                    if not is_recent(item.get("datePosted","")): continue
                    org  = item.get("hiringOrganization",{})
                    lo   = item.get("jobLocation",{})
                    if isinstance(lo, list): lo = lo[0] if lo else {}
                    addr = lo.get("address",{}) if isinstance(lo,dict) else {}
                    loc  = addr.get("addressLocality","") if isinstance(addr,dict) else ""
                    sal  = item.get("baseSalary",{})
                    sal_s = ""
                    if isinstance(sal,dict):
                        v = sal.get("value",{})
                        if isinstance(v,dict) and v.get("minValue") and v.get("maxValue"):
                            try: sal_s = f"${int(v['minValue']):,}–${int(v['maxValue']):,}"
                            except Exception: pass
                    _mj = make_job(
                        f"jr_{uid}", title,
                        org.get("name","Unknown") if isinstance(org,dict) else "Unknown",
                        loc, "Jobright",
                        item.get("url","https://jobright.ai"),
                        item.get("datePosted","Today"),
                        clean(item.get("description",""))[:1000], salary=sal_s,
                    )
                    if _mj: jobs.append(_mj)
            except Exception:
                pass
        time.sleep(1.0)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 6: Arbeitnow REST API ────────────────────────────────────────────

def scrape_arbeitnow(roles):
    print("  [Arbeitnow] scraping …")
    jobs, seen = [], set()
    for role in roles[:6]:
        for page in (1, 2):
            raw = fetch(f"https://www.arbeitnow.com/api/job-board-api"
                        f"?search={urllib.parse.quote(role)}&page={page}")
            if not raw: break
            try: data = json.loads(raw)
            except Exception: break
            batch = data.get("data", [])
            if not batch: break
            for j in batch:
                if not is_recent(j.get("created_at","")): continue
                title = j.get("title","")
                if not role_matches(title, j.get("description","")[:300], roles): continue
                uid = str(j.get("slug", abs(hash(title))))
                if uid in seen: continue
                seen.add(uid)
                loc = j.get("location","") or ("Remote" if j.get("remote") else "")
                _mj = make_job(
                    f"abn_{uid}", title, j.get("company_name","Unknown"), loc,
                    "Arbeitnow", j.get("url","https://arbeitnow.com"),
                    j.get("created_at","Today"), j.get("description","")[:1000],
                    tags=[str(t) for t in (j.get("tags") or []) if t][:5],
                )
                if _mj: jobs.append(_mj)
            time.sleep(0.8)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 7: Jobicy REST API ────────────────────────────────────────────────

def scrape_jobicy(roles):
    print("  [Jobicy] scraping …")
    tag_map = {
        "ml":"machine-learning","machine learning":"machine-learning",
        "data scientist":"machine-learning","data engineer":"data-engineering",
        "software":"software-engineering","backend":"software-engineering",
        "frontend":"software-engineering","devops":"devops","sre":"devops",
        "platform":"devops","cloud":"devops","ai engineer":"machine-learning",
        "llm":"machine-learning","nlp":"machine-learning",
    }
    tags = set()
    for r in roles:
        for k,v in tag_map.items():
            if k in r.lower(): tags.add(v)
    if not tags: tags.add("software-engineering")
    jobs, seen = [], set()
    for tag in list(tags)[:4]:
        raw = fetch(f"https://jobicy.com/api/v2/remote-jobs?count=50&tag={tag}")
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        for j in data.get("jobs",[]):
            if not is_recent(j.get("pubDate","")): continue
            title = j.get("jobTitle","")
            if not role_matches(title, j.get("jobDescription","")[:300], roles): continue
            uid = str(j.get("id", abs(hash(title))))
            if uid in seen: continue
            seen.add(uid)
            mn,mx = j.get("annualSalaryMin"), j.get("annualSalaryMax")
            try: sal = f"${int(mn):,}–${int(mx):,}" if mn and mx else ""
            except Exception: sal = ""
            _mj = make_job(
                f"jcy_{uid}", title, j.get("companyName","Unknown"),
                j.get("jobGeo","Remote"), "Jobicy",
                j.get("url","https://jobicy.com"), j.get("pubDate","Today"),
                j.get("jobDescription","")[:1000], salary=sal,
            )
            if _mj: jobs.append(_mj)
        time.sleep(0.8)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 8: HN Who's Hiring (Algolia) ─────────────────────────────────────

def scrape_hn(roles):
    print("  [HN Hiring] scraping …")
    thread_id = "40224955"
    try:
        raw = fetch("https://hn.algolia.com/api/v1/search"
                    "?query=who+is+hiring&tags=story&hitsPerPage=5")
        if raw:
            for h in json.loads(raw).get("hits",[]):
                if "who is hiring" in (h.get("title") or "").lower():
                    thread_id = h.get("objectID", thread_id); break
    except Exception: pass
    jobs, seen = [], set()
    for role in roles[:5]:
        raw = fetch(f"https://hn.algolia.com/api/v1/search"
                    f"?query={urllib.parse.quote(role)}"
                    f"&tags=comment,story_{thread_id}&hitsPerPage=20")
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        for hit in data.get("hits",[]):
            text = hit.get("comment_text","") or ""
            if not text or len(text) < 80: continue
            oid = hit.get("objectID","")
            if oid in seen: continue
            seen.add(oid)
            plain = re.sub(r"<[^>]+>"," ",text)
            if not role_matches(role, plain[:400], roles): continue
            first = plain.split("\n")[0].strip()[:55]
            # Extract any direct apply URL from the HN comment text
            apply_url_match = re.search(
                r'https?://[^\s<>"]+(?:apply|jobs|careers)[^\s<>"]*',
                plain[:500], re.IGNORECASE)
            direct_url = apply_url_match.group(0).rstrip(".,)") if apply_url_match else None
            hn_url = f"https://news.ycombinator.com/item?id={oid}"
            _mj = make_job(
                f"hn_{oid}", f"Engineer — {first}" if first else role,
                first[:40] or "HN Startup", "Often Remote", "HN Hiring",
                hn_url, hit.get("created_at","Today"), plain[:1000],
                apply_url=direct_url or hn_url,
            )
            if _mj: jobs.append(_mj)
        time.sleep(0.5)
    print(f"     ✓ {min(len(jobs),20)}")
    return jobs[:20]


# ─── Source 9: YC Work at a Startup ─────────────────────────────────────────

def scrape_yc(roles):
    print("  [YC Jobs] scraping …")
    jobs, seen = [], set()
    raw = fetch("https://www.workatastartup.com/jobs.json")
    if not raw or not raw.strip().startswith("["): return []
    try:
        for j in json.loads(raw):
            if not isinstance(j,dict): continue
            title = j.get("title") or j.get("role","")
            if not title or not role_matches(title, j.get("description","")[:300], roles): continue
            uid = str(j.get("id", abs(hash(title))))
            if uid in seen: continue
            seen.add(uid)
            comp = j.get("company",{})
            cn   = comp.get("name","Unknown") if isinstance(comp,dict) else str(comp)
            loc  = j.get("locations","Remote")
            if isinstance(loc,list): loc = ", ".join(loc) if loc else "Remote"
            _mj = make_job(
                f"yc_{uid}", title, cn, loc, "YC Jobs",
                f"https://www.workatastartup.com/jobs/{uid}",
                j.get("created_at","Today"), j.get("description","")[:1000],
                salary=j.get("salary",""), tags=j.get("skills",[])[:5],
            )
            if _mj: jobs.append(_mj)
    except Exception as e:
        print(f"    ⚠ YC: {e}")
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 10: Greenhouse public boards ─────────────────────────────────────

GREENHOUSE_BOARDS = [
    # Verified working as of 2025 (404s removed)
    "stripe", "figma", "vercel", "scaleai", "anthropic", "airbnb", "databricks",
    # Additional high-volume tech companies on Greenhouse
    "reddit", "discord", "duolingo", "robinhood", "plaid", "brex",
    "coinbase", "lyft", "dropbox", "zendesk", "twilio", "okta",
    "cloudflare", "hashicorp", "snowflakecomputing", "datadog",
    "elastic", "mongodb", "confluent", "dbt-labs", "prefect",
    "palantir", "asana", "notion-hq", "airtable",
]

def scrape_greenhouse(roles):
    print("  [Greenhouse boards] scraping …")
    jobs, seen = [], set()
    for board in GREENHOUSE_BOARDS:
        raw = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                    extra={"Accept":"application/json"})
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        for j in data.get("jobs",[]):
            title = j.get("title","")
            if not role_matches(title, "", roles): continue
            # Greenhouse jobs don't update daily - use 7-day window
            if not is_recent(j.get("updated_at",""), hours=168): continue
            uid = str(j.get("id",""))
            if uid in seen: continue
            seen.add(uid)
            loc = j.get("location",{}).get("name","") if isinstance(j.get("location"),dict) else ""
            url = j.get("absolute_url","")
            if not url: url = f"https://boards.greenhouse.io/{board}/jobs/{uid}"
            _mj = make_job(
                f"gh_{board}_{uid}", title, board.replace("-"," ").title(), loc,
                "Greenhouse", url, j.get("updated_at","Today"),
                j.get("content","")[:1000] if j.get("content") else f"{title} at {board}",
                apply_url=url,
            )
            if _mj: jobs.append(_mj)
        time.sleep(0.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 11: Lever public boards ──────────────────────────────────────────

LEVER_BOARDS = [
    "netflix","spotify","canva","miro","figma","notion","webflow",
    "brex","rippling","benchling","scale","weights-biases",
]

def scrape_lever(roles):
    print("  [Lever boards] scraping …")
    jobs, seen = [], set()
    for board in LEVER_BOARDS:
        raw = fetch(f"https://api.lever.co/v0/postings/{board}?mode=json",
                    extra={"Accept":"application/json"})
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        for j in data:
            title = j.get("text","")
            if not role_matches(title, j.get("descriptionPlain","")[:300], roles): continue
            # Lever createdAt is ms timestamp - use 7-day window for API jobs
            created = j.get("createdAt",0)
            if isinstance(created, (int,float)) and created > 1e9:
                from datetime import timezone
                dt = datetime.utcfromtimestamp(created/1000)
                if dt < datetime.utcnow() - timedelta(days=7): continue
            uid = j.get("id","")
            if uid in seen: continue
            seen.add(uid)
            loc_obj = j.get("categories",{})
            loc = loc_obj.get("location","") if isinstance(loc_obj,dict) else ""
            url = j.get("hostedUrl","") or f"https://jobs.lever.co/{board}/{uid}"
            _mj = make_job(
                f"lv_{board}_{uid}", title, board.replace("-"," ").title(), loc,
                "Lever", url, (
                    datetime.utcfromtimestamp(j["createdAt"]/1000).strftime("%Y-%m-%dT%H:%M:%SZ")
                    if isinstance(j.get("createdAt"), (int, float)) and j["createdAt"] > 1e9
                    else str(j.get("createdAt","Today"))
                ),
                j.get("descriptionPlain","")[:1000],
                apply_url=url+"/apply",
            )
            if _mj: jobs.append(_mj)
        time.sleep(0.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 12: Remotive API ─────────────────────────────────────────────────

def scrape_remotive(roles):
    print("  [Remotive] scraping …")
    cats = ["software-dev","data","devops-sysadmin","all"]
    jobs, seen = [], set()
    for cat in cats[:2]:
        raw = fetch(f"https://remotive.com/api/remote-jobs?category={cat}&limit=100",
                    extra={"Accept":"application/json"})
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        cutoff = datetime.utcnow() - timedelta(hours=WINDOW_HOURS)
        for j in data.get("jobs",[]):
            pub = j.get("publication_date","")
            try:
                dt = datetime.strptime(pub[:19], "%Y-%m-%dT%H:%M:%S")
                if dt < cutoff: continue
            except Exception: pass
            title = j.get("title","")
            if not role_matches(title, j.get("description","")[:300], roles): continue
            uid = str(j.get("id",""))
            if uid in seen: continue
            seen.add(uid)
            _mj = make_job(
                f"rem_{uid}", title, j.get("company_name","Unknown"),
                j.get("candidate_required_location","Remote"), "Remotive",
                j.get("url","https://remotive.com"), pub,
                j.get("description","")[:1000],
                salary=j.get("salary",""), tags=[str(t) for t in (j.get("tags") or []) if t][:6],
            )
            if _mj: jobs.append(_mj)
        time.sleep(0.8)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Main ─────────────────────────────────────────────────────────────────────

ALL_SCRAPERS = [
    ("LinkedIn HTML",    scrape_linkedin_html),
    ("LinkedIn RSS",     scrape_linkedin_rss),
    ("We Work Remotely", scrape_weworkremotely),
    ("RemoteOK",         scrape_remoteok),
    ("Jobright",         scrape_jobright),
    ("Arbeitnow",        scrape_arbeitnow),
    ("Jobicy",           scrape_jobicy),
    ("HN Hiring",        scrape_hn),
    ("YC Jobs",          scrape_yc),
    ("Greenhouse",       scrape_greenhouse),
    ("Lever boards",     scrape_lever),
    ("Remotive",         scrape_remotive),
]


def run(roles: list[str], work_pref: str = "Any",
        progress_cb=None) -> list[dict]:
    """
    Scrape all sources for the given roles.
    progress_cb(source_name, count_so_far) is called after each source.
    Returns list of new job dicts (already deduplicated).
    """
    print(f"\n🔍  Scraping {len(ALL_SCRAPERS)} sources | roles: {roles} | last {WINDOW_HOURS}h\n")
    t0 = time.time()

    all_jobs = []
    for name, fn in ALL_SCRAPERS:
        try:
            batch = fn(roles)
            # Filter to US locations only
            us_batch = [j for j in batch if is_us_location(j.get("location",""))]
            filtered_out = len(batch) - len(us_batch)
            if filtered_out > 0:
                print(f"     Filtered {filtered_out} non-US jobs from {name}")
            all_jobs.extend(us_batch)
        except Exception as e:
            print(f"  ⚠  {name}: {e}")
        if progress_cb:
            try: progress_cb(name, len(all_jobs))
            except Exception: pass
        time.sleep(0.3)

    # Drop None entries (non-US jobs filtered by make_job)
    all_jobs = [j for j in all_jobs if j is not None]

    # Work-preference filter
    if work_pref.lower() not in ("any","all",""):
        wt = work_pref.capitalize()
        filtered = [j for j in all_jobs if j.get("work_type") == wt]
        if filtered: all_jobs = filtered

    # Deduplicate by (norm-title, norm-company)
    seen_k, unique = set(), []
    for j in all_jobs:
        k = (re.sub(r"\W+","",j["title"].lower())[:35],
             re.sub(r"\W+","",j["company"].lower())[:20])
        if k not in seen_k:
            seen_k.add(k); unique.append(j)

    elapsed = round(time.time()-t0,1)
    by_src  = {}
    for j in unique: by_src[j["source"]] = by_src.get(j["source"],0)+1
    print(f"\n✅  {len(unique)} unique jobs scraped in {elapsed}s")
    print(f"    {by_src}")
    return unique
