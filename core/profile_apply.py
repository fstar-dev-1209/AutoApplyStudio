"""Push Settings profile into the exporter defaults."""

from __future__ import annotations

from core.settings_store import AppSettings, ProfileSettings
from engine import set_profile_defaults


def apply_settings_profile(settings: AppSettings | ProfileSettings) -> None:
    profile = settings.profile if isinstance(settings, AppSettings) else settings
    set_profile_defaults(
        name=profile.name,
        email=profile.email,
        phone=profile.phone,
        location=profile.location,
        education_school=profile.education_school,
        education_location=profile.education_location,
        education_degree=profile.education_degree,
        education_dates=profile.education_dates,
    )
