"""Application Studio visual system: dark navy base, restrained blue accent, compact tables."""

from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtGui import QColor, QFont, QPalette
from PySide6.QtWidgets import QApplication


@dataclass(frozen=True)
class ThemeColors:
    name: str
    app_bg: str
    sidebar: str
    sidebar_text: str
    sidebar_muted: str
    sidebar_hover: str
    sidebar_active: str
    panel: str
    panel_alt: str
    hover: str
    border: str
    border_soft: str
    text: str
    text_secondary: str
    accent: str
    accent_hover: str
    accent_soft: str
    accent_text: str
    success: str
    success_soft: str
    warning: str
    warning_soft: str
    danger: str
    danger_soft: str
    input_bg: str
    table_alt: str
    # categorical chart colours (validated for contrast on both themes)
    series: tuple[str, ...] = ()

    @property
    def is_light(self) -> bool:
        return self.name == "light"


LIGHT = ThemeColors(
    name="light",
    app_bg="#F1F4F9",
    sidebar="#0F1829",
    sidebar_text="#F8FAFC",
    sidebar_muted="#9AA8BD",
    sidebar_hover="rgba(255,255,255,0.07)",
    sidebar_active="rgba(59,130,246,0.30)",
    panel="#FFFFFF",
    panel_alt="#F8FAFC",
    hover="#F1F5F9",
    border="#DDE3EC",
    border_soft="#EAEFF5",
    text="#0F172A",
    text_secondary="#5B6B80",
    accent="#2563EB",
    accent_hover="#1D4ED8",
    accent_soft="#DBEAFE",
    accent_text="#FFFFFF",
    success="#15803D",
    success_soft="#DCFCE7",
    warning="#B45309",
    warning_soft="#FEF3C7",
    danger="#B91C1C",
    danger_soft="#FEE2E2",
    input_bg="#FFFFFF",
    table_alt="#F8FAFC",
    series=("#2563EB", "#7C3AED", "#0D9488", "#D97706", "#DC2626"),
)

DARK = ThemeColors(
    name="dark",
    app_bg="#0A0F1B",
    sidebar="#0C1220",
    sidebar_text="#E8EEF8",
    sidebar_muted="#8190A8",
    sidebar_hover="rgba(148,163,184,0.09)",
    sidebar_active="rgba(37,99,235,0.28)",
    panel="#0F1729",
    panel_alt="#0B1322",
    hover="#16223A",
    border="#1C2A44",
    border_soft="#15203A",
    text="#E8EEF9",
    text_secondary="#8A99B3",
    accent="#2F7BFF",
    accent_hover="#5391FF",
    accent_soft="#13284F",
    accent_text="#FFFFFF",
    success="#34D07B",
    success_soft="rgba(52,208,123,0.14)",
    warning="#F5B73B",
    warning_soft="rgba(245,183,59,0.14)",
    danger="#F26D6D",
    danger_soft="rgba(242,109,109,0.15)",
    input_bg="#0B1424",
    table_alt="#0D1626",
    series=("#4C86F5", "#A78BFA", "#2DD4BF", "#F5B73B", "#F26D6D"),
)

_current = DARK
_density = "comfortable"


def current() -> ThemeColors:
    return _current


def density() -> str:
    return _density


def row_height() -> int:
    return 28 if _density == "compact" else 32


def resolve_theme(preference: str, app: QApplication | None = None) -> ThemeColors:
    pref = (preference or "dark").lower()
    if pref == "dark":
        return DARK
    if pref == "light":
        return LIGHT
    if app is None:
        app = QApplication.instance()
    if app is not None:
        try:
            from PySide6.QtCore import Qt

            if app.styleHints().colorScheme() == Qt.ColorScheme.Light:
                return LIGHT
        except Exception:  # noqa: BLE001
            pass
    return DARK


_FONT_FAMILIES = ["Segoe UI Variable", "Segoe UI", "Ubuntu Sans", "Ubuntu", "Noto Sans", "Inter"]


def _pick_ui_font() -> QFont:
    from PySide6.QtGui import QFontDatabase

    available = set(QFontDatabase.families())
    for family in _FONT_FAMILIES:
        if family in available:
            font = QFont(family)
            font.setStyleHint(QFont.StyleHint.SansSerif)
            font.setPointSize(10)
            font.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
            return font
    font = QFont()
    font.setPointSize(10)
    return font


def title_font(point_size: int = 22, bold: bool = True) -> QFont:
    font = QFont(_pick_ui_font())
    font.setPointSize(point_size)
    font.setWeight(QFont.Weight.Bold if bold else QFont.Weight.DemiBold)
    return font


def apply_app_font(app: QApplication) -> None:
    app.setFont(_pick_ui_font())


def apply_theme(app: QApplication, colors: ThemeColors, density_name: str = "comfortable") -> None:
    global _current, _density
    _current = colors
    _density = density_name if density_name in ("comfortable", "compact") else "comfortable"
    from ui import icons

    icons.clear_cache()
    apply_app_font(app)
    palette = QPalette()
    for role, value in (
        (QPalette.ColorRole.Window, colors.app_bg),
        (QPalette.ColorRole.WindowText, colors.text),
        (QPalette.ColorRole.Base, colors.input_bg),
        (QPalette.ColorRole.AlternateBase, colors.table_alt),
        (QPalette.ColorRole.Text, colors.text),
        (QPalette.ColorRole.Button, colors.panel),
        (QPalette.ColorRole.ButtonText, colors.text),
        (QPalette.ColorRole.Highlight, colors.accent),
        (QPalette.ColorRole.HighlightedText, colors.accent_text),
        (QPalette.ColorRole.ToolTipBase, colors.panel),
        (QPalette.ColorRole.ToolTipText, colors.text),
        (QPalette.ColorRole.PlaceholderText, colors.text_secondary),
    ):
        palette.setColor(role, QColor(value))
    app.setPalette(palette)
    app.setStyleSheet(_stylesheet(colors))


def _dim(hex_color: str, alpha: float) -> str:
    """``#RRGGBB`` -> ``rgba(r,g,b,alpha)`` (QSS has no opacity property)."""
    h = hex_color.lstrip("#")
    return f"rgba({int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)},{alpha})"


def _icon_file(name: str, color: str, size: int = 14) -> str:
    """Render a line icon to a temp PNG so QSS (``image: url()``) can use it."""
    import tempfile
    from pathlib import Path

    from ui import icons

    path = Path(tempfile.gettempdir()) / f"application_studio_{name}_{color.lstrip('#')}_{size}.png"
    try:
        if not path.exists():
            icons.icon(name, color, size).pixmap(size, size).save(str(path), "PNG")
    except Exception:  # noqa: BLE001
        pass
    return path.as_posix()


def _check_png() -> str:
    return _icon_file("check", "#FFFFFF", 14)


def _stylesheet(c: ThemeColors) -> str:
    pad_cell = "4px 10px" if _density == "compact" else "7px 10px"
    chev = _icon_file("chevron", c.text_secondary, 12)
    chev_up = _icon_file("chevron-up", c.text_secondary, 10)
    return f"""
    * {{ font-family: "Segoe UI Variable", "Segoe UI", "Ubuntu Sans", "Ubuntu", "Noto Sans", sans-serif; }}
    QMainWindow, QDialog {{ background: {c.app_bg}; color: {c.text}; font-size: 13px; }}
    QWidget {{ color: {c.text}; }}
    QToolTip {{ background: {c.panel}; color: {c.text}; border: 1px solid {c.border}; border-radius: 6px;
        padding: 5px 8px; font-size: 12px; }}

    /* sidebar */
    QWidget#Sidebar {{ background: {c.sidebar}; border-right: 1px solid {c.border_soft}; }}
    QLabel#BrandTitle {{ color: {c.sidebar_text}; font-size: 14px; font-weight: 700; }}
    QLabel#BrandSub {{ color: {c.sidebar_muted}; font-size: 10.5px; }}
    QPushButton#NavButton {{ background: transparent; color: {c.sidebar_muted}; border: none; border-radius: 8px;
        text-align: left; padding: 9px 12px; font-size: 13px; font-weight: 550; }}
    QPushButton#NavButton:hover {{ background: {c.sidebar_hover}; color: {c.sidebar_text}; }}
    QPushButton#NavButton[active="true"] {{ background: {c.sidebar_active}; color: {c.sidebar_text}; font-weight: 650;
        border-left: 3px solid {c.accent}; padding-left: 9px; }}
    QFrame#Logo {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 #3B8BFF, stop:1 #1F5BDB); border-radius: 9px; }}
    QFrame#StatusCard {{ background: rgba(255,255,255,0.045); border: 1px solid rgba(255,255,255,0.08);
        border-radius: 12px; }}
    QLabel#StatusDot {{ background: {c.success}; border-radius: 4px; min-width: 8px; max-width: 8px;
        min-height: 8px; max-height: 8px; }}
    QLabel#StatusDotOff {{ background: {c.warning}; border-radius: 4px; min-width: 8px; max-width: 8px;
        min-height: 8px; max-height: 8px; }}
    QFrame#Hero {{ background: qlineargradient(x1:0,y1:0,x2:1,y2:1, stop:0 {c.panel}, stop:1 {c.panel_alt});
        border: 1px solid {c.border}; border-radius: 12px; }}
    QFrame#Avatar {{ border-radius: 8px; }}
    QLabel#Eyebrow {{ color: {c.text_secondary}; font-size: 11px; font-weight: 700; letter-spacing: 1px; }}
    QLabel#NavCount {{ background: {c.accent}; color: white; border-radius: 9px; padding: 1px 7px;
        font-size: 11px; font-weight: 700; }}
    QLabel#SidebarMeta {{ color: {c.sidebar_muted}; font-size: 11px; padding: 0; }}
    QLabel#SidebarValue {{ color: {c.sidebar_text}; font-size: 12px; font-weight: 600; }}
    QFrame#SidebarFooter {{ background: transparent; border: none; border-top: 1px solid {c.border_soft}; }}

    /* surfaces */
    QWidget#Workspace, QWidget#ContentPane {{ background: {c.app_bg}; }}
    QWidget#InspectorPane {{ background: {c.panel}; border-left: 1px solid {c.border}; }}
    QFrame#SurfaceCard, QFrame#ProgressCard, QFrame#TableCard, QFrame#Card {{
        background: {c.panel}; border: 1px solid {c.border}; border-radius: 12px; }}
    QFrame#Kpi {{ background: {c.panel}; border: 1px solid {c.border}; border-radius: 12px; }}
    QFrame#EmptyState {{ background: transparent; border: 1px dashed {c.border}; border-radius: 12px; }}
    QFrame#SelectionBar {{ background: {c.accent_soft}; border: 1px solid {c.accent}; border-radius: 8px; }}
    QFrame#Separator {{ background: {c.border}; max-height: 1px; min-height: 1px; border: none; }}

    /* text */
    QLabel {{ background: transparent; }}
    QLabel#PageTitle {{ font-size: 22px; font-weight: 700; color: {c.text}; }}
    QLabel#SectionTitle {{ font-size: 14px; font-weight: 650; color: {c.text}; }}
    QLabel#Muted {{ color: {c.text_secondary}; font-size: 12.5px; }}
    QLabel#FieldLabel {{ color: {c.text_secondary}; font-size: 12px; font-weight: 600; }}
    QLabel#EmptyTitle {{ color: {c.text}; font-size: 15px; font-weight: 650; }}
    QLabel#StatValue {{ font-size: 18px; font-weight: 700; }}
    QLabel#StatLabel {{ font-size: 11.5px; color: {c.text_secondary}; font-weight: 550; }}
    QLabel#KpiValue {{ font-size: 24px; font-weight: 700; color: {c.text}; }}
    QLabel#KpiLabel {{ font-size: 11.5px; color: {c.text_secondary}; font-weight: 600; }}
    QLabel#KpiSub {{ font-size: 11px; color: {c.text_secondary}; }}
    QLabel#Link {{ color: {c.accent}; }}

    /* inputs */
    QLineEdit, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit, QDateTimeEdit {{
        background: {c.input_bg}; color: {c.text}; border: 1px solid {c.border}; border-radius: 8px;
        padding: 6px 10px; font-size: 13px; selection-background-color: {c.accent}; }}
    QPlainTextEdit, QTextEdit {{ padding: 8px; }}
    QLineEdit:focus, QTextEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QSpinBox:focus,
    QDateEdit:focus, QDateTimeEdit:focus {{ border: 1px solid {c.accent}; }}
    QLineEdit:disabled, QComboBox:disabled {{ color: {c.text_secondary}; }}
    QLineEdit#SearchField {{ background: {c.input_bg}; padding: 6px 10px 6px 4px; min-height: 18px; }}
    QComboBox {{ padding-right: 26px; min-height: 18px; }}
    QComboBox::drop-down {{ border: none; width: 24px; subcontrol-origin: padding; subcontrol-position: center right; }}
    QComboBox::down-arrow {{ image: url({chev}); width: 12px; height: 12px; }}
    QSpinBox, QDoubleSpinBox, QDateEdit, QDateTimeEdit {{ padding-right: 24px; min-height: 18px; }}
    QSpinBox::up-button, QDoubleSpinBox::up-button {{ subcontrol-origin: border; subcontrol-position: top right;
        width: 20px; border: none; background: transparent; }}
    QSpinBox::down-button, QDoubleSpinBox::down-button {{ subcontrol-origin: border;
        subcontrol-position: bottom right; width: 20px; border: none; background: transparent; }}
    QSpinBox::up-arrow, QDoubleSpinBox::up-arrow {{ image: url({chev_up}); width: 10px; height: 10px; }}
    QSpinBox::down-arrow, QDoubleSpinBox::down-arrow {{ image: url({chev}); width: 10px; height: 10px; }}
    QDateEdit::drop-down, QDateTimeEdit::drop-down {{ border: none; width: 24px; subcontrol-origin: padding;
        subcontrol-position: center right; }}
    QDateEdit::down-arrow, QDateTimeEdit::down-arrow {{ image: url({chev}); width: 12px; height: 12px; }}
    QComboBox QAbstractItemView {{ background: {c.panel}; color: {c.text}; border: 1px solid {c.border};
        selection-background-color: {c.accent_soft}; selection-color: {c.text}; outline: none; }}
    QCheckBox, QRadioButton {{ spacing: 7px; color: {c.text}; font-size: 13px; }}
    QCheckBox::indicator {{ width: 15px; height: 15px; border: 1px solid {c.border}; border-radius: 4px;
        background: {c.input_bg}; }}
    QCheckBox::indicator:checked {{ background: {c.accent}; border-color: {c.accent}; image: url({_check_png()}); }}
    QRadioButton::indicator {{ width: 14px; height: 14px; border: 1px solid {c.border}; border-radius: 8px;
        background: {c.input_bg}; }}
    QRadioButton::indicator:checked {{ background: {c.accent}; border: 3px solid {c.input_bg};
        outline: 1px solid {c.accent}; }}
    QCalendarWidget QWidget {{ background: {c.panel}; color: {c.text}; }}

    /* buttons */
    QPushButton {{ background: {c.panel_alt}; color: {c.text}; border: 1px solid {c.border}; border-radius: 8px;
        padding: 6px 14px; font-size: 13px; font-weight: 600; min-height: 18px; }}
    QPushButton:hover {{ background: {c.hover}; }}
    QPushButton:pressed {{ background: {c.border_soft}; }}
    QPushButton:disabled {{ color: {c.text_secondary}; background: transparent; border-color: {c.border_soft}; }}
    QPushButton:focus {{ border: 1px solid {c.accent}; }}
    QPushButton#PrimaryButton {{ background: qlineargradient(x1:0,y1:0,x2:0,y2:1, stop:0 {c.accent_hover}, stop:1 {c.accent});
        color: {c.accent_text}; border: 1px solid {c.accent}; }}
    QPushButton#PrimaryButton:hover {{ background: {c.accent_hover}; border-color: {c.accent_hover}; }}
    QPushButton#PrimaryButton:disabled {{ background: {c.accent_soft}; color: {c.text_secondary}; border-color: transparent; }}
    QPushButton#SuccessButton {{ background: #16A34A; color: white; border: 1px solid #16A34A; }}
    QPushButton#SuccessButton:hover {{ background: #15803D; }}
    QPushButton#SuccessButton:disabled {{ background: transparent; color: {c.text_secondary}; border-color: {c.border_soft}; }}
    QPushButton#GhostButton {{ background: transparent; }}
    QPushButton#GhostButton:hover {{ background: {c.hover}; }}
    QPushButton#DangerButton {{ background: transparent; color: {c.danger}; border: 1px solid {c.danger}; }}
    QPushButton#IconButton, QToolButton#RowAction {{ background: transparent; border: 1px solid {c.border};
        border-radius: 7px; padding: 0; }}
    QToolButton#RowAction {{ border: none; }}
    QPushButton#IconButton:hover, QToolButton#RowAction:hover {{ background: {c.hover}; }}
    QToolButton#RowAction:disabled {{ opacity: 0.3; }}
    QPushButton#IconButtonPrimary {{ background: {c.accent}; border: 1px solid {c.accent}; border-radius: 7px; padding: 0; }}
    QPushButton#IconButtonSuccess {{ background: #16A34A; border: 1px solid #16A34A; border-radius: 7px; padding: 0; }}
    QPushButton#Chip {{ background: {c.panel_alt}; color: {c.text_secondary}; border: 1px solid {c.border};
        border-radius: 14px; padding: 4px 12px; font-size: 12px; font-weight: 600; }}
    QPushButton#Chip:hover {{ color: {c.text}; background: {c.hover}; }}
    QPushButton#Chip:checked {{ background: {c.accent}; color: {c.accent_text}; border: 1px solid {c.accent}; }}
    QPushButton#Segment {{ background: transparent; border: none; border-radius: 6px; padding: 5px 11px;
        color: {c.text_secondary}; font-size: 12.5px; font-weight: 600; }}
    QPushButton#Segment:checked {{ background: {c.accent_soft}; color: {c.accent}; }}
    QMenu {{ background: {c.panel}; color: {c.text}; border: 1px solid {c.border}; border-radius: 8px; padding: 5px; }}
    QMenu::item {{ padding: 6px 22px 6px 12px; border-radius: 5px; font-size: 13px; }}
    QMenu::item:selected {{ background: {c.accent_soft}; }}
    QMenu::item:disabled {{ color: {c.text_secondary}; }}
    QMenu::separator {{ height: 1px; background: {c.border}; margin: 4px 6px; }}

    /* tables */
    QTableWidget, QTableView {{ background: transparent; alternate-background-color: transparent; border: none;
        gridline-color: transparent; selection-background-color: {c.accent_soft}; selection-color: {c.text};
        font-size: 13px; outline: none; }}
    QTableWidget::item {{ padding: {pad_cell}; border-bottom: 1px solid {c.border_soft}; }}
    QTableWidget::item:focus {{ outline: none; border: none; }}
    QTableWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QHeaderView {{ background: transparent; }}
    QHeaderView::section {{ background: transparent; color: {c.text_secondary}; border: none;
        border-bottom: 1px solid {c.border}; padding: 8px 10px; font-size: 11px; font-weight: 700; }}
    QTreeWidget {{ background: transparent; border: none; font-size: 13px; outline: none; }}
    QTreeWidget::item {{ padding: 5px 4px; border-radius: 5px; }}
    QTreeWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}
    QListWidget {{ background: transparent; border: none; outline: none; font-size: 13px; }}
    QListWidget::item {{ padding: 6px 8px; border-radius: 6px; }}
    QListWidget::item:selected {{ background: {c.accent_soft}; color: {c.text}; }}

    /* tabs */
    QTabWidget::pane {{ border: none; background: transparent; }}
    QTabBar::tab {{ background: transparent; color: {c.text_secondary}; padding: 8px 12px; margin-right: 2px;
        border: none; border-bottom: 2px solid transparent; font-size: 12.5px; font-weight: 600; }}
    QTabBar::tab:hover {{ color: {c.text}; }}
    QTabBar::tab:selected {{ color: {c.accent}; border-bottom: 2px solid {c.accent}; }}

    /* scroll */
    QScrollArea {{ background: transparent; border: none; }}
    QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
    QScrollBar::handle:vertical {{ background: {c.border}; border-radius: 4px; min-height: 28px; }}
    QScrollBar::handle:vertical:hover {{ background: {c.text_secondary}; }}
    QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
    QScrollBar::handle:horizontal {{ background: {c.border}; border-radius: 4px; min-width: 28px; }}
    QScrollBar::add-line, QScrollBar::sub-line {{ width: 0; height: 0; }}
    QSplitter::handle {{ background: {c.border_soft}; }}
    QSplitter::handle:horizontal {{ width: 1px; }}
    QStatusBar {{ background: {c.app_bg}; color: {c.text_secondary}; border-top: 1px solid {c.border_soft};
        font-size: 12px; }}
    QProgressBar {{ background: {c.border_soft}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }}
    QProgressBar::chunk {{ background: {c.success}; border-radius: 4px; }}
    QGroupBox {{ background: {c.panel}; border: 1px solid {c.border}; border-radius: 12px; margin-top: 12px;
        padding: 16px 14px 12px; font-weight: 650; font-size: 13px; }}
    QGroupBox::title {{ subcontrol-origin: margin; left: 14px; padding: 0 6px; color: {c.text}; }}
    QFrame#EditorBox {{ background: {c.input_bg}; border: 1px solid {c.border}; border-radius: 10px; }}
    QFrame#EditorBox QPlainTextEdit {{ background: transparent; border: none; padding: 8px 4px; }}
    QLabel#CodeNote {{ background: {c.input_bg}; border: 1px solid {c.border}; border-radius: 8px; padding: 8px 10px;
        color: {c.text_secondary}; font-family: Consolas, 'Cascadia Mono', monospace; font-size: 12px; }}
    QFrame#SettingsFooter {{ background: {c.panel}; border-top: 1px solid {c.border}; }}
    QLabel#Badge {{ border-radius: 10px; padding: 2px 10px; font-size: 11.5px; font-weight: 650; }}

    /* ---- design v3: product-grade overrides ---- */
    QMainWindow, QDialog {{ font-size: 14px; }}
    QWidget#Sidebar {{ background: {c.sidebar}; border: 1px solid {c.border}; border-radius: 16px; }}
    QPushButton#NavButton {{ padding: 11px 14px; font-size: 14px; border-radius: 10px; min-height: 22px; }}
    QPushButton#NavButton:hover {{ background: {c.sidebar_hover}; }}
    QPushButton#NavButton[active="true"] {{ background: {c.accent}; color: #FFFFFF; font-weight: 700;
        border-left: none; padding-left: 14px; }}
    QLabel#NavCount {{ background: rgba(255,255,255,0.92); color: #1F5BDB; border-radius: 10px; padding: 1px 9px;
        font-size: 12px; font-weight: 800; }}
    QFrame#StatusCard {{ background: transparent; border: none; }}
    QLabel#Connected {{ color: {c.success}; font-size: 12.5px; font-weight: 650; }}
    QLabel#Disconnected {{ color: {c.warning}; font-size: 12.5px; font-weight: 650; }}
    QLabel#PageTitle {{ font-size: 24px; }}
    QWidget#InspectorPane {{ background: transparent; border: none; }}
    QFrame#Card {{ border-radius: 14px; }}
    QFrame#SurfaceCard, QFrame#TableCard, QFrame#ProgressCard {{ border-radius: 14px; }}
    QPushButton#Chip {{ padding: 7px 17px; font-size: 13px; border-radius: 18px; min-height: 18px; }}
    QProgressBar {{ min-height: 10px; max-height: 10px; border-radius: 5px; }}
    QProgressBar::chunk {{ border-radius: 5px; }}
    QTableWidget {{ font-size: 13.5px; }}
    QHeaderView::section {{ font-size: 12px; padding: 10px 10px; }}
    QPushButton {{ min-height: 20px; font-size: 13.5px; }}
    QPushButton#PrimaryButton, QPushButton#SuccessButton {{ min-height: 24px; padding: 8px 18px; }}
    QPushButton#Chip[compact="true"] {{ padding: 5px 11px; font-size: 12px; border-radius: 15px; }}
    QFrame#IntakeCard {{ background: {c.input_bg}; border: 1px solid {c.border}; border-radius: 12px; }}
    QFrame#IntakeCard QPlainTextEdit {{ border: none; background: transparent; padding: 0; }}
    QTabWidget#Segmented::pane {{ border: none; }}
    QTabWidget#Segmented QTabBar::tab {{ background: {c.panel_alt}; border: 1px solid {c.border}; padding: 8px 6px;
        margin: 0; color: {c.text_secondary}; font-weight: 650; border-bottom: 1px solid {c.border}; }}
    QTabWidget#Segmented QTabBar::tab:first {{ border-top-left-radius: 8px; border-bottom-left-radius: 8px; }}
    QTabWidget#Segmented QTabBar::tab:last {{ border-top-right-radius: 8px; border-bottom-right-radius: 8px; }}
    QTabWidget#Segmented QTabBar::tab:selected {{ background: {c.accent}; color: #FFFFFF; border-color: {c.accent}; }}

    /* ---- design v4: frameless chrome, flatter surfaces ---- */
    QWidget#TitleStrip {{ background: transparent; }}
    QPushButton#LinkButton {{ background: transparent; border: none; color: {c.accent}; padding: 0; font-size: 12.5px; font-weight: 600; min-height: 16px; }}
    QPushButton#LinkButton:hover {{ color: {c.accent_hover}; }}
    QFrame#Card QFrame#EmptyState {{ border: none; background: transparent; }}
    QToolButton#WinButton, QToolButton#WinClose {{ background: transparent; border: none; border-radius: 6px; }}
    QToolButton#WinButton:hover {{ background: {c.hover}; }}
    QToolButton#WinClose:hover {{ background: #E5484D; }}
    QWidget#Sidebar {{ border: 1px solid {c.border_soft}; border-radius: 14px; }}
    QLabel#BrandTitle {{ font-size: 13.5px; font-weight: 700; }}
    QPushButton#NavButton {{ font-size: 14.5px; padding: 12px 14px; }}
    QPushButton#Chip {{ background: {c.panel}; border: 1px solid transparent; border-radius: 10px;
        padding: 8px 18px; color: {c.text}; font-weight: 600; }}
    QPushButton#Chip:hover {{ background: {c.hover}; }}
    QPushButton#Chip:checked {{ background: {c.accent}; color: #FFFFFF; border: 1px solid {c.accent}; }}
    QPushButton#Chip[compact="true"] {{ border-radius: 9px; padding: 5px 12px; }}
    QFrame#SurfaceCard, QFrame#TableCard, QFrame#Card {{ border-radius: 12px; }}
    QFrame#TableCard {{ background: {c.panel_alt}; }}
    QFrame#SurfaceCard {{ background: {c.panel_alt}; }}
    QHeaderView::section {{ color: {c.text}; font-weight: 600; font-size: 13px; }}
    QTableWidget::item {{ border-bottom: 1px solid {c.border_soft}; }}
    QTabWidget QTabBar::tab:selected {{ color: {c.text}; border-bottom: 2px solid {c.accent}; }}

    /* ---- design v5: density + inspector ---- */
    QLabel#PageTitle {{ font-size: 22px; }}
    QTableWidget {{ font-size: 13px; }}
    QTableWidget::item {{ padding: 0 10px; }}
    QHeaderView::section {{ color: {c.text}; font-weight: 500; font-size: 13px; padding: 8px 10px; }}
    QFrame#Hero {{ background: transparent; border: none; border-radius: 0; }}
    QFrame#InspectorHead {{ background: transparent; border: none; border-bottom: 1px solid {c.border}; }}
    QProgressBar#StepBar::chunk {{ background: {c.accent}; }}
    QTabWidget#Segmented::pane {{ border: none; border-top: 1px solid {c.border}; }}
    QTabWidget#Segmented QTabBar::tab {{ background: transparent; border: none; border-bottom: 2px solid transparent;
        border-radius: 0; padding: 9px 6px; margin: 0; color: {c.text_secondary}; font-weight: 600; }}
    QTabWidget#Segmented QTabBar::tab:first, QTabWidget#Segmented QTabBar::tab:last {{ border-radius: 0; }}
    QTabWidget#Segmented QTabBar::tab:hover {{ color: {c.text}; }}
    QTabWidget#Segmented QTabBar::tab:selected {{ background: transparent; color: {c.text};
        border-bottom: 2px solid {c.accent}; border-color: {c.accent}; }}
    /* disabled controls: clearly "off" (flat, dim, no emphasis) vs. enabled */
    QPushButton:disabled {{ background: {c.border_soft}; color: {_dim(c.text_secondary, 0.55)};
        border: 1px solid transparent; }}
    QPushButton#PrimaryButton:disabled {{ background: {_dim(c.accent, 0.22)}; color: {_dim("#FFFFFF", 0.42)};
        border: 1px solid transparent; }}
    QPushButton#SuccessButton:disabled {{ background: {_dim("#16A34A", 0.2)}; color: {_dim("#FFFFFF", 0.4)};
        border: 1px solid transparent; }}
    QPushButton#GhostButton:disabled, QPushButton#PauseJob:disabled {{ background: transparent;
        color: {_dim(c.text_secondary, 0.5)}; border: 1px dashed {c.border}; }}
    QPushButton#IconButton:disabled {{ background: transparent; border: 1px dashed {c.border}; }}
    QPushButton#IconButtonPrimary:disabled {{ background: {_dim(c.accent, 0.22)}; border: 1px solid transparent; }}
    QPushButton#IconButtonSuccess:disabled {{ background: {_dim("#16A34A", 0.2)}; border: 1px solid transparent; }}
    QPushButton#Chip:disabled {{ color: {_dim(c.text_secondary, 0.5)}; }}
    QToolButton#RowAction:disabled {{ background: transparent; }}
    QLineEdit:disabled, QComboBox:disabled, QDateEdit:disabled, QDateTimeEdit:disabled, QSpinBox:disabled,
    QPlainTextEdit:disabled, QTextEdit:disabled {{ background: {c.border_soft}; color: {_dim(c.text_secondary, 0.6)};
        border: 1px solid transparent; }}
    QCheckBox:disabled {{ color: {_dim(c.text_secondary, 0.6)}; }}
    QLabel#SidebarValue {{ font-size: 13px; }}
    QLabel#SidebarMeta {{ font-size: 12px; }}
    QLabel#Connected, QLabel#Disconnected {{ font-size: 13px; }}
    QPushButton#PauseJob {{ background: {c.panel_alt}; border: 1px solid {c.border}; border-radius: 9px; min-height: 30px; }}
    """


# ---- badges -------------------------------------------------------------------------------

_BADGES_LIGHT = {
    "success": ("#15803D", "#DCFCE7"),
    "info": ("#1D4ED8", "#DBEAFE"),
    "warning": ("#B45309", "#FEF3C7"),
    "danger": ("#B91C1C", "#FEE2E2"),
    "neutral": ("#475569", "#E8EDF4"),
    "violet": ("#6D28D9", "#EDE9FE"),
}
_BADGES_DARK = {
    "success": ("#4ADE80", "#123524"),
    "info": ("#6AA3FF", "#15294D"),
    "warning": ("#F5B73B", "#3A2C0E"),
    "danger": ("#F68787", "#3D1A1F"),
    "neutral": ("#9AA8BD", "#1B2536"),
    "violet": ("#B7A3FB", "#2A2150"),
}

STATUS_KIND = {
    "Queued": "neutral", "Extracting": "info", "Generating": "info", "Exporting": "info",
    "Needs JD": "warning", "Done": "success", "Skipped": "neutral", "Failed": "danger",
}

STAGE_KIND = {
    "not_applied": "neutral", "applied": "info", "recruiter_contact": "violet", "screening": "violet",
    "technical": "warning", "final": "warning", "offer": "success",
}
OUTCOME_KIND = {"rejected": "danger", "withdrawn": "neutral", "position_closed": "neutral", "accepted": "success"}


# Saturated, solid status pills (identical on both themes) - what the product reference uses.
_SOLID = {
    "success": ("#06280F", "#2FD072"),
    "info": ("#FFFFFF", "#3B82F6"),
    "warning": ("#2B1C00", "#F5B83D"),
    "danger": ("#FFFFFF", "#EF5350"),
    "neutral": ("#FFFFFF", "#6B7487"),
    "violet": ("#FFFFFF", "#8B5CF6"),
}


def solid_colors(kind: str) -> tuple[str, str]:
    return _SOLID.get(kind, _SOLID["neutral"])


def badge_colors(kind: str, light: bool | None = None) -> tuple[str, str]:
    light = current().is_light if light is None else light
    return (_BADGES_LIGHT if light else _BADGES_DARK).get(kind, ("#64748B", "#E8EDF4"))


def badge_style(kind: str, light: bool | None = None) -> str:
    fg, bg = solid_colors(kind)
    return f"QLabel {{ color: {fg}; background: {bg}; border-radius: 10px; padding: 2px 10px; font-size: 11.5px; font-weight: 650; }}"


def status_badge_style(status: str, light: bool = True) -> str:
    return badge_style(STATUS_KIND.get(status, "neutral"), light)
