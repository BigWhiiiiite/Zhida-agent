"""Reuse a confirmed ATS choice without rewriting an education record.

The caller MUST supply the current compose_task_profile result: that function
filters memory to this user's selected CV and company. This helper never loads
global/master memory, reads storage, alters a profile, or infers a missing answer.
"""
from __future__ import annotations

from .browser_models import PageField
from .field_semantics import (education_level_hint, entity_scope_for, field_text,
                              normalize_text, option_fingerprint, semantic_key_for)
from .models import CandidateProfile, Education


EXPLICIT_EDUCATION_SCOPES = {
    "education:high_school", "education:associate", "education:bachelor",
    "education:master", "education:doctorate",
}
CHOICE_TYPES = {"combobox", "select-one", "select-multiple", "radio"}


def confirmed_education_choice(field: PageField, profile: CandidateProfile,
                               *, resolved_scope: str = "") -> tuple[str, str]:
    """Return a real option only after strict same-question identity checks.

    ``resolved_scope`` may come from an already verified education-group
    resolution, never from list order or a model-proposed scope. A conflicting
    explicit page scope or multiple records of the same degree fails closed.
    """
    from .form_agent import SENSITIVE_HINTS, THIRD_PARTY_HINTS

    key = semantic_key_for(field)
    if (not key.startswith("education.") or key.removeprefix("education.") not in Education.model_fields
            or field.field_type not in CHOICE_TYPES or field.multiple or not field.options):
        return "", ""
    text = field_text(field)
    if field.knowledge_block_reason or any(hint in text for hint in (*SENSITIVE_HINTS, *THIRD_PARTY_HINTS)):
        return "", ""
    page_scope = entity_scope_for(field, key)
    scope = resolved_scope or page_scope
    if scope not in EXPLICIT_EDUCATION_SCOPES or page_scope not in {scope, "education:unspecified"}:
        return "", ""
    records = [record for record in profile.education
               if "education:" + education_level_hint(record.degree) == scope]
    if len(records) != 1:
        return "", ""
    question = normalize_text(field.question_text or field.group_label or field.label)
    fingerprint = option_fingerprint(field.options)
    if not question or not fingerprint:
        return "", ""
    # Refuse an unfiltered multi-task collection. A singleton task collection is
    # still required to come from compose_task_profile, not directly from storage.
    if len({(item.resume_id, item.company_scope) for item in profile.application_answer_memory}) > 1:
        return "", ""
    matches = [item for item in profile.application_answer_memory
               if item.semantic_key == key and item.entity_scope == scope
               and item.normalized_question == question
               and item.option_fingerprint == fingerprint
               and item.field_type in CHOICE_TYPES
               and item.confirmed_count > 0]
    if not matches:
        return "", ""
    # A changed selector/signature need not erase confirmed semantics, but it
    # also must not override newer user correction for the same scoped question.
    newest_time = max(item.updated_at for item in matches)
    latest = [item for item in matches if item.updated_at == newest_time]
    values = {normalize_text(item.value) for item in latest}
    if len(values) != 1 or not next(iter(values)):
        return "", ""
    options = [option for option in field.options if normalize_text(option) in values]
    if len(options) != 1:
        return "", ""
    return options[0], "已确认答案记忆"
