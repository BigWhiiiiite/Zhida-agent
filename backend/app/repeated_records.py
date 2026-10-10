"""Bind a repeated form group to one source record, never by list position."""
from __future__ import annotations

import re
import unicodedata

from .browser_models import BrowserSnapshot
from .field_semantics import semantic_key_for
from .models import CandidateProfile
from .form_field_policy import formal_employment_only


def _identity(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())


def resolve_repeated_records(snapshot: BrowserSnapshot, profile: CandidateProfile) -> dict:
    resolved = {}
    for kind, records, anchor in (("experience", profile.internships, "organization"),
                                  ("project", profile.projects, "name")):
        groups = {}
        for field in snapshot.fields:
            if (field.container_key and not formal_employment_only(field)
                    and semantic_key_for(field).startswith(kind + ".")):
                groups.setdefault(field.container_key, []).append(field)
        candidates = {}
        empty_groups = []
        for key, fields in groups.items():
            if not any(field.current_value.strip() and not (field.field_type in {'checkbox', 'radio'}
                       and field.current_value.strip().casefold() == 'false') for field in fields):
                empty_groups.append(key)
                continue
            hits = set()
            for field in fields:
                current = _identity(field.current_value)
                if not current:
                    continue
                semantic = semantic_key_for(field)
                for index, record in enumerate(records):
                    name = _identity(getattr(record, anchor))
                    if semantic == f"{kind}.{anchor}" and name and current == name:
                        hits.add(index)
                    if kind == "project":
                        # Only this group's value is evidence. Dates, role names
                        # and neighboring headings cannot identify a project.
                        if semantic == "project.description" and len(name) >= 8 and name in current:
                            hits.add(index)
                        urls = re.findall(r"https?://[^\s<>\"'，；。)）]+", field.current_value, re.I)
                        expected_urls = {_identity(url).rstrip("/") for url in
                                         (record.project_url, record.github_url) if url}
                        if any(_identity(url).rstrip("/") in expected_urls for url in urls):
                            hits.add(index)
            candidates[key] = hits
        # Reject duplicate claims as a whole; iteration order grants no priority.
        used = {index: [key for key, hits in candidates.items() if index in hits]
                for index in range(len(records))}
        bindings = {key: next(iter(hits)) for key, hits in candidates.items()
                    if len(hits) == 1 and len(used[next(iter(hits))]) == 1}
        remaining = set(range(len(records))) - set(bindings.values())
        unresolved_nonempty = set(candidates) - set(bindings)
        if len(empty_groups) == 1 and len(remaining) == 1 and not unresolved_nonempty:
            bindings[empty_groups[0]] = next(iter(remaining))
        for key, index in bindings.items():
            for field in groups[key]:
                resolved[field.selector] = (kind, records[index])
    return resolved


def repeated_value(binding: tuple, semantic_key: str) -> str:
    kind, record = binding
    attribute = semantic_key.removeprefix(kind + ".")
    if kind == 'project' and attribute == 'responsibilities' and not record.responsibilities:
        # Verbatim, own-record evidence only. Team project outcomes/metrics
        # cannot silently become the candidate's personal responsibilities.
        sentences = re.split(r'(?<=[。！？])|\n', record.description)
        explicit = [s.strip() for s in sentences if re.match(r'^(?:本人)?负责', s.strip())]
        if explicit:
            return '\n'.join(explicit)
        if record.role.strip() in {'个人项目', '独立开发', '独立开发者', '个人独立完成'}:
            return record.description.strip()
        return ''
    if attribute == "description":
        parts = [getattr(record, "background", ""), record.description, *record.achievements]
        return "\n".join(dict.fromkeys(part.strip() for part in parts if part.strip()))
    return str(getattr(record, attribute, "") or "")
