"""Claude API client for tailored resume JSON generation."""

from __future__ import annotations

import json
from typing import Any

import httpx

from core.json_extract import JsonExtractError, extract_json_object
from core.prompt_builder import build_claude_chat_prompt
from core.resume_finalize import finalize_resume_object
from core.settings_store import AppSettings
from engine import ResumeError


class ClaudeError(RuntimeError):
    """Claude call or response could not be used."""


class ChatPageBlocked(ClaudeError):
    """The chat page needs a person: signed out, a human check, or a usage limit.

    The job fails so it can be retried by hand. It is not retried on its own, which would only hit the same page."""


class ClaudeClient:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings

    def validate_connection(self) -> tuple[bool, str]:
        key = self.settings.api_key.strip()
        if not key:
            return False, "No API key configured."
        try:
            with httpx.Client(timeout=20.0) as client:
                response = client.get(
                    "https://api.anthropic.com/v1/models",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                    },
                )
            if response.status_code == 200:
                return True, "Connected to Claude API."
            if response.status_code in {401, 403}:
                return False, "API key was rejected. Check the key in Settings."
            return False, f"Claude API returned HTTP {response.status_code}."
        except httpx.HTTPError as exc:
            return False, f"Could not reach Claude API: {exc}"

    def generate_resume(
        self,
        *,
        job_description: str,
        job_url: str = "",
        company: str = "",
        role_title: str = "",
    ) -> dict[str, Any]:
        key = self.settings.api_key.strip()
        if not key:
            raise ClaudeError("Claude API key is not configured.")

        user_message = build_claude_chat_prompt(
            self.settings,
            job_description=job_description,
            job_url=job_url,
            company=company,
            role_title=role_title,
        )
        body = {
            "model": self.settings.model,
            "max_tokens": 16384,
            "messages": [
                {
                    "role": "user",
                    "content": user_message,
                }
            ],
        }

        try:
            with httpx.Client(timeout=120.0) as client:
                response = client.post(
                    "https://api.anthropic.com/v1/messages",
                    headers={
                        "x-api-key": key,
                        "anthropic-version": "2023-06-01",
                        "content-type": "application/json",
                    },
                    json=body,
                )
        except httpx.HTTPError as exc:
            raise ClaudeError(f"Network error calling Claude: {exc}") from exc

        if response.status_code >= 400:
            detail = _safe_error(response)
            raise ClaudeError(f"Claude API error ({response.status_code}): {detail}")

        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise ClaudeError("Claude returned a non-JSON response.") from exc

        text = _extract_text(payload)
        try:
            resume_obj = extract_json_object(text)
        except JsonExtractError as exc:
            raise ClaudeError(str(exc)) from exc

        try:
            return finalize_resume_object(
                resume_obj,
                self.settings,
                company=company,
                role_title=role_title,
                job_url=job_url,
            )
        except ResumeError as exc:
            raise ClaudeError(str(exc)) from exc


def _extract_text(payload: dict[str, Any]) -> str:
    content = payload.get("content")
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(str(block.get("text") or ""))
        return "\n".join(parts).strip()
    if isinstance(content, str):
        return content.strip()
    raise ClaudeError("Claude response did not include text content.")


def _safe_error(response: httpx.Response) -> str:
    try:
        data = response.json()
        if isinstance(data, dict):
            err = data.get("error")
            if isinstance(err, dict):
                return str(err.get("message") or err)
            return str(data)[:400]
    except Exception:  # noqa: BLE001
        pass
    return (response.text or "")[:400]
