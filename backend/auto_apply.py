"""
auto_apply.py — Job Application Engine
=======================================

Supported platforms (in order of reliability):
  linkedin     — Easy Apply wizard via Playwright + saved session
  greenhouse   — Public REST API first, Playwright fallback
  lever        — Public /apply form via Playwright
  ashby        — Public form via Playwright
  workable     — Public form via Playwright
  smartrecruiters — Public form via Playwright
  indeed       — Login + form via Playwright
  icims        — Universal form filler
  bamboohr     — Universal form filler
  universal    — Generic form filler for unknown ATS

LinkedIn setup (run once):
    python backend/auto_apply.py --save-session
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import db

ROOT         = Path(__file__).parent.parent
DATA_DIR     = ROOT / "data"
SESSION_FILE = DATA_DIR / "linkedin_session.json"

DATA_DIR.mkdir(exist_ok=True)


# ── Utilities ─────────────────────────────────────────────────────────────────

def _jitter(lo=0.6, hi=1.8):
    import random
    time.sleep(lo + random.random() * (hi - lo))


def _nope(platform: str, reason: str, job: dict) -> dict:
    db.log("system", f"  [manual] {platform}: {reason[:100]}")
    return {
        "success":   False,
        "manual":    True,
        "platform":  platform,
        "reason":    reason,
        "apply_url": job.get("apply_url") or job.get("url", ""),
    }


def _pw():
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWT
        return sync_playwright, PWT
    except ImportError:
        return None, None


def _safe_fill(el, value: str):
    try:
        if el.is_visible() and not el.is_disabled() and not (el.input_value() or "").strip():
            el.fill(str(value))
            _jitter(0.1, 0.3)
    except Exception:
        pass


# ── Cover letter (AI or template) ─────────────────────────────────────────────

def _cover_letter(profile: dict, job: dict) -> str:
    key = os.environ.get("ANTHROPIC_API_KEY", "")
    title   = job.get("title", "the role")
    company = job.get("company", "your company")
    name    = profile.get("name", "")
    skills  = ", ".join((profile.get("skills", []) + profile.get("ml_skills", []))[:5])
    yrs     = profile.get("years_experience", "several")

    if not key:
        return (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {title} position at {company}. "
            f"With {yrs} years of experience and expertise in {skills or 'software engineering'}, "
            f"I am confident I would be a strong addition to your team.\n\n"
            f"Best regards,\n{name}"
        )
    try:
        import anthropic
        msg = anthropic.Anthropic(api_key=key).messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=400,
            messages=[{"role": "user", "content": (
                f"Write a 3-paragraph cover letter (max 180 words) for:\n"
                f"Role: {title} at {company}\n"
                f"Candidate: {name}, {yrs} years exp, skills: {skills}\n"
                f"JD: {job.get('description','')[:400]}\n\n"
                f"Address 'Hiring Team'. No clichés. Sign with candidate name. "
                f"Return ONLY the letter text."
            )}],
        )
        return msg.content[0].text.strip()
    except Exception:
        return (
            f"Dear Hiring Team,\n\n"
            f"I am excited to apply for the {title} position at {company}. "
            f"My {yrs} years of experience makes me a strong fit.\n\n"
            f"Best regards,\n{name}"
        )


# ── Smart answer / select / fill engines ─────────────────────────────────────
# These map every common ATS form field to a profile value.
# Profile fields used (all editable in Settings → Application Questions):
#   name, email, phone, location, linkedin, github, website
#   middle_name, address_line1, address_city, address_state, address_zip, address_country
#   current_company, years_experience, employment_type
#   work_authorized, requires_sponsorship, citizenship_status, visa_type
#   salary_expectation, salary_min, salary_max
#   willing_to_relocate, remote_preference, start_date, notice_period
#   highest_degree, degree_major, graduation_year
#   veteran_status, disability_status, gender, race_ethnicity, pronouns
#   referral_source, willing_background_check, willing_drug_test
#   cover_letter_default, portfolio_url
#   custom_answers: [{"question":"...","answer":"..."}]  — catch-all for anything else


def _answer(question: str, profile: dict) -> str:
    """
    Map any application question text to a profile value.
    Checks custom_answers first so the user can override anything.
    """
    q = (question or "").lower().strip()
    if not q:
        return ""

    # ── Check custom Q&A overrides first ────────────────────────────────────
    for qa in (profile.get("custom_answers") or []):
        if not isinstance(qa, dict): continue
        saved_q = (qa.get("question") or "").lower()
        if saved_q and (saved_q in q or q in saved_q):
            return str(qa.get("answer",""))

    # ── Name fields ──────────────────────────────────────────────────────────
    name_parts = (profile.get("name","") or "").split()
    if any(x in q for x in ("first name","first_name","given name","fname")):
        return name_parts[0] if name_parts else ""
    if any(x in q for x in ("last name","last_name","surname","family name","lname")):
        return " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
    if any(x in q for x in ("middle name","middle_name")):
        return profile.get("middle_name","")
    if q in ("name","full name","your name","legal name","candidate name"):
        return profile.get("name","")

    # ── Contact ──────────────────────────────────────────────────────────────
    if any(x in q for x in ("email","e-mail","email address")):
        return profile.get("email","")
    if any(x in q for x in ("phone","mobile","telephone","cell","contact number")):
        return profile.get("phone","")

    # ── Address ──────────────────────────────────────────────────────────────
    if any(x in q for x in ("street address","address line 1","address1","street")):
        return profile.get("address_line1","") or profile.get("location","")
    if any(x in q for x in ("city","town","municipality")):
        city = profile.get("address_city","")
        if not city:
            loc = profile.get("location","")
            city = loc.split(",")[0].strip() if loc else ""
        return city
    if any(x in q for x in ("state","province","region")) and "united" not in q:
        state = profile.get("address_state","")
        if not state:
            loc = profile.get("location","")
            parts = [p.strip() for p in loc.split(",")]
            state = parts[1] if len(parts) > 1 else ""
        return state
    if any(x in q for x in ("zip","postal code","postcode","zip code")):
        return profile.get("address_zip","")
    if any(x in q for x in ("country","nation")):
        return profile.get("address_country","United States")
    if any(x in q for x in ("location","where are you located","city, state")):
        return profile.get("location","")

    # ── Professional links ────────────────────────────────────────────────────
    if "linkedin" in q:
        li = profile.get("linkedin","")
        if li and not li.startswith("http"): li = "https://linkedin.com/in/" + li.lstrip("/")
        return li
    if "github" in q:
        gh = profile.get("github","")
        if gh and not gh.startswith("http"): gh = "https://github.com/" + gh.lstrip("/")
        return gh
    if any(x in q for x in ("website","portfolio","personal site","personal url")):
        return profile.get("portfolio_url","") or profile.get("website","") or profile.get("github","")

    # ── Current employment ────────────────────────────────────────────────────
    if any(x in q for x in ("current company","current employer","where do you work",
                              "current organization","present employer")):
        return profile.get("current_company","")
    if any(x in q for x in ("currently employed","are you employed","employment status")):
        return "Yes" if profile.get("current_company") else "No"

    # ── Work authorization ────────────────────────────────────────────────────
    if any(x in q for x in ("authorized to work","eligible to work","legally authorized",
                              "right to work","work authorization","work in the us",
                              "work in the united states","permitted to work")):
        return profile.get("work_authorized","Yes")
    if any(x in q for x in ("require sponsorship","need sponsorship","visa sponsorship",
                              "will you require","sponsor","h1b","h-1b")):
        return profile.get("requires_sponsorship","No")
    if any(x in q for x in ("citizenship","citizen","citizenship status","immigration status")):
        return profile.get("citizenship_status","U.S. Citizen")
    if any(x in q for x in ("visa type","visa status","current visa")):
        return profile.get("visa_type","")

    # ── Salary ───────────────────────────────────────────────────────────────
    if any(x in q for x in ("salary expectation","expected salary","desired salary",
                              "salary requirement","compensation expectation","what salary",
                              "minimum salary","base salary","target compensation")):
        sal = profile.get("salary_expectation","")
        if sal: return str(sal)
        yrs = int(profile.get("years_experience",3) or 3)
        return f"${90000 + yrs * 8000:,}"
    if "salary minimum" in q or "minimum salary" in q or "salary min" in q:
        return str(profile.get("salary_min","") or profile.get("salary_expectation",""))
    if "salary maximum" in q or "maximum salary" in q or "salary max" in q:
        return str(profile.get("salary_max","") or profile.get("salary_expectation",""))

    # ── Logistics ────────────────────────────────────────────────────────────
    if any(x in q for x in ("willing to relocate","open to relocation","relocate","relocation")):
        return profile.get("willing_to_relocate","Yes")
    if any(x in q for x in ("remote","work from home","hybrid","in office","on site","onsite")):
        return profile.get("remote_preference","Open to both")
    if any(x in q for x in ("start date","available to start","when can you start",
                              "earliest start","availability")):
        return profile.get("start_date","2 weeks")
    if any(x in q for x in ("notice period","how much notice","current notice")):
        return profile.get("notice_period","2 weeks")
    if any(x in q for x in ("employment type","job type","full time","part time","contract")):
        return profile.get("employment_type","Full-time")

    # ── Experience ───────────────────────────────────────────────────────────
    if re.search(r"years.{0,25}(experience|exp)", q) or re.search(r"how many years", q):
        return str(profile.get("years_experience",3) or 3)
    if any(x in q for x in ("years of experience","experience level","seniority level")):
        return str(profile.get("years_experience",3) or 3)

    # ── Education ────────────────────────────────────────────────────────────
    if any(x in q for x in ("highest degree","highest level of education","highest education",
                              "education level","degree level","academic level")):
        return profile.get("highest_degree","Master's Degree")
    if any(x in q for x in ("degree","major","field of study","area of study")):
        edu = profile.get("education") or []
        if edu:
            deg = edu[0].get("degree","")
            return deg if deg else profile.get("highest_degree","Master's Degree")
        return profile.get("highest_degree","Master's Degree")
    if any(x in q for x in ("major","concentration","area of study","field of study")):
        return profile.get("degree_major","Data Engineering")
    if any(x in q for x in ("graduation year","graduated","year of graduation","when did you graduate")):
        gyr = profile.get("graduation_year","")
        if not gyr:
            edu = profile.get("education") or []
            if edu:
                dates = edu[0].get("dates","")
                m = re.search(r"(20\d{2})", str(dates))
                gyr = m.group(1) if m else ""
        return str(gyr)
    if any(x in q for x in ("school","university","college","institution")):
        edu = profile.get("education") or []
        return edu[0].get("school","") if edu else ""

    # ── EEO / Demographic ────────────────────────────────────────────────────
    if any(x in q for x in ("veteran","military service","military status","protected veteran")):
        return profile.get("veteran_status","I am not a veteran")
    if any(x in q for x in ("disability","disabled","accommodation")):
        return profile.get("disability_status","I do not have a disability")
    if re.search(r"gender|sex", q):
        return profile.get("gender","Prefer not to say")
    if any(x in q for x in ("race","ethnicity","hispanic","latino","ancestry")):
        return profile.get("race_ethnicity","Prefer not to say")
    if any(x in q for x in ("pronoun","preferred pronoun")):
        return profile.get("pronouns","") or "Prefer not to say"

    # ── Background / compliance ───────────────────────────────────────────────
    if any(x in q for x in ("background check","criminal background","background screening")):
        return profile.get("willing_background_check","Yes")
    if any(x in q for x in ("drug test","substance test","drug screen")):
        return profile.get("willing_drug_test","Yes")

    # ── Referral source ───────────────────────────────────────────────────────
    if any(x in q for x in ("hear about","how did you find","referral","source","referred by",
                              "where did you hear")):
        return profile.get("referral_source","LinkedIn")

    # ── Generic yes/no defaults ───────────────────────────────────────────────
    yes_patterns = ["agree","consent","acknowledge","confirm","understand",
                    "18 years","18 or older","us person","willing","able to",
                    "available","authorized","eligible"]
    if any(x in q for x in yes_patterns):
        return "Yes"

    return ""


def _select(label: str, options: list, profile: dict) -> str:
    """Pick the best dropdown option for any application question."""
    lab  = (label or "").lower()
    opts = [str(o) for o in options if str(o).strip() and str(o).strip() not in ("--","Select","Choose","- Select -","Please select","N/A")]
    if not opts:
        return ""
    ol = [o.lower() for o in opts]

    def first(*kws):
        for kw in kws:
            for i, o in enumerate(ol):
                if kw in o: return opts[i]
        return None

    def match_answer(answer: str):
        """Find option that best matches a plain-text answer."""
        if not answer: return None
        al = answer.lower()
        # Exact match
        for i, o in enumerate(ol):
            if o == al: return opts[i]
        # Contains match
        for i, o in enumerate(ol):
            if al in o or o in al: return opts[i]
        # First word match
        aw = al.split()[0] if al.split() else ""
        for i, o in enumerate(ol):
            if aw and aw in o: return opts[i]
        return None

    # Work authorization
    if any(x in lab for x in ("authorized","eligible","right to work","work authorization")):
        auth = profile.get("work_authorized","Yes")
        if auth == "Yes": return first("yes","authorized","citizen","permanent") or opts[0]
        return first("no","not authorized") or opts[-1]

    # Sponsorship
    if any(x in lab for x in ("sponsor","sponsorship","visa")):
        if profile.get("requires_sponsorship","No") == "No":
            return first("no","will not","do not") or opts[0]
        return first("yes","will require") or opts[-1]

    # Citizenship
    if "citizen" in lab or "immigration" in lab:
        cs = profile.get("citizenship_status","U.S. Citizen")
        return match_answer(cs) or first("citizen","us citizen","permanent") or opts[0]

    # Experience level / seniority
    if any(x in lab for x in ("experience level","seniority","level","career level","grade")):
        yrs = int(profile.get("years_experience",3) or 3)
        if yrs >= 10: return first("staff","principal","director","vp","10","executive") or opts[-1]
        if yrs >= 7:  return first("staff","senior","lead","sr","7","8","9") or opts[0]
        if yrs >= 4:  return first("senior","mid","sr","iii","4","5","6") or opts[0]
        if yrs >= 2:  return first("mid","associate","ii","2","3") or opts[0]
        return first("junior","entry","associate","i","0","1") or opts[0]

    # Employment type
    if any(x in lab for x in ("employment type","job type","work type","position type")):
        et = (profile.get("employment_type","Full-time") or "Full-time").lower()
        if "contract" in et: return first("contract","contractor","1099") or opts[0]
        if "part" in et:     return first("part-time","part time") or opts[0]
        return first("full-time","full time","permanent","regular") or opts[0]

    # Education / degree
    if any(x in lab for x in ("education","degree","highest","qualification","academic")):
        deg = (profile.get("highest_degree","Master's Degree") or "").lower()
        if "phd" in deg or "doctor" in deg: return first("phd","doctorate","doctoral") or opts[0]
        if "master" in deg: return first("master","ms ","m.s","mba","graduate") or opts[0]
        if "bachelor" in deg: return first("bachelor","bs ","b.s","ba ","b.a","undergrad") or opts[0]
        return first("bachelor","undergraduate","some college") or opts[0]

    # Salary / compensation
    if any(x in lab for x in ("salary","compensation","pay range","hourly rate")):
        sal = profile.get("salary_expectation","")
        if sal: return match_answer(str(sal)) or opts[0]
        return opts[0]

    # Relocation
    if "relocat" in lab:
        rel = profile.get("willing_to_relocate","Yes")
        if rel == "Yes": return first("yes","willing","open to") or opts[0]
        return first("no","not willing","unable") or opts[-1]

    # Remote / work arrangement
    if any(x in lab for x in ("remote","work arrangement","hybrid","work location","office")):
        pref = (profile.get("remote_preference","Open to both") or "").lower()
        if "remote" in pref: return first("remote","fully remote","100% remote") or opts[0]
        if "office" in pref or "on-site" in pref: return first("onsite","on-site","office") or opts[0]
        return first("hybrid","flexible","open","remote") or opts[0]

    # Veteran
    if "veteran" in lab or "military" in lab:
        vs = (profile.get("veteran_status","I am not a veteran") or "").lower()
        if "not" in vs: return first("not","no","i am not","non-veteran","0") or opts[0]
        if "disabled" in vs: return first("disabled veteran","service-connected") or opts[0]
        return first("veteran","yes","protected") or opts[0]

    # Disability
    if "disab" in lab:
        ds = (profile.get("disability_status","I do not have a disability") or "").lower()
        if "not" in ds or "no" in ds: return first("no","not","i don","do not","0") or opts[0]
        return first("yes","i have","1") or opts[0]

    # Gender
    if re.search(r"gender|sex(?:$| )", lab):
        gd = (profile.get("gender","Prefer not to say") or "").lower()
        if gd == "male": return first("male","man") or opts[0]
        if gd == "female": return first("female","woman") or opts[0]
        return first("prefer not","decline","other","non-binary") or opts[0]

    # Race / ethnicity
    if any(x in lab for x in ("race","ethnic","hispanic","origin")):
        re_val = (profile.get("race_ethnicity","Prefer not to say") or "").lower()
        return match_answer(re_val) or first("prefer not","decline","not specified") or opts[0]

    # Background check / drug test
    if "background" in lab or "drug" in lab:
        return first("yes","agree","consent","i consent") or opts[0]

    # Referral
    if any(x in lab for x in ("source","hear","referral","how did")):
        ref = profile.get("referral_source","LinkedIn")
        return match_answer(ref) or first("linkedin","internet","website","online") or opts[0]

    # Start / availability
    if any(x in lab for x in ("start","available","notice")):
        sd = (profile.get("start_date","2 weeks") or "").lower()
        if "immediately" in sd or "now" in sd: return first("immediately","now","asap") or opts[0]
        if "2 week" in sd or "two week" in sd: return first("2 week","two week") or opts[0]
        if "1 month" in sd or "30" in sd: return first("1 month","30 day","four week") or opts[0]
        return first("2 week","two week","flexible") or opts[0]

    # Yes/No generic
    yes_labels = ["agree","consent","acknowledge","authorized","eligible","willing",
                   "available","able","confirm","18","us person","authorized to"]
    if any(x in lab for x in yes_labels):
        return first("yes","i agree","i consent","agree","true") or opts[0]

    # Fallback: try to match using _answer
    answer = _answer(label, profile)
    if answer:
        return match_answer(answer) or opts[0]

    return opts[0]


def _get_label(page, element) -> str:
    """Try every strategy to get the form field question label."""
    try:
        return page.evaluate("""(el) => {
            // 1. aria-label attribute
            const al = el.getAttribute('aria-label');
            if (al && al.trim()) return al.trim();
            // 2. aria-labelledby
            const alb = el.getAttribute('aria-labelledby');
            if (alb) {
                const l = document.getElementById(alb);
                if (l) return l.innerText.trim();
            }
            // 3. <label for="id">
            if (el.id) {
                const l = document.querySelector('label[for="' + el.id + '"]');
                if (l) return l.innerText.trim();
            }
            // 4. Walk up DOM looking for label/legend/heading
            let p = el.parentElement;
            for (let i = 0; i < 8; i++) {
                if (!p) break;
                const l = p.querySelector('label,legend,.label,.form-label,.field-label,[class*="label"],[class*="Label"]');
                if (l && l !== el && !l.contains(el)) return l.innerText.trim();
                // Also check for heading-style question text
                const h = p.querySelector('h3,h4,h5,[class*="question"],[class*="Question"]');
                if (h && h !== el) return h.innerText.trim();
                p = p.parentElement;
            }
            // 5. placeholder or name attribute
            return el.getAttribute('placeholder') || el.getAttribute('name') || '';
        }""", element.element_handle())
    except Exception:
        return ""


def _fill_form(page, profile: dict, job: dict, cover: str) -> bool:
    """
    Fill EVERY visible form field on the current page using profile data.
    Handles: text, email, tel, number, select, radio, checkbox, textarea.
    Returns True if a submit button was found and clicked.
    """
    name_parts  = (profile.get("name","") or "").split()
    resume_path = job.get("resume_path") or profile.get("base_resume_path","")

    # ── 1. Resume upload ─────────────────────────────────────────────────────
    if resume_path and Path(resume_path).exists():
        for fi in page.locator("input[type=file]").all():
            try:
                if fi.is_visible():
                    fi.set_input_files(resume_path)
                    _jitter(1.5, 3.0)
                    break
            except Exception:
                pass

    # ── 2. Text / email / tel / number / url inputs ──────────────────────────
    # Include Workday custom inputs (data-uxi='textField') and React inputs
    for inp in page.locator(
        "input[type=text]:visible,"
        "input[type=email]:visible,"
        "input[type=tel]:visible,"
        "input[type=number]:visible,"
        "input[type=url]:visible,"
        "input[data-uxi='textField']:visible,"
        "input[data-automation-id='textInput']:visible,"
        "input:not([type]):visible"
    ).all():
        try:
            if not inp.is_visible() or inp.is_disabled():
                continue
            if (inp.input_value() or "").strip():
                continue   # already filled

            itype    = (inp.get_attribute("type") or "text").lower()
            attrs    = " ".join(filter(None, [
                inp.get_attribute("id") or "",
                inp.get_attribute("name") or "",
                inp.get_attribute("aria-label") or "",
                inp.get_attribute("placeholder") or "",
                inp.get_attribute("autocomplete") or "",
            ])).lower()
            label    = _get_label(page, inp) or ""
            combined = (attrs + " " + label).lower()

            val = ""

            # ── Explicit field-type matching ──────────────────────────────
            if itype == "email" or "email" in combined:
                val = profile.get("email","")
            elif itype == "tel" or any(x in combined for x in ("phone","mobile","tel","cell")):
                val = profile.get("phone","")
            elif itype == "url":
                if "linkedin" in combined: val = profile.get("linkedin","")
                elif "github" in combined: val = profile.get("github","")
                else: val = profile.get("portfolio_url","") or profile.get("website","")

            # ── Name variants ─────────────────────────────────────────────
            elif any(x in combined for x in ("first name","first_name","given name","fname")):
                val = name_parts[0] if name_parts else ""
            elif any(x in combined for x in ("last name","last_name","surname","family","lname")):
                val = " ".join(name_parts[1:]) if len(name_parts) > 1 else ""
            elif any(x in combined for x in ("middle name","middle_name","middle initial")):
                val = profile.get("middle_name","")
            elif any(x in combined for x in ("full name","legal name","your name","candidate name")):
                val = profile.get("name","")
            elif combined.strip() == "name" or combined.endswith(" name"):
                val = profile.get("name","")

            # ── Address ───────────────────────────────────────────────────
            elif any(x in combined for x in ("street address","address1","address line 1","street")):
                val = profile.get("address_line1","") or profile.get("location","")
            elif any(x in combined for x in ("city","town")) and "new york" not in combined:
                val = profile.get("address_city","") or (profile.get("location","").split(",")[0].strip())
            elif any(x in combined for x in ("state","province")) and "statement" not in combined:
                val = profile.get("address_state","") or (profile.get("location","").split(",")[-1].strip() if "," in profile.get("location","") else "")
            elif any(x in combined for x in ("zip","postal","postcode")):
                val = profile.get("address_zip","")
            elif "country" in combined:
                val = profile.get("address_country","United States")
            elif any(x in combined for x in ("location","where are you based","city, state")):
                val = profile.get("location","")

            # ── Professional links ────────────────────────────────────────
            elif "linkedin" in combined:
                li = profile.get("linkedin","")
                val = ("https://linkedin.com/in/" + li.lstrip("/")) if li and not li.startswith("http") else li
            elif "github" in combined:
                gh = profile.get("github","")
                val = ("https://github.com/" + gh.lstrip("/")) if gh and not gh.startswith("http") else gh
            elif any(x in combined for x in ("website","portfolio","personal url","personal site")):
                val = profile.get("portfolio_url","") or profile.get("website","") or profile.get("github","")

            # ── Company ───────────────────────────────────────────────────
            elif any(x in combined for x in ("current company","current employer","company name","employer","organization")):
                val = profile.get("current_company","")

            # ── Numeric fields ────────────────────────────────────────────
            elif itype == "number":
                if any(x in combined for x in ("salary","compensation","pay")):
                    sal = profile.get("salary_expectation","")
                    val = re.sub(r"[^\d]","",str(sal)) if sal else str(90000 + int(profile.get("years_experience",3) or 3)*8000)
                elif any(x in combined for x in ("year","experience","years")):
                    val = str(profile.get("years_experience",3) or 3)
                elif any(x in combined for x in ("zip","postal")):
                    val = profile.get("address_zip","")

            # ── Use _answer() for everything else ─────────────────────────
            else:
                val = _answer(label or combined, profile)

            if val:
                inp.fill(str(val))
                _jitter(0.1, 0.3)
        except Exception:
            pass

    # ── 3. Select dropdowns (including Workday custom dropdowns) ────────────
    for sel_el in page.locator(
        "select:visible, "
        "[data-uxi='selectWidget']:visible, "
        "[data-automation-id='selectDropdown']:visible"
    ).all():
        try:
            if sel_el.is_disabled():
                continue
            current = sel_el.input_value() or ""
            if current and current not in ("","--","0","Select","Choose","Please select"):
                continue   # already has a value

            opts  = []
            for o in sel_el.locator("option").all():
                try: opts.append(o.inner_text().strip())
                except Exception: pass

            label = _get_label(page, sel_el) or sel_el.get_attribute("name") or sel_el.get_attribute("aria-label") or ""
            best  = _select(label, opts, profile)
            if best:
                try:    sel_el.select_option(label=best)
                except Exception:
                    try: sel_el.select_option(value=best)
                    except Exception: pass
                _jitter(0.1, 0.2)
        except Exception:
            pass

    # ── 4. Radio buttons ────────────────────────────────────────────────────
    # Try fieldsets first (most reliable grouping)
    for fs in page.locator("fieldset:visible").all():
        try:
            radios = fs.locator("input[type=radio]").all()
            if not radios or any(r.is_checked() for r in radios):
                continue

            legend = ""
            try:    legend = fs.locator("legend").first.inner_text()
            except Exception: pass

            # Also try heading/label near the fieldset
            if not legend:
                try:
                    legend = page.evaluate("""(el) => {
                        let p = el.previousElementSibling;
                        for (let i=0; i<3; i++) {
                            if (!p) break;
                            const t = p.innerText?.trim();
                            if (t && t.length < 200) return t;
                            p = p.previousElementSibling;
                        }
                        return '';
                    }""", fs.element_handle())
                except Exception: pass

            labels = []
            for lbl in fs.locator("label").all():
                try: labels.append(lbl.inner_text().strip())
                except Exception: labels.append("")

            answer   = _answer(legend, profile)
            best_idx = 0
            for i, lt in enumerate(labels):
                lt_l = lt.lower()
                ans_l = answer.lower() if answer else ""
                # Check various match strategies
                if ans_l and ans_l in lt_l:
                    best_idx = i; break
                if ans_l and lt_l in ans_l:
                    best_idx = i; break
                # Yes/No specific
                if ans_l == "yes" and lt_l in ("yes","y","true","1"): best_idx = i; break
                if ans_l == "no"  and lt_l in ("no","n","false","0"): best_idx = i; break

            if best_idx < len(radios):
                radios[best_idx].check()
            _jitter(0.1, 0.2)
        except Exception:
            pass

    # Also handle ungrouped radio buttons (not in fieldset)
    for grp_name in set():
        pass  # handled above

    # ── 5. Checkboxes ───────────────────────────────────────────────────────
    for cb in page.locator("input[type=checkbox]:visible").all():
        try:
            if cb.is_checked() or cb.is_disabled():
                continue
            label = _get_label(page, cb) or ""
            # Auto-check agreement/consent checkboxes
            agree_words = ["agree","consent","acknowledge","terms","privacy","policy",
                           "18 years","18 or older","accurate","truthful","certify",
                           "confirm","understand","authorized"]
            if any(x in label.lower() for x in agree_words):
                cb.check()
                _jitter(0.1, 0.2)
        except Exception:
            pass

    # ── 6. Textareas ────────────────────────────────────────────────────────
    for ta in page.locator("textarea:visible").all():
        try:
            if not ta.is_visible() or ta.is_disabled():
                continue
            if (ta.input_value() or "").strip():
                continue

            label = _get_label(page, ta) or ""
            ll    = label.lower()

            # Cover letter
            if any(x in ll for x in ["cover letter","cover_letter","covering letter",
                                       "letter of interest","why do you want","why are you",
                                       "tell us why","motivation","message to","introduction"]):
                # Use custom cover letter if saved, otherwise generate
                cl = profile.get("cover_letter_default","") or cover
                ta.fill(cl)
                _jitter(0.5, 1.0)

            # Additional info / anything else
            elif any(x in ll for x in ["additional","anything else","other information",
                                         "comments","anything you","supplement","more info"]):
                ta.fill("")   # leave blank unless user has custom answer
                # Check custom_answers for this
                ans = _answer(label, profile)
                if ans: ta.fill(ans); _jitter(0.3, 0.6)

            # Generic long text
            elif len(ll) > 5:
                ans = _answer(label, profile)
                if ans:
                    ta.fill(ans)
                    _jitter(0.3, 0.6)

        except Exception:
            pass

    # ── 7. Find and click Submit ────────────────────────────────────────────
    submit_selectors = [
        "button[aria-label*='Submit application']:visible",
        "button[aria-label*='Submit Application']:visible",
        "button[data-qa='submit-application']:visible",
        "button[type=submit]:has-text('Submit Application'):visible",
        "button[type=submit]:has-text('Submit my application'):visible",
        "button[type=submit]:has-text('Submit'):visible",
        "button[type=submit]:has-text('Apply Now'):visible",
        "button[type=submit]:has-text('Apply'):visible",
        "button[type=submit]:has-text('Send Application'):visible",
        "button[type=submit]:has-text('Send'):visible",
        "input[type=submit][value*='Submit']:visible",
        "input[type=submit][value*='Apply']:visible",
        "input[type=submit]:visible",
        "button[type=submit]:visible",
    ]
    for sel in submit_selectors:
        try:
            btn = page.locator(sel)
            if btn.count() > 0:
                btn.first.scroll_into_view_if_needed()
                btn.first.click()
                _jitter(2, 4)
                return True
        except Exception:
            pass

    return False


# ── LinkedIn session ───────────────────────────────────────────────────────────

def save_session():
    """Open visible browser, let user log in, save cookies."""
    sp, _ = _pw()
    if not sp:
        print("Install playwright: pip install playwright && playwright install chromium")
        return
    print("\nA browser will open. Log in to LinkedIn, then press ENTER here.\n")
    with sp() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"))
        page = ctx.new_page()
        page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded", timeout=30000)
        input(">>> Press ENTER once you see your LinkedIn feed … ")
        if "linkedin.com" not in page.url:
            print("Not on LinkedIn — try again.")
            browser.close()
            return
        ctx.storage_state(path=str(SESSION_FILE))
        browser.close()
    print(f"✓ Session saved → {SESSION_FILE}")


def _li_browser(p):
    """Return (browser, ctx, page, error) using saved session or credentials."""
    UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
          "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36")

    # Fast-fail: check if we have any credentials before launching browser
    has_session = SESSION_FILE.exists()
    le = os.environ.get("LINKEDIN_EMAIL", "")
    lp = os.environ.get("LINKEDIN_PASSWORD", "")
    if not has_session and not (le and lp):
        # Return a dummy browser that we never close + error
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        return browser, None, None, (
            "LinkedIn session not saved. "
            "Run once: python backend/auto_apply.py --save-session  "
            "OR set LINKEDIN_EMAIL + LINKEDIN_PASSWORD in .env"
        )

    browser = p.chromium.launch(
        headless=True,
        args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
    )

    # Try saved session first
    if has_session:
        ctx  = browser.new_context(storage_state=str(SESSION_FILE), user_agent=UA)
        page = ctx.new_page()
        try:
            page.goto("https://www.linkedin.com/feed/",
                      wait_until="domcontentloaded", timeout=15000)
            if "feed" in page.url or "mynetwork" in page.url:
                return browser, ctx, page, None
            # Session expired
            ctx.close()
            print("  LinkedIn session expired, trying password login...")
        except Exception:
            ctx.close()

    # Try email/password
    if le and lp:
        ctx  = browser.new_context(user_agent=UA)
        page = ctx.new_page()
        try:
            page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            page.fill("#username", le)
            page.fill("#password", lp)
            page.click("button[type=submit]")
            page.wait_for_url("**/feed**", timeout=20000)
            _jitter(1, 2)
            ctx.storage_state(path=str(SESSION_FILE))
            return browser, ctx, page, None
        except Exception as e:
            ctx.close()
            return browser, None, None, f"LinkedIn password login failed: {e}"

    return browser, None, None, (
        "LinkedIn session expired. Run: python backend/auto_apply.py --save-session"
    )


# ── LinkedIn Easy Apply ────────────────────────────────────────────────────────

def apply_linkedin(job: dict, profile: dict, username: str) -> dict:
    db.log(username, f"[LinkedIn] {job.get('title')} @ {job.get('company')}")
    sp, PWT = _pw()
    if not sp:
        return _nope("linkedin", "Playwright not installed. Run: pip install playwright && playwright install chromium", job)

    # Fast check: do we have any login method?
    has_session = SESSION_FILE.exists()
    has_creds   = bool(os.environ.get("LINKEDIN_EMAIL") and os.environ.get("LINKEDIN_PASSWORD"))
    if not has_session and not has_creds:
        db.log(username, (
            "  LinkedIn: no session — run: python backend/auto_apply.py --save-session"
        ))
        return {
            "success":   False,
            "manual":    True,
            "platform":  "linkedin",
            "reason":    "LinkedIn session required. Run: python backend/auto_apply.py --save-session",
            "apply_url": job.get("url",""),   # give back the LinkedIn URL for manual apply
        }

    cover = _cover_letter(profile, job)

    with sp() as p:
        browser, ctx, page, err = _li_browser(p)
        if err:
            try: browser.close()
            except Exception: pass
            return _nope("linkedin", err, job)

        try:
            page.goto(job["url"], wait_until="domcontentloaded", timeout=25000)
            _jitter(2, 3)

            # ── Find Easy Apply button (multiple selectors — LinkedIn changes DOM often) ──
            easy_apply_clicked = False
            for selector in [
                "button.jobs-apply-button",
                "button[aria-label*='Easy Apply']",
                "button[aria-label*='easy apply']",
                ".jobs-apply-button",
                "button:has-text('Easy Apply')",
                "[data-control-name='jobdetails_topcard_inapply']",
            ]:
                try:
                    btn = page.locator(selector)
                    btn.first.wait_for(state="visible", timeout=3000)
                    btn.first.click()
                    _jitter(1.5, 2.5)
                    easy_apply_clicked = True
                    break
                except Exception:
                    continue

            if not easy_apply_clicked:
                # Check if it's truly no Easy Apply or we need to scroll
                page.evaluate("window.scrollTo(0, 300)")
                _jitter(1, 1.5)
                for selector in [
                    "button.jobs-apply-button",
                    "button[aria-label*='Apply']",
                ]:
                    try:
                        btn = page.locator(selector)
                        if btn.count() > 0:
                            btn.first.click()
                            _jitter(1.5, 2.5)
                            easy_apply_clicked = True
                            break
                    except Exception:
                        continue

            if not easy_apply_clicked:
                # No Easy Apply — look for the external "Apply" button and extract its URL
                external_url = None
                for ext_sel in [
                    "a.jobs-apply-button",                      # anchor with apply class
                    "a[aria-label*='Apply']",                   # anchor aria-label Apply
                    "a[href*='apply']",                         # any link with /apply/
                    "button.jobs-apply-button + a",             # sibling anchor
                    ".jobs-unified-top-card__content--two-pane a[href*='http']",
                ]:
                    try:
                        el = page.locator(ext_sel)
                        if el.count() > 0:
                            href = el.first.get_attribute("href") or ""
                            if href and href.startswith("http") and "linkedin.com" not in href:
                                external_url = href
                                break
                    except Exception:
                        pass

                # Try intercepting click: catches both new-tab popup AND same-page navigation
                if not external_url:
                    apply_btn = page.locator(
                        "button[aria-label*='Apply']:visible,"
                        "button.jobs-apply-button:visible,"
                        ".jobs-apply-button:visible"
                    )
                    if apply_btn.count() > 0:
                        # Strategy A: catch new tab (most external apply buttons open a new tab)
                        try:
                            with ctx.expect_page(timeout=5000) as new_page_info:
                                apply_btn.first.click()
                            new_page = new_page_info.value
                            new_page.wait_for_load_state("domcontentloaded", timeout=8000)
                            new_url = new_page.url
                            if new_url and "linkedin.com" not in new_url:
                                external_url = new_url
                                db.log(username, f"  Caught new-tab: {new_url[:60]}")
                        except Exception:
                            pass

                        # Strategy B: same-page navigation
                        if not external_url:
                            try:
                                with page.expect_navigation(
                                    wait_until="domcontentloaded", timeout=5000
                                ) as nav_info:
                                    apply_btn.first.click()
                                nav_url = nav_info.value.url
                                if nav_url and "linkedin.com" not in nav_url:
                                    external_url = nav_url
                            except Exception:
                                pass

                browser.close()

                if external_url:
                    # We found the external ATS URL — detect platform and re-dispatch
                    ats_platform = detect_ats(external_url)
                    db.log(username, (
                        f"  LinkedIn external apply detected: {ats_platform} → {external_url[:60]}"
                    ))
                    # Update the job record with the real apply URL
                    db.update_job(username, job.get("id",""), apply_url=external_url,
                                  apply_platform=ats_platform, easy_apply=False)
                    job_updated = dict(job)
                    job_updated["apply_url"]      = external_url
                    job_updated["apply_platform"] = ats_platform
                    job_updated["easy_apply"]     = False
                    # Dispatch to the right ATS handler
                    fn = DISPATCHERS.get(ats_platform, apply_universal)
                    return fn(job_updated, profile, username)

                # Could not detect the external URL without session
                # The apply_url is stored as LinkedIn URL - give it back for manual apply
                db.log(username, "  LinkedIn: could not detect external ATS URL (may need session)")
                return {
                    "success":   False,
                    "manual":    True,
                    "platform":  "linkedin",
                    "reason":    "External ATS URL not detected. If using Easy Apply, run --save-session. Otherwise apply manually.",
                    "apply_url": job.get("url",""),
                }

            # ── Walk the multi-step wizard ──
            profile_with_resume = dict(profile)
            profile_with_resume["resume_path"] = (
                job.get("resume_path") or profile.get("base_resume_path", "")
            )

            for step in range(20):
                _jitter(0.8, 1.5)

                # Upload resume if input appears
                if profile_with_resume.get("resume_path"):
                    fi = page.locator("input[type=file]")
                    if fi.count() > 0:
                        try:
                            fi.first.set_input_files(profile_with_resume["resume_path"])
                            _jitter(1, 2)
                        except Exception:
                            pass

                # Fill all text/number/select/radio fields
                _fill_form(page, profile_with_resume, job, cover)

                # Navigation buttons — check in priority order
                # 1. Submit
                sub = page.locator(
                    "button[aria-label*='Submit application'],"
                    "button[aria-label*='Submit Application']"
                )
                if sub.count() > 0:
                    sub.first.click()
                    _jitter(2, 4)
                    browser.close()
                    db.log(username, f"  ✓ LinkedIn submitted: {job.get('title')}")
                    return {"success": True, "platform": "linkedin", "manual": False}

                # 2. Review
                rev = page.locator("button[aria-label*='Review']")
                if rev.count() > 0:
                    rev.first.click()
                    continue

                # 3. Next / Continue
                nxt = page.locator(
                    "button[aria-label*='Next'],"
                    "button[aria-label*='Continue'],"
                    "button[aria-label*='next step'],"
                    "button[aria-label*='continue to']"
                )
                if nxt.count() > 0:
                    nxt.first.click()
                    continue

                # 4. Done / Close (after submission confirmation)
                done = page.locator(
                    "button[aria-label*='Done'],"
                    "button[aria-label*='Dismiss']"
                )
                if done.count() > 0:
                    browser.close()
                    db.log(username, f"  ✓ LinkedIn submitted (done button): {job.get('title')}")
                    return {"success": True, "platform": "linkedin", "manual": False}

                # No navigation found — stuck
                break

            browser.close()
            return _nope("linkedin", "Form incomplete after 20 steps", job)

        except Exception as e:
            try: browser.close()
            except Exception: pass
            db.log(username, f"  ✗ LinkedIn error: {e}")
            return _nope("linkedin", str(e)[:200], job)


# ── Greenhouse ─────────────────────────────────────────────────────────────────
# Greenhouse has a public REST API — use it when possible (no browser needed)

def apply_greenhouse(job: dict, profile: dict, username: str) -> dict:
    db.log(username, f"[Greenhouse] {job.get('title')} @ {job.get('company')}")

    apply_url = job.get("apply_url") or job.get("url", "")
    if not apply_url:
        return _nope("greenhouse", "No apply URL", job)

    # Try to extract the job token for the REST API
    # Greenhouse apply URLs look like:
    #   https://boards.greenhouse.io/COMPANY/jobs/JOB_ID
    #   https://boards.greenhouse.io/COMPANY/jobs/JOB_ID#app
    token_match = re.search(
        r"greenhouse\.io/([^/]+)/jobs/(\d+)",
        apply_url,
    )

    if token_match:
        board = token_match.group(1)
        job_id = token_match.group(2)
        result = _greenhouse_api(job, profile, username, board, job_id)
        if result.get("success"):
            return result
        db.log(username, f"  API submit failed ({result.get('reason','')}), trying Playwright…")

    # Playwright fallback
    return _greenhouse_playwright(job, profile, username, apply_url)


def _greenhouse_api(job, profile, username, board, job_id):
    """Submit to Greenhouse REST API. No browser needed."""
    import urllib.request, urllib.error
    CRLF = b"\r\n"
    bd   = "GHBound" + str(int(time.time()))

    resume_path = job.get("resume_path") or profile.get("base_resume_path","")
    if not resume_path or not Path(resume_path).exists():
        reason = "Resume file not found: " + str(resume_path)
        db.log(username, "  GH API: " + reason)
        return {"success": False, "reason": reason}

    db.log(username, "  GH API board=" + board + " job=" + job_id)

    name_parts = (profile.get("name","") or "Candidate").split()
    first = name_parts[0] if name_parts else "Candidate"
    last  = " ".join(name_parts[1:]) if len(name_parts) > 1 else "."

    def li(v, pfx=""):
        v = str(v or "").strip()
        if not v: return ""
        if v.startswith("http"): return v
        base = pfx.split("//")[-1] if "//" in pfx else pfx
        return (pfx + v) if pfx and not v.startswith(base) else v

    fields = [
        ("first_name",             first),
        ("last_name",              last),
        ("email",                  profile.get("email","")),
        ("phone",                  profile.get("phone","")),
        ("location",               profile.get("location","")),
        ("website",                li(profile.get("website","") or profile.get("github",""), "https://")),
        ("linkedin_profile_url",   li(profile.get("linkedin",""), "https://linkedin.com/in/")),
        ("cover_letter_text",      _cover_letter(profile, job)),
    ]

    body = b""
    for name, val in fields:
        if not val: continue
        body += b"--" + bd.encode() + CRLF
        body += b"Content-Disposition: form-data; name=\"" + name.encode() + b"\"" + CRLF + CRLF
        body += val.encode("utf-8", errors="replace") + CRLF

    try:
        resume_data = open(resume_path, "rb").read()
        fname       = Path(resume_path).name
        body += b"--" + bd.encode() + CRLF
        body += b"Content-Disposition: form-data; name=\"resume\"; filename=\"" + fname.encode() + b"\"" + CRLF
        body += b"Content-Type: application/pdf" + CRLF + CRLF
        body += resume_data + CRLF
    except Exception as exc:
        return {"success": False, "reason": "Cannot read resume: " + str(exc)}

    body += b"--" + bd.encode() + b"--" + CRLF

    url = ("https://boards-api.greenhouse.io/v1/boards/"
           + board + "/jobs/" + job_id + "/applications")
    req = urllib.request.Request(
        url, data=body,
        headers={
            "Content-Type": "multipart/form-data; boundary=" + bd,
            "User-Agent":   "Mozilla/5.0 (compatible; JobHunt/1.0)",
            "Accept":       "application/json",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            status = resp.status
            rbody  = resp.read().decode("utf-8", errors="replace")
            db.log(username, "  GH API response: " + str(status) + " " + rbody[:60])
            if status in (200, 201):
                return {"success": True, "platform": "greenhouse", "manual": False}
            return {"success": False, "reason": "API " + str(status) + ": " + rbody[:100]}
    except urllib.error.HTTPError as e:
        rbody = e.read().decode("utf-8", errors="replace")
        db.log(username, "  GH API error: " + str(e.code) + " " + rbody[:100])
        return {"success": False, "reason": "HTTP " + str(e.code) + ": " + rbody[:100]}
    except Exception as exc:
        db.log(username, "  GH API exception: " + str(exc)[:100])
        return {"success": False, "reason": str(exc)[:200]}


def _greenhouse_playwright(job: dict, profile: dict, username: str,
                            apply_url: str) -> dict:
    """Greenhouse via browser — fallback when API fails."""
    sp, _ = _pw()
    if not sp:
        return _nope("greenhouse", "Playwright not installed", job)

    cover      = _cover_letter(profile, job)
    name_parts = (profile.get("name", "") or "Candidate").split()

    with sp() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page    = browser.new_page()
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=25000)
            _jitter(1, 2)

            # Standard Greenhouse field IDs
            std = {
                "#first_name":                   name_parts[0] if name_parts else "",
                "#last_name":                    " ".join(name_parts[1:]) if len(name_parts) > 1 else "",
                "#email":                        profile.get("email", ""),
                "#phone":                        profile.get("phone", ""),
                "#job_application_location":     profile.get("location", ""),
                "#job_application_linkedin_url": _fmt_url(profile.get("linkedin", ""), "https://linkedin.com/in/"),
                "#job_application_website":      profile.get("website", ""),
                "input[id*='github']":           _fmt_url(profile.get("github", ""), "https://github.com/"),
            }
            for sel, val in std.items():
                if not val: continue
                try:
                    el = page.locator(sel)
                    if el.count() > 0:
                        _safe_fill(el.first, val)
                except Exception:
                    pass

            # Resume upload
            resume_path = job.get("resume_path") or profile.get("base_resume_path", "")
            if resume_path and Path(resume_path).exists():
                for fi_sel in ("#resume", "input[id*=resume]", "input[type=file]"):
                    fi = page.locator(fi_sel)
                    if fi.count() > 0:
                        try:
                            fi.first.set_input_files(resume_path)
                            _jitter(1.5, 2.5)
                            break
                        except Exception:
                            pass

            # Cover letter textarea
            for cl_sel in ("textarea[id*=cover]", "#cover_letter", "textarea[name*=cover]"):
                cl = page.locator(cl_sel)
                if cl.count() > 0:
                    try:
                        _safe_fill(cl.first, cover)
                        break
                    except Exception:
                        pass

            # Fill everything else
            _fill_form(page, profile, job, cover)

            # Submit
            sub = page.locator(
                "button[type=submit]:has-text('Submit'),"
                "button:has-text('Submit Application'),"
                "input[type=submit][value*='Submit']"
            )
            if sub.count() > 0:
                sub.first.click()
                _jitter(3, 5)
                # Check success
                success = (
                    any(page.locator(f"text={m}").count() > 0
                        for m in ["Thank you", "Application received", "application complete"])
                    or any(k in page.url.lower() for k in ("thank", "confirm", "success"))
                )
                browser.close()
                if success:
                    db.log(username, f"  ✓ Greenhouse submitted: {job.get('title')}")
                    return {"success": True, "platform": "greenhouse", "manual": False}
                # Even if we can't confirm, we tried
                db.log(username, f"  ~ Greenhouse submitted (unconfirmed): {job.get('title')}")
                return {"success": True, "platform": "greenhouse", "manual": False,
                        "note": "Submitted — success unconfirmed"}

            browser.close()
            return _nope("greenhouse", "Submit button not found", job)

        except Exception as e:
            try: browser.close()
            except Exception: pass
            return _nope("greenhouse", str(e)[:200], job)


# ── Lever ──────────────────────────────────────────────────────────────────────

def apply_lever(job: dict, profile: dict, username: str) -> dict:
    db.log(username, f"[Lever] {job.get('title')} @ {job.get('company')}")
    sp, _ = _pw()
    if not sp:
        return _nope("lever", "Playwright not installed", job)

    apply_url = job.get("apply_url") or job.get("url", "")
    if not apply_url:
        return _nope("lever", "No apply URL", job)
    if "/apply" not in apply_url:
        apply_url = apply_url.rstrip("/") + "/apply"

    cover      = _cover_letter(profile, job)
    resume_path = job.get("resume_path") or profile.get("base_resume_path", "")

    with sp() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page    = browser.new_page()
        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=25000)
            _jitter(1, 2)

            # Standard Lever fields
            std = [
                (["input[name='name']",   "input[id='name']"],   profile.get("name", "")),
                (["input[name='email']",  "input[type=email]"],  profile.get("email", "")),
                (["input[name='phone']",  "input[type=tel]"],    profile.get("phone", "")),
                (["input[name='org']",    "input[name='current_company']"],
                 profile.get("current_company", "")),
                (["input[name='urls[LinkedIn]']"],
                 _fmt_url(profile.get("linkedin", ""), "https://linkedin.com/in/")),
                (["input[name='urls[GitHub]']"],
                 _fmt_url(profile.get("github", ""), "https://github.com/")),
                (["input[name='urls[Portfolio]']", "input[name='urls[Website]']"],
                 profile.get("website", "")),
            ]
            for selectors, val in std:
                if not val: continue
                for sel in selectors:
                    el = page.locator(sel)
                    if el.count() > 0:
                        _safe_fill(el.first, val)
                        break

            # Resume
            if resume_path and Path(resume_path).exists():
                for fi_sel in ("input[type=file]", "input[name='resume']"):
                    fi = page.locator(fi_sel)
                    if fi.count() > 0:
                        try:
                            fi.first.set_input_files(resume_path)
                            _jitter(1.5, 2.5)
                            break
                        except Exception:
                            pass

            # Cover letter / comments
            for ta_sel in ("textarea[name='comments']", "textarea[name='cover_letter']",
                           "textarea[name='message']"):
                ta = page.locator(ta_sel)
                if ta.count() > 0:
                    _safe_fill(ta.first, cover)
                    break

            # Fill remaining + submit
            submitted = _fill_form(page, profile, job, cover)

            if not submitted:
                # Try Lever-specific submit selector
                sub = page.locator(
                    "button[type=submit]:has-text('Submit'),"
                    "button:has-text('Submit Application'),"
                    "button[data-qa='submit-application']"
                )
                if sub.count() > 0:
                    sub.first.click()
                    _jitter(3, 5)
                    submitted = True

            browser.close()
            if submitted:
                db.log(username, f"  ✓ Lever submitted: {job.get('title')}")
                return {"success": True, "platform": "lever", "manual": False}
            return _nope("lever", "Submit button not found", job)

        except Exception as e:
            try: browser.close()
            except Exception: pass
            return _nope("lever", str(e)[:200], job)


# ── Generic ATS platforms (Ashby, Workable, SmartRecruiters, iCIMS, BambooHR) ─

def _generic_apply(platform: str, job: dict, profile: dict, username: str) -> dict:
    """
    Generic apply handler for any ATS.

    Strategy:
    1. Navigate to the apply URL
    2. If it's a job listing page (not a form), find and click the Apply button
       — handles new-tab popups by switching to the new page
    3. Walk through multi-page forms (Next → Next → Submit)
    4. Return success or manual fallback
    """
    db.log(username, f"[{platform.title()}] {job.get('title')} @ {job.get('company')}")
    sp, _ = _pw()
    if not sp:
        return _nope(platform, "Playwright not installed", job)

    apply_url = job.get("apply_url") or job.get("url", "")
    if not apply_url:
        return _nope(platform, "No apply URL", job)

    cover = _cover_letter(profile, job)

    APPLY_BTN_SELECTORS = [
        # Common ATS-specific
        "a[data-ui='apply-button']",
        "button[data-ui='apply-button']",
        "[data-testid='apply-button']",
        "[data-qa='apply-button']",
        # Greenhouse
        "#apply_button",
        "a.btn-primary:has-text('Apply')",
        # Generic text matches
        "button:has-text('Apply for this job')",
        "button:has-text('Apply for Job')",
        "a:has-text('Apply for this job')",
        "button:has-text('Apply Now')",
        "a:has-text('Apply Now')",
        "button:has-text('Apply to this job')",
        "button:has-text('Quick Apply')",
        "a.apply-btn",
        ".apply-btn",
        "[class*='apply-button']",
        "[class*='applyButton']",
        "button:has-text('Apply')",
        "a:has-text('Apply')",
    ]

    with sp() as p:
        browser = p.chromium.launch(headless=True,
                                    args=["--no-sandbox",
                                          "--disable-blink-features=AutomationControlled"])
        ctx  = browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"))
        page = ctx.new_page()

        try:
            page.goto(apply_url, wait_until="domcontentloaded", timeout=30000)
            # Wait for JS-heavy pages (Workday, custom ATS) to finish rendering
            try:
                page.wait_for_load_state("networkidle", timeout=8000)
            except Exception:
                pass
            _jitter(1.5, 2.5)
            db.log(username, f"  Loaded: {page.url[:80]}")

            # ── Try to find the Apply button and navigate to the form ──────
            form_page = page  # default: form might be on this same page
            clicked_apply = False

            # Log all visible buttons to help debug
            try:
                all_btns = page.locator("button:visible,a:visible").all()
                btn_texts = []
                for b in all_btns[:20]:
                    t = b.inner_text()[:30].strip()
                    if t: btn_texts.append(t)
                db.log(username, f"  Visible buttons: {btn_texts[:8]}")
            except Exception:
                pass

            for sel in APPLY_BTN_SELECTORS:
                try:
                    btn = page.locator(f"{sel}:visible")
                    if btn.count() == 0:
                        continue
                    # Check if clicking opens a new tab
                    with ctx.expect_page(timeout=4000) as new_page_info:
                        btn.first.click()
                    new_page = new_page_info.value
                    new_page.wait_for_load_state("domcontentloaded", timeout=10000)
                    try:
                        new_page.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                    form_page = new_page
                    clicked_apply = True
                    db.log(username, f"  Apply button opened new tab: {new_page.url[:60]}")
                    break
                except Exception:
                    # No new tab — button navigated on same page or failed
                    try:
                        if page.url != apply_url:
                            # Page navigated
                            form_page = page
                            clicked_apply = True
                            break
                        # Check if a modal/overlay appeared
                        modal = page.locator("[role='dialog']:visible, .modal:visible, [class*='modal']:visible")
                        if modal.count() > 0:
                            form_page = page
                            clicked_apply = True
                            break
                    except Exception:
                        pass

            _jitter(1, 2)

            # ── Walk form pages (handle multi-step) ──────────────────────
            NEXT_SELECTORS = [
                "button:has-text('Next'):visible",
                "button:has-text('Continue'):visible",
                "button:has-text('Next Step'):visible",
                "button[aria-label*='Next']:visible",
                "button[data-qa*='next']:visible",
                "[class*='next-button']:visible",
            ]

            # Verify we actually landed on a form page, not still on a listing
            form_inputs = 0
            try:
                form_inputs = form_page.locator(
                    "input:visible, select:visible, textarea:visible"
                ).count()
                db.log(username, f"  Form inputs found: {form_inputs} on {form_page.url[:60]}")
            except Exception:
                pass

            if form_inputs == 0:
                # Still on listing page — try scrolling and waiting for form to appear
                try:
                    form_page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    _jitter(1, 2)
                    form_inputs = form_page.locator("input:visible, select:visible").count()
                except Exception:
                    pass

            for step in range(8):  # up to 8 form pages
                _jitter(0.5, 1.0)

                submitted = _fill_form(form_page, profile, job, cover)
                if submitted:
                    db.log(username, f"  ✓ {platform.title()} submitted: {job.get('title')}")
                    browser.close()
                    return {"success": True, "platform": platform, "manual": False}

                # Not submitted yet — try Next button to advance multi-step form
                advanced = False
                for nxt_sel in NEXT_SELECTORS:
                    try:
                        nxt = form_page.locator(nxt_sel)
                        if nxt.count() > 0:
                            nxt.first.click()
                            _jitter(1, 2)
                            advanced = True
                            break
                    except Exception:
                        pass

                if not advanced:
                    break  # no next button and no submit — stuck

            browser.close()

            # Could not submit — return manual with the actual form URL
            form_url = form_page.url if form_page else apply_url
            db.log(username, f"  ✗ {platform.title()}: could not find/click submit on {form_url[:60]}")
            return _nope(platform, "Filled form — submit button not found", {**job, "apply_url": form_url})

        except Exception as e:
            try: browser.close()
            except Exception: pass
            db.log(username, f"  ✗ {platform.title()} error: {str(e)[:100]}")
            return _nope(platform, str(e)[:200], job)


def apply_ashby(job, profile, username):
    return _generic_apply("ashby", job, profile, username)

def apply_workable(job, profile, username):
    return _generic_apply("workable", job, profile, username)

def apply_smartrecruiters(job, profile, username):
    return _generic_apply("smartrecruiters", job, profile, username)

def apply_icims(job, profile, username):
    return _generic_apply("icims", job, profile, username)

def apply_bamboohr(job, profile, username):
    return _generic_apply("bamboohr", job, profile, username)

def apply_universal(job, profile, username):
    return _generic_apply("universal", job, profile, username)


# ── Indeed ─────────────────────────────────────────────────────────────────────

def apply_indeed(job: dict, profile: dict, username: str) -> dict:
    db.log(username, f"[Indeed] {job.get('title')} @ {job.get('company')}")
    sp, PWT = _pw()
    if not sp:
        return _nope("indeed", "Playwright not installed", job)
    ie = os.environ.get("INDEED_EMAIL", "")
    ip = os.environ.get("INDEED_PASSWORD", "")
    if not ie or not ip:
        return _nope("indeed", "Set INDEED_EMAIL and INDEED_PASSWORD in .env", job)

    cover = _cover_letter(profile, job)

    with sp() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page    = browser.new_page()
        try:
            page.goto("https://secure.indeed.com/account/login",
                      wait_until="domcontentloaded", timeout=20000)
            _jitter()
            try:
                page.fill("input[type=email]", ie)
                page.keyboard.press("Enter")
                _jitter(1, 2)
                page.fill("input[type=password]", ip)
                page.keyboard.press("Enter")
                page.wait_for_load_state("domcontentloaded", timeout=20000)
                _jitter(1, 2)
            except Exception as e:
                browser.close()
                return _nope("indeed", f"Login failed: {e}", job)

            page.goto(job["url"], wait_until="domcontentloaded", timeout=20000)
            _jitter(1, 2)

            for btn_sel in [
                "button.ia-IndeedApplyButton",
                "button[data-testid='apply-button']",
                "button[id*='applyButton']",
            ]:
                try:
                    btn = page.locator(btn_sel)
                    btn.first.wait_for(state="visible", timeout=5000)
                    btn.first.click()
                    _jitter(1.5, 2.5)
                    break
                except Exception:
                    continue

            submitted = _fill_form(page, profile, job, cover)
            browser.close()

            if submitted:
                db.log(username, f"  ✓ Indeed submitted: {job.get('title')}")
                return {"success": True, "platform": "indeed", "manual": False}
            return _nope("indeed", "Could not complete Indeed form", job)

        except Exception as e:
            try: browser.close()
            except Exception: pass
            return _nope("indeed", str(e)[:200], job)


# ── ATS URL detection ──────────────────────────────────────────────────────────

def detect_ats(url: str) -> str:
    u = (url or "").lower()
    if "linkedin.com"          in u: return "linkedin"
    if "indeed.com"            in u: return "indeed"
    if "greenhouse.io"         in u: return "greenhouse"
    if "boards-api.greenhouse" in u: return "greenhouse"
    if "jobs.lever.co"         in u: return "lever"
    if "lever.co"              in u: return "lever"
    if "smartrecruiters.com"   in u: return "smartrecruiters"
    if "ashbyhq.com"           in u: return "ashby"
    if "workable.com"          in u: return "workable"
    if "myworkdayjobs.com"     in u: return "workday"
    if "workday.com"           in u: return "workday"   # broader workday match
    if "icims.com"             in u: return "icims"
    if "bamboohr.com"          in u: return "bamboohr"
    # Community/news sites are NOT ATS forms - mark manual
    if "ycombinator.com"       in u: return "manual"
    if "news.ycombinator"      in u: return "manual"
    if "workatastartup.com"    in u: return "manual"
    if "taleo.net"             in u: return "manual"
    if "jobvite.com"           in u: return "universal"
    return "universal"


def _fmt_url(val: str, prefix: str = "") -> str:
    v = str(val or "").strip()
    if not v: return ""
    if v.startswith("http"): return v
    base = prefix.split("//")[-1] if "//" in prefix else prefix
    return (prefix + v) if prefix and not v.startswith(base) else v


# ── Dispatcher ────────────────────────────────────────────────────────────────

DISPATCHERS = {
    "linkedin":         apply_linkedin,
    "indeed":           apply_indeed,
    "greenhouse":       apply_greenhouse,
    "lever":            apply_lever,
    "ashby":            apply_ashby,
    "workable":         apply_workable,
    "smartrecruiters":  apply_smartrecruiters,
    "icims":            apply_icims,
    "bamboohr":         apply_bamboohr,
    "workday":          apply_universal,   # Workday uses same generic flow
    "universal":        apply_universal,
}


def apply_job(jid: str, profile: dict, username: str) -> dict:
    job = db.get_job(username, jid)
    if not job:
        return {"success": False, "reason": "Job not found"}

    apply_url = job.get("apply_url") or job.get("url", "")
    plat      = job.get("apply_platform") or detect_ats(apply_url) or "universal"

    # Never auto-apply to manual-only sources
    if plat == "manual":
        return _nope("manual", "This job requires manual application (HN/YC/unknown ATS)", job)

    job_copy = dict(job)
    job_copy["resume_path"] = (
        job.get("resume_path") or profile.get("base_resume_path", "")
    )

    db.log(username, f"Applying: {job.get('title')} @ {job.get('company')} via [{plat}]")
    db.log(username, f"  URL: {apply_url[:80]}")
    db.log(username, f"  Resume: {job_copy.get('resume_path','(none)')}")
    db.update_job(username, jid, status="applying")

    fn     = DISPATCHERS.get(plat, apply_universal)
    if fn is apply_universal and plat not in ("universal","manual"):
        db.log(username, f"  ⚠ No handler for [{plat}] — using universal filler")
    result = fn(job_copy, profile, username)
    db.log(username, f"  Result: success={result.get('success')} manual={result.get('manual')} reason={result.get('reason','')[:60]}")

    if result.get("success"):
        db.update_job(username, jid,
                      status       = "submitted",
                      submitted_at = datetime.now().isoformat())
    elif result.get("manual") or result.get("pre_filled"):
        db.update_job(username, jid,
                      status           = "manual",
                      manual_reason    = result.get("reason", ""),
                      manual_apply_url = result.get("apply_url", ""))
    else:
        db.update_job(username, jid,
                      status      = "failed",
                      apply_error = result.get("reason", ""))

    return result


def apply_batch(profile: dict, username: str) -> list:
    jobs     = db.load_jobs(username)
    eligible = [
        j for j in jobs
        if j.get("status") == "ready"
        and (j.get("resume_path") or profile.get("base_resume_path"))
    ]
    db.log(username, f"Batch apply: {len(eligible)} jobs")
    results = []
    for job in eligible:
        result = apply_job(job["id"], profile, username)
        results.append({"id": job["id"], "title": job["title"], "result": result})
        time.sleep(3)
    return results


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")

    parser = argparse.ArgumentParser()
    parser.add_argument("--save-session", action="store_true")
    parser.add_argument("--job-id")
    parser.add_argument("--batch",  action="store_true")
    parser.add_argument("--user", default="", dest="username")
    args = parser.parse_args()

    if args.save_session:
        save_session()
    elif args.job_id and args.username:
        pf = db.get_profile(args.username)
        if pf: print(json.dumps(apply_job(args.job_id, pf, args.username), indent=2))
        else:  print(f"Profile '{args.username}' not found")
    elif args.batch and args.username:
        pf = db.get_profile(args.username)
        if pf: print(json.dumps(apply_batch(pf, args.username), indent=2))
        else:  print(f"Profile '{args.username}' not found")
    else:
        parser.print_help()
