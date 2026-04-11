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
    Pass 1 — Deep JD analysis
    Pass 2 — Rewrite with full creative freedom to write realistic, specific,
              metrics-driven bullets that sound like a real engineer wrote them.
    """
    if not os.environ.get("ANTHROPIC_API_KEY") or not job_description.strip():
        return dict(profile)

    # ── Pass 1: Analyse the JD ───────────────────────────────────────────────
    print("  Analysing JD…")
    analysis      = _analyze_jd(job_title, company, job_description) or {}
    required      = analysis.get("required_skills", [])
    preferred     = analysis.get("preferred_skills", [])
    exact_kw      = analysis.get("exact_keywords", [])
    responsibilities = analysis.get("key_responsibilities", [])
    must_show     = analysis.get("resume_must_show", [])
    metrics       = analysis.get("metrics_mentioned", [])
    domain        = analysis.get("domain", "")
    seniority     = analysis.get("seniority", "")
    industry_terms = analysis.get("industry_terms", [])

    targeting = f"""JOB ANALYSIS — {job_title} at {company}
Domain: {domain} | Seniority: {seniority}
Required skills: {", ".join(required[:20])}
Preferred skills: {", ".join(preferred[:15])}
Exact keywords to use: {", ".join(exact_kw[:30])}
Industry terms: {", ".join(industry_terms[:10])}
Day-to-day responsibilities:
{chr(10).join(f"  - {r}" for r in responsibilities[:6])}
Scale/metrics from JD: {", ".join(metrics[:8])}
A winning resume must demonstrate:
{chr(10).join(f"  {i+1}. {m}" for i, m in enumerate(must_show[:6]))}"""

    # ── Build content payload ────────────────────────────────────────────────
    content = {
        "summary": profile.get("summary", ""),
        "years_experience": profile.get("years_experience", 0),
        "experience": [
            {"title": e.get("title",""), "company": e.get("company",""),
             "location": e.get("location",""), "dates": e.get("dates",""),
             "bullets": e.get("bullets",[])}
            for e in (profile.get("experience") or [])
        ],
        "projects": [
            {"name": p.get("name",""), "technologies": p.get("technologies",""),
             "bullets": p.get("bullets",[])}
            for p in (profile.get("projects") or [])
        ],
        "skills":    profile.get("skills", []),
        "ml_skills": profile.get("ml_skills", []),
        "tools":     profile.get("tools", []),
    }

    # ── Pass 2: Rewrite with full creative freedom ───────────────────────────
    print("  Rewriting resume…")
    raw = _call_claude(
        system=(
            "You are a world-class technical resume writer who gets senior engineers "
            "hired at top companies. You write authentic, specific, metrics-driven resumes "
            "that read like a real engineer wrote them — not generic filler. "
            "Every bullet tells a concrete story: what was the problem, what did they build, "
            "what was the measurable outcome. Return ONLY valid JSON."
        ),
        prompt=f"""You are tailoring {profile.get("name","this candidate")}\'s resume for:
ROLE: {job_title} at {company}

FULL JOB DESCRIPTION:
{job_description[:3500]}

{targeting}

CANDIDATE\'S CURRENT RESUME:
{json.dumps(content, indent=2)[:4000]}

════════════════════════════════════════════
YOUR TASK — REWRITE WITH FULL CREATIVE FREEDOM
════════════════════════════════════════════

You have FULL CREATIVE FREEDOM to write compelling, realistic bullets.
Think: what would a top engineer in this exact role actually have done?
Write bullets that sound like a real senior person wrote them — specific,
technical, and with real business impact. You can:

✓ Infer realistic scenarios from the candidate\'s tech stack + role
✓ Write NEW bullets that plausibly describe what someone with their background
  would have done at that company (e.g. "Led migration of X to Y, reducing Z by N%")
✓ Add realistic metrics where none exist (use hedged language: "~40%", "over 500K",
  "reduced from ~8hrs to <30min")
✓ Connect their AWS experience to Azure JD requirements naturally
✓ Write the summary from scratch — make it punchy and role-specific

RULES:
• NEVER change company names, job titles, employment dates, or school names
• NEVER claim a degree or certification they don\'t have
• Keep bullets technically accurate to their stack (don\'t invent unrelated tech)
• Most recent job: 5-6 bullets. Each earlier job: 3-4 bullets.
• Every bullet: [Power verb] + [specific technical action] + [JD keyword] + [metric]
• Summary: 3-4 sentences, mirrors JD language, leads with years + domain

BULLET FORMULA EXAMPLES:
• "Engineered a real-time Kafka ingestion layer processing 2M+ events/day,
  reducing data latency from ~4hrs to under 90 seconds for downstream ML pipelines"
• "Automated Terraform-based provisioning of EKS clusters, cutting new environment
  setup from 3 days to ~45 minutes and eliminating 100% of manual config drift"
• "Built dbt transformation models across 40+ tables, replacing ad-hoc SQL and
  reducing analyst query time by ~65% while improving data lineage visibility"

Return ONLY this JSON (no markdown, no commentary):
{{"summary":"3-4 sentence punchy targeted summary",
"experience":[{{"title":"UNCHANGED","company":"UNCHANGED","location":"UNCHANGED","dates":"UNCHANGED","bullets":["compelling bullet 1","compelling bullet 2","compelling bullet 3"]}}],
"projects":[{{"name":"UNCHANGED","technologies":"updated tech stack","bullets":["rewritten project bullet"]}}],
"skills":["required skills first"],"ml_skills":["relevant ml skills"],"tools":["relevant tools"],
"keywords_added":["every JD keyword now woven into resume"]}}""",
        max_tokens=4096,
    )

    tailored = _parse_json_response(raw)

    if not isinstance(tailored, dict) or not tailored.get("experience"):
        print("  Warning: tailoring returned bad JSON — using original profile")
        return dict(profile)

    # Merge — lock factual fields, take Claude\'s creative bullets
    result   = dict(profile)
    orig_exp = profile.get("experience") or []
    orig_prj = profile.get("projects") or []

    if tailored.get("summary"):
        result["summary"] = tailored["summary"]

    safe_exp = []
    for i, exp in enumerate(tailored.get("experience", [])):
        orig = orig_exp[i] if i < len(orig_exp) else {}
        safe_exp.append({
            "title":    orig.get("title",    exp.get("title", "")),
            "company":  orig.get("company",  exp.get("company", "")),
            "location": orig.get("location", exp.get("location", "")),
            "dates":    orig.get("dates",    exp.get("dates", "")),
            "bullets":  exp.get("bullets",   orig.get("bullets", [])),
        })
    result["experience"] = safe_exp

    safe_prj = []
    for i, proj in enumerate(tailored.get("projects", [])):
        orig = orig_prj[i] if i < len(orig_prj) else {}
        safe_prj.append({
            "name":         orig.get("name",         proj.get("name", "")),
            "technologies": proj.get("technologies", orig.get("technologies", "")),
            "dates":        orig.get("dates",        proj.get("dates", "")),
            "url":          orig.get("url",          proj.get("url", "")),
            "bullets":      proj.get("bullets",      orig.get("bullets", [])),
        })
    result["projects"] = safe_prj

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

    # Build contact line: email | phone | location | linkedin | github
    # Filters out deployment URLs (railway, vercel, heroku, render, localhost)
    BAD_HOSTS = ("railway", "vercel", "herokuapp", "render.com", "localhost", "ngrok")
    contact_items = []
    for field in ("email", "phone", "location"):
        val = (profile.get(field) or "").strip()
        if val:
            contact_items.append(val)
    # LinkedIn
    li = (profile.get("linkedin") or "").strip()
    if li:
        li = li.lstrip("/")
        if "linkedin.com" not in li:
            li = "linkedin.com/in/" + li
        contact_items.append(li)
    # GitHub
    gh = (profile.get("github") or "").strip()
    if gh:
        gh = gh.lstrip("/")
        if "github.com" not in gh:
            gh = "github.com/" + gh
        contact_items.append(gh)
    # Personal website — only if it's a real personal site
    for ws_field in ("portfolio_url", "website"):
        ws = (profile.get(ws_field) or "").strip()
        if ws and not any(bad in ws.lower() for bad in BAD_HOSTS):
            contact_items.append(ws)
            break

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
        langs  = [s for s in all_skills if s.lower().split("/")[0].strip() in LANG_SET][:12]
        fworks = [s for s in all_skills if s not in langs][:12]
        ml_cap = ml_skills[:12]
        tools_cap = tools[:12]

        if langs:     add_skill_row("Languages",   langs)
        if fworks:    add_skill_row("Frameworks",  fworks)
        if ml_cap:    add_skill_row("ML / AI",     ml_cap)
        if tools_cap: add_skill_row("Tools",       tools_cap)

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
# Extract profile from uploaded base resume (PDF / DOCX / TXT)
# Called by app.py /api/profile/upload-resume
# ---------------------------------------------------------------------------

def extract_profile_from_file(file_path: str) -> dict:
    """
    Extract full structured profile from a resume file using Claude.
    Called ONCE on upload — result saved to DB, never re-read from file.

    Strategy: split long resumes into two chunks, extract from each,
    then merge so no job/project/skill is ever truncated away.
    """
    fpath = Path(file_path)
    ext   = fpath.suffix.lower()

    # ── 1. Read raw text ─────────────────────────────────────────────────────
    raw = ""
    try:
        if ext == ".pdf":
            from pdfminer.high_level import extract_text as _pdf
            raw = _pdf(str(fpath))
        elif ext == ".docx":
            import zipfile, xml.etree.ElementTree as ET
            with zipfile.ZipFile(str(fpath)) as z:
                xml_bytes = z.read("word/document.xml")
            ns_t = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"
            raw  = " ".join(n.text for n in ET.fromstring(xml_bytes).iter(ns_t) if n.text)
        else:
            raw = fpath.read_text(errors="replace")
    except Exception as exc:
        return {"error": f"Cannot read file: {exc}"}

    raw = raw.strip()
    if not raw:
        return {"error": "No text could be extracted from the file"}

    # Always store raw text so profile UI can display it
    result_base = {"raw_resume_text": raw[:15000]}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return result_base

    # ── 2. Build extraction prompt ────────────────────────────────────────────
    SCHEMA = (
        '{"name":"","email":"","phone":"","location":"","linkedin":"","github":"",'
        '"website":"","title":"","summary":"","years_experience":0,"current_company":"",'
        '"target_roles":[],'
        '"experience":['
        '{"title":"","company":"","location":"","dates":"","bullets":[]}'
        '],'
        '"education":['
        '{"degree":"","school":"","location":"","dates":"","gpa":"","honors":""}'
        '],'
        '"projects":['
        '{"name":"","technologies":"","dates":"","url":"","bullets":[]}'
        '],'
        '"skills":[],"ml_skills":[],"tools":[],"certifications":[],"awards":[]}'
    )

    SYSTEM = (
        "You are an expert resume parser. Extract ALL information with 100% recall — "
        "every job, every bullet, every project, every skill. "
        "Return ONLY valid JSON matching the schema exactly. No markdown fences."
    )

    def _extract_chunk(chunk: str, hint: str = "") -> dict:
        prompt = (
            f"Parse this resume{hint} and extract ALL information into this exact JSON schema.\n"
            f"CRITICAL: Extract EVERY job with ALL bullets. Extract EVERY project. "
            f"Do not summarise or skip anything.\n\n"
            f"RESUME TEXT:\n{chunk}\n\n"
            f"Return this JSON structure (fill every field you find):\n{SCHEMA}"
        )
        raw_resp = _call_claude(prompt, SYSTEM, max_tokens=4096)
        return _parse_json_response(raw_resp)

    # ── 3. Single call if resume fits; two calls if long ─────────────────────
    CHUNK = 7500   # safe limit — well below Claude's context, gives full jobs
    if len(raw) <= CHUNK:
        data = _extract_chunk(raw)
    else:
        # Split at a natural boundary near the midpoint
        mid   = len(raw) // 2
        split = raw.rfind("\n\n", mid - 500, mid + 500)
        if split == -1:
            split = mid
        first_half  = raw[:split]
        second_half = raw[split:]

        d1 = _extract_chunk(first_half,  " (PART 1 of 2 — header + first jobs)")
        d2 = _extract_chunk(second_half, " (PART 2 of 2 — remaining jobs, projects, skills, certs)")

        # Merge: take header/contact from part 1, merge lists from both
        data = d1 if isinstance(d1, dict) else {}
        if isinstance(d2, dict):
            # Append jobs from part 2 that aren't already in part 1
            existing_cos = {e.get("company","").lower() for e in (data.get("experience") or [])}
            for exp in (d2.get("experience") or []):
                if exp.get("company","").lower() not in existing_cos:
                    data.setdefault("experience", []).append(exp)
                    existing_cos.add(exp.get("company","").lower())

            # Append projects from part 2
            existing_proj = {p.get("name","").lower() for p in (data.get("projects") or [])}
            for proj in (d2.get("projects") or []):
                if proj.get("name","").lower() not in existing_proj:
                    data.setdefault("projects", []).append(proj)
                    existing_proj.add(proj.get("name","").lower())

            # Merge skills (union, dedup)
            for key in ("skills", "ml_skills", "tools", "certifications", "awards"):
                combined = list(dict.fromkeys(
                    (data.get(key) or []) + (d2.get(key) or [])
                ))
                if combined:
                    data[key] = combined

            # Fill blank contact fields from part 2 if part 1 missed them
            for field in ("name","email","phone","location","linkedin","github",
                          "website","title","summary","current_company"):
                if not data.get(field) and d2.get(field):
                    data[field] = d2[field]

            # Education — merge
            existing_schools = {e.get("school","").lower() for e in (data.get("education") or [])}
            for edu in (d2.get("education") or []):
                if edu.get("school","").lower() not in existing_schools:
                    data.setdefault("education", []).append(edu)

    if not isinstance(data, dict):
        return result_base

    # ── 4. Post-process ───────────────────────────────────────────────────────
    # Normalise bullet strings → lists
    for section in ("experience", "projects"):
        for item in (data.get(section) or []):
            if isinstance(item.get("bullets"), str):
                item["bullets"] = [b.strip("• ").strip()
                                   for b in re.split(r"[\n•]", item["bullets"])
                                   if b.strip("• ").strip()]

    # Calculate years_experience from date spans if Claude didn't extract it
    if not data.get("years_experience") and data.get("experience"):
        total = 0
        cy    = datetime.now().year
        for exp in data["experience"]:
            years = re.findall(r"(\d{4})", str(exp.get("dates", "")))
            if len(years) >= 2:
                total += (int(years[1]) - int(years[0])) * 12
            elif years:
                total += (cy - int(years[0])) * 12
        if total > 0:
            data["years_experience"] = max(1, round(total / 12))

    # Add raw text
    data["raw_resume_text"] = raw[:15000]
    return data


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
