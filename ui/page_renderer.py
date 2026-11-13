"""Loads a job page in a hidden Chromium view and returns the rendered HTML.

Used as a fallback when a plain HTTP fetch gets an empty script-built shell (JobDiva, RippleHire, ...).
It never tries to get past a bot check or login wall. The worker thread calls `fetch`;
all Qt work happens on the GUI thread.
"""

from __future__ import annotations

import threading

from PySide6.QtCore import QMetaObject, QObject, Qt, QTimer, QUrl, Slot
from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile

POLL_MS = 1000
MIN_TEXT = 400  # visible characters before a page counts as rendered
STABLE_POLLS = 2
CHALLENGE_TITLES = ("just a moment", "human verification", "attention required", "access denied", "verify you")

# Pages can inline megabytes of scripts/images; send back only the text-bearing markup.
_STATE_JS = """
(function () {
  var t = document.body ? document.body.innerText : "";
  var html = "";
  if (document.documentElement) {
    var c = document.documentElement.cloneNode(true);
    c.querySelectorAll("script:not([type='application/ld+json']),style,svg,img,picture,video,canvas,iframe,link,noscript")
      .forEach(function (n) { n.remove(); });
    html = c.outerHTML;
  }
  return JSON.stringify({title: document.title || "", len: t.length, html: html});
})()
"""


class PageRenderer(QObject):
    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._profile = QWebEngineProfile(self)  # off-the-record: no cookies kept, separate from the Claude login
        self._page: QWebEnginePage | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._poll)
        self._url = ""
        self._deadline = 0
        self._polls = 0
        self._last_len = -1
        self._stable = 0
        self._done = threading.Event()
        self._html: str | None = None

    # ------------------------------------------------------------ worker thread

    def fetch(self, url: str, timeout: float = 45.0) -> str | None:
        """Blocking; call from a non-GUI thread. Returns rendered HTML or None."""
        self._done.clear()
        self._html = None
        self._url = url
        self._deadline = int(timeout * 1000 / POLL_MS)
        QMetaObject.invokeMethod(self, "_begin", Qt.ConnectionType.BlockingQueuedConnection)
        self._done.wait(timeout + 10)
        QMetaObject.invokeMethod(self, "_end", Qt.ConnectionType.BlockingQueuedConnection)
        return self._html

    # ---------------------------------------------------------------- GUI thread

    @Slot()
    def _begin(self) -> None:
        self._end()
        self._page = QWebEnginePage(self._profile, self)
        self._page.setAudioMuted(True)
        self._polls = 0
        self._last_len = -1
        self._stable = 0
        self._page.load(QUrl(self._url))
        self._timer.start()

    @Slot()
    def _end(self) -> None:
        self._timer.stop()
        if self._page is not None:
            self._page.deleteLater()
            self._page = None

    def _poll(self) -> None:
        if self._page is None:
            return
        self._polls += 1
        if self._polls > self._deadline:
            self._finish_with_html()
            return
        self._page.runJavaScript(_STATE_JS, self._on_state)

    def _on_state(self, value: object) -> None:
        import json

        if self._page is None or not isinstance(value, str):
            return
        try:
            state = json.loads(value)
        except ValueError:
            return
        title = str(state.get("title", "")).lower()
        length = int(state.get("len", 0))
        if any(token in title for token in CHALLENGE_TITLES):
            # A bot check: respect it. Give up so the job falls back to "paste the description".
            self._timer.stop()
            self._done.set()
            return
        if length >= MIN_TEXT and length == self._last_len:
            self._stable += 1
        else:
            self._stable = 0
        self._last_len = length
        if self._stable >= STABLE_POLLS:
            self._html = str(state.get("html") or "")
            self._timer.stop()
            self._done.set()

    def _finish_with_html(self) -> None:
        """Out of time: hand back whatever rendered (it may still contain JSON-LD), unless it is a bot check."""
        if self._page is None:
            self._done.set()
            return

        def got(value: object) -> None:
            import json

            try:
                state = json.loads(value) if isinstance(value, str) else {}
            except ValueError:
                state = {}
            if not any(t in str(state.get("title", "")).lower() for t in CHALLENGE_TITLES):
                self._html = str(state.get("html") or "") or None
            self._done.set()

        self._timer.stop()
        self._page.runJavaScript(_STATE_JS, got)
