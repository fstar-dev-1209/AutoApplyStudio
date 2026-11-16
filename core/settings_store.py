"""Persistent application settings."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from core.app_lock import AppLock, LockedError
from core.apply_facts import clean_facts, clean_form_questions
from core.paths import config_dir, default_output_root
from core.secrets import load_secret, store_secret


DEFAULT_PROMPT = """You are an expert resume writer for software engineers applying to remote roles.

Given the candidate profile and the job description, produce a single JSON object that matches this schema closely enough to export:

{
  "name": "...",
  "email": "...",
  "phone": "...",
  "location": "...",
  "company_name": "...",
  "role_title": "...",
  "job_link": "...",
  "subtitle": "...",
  "summary": "...",
  "experience": [
    {
      "title": "...",
      "company": "...",
      "location": "...",
      "dates": "Mon YYYY - Mon YYYY",
      "bullets": ["..."]
    }
  ],
  "education": [
    {"school": "...", "location": "...", "degree": "...", "dates": "..."}
  ],
  "skill_groups": [
    {"name": "...", "skills": ["..."]}
  ],
  "cover_letter": "Full cover letter body paragraphs..."
}

Rules:
- Output ONLY valid JSON. No markdown fences.
- Tailor summary, bullets, and skills to the job description without inventing employers.
- Keep experience employers and dates truthful to the profile when provided.
- Always include company_name, role_title, and cover_letter.
"""


@dataclass
class ProfileSettings:
    name: str = "Christopher Hosier"
    email: str = "christopher.a.hosier@outlook.com"
    phone: str = "(209) 322-9526"
    location: str = "Mt Dora, FL"
    education_school: str = "University of Florida"
    education_location: str = "Gainesville, FL"
    education_degree: str = "B.S. Computer Science"
    education_dates: str = ""
    # The real jobs, as plain text. Sent with every resume request as fixed facts; a resume naming any other employer
    # is rejected. Blank = not set (Auto mode will not apply until it is filled in).
    work_history: str = ""
    # Used only to fill employer application forms (job apply assist). Blank means "leave the field empty".
    linkedin_url: str = ""
    github_url: str = ""
    website_url: str = ""
    work_authorization: str = ""  # "Yes" | "No" | "" - legally authorized to work in the job's country
    needs_sponsorship: str = ""  # "Yes" | "No" | "" - will now or later need visa sponsorship


GENERATION_CLAUDE_CHAT = "claude_chat"  # web chat; which site is ``AppSettings.chat_provider``
GENERATION_CLAUDE_API = "claude_api"

CHAT_PROVIDERS = ("claude", "chatgpt")

MODE_STANDALONE = "standalone"
MODE_CONTROLLER = "controller"
MODE_VIEWER = "viewer"
MODES = (MODE_STANDALONE, MODE_CONTROLLER, MODE_VIEWER)
CONTROLLER_TOKEN_SECRET = "controller_token"
PROXY_SECRET = "web_proxy_credentials"  # "user:password" for the proxy, when it needs one


def normalize_controller_url(text: str) -> str:
    """"vps.example:47821" -> "http://vps.example:47821"; anything after the host and port is dropped."""
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return ""
    return f"{parts.scheme}://{parts.netloc}"


def normalize_proxy_url(text: str) -> str:
    """"1.2.3.4:8080" -> "http://1.2.3.4:8080"; http, https, socks5 only; credentials and paths are dropped."""
    text = (text or "").strip()
    if not text:
        return ""
    if "://" not in text:
        text = "http://" + text
    parts = urlsplit(text)
    if parts.scheme not in {"http", "https", "socks5"} or not parts.hostname:
        return ""
    try:
        port = parts.port
    except ValueError:
        return ""
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port else "")


@dataclass
class AppSettings:
    profile: ProfileSettings = field(default_factory=ProfileSettings)
    generation_mode: str = GENERATION_CLAUDE_CHAT  # claude_chat | claude_api
    api_key: str = ""
    model: str = "claude-sonnet-4-20250514"
    prompt_template: str = DEFAULT_PROMPT
    output_root: str = ""
    theme: str = "dark"  # system | light | dark
    density: str = "comfortable"  # comfortable | compact
    max_retries: int = 1
    request_delay_seconds: float = 0.5
    api_key_saved: bool = False
    auto_retries: int = 0  # extra automatic attempts after a failed generation (0 = manual retry only)
    export_pdf: bool = True
    export_cover_letter: bool = True
    ghost_after_days: int = 14  # applied with no reply for this long -> "No response"
    intake_enabled: bool = True  # listen for jobs sent by the browser extension
    intake_auto_start: bool = True  # start the queue when a job arrives (unless you paused it)
    ui_revision: int = 2  # 2 = tracking redesign (dark navy default)
    # How this copy of the app runs. standalone = everything on this computer (the original behaviour);
    # controller = also serves a viewer (run it on the VPS); viewer = shows and manages the controller's data.
    app_mode: str = MODE_STANDALONE
    intake_bind: str = "127.0.0.1"  # controller: address to listen on; only change it behind a VPN or TLS
    intake_allowed_hosts: str = ""  # controller: extra "host:port" names clients use to reach it
    controller_url: str = ""  # viewer: address of the controller, e.g. http://100.64.0.5:47821
    auto_sync_files: bool = True  # viewer: rebuild each newly finished resume on this computer
    chat_provider: str = "claude"  # which web chat writes resumes: claude | chatgpt
    web_proxy: str = ""  # optional proxy for the chat browser (and the apply browser), e.g. http://1.2.3.4:8080
    # --- auto apply (all off / safe until switched on in Settings)
    discovery_enabled: bool = False  # poll Jobright for new postings
    discovery_interval_minutes: int = 60  # one batch of fresh postings per interval, newest first
    discovery_hourly_cap: int = 20  # new jobs queued per hour; what is not queued or not generated in time is dropped
    discovery_url: str = ""  # blank = Jobright's recommended list; your filters live in your Jobright account
    discovery_max_age_hours: int = 24  # only postings from the last day
    discovery_blocked_companies: str = ""  # comma / newline separated
    discovery_exclude_words: str = ""  # title words that reject a posting
    bot_timezone: str = "America/New_York"  # "today" and the active hours follow this clock
    active_start_hour: int = 0
    active_end_hour: int = 24  # 0-24 = always on
    bot_browser: str = "chrome"  # chrome | msedge
    apply_mode: str = "off"  # off | dry_run | auto
    apply_daily_cap: int = 40  # applications the bot may submit per day
    salary_floor: int = 80_000  # the least you accept, per year: salary answers never go below it
    salary_band_low: int = 40  # aim this far (percent) into a job's posted pay range ...
    salary_band_high: int = 55  # ... up to this far; the lower-middle, to get replies
    discovery_skip_below_floor: bool = True  # do not queue a job whose posted pay tops out below your minimum
    apply_skip_optional: bool = True  # questions the form does not require are left blank, never answered
    notify_reports: bool = True  # status reports (job search, resumes, applications, answers) to Telegram/Discord
    apply_facts: dict[str, str] = field(default_factory=dict)  # see core.apply_facts; sealed with the app password
    form_questions: list[dict[str, Any]] = field(default_factory=list)  # a form's own choices, once it has asked; sealed too
    answer_prompt: str = ""  # style prompt for open questions; blank = core.chat_questions.DEFAULT_ANSWER_PROMPT
    notify_telegram_chat: str = ""  # the chat id; the bot token and the Discord webhook are kept as secrets

    def resolved_output_root(self) -> Path:
        if self.output_root.strip():
            return Path(self.output_root.strip())
        return default_output_root()

    def discovery_batch_size(self) -> int:
        """Jobs one discovery cycle may queue: the hourly cap, scaled to how often the cycle runs (60 min -> the cap)."""
        return max(1, -(-self.discovery_hourly_cap * max(1, self.discovery_interval_minutes) // 60))

    def discovery_window_minutes(self) -> float:
        """A job still waiting to be generated this long after it was queued belongs to an earlier batch: half an
        interval, so that the previous scheduled batch is always older than this when the next one arrives."""
        return max(1, self.discovery_interval_minutes) / 2

    def masked_api_key(self) -> str:
        key = self.api_key.strip()
        if not key:
            return ""
        if len(key) <= 8:
            return "•" * len(key)
        return key[:4] + "•" * (len(key) - 8) + key[-4:]

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AppSettings:
        profile_raw = data.get("profile") if isinstance(data.get("profile"), dict) else {}
        profile = ProfileSettings(
            name=str(profile_raw.get("name") or ProfileSettings.name),
            email=str(profile_raw.get("email") or ProfileSettings.email),
            phone=str(profile_raw.get("phone") or ProfileSettings.phone),
            location=str(profile_raw.get("location") or ProfileSettings.location),
            education_school=str(
                profile_raw.get("education_school") or ProfileSettings.education_school
            ),
            education_location=str(
                profile_raw.get("education_location") or ProfileSettings.education_location
            ),
            education_degree=str(
                profile_raw.get("education_degree") or ProfileSettings.education_degree
            ),
            education_dates=str(profile_raw.get("education_dates") or ""),
            work_history=str(profile_raw.get("work_history") or ""),
            linkedin_url=str(profile_raw.get("linkedin_url") or ""),
            github_url=str(profile_raw.get("github_url") or ""),
            website_url=str(profile_raw.get("website_url") or ""),
            work_authorization=yes_no(profile_raw.get("work_authorization")),
            needs_sponsorship=yes_no(profile_raw.get("needs_sponsorship")),
        )
        mode = str(data.get("generation_mode") or GENERATION_CLAUDE_CHAT)
        if mode not in {GENERATION_CLAUDE_CHAT, GENERATION_CLAUDE_API}:
            mode = GENERATION_CLAUDE_CHAT
        settings = cls(
            profile=profile,
            generation_mode=mode,
            api_key=str(data.get("api_key") or ""),
            model=str(data.get("model") or "claude-sonnet-4-20250514"),
            prompt_template=str(data.get("prompt_template") or DEFAULT_PROMPT),
            output_root=str(data.get("output_root") or ""),
            theme=_migrated_theme(data),
            density=str(data.get("density") or "comfortable"),
            max_retries=int(data.get("max_retries") or 1),
            request_delay_seconds=float(data.get("request_delay_seconds") or 0.5),
            api_key_saved=bool(data.get("api_key_saved")),
            auto_retries=max(0, min(5, int(data.get("auto_retries") or 0))),
            export_pdf=bool(data.get("export_pdf", True)),
            export_cover_letter=bool(data.get("export_cover_letter", True)),
            ghost_after_days=max(1, int(data.get("ghost_after_days") or 14)),
            intake_enabled=bool(data.get("intake_enabled", True)),
            intake_auto_start=bool(data.get("intake_auto_start", True)),
            app_mode=str(data.get("app_mode")) if data.get("app_mode") in MODES else MODE_STANDALONE,
            intake_bind=str(data.get("intake_bind") or "127.0.0.1").strip() or "127.0.0.1",
            intake_allowed_hosts=str(data.get("intake_allowed_hosts") or "").strip(),
            controller_url=normalize_controller_url(str(data.get("controller_url") or "")),
            auto_sync_files=bool(data.get("auto_sync_files", True)),
            chat_provider=str(data.get("chat_provider")) if data.get("chat_provider") in CHAT_PROVIDERS else "claude",
            web_proxy=normalize_proxy_url(str(data.get("web_proxy") or "")),
            discovery_enabled=bool(data.get("discovery_enabled", False)),
            discovery_interval_minutes=_clamp(data.get("discovery_interval_minutes"), 2, 240, 60),
            discovery_hourly_cap=_clamp(data.get("discovery_hourly_cap"), 1, 200, 20),
            discovery_url=_https_url(data.get("discovery_url")),
            discovery_max_age_hours=_clamp(data.get("discovery_max_age_hours"), 1, 168, 24),
            discovery_blocked_companies=str(data.get("discovery_blocked_companies") or "").strip(),
            discovery_exclude_words=str(data.get("discovery_exclude_words") or "").strip(),
            bot_timezone=_timezone_name(data.get("bot_timezone")),
            active_start_hour=_clamp(data.get("active_start_hour"), 0, 23, 0),
            active_end_hour=_clamp(data.get("active_end_hour"), 1, 24, 24),
            bot_browser=str(data.get("bot_browser")) if data.get("bot_browser") in {"chrome", "msedge"} else "chrome",
            apply_mode=str(data.get("apply_mode")) if data.get("apply_mode") in {"off", "dry_run", "auto"} else "off",
            apply_daily_cap=_clamp(data.get("apply_daily_cap"), 1, 500, 40),
            salary_floor=_clamp(data.get("salary_floor"), 0, 5_000_000, 80_000),
            salary_band_low=_clamp(data.get("salary_band_low"), 0, 100, 40),
            salary_band_high=max(_clamp(data.get("salary_band_high"), 0, 100, 55), _clamp(data.get("salary_band_low"), 0, 100, 40)),
            discovery_skip_below_floor=bool(data.get("discovery_skip_below_floor", True)),
            apply_skip_optional=bool(data.get("apply_skip_optional", True)),
            notify_reports=bool(data.get("notify_reports", True)),
            apply_facts=clean_facts(data.get("apply_facts")),
            form_questions=clean_form_questions(data.get("form_questions")),
            answer_prompt=str(data.get("answer_prompt") or "").strip(),
            notify_telegram_chat=str(data.get("notify_telegram_chat") or "").strip(),
        )
        if settings.api_key:
            settings.api_key_saved = True
        return settings


def _clamp(value: Any, low: int, high: int, default: int) -> int:
    try:
        return max(low, min(high, int(value)))
    except (TypeError, ValueError):
        return default


def _https_url(value: Any) -> str:
    text = str(value or "").strip()
    parts = urlsplit(text)
    return text if parts.scheme in {"http", "https"} and parts.hostname else ""


def _timezone_name(value: Any) -> str:
    name = str(value or "").strip() or "America/New_York"
    try:
        from zoneinfo import ZoneInfo

        ZoneInfo(name)
        return name
    except Exception:  # noqa: BLE001 - unknown zone name
        return "America/New_York"


def yes_no(value: Any) -> str:
    text = str(value or "").strip().lower()
    return {"yes": "Yes", "no": "No"}.get(text, "")


def _migrated_theme(data: dict[str, Any]) -> str:
    """The pre-redesign app defaulted to light; the redesign defaults to dark navy. Settings written
    before ``ui_revision`` 2 carry the old default, so they move to dark once (changeable in Settings)."""
    theme = str(data.get("theme") or "dark")
    if int(data.get("ui_revision") or 1) < 2 and theme == "light":
        return "dark"
    return theme


API_KEY_SECRET = "anthropic_api_key"


class SettingsStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (config_dir() / "settings.json")
        self.secret_path = self.path.with_name("credentials.dat")
        self.lock = AppLock(self.path.with_name("lock.json"))
        self._protected = ""  # encrypted prompt + profile as read from disk (kept verbatim while locked)
        self.settings = AppSettings()
        self.load()

    @property
    def data_locked(self) -> bool:
        """True when the prompt and personal info exist on disk but could not be decrypted (yet)."""
        return self.lock.enabled() and not self.lock.unlocked()

    def _unseal(self, raw: dict[str, Any]) -> None:
        """Merge the encrypted prompt and profile into the plain settings dict (when unlocked)."""
        self._protected = str(raw.get("protected") or "")
        if not self._protected or not self.lock.unlocked():
            return
        try:
            raw.update(json.loads(self.lock.open(self._protected)))
        except (LockedError, json.JSONDecodeError):
            pass

    def load(self) -> None:
        if not self.path.exists():
            if not self.settings.output_root:
                self.settings.output_root = str(default_output_root())
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        migrate_plaintext = False
        if isinstance(raw, dict) and int(raw.get("ui_revision") or 1) < 2:
            backup = self.path.with_name(self.path.name + ".pre-redesign.bak")
            if not backup.exists():  # one-time safety copy before the first rewrite by the new version
                try:
                    backup.write_bytes(self.path.read_bytes())
                except OSError:
                    pass
        if isinstance(raw, dict):
            self._unseal(raw)
            self.settings = AppSettings.from_dict(raw)
            migrate_plaintext = bool(self.settings.api_key.strip())
        if not self.settings.output_root:
            self.settings.output_root = str(default_output_root())
        if migrate_plaintext:
            self.save()  # moves the plaintext key into OS-backed storage and rewrites settings.json without it
        else:
            self.settings.api_key = load_secret(API_KEY_SECRET, self.secret_path)
            self.settings.api_key_saved = bool(self.settings.api_key)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self.settings.to_dict()
        key = payload.pop("api_key", "").strip()
        store_secret(API_KEY_SECRET, key, self.secret_path)  # OS-backed; never written to settings.json
        payload["api_key_saved"] = bool(key)
        self.settings.api_key_saved = bool(key)
        if self.lock.enabled():
            secret = {"prompt_template": payload.pop("prompt_template"), "profile": payload.pop("profile"),
                      "apply_facts": payload.pop("apply_facts"), "form_questions": payload.pop("form_questions")}
            if self.lock.unlocked():
                self._protected = self.lock.seal(json.dumps(secret))
            payload["protected"] = self._protected  # locked: the stored value is written back untouched
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash mid-save cannot truncate settings

    # ------------------------------------------------------------------ app password

    def enable_protection(self, password: str) -> None:
        """Set the app password; the prompt and personal info are stored encrypted from now on."""
        self.lock.create(password)
        self.save()

    def change_password(self, old: str, new: str) -> bool:
        if not self.lock.verify(old):
            return False
        self.lock.create(new)
        self.save()  # re-encrypts with the new key
        return True

    def disable_protection(self, password: str) -> bool:
        if not self.lock.verify(password):
            return False
        self.lock.unlock(password)
        self.load()  # make sure the plain values are in memory before the lock goes away
        self.lock.remove()
        self._protected = ""
        self.save()
        return True

    def reset_protection(self) -> None:
        """Forgotten password: drop the lock and wipe what it protected (prompt and personal info)."""
        self.lock.remove()
        self._protected = ""
        defaults = AppSettings()
        self.settings.profile = defaults.profile
        self.settings.prompt_template = defaults.prompt_template
        self.settings.apply_facts = {}
        self.settings.form_questions = []
        self.save()

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if key == "profile" and isinstance(value, dict):
                for pkey, pval in value.items():
                    if hasattr(self.settings.profile, pkey):
                        setattr(self.settings.profile, pkey, pval)
            elif hasattr(self.settings, key):
                setattr(self.settings, key, value)
        if self.settings.api_key.strip():
            self.settings.api_key_saved = True
        self.save()

    def snapshot(self) -> AppSettings:
        return deepcopy(self.settings)

    def has_api_key(self) -> bool:
        return bool(self.settings.api_key.strip())

    def uses_claude_chat(self) -> bool:
        return self.settings.generation_mode == GENERATION_CLAUDE_CHAT

    def generation_ready(self) -> bool:
        if self.uses_claude_chat():
            return True
        return self.has_api_key()

    @property
    def mode(self) -> str:
        return self.settings.app_mode

    @property
    def is_viewer(self) -> bool:
        return self.settings.app_mode == MODE_VIEWER

    def controller_token(self) -> str:
        """The shared secret a viewer sends to its controller (kept in OS-backed storage, never in settings.json)."""
        return load_secret(CONTROLLER_TOKEN_SECRET, self.secret_path)

    def set_controller_token(self, token: str) -> None:
        store_secret(CONTROLLER_TOKEN_SECRET, token.strip(), self.secret_path)

    def proxy_credentials(self) -> tuple[str, str]:
        """(user, password) for the web proxy, from OS-backed storage; ("", "") when the proxy needs none."""
        raw = load_secret(PROXY_SECRET, self.secret_path)
        user, _, password = raw.partition(":")
        return (user, password) if raw else ("", "")

    def set_proxy_credentials(self, user: str, password: str) -> None:
        store_secret(PROXY_SECRET, f"{user.strip()}:{password}" if user.strip() else "", self.secret_path)

    @property
    def chat_provider(self) -> str:
        return self.settings.chat_provider

    def notify_secret(self, name: str) -> str:
        """``discord_webhook`` or ``telegram_token`` (OS-backed storage, never in settings.json)."""
        return load_secret(f"notify_{name}", self.secret_path)

    def set_notify_secret(self, name: str, value: str) -> None:
        store_secret(f"notify_{name}", value.strip(), self.secret_path)
