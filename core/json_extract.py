"""Extract JSON resume objects from model text."""

from __future__ import annotations

import json
import re
from typing import Any

from core.claude_limits import MIN_RESUME_JSON_SNIPPET_CHARS


class JsonExtractError(ValueError):
    """Text did not contain a usable JSON object."""


def normalize_json_text(text: str) -> str:
    """Fix common Unicode quotes that break json.loads on scraped page text."""
    return (
        (text or "")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\ufeff", "")
    )


def _repair_json_text(blob: str) -> str:
    """Fix trailing commas and similar issues Claude sometimes emits."""
    out = blob
    for _ in range(3):
        fixed = re.sub(r",\s*([}\]])", r"\1", out)
        if fixed == out:
            break
        out = fixed
    return out


def extract_json_object(text: str) -> dict[str, Any]:
    cleaned = normalize_json_text((text or "").strip())
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", cleaned, re.I)
    if fence:
        cleaned = fence.group(1).strip()
    cleaned = _repair_json_text(cleaned)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            raise JsonExtractError("Response did not include a JSON object.")
        slice_text = _repair_json_text(cleaned[start : end + 1])
        try:
            data = json.loads(slice_text)
        except json.JSONDecodeError as exc:
            raise JsonExtractError(f"Could not parse JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JsonExtractError("JSON must be an object.")
    return data


def is_usable_resume(obj: dict[str, Any]) -> bool:
    """True when JSON looks like a generated resume, not a UI fragment or prompt snippet."""
    experience = obj.get("experience")
    if isinstance(experience, list) and len(experience) > 0:
        return True
    skill_groups = obj.get("skill_groups")
    if isinstance(skill_groups, list) and len(skill_groups) > 0:
        return True
    return False


def extract_usable_resume(text: str) -> dict[str, Any]:
    obj = extract_json_object(text)
    if not is_usable_resume(obj):
        raise JsonExtractError(
            "JSON is missing resume fields (experience or skill_groups). "
            "Copy the full Claude reply, not a snippet from the job description."
        )
    return obj


def find_resume_json_string(text: str) -> str:
    """Best-effort resume JSON string from Claude reply text (markdown, fences, prose)."""
    raw = normalize_json_text((text or "").strip())
    if not raw:
        return ""

    def try_blob(blob: str) -> str:
        blob = blob.strip()
        if len(blob) < MIN_RESUME_JSON_SNIPPET_CHARS:
            return ""
        try:
            obj = extract_json_object(blob)
        except JsonExtractError:
            return ""
        if not is_usable_resume(obj):
            return ""
        start = blob.find("{")
        end = blob.rfind("}")
        if start >= 0 and end > start:
            return blob[start : end + 1]
        return json.dumps(obj)

    best = ""
    for fence in re.finditer(r"```(?:json)?\s*([\s\S]*?)```", raw, re.I):
        hit = try_blob(fence.group(1))
        if len(hit) > len(best):
            best = hit

    hit = try_blob(raw)
    if len(hit) > len(best):
        best = hit

    decoder = json.JSONDecoder()
    idx = 0
    length = len(raw)
    while idx < length:
        brace = raw.find("{", idx)
        if brace < 0:
            break
        try:
            obj, end = decoder.raw_decode(raw, brace)
        except json.JSONDecodeError:
            idx = brace + 1
            continue
        if isinstance(obj, dict):
            slice_text = raw[brace:end]
            if is_usable_resume(obj) and len(slice_text) > len(best):
                best = slice_text
        idx = end if end > brace else brace + 1

    pos = 0
    while pos < len(raw):
        start = raw.find("{", pos)
        if start < 0:
            break
        depth = 0
        in_str = False
        esc = False
        end = -1
        for i in range(start, len(raw)):
            c = raw[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
                continue
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end < 0:
            break
        chunk = raw[start : end + 1]
        hit = try_blob(chunk)
        if len(hit) > len(best):
            best = hit
        pos = start + 1
    return best


def classify_claude_reply(text: str) -> str:
    """
    Classify scraped assistant text for debugging / user messaging.

    Returns one of: json_ok, prose_only, broken_json, empty, waiting.
    """
    raw = (text or "").strip()
    if not raw:
        return "empty"
    if find_resume_json_string(raw):
        return "json_ok"
    if len(raw) > 1500 and "{" not in raw:
        return "prose_only"
    if "{" in raw and (
        '"company_name"' in raw or '"skill_groups"' in raw or '"experience"' in raw
    ):
        return "broken_json"
    return "waiting"


_PLACEHOLDER = re.compile(
    r"\[\s*(add|insert|enter|fill|your|verified?|tbd|todo|placeholder|company|date|dates|name|title|role|"
    r"job title|employer|to be|n/a|xx)[^\]\n]{0,100}\]", re.I)


# Field descriptions copied from a prompt's schema ("hiring company from JD, or empty string if absent") are not values.
_SCHEMA_TEXT = re.compile(r"[^.\n]{0,40}\b(or empty string|if absent|if not present|if not available)\b[^.\n]{0,20}", re.I)


def find_placeholder(value: Any) -> str:
    """The first template-style placeholder ("[Insert verified dates]") or schema description in any text of a
    resume, or ''.

    A reply that still contains one was not finished, and exporting it would put that text on a resume."""
    if isinstance(value, str):
        m = _PLACEHOLDER.search(value) or _SCHEMA_TEXT.search(value)
        return m.group(0).strip() if m else ""
    if isinstance(value, dict):
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return ""
    for child in children:
        found = find_placeholder(child)
        if found:
            return found
    return ""
