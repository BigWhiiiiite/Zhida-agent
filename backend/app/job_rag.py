"""Small-corpus, evidence-grounded retrieval for campus job recommendations.

One project is a parent record, not a chunk. Its independently stated actions,
achievements, and technology claims become source-addressable evidence cards.
Dense retrieval discovers related wording; exact/FTS retrieval protects named
technologies. Neither a nearest neighbour nor an LLM may invent experience.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .job_models import (JobEvidenceExplanation, JobEvidenceMatch, JobPosting,
                         JobRecommendation, RecommendationBatch)
from .models import CandidateProfile
from .storage import get_rag_index, rag_fts_ranks, replace_rag_index


MODEL_NAME = "BAAI/bge-small-zh-v1.5"
MODEL_CACHE = Path(__file__).resolve().parents[1] / "data" / "embedding_models"
MAX_ENRICHED_JOBS = 60
MAX_REQUIREMENTS_PER_JOB = 12
MAX_CHUNKS = 120


@dataclass(frozen=True)
class EvidenceChunk:
    chunk_id: str
    source_kind: str
    source_title: str
    source_path: str
    quote: str
    embedding_text: str
    strength: float
    vector: tuple[float, ...] = ()


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def _facts(value: str) -> list[str]:
    """Split at factual sentence/bullet boundaries, never at arbitrary tokens."""
    lines = re.split(r"[\n\r]+|(?<=[。！？；;])", value)
    parts: list[str] = []
    for line in lines:
        line = re.sub(r"^[\s\-•·●\d.、）)]+", "", line).strip()
        if not line:
            continue
        # A very long list entry can contain several independent clauses. Keep
        # short action-method-result phrases intact while bounding model input.
        if len(line) > 320:
            clauses = re.split(r"(?<=[，,])", line)
            current = ""
            for clause in clauses:
                if current and len(current) + len(clause) > 240:
                    parts.append(_normalize_text(current))
                    current = ""
                current += clause
            if current.strip():
                parts.append(_normalize_text(current))
        else:
            parts.append(_normalize_text(line))
    return [part for part in parts if len(part) >= 5]


def split_profile_evidence(profile: CandidateProfile) -> list[EvidenceChunk]:
    cards: list[EvidenceChunk] = []
    for source_kind, records in (("project", profile.projects), ("internship", profile.internships)):
        collection = "projects" if source_kind == "project" else "internships"
        for record_index, record in enumerate(records):
            title = (record.name if source_kind == "project" else
                     " · ".join(filter(None, (record.organization, record.role)))) or f"{collection} {record_index + 1}"
            role = record.role.strip()
            pieces: list[tuple[str, int, str, float]] = []
            for fact_index, fact in enumerate(_facts(record.description)):
                pieces.append(("description", fact_index, fact, .9))
            if source_kind == "project":
                for fact_index, fact in enumerate(_facts(record.responsibilities)):
                    pieces.append(("responsibilities", fact_index, fact, .9))
            for item_index, achievement in enumerate(record.achievements):
                for fact_index, fact in enumerate(_facts(achievement)):
                    pieces.append((f"achievements[{item_index}]", fact_index, fact, 1.0))
            technologies = [item.strip() for item in record.technologies if item.strip()]
            if technologies:
                # A bare stack list is weaker evidence than a concrete action.
                pieces.append(("technologies", 0, "使用技术：" + "、".join(technologies), .55))
            seen: set[str] = set()
            for field_name, fact_index, quote, strength in pieces:
                normalized = re.sub(r"\W+", "", quote.casefold())
                if normalized in seen:
                    continue
                seen.add(normalized)
                source_path = f"{collection}[{record_index}].{field_name}[{fact_index}]"
                identity = f"{source_path}:{fact_index}:{quote}"
                chunk_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
                context = f"{title}；本人角色：{role}；" if role else f"{title}；"
                cards.append(EvidenceChunk(
                    chunk_id=chunk_id, source_kind=source_kind, source_title=title,
                    source_path=source_path, quote=quote,
                    embedding_text=(context + "真实经历：" + quote)[:550], strength=strength,
                ))
    return cards[:MAX_CHUNKS]


def _profile_hash(cards: list[EvidenceChunk]) -> str:
    payload = [(card.chunk_id, card.embedding_text, card.strength) for card in cards]
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def _unit(vector: Iterable[float]) -> tuple[float, ...]:
    values = tuple(float(value) for value in vector)
    length = math.sqrt(sum(value * value for value in values))
    if not length or not math.isfinite(length):
        raise ValueError("embedding 模型返回了无效向量")
    return tuple(value / length for value in values)


@lru_cache(maxsize=2)
def _local_embedder(model_name: str):
    from fastembed import TextEmbedding

    MODEL_CACHE.mkdir(parents=True, exist_ok=True)
    # Installation/prewarming is explicit; normal requests never download a
    # model or send private resume text to a third-party host.
    return TextEmbedding(model_name=model_name, cache_dir=str(MODEL_CACHE), local_files_only=True)


def _embed(texts: list[str], model_name: str) -> list[tuple[float, ...]]:
    if not texts:
        return []
    model = _local_embedder(model_name)
    return [_unit(vector) for vector in model.embed(texts, batch_size=32)]


def _indexed_chunks(profile: CandidateProfile, model_name: str) -> tuple[list[EvidenceChunk], bool]:
    cards = split_profile_evidence(profile)
    if not cards:
        return [], True
    profile_hash = _profile_hash(cards)
    stored = get_rag_index(profile_hash, model_name)
    if stored is not None and len(stored) == len(cards):
        vectors = {row["chunk_id"]: tuple(json.loads(row["vector_json"])) for row in stored}
        return [EvidenceChunk(**{**card.__dict__, "vector": vectors[card.chunk_id]})
                for card in cards], bool(vectors and next(iter(vectors.values())))

    try:
        vectors = _embed([card.embedding_text for card in cards], model_name)
        dense_ready = len(vectors) == len(cards)
    except (ImportError, OSError, RuntimeError, ValueError):
        vectors, dense_ready = [()] * len(cards), False
    if not dense_ready:
        # Still build the lexical index, but do not claim that embeddings ran.
        replace_rag_index(profile_hash, "keyword-only", [
            {**card.__dict__, "vector_json": "[]"} for card in cards
        ])
        return cards, False
    indexed = [EvidenceChunk(**{**card.__dict__, "vector": vector})
               for card, vector in zip(cards, vectors)]
    replace_rag_index(profile_hash, model_name, [
        {**card.__dict__, "vector_json": json.dumps(card.vector)} for card in indexed
    ])
    return indexed, True


def _term_in_text(term: str, text: str) -> bool:
    term = term.casefold().strip()
    lowered = text.casefold()
    if not term:
        return False
    if re.fullmatch(r"[a-z0-9+#.\- ]+", term):
        return bool(re.search(r"(?<![a-z0-9])" + re.escape(term) + r"(?![a-z0-9])", lowered))
    return term in lowered


def _lexical_score(requirement: str, card: EvidenceChunk,
                   aliases: dict[str, tuple[str, ...]], fts_rank: int | None) -> float:
    # Case-folding conflates the React UI library with the ReAct agent method.
    # The FTS trigram tokenizer is also case-insensitive, so these two need a
    # guarded literal/context match instead of a broad substring match.
    if requirement in ("React", "ReAct"):
        exact = bool(re.search(r"(?<![A-Za-z])" + requirement + r"(?![A-Za-z])", card.quote))
        specific_aliases = () if requirement == "React" else aliases.get("ReAct", ())
        return card.strength if exact or any(_term_in_text(term, card.quote) for term in specific_aliases) else 0.0
    terms = (requirement, *aliases.get(requirement, ()))
    if any(_term_in_text(term, card.quote) for term in terms):
        return 1.0 * card.strength
    if fts_rank:
        return min(.7, (.72 / math.sqrt(fts_rank))) * card.strength
    return 0.0


def _requirements(job: JobPosting) -> list[tuple[str, str]]:
    seen: set[str] = set()
    result: list[tuple[str, str]] = []
    for kind, values in (("required", job.required_skills), ("preferred", job.preferred_skills)):
        for raw in values:
            name = raw.strip()
            key = name.casefold()
            if name and key not in seen:
                seen.add(key)
                result.append((name, kind))
    return result[:MAX_REQUIREMENTS_PER_JOB]


def _dot(left: tuple[float, ...], right: tuple[float, ...]) -> float:
    return sum(a * b for a, b in zip(left, right)) if len(left) == len(right) else 0.0


def _best_evidence(requirement: str, kind: str, cards: list[EvidenceChunk],
                   vector: tuple[float, ...], aliases: dict[str, tuple[str, ...]]) -> JobEvidenceMatch | None:
    if not cards:
        return None
    ranks = rag_fts_ranks(requirement)
    lexical = {card.chunk_id: _lexical_score(requirement, card, aliases, ranks.get(card.chunk_id))
               for card in cards}
    semantic = {card.chunk_id: _dot(card.vector, vector) if vector and card.vector else 0.0
                for card in cards}
    lexical_order = [card.chunk_id for card in sorted(cards, key=lambda item: lexical[item.chunk_id], reverse=True)
                     if lexical[card.chunk_id] > 0]
    semantic_order = [card.chunk_id for card in sorted(cards, key=lambda item: semantic[item.chunk_id], reverse=True)
                      if vector and card.vector]
    lex_positions = {chunk_id: rank + 1 for rank, chunk_id in enumerate(lexical_order)}
    sem_positions = {chunk_id: rank + 1 for rank, chunk_id in enumerate(semantic_order)}
    named_technology = bool(re.fullmatch(r"[A-Za-z0-9+#.\-/ ]{2,40}", requirement))
    lex_weight, sem_weight = (1.35, 1.0) if named_technology else (1.0, 1.15)

    def fused(card: EvidenceChunk) -> float:
        lex = lex_weight / (10 + lex_positions[card.chunk_id]) if card.chunk_id in lex_positions else 0.0
        dense = sem_weight / (10 + sem_positions[card.chunk_id]) if card.chunk_id in sem_positions else 0.0
        return lex + dense

    for card in sorted(cards, key=fused, reverse=True):
        lex, sem = lexical[card.chunk_id], semantic[card.chunk_id]
        # RRF ranks candidates but is not an acceptance threshold. A related
        # vector alone must not be called proof of a named skill.
        support = "direct" if lex >= .5 else "related" if sem >= .5 else ""
        if not support:
            continue
        return JobEvidenceMatch(
            requirement=requirement, requirement_type=kind,
            evidence_id=card.chunk_id, source_kind=card.source_kind,
            source_title=card.source_title, source_path=card.source_path,
            quote=card.quote, support=support, lexical_score=round(lex, 3),
            semantic_score=round(sem, 3),
        )
    return None


def _evidence_for_job(item: JobRecommendation,
                      evidence_lookup: dict[tuple[str, str], JobEvidenceMatch | None]) -> JobRecommendation:
    requirements = _requirements(item.job)
    matches = [evidence_lookup[(requirement, kind)] for requirement, kind in requirements
               if evidence_lookup.get((requirement, kind)) is not None]
    direct = {evidence.requirement for evidence in matches if evidence.support == "direct"}
    required = item.job.required_skills
    preferred = item.job.preferred_skills
    missing = [skill for skill in required if skill not in direct]
    supported = [skill for skill in [*required, *preferred] if skill in direct]
    old_required = len(set(required) & set(item.matched_skills))
    old_preferred = len(set(preferred) & set(item.matched_skills))
    old_points = round(old_required / max(1, len(required)) * 35) + round(old_preferred / max(1, len(preferred)) * 15)
    new_points = round(len(set(required) & direct) / max(1, len(required)) * 35) + round(
        len(set(preferred) & direct) / max(1, len(preferred)) * 15)
    score = max(0, min(100, item.match_score - old_points + new_points))
    direct_matches = [match for match in matches if match.support == "direct"]
    reasons = [f"{match.requirement}：{match.source_title}中的经历“{match.quote[:70]}”"
               for match in direct_matches[:2]]
    if not reasons:
        reasons = ["主档案尚无可直接支持该岗位技能要求的项目或实习证据"]
    reasons.extend(reason for reason in item.reasons if reason.startswith((
        "目标岗位与", "工作地点", "毕业时间",
    )))
    if missing:
        reasons.append("待核实的岗位要求：" + "、".join(missing[:3]))
    return item.model_copy(update={
        "match_score": score, "matched_skills": supported, "missing_skills": missing,
        "evidence_matches": matches, "evidence_gaps": missing,
        "reasons": reasons,
        "queue_track": "stretch" if "算法" in item.job.title or "AIDU" in item.job.title or len(missing) >= 2 else "steady",
    })


def enrich_recommendation_batch(batch: RecommendationBatch, profile: CandidateProfile,
                                aliases: dict[str, tuple[str, ...]]) -> RecommendationBatch:
    """Enrich top candidates; keep official-site and eligibility gates unchanged."""
    model_name = os.getenv("APP_JOB_EMBEDDING_MODEL", MODEL_NAME).strip() or MODEL_NAME
    cards, dense_ready = _indexed_chunks(profile, model_name)
    if not cards:
        conservative = [_evidence_for_job(item, {}) for item in batch.jobs]
        return batch.model_copy(update={
            "engine": "evidence-rag-v1", "rag_status": "no_evidence",
            "jobs": conservative, "rag_enriched_jobs": len(conservative),
            "rag_model": model_name, "rag_message": "请先在主档案补充项目或实习的具体职责、技术和成果",
        })
    target = batch.jobs[:MAX_ENRICHED_JOBS]
    requirements = list(dict.fromkeys(
        name for item in target for name, _ in _requirements(item.job)
    ))
    vectors: dict[str, tuple[float, ...]] = {}
    if dense_ready and requirements:
        try:
            vectors = dict(zip(requirements, _embed(
                ["为这个句子生成表示以用于检索相关文章：" + name for name in requirements], model_name
            )))
        except (ImportError, OSError, RuntimeError, ValueError):
            dense_ready = False
    lookup = {(requirement, kind): _best_evidence(
        requirement, kind, cards, vectors.get(requirement, ()), aliases,
    ) for requirement, kind in dict.fromkeys(
        pair for item in target for pair in _requirements(item.job)
    )}
    enriched = [_evidence_for_job(item, lookup) for item in target]
    # Never leak legacy skill matches into the long tail that was not retrieved.
    remainder = [_evidence_for_job(item, {}) for item in batch.jobs[MAX_ENRICHED_JOBS:]]
    jobs = sorted([*enriched, *remainder],
                  key=lambda item: (item.formal_queue_eligible, item.match_score,
                                    item.job.source_status == "verified"), reverse=True)
    return batch.model_copy(update={
        "engine": "evidence-rag-bge-hybrid-v1" if dense_ready else "evidence-rag-keyword-v1",
        "jobs": jobs, "rag_status": "ready" if dense_ready else "keyword_only",
        "rag_model": model_name if dense_ready else "",
        "rag_evidence_count": len(cards), "rag_enriched_jobs": len(enriched),
        "rag_message": ("本机 embedding + 精确词/全文索引；每条理由可回溯到主档案"
                        if dense_ready else "本机 embedding 模型不可用，当前仅使用精确词/全文索引；未冒充向量匹配"),
    })


async def explain_evidence_matches(item: JobRecommendation) -> JobEvidenceExplanation:
    """On explicit user request, phrase only retrieved evidence through the LLM.

    The proxy can be unavailable, so a deterministic cited explanation is always
    available. This never modifies the recommendation score or eligibility gates.
    """
    direct = [match for match in item.evidence_matches if match.support == "direct"][:3]
    gaps = item.evidence_gaps[:4]
    if not direct:
        return JobEvidenceExplanation(
            job_id=item.job.id, status="no_evidence",
            summary="主档案中没有足以直接支持该岗位要求的项目或实习证据。",
            gaps=gaps,
        )
    local_reasons = [
        f"{match.requirement}：{match.source_title}——{match.quote[:130]}"
        for match in direct
    ]
    fallback = JobEvidenceExplanation(
        job_id=item.job.id, status="local_fallback",
        summary="以下依据来自主档案中的项目或实习原文；未证实的要求仍需人工核对。",
        supported_reasons=local_reasons, gaps=gaps,
        evidence_ids=[match.evidence_id for match in direct],
    )
    try:
        from agents import Agent, Runner
        from .model_provider import configured_model

        model, settings = configured_model(reasoning_effort="low", timeout_seconds=60)
        agent = Agent(
            name="Zhida evidence explainer",
            instructions=(
                "你是求职推荐解释器，只依据提供的岗位要求和简历原文证据写中文说明。"
                "不得编造技能、职责、量化结果、资格或证据 ID。"
                "返回严格 JSON：{\"summary\":字符串,\"reasons\":[{\"evidence_id\":字符串,\"reason\":字符串}]}。"
                "每个 reason 只解释其对应证据为何支持该要求，最多三条；不支持的要求不要写为优势。"
            ),
            model=model, model_settings=settings,
        )
        prompt = json.dumps({
            "job": {"title": item.job.title, "required_skills": item.job.required_skills,
                    "preferred_skills": item.job.preferred_skills},
            "retrieved_evidence": [match.model_dump() for match in direct],
            "unproven_requirements": gaps,
        }, ensure_ascii=False)
        result = await Runner.run(agent, prompt, max_turns=1)
        raw = str(result.final_output).strip()
        if raw.startswith("```"):
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=re.I)
        draft = json.loads(raw)
        allowed = {match.evidence_id: match for match in direct}
        reasons = []
        evidence_ids = []
        for claim in draft.get("reasons", [])[:3]:
            if not isinstance(claim, dict):
                continue
            evidence_id = str(claim.get("evidence_id", ""))
            reason = _normalize_text(str(claim.get("reason", "")))[:180]
            match = allowed.get(evidence_id)
            if match and reason and evidence_id not in evidence_ids:
                reasons.append(f"{reason}（依据：{match.source_title}——{match.quote[:110]}）")
                evidence_ids.append(evidence_id)
        if not reasons:
            return fallback
        return JobEvidenceExplanation(
            job_id=item.job.id, status="model",
            model=os.getenv("APP_AGENT_MODEL", ""),
            summary=_normalize_text(str(draft.get("summary", "")))[:240] or fallback.summary,
            supported_reasons=reasons, gaps=gaps, evidence_ids=evidence_ids,
        )
    except Exception:
        # Fail closed: show source-backed local wording, not a proxy traceback or
        # a fabricated model answer. The user can retry when the proxy recovers.
        return fallback
