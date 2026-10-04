"""Retrieve source-addressable experience facts; similarity is NOT permission to fill."""
from __future__ import annotations

import re

from .browser_models import BrowserSnapshot
from .job_rag import MODEL_NAME, _embed, _indexed_chunks
from .models import CandidateProfile


def _terms(text: str) -> set[str]:
    words = set(re.findall(r"[a-z][a-z0-9+#.-]+", text.casefold()))
    for part in re.findall(r"[\u4e00-\u9fff]+", text):
        words.update(part[i:i + 2] for i in range(len(part) - 1))
    return words


def retrieve_form_evidence(snapshot: BrowserSnapshot, profile: CandidateProfile) -> dict:
    queries = [field for field in snapshot.fields if re.search(
        r"项目|实习|工作经历|技术|技能|成果|优势|自我介绍|project|experience|skill",
        field.question_text or field.group_label or field.label, re.I)][:12]
    if not queries or not (profile.projects or profile.internships):
        return {"mode": "not-needed", "matches": []}
    cards, dense = _indexed_chunks(profile, MODEL_NAME)
    texts = [field.question_text or field.group_label or field.label for field in queries]
    try:
        vectors = _embed(texts, MODEL_NAME) if dense else []
    except (ImportError, OSError, RuntimeError, ValueError):
        vectors = []
    hybrid = len(vectors) == len(queries) and bool(vectors)
    matches = []
    for index, (field, query) in enumerate(zip(queries, texts)):
        terms = _terms(query)
        ranked = []
        for card in cards:
            # Do not answer internship questions with a similarly named project.
            if field.semantic_key.startswith("experience.") and card.source_kind != "internship":
                continue
            if field.semantic_key.startswith("project.") and card.source_kind != "project":
                continue
            source_kind = "项目经历" if card.source_kind == "project" else "实习工作经历"
            overlap = len(terms & _terms(source_kind + " " + card.embedding_text)) / max(1, len(terms))
            similarity = sum(a * b for a, b in zip(vectors[index], card.vector)) if hybrid else 0
            if overlap <= 0 and similarity < .55:
                continue
            score = .45 * overlap + .55 * max(0, similarity) if hybrid else overlap
            ranked.append((score, card))
        for score, card in sorted(ranked, key=lambda item: item[0], reverse=True)[:3]:
            matches.append({"selector": field.selector, "question": query,
                            "chunk_id": card.chunk_id, "source_path": card.source_path,
                            "source_title": card.source_title, "quote": card.quote,
                            "score": round(score, 4), "requires_grounding": True})
    return {"mode": "hybrid" if hybrid else "keyword-only", "matches": matches}
