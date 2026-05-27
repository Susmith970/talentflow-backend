"""TalentFlow Scraper — Maximum Coverage + FAANG Direct + H1B Sponsors + Apify
Sources:
  1. LinkedIn       — public HTML search (multiple queries, pagination)
  2. LinkedIn RSS   — job alert RSS feeds (free, reliable)
  3. We Work Remotely — official RSS (5 categories)
  4. RemoteOK       — free JSON API
  5. Jobright.ai    — JSON-LD structured data
  6. Arbeitnow      — free REST API
  7. Jobicy         — free REST API
  8. HN Who's Hiring — Algolia API
  9. YC Jobs        — public JSON
 10. Greenhouse API — public board listings (incl. H1B sponsors)
 11. Lever          — public job board API (incl. H1B sponsors)
 12. Remotive       — free API
 13. Otta           — tech jobs API
 14. FindWork.dev   — tech jobs API
 15. USAJobs.gov    — federal + contract tech jobs
 16. Google Jobs    — direct careers.google.com API
 17. Amazon Jobs    — direct amazon.jobs API
 18. Apple Jobs     — direct jobs.apple.com API
 19. Microsoft Jobs — direct careers.microsoft.com API
 20. Meta Jobs      — direct metacareers.com API
 21. Netflix Jobs   — direct jobs.netflix.com API
 22. Apify          — LinkedIn/Indeed scraper (APIFY_API_TOKEN env var)

All sources deduplicated, filtered to last 24 hours, role-matched.
"""
# Sources count: 22
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

def fetch(url, timeout=18, ua=UA_DESKTOP, extra=None, retries=2, method="GET", data=None):
    for attempt in range(retries + 1):
        headers = {
            "User-Agent": ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate",
        }
        if extra:
            headers.update(extra)
        body_bytes = None
        if data is not None:
            if isinstance(data, (dict, list)):
                body_bytes = json.dumps(data).encode("utf-8")
                headers.setdefault("Content-Type", "application/json")
            elif isinstance(data, str):
                body_bytes = data.encode("utf-8")
            elif isinstance(data, bytes):
                body_bytes = data
        req = urllib.request.Request(
            url, headers=headers, data=body_bytes,
            method=method if (body_bytes is not None or method != "GET") else None
        )
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
        if "ghc.greenhouse.io"     in u: return "greenhouse"
        if "jobs.lever.co"         in u: return "lever"
        if "lever.co"              in u: return "lever"
        if "ashbyhq.com"           in u: return "ashby"
        if "workable.com"          in u: return "workable"
        if "smartrecruiters.com"   in u: return "smartrecruiters"
        if "myworkdayjobs.com"     in u: return "workday"
        if "icims.com"             in u: return "icims"
        if "bamboohr.com"          in u: return "bamboohr"
        if "taleo.net"             in u: return "manual"
        if "ycombinator.com"       in u: return "manual"
        if "news.ycombinator"      in u: return "manual"
        if "workatastartup.com"    in u: return "manual"
        if "careers.google.com"    in u: return "greenhouse"
        if "amazon.jobs"           in u: return "amazon"
        if "jobs.apple.com"        in u: return "apple"
        if "careers.microsoft.com" in u: return "microsoft"
        if "metacareers.com"       in u: return "meta"
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


US_KEYWORDS = {
    "united states","usa","u.s.a","u.s.","remote","us remote","remote us",
    "anywhere","north america",
    "alabama","alaska","arizona","arkansas","california","colorado","connecticut",
    "delaware","florida","georgia","hawaii","idaho","illinois","indiana","iowa",
    "kansas","kentucky","louisiana","maine","maryland","massachusetts","michigan",
    "minnesota","mississippi","missouri","montana","nebraska","nevada",
    "new hampshire","new jersey","new mexico","new york","north carolina",
    "north dakota","ohio","oklahoma","oregon","pennsylvania","rhode island",
    "south carolina","south dakota","tennessee","texas","utah","vermont",
    "virginia","washington","west virginia","wisconsin","wyoming",
    " al "," ak "," az "," ar "," ca "," co "," ct "," de "," fl "," ga ",
    " hi "," id "," il "," in "," ia "," ks "," ky "," la "," me "," md ",
    " ma "," mi "," mn "," ms "," mo "," mt "," ne "," nv "," nh "," nj ",
    " nm "," ny "," nc "," nd "," oh "," ok "," or "," pa "," ri "," sc ",
    " sd "," tn "," tx "," ut "," vt "," va "," wa "," wv "," wi "," wy ",
    "new york","los angeles","chicago","houston","phoenix","philadelphia",
    "san antonio","san diego","dallas","san jose","austin","jacksonville",
    "san francisco","columbus","charlotte","seattle","denver","boston",
    "washington dc","nashville","baltimore","memphis","louisville","portland",
    "atlanta","miami","minneapolis","raleigh","richmond","virginia beach",
    "menlo park","mountain view","sunnyvale","cupertino","redmond","bellevue",
    "kirkland","palo alto","santa clara","san mateo","new york city","nyc",
}

def is_us_location(loc: str) -> bool:
    if not loc or not loc.strip():
        return True
    l = (" " + loc.lower() + " ")
    non_us = ["india","uk ","united kingdom","canada","australia","germany",
               "france","spain","italy","netherlands","brazil","mexico",
               "singapore","japan","china","europe","latam","apac","emea",
               "worldwide","global","international","anywhere in the world",
               "bangalore","mumbai","hyderabad","delhi","london","berlin",
               "paris","toronto","sydney","amsterdam","dublin","stockholm",
               "tel aviv","dubai","uae","pakistan","philippines","nigeria"]
    for region in non_us:
        if region in l:
            return False
    for kw in US_KEYWORDS:
        if kw in l:
            return True
    if "remote" in l:
        return True
    return False


def make_job(uid, title, company, loc, source, url, posted, desc,
             salary="", tags=None, easy_apply=False, apply_url=None):
    _au = apply_url or url
    _emp_text = (title + " " + desc).lower()
    if any(w in _emp_text for w in (
        "contract","contractor","contract-to-hire","c2h","c2c",
        "corp-to-corp","corp to corp","freelance","consultant",
        "temporary","temp role","temp position","1099","independent contractor",
        "contract position","contract role","contract opportunity",
        "6 month","6-month","12 month","12-month","contract to hire")):
        _emp_type = "Contract"
    elif any(w in _emp_text for w in ("part-time","part time","parttime","20 hours","20hrs")):
        _emp_type = "Part-time"
    else:
        _emp_type = "Full-time"

    return {
        "id": uid, "title": title.strip(), "company": company.strip(),
        "location": (loc or "").strip(), "work_type": work_type(loc),
        "employment_type": _emp_type,
        "source": source, "url": url, "apply_url": _au,
        "posted": (posted or "Today")[:20],
        "description": clean(desc)[:2000],
        "salary": salary or "", "tags": (tags or [])[:10],
        "category": job_category(title),
        "easy_apply": easy_apply,
        "apply_platform": detect_platform(url, _au),
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


# ─── Source 1: LinkedIn HTML search ──────────────────────────────────────────

def scrape_linkedin_html(roles):
    print("  [LinkedIn HTML] scraping …")
    jobs, seen = [], set()
    for role in roles[:8]:
        for start in (0, 25, 50):
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
                break

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
                    apply_url=job_url,
                )
                if _mj: jobs.append(_mj)
            time.sleep(2.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 2: LinkedIn RSS ───────────────────────────────────────────────────

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
    # Big Tech / Cloud
    "stripe", "anthropic", "airbnb", "databricks", "scaleai",
    "coinbase", "lyft", "dropbox", "twilio", "okta",
    "cloudflare", "datadog", "elastic", "mongodb",
    "asana", "airtable", "duolingo", "discord", "reddit",
    "robinhood", "github", "shopify", "zendesk",
    # Data / ML companies
    "huggingface", "together", "cohere", "mistral",
    "weights-biases", "great-expectations", "astronomer",
    "dbtlabs", "fivetran", "hightouch", "getcensus",
    # Fintech / Enterprise
    "plaid", "brex", "ramp", "mercury", "moderntreasury",
    "rippling", "gusto", "lattice", "leapsome",
    # Healthcare / Other
    "tempus", "flatiron", "komodohealth", "babylonhealth",
    # ── H1B Sponsor Tech Unicorns ──────────────────────────────────────────
    "openai", "snowflake", "palantir", "confluent", "cockroachdb",
    "rubrik", "toast", "launchdarkly", "hashicorp", "pagerduty",
    "benchling", "samsara", "block", "square",
    "pinterest", "snap", "twitter",
    "servicenow", "splunk", "new-relic", "fastly",
    "checkr", "gong", "highspot",
    # ── Financial Services — major H1B sponsors ────────────────────────────
    "bloomberg", "two-sigma", "citadel", "drw",
    "jane-street", "optiver", "imc-trading",
    "point72", "bridgewater", "aqr-capital",
    # BlackRock / JPMorgan / Goldman use various ATS — try anyway
    "blackrock-inc", "jpmc", "goldmansachs", "morganstanley",
    "citi-technology",
    # ── Automotive / Autonomous Vehicles ──────────────────────────────────
    "waymo", "cruise", "aurora-innovation", "zoox",
    # ── Healthcare / Biotech (H1B sponsors) ───────────────────────────────
    "modernatx", "illumina", "10xgenomics", "grail",
    "23andme", "guardanthealth", "exactsciences",
    # ── More consumer/enterprise tech ─────────────────────────────────────
    "doordash", "instacart", "opendoor",
    "scale-ai", "labelbox",
]

def scrape_greenhouse(roles):
    print("  [Greenhouse boards] scraping …")
    jobs, seen = [], set()
    for board in GREENHOUSE_BOARDS:
        raw = fetch(f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs",
                    extra={"Accept":"application/json"})
        if not raw:
            raw = fetch(f"https://job-boards.greenhouse.io/{board}/jobs.json",
                        extra={"Accept":"application/json"})
        if not raw: continue
        try: data = json.loads(raw)
        except Exception: continue
        for j in data.get("jobs",[]):
            title = j.get("title","")
            if not role_matches(title, "", roles): continue
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
    "netflix", "spotify", "notion", "webflow", "rippling",
    "linear", "vercel", "retool", "temporal", "ramp",
    "census", "dagster", "preset", "airbyte",
    "mixpanel", "amplitude", "segment", "heap",
    "grafana", "honeycomb", "chronosphere", "observe",
    "stytch", "clerk", "auth0", "okta",
    "figma", "miro", "loom", "notion",
    # ── H1B Sponsor additions ──────────────────────────────────────────────
    "openai", "scale-ai", "cohere-ai",
    "waymo", "cruise",
    "benchling", "osmo",
    "optiver",
    "perplexity-ai", "character-ai", "inflection-ai",
    "replit", "codeium", "cursor",
    "anduril", "palantir-technologies",
    "robinhood-markets",
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
    cats = ["data","software-dev","devops-sysadmin"]
    jobs, seen = [], set()
    for cat in cats:
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


# ─── Source 13: Otta ─────────────────────────────────────────────────────────

def scrape_otta(roles):
    print("  [Otta] scraping …")
    jobs, seen = [], set()
    try:
        raw = fetch("https://api.otta.com/graphql",
                    extra={"Content-Type":"application/json","Accept":"application/json"},
                    method="POST",
                    data='{"query":"{ jobs(limit:100,offset:0) { id title company { name } location salary { min max currency } url description } }"}')
        if raw:
            data = json.loads(raw)
            for j in (data.get("data",{}).get("jobs") or []):
                title = j.get("title","")
                if not role_matches(title,"",roles): continue
                uid = str(j.get("id",""))
                if uid in seen: continue
                seen.add(uid)
                _mj = make_job(f"otta_{uid}", title,
                    j.get("company",{}).get("name","Unknown"),
                    j.get("location","Remote"), "Otta",
                    j.get("url","https://app.otta.com"), "Today",
                    j.get("description","")[:1000])
                if _mj: jobs.append(_mj)
    except Exception as e:
        print(f"    ⚠  Otta: {e}")
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 14: FindWork.dev ─────────────────────────────────────────────────

def scrape_findwork(roles):
    print("  [FindWork] scraping …")
    jobs, seen = [], set()
    for role in roles[:4]:
        try:
            q   = urllib.parse.quote(role)
            raw = fetch(f"https://findwork.dev/api/jobs/?search={q}&location=usa&order_by=-date",
                        extra={"Authorization": "Token public"})
            if not raw: continue
            data = json.loads(raw)
            for j in (data.get("results") or []):
                title = j.get("role","")
                if not role_matches(title,"",roles): continue
                uid = str(j.get("id",""))
                if uid in seen: continue
                seen.add(uid)
                loc = j.get("location","") or ("Remote" if j.get("remote") else "")
                _mj = make_job(f"fw_{uid}", title,
                    j.get("company_name","Unknown"), loc or "USA", "FindWork",
                    j.get("url","https://findwork.dev"), j.get("date_posted","Today"),
                    j.get("text","")[:1000])
                if _mj: jobs.append(_mj)
        except Exception as e:
            print(f"    ⚠  FindWork: {e}")
        time.sleep(0.3)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 15: USAJobs.gov ───────────────────────────────────────────────────

def scrape_usajobs(roles):
    print("  [USAJobs] scraping …")
    jobs, seen = [], set()
    for role in roles[:3]:
        try:
            q   = urllib.parse.quote(role)
            raw = fetch(
                f"https://data.usajobs.gov/api/search?Keyword={q}&LocationName=United+States&ResultsPerPage=25&DatePosted=7",
                extra={"Host":"data.usajobs.gov","User-Agent":"talentflow/1.0"})
            if not raw: continue
            data = json.loads(raw)
            items = data.get("SearchResult",{}).get("SearchResultItems",[])
            for item in items:
                jd = item.get("MatchedObjectDescriptor",{})
                title = jd.get("PositionTitle","")
                if not role_matches(title,"",roles): continue
                uid = jd.get("PositionID","")
                if uid in seen: continue
                seen.add(uid)
                loc = jd.get("PositionLocationDisplay","USA")
                pay = jd.get("PositionRemuneration",[{}])
                sal = pay[0].get("MinimumRange","") if pay else ""
                url = jd.get("ApplyURI",[""])[0] if jd.get("ApplyURI") else jd.get("PositionURI","")
                desc = jd.get("UserArea",{}).get("Details",{}).get("JobSummary","")[:1000]
                _mj = make_job(f"usaj_{uid}", title,
                    jd.get("OrganizationName","US Government"), loc, "USAJobs",
                    url, jd.get("PublicationStartDate","Today")[:10], desc,
                    salary=sal)
                if _mj: jobs.append(_mj)
        except Exception as e:
            print(f"    ⚠  USAJobs: {e}")
        time.sleep(0.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 16: Google Jobs (direct careers API) ─────────────────────────────

def scrape_google_jobs(roles):
    print("  [Google Jobs] scraping …")
    jobs, seen = [], set()
    API_URLS = [
        "https://careers.google.com/api/jobs/jobs/?jobs_per_page=20&location=United+States&q={q}&jlo=en_US&hl=en",
        "https://careers.google.com/api/jobs/jobs/?jobs_per_page=20&q={q}&hl=en",
    ]
    EXTRA = {
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://careers.google.com/jobs/results/",
        "Origin": "https://careers.google.com",
        "X-Requested-With": "XMLHttpRequest",
    }
    for role in roles[:4]:
        q   = urllib.parse.quote(role)
        raw = ""
        for url_tpl in API_URLS:
            raw = fetch(url_tpl.format(q=q), extra=EXTRA)
            if raw and raw.strip().startswith("{"):
                break
            raw = ""
            time.sleep(0.8)

        parsed_jobs = []
        if raw:
            try:
                data = json.loads(raw)
                parsed_jobs = data.get("jobs") or []
            except Exception:
                pass

        # HTML fallback — parse JSON-LD from careers page
        if not parsed_jobs:
            html = fetch(
                f"https://careers.google.com/jobs/results/?jobs_per_page=20&q={q}&location=United+States&hl=en",
                extra={"Accept": "text/html,application/xhtml+xml,*/*;q=0.9", "Referer": "https://careers.google.com/"}
            )
            if html:
                for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
                    try:
                        obj = json.loads(block)
                        items = obj if isinstance(obj, list) else [obj]
                        for item in items:
                            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                                parsed_jobs.append({
                                    "title": item.get("title", ""),
                                    "description": item.get("description", ""),
                                    "id": str(abs(hash(item.get("title","") + item.get("datePosted","")))),
                                    "locations": [item.get("jobLocation", {}).get("address", {}).get("addressLocality", "United States")] if isinstance(item.get("jobLocation"), dict) else ["United States"],
                                    "apply_url": item.get("url", "https://careers.google.com/jobs/results/"),
                                    "publish_date": item.get("datePosted", "Today"),
                                    "categories": [item.get("occupationalCategory", "")],
                                })
                    except Exception:
                        pass

        for j in parsed_jobs:
            title = j.get("title", "")
            if not role_matches(title, j.get("description", "")[:300], roles):
                continue
            uid = str(j.get("id", j.get("job_id", "")))
            if not uid:
                uid = str(abs(hash(title)))
            if uid in seen:
                continue
            seen.add(uid)
            locs = j.get("locations", j.get("location", ["United States"]))
            if isinstance(locs, str):
                locs = [locs]
            loc = locs[0] if locs else "United States"
            if not is_us_location(loc):
                continue
            posted = j.get("publish_date", j.get("datePosted", j.get("date", "Today")))
            if not is_recent(posted, hours=168):
                continue
            apply_url = j.get("apply_url", j.get("job_url",
                              f"https://careers.google.com/jobs/results/{uid}"))
            cats = j.get("categories", j.get("department", []))
            if isinstance(cats, str):
                cats = [cats]
            _mj = make_job(
                f"goog_{uid}", title, "Google", loc, "Google Jobs",
                apply_url, posted,
                clean(j.get("description", ""))[:1500],
                tags=(cats or [])[:5],
                apply_url=apply_url,
            )
            if _mj:
                jobs.append(_mj)
        time.sleep(1.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 17: Amazon Jobs (direct API) ─────────────────────────────────────

def scrape_amazon_jobs(roles):
    print("  [Amazon Jobs] scraping …")
    jobs, seen = [], set()
    for role in roles[:4]:
        q = urllib.parse.quote(role)
        url = (f"https://www.amazon.jobs/en/search.json"
               f"?normalized_country_code[]=USA"
               f"&result_limit=15&sort=relevant&base_query={q}")
        raw = fetch(url, extra={
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://www.amazon.jobs/en/jobs",
        })
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except Exception:
            continue
        for j in (data.get("jobs") or []):
            title = j.get("title", "")
            desc  = j.get("description", j.get("basic_qualifications", ""))
            if not role_matches(title, desc[:300], roles):
                continue
            uid = str(j.get("id", j.get("job_id", "")))
            if not uid:
                uid = str(abs(hash(title)))
            if uid in seen:
                continue
            seen.add(uid)
            loc = j.get("normalized_location", j.get("location", "USA"))
            if not is_us_location(loc):
                continue
            posted = j.get("posted_date", j.get("updated_time", "Today"))
            if not is_recent(posted, hours=168):
                continue
            url_path = j.get("job_path", "")
            job_url = (f"https://www.amazon.jobs{url_path}" if url_path
                       else f"https://www.amazon.jobs/en/jobs/{uid}")
            cats = j.get("category_friendly", j.get("job_category", ""))
            _mj = make_job(
                f"amz_{uid}", title, "Amazon", loc, "Amazon Jobs",
                job_url, posted,
                clean(desc)[:1500],
                tags=[cats][:1] if cats else [],
                apply_url=job_url,
            )
            if _mj:
                jobs.append(_mj)
        time.sleep(1.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 18: Apple Jobs (direct API) ──────────────────────────────────────

def scrape_apple_jobs(roles):
    print("  [Apple Jobs] scraping …")
    jobs, seen = [], set()
    for role in roles[:4]:
        q = urllib.parse.quote(role)
        # Try multiple API param formats
        API_VARIANTS = [
            f"https://jobs.apple.com/api/role/search?search={q}&filters=LOC-USA&offset=0&sort=newest&limit=20",
            f"https://jobs.apple.com/api/role/search?search={q}&offset=0&sort=newest&limit=20",
            f"https://jobs.apple.com/en-us/search?search={q}&sort=newest&location=united-states-USA",
        ]
        EXTRA = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Referer": "https://jobs.apple.com/en-us/search",
            "X-Apple-Widget-Key": "af1139274f7b4cfdb1de68b2f604c6ed",
        }
        search_results = []
        for api_url in API_VARIANTS:
            raw = fetch(api_url, extra=EXTRA)
            if not raw:
                continue
            try:
                data = json.loads(raw)
                results = data.get("searchResults") or data.get("results") or []
                if results:
                    search_results = results
                    break
            except Exception:
                pass

        # HTML fallback
        if not search_results:
            html = fetch(
                f"https://jobs.apple.com/en-us/search?search={q}&sort=newest",
                extra={"Accept": "text/html,application/xhtml+xml,*/*;q=0.9"}
            )
            if html:
                for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
                    try:
                        obj = json.loads(block)
                        items = obj if isinstance(obj, list) else [obj]
                        for item in items:
                            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                                uid = str(abs(hash(item.get("title","") + item.get("datePosted",""))))
                                search_results.append({
                                    "positionId": uid,
                                    "postingTitle": item.get("title", ""),
                                    "jobSummary": item.get("description", ""),
                                    "postDateInGMT": item.get("datePosted", "Today"),
                                    "locations": [{"name": "USA"}],
                                    "_url": item.get("url", f"https://jobs.apple.com/en-us/details/{uid}"),
                                })
                    except Exception:
                        pass

        for j in search_results:
            title = j.get("postingTitle", j.get("name", j.get("title", "")))
            if not role_matches(title, j.get("jobSummary", j.get("description", ""))[:300], roles):
                continue
            uid = str(j.get("positionId", j.get("id", "")))
            if not uid:
                uid = str(abs(hash(title)))
            if uid in seen:
                continue
            seen.add(uid)
            locs = j.get("locations", [])
            if isinstance(locs, list) and locs:
                loc_obj = locs[0]
                loc = loc_obj.get("name", "USA") if isinstance(loc_obj, dict) else str(loc_obj)
            else:
                loc = "USA"
            if not is_us_location(loc):
                continue
            posted = j.get("postDateInGMT", j.get("datePosted", j.get("date", "Today")))
            if not is_recent(posted, hours=168):
                continue
            team = j.get("team", {})
            team_name = team.get("teamName", "") if isinstance(team, dict) else ""
            job_url = j.get("_url", f"https://jobs.apple.com/en-us/details/{uid}")
            _mj = make_job(
                f"appl_{uid}", title, "Apple", loc, "Apple Jobs",
                job_url, posted,
                clean(j.get("jobSummary", j.get("description", f"{title} position at Apple.")))[:1500],
                tags=[team_name][:1] if team_name else [],
                apply_url=job_url,
            )
            if _mj:
                jobs.append(_mj)
        time.sleep(1.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 19: Microsoft Jobs (direct API) ──────────────────────────────────

def scrape_microsoft_jobs(roles):
    print("  [Microsoft Jobs] scraping …")
    jobs, seen = [], set()
    for role in roles[:4]:
        q = urllib.parse.quote(role)
        # Try new URL first, fall back to old
        API_VARIANTS = [
            f"https://jobs.careers.microsoft.com/global/en/search?q={q}&lc=United+States&l=en_us&pg=1&pgSz=20&o=Relevance&flt=true",
            f"https://gcsservices.careers.microsoft.com/search/api/v1/search?q={q}&lc=United+States&l=en_us&pg=1&pgSz=20&o=Relevance&flt=true",
        ]
        EXTRA = {
            "Accept": "application/json, text/plain, */*",
            "Referer": "https://careers.microsoft.com/",
            "Origin": "https://careers.microsoft.com",
        }
        raw = ""
        for api_url in API_VARIANTS:
            raw = fetch(api_url, extra=EXTRA)
            if raw and raw.strip().startswith("{"):
                break
            raw = ""
            time.sleep(0.8)

        if not raw:
            time.sleep(1.5)
            continue
        try:
            data = json.loads(raw)
        except Exception:
            time.sleep(1.5)
            continue

        # Handle both old and new response structures
        result = (
            data.get("operationResult", {}).get("result", {})
            or data.get("result", {})
            or data
        )
        job_list = result.get("jobs") or result.get("value") or []

        for j in job_list:
            title = j.get("title", j.get("Title", ""))
            desc  = j.get("description", j.get("jobSummary", j.get("Description", "")))
            if not role_matches(title, desc[:300], roles):
                continue
            uid = str(j.get("jobId", j.get("JobId", j.get("id", ""))))
            if not uid:
                uid = str(abs(hash(title)))
            if uid in seen:
                continue
            seen.add(uid)
            loc = j.get("primaryWorkLocation", j.get("workLocation", j.get("PrimaryWorkLocation", "USA")))
            if not is_us_location(loc):
                continue
            posted = j.get("postedDate", j.get("PostedDate", j.get("updatedDate", "Today")))
            if not is_recent(posted, hours=168):
                continue
            job_url = f"https://careers.microsoft.com/us/en/job/{uid}"
            _mj = make_job(
                f"msft_{uid}", title, "Microsoft", loc, "Microsoft Jobs",
                job_url, posted,
                clean(desc)[:1500],
                apply_url=job_url,
            )
            if _mj:
                jobs.append(_mj)
        time.sleep(1.5)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 20: Meta Jobs (direct API) ───────────────────────────────────────

def scrape_meta_jobs(roles):
    print("  [Meta Jobs] scraping …")
    jobs, seen = [], set()
    for role in roles[:3]:
        q = urllib.parse.quote(role)
        url = f"https://www.metacareers.com/jobs/?q={q}&locations[0]=United+States"
        html = fetch(url, extra={
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.9",
            "Referer": "https://www.metacareers.com/",
        })
        if not html:
            continue
        # Try __NEXT_DATA__ embedded JSON
        next_m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if next_m:
            try:
                nd = json.loads(next_m.group(1))
                props    = nd.get("props", {})
                page_props = props.get("pageProps", {})
                jobs_list  = (page_props.get("jobs") or
                              page_props.get("initialData", {}).get("jobs") or [])
                for j in jobs_list[:30]:
                    title = j.get("title", j.get("name", ""))
                    if not role_matches(title, j.get("description", "")[:300], roles):
                        continue
                    uid = str(j.get("id", ""))
                    if not uid:
                        uid = str(abs(hash(title)))
                    if uid in seen:
                        continue
                    seen.add(uid)
                    loc_obj = j.get("location", j.get("jobLocation", {}))
                    if isinstance(loc_obj, dict):
                        loc = loc_obj.get("city", loc_obj.get("name", "Menlo Park, CA"))
                    else:
                        loc = str(loc_obj) if loc_obj else "Menlo Park, CA"
                    if not is_us_location(loc):
                        continue
                    posted = j.get("post_date", j.get("datePosted", "Today"))
                    if not is_recent(posted, hours=168):
                        continue
                    job_url = f"https://www.metacareers.com/jobs/{uid}"
                    _mj = make_job(
                        f"meta_{uid}", title, "Meta", loc, "Meta Jobs",
                        job_url, posted,
                        clean(j.get("description", ""))[:1500],
                        apply_url=job_url,
                    )
                    if _mj:
                        jobs.append(_mj)
            except Exception as e:
                print(f"    ⚠ Meta JSON parse: {e}")
        # Fallback: JSON-LD
        if not jobs:
            for block in re.findall(r'<script type="application/ld\+json">(.*?)</script>',
                                    html, re.DOTALL):
                try:
                    obj = json.loads(block)
                    if isinstance(obj, dict) and obj.get("@type") == "JobPosting":
                        objs = [obj]
                    elif isinstance(obj, list):
                        objs = obj
                    else:
                        continue
                    for item in objs:
                        if not isinstance(item, dict): continue
                        title = item.get("title", "")
                        if not role_matches(title, "", roles): continue
                        uid = str(abs(hash(title + item.get("datePosted", ""))))
                        if uid in seen: continue
                        seen.add(uid)
                        job_url = item.get("url", "https://www.metacareers.com/jobs")
                        _mj = make_job(
                            f"meta_{uid}", title, "Meta", "Menlo Park, CA",
                            "Meta Jobs", job_url,
                            item.get("datePosted", "Today"),
                            clean(item.get("description", ""))[:1500],
                            apply_url=job_url,
                        )
                        if _mj: jobs.append(_mj)
                except Exception:
                    pass
        time.sleep(2.0)
    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Source 21: Apify connector (LinkedIn + Indeed scrapers) ─────────────────
# Set APIFY_API_TOKEN env var to enable. Uses apify/linkedin-jobs-scraper.

def scrape_apify(roles):
    api_token = os.environ.get("APIFY_API_TOKEN", "")
    if not api_token:
        return []
    print("  [Apify] scraping …")
    jobs, seen = [], set()

    # LinkedIn jobs scraper via Apify
    actor_id = "bebity~linkedin-jobs-scraper"
    for role in roles[:3]:
        try:
            url = (f"https://api.apify.com/v2/acts/{actor_id}/"
                   f"run-sync-get-dataset-items"
                   f"?token={api_token}&timeout=90&memory=256")
            raw = fetch(url, method="POST", data={
                "keywords": role,
                "location": "United States",
                "datePosted": "past24Hours",
                "contractType": "fullTime",
                "maxResults": 30,
            }, extra={"Content-Type": "application/json"}, timeout=95)
            if not raw:
                continue
            data_list = json.loads(raw)
            if not isinstance(data_list, list):
                continue
            for j in data_list:
                title = j.get("position", j.get("title", ""))
                if not role_matches(title, j.get("description", "")[:300], roles):
                    continue
                job_url = j.get("jobUrl", j.get("url", ""))
                uid = str(j.get("id", abs(hash(job_url or title))))
                if uid in seen:
                    continue
                seen.add(uid)
                loc = j.get("location", "")
                if not is_us_location(loc):
                    continue
                posted = j.get("postedAt", j.get("publishedAt",
                               j.get("datePosted", "Today")))
                if not is_recent(posted):
                    continue
                company = j.get("companyName", j.get("company", "Unknown"))
                desc = (j.get("description") or
                        j.get("descriptionHtml") or
                        j.get("jobDescription", ""))
                _mj = make_job(
                    f"apify_li_{uid}", title, company, loc,
                    "Apify/LinkedIn", job_url, posted,
                    clean(desc)[:1500],
                    apply_url=j.get("applyUrl", job_url),
                    easy_apply=bool(j.get("easyApply")),
                )
                if _mj:
                    jobs.append(_mj)
        except Exception as e:
            print(f"    ⚠ Apify LinkedIn: {e}")
        time.sleep(2.0)

    # Indeed scraper via Apify (fallback)
    if len(jobs) < 10:
        indeed_actor = "misceres~indeed-scraper"
        for role in roles[:2]:
            try:
                url = (f"https://api.apify.com/v2/acts/{indeed_actor}/"
                       f"run-sync-get-dataset-items"
                       f"?token={api_token}&timeout=90&memory=256")
                raw = fetch(url, method="POST", data={
                    "position": role,
                    "country": "US",
                    "location": "United States",
                    "maxItems": 20,
                }, extra={"Content-Type": "application/json"}, timeout=95)
                if not raw:
                    continue
                data_list = json.loads(raw)
                if not isinstance(data_list, list):
                    continue
                for j in data_list:
                    title = j.get("positionName", j.get("title", ""))
                    if not role_matches(title, j.get("description", "")[:300], roles):
                        continue
                    job_url = j.get("url", j.get("jobUrl", ""))
                    uid = str(j.get("id", abs(hash(job_url or title))))
                    if uid in seen:
                        continue
                    seen.add(uid)
                    loc = j.get("location", "")
                    if not is_us_location(loc):
                        continue
                    company = j.get("company", j.get("companyName", "Unknown"))
                    desc = j.get("description", "")[:1500]
                    posted = j.get("postedAt", j.get("datePosted", "Today"))
                    _mj = make_job(
                        f"apify_ind_{uid}", title, company, loc,
                        "Apify/Indeed", job_url, posted,
                        clean(desc),
                        apply_url=j.get("externalApplyLink", job_url),
                    )
                    if _mj:
                        jobs.append(_mj)
            except Exception as e:
                print(f"    ⚠ Apify Indeed: {e}")
            time.sleep(2.0)

    print(f"     ✓ {len(jobs)}")
    return jobs


# ─── Main ─────────────────────────────────────────────────────────────────────

ALL_SCRAPERS = [
    ("LinkedIn HTML",    scrape_linkedin_html),
    ("LinkedIn RSS",     scrape_linkedin_rss),
    ("We Work Remotely", scrape_weworkremotely),
    ("RemoteOK",         scrape_remoteok),
    ("Otta",             scrape_otta),
    ("FindWork",         scrape_findwork),
    ("USAJobs",          scrape_usajobs),
    ("Jobright",         scrape_jobright),
    ("Arbeitnow",        scrape_arbeitnow),
    ("Jobicy",           scrape_jobicy),
    ("HN Hiring",        scrape_hn),
    ("YC Jobs",          scrape_yc),
    ("Greenhouse",       scrape_greenhouse),
    ("Lever boards",     scrape_lever),
    ("Remotive",         scrape_remotive),
    # FAANG direct
    ("Google Jobs",      scrape_google_jobs),
    ("Amazon Jobs",      scrape_amazon_jobs),
    ("Apple Jobs",       scrape_apple_jobs),
    ("Microsoft Jobs",   scrape_microsoft_jobs),
    ("Meta Jobs",        scrape_meta_jobs),
    # Apify (only runs if APIFY_API_TOKEN is set)
    ("Apify",            scrape_apify),
]


def run(roles: list[str], work_pref: str = "Any",
        emp_type: str = "Any", progress_cb=None) -> list[dict]:
    """
    Scrape all sources for the given roles.
    progress_cb(source_name, count_so_far) is called after each source.
    Returns list of new job dicts (already deduplicated).
    """
    print(f"\n\U0001f50d  Scraping {len(ALL_SCRAPERS)} sources | roles: {roles} | type: {emp_type} | work: {work_pref} | last {WINDOW_HOURS}h\n")
    t0 = time.time()

    all_jobs = []
    for name, fn in ALL_SCRAPERS:
        try:
            batch = fn(roles)
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

    all_jobs = [j for j in all_jobs if j is not None]

    if work_pref.lower() not in ("any","all",""):
        wt = work_pref.capitalize()
        filtered = [j for j in all_jobs if j.get("work_type") == wt]
        if filtered: all_jobs = filtered

    if emp_type and emp_type.lower() not in ("any","all",""):
        et = emp_type.strip().title()
        type_filtered = [j for j in all_jobs if j.get("employment_type","Full-time") == et]
        if type_filtered:
            all_jobs = type_filtered
            print(f"     Employment filter '{et}': {len(all_jobs)} jobs")

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
