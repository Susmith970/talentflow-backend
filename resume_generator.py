"""
resume_generator.py
===================
Clean, production-grade resume generation with FAANG-optimized ATS tailoring.

How it works:
1. Profile data is collected via the UI (no PDF parsing during generation)
2. Two-pass Claude tailoring: deep JD analysis → full rewrite
3. Deterministic PDF renderer produces a clean, ATS-friendly resume

PDF Template (Jake's Resume — industry standard for engineers):
  - Name centered, large, bold, dark navy
  - Contact line centered, pipe-separated
  - Full-width rule (navy)
  - Core Competencies keyword grid (ATS boost)
  - Section: ALL CAPS bold + navy underline rule
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

import os as _os
_data_env = _os.environ.get("DATA_DIR", "")
if _data_env:
    DATA_DIR = Path(_data_env) / "data"
elif (Path(__file__).parent / "data").exists():
    DATA_DIR = Path(__file__).parent / "data"
else:
    DATA_DIR = Path(__file__).parent.parent / "data"
RESUMES_DIR = DATA_DIR / "resumes"
RESUMES_DIR.mkdir(parents=True, exist_ok=True)

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


def _x(value) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# ---------------------------------------------------------------------------
# JD Analysis — Pass 1
# ---------------------------------------------------------------------------
def _analyze_jd(job_title: str, company: str, jd: str) -> dict:
    if not jd.strip():
        return {}
    raw = _call_claude(
        prompt=(
            f"Analyse this job description for: {job_title} at {company}\n\n"
            f"JD:\n{jd[:4000]}\n\n"
            f"Extract EVERYTHING the resume must address. Return JSON only:\n"
            f"{{\"domain\": \"e.g. Data Analytics / ML Engineering\","
            f"\"seniority\": \"e.g. Senior / Staff / Lead\","
            f"\"required_skills\": [\"every required skill\"],"
            f"\"preferred_skills\": [\"preferred/nice-to-have skills\"],"
            f"\"key_responsibilities\": [\"top 5 daily responsibilities\"],"
            f"\"exact_keywords\": [\"every technical term, tool, framework, methodology\"],"
            f"\"action_verbs_used\": [\"verbs from JD: analyze, build, lead, etc\"],"
            f"\"metrics_mentioned\": [\"numbers: 100M users, petabyte scale, <100ms latency\"],"
            f"\"soft_skills\": [\"collaboration, communication, etc\"],"
            f"\"industry_terms\": [\"domain-specific jargon\"],"
            f"\"deal_breakers\": [\"must-have requirements\"],"
            f"\"resume_must_show\": [\"5 things a winning resume for this role must demonstrate\"]"
            f"}}"
        ),
        system="Return ONLY valid JSON. Be exhaustive — miss nothing from the JD.",
        max_tokens=2000,
    )
    return _parse_json_response(raw)


# ---------------------------------------------------------------------------
# ATS scoring
# ---------------------------------------------------------------------------
def ats_score_job(profile: dict, job: dict) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return {
            "ats_score": 0, "match_label": "Unscored",
            "match_reason": "", "matched_keywords": [],
            "missing_keywords": [], "ats_tips": [],
        }

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
            f"You are a senior technical recruiter at a FAANG company and ATS expert scoring a tailored resume.\n\n"
            f"JOB: {job.get('title','')} at {job.get('company','')}\n"
            f"JD: {job.get('description','')[:1500]}\n\n"
            f"RESUME:\n{resume_text}\n\n"
            f"Score 0-100 using these weights:\n"
            f"  Keyword match (40%): Does every major JD technical term appear verbatim?\n"
            f"  Bullet quality (30%): Are bullets STAR-formatted with quantified outcomes?\n"
            f"  Seniority alignment (15%): Does experience level match the role?\n"
            f"  ATS parse-ability (15%): Clean text, no tables breaking content, proper section labels?\n\n"
            f"FAANG ATS systems (Workday, Taleo, Greenhouse, Lever) specifically weight:\n"
            f"  - Exact keyword matches (not synonyms) from the JD\n"
            f"  - Quantified achievements (%, $, scale numbers)\n"
            f"  - Technical depth (not just listing tools, but describing how they used them)\n\n"
            f"Be honest. 85+ = recruiter would definitely shortlist.\n\n"
            f"Return JSON only:\n"
            f'{{"ats_score":88,"match_label":"Strong Match",'
            f'"match_reason":"2 specific sentences explaining the score",'
            f'"matched_keywords":["Python","SQL","dbt","Spark"],'
            f'"missing_keywords":["Looker","A/B testing"],'
            f'"ats_tips":["Add Looker to skills","Quantify data pipeline scale in bullet 3","Mirror JD phrase \\"cross-functional\\" in summary"]}}'
        ),
        system="Return ONLY valid JSON. Be precise and honest about the score.",
        max_tokens=700,
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
# AI tailoring — two-pass FAANG-optimized
# ---------------------------------------------------------------------------
def tailor_for_job(profile: dict, job_description: str,
                   job_title: str, company: str) -> dict:
    if not os.environ.get("ANTHROPIC_API_KEY") or not job_description.strip():
        return dict(profile)

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

    # Detect if this is a FAANG / top-tier company
    faang_names = {"google", "amazon", "apple", "meta", "microsoft", "netflix",
                   "facebook", "alphabet", "aws", "azure", "openai", "deepmind",
                   "blackrock", "goldman", "citadel", "two sigma", "jane street"}
    is_faang = any(f in company.lower() for f in faang_names)
    faang_note = (
        "\nFAANG/TOP-TIER COMPANY NOTE: This company's ATS and recruiters specifically look for:\n"
        "  - System-scale numbers (DAUs, QPS, petabytes, latency ms, uptime %)\n"
        "  - Ownership language: \"owned\", \"led\", \"designed\", \"architected\", \"drove\"\n"
        "  - Cross-functional impact: mention collaboration with PM, design, data science\n"
        "  - Complexity signals: distributed systems, ML at scale, multi-region, real-time\n"
        "  - Exact JD phrase mirrors (ATS exact-match scoring)\n"
    ) if is_faang else ""

    targeting = f"""JOB ANALYSIS — {job_title} at {company}
Domain: {domain} | Seniority: {seniority}
Required skills: {", ".join(required[:20])}
Preferred skills: {", ".join(preferred[:15])}
Exact keywords to use verbatim: {", ".join(exact_kw[:30])}
Industry terms: {", ".join(industry_terms[:10])}
Day-to-day responsibilities:
{chr(10).join(f'  - {r}' for r in responsibilities[:6])}
Scale/metrics from JD: {", ".join(metrics[:8])}
A winning resume must demonstrate:
{chr(10).join(f'  {i+1}. {m}' for i, m in enumerate(must_show[:6]))}{faang_note}"""

    yrs = int(profile.get("years_experience", 0) or 0)
    content = {
        "summary": profile.get("summary", ""),
        "years_experience": yrs,
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

    print("  Rewriting resume…")
    raw = _call_claude(
        system=(
            "You are a world-class technical resume writer who gets senior engineers "
            "hired at Google, Meta, Amazon, Apple, Microsoft, and top hedge funds. "
            "You write authentic, specific, metrics-driven resumes that read like a real "
            "engineer wrote them — not generic filler. "
            "Every bullet: [Power verb] + [specific technical action] + [JD keyword] + [quantified outcome]. "
            "STAR format: what was the scale, what did they build, what was the business result. "
            "ATS optimization: every exact keyword from the JD appears naturally at least once. "
            "Return ONLY valid JSON."
        ),
        prompt=f"""You are tailoring {profile.get("name","this candidate")}’s resume for:
ROLE: {job_title} at {company}

FULL JOB DESCRIPTION:
{job_description[:3500]}

{targeting}

CANDIDATE’S CURRENT RESUME:
{json.dumps(content, indent=2)[:4000]}

════════════════════════════════════════
YOUR TASK — REWRITE WITH FULL CREATIVE FREEDOM
════════════════════════════════════════

You have FULL CREATIVE FREEDOM to write compelling, realistic bullets.

✓ Infer realistic scenarios from the candidate’s tech stack + role
✓ Write NEW bullets that plausibly describe what someone with their background would have done
✓ Add realistic metrics (“~40%”, “over 500K”, “reduced from ~8hrs to <30min”)
✓ Mirror exact JD phrases — ATS scores exact string matches highly
✓ Summary: 3-4 punchy sentences, opens with years_experience, mirrors JD language
✓ Skills section: put required JD skills FIRST, then preferred, then other

RULES — ABSOLUTE CONSTRAINTS:
• NEVER change company names, job titles, employment dates, school names
• NEVER invent companies. Use exact company names from input.
• NEVER change years_experience number in summary
• NEVER claim a degree or certification they don’t have
• Most recent job: 5-6 bullets. Second job: 4-5. Earlier: 2-3. Internship: max 2-3.
• Return ALL experience entries — never omit any.
• Every bullet: [Power verb] + [technical action] + [JD keyword] + [metric/impact]

BULLET FORMULA EXAMPLES:
• \"Engineered real-time Kafka ingestion pipeline processing 2M+ events/day, reducing
  downstream ML feature latency from ~4hrs to under 90 seconds\"
• \"Architected multi-region EKS deployment with Terraform, cutting environment provisioning
  from 3 days to ~45 minutes while eliminating 100% of configuration drift\"
• \"Led migration of 40+ dbt models to Snowflake, reducing analyst query time ~65% and
  improving data lineage visibility across 12 downstream dashboards\"

Return ONLY this JSON:
{{"summary":"3-4 sentence punchy targeted summary",
"experience":[{{"title":"UNCHANGED","company":"UNCHANGED","location":"UNCHANGED","dates":"UNCHANGED","bullets":["bullet 1","bullet 2"]}}],
"projects":[{{"name":"UNCHANGED","technologies":"updated stack","bullets":["project bullet"]}}],
"skills":["JD-required skills first"],"ml_skills":["relevant ml skills"],"tools":["relevant tools"],
"keywords_added":["every JD keyword woven in"]}}""",
        max_tokens=4096,
    )

    tailored = _parse_json_response(raw)

    if not isinstance(tailored, dict) or not tailored.get("experience"):
        print("  Warning: tailoring returned bad JSON — using original profile")
        return dict(profile)

    result        = dict(profile)
    orig_exp      = profile.get("experience") or []
    orig_prj      = profile.get("projects") or []
    tailored_exps = tailored.get("experience", [])

    if tailored.get("summary"):
        result["summary"] = tailored["summary"]

    def _norm(s): return re.sub(r"\W+", "", str(s or "").lower())
    tailored_map = {}
    for i, texp in enumerate(tailored_exps):
        key = _norm(texp.get("company", "")) or f"__idx_{i}"
        tailored_map[key] = texp.get("bullets", [])
        tailored_map[f"__idx_{i}"] = tailored_map.get(f"__idx_{i}") or texp.get("bullets", [])

    safe_exp = []
    for i, orig in enumerate(orig_exp):
        key         = _norm(orig.get("company", ""))
        pos_key     = f"__idx_{i}"
        new_bullets = (tailored_map.get(key) or
                       tailored_map.get(pos_key) or
                       orig.get("bullets", []))
        safe_exp.append({
            "title":    orig.get("title",    ""),
            "company":  orig.get("company",  ""),
            "location": orig.get("location", ""),
            "dates":    orig.get("dates",    ""),
            "bullets":  new_bullets if new_bullets else orig.get("bullets", []),
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
# PDF renderer — Jake’s Resume template, enhanced with navy accent
# ---------------------------------------------------------------------------
def render_pdf(profile: dict, output_filename: str) -> str:
    """
    Render profile data to a formatted, ATS-friendly PDF.

    Visual layout:
        FIRSTNAME LASTNAME                     ← 20pt bold navy, centered
        email | phone | location | linkedin    ← 9pt, centered
        ───────────────────────────────── navy rule
        CORE COMPETENCIES (keyword grid for ATS)
        EXPERIENCE                             ← 11pt bold navy + rule
        Job Title             Company          ← bold left | bold right
                         Location · Dates      ← italic right
        • Bullet one
        ...
    """
    if not REPORTLAB_AVAILABLE:
        raise RuntimeError("reportlab not installed: pip install reportlab")

    RESUMES_DIR.mkdir(parents=True, exist_ok=True)
    output_path = RESUMES_DIR / output_filename

    PAGE_W, PAGE_H = letter
    MARGIN     = 0.50 * inch
    BODY_WIDTH = PAGE_W - 2 * MARGIN

    doc = SimpleDocTemplate(
        str(output_path),
        pagesize   = letter,
        leftMargin = MARGIN, rightMargin  = MARGIN,
        topMargin  = MARGIN, bottomMargin = MARGIN,
    )

    # Colors
    NAVY  = HexColor("#1a2744")   # header text + section rules
    BLACK = HexColor("#000000")
    DARK  = HexColor("#111111")
    GRAY  = HexColor("#444444")
    LIGHT = HexColor("#555555")
    RULE  = HexColor("#1a2744")   # navy rules for sections

    F  = "Helvetica"
    FB = "Helvetica-Bold"
    FI = "Helvetica-Oblique"

    def S(name, font=F, size=10, color=DARK,
          align=TA_LEFT, before=0, after=0, leading=None, **kw):
        return ParagraphStyle(
            name,
            fontName    = font,
            fontSize    = size,
            textColor   = color,
            alignment   = align,
            spaceBefore = before,
            spaceAfter  = after,
            leading     = leading or round(size * 1.25, 1),
            **kw,
        )

    st_name     = S("name",    FB,  20, NAVY,  TA_CENTER, 0,  2,  24)
    st_contact  = S("contact", F,    9, LIGHT, TA_CENTER, 0,  4,  11)
    st_sec_hd   = S("sechd",   FB,  11, NAVY,  TA_LEFT,   6,  1,  13)
    st_job_l    = S("jobl",    FB,  10, BLACK, TA_LEFT,   0,  0,  12)
    st_job_r    = S("jobr",    FI,  10, LIGHT, TA_RIGHT,  0,  0,  12)
    st_job_sub  = S("jobsub",  FI,  10, LIGHT, TA_LEFT,   0,  1,  12)
    st_bullet   = S("bullet",  F,   10, DARK,  TA_LEFT,   0,  2,  12.5,
                    leftIndent=0.18*inch, firstLineIndent=-0.12*inch)
    st_body     = S("body",    F,   10, DARK,  TA_LEFT,   0,  2,  13)
    st_sk_lbl   = S("sklbl",   FB,  10, BLACK, TA_LEFT,   0,  2,  12)
    st_sk_val   = S("skval",   F,   10, DARK,  TA_LEFT,   0,  2,  12)
    st_cc_item  = S("ccitem",  F,    9, DARK,  TA_CENTER, 0,  1,  11)

    story = []

    def add_rule(thickness=0.6, color=RULE, before=1, after=3):
        story.append(HRFlowable(
            width="100%", thickness=thickness, color=color,
            spaceBefore=before, spaceAfter=after,
        ))

    def add_section(title: str):
        story.append(Spacer(1, 3))
        story.append(Paragraph(title.upper(), st_sec_hd))
        add_rule(thickness=0.5, color=NAVY, before=1, after=3)

    def add_two_col(left_text, right_text, left_style, right_style, left_frac=0.60):
        lw = BODY_WIDTH * left_frac
        rw = BODY_WIDTH * (1.0 - left_frac)
        tbl = Table(
            [[Paragraph(left_text, left_style), Paragraph(right_text, right_style)]],
            colWidths=[lw, rw], hAlign="LEFT",
        )
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0,0), (-1,-1), "BOTTOM"),
            ("LEFTPADDING",   (0,0), (-1,-1), 0),
            ("RIGHTPADDING",  (0,0), (-1,-1), 0),
            ("TOPPADDING",    (0,0), (-1,-1), 0),
            ("BOTTOMPADDING", (0,0), (-1,-1), 0),
        ]))
        story.append(tbl)

    def add_bullet(text: str):
        clean_b = re.sub(r"^[•\-–—*]\s*", "", str(text or "").strip())
        if clean_b:
            story.append(Paragraph(f"• &nbsp;{_x(clean_b)}", st_bullet))

    def add_skill_row(label: str, items: list):
        clean_i = [str(i).strip() for i in items if str(i).strip()]
        if not clean_i:
            return
        tbl = Table(
            [[Paragraph(f"<b>{_x(label)}:</b>", st_sk_lbl),
              Paragraph(_x(",  ".join(clean_i)), st_sk_val)]],
            colWidths=[BODY_WIDTH*0.22, BODY_WIDTH*0.78], hAlign="LEFT",
        )
        tbl.setStyle(TableStyle([
            ("VALIGN",        (0,0), (-1,-1), "TOP"),
            ("LEFTPADDING",   (0,0), (-1,-1), 0),
            ("RIGHTPADDING",  (0,0), (-1,-1), 0),
            ("TOPPADDING",    (0,0), (-1,-1), 1),
            ("BOTTOMPADDING", (0,0), (-1,-1), 1),
        ]))
        story.append(tbl)

    # ── 1. HEADER ────────────────────────────────────────────────────────────────────
    name = (profile.get("name") or "Your Name").strip()
    story.append(Paragraph(_x(name), st_name))

    BAD_HOSTS = ("railway", "vercel", "herokuapp", "render.com", "localhost", "ngrok")
    contact_items = []
    for field in ("email", "phone", "location"):
        val = (profile.get(field) or "").strip()
        if val:
            contact_items.append(val)
    li = (profile.get("linkedin") or "").strip()
    if li:
        li = li.lstrip("/")
        if "linkedin.com" not in li:
            li = "linkedin.com/in/" + li
        contact_items.append(li)
    gh = (profile.get("github") or "").strip()
    if gh:
        gh = gh.lstrip("/")
        if "github.com" not in gh:
            gh = "github.com/" + gh
        contact_items.append(gh)
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

    add_rule(thickness=1.2, color=NAVY, before=4, after=0)

    # ── 2. SUMMARY ───────────────────────────────────────────────────────────────────
    summary = (profile.get("summary") or "").strip()
    if summary:
        add_section("Summary")
        story.append(Paragraph(_x(summary), st_body))

    # ── 3. CORE COMPETENCIES (ATS keyword grid) ────────────────────────────────
    # Pull top skills (JD-matched skills are first after tailoring)
    all_cc = (
        list(profile.get("skills", []))[:8] +
        list(profile.get("ml_skills", []))[:4] +
        list(profile.get("tools", []))[:4]
    )
    competencies = [s for s in all_cc if s and str(s).strip()][:15]
    if competencies:
        add_section("Core Competencies")
        # 3-column grid
        cols = 3
        rows = [competencies[i:i+cols] for i in range(0, len(competencies), cols)]
        col_w = BODY_WIDTH / cols
        for row in rows:
            padded = row + [""] * (cols - len(row))
            cells  = [Paragraph(f"• {_x(c)}", st_cc_item) if c else Paragraph("", st_cc_item)
                      for c in padded]
            tbl = Table([cells], colWidths=[col_w]*cols, hAlign="LEFT")
            tbl.setStyle(TableStyle([
                ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
                ("LEFTPADDING",   (0,0), (-1,-1), 2),
                ("RIGHTPADDING",  (0,0), (-1,-1), 2),
                ("TOPPADDING",    (0,0), (-1,-1), 1),
                ("BOTTOMPADDING", (0,0), (-1,-1), 1),
            ]))
            story.append(tbl)

    # ── 4. EXPERIENCE ──────────────────────────────────────────────────────────────
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

            add_two_col(
                f"<b>{_x(title)}</b>",
                f"<b>{_x(company)}</b>",
                st_job_l, st_job_r, left_frac=0.60,
            )
            sub_right = "  ·  ".join(p for p in [_x(loc), _x(dates)] if p)
            if sub_right:
                add_two_col("", sub_right, st_job_sub, st_job_r, left_frac=0.60)

            for b in bullets:
                add_bullet(b)
            story.append(Spacer(1, 6))

    # ── 5. PROJECTS ──────────────────────────────────────────────────────────────────
    projects = [p for p in (profile.get("projects") or []) if p.get("name")]
    if projects:
        add_section("Projects")
        for proj in projects:
            pname   = (proj.get("name",         "") or "").strip()
            tech    = (proj.get("technologies", "") or "").strip()
            dates   = (proj.get("dates",        "") or "").strip()
            url     = (proj.get("url",          "") or "").strip()
            bullets = [b for b in (proj.get("bullets") or []) if str(b).strip()]

            left_text = f"<b>{_x(pname)}</b>"
            if tech:
                left_text += f"  |  <i>{_x(tech)}</i>"
            add_two_col(left_text, _x(dates), st_job_l, st_job_r, left_frac=0.70)
            if url:
                story.append(Paragraph(f"<i>{_x(url)}</i>", st_job_sub))
            for b in bullets:
                add_bullet(b)
            story.append(Spacer(1, 6))

    # ── 6. EDUCATION ────────────────────────────────────────────────────────────────
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

            right_parts = [p for p in [_x(loc), _x(dates)] if p]
            add_two_col(
                f"<b>{_x(school)}</b>",
                "  ·  ".join(right_parts),
                st_job_l, st_job_r, left_frac=0.60,
            )
            sub_parts = [_x(degree)] if degree else []
            if gpa:    sub_parts.append(f"GPA: {_x(gpa)}")
            if honors: sub_parts.append(_x(honors))
            if sub_parts:
                story.append(Paragraph(",  ".join(sub_parts), st_job_sub))
            if courses:
                story.append(Paragraph(
                    f"<i>Relevant Coursework:</i>  {_x(courses)}", st_job_sub))
            story.append(Spacer(1, 6))

    # ── 7. TECHNICAL SKILLS ────────────────────────────────────────────────────
    all_skills = list(profile.get("skills",    []))
    ml_skills  = list(profile.get("ml_skills", []))
    tools      = list(profile.get("tools",     []))

    if all_skills or ml_skills or tools:
        add_section("Technical Skills")
        LANG_SET = {
            "python", "sql", "r", "java", "go", "golang", "scala", "kotlin",
            "swift", "typescript", "javascript", "js", "ts", "c", "c++", "c#",
            "ruby", "rust", "php", "matlab", "bash", "shell", "html", "css",
        }
        langs  = [s for s in all_skills if s.lower().split("/")[0].strip() in LANG_SET][:12]
        fworks = [s for s in all_skills if s not in langs][:12]
        ml_cap = ml_skills[:12]
        tools_cap = tools[:12]

        if langs:     add_skill_row("Languages",  langs)
        if fworks:    add_skill_row("Frameworks", fworks)
        if ml_cap:    add_skill_row("ML / AI",    ml_cap)
        if tools_cap: add_skill_row("Tools",      tools_cap)

    # ── 8. CERTIFICATIONS ────────────────────────────────────────────────────────
    def _clean_cert(raw: str) -> str:
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

    certs = [_clean_cert(str(c)) for c in (profile.get("certifications") or []) if str(c).strip()]
    certs = [c for c in certs if c]
    if certs:
        add_section("Certifications")
        for cert in certs:
            add_bullet(cert)

    # ── 9. AWARDS ────────────────────────────────────────────────────────────────────
    awards = [str(a).strip() for a in (profile.get("awards") or []) if str(a).strip()]
    if awards:
        add_section("Awards & Honors")
        for award in awards:
            add_bullet(award)

    doc.build(story)
    return str(output_path)


# ---------------------------------------------------------------------------
# Extract profile from uploaded base resume (PDF / DOCX / TXT)
# ---------------------------------------------------------------------------
def extract_profile_from_file(file_path: str) -> dict:
    fpath = Path(file_path)
    ext   = fpath.suffix.lower()

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

    result_base = {"raw_resume_text": raw[:15000]}

    if not os.environ.get("ANTHROPIC_API_KEY"):
        return result_base

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

    CHUNK = 7500
    if len(raw) <= CHUNK:
        data = _extract_chunk(raw)
    else:
        mid   = len(raw) // 2
        split = raw.rfind("\n\n", mid - 500, mid + 500)
        if split == -1:
            split = mid
        first_half  = raw[:split]
        second_half = raw[split:]

        d1 = _extract_chunk(first_half,  " (PART 1 of 2 — header + first jobs)")
        d2 = _extract_chunk(second_half, " (PART 2 of 2 — remaining jobs, projects, skills, certs)")

        data = d1 if isinstance(d1, dict) else {}
        if isinstance(d2, dict):
            existing_cos = {e.get("company","").lower() for e in (data.get("experience") or [])}
            for exp in (d2.get("experience") or []):
                if exp.get("company","").lower() not in existing_cos:
                    data.setdefault("experience", []).append(exp)
                    existing_cos.add(exp.get("company","").lower())

            existing_proj = {p.get("name","").lower() for p in (data.get("projects") or [])}
            for proj in (d2.get("projects") or []):
                if proj.get("name","").lower() not in existing_proj:
                    data.setdefault("projects", []).append(proj)
                    existing_proj.add(proj.get("name","").lower())

            for key in ("skills", "ml_skills", "tools", "certifications", "awards"):
                combined = list(dict.fromkeys(
                    (data.get(key) or []) + (d2.get(key) or [])
                ))
                if combined:
                    data[key] = combined

            for field in ("name","email","phone","location","linkedin","github",
                          "website","title","summary","current_company"):
                if not data.get(field) and d2.get(field):
                    data[field] = d2[field]

            existing_schools = {e.get("school","").lower() for e in (data.get("education") or [])}
            for edu in (d2.get("education") or []):
                if edu.get("school","").lower() not in existing_schools:
                    data.setdefault("education", []).append(edu)

    if not isinstance(data, dict):
        return result_base

    for section in ("experience", "projects"):
        for item in (data.get(section) or []):
            if isinstance(item.get("bullets"), str):
                item["bullets"] = [b.strip("• ").strip()
                                   for b in re.split(r"[\n•]", item["bullets"])
                                   if b.strip("• ").strip()]

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

    data["raw_resume_text"] = raw[:15000]
    return data


# ---------------------------------------------------------------------------
# Public API — called by app.py
# ---------------------------------------------------------------------------
def generate(profile: dict, job_description: str = "",
             job_title: str = "Role", company: str = "Company") -> dict:
    if not REPORTLAB_AVAILABLE:
        return {"error": "reportlab not installed — run: pip install reportlab"}

    if not profile or not profile.get("name"):
        return {"error": "No profile found. Complete your profile in Settings first."}

    print(f"\n  Generating resume: {job_title} at {company}")
    print(f"  Profile: {profile.get('name')} | "
          f"{len(profile.get('experience') or [])} jobs | "
          f"{len(profile.get('education') or [])} edu | "
          f"{len(profile.get('projects') or [])} projects")

    if job_description.strip() and os.environ.get("ANTHROPIC_API_KEY"):
        print("  Tailoring with Claude…")
        tailored = tailor_for_job(profile, job_description, job_title, company)
    else:
        tailored = dict(profile)

    keywords_added = tailored.pop("keywords_added", [])

    co       = re.sub(r"\W+", "_", company)[:16]
    ti       = re.sub(r"\W+", "_", job_title)[:20]
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"resume_{co}_{ti}_{ts}.pdf"

    try:
        pdf_path = render_pdf(tailored, filename)
    except Exception as exc:
        import traceback
        traceback.print_exc()
        return {"error": f"PDF render failed: {exc}"}

    job_stub = {
        "title":       job_title,
        "company":     company,
        "description": job_description,
    }
    score = ats_score_job(tailored, job_stub) if job_description.strip() else {}

    print(f"  Done: {filename}")
    return {
        "filename":       filename,
        "path":           pdf_path,
        "url":            f"/api/resume/download/{filename}",
        "keywords_added": keywords_added,
        "rewritten":      bool(job_description.strip() and os.environ.get("ANTHROPIC_API_KEY")),
        **score,
    }
