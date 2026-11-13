"""Render the first pages of a .docx / .pdf as images, for the Generated Files gallery and preview pane.

PDFs go through QtPdf. DOCX files are laid out with QTextDocument from their paragraphs, runs and tables, which is
close enough for the documents this app writes (headings, bold/italic runs, bullets, right tabs, a shaded accent bar).
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import (
    QTextFormat,
    QColor,
    QFont,
    QImage,
    QPainter,
    QPixmap,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
    QTextLength,
    QTextOption,
    QTextTableCellFormat,
    QTextTableFormat,
)

SUPER = 4  # lay out at 4x, so fractional point sizes survive Qt's integer pixel sizes
PX_PER_PT = 96 / 72 * SUPER
DEFAULT_PAGE = (8.5 * 96 * SUPER, 11 * 96 * SUPER)
_cache: dict[tuple, list[QPixmap]] = {}


def render_pages(path: str | Path, width: int, max_pages: int = 2) -> list[QPixmap]:
    """Up to ``max_pages`` page images, each ``width`` px wide (before device pixel ratio). [] if unreadable."""
    p = Path(path)
    try:
        st = p.stat()
    except OSError:
        return []
    key = (str(p), st.st_mtime_ns, st.st_size, width, max_pages)
    if key not in _cache:
        suffix = p.suffix.lower()
        try:
            pages = _pdf_pages(p, width, max_pages) if suffix == ".pdf" else (
                _docx_pages(p, width, max_pages) if suffix == ".docx" else [])
        except Exception:  # a corrupt or unusual file must never break the page
            pages = []
        if len(_cache) > 120:
            _cache.clear()
        _cache[key] = pages
    return _cache[key]


def first_page(path: str | Path, width: int) -> QPixmap | None:
    pages = render_pages(path, width, 1)
    return pages[0] if pages else None


def _blank(w: int, h: int) -> QImage:
    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(QColor("white"))
    return img


# ------------------------------------------------------------------------------ pdf


def _pdf_pages(path: Path, width: int, max_pages: int) -> list[QPixmap]:
    from PySide6.QtCore import QSize
    from PySide6.QtPdf import QPdfDocument

    doc = QPdfDocument()
    if doc.load(str(path)) != QPdfDocument.Error.None_:
        return []
    out = []
    for i in range(min(doc.pageCount(), max_pages)):
        size = doc.pagePointSize(i)
        if size.width() <= 0:
            continue
        h = max(1, round(width * size.height() / size.width()))
        img = doc.render(i, QSize(width, h))
        out.append(QPixmap.fromImage(img))
    doc.close()
    return out


# ----------------------------------------------------------------------------- docx


def _docx_pages(path: Path, width: int, max_pages: int) -> list[QPixmap]:
    import docx

    document = docx.Document(str(path))
    sec = document.sections[0]
    page_w = sec.page_width.pt * PX_PER_PT if sec.page_width else DEFAULT_PAGE[0]
    page_h = sec.page_height.pt * PX_PER_PT if sec.page_height else DEFAULT_PAGE[1]
    lm, rm = (sec.left_margin.pt * PX_PER_PT if sec.left_margin else 72 * SUPER), (
        sec.right_margin.pt * PX_PER_PT if sec.right_margin else 72 * SUPER)
    tm, bm = (sec.top_margin.pt * PX_PER_PT if sec.top_margin else 72 * SUPER), (
        sec.bottom_margin.pt * PX_PER_PT if sec.bottom_margin else 72 * SUPER)
    content_w, content_h = page_w - lm - rm, page_h - tm - bm

    normal = document.styles["Normal"].font
    base = QTextCharFormat()
    base.setFontFamilies([normal.name or "Arial", "Arial", "Segoe UI"])
    _set_px(base, (normal.size.pt if normal.size else 11) * PX_PER_PT)

    td = QTextDocument()
    td.setDocumentMargin(0)
    td.setUseDesignMetrics(True)
    default = QFont()
    default.setHintingPreference(QFont.HintingPreference.PreferNoHinting)  # we draw scaled down
    td.setDefaultFont(default)
    td.setPageSize(QSizeF(content_w, content_h))
    cur = QTextCursor(td)
    fresh = [True]  # the cursor sits in an empty, unused block (document start, or just after a table)

    def add_paragraph(par) -> None:
        if not fresh[0]:
            cur.insertBlock()
        fresh[0] = False
        cur.setBlockFormat(_block_format(par))
        cur.setBlockCharFormat(base)
        if _is_list_item(par):  # a real Word bullet has no text of its own, so draw its glyph
            cur.insertText("• ", _style_format(par, base))
        for item in par.iter_inner_content():
            for run in (item.runs if hasattr(item, "runs") else [item]):
                if run.text:
                    cur.insertText(run.text, _run_format(run, base, par))
        tabs = _tab_stops(par, content_w)
        if tabs:
            fmt = cur.blockFormat()
            fmt.setTabPositions(tabs)
            cur.setBlockFormat(fmt)

    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for child in document.element.body.iterchildren():
        tag = child.tag.rsplit("}", 1)[-1]
        if tag == "p":
            add_paragraph(Paragraph(child, document))
        elif tag == "tbl":
            if not fresh[0]:
                cur.insertBlock()
            _add_table(cur, Table(child, document), base)
            fresh[0] = True

    pages = max(1, td.pageCount())
    scale = width / page_w
    out: list[QPixmap] = []
    for i in range(min(pages, max_pages)):
        img = _blank(width, max(1, round(page_h * scale)))
        painter = QPainter(img)
        painter.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing
                               | QPainter.RenderHint.SmoothPixmapTransform)
        painter.scale(scale, scale)
        painter.translate(lm, tm)
        clip = QRectF(0, 0, content_w, content_h)
        painter.setClipRect(clip.adjusted(-lm, -tm, rm, bm))
        painter.translate(0, -i * content_h)
        painter.setClipRect(QRectF(-lm, i * content_h - tm, page_w, page_h))
        td.drawContents(painter)
        painter.end()
        out.append(QPixmap.fromImage(img))
    return out


def _set_px(fmt: QTextCharFormat, px: float) -> None:
    fmt.setProperty(QTextFormat.Property.FontPixelSize, max(1, round(px)))


def _pt(length) -> float:
    return length.pt * PX_PER_PT if length is not None else 0.0


def _styles(par):
    """The paragraph's own style, then the styles it is based on."""
    style = par.style
    while style is not None:
        yield style
        style = style.base_style


def _inherited(par, read):
    """A paragraph setting: written on the paragraph itself, else taken from its style chain."""
    value = read(par.paragraph_format)
    for style in _styles(par):
        if value is not None:
            break
        value = read(style.paragraph_format)
    return value


def _is_list_item(par) -> bool:
    ppr = par._p.pPr
    if ppr is not None and ppr.numPr is not None:
        return True
    return any(s.element.pPr is not None and s.element.pPr.numPr is not None for s in _styles(par))


def _block_format(par) -> QTextBlockFormat:
    fmt = QTextBlockFormat()
    align = par.paragraph_format.alignment if par.paragraph_format.alignment is not None else par.alignment
    fmt.setAlignment({1: Qt.AlignmentFlag.AlignHCenter, 2: Qt.AlignmentFlag.AlignRight,
                      3: Qt.AlignmentFlag.AlignJustify}.get(int(align) if align is not None else 0,
                                                            Qt.AlignmentFlag.AlignLeft))
    fmt.setTopMargin(_pt(_inherited(par, lambda f: f.space_before)))
    fmt.setBottomMargin(_pt(_inherited(par, lambda f: f.space_after)))
    left = _pt(_inherited(par, lambda f: f.left_indent))
    first = _pt(_inherited(par, lambda f: f.first_line_indent))  # negative for a hanging indent
    fmt.setLeftMargin(left)
    fmt.setTextIndent(first)
    ls = _inherited(par, lambda f: f.line_spacing)
    if ls is not None:
        if hasattr(ls, "pt"):  # exact / at-least height
            height = ls.pt * PX_PER_PT
            fmt.setLineHeight(height, QTextBlockFormat.LineHeightTypes.MinimumHeight.value)
        else:
            fmt.setLineHeight(max(ls, 1.0) * 100, QTextBlockFormat.LineHeightTypes.ProportionalHeight.value)
    return fmt


def _tab_stops(par, content_w: float) -> list:
    out = []
    try:
        for stop in par.paragraph_format.tab_stops:
            tab = QTextOption.Tab()
            tab.position = min(_pt(stop.position), content_w)
            tab.type = {"RIGHT": QTextOption.TabType.RightTab, "CENTER": QTextOption.TabType.CenterTab}.get(
                stop.alignment.name, QTextOption.TabType.LeftTab)
            out.append(tab)
    except Exception:
        return []
    return out


def _font_value(par, run, read):
    """A character setting: written on the run, else from the paragraph style chain."""
    value = read(run.font) if run is not None else None
    for style in _styles(par):
        if value is not None:
            break
        value = read(style.font)
    return value


def _color(font) -> str | None:
    try:
        return str(font.color.rgb) if font.color and font.color.type is not None else None
    except Exception:
        return None


def _run_format(run, base: QTextCharFormat, par=None) -> QTextCharFormat:
    return _style_format(par if par is not None else run._parent, base, run)


def _style_format(par, base: QTextCharFormat, run=None) -> QTextCharFormat:
    fmt = QTextCharFormat(base)
    name = _font_value(par, run, lambda f: f.name)
    if name:
        fmt.setFontFamilies([name, "Arial", "Segoe UI"])
    size = _font_value(par, run, lambda f: f.size)
    if size:
        _set_px(fmt, size.pt * PX_PER_PT)
    if _font_value(par, run, lambda f: f.bold):
        fmt.setFontWeight(QFont.Weight.Bold)
    if _font_value(par, run, lambda f: f.italic):
        fmt.setFontItalic(True)
    if run is not None and run.font.underline:
        fmt.setFontUnderline(True)
    rgb = _font_value(par, run, _color)
    fmt.setForeground(QColor(f"#{rgb}") if rgb else QColor("black"))
    return fmt


def _add_table(cur: QTextCursor, table, base: QTextCharFormat) -> None:
    rows, cols = len(table.rows), len(table.columns)
    if not rows or not cols:
        return
    tf = QTextTableFormat()
    tf.setBorder(0)
    tf.setCellSpacing(0)
    tf.setCellPadding(0)
    tf.setWidth(QTextLength(QTextLength.Type.PercentageLength, 100))
    qt = cur.insertTable(rows, cols, tf)
    for r, row in enumerate(table.rows):
        for c, cell in enumerate(row.cells[:cols]):
            qcell = qt.cellAt(r, c)
            cf = QTextTableCellFormat()
            shade = cell._tc.tcPr.find(
                "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}shd") if cell._tc.tcPr is not None else None
            fill = shade.get("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}fill") if shade is not None else None
            if fill and fill != "auto":
                cf.setBackground(QColor(f"#{fill}"))
            qcell.setFormat(cf)
            ccur = qcell.firstCursorPosition()
            text = "\n".join(p.text for p in cell.paragraphs).strip()
            size = 1
            for p in cell.paragraphs:
                for run in p.runs:
                    if run.font.size:
                        size = max(1, run.font.size.pt)
            ch = QTextCharFormat(base)
            _set_px(ch, size * PX_PER_PT)
            bf = QTextBlockFormat()
            bf.setLineHeight(max(3.0, size * PX_PER_PT), QTextBlockFormat.LineHeightTypes.FixedHeight.value)
            ccur.setBlockFormat(bf)
            ccur.insertText(text or " ", ch)
    cur.setPosition(qt.lastPosition() + 1)  # the empty block after the table
