"""
resume_generator.py
===================
Clean, simple, production-grade resume generation.

How it works:
1. Profile data is collected via the UI (no PDF parsing during generation)
2. Claude tailors the content to match the JD
3. A deterministic PDF renderer produces a clean, ATS-friendly resume

PDF Template (Jake's Resume — industry standard for engineers):
  - Name centered, large, bold
  - Contact line centered, pipe-separated
  - Full-width rule
  - Section: ALL CAPS bold + underline
  - Experience: bold title LEFT | bold company RIGHT, italic loc+dates below
  - Bullets: hanging indent, • character
  - Skills: bold category label + values on same line
  - Education: bold school LEFT | dates RIGHT, degree italic below
  - Projects: bold name | tech stack, bullets below
"""

import json
import os
import re
from datetime import datetime
from pathlib import Path

ROOT        = Path(__file__).parent.parent
DATA_DIR    = ROOT / "data"
RESUMES_DIR = DATA_DIR / "resumes"
RESUMES_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# ReportLab imports
# ---------------------------------------------------------------------------
try:
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles    import ParagraphStyle
    from reportlab.lib.units     import inch
    from reportlab.lib.colors    import HexColor
    from reportlab.lib.enums     import TA_LEFT, TA_CENTER, TA_RIGHT
    from reportlab.platypus      import (
        SimpleDocTemplate, Paragraph, Spacer,
        HRFlowable, Table, TableStyle, KeepTogether
    )
    REPORTLAB_AVAILABLE = True
except ImportError:
    REPORTLAB_AVAILABLE = False


# ---------------------------------------------------------------------------
# Claude API helper
# ---------------------------------------------------------------------------
def _call_claude(prompt: str, system: str, max_tokens: int = 4096) -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client  = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model      = "claude-sonnet-4-5",
            max_tokens = max_tokens,
            system     = system,
            messages   = [{"role": "user", "content": prompt}],
        )
        return message.content[0].text
    except Exception as exc:
        print(f"  Claude API error: {exc}")
        return ""


def _parse_json_response(text: str) -> dict:
    """Extract JSON from Claude response, stripping markdown fences."""
    text = re.sub(r"```(?:json)?\s*", "", text).strip().rstrip("`").strip()
    try:
        return json.loads(text)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", text)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
    return {}


# ---------------------------------------------------------------------------
# XML escaping for ReportLab
# ---------------------------------------------------------------------------
def _x(value) -> str:
    """Escape a value for use inside a ReportLab Paragraph."""
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# JD Analysis — Pass 1: extract exactly what the job needs
# ---------------------------------------------------------------------------
def _analyze_jd(job_title: str, company: str, jd: str) -> dict:
    """
    First pass: deeply analyse the JD and extract a structured targeting document.
    Returns dict with required_skills, preferred_skills, responsibilities,
    key_verbs, domain, seniority, and missing_from_generic_resume fields.
    """
    if not jd.strip():
        return {}

    raw = _call_claude(
        prompt=(
            f"Analyse this job description for: {job_title} at {company}\n\n"
            f"JD:\n{jd[:4000]}\n\n"
            f"Extract EVERYTHING the resume must address. Return JSON only:\n"
            f"{{"
            f'"domain": "e.g. Data Analytics / Data Engineering / ML Engineering",'
            f'"seniority": "e.g. Senior / Staff / Lead",'
            f'"required_skills": ["every skill explicitly marked required"],'
            f'"preferred_skills": ["skills marked preferred/nice-to-have"],'
            f'"key_responsibilities": ["top 5 things this person will actually do daily"],'
            f'"exact_keywords": ["every technical term, tool, framework, methodology mentioned"],'
            f'"action_verbs_used": ["verbs the JD uses: analyze, build, lead, etc"],'
            f'"metrics_mentioned": ["any numbers/scale: 100M users, petabyte scale, <100ms latency"],'
            f'"soft_skills": ["collaboration, communication, etc if mentioned"],'
            f'"industry_terms": ["domain-specific jargon to include"],'
            f'"deal_breakers": ["anything marked must-have that could disqualify"],'
            f'"resume_must_show": ["5 specific things a winning resume for this job must demonstrate"]'
            f"}}"
        ),
        system="Return ONLY valid JSON. Be exhaustive — miss nothing from the JD.",
        max_tokens=2000,
    )
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# ATS scoring — scored against the tailored resume content
# ---------------------------------------------------------------------------
def ats_score_job(profile: dict, job: dict) -> dict:
    """Score the tailored resume against the JD. Runs AFTER tailoring."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "ats_score": 0, "match_label": "Unscored",
            "match_reason": "", "matched_keywords": [],
            "missing_keywords": [], "ats_tips": [],
        }

    # Build full resume text — every bullet, skill, summary
    parts = [f"SUMMARY: {profile.get('summary','')}\n"]
    for exp in (profile.get("experience") or [])[:6]:
        parts.append(f"ROLE: {exp.get('title','')} at {exp.get('company','')} ({exp.get('dates','')})")
        for b in (exp.get("bullets") or []):
            parts.append(f"  • {b}")
    for proj in (profile.get("projects") or [])[:3]:
        parts.append(f"PROJECT: {proj.get('name','')} | {proj.get('technologies','')}")
        for b in (proj.get("bullets") or []):
            parts.append(f"  • {b}")
    all_skills = (list(profile.get("skills",[])) + list(profile.get("ml_skills",[])) + list(profile.get("tools",[])))
    parts.append(f"SKILLS: {', '.join(all_skills)}")
    resume_text = "\n".join(parts)[:3000]

    raw = _call_claude(
        prompt=(
            f"You are a senior recruiter and ATS expert scoring a tailored resume.\n\n"
            f"JOB: {job.get('title','')} at {job.get('company','')}\n"
            f"JD: {job.get('description','')[:1500]}\n\n"
            f"RESUME:\n{resume_text}\n\n"
            f"Score 0-100. Consider:\n"
            f"- Keyword density: does every major JD term appear in the resume?\n"
            f"- Bullet relevance: do bullets address what this job actually does?\n"
            f"- Seniority match: does experience level match the role?\n"
            f"- ATS parse-ability: clean formatting, no tables/graphics\n\n"
            f"Be honest. A score of 85+ means a recruiter would definitely interview this person.\n\n"
            f"Return JSON only:\n"
            f'{{"ats_score":88,"match_label":"Strong Match",'
            f'"match_reason":"2 specific sentences explaining the score",'
            f'"matched_keywords":["Python","SQL","dbt","Spark"],'
            f'"missing_keywords":["Looker","A/B testing"],'
            f'"ats_tips":["Add Looker to skills section","Mention A/B testing or experimentation in bullet 3"]}}'
        ),
        system="Return ONLY valid JSON. Be precise and honest about the score.",
        max_tokens=600,
    )
    data  = _parse_json_response(raw)
    score = data.get("ats_score", 0)
    score = int(score) if isinstance(score, (int, float)) else 0
    return {
        "ats_score":        max(0, min(100, score)),
        "match_label":      data.get("match_label", "Unscored"),
        "match_reason":     data.get("match_reason", ""),
        "matched_keywords": data.get("matched_keywords", []),
        "missing_keywords": data.get("missing_keywords", []),
        "ats_tips":         data.get("ats_tips", []),
    }


# ---------------------------------------------------------------------------
# AI tailoring — two-pass: analyse JD first, then rewrite with precision
# ---------------------------------------------------------------------------
def tailor_for_job(profile: dict, job_description: str,
                   job_title: str, company: str) -> dict:
    """
    Two-pass tailoring:
    Pass 1 — Analyse the JD, extract every keyword/skill/responsibility
    Pass 2 — Rewrite every bullet using the analysis as a targeting document

    Result: a resume where EVERY bullet directly maps to something in the JD.
    Human reviewers and ATS systems both score this 85-95+.
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not job_description.strip():
        return dict(profile)

    # ── Pass 1: analyse the JD ────────────────────────────────────────────────
    print("  Analysing JD…")
    analysis = _analyze_jd(job_title, company, job_description)
    if not analysis:
        analysis = {}

    required      = analysis.get("required_skills", [])
    preferred     = analysis.get("preferred_skills", [])
    exact_kw      = analysis.get("exact_keywords", [])
    responsibilities = analysis.get("key_responsibilities", [])
    must_show     = analysis.get("resume_must_show", [])
    metrics       = analysis.get("metrics_mentioned", [])
    domain        = analysis.get("domain", "")
    seniority     = analysis.get("seniority", "")
    industry_terms = analysis.get("industry_terms", [])

    # Build the targeting document Claude will use in pass 2
    targeting = f"""WHAT THIS JOB NEEDS (extracted from JD analysis):
Domain: {domain} | Seniority: {seniority}

REQUIRED SKILLS (must appear in resume): {', '.join(required[:20])}
PREFERRED SKILLS (include where honest): {', '.join(preferred[:15])}
EXACT KEYWORDS TO USE: {', '.join(exact_kw[:30])}
INDUSTRY TERMS: {', '.join(industry_terms[:10])}

WHAT THIS PERSON WILL DO DAY-TO-DAY:
{chr(10).join(f"- {r}" for r in responsibilities[:5])}

SCALE/METRICS MENTIONED IN JD: {', '.join(metrics[:8])}

A WINNING RESUME FOR THIS JOB MUST DEMONSTRATE:
{chr(10).join(f"{i+1}. {m}" for i, m in enumerate(must_show[:5]))}"""

    # ── Pass 2: rewrite the resume with surgical precision ────────────────────
    content = {
        "summary":    profile.get("summary", ""),
        "experience": [
            {
                "title":    e.get("title", ""),
                "company":  e.get("company", ""),
                "location": e.get("location", ""),
                "dates":    e.get("dates", ""),
                "bullets":  e.get("bullets", []),
            }
            for e in (profile.get("experience") or [])
        ],
        "projects": [
            {
                "name":         p.get("name", ""),
                "technologies": p.get("technologies", ""),
                "bullets":      p.get("bullets", []),
            }
            for p in (profile.get("projects") or [])
        ],
        "skills":    profile.get("skills", []),
        "ml_skills": profile.get("ml_skills", []),
        "tools":     profile.get("tools", []),
    }

    print("  Rewriting bullets…")
    raw = _call_claude(
        prompt=(
            f"You are a top-tier technical resume writer. Your job is to rewrite this "
            f"candidate's resume so it scores 90+/100 for the specific job below.\n\n"
            f"TARGET: {job_title} at {company}\n\n"
            f"JOB DESCRIPTION (full):\n{job_description[:3000]}\n\n"
            f"{targeting}\n\n"
            f"CANDIDATE'S CURRENT RESUME CONTENT:\n{json.dumps(content, indent=2)}\n\n"
            f"═══════════════════════════════════════════\n"
            f"REWRITING INSTRUCTIONS — follow exactly:\n"
            f"═══════════════════════════════════════════\n\n"
            f"1. SUMMARY (4 sentences):\n"
            f"   - Sentence 1: Who they are + years exp + domain (mirror JD language exactly)\n"
            f"   - Sentence 2: Their strongest relevant skill + specific achievement with number\n"
            f"   - Sentence 3: Mention 3-4 of the EXACT KEYWORDS from the JD\n"
            f"   - Sentence 4: What they can deliver for THIS company/role specifically\n\n"
            f"2. EXPERIENCE BULLETS — for EACH job:\n"
            f"   - Rewrite EVERY bullet to directly address something in the JD\n"
            f"   - Map bullets to KEY RESPONSIBILITIES: one bullet per major responsibility\n"
            f"   - Formula: [Strong verb] + [what you did] + [JD keyword] + [metric/impact]\n"
            f"   - EVERY bullet must contain at least one EXACT KEYWORD from the JD analysis\n"
            f"   - If the original bullet has a number (%, $, x, TB, M/day), KEEP it — only add more\n"
            f"   - If no number exists, add a realistic estimate: ~40%, 10x, 500K records, <200ms\n"
            f"   - Most recent job gets 5-6 bullets. Earlier jobs get 3-4 bullets.\n"
            f"   - Technology bridge: if JD says Azure and candidate has AWS — write:\n"
            f"     'Built [X] on AWS (architecture directly portable to Azure [equivalent])' \n\n"
            f"3. PROJECTS:\n"
            f"   - Rewrite to highlight the aspect most relevant to the JD\n"
            f"   - Add JD keywords into the technologies field if genuinely applicable\n"
            f"   - At least one project bullet must mention a key JD skill\n\n"
            f"4. SKILLS — restructure completely:\n"
            f"   - Put REQUIRED SKILLS from JD first\n"
            f"   - Add any JD skills the candidate has equivalent experience with\n"
            f"   - Remove skills not relevant to this domain\n\n"
            f"5. NEVER change: company names, job titles, employment dates, school names\n"
            f"6. NEVER fabricate: only write what the candidate actually did (reframe, not invent)\n\n"
            f"Return ONLY this JSON (no markdown, no commentary):\n"
            f'{{"summary":"4-sentence targeted summary",'
            f'"experience":[{{"title":"UNCHANGED","company":"UNCHANGED","location":"UNCHANGED","dates":"UNCHANGED","bullets":["rewritten bullet 1","rewritten bullet 2"]}}],'
            f'"projects":[{{"name":"UNCHANGED","technologies":"may add JD-relevant tech","bullets":["rewritten"]}}],'
            f'"skills":["JD-required first","then other relevant"],'
            f'"ml_skills":["reordered and augmented"],'
            f'"tools":["reordered and augmented"],'
            f'"keywords_added":["every JD keyword now in this resume"]}}'
        ),
        system=(
            "You are a world-class technical resume writer who gets engineers hired at FAANG. "
            "You write aggressive, specific, metrics-driven resumes where every single bullet "
            "directly maps to a job requirement. You never waste a bullet on something the JD "
            "doesn't care about. Return ONLY valid JSON."
        ),
        max_tokens=4096,
    )

    tailored = _parse_json_response(raw)

    if not isinstance(tailored, dict) or not tailored.get("experience"):
        print("  Warning: tailoring returned bad JSON — using original")
        return dict(profile)

    # Merge: always lock company/title/dates to originals
    result = dict(profile)

    if tailored.get("summary"):
        result["summary"] = tailored["summary"]

    if tailored.get("experience"):
        safe_exp = []
        orig_exp = profile.get("experience") or []
        for i, exp in enumerate(tailored["experience"]):
            orig = orig_exp[i] if i < len(orig_exp) else {}
            safe_exp.append({
                "title":    orig.get("title",    exp.get("title", "")),
                "company":  orig.get("company",  exp.get("company", "")),
                "location": orig.get("location", exp.get("location", "")),
                "dates":    orig.get("dates",    exp.get("dates", "")),
                "bullets":  exp.get("bullets",   orig.get("bullets", [])),
            })
        result["experience"] = safe_exp

    if tailored.get("projects"):
        safe_proj = []
        orig_proj = profile.get("projects") or []
        for i, proj in enumerate(tailored["projects"]):
            orig = orig_proj[i] if i < len(orig_proj) else {}
            safe_proj.append({
                "name":         orig.get("name",         proj.get("name", "")),
                "technologies": proj.get("technologies", orig.get("technologies", "")),
                "dates":        orig.get("dates",        proj.get("dates", "")),
                "url":          orig.get("url",          proj.get("url", "")),
                "bullets":      proj.get("bullets",      orig.get("bullets", [])),
            })
        result["projects"] = safe_proj

    for key in ("skills", "ml_skills", "tools"):
        if tailored.get(key):
            result[key] = tailored[key]

    result["keywords_added"] = tailored.get("keywords_added", [])
    return result

# ---------------------------------------------------------------------------
# PDF renderer — Jake's Resume template
# ---------------------------------------------------------------------------
def render_pdf(profile: dict, output_filename: str) -> str:
    """
    Render profile data to a formatted, ATS-friendly PDF.

    Visual layout:
        FIRSTNAME LASTNAME                     ← 20pt bold, centered
        email | phone | location | linkedin    ← 9pt, centered
        ─────────────────────────────────      ← 1pt rule
        EXPERIENCE                             ← 11pt bold + 0.5pt rule
        Job Title             Company          ← bold left | bold right
                         Location · Dates      ← italic right
        • Bullet one
        • Bullet two
        EDUCATION
        School Name                  Dates     ← bold left | italic right
        Degree, GPA, Honors                    ← 10pt normal
        PROJECTS
        Name | Tech Stack            Date      ← bold left | italic right
        • Bullet
        TECHNICAL SKILLS
        Languages:   Python, SQL, Go
        ML / AI:     PyTorch, PySpark
        Tools:       AWS, Docker, K8s
        CERTIFICATIONS
        • AWS Solutions Architect
    """
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab not installed: pip install reportlab")

    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESUMES_DIR / output_filename

    # ── Page geometry ──────────────────────────────────────────────────────
    PAGE_W, PAGE_H = letter
    MARGIN         = 0.5 * inch
    BODY_WIDTH     = PAGE_W - 2 * MARGIN   # 7.5 inches

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize   = letter,
        leftMargin = MARGIN, rightMargin  = MARGIN,
        topMargin  = MARGIN, bottomMargin = MARGIN,
    )

    # ── Colors ─────────────────────────────────────────────────────────────
    BLACK = HexColor("#000000")
    DARK  = HexColor("#111111")
    GRAY  = HexColor("#444444")
    LIGHT = HexColor("#666666")
    LINE  = HexColor("#000000")

    # ── Fonts ──────────────────────────────────────────────────────────────
    F  = "Helvetica"
    FB = "Helvetica-Bold"
    FI = "Helvetica-Oblique"

    # ── Style factory ──────────────────────────────────────────────────────
    def S(name, font=F, size=10, color=DARK,
          align=TA_LEFT, before=0, after=0, leading=None, **kw):
        return ParagraphStyle(
            name,
            fontName     = font,
            fontSize     = size,
            textColor    = color,
            alignment    = align,
            spaceBefore  = before,
            spaceAfter   = after,
            leading      = leading or round(size * 1.25, 1),
            **kw,
        )

    # Define every style once, clearly named
    st_name    = S("name",    FB,  20, BLACK, TA_CENTER, 0,  2,  24)
    st_contact = S("contact", F,    9, LIGHT, TA_CENTER, 0,  4,  11)
    st_sec_hd  = S("sechd",   FB,  11, BLACK, TA_LEFT,   6,  1,  13)  # section heading
    st_job_l   = S("jobl",    FB,  10, BLACK, TA_LEFT,   0,  0,  12)  # left col of job row
    st_job_r   = S("jobr",    FI,  10, LIGHT, TA_RIGHT,  0,  0,  12)  # right col of job row
    st_job_sub = S("jobsub",  FI,  10, LIGHT, TA_LEFT,   0,  1,  12)  # degree / subtitle
    st_bullet  = S("bullet",  F,   10, DARK,  TA_LEFT,   0,  2,  12.5,
                   leftIndent = 0.18 * inch, firstLineIndent = -0.12 * inch)
    st_body    = S("body",    F,   10, DARK,  TA_LEFT,   0,  2,  13)
    st_sk_lbl  = S("sklbl",   FB,  10, BLACK, TA_LEFT,   0,  2,  12)
    st_sk_val  = S("skval",   F,   10, DARK,  TA_LEFT,   0,  2,  12)

    # ── Story builder ──────────────────────────────────────────────────────
    story = []

    def add_rule(thickness=0.6, color=LINE, before=1, after=3):
        story.append(HRFlowable(
            width       = "100%",
            thickness   = thickness,
            color       = color,
            spaceBefore = before,
            spaceAfter  = after,
        ))

    def add_section(title: str):
        story.append(Spacer(1, 3))
        story.append(Paragraph(title.upper(), st_sec_hd))
        add_rule(thickness=0.5, before=1, after=3)

    def add_two_col(left_text: str, right_text: str,
                    left_style, right_style, left_frac=0.60):
        """One table row with left and right paragraphs, zero padding."""
        left_w  = BODY_WIDTH * left_frac
        right_w = BODY_WIDTH * (1.0 - left_frac)
        tbl = Table(
            [[Paragraph(left_text, left_style),
              Paragraph(right_text, right_style)]],
            colWidths = [left_w, right_w],
            hAlign    = "LEFT",
        )
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "BOTTOM"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 0),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
        ]))
        story.append(tbl)

    def add_bullet(text: str):
        clean = re.sub(r"^[•\-–—*]\s*", "", str(text or "").strip())
        if clean:
            story.append(Paragraph(f"&#x2022;&#160;&#160;{_x(clean)}", st_bullet))

    def add_skill_row(label: str, items: list):
        clean = [str(i).strip() for i in items if str(i).strip()]
        if not clean:
            return
        tbl = Table(
            [[Paragraph(f"<b>{_x(label)}:</b>", st_sk_lbl),
              Paragraph(_x(",  ".join(clean)), st_sk_val)]],
            colWidths = [BODY_WIDTH * 0.22, BODY_WIDTH * 0.78],
            hAlign    = "LEFT",
        )
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING",   (0, 0), (-1, -1), 0),
            ("RIGHTPADDING",  (0, 0), (-1, -1), 0),
            ("TOPPADDING",    (0, 0), (-1, -1), 1),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 1),
        ]))
        story.append(tbl)

    # ── 1. HEADER ──────────────────────────────────────────────────────────
    name = (profile.get("name") or "Your Name").strip()
    story.append(Paragraph(_x(name), st_name))

    # Build contact line — only non-empty values
    contact_items = []
    for field in ("email", "phone", "location"):
        val = (profile.get(field) or "").strip()
        if val:
            contact_items.append(val)
    for field, prefix in (("linkedin", ""), ("github", ""), ("website", "")):
        val = (profile.get(field) or "").strip()
        if val:
            # Normalise URL display
            if not val.startswith("http"):
                val = val.lstrip("/")
            contact_items.append(val)

    if contact_items:
        story.append(Paragraph(
            "  |  ".join(_x(c) for c in contact_items),
            st_contact,
        ))

    add_rule(thickness=1.0, before=4, after=0)

    # ── 2. SUMMARY ─────────────────────────────────────────────────────────
    summary = (profile.get("summary") or "").strip()
    if summary:
        add_section("Summary")
        story.append(Paragraph(_x(summary), st_body))

    # ── 3. EXPERIENCE ──────────────────────────────────────────────────────
    experience = [e for e in (profile.get("experience") or [])
                  if (e.get("title") or e.get("company"))]
    if experience:
        add_section("Experience")
        for exp in experience:
            title   = (exp.get("title",    "") or "").strip()
            company = (exp.get("company",  "") or "").strip()
            loc     = (exp.get("location", "") or "").strip()
            dates   = (exp.get("dates",    "") or "").strip()
            bullets = [b for b in (exp.get("bullets") or []) if str(b).strip()]

            # Row 1: bold title left | bold company right
            add_two_col(
                f"<b>{_x(title)}</b>",
                f"<b>{_x(company)}</b>",
                st_job_l, st_job_r,
                left_frac=0.60,
            )

            # Row 2: empty left | italic "location  ·  dates" right
            sub_right = "  ·  ".join(p for p in [_x(loc), _x(dates)] if p)
            if sub_right:
                add_two_col(
                    "",
                    sub_right,
                    st_job_sub, st_job_r,
                    left_frac=0.60,
                )

            for b in bullets:
                add_bullet(b)

            story.append(Spacer(1, 6))

    # ── 4. PROJECTS ────────────────────────────────────────────────────────
    projects = [p for p in (profile.get("projects") or []) if p.get("name")]
    if projects:
        add_section("Projects")
        for proj in projects:
            pname   = (proj.get("name",         "") or "").strip()
            tech    = (proj.get("technologies", "") or "").strip()
            dates   = (proj.get("dates",        "") or "").strip()
            url     = (proj.get("url",          "") or "").strip()
            bullets = [b for b in (proj.get("bullets") or []) if str(b).strip()]

            # Row: bold name | tech   dates right
            left_text  = f"<b>{_x(pname)}</b>"
            if tech:
                left_text += f"  |  <i>{_x(tech)}</i>"
            add_two_col(left_text, _x(dates), st_job_l, st_job_r, left_frac=0.70)

            if url:
                story.append(Paragraph(f"<i>{_x(url)}</i>", st_job_sub))

            for b in bullets:
                add_bullet(b)

            story.append(Spacer(1, 6))

    # ── 5. EDUCATION ───────────────────────────────────────────────────────
    education = [e for e in (profile.get("education") or [])
                 if (e.get("degree") or e.get("school"))]
    if education:
        add_section("Education")
        for edu in education:
            school  = (edu.get("school",  "") or "").strip()
            degree  = (edu.get("degree",  "") or "").strip()
            loc     = (edu.get("location","") or "").strip()
            dates   = (edu.get("dates",   "") or "").strip()
            gpa     = (edu.get("gpa",     "") or "").strip()
            honors  = (edu.get("honors",  "") or "").strip()
            courses = (edu.get("relevant_courses", "") or "").strip()

            # Row: bold school left | dates right
            right_parts = [p for p in [_x(loc), _x(dates)] if p]
            add_two_col(
                f"<b>{_x(school)}</b>",
                "  ·  ".join(right_parts),
                st_job_l, st_job_r,
                left_frac=0.60,
            )

            # Degree + GPA + honors on one line
            sub_parts = [_x(degree)] if degree else []
            if gpa:    sub_parts.append(f"GPA: {_x(gpa)}")
            if honors: sub_parts.append(_x(honors))
            if sub_parts:
                story.append(Paragraph(",  ".join(sub_parts), st_job_sub))
            if courses:
                story.append(Paragraph(
                    f"<i>Relevant Coursework:</i>  {_x(courses)}", st_job_sub))

            story.append(Spacer(1, 6))

    # ── 6. TECHNICAL SKILLS ────────────────────────────────────────────────
    all_skills = list(profile.get("skills",    []))
    ml_skills  = list(profile.get("ml_skills", []))
    tools      = list(profile.get("tools",     []))

    if all_skills or ml_skills or tools:
        add_section("Technical Skills")

        # Separate programming languages from frameworks heuristically
        LANG_SET = {
            "python", "sql", "r", "java", "go", "golang", "scala", "kotlin",
            "swift", "typescript", "javascript", "js", "ts", "c", "c++", "c#",
            "ruby", "rust", "php", "matlab", "bash", "shell", "html", "css",
        }
        langs  = [s for s in all_skills if s.lower().split("/")[0].strip() in LANG_SET]
        fworks = [s for s in all_skills if s not in langs]

        if langs:      add_skill_row("Languages",   langs)
        if fworks:     add_skill_row("Frameworks",  fworks)
        if ml_skills:  add_skill_row("ML / AI",     ml_skills)
        if tools:      add_skill_row("Tools",       tools)

    # ── 7. CERTIFICATIONS ──────────────────────────────────────────────────
    def _clean_cert(raw: str) -> str:
        """Convert {'name': 'X', 'issuer': 'Y', 'date': 'Z'} to 'X – Y, Z'."""
        raw = raw.strip()
        if not raw.startswith("{"):
            return raw
        name   = re.search(r"'name'\s*:\s*'([^']+)'",   raw)
        issuer = re.search(r"'issuer'\s*:\s*'([^']+)'", raw)
        date   = re.search(r"'date'\s*:\s*'([^']+)'",   raw)
        parts  = []
        if name:   parts.append(name.group(1))
        if issuer: parts.append(issuer.group(1))
        if date:   parts.append(date.group(1))
        return ",  ".join(parts) if parts else raw

    certs = [_clean_cert(str(c)) for c in (profile.get("certifications") or [])
             if str(c).strip()]
    certs = [c for c in certs if c]
    if certs:
        add_section("Certifications")
        for cert in certs:
            add_bullet(cert)

    # ── 8. AWARDS ──────────────────────────────────────────────────────────
    awards = [str(a).strip() for a in (profile.get("awards") or []) if str(a).strip()]
    if awards:
        add_section("Awards & Honors")
        for award in awards:
            add_bullet(award)

    doc.build(story)
    return str(output_path)


# ---------------------------------------------------------------------------
# Public API — called by app.py
# ---------------------------------------------------------------------------
def generate(profile: dict, job_description: str = "",
             job_title: str = "Role", company: str = "Company") -> dict:
    """
    Tailor resume content for a job, render to PDF, return result dict.
    """
    if not REPORTLAB_AVAILABLE:
        return {"error": "reportlab not installed — run: pip install reportlab"}

    if not profile or not profile.get("name"):
        return {"error": "No profile found. Complete your profile in Settings first."}

    print(f"\n  Generating resume: {job_title} at {company}")
    print(f"  Profile: {profile.get('name')} | "
          f"{len(profile.get('experience') or [])} jobs | "
          f"{len(profile.get('education') or [])} edu | "
          f"{len(profile.get('projects') or [])} projects")

    # Tailor content if JD is provided
    if job_description.strip() and os.environ.get("ANTHROPIC_API_KEY"):
        print("  Tailoring with Claude...")
        tailored = tailor_for_job(profile, job_description, job_title, company)
    else:
        tailored = dict(profile)

    keywords_added = tailored.pop("keywords_added", [])

    # Generate filename
    co       = re.sub(r"\W+", "_", company)[:16]
    ti       = re.sub(r"\W+", "_", job_title)[:20]
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"resume_{co}_{ti}_{ts}.pdf"

    # Render PDF
    try:
        pdf_path = render_pdf(tailored, filename)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"error": f"PDF render failed: {exc}"}

    # ATS score
    job_stub = {
        "title":       job_title,
        "company":     company,
        "description": job_description,
    }
    score = ats_score_job(tailored, job_stub) if job_description.strip() else {}  # Score AFTER tailoring

    print(f"  Done: {filename}")
    return {
        "filename":       filename,
        "path":           pdf_path,
        "url":            f"/api/resume/download/{filename}",
        "keywords_added": keywords_added,
        "rewritten":      bool(job_description.strip() and os.environ.get("ANTHROPIC_API_KEY")),
        **score,
    }
