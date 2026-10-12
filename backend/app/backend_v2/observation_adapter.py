"""Pure bridge from the existing browser scanner to D2 recognition evidence.

No browser is opened, no menu is clicked, and no model is called here.  The
scanner's question ownership, option completeness and record evidence are
preserved for the normalizer to verify, not upgraded into trusted facts.

This is data minimization, NOT a claim that the artifact is anonymous.  Page
captions/context/URLs and the narrowly retained record anchors can contain
personal information.  Store it as tenant-scoped sensitive evidence; do not
put it in logs/checkpoints or send it to a model without the relevant consent.
"""
from __future__ import annotations

import json
import math
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from pydantic import ValidationError

from ..browser_models import BrowserSnapshot
from .form_recognition import MAX_FIELDS


MAX_OBSERVATION_BYTES = 2_000_000
MAX_OBSERVED_FIELDS = MAX_FIELDS
MAX_JSON_NODES = 60_000
MAX_JSON_DEPTH = 16
MAX_JSON_STRING = 65_536

_SNAPSHOT_KEYS = {
    "session_id", "url", "title", "browser_engine", "recognition_profile",
    "fields", "context_fields", "extraction_report",
}
_FIELD_EXCLUDED = {
    "current_value", "knowledge_profile_path", "knowledge_id", "knowledge_block_reason",
}
# These are evidence of which visible repeated record this is, not authority to
# bind it to a resume entry.  Recognition must still prove the owned question
# and the DOM record boundary; matching must find exactly one resume entity.
_RECORD_ANCHORS = {
    "education.school", "education.degree", "education.academic_degree",
    "education.major", "education.college",
    "experience.organization", "experience.title", "project.name", "language.name",
}
_CREDENTIALS = re.compile(
    r"password|passcode|captcha|verification|one[-_ ]time[-_ ]code|验证码|密码|\botp\b",
    re.IGNORECASE,
)
_OWNED_SOURCES = {"explicit", "aria-labelledby", "container-owned", "aria",
                  "ant-resume-owned", "autohome-owned", "phoenix-owned"}


def _owned_anchor_question(field: Any) -> bool:
    """Minimize value retention; this still does not establish record identity."""
    def identity(value: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value)).strip(" *:：").casefold()

    question = identity(field.question_text or field.label)
    if not question or question in {"是", "否", "男", "女", "请选择", "请输入"}:
        return False
    if field.question_candidates:
        # Preserve ALL candidates elsewhere, but do not retain a personal value
        # when even one owned candidate contradicts the visible question.
        owned = [item for item in field.question_candidates if item.owned]
        return bool(owned and all(item.source in _OWNED_SOURCES for item in owned)
                    and {identity(item.text) for item in owned} == {question})
    return field.label_source in _OWNED_SOURCES


def _check_json_size(value: Any) -> None:
    """Reject incomplete/oversized evidence rather than silently truncate it."""
    remaining = MAX_JSON_NODES

    def visit(item: Any, depth: int) -> None:
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > MAX_JSON_DEPTH:
            raise ValueError("observation structure exceeds the bounded evidence limit")
        if item is None or isinstance(item, (bool, int)):
            return
        if isinstance(item, float):
            if not math.isfinite(item):
                raise ValueError("observation must contain finite JSON values")
            return
        if isinstance(item, str):
            if len(item) > MAX_JSON_STRING:
                raise ValueError("observation text exceeds the bounded evidence limit")
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if isinstance(item, Mapping):
            for key, child in item.items():
                if not isinstance(key, str):
                    raise ValueError("observation object keys must be strings")
                visit(key, depth + 1)
                visit(child, depth + 1)
            return
        raise ValueError("observation must contain only JSON data")

    visit(value, 0)
    try:
        size = len(json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8"))
    except (TypeError, ValueError, UnicodeError):
        raise ValueError("observation must contain valid bounded JSON data") from None
    if size > MAX_OBSERVATION_BYTES:
        raise ValueError("observation exceeds the bounded evidence size limit")


def _typed_options(values: Any) -> list | None:
    """Accept explicit richer option evidence without inventing native values.

    The legacy scanner emits label strings.  An explicitly enriched reader may
    also report value/disabled metadata; losing disabled=True is never safe.
    """
    if not isinstance(values, list) or not any(isinstance(item, dict) for item in values):
        return None
    options = []
    for item in values:
        if isinstance(item, str):
            options.append(item)
            continue
        if not isinstance(item, dict):
            raise ValueError("invalid typed option evidence")
        label = item.get("label", item.get("text"))
        if not isinstance(label, str) or not label.strip():
            raise ValueError("invalid typed option label")
        result = {"label": label}
        if "value" in item:
            if not isinstance(item["value"], str):
                raise ValueError("typed option value must be explicit text")
            result["value"] = item["value"]
        if "disabled" in item:
            if not isinstance(item["disabled"], bool):
                raise ValueError("typed option disabled state must be boolean")
            result["disabled"] = item["disabled"]
        options.append(result)
    return options


def adapt_browser_snapshot(snapshot: BrowserSnapshot | Mapping[str, Any]) -> dict[str, Any]:
    """Copy a real ``BrowserSnapshot.model_dump()`` into recognition input.

    Browser session ownership, run revision, capture freshness and URL/window
    epoch authorization are NOT established by this adapter.  The enclosing
    ingestion service must validate those independently against its database.
    A ``session_id`` from the scanner must never select a different V2 session.

    Screenshots/accessibility observations are not part of BrowserSnapshot.
    Their mere presence as extra keys cannot manufacture OCR text, evidence
    references, consent, or a verified title.  A later explicit region-evidence
    bridge must bind them to this document and an exact collected target.
    """
    if isinstance(snapshot, BrowserSnapshot):
        raw = snapshot.model_dump(mode="json")
    elif isinstance(snapshot, Mapping):
        raw = dict(snapshot)
    else:
        raise ValueError("expected BrowserSnapshot or its JSON object")

    for name in ("fields", "context_fields"):
        fields = raw.get(name, [])
        if not isinstance(fields, list) or len(fields) > MAX_OBSERVED_FIELDS:
            raise ValueError("observation field list exceeds the bounded evidence limit")
    if len(raw.get("fields", [])) + len(raw.get("context_fields", [])) > MAX_OBSERVED_FIELDS:
        raise ValueError("observation field list exceeds the bounded evidence limit")
    _check_json_size(raw)
    # Keep the existing model contract, with two evidence-only extensions.  No
    # arbitrary model output or caller-provided executable target is accepted.
    extensions = {}
    for name in ("fields", "context_fields"):
        rows = []
        for index, original in enumerate(raw.get(name, [])):
            if not isinstance(original, dict):
                raise ValueError("invalid browser observation field")
            item, extra = dict(original), {}
            options = _typed_options(item.get("options"))
            if options is not None:
                extra["options"] = options
                item["options"] = [option["label"] if isinstance(option, dict) else option for option in options]
            if "disabled" in item:
                if not isinstance(item["disabled"], bool):
                    raise ValueError("control disabled state must be boolean")
                extra["disabled"] = item["disabled"]
            extensions[(name, index)] = extra
            rows.append(item)
        raw[name] = rows
    try:
        parsed = BrowserSnapshot.model_validate(raw)
    except ValidationError:
        # Pydantic errors can include raw applicant values; never propagate them
        # to an API error or a worker log.
        raise ValueError("invalid browser observation evidence schema") from None

    result = parsed.model_dump(mode="json", include=_SNAPSHOT_KEYS)
    for name in ("fields", "context_fields"):
        projected = []
        for index, field in enumerate(getattr(parsed, name)):
            item = field.model_dump(mode="json", exclude=_FIELD_EXCLUDED)
            item.update(extensions[(name, index)])
            credential_context = " ".join((field.label, field.question_text,
                                             field.name, field.autocomplete))
            if (field.semantic_key in _RECORD_ANCHORS
                    and field.field_type not in {"password", "hidden", "file", "section-button"}
                    and not _CREDENTIALS.search(credential_context)
                    and _owned_anchor_question(field)
                    and field.current_value):
                item["current_value"] = field.current_value
            projected.append(item)
        result[name] = projected
    return result
