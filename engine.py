"""Build a US-style resume Word file from JSON, then convert it to PDF."""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.text import WD_LINE_SPACING
from docx.opc.constants import RELATIONSHIP_TYPE
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Inches, Pt, RGBColor

FONT = "Arial"
BLUE = RGBColor(0x27, 0x73, 0xC4)
GRAY = RGBColor(0x44, 0x44, 0x44)
BLACK = RGBColor(0x00, 0x00, 0x00)

# Page geometry, shared by the Word and PDF renderers (US Letter, one column).
PAGE_WIDTH_IN = 8.5
PAGE_HEIGHT_IN = 11.0
MARGIN_SIDE_IN = 0.70
MARGIN_TOP_IN = 0.62
MARGIN_BOTTOM_IN = 0.62
CONTENT_WIDTH_TWIPS = round((PAGE_WIDTH_IN - 2 * MARGIN_SIDE_IN) * 1440)  # usable line width


@dataclass(frozen=True)
class TextStyle:
    """One paragraph style. Both renderers are built from these, so the layouts stay in step.

    Spacing is only ever "before" (points). Word adds before and after together while ReportLab merges
    them, so keeping one side zero is what lets the two files line up.
    """

    name: str
    size: float = 10  # Word stores sizes in half-points, so use whole or .5 values
    bold: bool = False
    color: str = "000000"
    before: float = 0.0
    line: float = 13.0  # minimum line height, points
    keep_next: bool = False
    keep_lines: bool = False
    left: float = 0.0
    hanging: float = 0.0


STYLES: dict[str, TextStyle] = {
    "name": TextStyle("ResumeName", size=20, bold=True, color="172B4D", line=24),
    "subtitle": TextStyle("ResumeSubtitle", size=10, color="374151", before=1, line=13),
    "contact": TextStyle("ResumeContact", size=9, color="4B5563", before=2, line=12),
    "section": TextStyle("ResumeSection", size=11, bold=True, color="203B60", before=10, line=14, keep_next=True),
    "body": TextStyle("ResumeBody", size=10, color="111827", before=3, line=12.2, keep_lines=True),
    "employer": TextStyle("ResumeEmployer", size=10.5, bold=True, color="111827", before=8, line=13, keep_next=True),
    "dates": TextStyle("ResumeDates", size=10, color="374151", line=13),  # run formatting on the employer line
    "job_title": TextStyle("ResumeJobTitle", size=10, bold=True, color="374151", before=1, line=12.6, keep_next=True),
    "role_context": TextStyle("ResumeRoleContext", size=10, color="374151", before=2, line=12.2, keep_next=True),
    "bullet": TextStyle("ResumeBullet", size=10, color="111827", before=2.3, line=12.2, keep_lines=True,
                        left=13.5, hanging=9.15),
    "skills": TextStyle("ResumeSkills", size=10, color="111827", before=2.5, line=12.2, keep_lines=True),
    "competency": TextStyle("ResumeCompetency", size=10, color="111827", before=2.5, line=12.2, keep_lines=True),
    "education": TextStyle("ResumeEducation", size=10, color="111827", before=3, line=12.2),
    "extra": TextStyle("ResumeExtra", size=10, color="111827", before=2, line=12.2, keep_lines=True),
}
FIRST_AFTER_HEADING = 3.0  # the first item under a section heading
HIGHLIGHT_RE = re.compile(r"(\S+?)\s*\[highlight\]", re.IGNORECASE)
PLACEHOLDER_RE = re.compile(r"\[(?:Verify|Verified)\b", re.IGNORECASE)
DATE_RANGE_SPLIT_RE = re.compile(r"\s*[-–—]\s*")
MONTH_ABBREV = (
    "",
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)

OMITTED_TOP_LEVEL = {
    "company",
    "company_name",
    "job_description",
    "company_information",
    "job_link",
}

DEFAULT_PROFILE = {
    "name": "Christopher Hosier",
    "email": "christopher.a.hosier@outlook.com",
    "phone": "(209) 322-9526",
    "location": "Mt Dora, FL",
}

DEFAULT_EDUCATION = [
    {
        "school": "University of Florida",
        "location": "Gainesville, FL",
        "degree": "B.S. Computer Science",
        "dates": "",
    }
]


def set_profile_defaults(
    *,
    name: str | None = None,
    email: str | None = None,
    phone: str | None = None,
    location: str | None = None,
    education_school: str | None = None,
    education_location: str | None = None,
    education_degree: str | None = None,
    education_dates: str | None = None,
) -> None:
    """Update the defaults used when resume JSON omits contact / education fields.

    Called from Settings so name, email, phone, location, and education are configurable.
    """
    if name is not None and str(name).strip():
        DEFAULT_PROFILE["name"] = str(name).strip()
    if email is not None and str(email).strip():
        DEFAULT_PROFILE["email"] = str(email).strip()
    if phone is not None and str(phone).strip():
        DEFAULT_PROFILE["phone"] = str(phone).strip()
    if location is not None and str(location).strip():
        DEFAULT_PROFILE["location"] = str(location).strip()

    edu = DEFAULT_EDUCATION[0]
    if education_school is not None and str(education_school).strip():
        edu["school"] = str(education_school).strip()
    if education_location is not None and str(education_location).strip():
        edu["location"] = str(education_location).strip()
    if education_degree is not None and str(education_degree).strip():
        edu["degree"] = str(education_degree).strip()
    if education_dates is not None:
        edu["dates"] = str(education_dates).strip()


class ResumeError(ValueError):
    """The JSON is readable but is not a usable resume."""


class PdfExportError(RuntimeError):
    """The Word file was written, but PDF conversion failed."""


@dataclass
class ExportResult:
    docx_path: Path
    pdf_path: Path | None
    cover_letter_path: Path | None
    warnings: list[str]
    pdf_converter: str | None


def load_resume(text: str) -> dict:
    raw = text.strip()
    if not raw:
        raise ResumeError("Paste a resume JSON object first.")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ResumeError(
            f"This is not valid JSON ({exc.msg}) near line {exc.lineno}, column {exc.colno}."
        ) from exc
    if not isinstance(data, dict):
        raise ResumeError("Paste one JSON object. A list or a single value cannot be exported.")
    resume = normalize_resume(data)
    if not (
        clean_text(resume.get("name"))
        or clean_text(resume.get("subtitle"))
        or clean_text(resume.get("summary"))
        or resume.get("experience")
    ):
        raise ResumeError(
            'This JSON needs at least a "name", "role_title"/"subtitle", "summary", or "experience".'
        )
    return resume


def normalize_resume(data: dict) -> dict:
    """Accept the original schema and the newer skill_groups schema."""
    resume = dict(data)

    for key, default in DEFAULT_PROFILE.items():
        if not clean_text(resume.get(key)):
            resume[key] = default
        elif key in {"email", "phone", "location", "name"}:
            resume[key] = clean_text(resume.get(key))

    if not clean_text(resume.get("subtitle")):
        role_title = clean_text(resume.get("role_title"))
        if role_title:
            resume["subtitle"] = role_title

    if "technicalskills" not in resume and isinstance(resume.get("skill_groups"), list):
        resume["technicalskills"] = _groups_to_skill_map(resume["skill_groups"])

    if _normalize_groups(resume.get("additional_categories")):
        # The model's additional_categories are the source of Core Competencies, all of them, in order.
        resume["competency_groups"] = _normalize_groups(resume["additional_categories"])
    elif isinstance(resume.get("competency_groups"), list):
        resume["competency_groups"] = _normalize_groups(resume["competency_groups"])

    experience = resume.get("experience")
    if isinstance(experience, list):
        normalized_jobs = []
        for job in experience:
            if not isinstance(job, dict):
                continue
            item = dict(job)
            if not clean_text(item.get("title")) and clean_text(item.get("role_title")):
                item["title"] = clean_text(item.get("role_title"))
            normalized_jobs.append(item)
        _apply_latest_job_end_date(normalized_jobs)
        resume["experience"] = normalized_jobs

    education = resume.get("education")
    if not isinstance(education, list) or not any(
        isinstance(item, dict) and _education_has_content(item) for item in education
    ):
        resume["education"] = _default_education(resume.get("experience"))
    else:
        # The graduation year always follows the same rule, whatever dates the JSON carries.
        year = clean_text(DEFAULT_EDUCATION[0].get("dates"))
        if not year:
            earliest = _earliest_experience_year(resume.get("experience"))
            year = str(earliest - 1) if earliest is not None else ""
        if year:
            resume["education"] = [dict(item, dates=year) if isinstance(item, dict) else item for item in education]

    return resume


def _default_education(experience) -> list[dict]:
    education = [dict(item) for item in DEFAULT_EDUCATION]
    # If Settings provided an explicit education year/dates, keep it.
    # Otherwise derive year as earliest job year - 1 (existing behavior).
    if not clean_text(education[0].get("dates")):
        earliest = _earliest_experience_year(experience)
        if earliest is not None:
            education[0]["dates"] = str(earliest - 1)
    return education


def _earliest_experience_year(experience) -> int | None:
    if not isinstance(experience, list):
        return None
    years: list[int] = []
    for job in experience:
        if not isinstance(job, dict):
            continue
        dates = clean_text(job.get("dates"))
        found = re.findall(r"\b(?:19|20)\d{2}\b", dates)
        if found:
            years.append(int(found[0]))
    return min(years) if years else None


def _apply_latest_job_end_date(jobs: list[dict]) -> None:
    latest = _latest_experience_job(jobs)
    if latest is None:
        return
    end_label = _months_ago_label(2)
    dates = clean_text(latest.get("dates"))
    parts = DATE_RANGE_SPLIT_RE.split(dates, maxsplit=1)
    if len(parts) == 2 and parts[0]:
        latest["dates"] = f"{parts[0]} - {end_label}"
    elif parts and parts[0]:
        latest["dates"] = f"{parts[0]} - {end_label}"
    else:
        latest["dates"] = end_label


def _latest_experience_job(jobs: list[dict]) -> dict | None:
    if not jobs:
        return None
    present = []
    scored = []
    for index, job in enumerate(jobs):
        dates = clean_text(job.get("dates"))
        if re.search(r"\b(present|current|now)\b", dates, re.IGNORECASE):
            present.append((index, job))
        start = _start_year_month(dates)
        scored.append((start or (0, 0), -index, job))
    if present:
        return present[0][1]
    scored.sort(reverse=True)
    return scored[0][2]


def _start_year_month(dates: str) -> tuple[int, int] | None:
    match = re.search(
        r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\s+((?:19|20)\d{2})\b",
        dates,
        re.IGNORECASE,
    )
    if match:
        month = {name.lower(): index for index, name in enumerate(MONTH_ABBREV) if name}.get(
            match.group(1)[:3].lower(), 1
        )
        return int(match.group(2)), month
    year = re.search(r"\b((?:19|20)\d{2})\b", dates)
    if year:
        return int(year.group(1)), 1
    return None


def _months_ago_label(months: int) -> str:
    today = date.today()
    month = today.month - months
    year = today.year
    while month <= 0:
        month += 12
        year -= 1
    return f"{MONTH_ABBREV[month]} {year}"


def suggested_stem(resume: dict) -> str:
    return resume_stem(resume)


def resume_stem(resume: dict) -> str:
    return f"{_person_stem(resume)}_Resume"


def cover_letter_stem(resume: dict) -> str:
    return f"{_person_stem(resume)}_Cover_Letter"


def _person_stem(resume: dict) -> str:
    return safe_stem(clean_text(resume.get("name")) or "Candidate")


def safe_stem(value: str) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", value).strip().rstrip(". ")
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned or "Resume"


def application_output_dir(resume: dict) -> Path:
    """E:\\Remote\\{Name}\\{Company}\\{Role_title}\\ on Windows."""
    name = _person_stem(resume)
    company = safe_stem(clean_text(resume.get("company_name")) or "Company")
    role = safe_stem(
        clean_text(resume.get("role_title"))
        or clean_text(resume.get("subtitle"))
        or "Role"
    )
    if sys.platform == "win32":
        root = Path("E:/Remote")
    else:
        root = Path.home() / "Remote"
    return root / name / company / role


def export_resume(
    resume: dict,
    docx_path: Path,
    pdf_path: Path | None,
    cover_letter_path: Path | None = None,
) -> ExportResult:
    resume = normalize_resume(resume)
    docx_path.parent.mkdir(parents=True, exist_ok=True)
    warnings = build_docx(resume, docx_path)
    converter = None
    if pdf_path is not None:
        pdf_path.parent.mkdir(parents=True, exist_ok=True)
        converter = build_pdf_file(resume, pdf_path)

    written_cover = None
    raw_cover = str(resume.get("cover_letter") or "").strip()
    if cover_letter_path is not None:
        if raw_cover:
            from cover_letter import build_cover_letter_docx

            written_cover = build_cover_letter_docx(resume, cover_letter_path)
        else:
            warnings.append("No cover_letter text was provided, so no cover letter file was written.")

    return ExportResult(docx_path, pdf_path, written_cover, warnings, converter)


def build_pdf_file(resume: dict, pdf_path: Path) -> str:
    """Write PDF with the bundled engine. No Word or LibreOffice required."""
    try:
        from pdf_engine import build_pdf
    except ImportError as exc:
        raise PdfExportError(
            "PDF export needs reportlab. In this folder run: py -3 -m pip install -r requirements.txt"
        ) from exc
    try:
        return build_pdf(resume, pdf_path)
    except Exception as exc:  # noqa: BLE001
        raise PdfExportError(f"Could not create the PDF. ({exc})") from exc


def build_docx(resume: dict, output_path: Path) -> list[str]:
    resume = normalize_resume(resume)
    document = Document()
    _configure_page(document)
    _add_header(document, resume)
    _add_summary(document, resume)
    _add_experience(document, resume)
    _add_education(document, resume)
    _add_skills(document, resume)
    _add_competencies(document, resume)
    _add_additional(document, resume)
    _drop_leading_empty_paragraph(document)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    return collect_warnings(resume)


def clean_text(value) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]", "", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", " ", text)
    return text.strip()


def collect_warnings(resume: dict) -> list[str]:
    resume = normalize_resume(resume)
    warnings = []
    visible = _visible_text(resume)
    if PLACEHOLDER_RE.search(visible):
        warnings.append(
            "Some fields still contain [Verify ...] or [Verified ...] placeholders. "
            "Replace those with real details before submitting this resume."
        )
    experience = resume.get("experience")
    if not isinstance(experience, list) or not experience:
        warnings.append("No work experience was included.")
    return warnings


def _visible_text(resume: dict) -> str:
    chunks: list[str] = []

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        else:
            chunks.append(clean_text(value))

    printed = {
        key: value
        for key, value in resume.items()
        if not (isinstance(key, str) and (key.startswith("_") or key in OMITTED_TOP_LEVEL))
    }
    walk(printed)
    return "\n".join(chunks)


def _configure_page(document: Document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(STYLES["body"].size)
    normal.font.color.rgb = BLACK
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    _set_rfonts(normal.element.get_or_add_rPr())

    _define_styles(document)

    section = document.sections[0]
    section.page_width = Inches(PAGE_WIDTH_IN)
    section.page_height = Inches(PAGE_HEIGHT_IN)
    section.top_margin = Inches(MARGIN_TOP_IN)
    section.bottom_margin = Inches(MARGIN_BOTTOM_IN)
    section.left_margin = Inches(MARGIN_SIDE_IN)
    section.right_margin = Inches(MARGIN_SIDE_IN)
    section.header_distance = _twips(708)
    section.footer_distance = _twips(708)

    sect_pr = section._sectPr
    grid = sect_pr.find(qn("w:docGrid"))
    if grid is None:
        grid = OxmlElement("w:docGrid")
        sect_pr.append(grid)
    grid.set(qn("w:linePitch"), "360")

    core = document.core_properties
    core.category = "Resume"


def _define_styles(document: Document) -> None:
    """Named paragraph styles, so headings, jobs and bullets are real structure and not just `Normal`."""
    for key, spec in STYLES.items():
        style = document.styles.add_style(spec.name, WD_STYLE_TYPE.PARAGRAPH)
        style.base_style = document.styles["List Bullet" if key == "bullet" else "Normal"]
        style.quick_style = True
        style.font.name = FONT
        style.font.size = Pt(spec.size)
        style.font.bold = spec.bold
        style.font.italic = False
        style.font.color.rgb = RGBColor.from_string(spec.color.upper())
        _set_rfonts(style.element.get_or_add_rPr())

        fmt = style.paragraph_format
        fmt.space_before = Pt(spec.before)
        fmt.space_after = Pt(0)
        fmt.line_spacing = Pt(spec.line)
        fmt.line_spacing_rule = WD_LINE_SPACING.AT_LEAST
        fmt.keep_with_next = spec.keep_next
        fmt.keep_together = spec.keep_lines
        fmt.widow_control = True
        fmt.left_indent = Pt(spec.left)
        fmt.first_line_indent = Pt(-spec.hanging) if spec.hanging else Pt(0)

        props = style.element.get_or_add_pPr()
        if key == "bullet":  # List Bullet turns this on, which would swallow the gap between bullets
            contextual = OxmlElement("w:contextualSpacing")
            contextual.set(qn("w:val"), "0")
            props.append(contextual)
        if key == "section":  # lets Word's navigation pane and screen readers see the headings
            outline = OxmlElement("w:outlineLvl")
            outline.set(qn("w:val"), "0")
            props.append(outline)


def _add_header(document: Document, resume: dict) -> None:
    name = clean_text(resume.get("name"))
    subtitle = clean_text(resume.get("subtitle"))
    document.core_properties.author = name or subtitle
    document.core_properties.title = f"{name or subtitle or 'Resume'} Resume"

    if name:
        document.add_paragraph(name, style=STYLES["name"].name)
    elif subtitle:
        document.add_paragraph(subtitle, style=STYLES["name"].name)
        subtitle = ""

    if subtitle:
        document.add_paragraph(subtitle, style=STYLES["subtitle"].name)

    contact = _contact_parts(resume)
    if contact:
        contact_paragraph = document.add_paragraph(style=STYLES["contact"].name)
        for index, (kind, value) in enumerate(contact):
            if index:
                _add_run(contact_paragraph, " | ")
            if kind == "email":
                _add_hyperlink(contact_paragraph, value, f"mailto:{value}")
            else:
                _add_run(contact_paragraph, value)


def _contact_parts(resume: dict) -> list[tuple[str, str]]:
    parts = []
    location = clean_text(resume.get("location"))
    email = clean_text(resume.get("email"))
    phone = clean_text(resume.get("phone"))
    if location:
        parts.append(("text", location))
    if email:
        parts.append(("email" if "@" in email else "text", email))
    if phone:
        parts.append(("text", phone))
    return parts


def _add_summary(document: Document, resume: dict) -> None:
    summary = clean_text(resume.get("summary"))
    if not summary:
        return
    _add_section_heading(document, "Professional Summary")
    paragraph = document.add_paragraph(style=STYLES["body"].name)
    _add_highlighted(paragraph, summary)


def _add_experience(document: Document, resume: dict) -> None:
    jobs = resume.get("experience")
    if not isinstance(jobs, list):
        return
    blocks = [job for job in jobs if isinstance(job, dict) and _job_has_content(job)]
    if not blocks:
        return
    _add_section_heading(document, "Work Experience")
    for index, job in enumerate(blocks):
        company = clean_text(job.get("company"))
        location = clean_text(job.get("location"))
        dates = clean_text(job.get("dates"))
        title = clean_text(job.get("title")) or clean_text(job.get("role_title"))
        role_description = clean_text(job.get("role_description"))
        bullets = _bullet_texts(job.get("bullets"))
        place = ", ".join(part for part in (company, location) if part)

        # Employer, title, role context and the first bullet stay on one page (keep-with-next in the styles);
        # the remaining bullets flow on. With no bullets, the last line must not drag the next job along.
        lead: list = []
        if place or dates:
            header = document.add_paragraph(style=STYLES["employer"].name)
            _set_right_tab(header)
            if place:
                _add_run(header, place)
            if dates:
                date_style = STYLES["dates"]
                _add_run(header, "\t", bold=False)
                _add_run(header, dates, size=date_style.size, bold=False,
                         color=RGBColor.from_string(date_style.color))
            lead.append(header)

        if title:
            lead.append(document.add_paragraph(title, style=STYLES["job_title"].name))

        if role_description:
            lead.append(document.add_paragraph(role_description, style=STYLES["role_context"].name))

        if lead and index == 0:
            lead[0].paragraph_format.space_before = Pt(FIRST_AFTER_HEADING)
        if lead and not bullets:
            lead[-1].paragraph_format.keep_with_next = False

        for bullet in bullets:
            paragraph = document.add_paragraph(style=STYLES["bullet"].name)
            _set_hanging_indent(paragraph)
            _add_highlighted(paragraph, bullet)


def _add_education(document: Document, resume: dict) -> None:
    items = resume.get("education")
    if not isinstance(items, list):
        return
    rows = [item for item in items if isinstance(item, dict) and _education_has_content(item)]
    if not rows:
        return
    _add_section_heading(document, "Education")
    for index, item in enumerate(rows):
        school = clean_text(item.get("school"))
        location = clean_text(item.get("location"))
        degree = clean_text(item.get("degree"))
        dates = clean_text(item.get("dates"))
        place = ", ".join(part for part in (school, location) if part)

        first = None
        if place:
            first = document.add_paragraph(style=STYLES["education"].name)
            _add_run(first, place, bold=True)
            if degree or dates:
                first.paragraph_format.keep_with_next = True
        if degree or dates:
            detail = document.add_paragraph(style=STYLES["education"].name)
            first = first or detail
            if place:
                detail.paragraph_format.space_before = Pt(0)
            if degree:
                _add_run(detail, degree, bold=True)
            if dates:
                if degree:
                    _add_run(detail, " | ")
                _add_run(detail, dates)
        if first is not None and index == 0:
            first.paragraph_format.space_before = Pt(FIRST_AFTER_HEADING)
        elif first is not None:
            first.paragraph_format.space_before = Pt(6)


def _add_skills(document: Document, resume: dict) -> None:
    lines = list(_category_lines(resume.get("skill_groups"))) or list(
        _skill_lines(resume.get("technicalskills"))
    )
    if not lines:
        return
    _add_section_heading(document, "Technical Skills")
    _add_label_lines(document, lines, "skills")


def competency_lines(resume: dict) -> list[tuple[str, str]]:
    """Core Competencies rows (label, items), in source order. A category with no items is skipped."""
    return [(label, value) for label, value in _category_lines(resume.get("competency_groups")) if value]


def _add_competencies(document: Document, resume: dict) -> None:
    lines = competency_lines(resume)
    if not lines:
        return
    _add_section_heading(document, "Core Competencies")
    _add_label_lines(document, lines, "competency")


def _add_additional(document: Document, resume: dict) -> None:
    lines = list(_additional_lines(resume.get("additionalsection")))
    if not lines:
        return
    _add_section_heading(document, "Additional Information")
    _add_label_lines(document, lines, "extra")


def _add_label_lines(document: Document, lines, style_key: str) -> None:
    for index, (label, value) in enumerate(lines):
        paragraph = document.add_paragraph(style=STYLES[style_key].name)
        if index == 0:
            paragraph.paragraph_format.space_before = Pt(FIRST_AFTER_HEADING)
        if label:
            _add_run(paragraph, f"{label}: ", bold=True)
        if value:
            _add_highlighted(paragraph, value)


def _add_section_heading(document: Document, text: str) -> None:
    document.add_paragraph(text, style=STYLES["section"].name)


def _skill_lines(skills):
    if isinstance(skills, dict):
        for label, value in skills.items():
            formatted = _format_skill_value(value)
            label_text = clean_text(label)
            if label_text or formatted:
                yield label_text, formatted
    elif isinstance(skills, list):
        for value in skills:
            if isinstance(value, dict) and ("category" in value or "items" in value):
                yield from _category_lines([value])
            elif isinstance(value, dict):
                yield from _skill_lines(value)
            else:
                formatted = _format_skill_value(value)
                if formatted:
                    yield "", formatted


def _category_lines(groups):
    for group in _normalize_groups(groups):
        label = clean_text(group.get("category"))
        value = _format_skill_value(group.get("items"))
        if label or value:
            yield label, value


def _normalize_groups(groups) -> list[dict]:
    if not isinstance(groups, list):
        return []
    rows = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        category = clean_text(group.get("category") or group.get("label") or group.get("name"))
        items = group.get("items")
        if items is None:
            items = group.get("skills") or group.get("values")
        formatted_items = []
        if isinstance(items, list):
            formatted_items = [clean_text(item) for item in items if clean_text(item)]
        elif clean_text(items):
            formatted_items = [part.strip() for part in clean_text(items).split(",") if part.strip()]
        if category or formatted_items:
            rows.append({"category": category, "items": formatted_items})
    return rows


def _groups_to_skill_map(groups) -> dict:
    mapping = {}
    for group in _normalize_groups(groups):
        label = clean_text(group.get("category")) or "Skills"
        mapping[label] = group.get("items") or []
    return mapping


def _format_skill_value(value) -> str:
    if isinstance(value, list):
        parts = [clean_text(item) for item in value]
    else:
        parts = [part.strip() for part in clean_text(value).split(",")]
    return " | ".join(part for part in parts if part)


def _additional_lines(section):
    if isinstance(section, dict):
        for label, value in section.items():
            label_text = clean_text(label)
            value_text = clean_text(value)
            if label_text or value_text:
                yield label_text, value_text
    elif isinstance(section, list):
        if section and isinstance(section[0], dict) and "items" in section[0]:
            yield from _category_lines(section)
            return
        for value in section:
            text = clean_text(value)
            if text:
                yield "", text


def _job_has_content(job: dict) -> bool:
    return any(
        (
            clean_text(job.get("company")),
            clean_text(job.get("title")),
            clean_text(job.get("role_title")),
            clean_text(job.get("dates")),
            clean_text(job.get("location")),
            clean_text(job.get("role_description")),
            _bullet_texts(job.get("bullets")),
        )
    )


def _education_has_content(item: dict) -> bool:
    return any(clean_text(item.get(key)) for key in ("degree", "school", "dates", "location"))


def _bullet_texts(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (clean_text(item) for item in value) if text]


def _add_highlighted(paragraph, text: str) -> None:
    for chunk, highlighted in _split_highlights(text):
        _add_run(paragraph, chunk, bold=True if highlighted else None)


def _split_highlights(text: str) -> list[tuple[str, bool]]:
    parts: list[tuple[str, bool]] = []
    position = 0
    for match in HIGHLIGHT_RE.finditer(text):
        if match.start() > position:
            parts.append((text[position:match.start()], False))
        parts.append((match.group(1), True))
        position = match.end()
    if position < len(text):
        parts.append((text[position:], False))
    return [(chunk, flag) for chunk, flag in parts if chunk] or [("", False)]


def _add_run(paragraph, text: str, size=None, bold=None, italic=None, color=None):
    """A run that inherits everything from its paragraph style unless told otherwise."""
    run = paragraph.add_run(text)
    if bold is not None:
        run.bold = bold
    if italic is not None:
        run.italic = italic
    if size is not None:
        run.font.size = Pt(size)
    if color is not None:
        run.font.color.rgb = color
    return run


def _add_hyperlink(paragraph, text: str, url: str) -> None:
    relation_id = paragraph.part.relate_to(url, RELATIONSHIP_TYPE.HYPERLINK, is_external=True)
    hyperlink = OxmlElement("w:hyperlink")
    hyperlink.set(qn("r:id"), relation_id)

    run = OxmlElement("w:r")
    run_props = OxmlElement("w:rPr")
    fonts = OxmlElement("w:rFonts")
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn(f"w:{attr}"), FONT)
    run_props.append(fonts)

    underline = OxmlElement("w:u")  # size and colour come from the paragraph's style
    underline.set(qn("w:val"), "none")
    run_props.append(underline)
    run.append(run_props)

    text_element = OxmlElement("w:t")
    text_element.set(qn("xml:space"), "preserve")
    text_element.text = text
    run.append(text_element)
    hyperlink.append(run)
    paragraph._p.append(hyperlink)


def _set_rfonts(run_props) -> None:
    fonts = run_props.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run_props.append(fonts)
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn(f"w:{attr}"), FONT)
    for attr in ("asciiTheme", "hAnsiTheme", "csTheme", "cstheme", "eastAsiaTheme"):
        qualified = qn(f"w:{attr}")
        if qualified in fonts.attrib:
            del fonts.attrib[qualified]


def _set_spacing(paragraph, before: int, after: int, line: int, rule: str = "atLeast") -> None:
    props = paragraph._p.get_or_add_pPr()
    spacing = props.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        props.append(spacing)
    spacing.set(qn("w:before"), str(before))
    spacing.set(qn("w:after"), str(after))
    spacing.set(qn("w:line"), str(line))
    spacing.set(qn("w:lineRule"), rule)


def _set_right_tab(paragraph, position: int = CONTENT_WIDTH_TWIPS) -> None:
    props = paragraph._p.get_or_add_pPr()
    tabs = props.find(qn("w:tabs"))
    if tabs is None:
        tabs = OxmlElement("w:tabs")
        props.append(tabs)
    tab = OxmlElement("w:tab")
    tab.set(qn("w:val"), "right")
    tab.set(qn("w:pos"), str(position))
    tabs.append(tab)


def _set_hanging_indent(paragraph, left: int = 270, hanging: int = 183) -> None:
    props = paragraph._p.get_or_add_pPr()
    indent = props.find(qn("w:ind"))
    if indent is None:
        indent = OxmlElement("w:ind")
        props.append(indent)
    indent.set(qn("w:left"), str(left))
    indent.set(qn("w:hanging"), str(hanging))


def _keep_with_next(paragraph) -> None:
    props = paragraph._p.get_or_add_pPr()
    if props.find(qn("w:keepNext")) is None:
        props.append(OxmlElement("w:keepNext"))


def _drop_leading_empty_paragraph(document: Document) -> None:
    body = document.element.body
    first = body[0]
    if first.tag != qn("w:p"):
        return
    texts = [node.text or "" for node in first.findall(".//" + qn("w:t"))]
    if not any(text.strip() for text in texts):
        body.remove(first)


def _twips(value: int):
    return Emu(value * 635)
