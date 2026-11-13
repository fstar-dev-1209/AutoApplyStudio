"""Claude web chat size limits — resumes may be 10k+ characters; no upper cap on JSON length."""

# Maximum text returned from page JavaScript (Qt string safety margin).
CLAUDE_MAX_SCRAPED_TEXT_CHARS = 10_000_000

# Ignore tiny DOM false positives (e.g. 101-char JD fragments), not real resumes.
MIN_RESUME_JSON_SNIPPET_CHARS = 40
