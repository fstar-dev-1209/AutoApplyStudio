"""Build a cover letter Word file matching the Christopher Hosier letter layout."""

from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Emu, Pt, RGBColor

from engine import clean_text

FONT = "Calibri"
BLACK = RGBColor(0x00, 0x00, 0x00)


def build_cover_letter_docx(resume: dict, output_path: Path) -> Path:
    raw = str(resume.get("cover_letter") or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    if not raw:
        raise ValueError("No cover_letter text was provided.")

    name = clean_text(resume.get("name"))
    location = clean_text(resume.get("location"))
    phone = clean_text(resume.get("phone"))
    email = clean_text(resume.get("email"))
    company = clean_text(resume.get("company_name"))
    role = clean_text(resume.get("role_title")) or clean_text(resume.get("subtitle"))

    document = Document()
    _configure_page(document)
    document.core_properties.author = name
    document.core_properties.title = f"{name} Cover Letter"

    name_paragraph = document.add_paragraph()
    name_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_spacing(name_paragraph, after=60, line=252)
    _add_run(name_paragraph, name, size=16, bold=True)

    contact = "  |  ".join(part for part in (location, phone, email) if part)
    if contact:
        contact_paragraph = document.add_paragraph()
        _set_spacing(contact_paragraph, after=360)
        _add_run(contact_paragraph, contact, size=10)

    date_paragraph = document.add_paragraph()
    date_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_spacing(date_paragraph, after=240, line=252)
    _add_run(date_paragraph, _today_label(), size=10.5)

    hiring = document.add_paragraph()
    hiring.alignment = WD_ALIGN_PARAGRAPH.LEFT
    _set_spacing(hiring, after=0, line=252)
    _add_run(hiring, "Hiring Team", size=10.5)

    if company:
        company_paragraph = document.add_paragraph()
        company_paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_spacing(company_paragraph, after=240, line=252)
        _add_run(company_paragraph, company, size=10.5)

    if role:
        re_paragraph = document.add_paragraph()
        _set_spacing(re_paragraph, after=240)
        _add_run(re_paragraph, "Re: ", size=10.5, bold=True)
        _add_run(re_paragraph, role, size=10.5, bold=True)

    body_paragraphs = _letter_paragraphs(raw)
    has_signoff = any(re.match(r"(?i)^sincerely,?$", paragraph) for paragraph in body_paragraphs)
    for paragraph_text in body_paragraphs:
        paragraph = document.add_paragraph()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_spacing(paragraph, after=140, line=252)
        _add_run(paragraph, paragraph_text, size=10.5)

    if not has_signoff:
        sincere = document.add_paragraph()
        sincere.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_spacing(sincere, after=120, line=252)
        _add_run(sincere, "Sincerely,", size=10.5)

        signature = document.add_paragraph()
        signature.alignment = WD_ALIGN_PARAGRAPH.LEFT
        _set_spacing(signature, after=0, line=252)
        _add_run(signature, name, size=10.5)

    _drop_leading_empty_paragraph(document)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    document.save(output_path)
    return output_path


def _letter_paragraphs(text: str) -> list[str]:
    chunks = re.split(r"\n\s*\n", text.strip())
    paragraphs = []
    for chunk in chunks:
        paragraph = clean_text(chunk.replace("\n", " "))
        if paragraph:
            paragraphs.append(paragraph)
    return paragraphs


def _today_label() -> str:
    today = date.today()
    return f"{today.strftime('%B')} {today.day}, {today.year}"


def _configure_page(document: Document) -> None:
    normal = document.styles["Normal"]
    normal.font.name = FONT
    normal.font.size = Pt(10.5)
    normal.font.color.rgb = BLACK
    normal.paragraph_format.space_before = Pt(0)
    normal.paragraph_format.space_after = Pt(0)
    _set_rfonts(normal.element.get_or_add_rPr())

    section = document.sections[0]
    section.page_width = Emu(12240 * 635)
    section.page_height = Emu(15840 * 635)
    section.top_margin = Emu(1008 * 635)
    section.bottom_margin = Emu(1008 * 635)
    section.left_margin = Emu(1296 * 635)
    section.right_margin = Emu(1296 * 635)


def _add_run(paragraph, text: str, size: float, bold: bool = False):
    run = paragraph.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    run.font.color.rgb = BLACK
    run.font.name = FONT
    _set_rfonts(run._element.get_or_add_rPr())
    return run


def _set_rfonts(run_props) -> None:
    fonts = run_props.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        run_props.append(fonts)
    for attr in ("ascii", "hAnsi", "cs", "eastAsia"):
        fonts.set(qn(f"w:{attr}"), FONT)


def _set_spacing(paragraph, after: int = 0, before: int = 0, line: int | None = None) -> None:
    props = paragraph._p.get_or_add_pPr()
    spacing = props.find(qn("w:spacing"))
    if spacing is None:
        spacing = OxmlElement("w:spacing")
        props.append(spacing)
    spacing.set(qn("w:before"), str(before))
    spacing.set(qn("w:after"), str(after))
    if line is not None:
        spacing.set(qn("w:line"), str(line))
        spacing.set(qn("w:lineRule"), "auto")


def _drop_leading_empty_paragraph(document: Document) -> None:
    body = document.element.body
    first = body[0]
    if first.tag != qn("w:p"):
        return
    texts = [node.text or "" for node in first.findall(".//" + qn("w:t"))]
    if not any(text.strip() for text in texts):
        body.remove(first)
