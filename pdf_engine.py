"""Build a PDF resume that mirrors the Word layout without Microsoft Word.

The type scale, spacing and margins come from `engine.STYLES`, the same table the Word file is built from,
so the two files share fonts and line heights and break pages by the same keep-together rules.
"""

from __future__ import annotations

import os
from pathlib import Path

from reportlab.lib.colors import HexColor
from reportlab.lib.enums import TA_RIGHT
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

from engine import (
    FIRST_AFTER_HEADING,
    MARGIN_BOTTOM_IN,
    MARGIN_SIDE_IN,
    MARGIN_TOP_IN,
    STYLES,
    TextStyle,
    _additional_lines,
    _bullet_texts,
    _category_lines,
    _education_has_content,
    _job_has_content,
    _skill_lines,
    _split_highlights,
    clean_text,
    competency_lines,
    normalize_resume,
)

FAMILY = "ResumeFont"
_FONT_FILES = {  # the Word file asks for Arial; embed the real thing when this computer has it
    "normal": ("arial.ttf", "Arial.ttf", "LiberationSans-Regular.ttf"),
    "bold": ("arialbd.ttf", "Arial Bold.ttf", "LiberationSans-Bold.ttf"),
    "italic": ("ariali.ttf", "Arial Italic.ttf", "LiberationSans-Italic.ttf"),
    "boldItalic": ("arialbi.ttf", "Arial Bold Italic.ttf", "LiberationSans-BoldItalic.ttf"),
}
_FONT_DIRS = (
    Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts",
    Path("/Library/Fonts"),
    Path("/System/Library/Fonts/Supplemental"),
    Path("/usr/share/fonts/truetype/msttcorefonts"),
    Path("/usr/share/fonts/truetype/liberation"),
    Path("/usr/share/fonts/truetype/liberation2"),
)
_registered: str | None = None


def _font_family() -> tuple[str, str]:
    """(regular, bold) font names. An embedded Arial family when found, else the built-in Helvetica."""
    global _registered
    if _registered is None:
        found = {}
        for slot, names in _FONT_FILES.items():
            for folder in _FONT_DIRS:
                match = next((folder / n for n in names if (folder / n).is_file()), None)
                if match:
                    found[slot] = match
                    break
        if len(found) == len(_FONT_FILES):
            try:
                for slot, path in found.items():
                    pdfmetrics.registerFont(TTFont(f"{FAMILY}-{slot}", str(path)))
                pdfmetrics.registerFontFamily(
                    FAMILY, normal=f"{FAMILY}-normal", bold=f"{FAMILY}-bold",
                    italic=f"{FAMILY}-italic", boldItalic=f"{FAMILY}-boldItalic",
                )
                _registered = FAMILY
            except Exception:  # noqa: BLE001 - an unreadable font file must not stop the export
                _registered = ""
        else:
            _registered = ""
    if _registered:
        return f"{FAMILY}-normal", f"{FAMILY}-bold"
    return "Helvetica", "Helvetica-Bold"


def build_pdf(resume: dict, output_path: Path) -> str:
    resume = normalize_resume(resume)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    side = MARGIN_SIDE_IN * inch
    content_width = letter[0] - 2 * side
    title_name = clean_text(resume.get("name")) or clean_text(resume.get("subtitle")) or "Resume"
    top, bottom = MARGIN_TOP_IN * inch, MARGIN_BOTTOM_IN * inch
    document = BaseDocTemplate(
        str(output_path),
        pagesize=letter,
        leftMargin=side,
        rightMargin=side,
        topMargin=top,
        bottomMargin=bottom,
        title=f"{title_name} Resume",
        author=title_name,
        subject="Resume",
        creator="Application Studio",
        lang="en-US",
    )
    # No padding inside the frame, so the text edge is exactly the margin, as in the Word file.
    frame = Frame(side, bottom, content_width, letter[1] - top - bottom, id="body",
                  leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)
    document.addPageTemplates([PageTemplate(id="resume", frames=[frame])])
    styles = _styles()
    story: list = []
    story.extend(_header_blocks(resume, styles))
    story.extend(_summary_blocks(resume, styles))
    story.extend(_experience_blocks(resume, styles, content_width))
    story.extend(_education_blocks(resume, styles))
    story.extend(_skill_blocks(resume, styles))
    story.extend(_competency_blocks(resume, styles))
    story.extend(_additional_blocks(resume, styles))
    document.build(story)
    return "bundled PDF engine"


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()["Normal"]
    regular, bold = _font_family()

    def make(spec: TextStyle, **extra) -> ParagraphStyle:
        return ParagraphStyle(
            spec.name,
            parent=base,
            fontName=bold if spec.bold else regular,
            fontSize=spec.size,
            leading=spec.line,
            textColor=HexColor(f"#{spec.color}"),
            spaceBefore=spec.before,
            spaceAfter=0,
            allowWidows=0,
            allowOrphans=0,
            **extra,
        )

    out = {key: make(spec) for key, spec in STYLES.items()}
    bullet = STYLES["bullet"]
    out["bullet"] = make(
        bullet,
        leftIndent=bullet.left,
        bulletIndent=bullet.left - bullet.hanging,
        bulletFontName=regular,
        bulletFontSize=bullet.size,
    )
    out["dates"] = make(STYLES["dates"], alignment=TA_RIGHT)
    out["employer_name"] = make(STYLES["employer"])
    out["employer_name"].spaceBefore = 0
    return out


def _header_blocks(resume: dict, styles: dict) -> list:
    blocks = []
    name = clean_text(resume.get("name"))
    subtitle = clean_text(resume.get("subtitle"))
    if name:
        blocks.append(Paragraph(_escape(name), styles["name"]))
        if subtitle:
            blocks.append(Paragraph(_escape(subtitle), styles["subtitle"]))
    elif subtitle:
        blocks.append(Paragraph(_escape(subtitle), styles["name"]))
    contact = _contact_line(resume)
    if contact:
        blocks.append(Paragraph(contact, styles["contact"]))
    return blocks


def _contact_line(resume: dict) -> str:
    parts = []
    location = clean_text(resume.get("location"))
    email = clean_text(resume.get("email"))
    phone = clean_text(resume.get("phone"))
    if location:
        parts.append(_escape(location))
    if email:
        if "@" in email:
            parts.append(f'<link href="mailto:{_escape(email)}">{_escape(email)}</link>')
        else:
            parts.append(_escape(email))
    if phone:
        parts.append(_escape(phone))
    return " | ".join(parts)


def _summary_blocks(resume: dict, styles: dict) -> list:
    summary = clean_text(resume.get("summary"))
    if not summary:
        return []
    return [
        KeepTogether([
            Paragraph("Professional Summary", styles["section"]),
            Paragraph(_rich(summary), styles["body"]),
        ])
    ]


def _experience_blocks(resume: dict, styles: dict, width: float) -> list:
    jobs = resume.get("experience")
    if not isinstance(jobs, list):
        return []
    blocks = []
    items = [job for job in jobs if isinstance(job, dict) and _job_has_content(job)]
    if not items:
        return []
    heading = Paragraph("Work Experience", styles["section"])
    for index, job in enumerate(items):
        company = clean_text(job.get("company"))
        location = clean_text(job.get("location"))
        dates = clean_text(job.get("dates"))
        title = clean_text(job.get("title")) or clean_text(job.get("role_title"))
        role_description = clean_text(job.get("role_description"))
        bullets = _bullet_texts(job.get("bullets"))
        place = ", ".join(part for part in (company, location) if part)

        # Employer, title, role context and the first bullet stay together; later bullets flow on.
        lead: list = [heading] if index == 0 else []
        lead.append(Spacer(1, FIRST_AFTER_HEADING if index == 0 else STYLES["employer"].before))
        if place or dates:
            table = Table(
                [[Paragraph(_escape(place), styles["employer_name"]), Paragraph(_escape(dates), styles["dates"])]],
                colWidths=[width * 0.68, width * 0.32],
            )
            table.setStyle(
                TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("TOPPADDING", (0, 0), (-1, -1), 0),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
                    ]
                )
            )
            lead.append(table)
        if title:
            lead.append(Paragraph(_escape(title), styles["job_title"]))
        if role_description:
            lead.append(Paragraph(_escape(role_description), styles["role_context"]))
        if bullets:
            lead.append(_bullet(bullets[0], styles))
        blocks.append(KeepTogether(lead))
        blocks.extend(KeepTogether([_bullet(text, styles)]) for text in bullets[1:])
    return blocks


def _bullet(text: str, styles: dict) -> Paragraph:
    return Paragraph(_rich(text), styles["bullet"], bulletText="\u2022")


def _education_blocks(resume: dict, styles: dict) -> list:
    items = resume.get("education")
    if not isinstance(items, list):
        return []
    rows = [item for item in items if isinstance(item, dict) and _education_has_content(item)]
    if not rows:
        return []
    blocks = []
    for index, item in enumerate(rows):
        school = clean_text(item.get("school"))
        location = clean_text(item.get("location"))
        degree = clean_text(item.get("degree"))
        dates = clean_text(item.get("dates"))
        place = ", ".join(part for part in (school, location) if part)
        group = [Paragraph("Education", styles["section"])] if index == 0 else []
        group.append(Spacer(1, FIRST_AFTER_HEADING if index == 0 else 6))
        if place:
            group.append(Paragraph(f"<b>{_escape(place)}</b>", _flat(styles["education"])))
        detail_parts = []
        if degree:
            detail_parts.append(f"<b>{_escape(degree)}</b>")
        if dates:
            detail_parts.append(_escape(dates))
        if detail_parts:
            group.append(Paragraph(" | ".join(detail_parts), _flat(styles["education"])))
        blocks.append(KeepTogether(group))
    return blocks


def _flat(style: ParagraphStyle) -> ParagraphStyle:
    """The same style without its space before (the spacer in front of the group supplies it)."""
    flat = ParagraphStyle(f"{style.name}Flat", parent=style)
    flat.spaceBefore = 0
    return flat


def _label_blocks(heading: str, lines: list, styles: dict, key: str) -> list:
    if not lines:
        return []
    blocks = []
    for index, (label, value) in enumerate(lines):
        text = f"<b>{_escape(label)}:</b> {_rich(value)}" if label else _rich(value)
        style = styles[key]
        if index == 0:
            style = ParagraphStyle(f"{style.name}First", parent=style, spaceBefore=FIRST_AFTER_HEADING)
        line = Paragraph(text, style)
        blocks.append(KeepTogether([Paragraph(heading, styles["section"]), line]) if index == 0 else line)
    return blocks


def _skill_blocks(resume: dict, styles: dict) -> list:
    lines = list(_category_lines(resume.get("skill_groups"))) or list(
        _skill_lines(resume.get("technicalskills"))
    )
    return _label_blocks("Technical Skills", lines, styles, "skills")


def _competency_blocks(resume: dict, styles: dict) -> list:
    return _label_blocks("Core Competencies", competency_lines(resume), styles, "competency")


def _additional_blocks(resume: dict, styles: dict) -> list:
    lines = list(_additional_lines(resume.get("additionalsection")))
    return _label_blocks("Additional Information", lines, styles, "extra")


def _rich(text: str) -> str:
    parts = []
    for chunk, highlighted in _split_highlights(text):
        escaped = _escape(chunk)
        parts.append(f"<b>{escaped}</b>" if highlighted else escaped)
    return "".join(parts)


def _escape(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
