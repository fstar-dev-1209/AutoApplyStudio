"""Resume export format: named Word styles, keep-together rules, exact dates, and Word/PDF agreement.

The PDF checks read real page text back out of the file, so they catch a heading left alone at the bottom of a
page or a job split from its first bullet. Word itself is not available to the tests; the Word checks look at
the properties Word obeys when it paginates (styles, keep-with-next, list numbering).
"""

from __future__ import annotations

import re
import zipfile
from collections import Counter

import docx
import pytest

import engine
import pdf_engine
from tests.resume_fixtures import FIXTURES

pypdf = pytest.importorskip("pypdf")

HEADINGS = {"Professional Summary", "Work Experience", "Education", "Technical Skills",
            "Core Competencies", "Additional Information"}
BULLET_GLYPHS = "•"


@pytest.fixture(params=sorted(FIXTURES), ids=sorted(FIXTURES))
def exported(request, tmp_path):
    resume = FIXTURES[request.param]
    result = engine.export_resume(resume, tmp_path / "r.docx", tmp_path / "r.pdf")
    return request.param, resume, result.docx_path, result.pdf_path


def _pdf_lines(path) -> list[list[str]]:
    reader = pypdf.PdfReader(str(path))
    return [[ln.strip() for ln in page.extract_text().splitlines() if ln.strip()] for page in reader.pages]


def _words(text: str) -> list[str]:
    return re.sub(f"[{BULLET_GLYPHS}]", " ", text).split()


def _docx_text(path) -> str:
    return "\n".join(p.text for p in docx.Document(str(path)).paragraphs)


# ----------------------------------------------------------------------------- Word structure


def test_every_paragraph_uses_a_named_resume_style(exported):
    _, _, docx_path, _ = exported
    styles = {p.style.name for p in docx.Document(str(docx_path)).paragraphs}
    assert "Normal" not in styles
    assert styles >= {"ResumeName", "ResumeSubtitle", "ResumeContact", "ResumeSection", "ResumeBody",
                      "ResumeEmployer", "ResumeJobTitle", "ResumeRoleContext", "ResumeBullet", "ResumeSkills",
                      "ResumeCompetency", "ResumeEducation"}


def test_word_keep_rules_are_set_on_the_styles(exported):
    _, _, docx_path, _ = exported
    styles = docx.Document(str(docx_path)).styles
    for key in ("section", "employer", "job_title", "role_context"):
        assert styles[engine.STYLES[key].name].paragraph_format.keep_with_next is True, key
    bullet = styles["ResumeBullet"].paragraph_format
    assert bullet.keep_together is True and bullet.widow_control is True
    assert styles["ResumeBullet"].font.italic is False and styles["ResumeJobTitle"].font.italic is False


def test_bullets_are_real_list_items_with_a_hanging_indent(exported):
    _, _, docx_path, _ = exported
    document = docx.Document(str(docx_path))
    bullets = [p for p in document.paragraphs if p.style.name == "ResumeBullet"]
    assert bullets
    numbering = document.styles["List Bullet"].element.pPr.numPr
    assert numbering is not None  # the list formatting the bullet style is built on
    for p in bullets:
        assert not p.text.lstrip().startswith(tuple(BULLET_GLYPHS + "-*"))  # no typed bullet character
        assert p.paragraph_format.first_line_indent < 0 < p.paragraph_format.left_indent


def test_dates_sit_on_a_right_tab_at_the_text_width_and_there_are_no_hard_page_breaks(exported):
    _, _, docx_path, _ = exported
    document = docx.Document(str(docx_path))
    for p in (p for p in document.paragraphs if p.style.name == "ResumeEmployer"):
        assert "\t" in p.text and "  " not in p.text
        stops = list(p.paragraph_format.tab_stops)
        assert len(stops) == 1 and stops[0].alignment.name == "RIGHT"
        assert stops[0].position.twips == engine.CONTENT_WIDTH_TWIPS
    xml = zipfile.ZipFile(docx_path).read("word/document.xml").decode()
    assert 'w:type="page"' not in xml and "pageBreakBefore" not in xml


def test_word_and_pdf_share_page_size_and_type_scale(exported):
    _, _, docx_path, pdf_path = exported
    section = docx.Document(str(docx_path)).sections[0]
    assert (section.page_width.inches, section.page_height.inches) == (8.5, 11.0)
    assert section.left_margin.inches == section.right_margin.inches == engine.MARGIN_SIDE_IN
    page = pypdf.PdfReader(str(pdf_path)).pages[0]
    assert (float(page.mediabox.width), float(page.mediabox.height)) == (612.0, 792.0)

    styles = docx.Document(str(docx_path)).styles
    pdf_styles = pdf_engine._styles()
    for key, spec in engine.STYLES.items():
        assert styles[spec.name].font.size.pt == spec.size == pdf_styles[key].fontSize
        assert styles[spec.name].paragraph_format.line_spacing.pt == spec.line == pdf_styles[key].leading


def test_the_design_is_plain_white_and_readable(exported):
    _, _, docx_path, _ = exported
    document = docx.Document(str(docx_path))
    assert not document.tables  # no bars, cards or grids
    xml = zipfile.ZipFile(docx_path).read("word/document.xml").decode()
    assert "w:shd" not in xml and "<w:drawing" not in xml and "txbxContent" not in xml
    for key in ("body", "bullet", "employer", "role_context", "job_title", "skills", "competency"):
        assert 9.8 <= engine.STYLES[key].size <= 10.5, key  # no emergency small type
    for key, spec in engine.STYLES.items():
        assert spec.size * 2 == int(spec.size * 2), key  # Word rounds to half-points; the PDF must not differ
    assert engine.STYLES["name"].size == 20 and engine.STYLES["name"].color == "172B4D"
    assert (engine.MARGIN_SIDE_IN, engine.MARGIN_TOP_IN) == (0.70, 0.62)


# ----------------------------------------------------------------------------- core competencies


def test_every_core_competency_appears_once_in_order_in_both_files(exported):
    _, resume, docx_path, pdf_path = exported
    expected = [(g["category"], " | ".join(g["items"])) for g in resume["additional_categories"]]
    doc_paragraphs = docx.Document(str(docx_path)).paragraphs
    rows = [p.text for p in doc_paragraphs if p.style.name == "ResumeCompetency"]
    assert rows == [f"{label}: {items}" for label, items in expected]
    heading = [p.text for p in doc_paragraphs if p.style.name == "ResumeSection"]
    assert heading.index("Core Competencies") == heading.index("Technical Skills") + 1  # after Skills, own section
    pdf = "\n".join("\n".join(page) for page in _pdf_lines(pdf_path))
    assert pdf.count("Core Competencies") == 1
    for label, items in expected:
        assert pdf.count(f"{label}: {items}") == 1


def test_core_competencies_rules_for_odd_input():
    base = {"name": "A", "experience": []}
    only_labels = engine.normalize_resume({**base, "additional_categories": [
        {"category": "Empty", "items": []}, {"category": "Kept", "items": ["One", "Two"]}]})
    assert engine.competency_lines(only_labels) == [("Kept", "One | Two")]  # a category with no items is skipped
    assert engine.competency_lines(engine.normalize_resume({**base, "additional_categories": []})) == []
    # additional_categories is the source, even if an older competency_groups key is also present
    both = engine.normalize_resume({**base, "competency_groups": [{"category": "Old", "items": ["x"]}],
                                    "additional_categories": [{"category": "New", "items": ["y"]}]})
    assert engine.competency_lines(both) == [("New", "y")]


# ----------------------------------------------------------------------------- dates


def test_dates_are_rendered_as_supplied_except_the_latest_jobs_end(exported):
    _, resume, docx_path, pdf_path = exported
    expected = engine.normalize_resume(resume)["experience"]
    docx_text, pdf_text = _docx_text(docx_path), " ".join(" ".join(p) for p in _pdf_lines(pdf_path))
    for original, shown in zip(resume["experience"], expected, strict=True):
        if original is not resume["experience"][0]:
            assert shown["dates"] == original["dates"]  # older jobs are untouched
        assert shown["dates"] in docx_text.replace("\t", " ")
        assert shown["dates"] in pdf_text
    # The current job keeps its start and ends two months back (the app's standing rule).
    assert expected[0]["dates"] == f"Jun 2023 - {engine._months_ago_label(2)}"


def test_normalising_twice_changes_nothing_and_the_input_is_not_modified():
    resume = FIXTURES["18 bullets"]
    before = repr(resume)
    once = engine.normalize_resume(resume)
    assert engine.normalize_resume(once) == once
    assert repr(resume) == before


# ----------------------------------------------------------------------------- Word / PDF agreement


def test_docx_and_pdf_contain_the_same_words_in_the_same_order(exported):
    _, _, docx_path, pdf_path = exported
    word_words = _words(_docx_text(docx_path))
    pdf_words = _words("\n".join("\n".join(page) for page in _pdf_lines(pdf_path)))
    assert Counter(word_words) == Counter(pdf_words)
    assert word_words == pdf_words  # natural reading order


def test_no_resume_content_is_dropped_or_changed(exported):
    _, resume, _, pdf_path = exported
    text = " ".join(_words("\n".join("\n".join(p) for p in _pdf_lines(pdf_path))))
    for job in resume["experience"]:
        for bullet in job["bullets"]:
            assert " ".join(bullet.split()) in text
        assert job["company"] in text and job["title"] in text
    for group in resume["skill_groups"]:
        assert group["category"] in text and " | ".join(group["items"]) in text


# ----------------------------------------------------------------------------- pagination (PDF)


def test_no_heading_or_job_is_stranded_at_the_bottom_of_a_page(exported):
    _, resume, _, pdf_path = exported
    companies = [job["company"] for job in resume["experience"]]
    for number, lines in enumerate(_pdf_lines(pdf_path), start=1):
        assert lines[-1] not in HEADINGS, f"page {number} ends with a heading"
        for index, line in enumerate(lines):
            if any(line.startswith(c) for c in companies):
                following = lines[index + 1:index + 6]
                assert any(f.startswith(tuple(BULLET_GLYPHS)) for f in following), (
                    f"page {number}: {line!r} is not followed by its first bullet on the same page")


def test_page_count_and_no_third_page_sliver(exported):
    name, _, _, pdf_path = exported
    pages = _pdf_lines(pdf_path)
    assert len(pages) <= 2, name  # senior resumes of this size must fit two pages without shrinking type
    if len(pages) > 2:
        assert len(pages[-1]) >= 6, "the last page holds only a sliver of content"


# ----------------------------------------------------------------------------- PDF details


def test_pdf_metadata_links_and_embedded_font(exported):
    _, _, docx_path, pdf_path = exported
    reader = pypdf.PdfReader(str(pdf_path))
    assert reader.metadata.title == "Test Person Resume" and reader.metadata.author == "Test Person"
    links = [a.get_object().get("/A", {}).get("/URI") for p in reader.pages for a in (p.get("/Annots") or [])]
    assert "mailto:test.person@example.com" in links
    xml = zipfile.ZipFile(docx_path).read("word/_rels/document.xml.rels").decode()
    assert "mailto:test.person@example.com" in xml

    if pdf_engine._font_family()[0] != "Helvetica":  # only where an Arial/Liberation Sans file exists
        fonts = {f.get_object()["/BaseFont"] for p in reader.pages for f in p["/Resources"]["/Font"].values()}
        assert any(str(f).startswith("/") and "+" in str(f) for f in fonts)  # an embedded subset


# ----------------------------------------------------------------------------- app preview


def test_preview_reads_formatting_from_styles(exported, qapp):
    from ui import doc_preview

    _, _, docx_path, _ = exported
    document = docx.Document(str(docx_path))
    bullet = next(p for p in document.paragraphs if p.style.name == "ResumeBullet")
    section = next(p for p in document.paragraphs if p.style.name == "ResumeSection")
    assert doc_preview._is_list_item(bullet) and not doc_preview._is_list_item(section)
    block = doc_preview._block_format(bullet)
    assert block.leftMargin() > 0 and block.textIndent() < 0 and block.topMargin() > 0

    base = doc_preview.QTextCharFormat()
    heading = doc_preview._run_format(section.runs[0], base, section)
    assert heading.fontWeight() >= 700 and heading.foreground().color().name() == "#203b60"
    assert heading.property(heading.Property.FontPixelSize) > doc_preview._run_format(
        bullet.runs[0], base, bullet).property(heading.Property.FontPixelSize)
