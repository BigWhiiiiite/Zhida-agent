"""User-confirmed application knowledge, separate from resume evidence RAG.

Only exact, scoped mappings can identify a profile field. Hybrid neighbours and
company rules are reference material, never authority to fill or submit a form.
No applicant answer values or remote embedding requests are stored here.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import storage
from .browser_models import BrowserSnapshot, PageField
from .field_semantics import entity_scope_for, normalize_text, semantic_key_for
from .job_rag import MODEL_NAME, _embed


MAX_RECORDS = 400
MAX_MATCHES_PER_FIELD = 3
EDUCATION_SCOPES = {f"education:{level}" for level in
                    ("high_school", "associate", "bachelor", "master", "doctorate")}


class MappingTarget(BaseModel):
    path: str
    label: str
    semantic_key: str
    requires_entity_scope: bool = False


_ROOT_TARGETS = (
    ("name", "姓名", "candidate.name"),
    ("english_name", "英文姓名", "candidate.english_name"),
    ("age", "年龄（已提供的周岁）", "candidate.age"),
    ("birth_date", "出生日期", "candidate.birth_date"),
    ("gender", "性别", "candidate.gender"),
    ("phone", "本人手机号码", "candidate.phone"),
    ("email", "本人电子邮箱", "candidate.email"),
    ("country_region", "国家/地区", "candidate.country_region"),
    ("nationality", "国籍", "candidate.nationality"),
    ("qq", "QQ 号", "candidate.qq"),
    ("wechat", "微信号", "candidate.wechat"),
    ("location", "当前所在地", "candidate.current_location"),
    ("hukou_location", "户籍所在地", "candidate.hukou_location"),
    ("address", "通讯地址", "candidate.address"),
    ("website", "个人网站/作品集", "candidate.website"),
    ("github", "GitHub", "candidate.github"),
    ("linkedin", "LinkedIn", "candidate.linkedin"),
    ("target_cities", "期望工作城市", "preference.work_location"),
    ("preferred_business_groups", "意向事业群", "preference.business_group"),
    ("interview_preferences", "面试城市/方式", "preference.interview_location"),
    ("willing_to_relocate", "是否接受城市调剂", "preference.relocation"),
    ("campus_candidate_type", "应届生类型", "candidate.campus_type"),
    ("skills", "技能", "candidate.skills"),
    ("languages", "语言能力", "candidate.languages"),
)
_EDUCATION_TARGETS = (
    ("school", "学校全称"), ("college", "学院/院系"), ("major", "专业"),
    ("degree", "学历/学位"), ("study_mode", "培养/学习方式"),
    ("academic_system", "学制"), ("student_id", "学号"), ("advisor", "导师"),
    ("laboratory", "实验室"), ("research_direction", "研究方向"),
    ("location", "学校所在地"), ("start_date", "入学时间"),
    ("end_date", "毕业时间"), ("gpa", "绩点"), ("ranking", "成绩排名"),
)
_TARGETS = [MappingTarget(path=path, label=label, semantic_key=key)
            for path, label, key in _ROOT_TARGETS] + [
    MappingTarget(path=f"education.{path}", label=f"教育经历 · {label}",
                  semantic_key=f"education.{path}", requires_entity_scope=True)
    for path, label in _EDUCATION_TARGETS
]
_TARGET_BY_PATH = {target.path: target for target in _TARGETS}
_UNSAFE = re.compile(
    r"密码|口令|验证码|校验码|身份证|证件号|护照|银行卡|紧急联系人|紧急联络人|"
    r"推荐人|证明人|监护人|隐私政策|隐私协议|用户协议|授权协议|同意.{0,12}(?:条款|协议|隐私)|"
    r"password|passcode|one.time.password|verification.code|captcha|national.id|passport|"
    r"emergency.contact|referee|guardian|recommender|privacy.policy|terms.of|consent",
    re.IGNORECASE,
)
_AMBIGUOUS_QUESTION = {"是", "否", "男女", "男", "女", "yes", "no", "yesno", "选项", "请选择", "选择", "其他"}


def mapping_targets() -> list[MappingTarget]:
    return [target.model_copy() for target in _TARGETS]


def _meaningful_question(value: str) -> bool:
    normalized = normalize_text(value)
    return len(normalized) >= 2 and normalized not in _AMBIGUOUS_QUESTION and not normalized.isdigit()


def _parsed_url(value: str):
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("请填写不含账号密码的完整招聘网页 http(s) 网址")
    if any(ord(char) < 32 for char in value):
        raise ValueError("招聘网址含有无效字符")
    return parsed


def company_scope(source_url: str) -> str:
    """Derive tenant boundaries before removing query/fragment from stored URLs.

    Known shared ATS hosts require a tenant in the URL. Unknown URL layouts use
    an exact-page hash (including query/fragment), sacrificing recall for safety.
    No ownership type (state/private/foreign) participates in routing.
    """
    parsed = _parsed_url(source_url)
    host = (parsed.hostname or "").lower()
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    if any(part in {".", ".."} or "/" in part or "\\" in part for part in parts):
        raise ValueError("招聘网址路径不明确，不能确定公司范围")
    if host == "join.qq.com":
        return f"host:{host}"
    if (host == "mokahr.com" or host.endswith(".mokahr.com") or
            host == "moka.com" or host.endswith(".moka.com")):
        if len(parts) >= 2 and parts[0] in {"campus-recruitment", "social-recruitment"}:
            return f"tenant:{host}:moka:{parts[1]}"
    if host in {"jobs.lever.co", "jobs.eu.lever.co"} and parts:
        return f"tenant:{host}:lever:{parts[0]}"
    if host in {"boards.greenhouse.io", "job-boards.greenhouse.io", "job-boards.eu.greenhouse.io"}:
        if parts and parts[0] != "embed":
            return f"tenant:{host}:greenhouse:{parts[0]}"
        company = parse_qs(parsed.query).get("for", [""])[0]
        if company:
            return f"tenant:{host}:greenhouse:{company}"
    if host.endswith(".myworkdayjobs.com") and parts:
        candidates = parts[1:] if re.fullmatch(r"[a-z]{2}-[A-Z]{2}", parts[0]) else parts
        if candidates:
            return f"tenant:{host}:workday:{candidates[0]}"
    # Never assume a generic shared hostname is a company. Hashing avoids
    # persisting tokens that may be present in an unrecognised site's query.
    suffix = hashlib.sha256((parsed.query + "#" + parsed.fragment).encode()).hexdigest()[:20]
    authority = f"{host}:{parsed.port}" if parsed.port else host
    return f"page:{authority}:{parsed.path or '/'}:{suffix}"


def _safe_source_url(value: str) -> str:
    parsed = _parsed_url(value)
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", "", ""))


class KnowledgeCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    kind: Literal["mapping", "rule"] = "mapping"
    question: str = Field(min_length=2, max_length=500)
    aliases: list[str] = Field(default_factory=list, max_length=12)
    source_url: str = Field(min_length=1, max_length=2000)
    section: str = Field(default="", max_length=300)
    profile_path: str = Field(default="", max_length=80)
    entity_scope: str = Field(default="", max_length=100)
    field_signature: str = Field(default="", max_length=100)
    field_type: str = Field(default="", max_length=60)
    note: str = Field(default="", max_length=2000)
    confirmed: bool = False
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def validate_knowledge(self):
        _parsed_url(self.source_url)
        if not _meaningful_question(self.question):
            raise ValueError("请填写完整问题，不能只记录“是”“否”或选项文字")
        if any(not _meaningful_question(alias) or len(alias) > 500 for alias in self.aliases):
            raise ValueError("问题别名也必须是完整问题，最长 500 字")
        if not self.confirmed:
            raise ValueError("需本人核对网页问题和主档案对应关系，再确认保存")
        if self.expires_at is not None and self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=timezone.utc)
        if self.kind == "rule":
            if not self.note:
                raise ValueError("参考规则需要说明内容")
            if self.profile_path or self.entity_scope:
                raise ValueError("参考规则不能绑定档案字段，也不会直接触发填写")
            return self
        target = _TARGET_BY_PATH.get(self.profile_path)
        if target is None:
            raise ValueError("只能选择白名单中的主档案字段，不支持数组序号、证件或账号凭据")
        if _UNSAFE.search(" ".join((self.question, *self.aliases, self.section))):
            raise ValueError("凭据、证件、第三方联系人和法律同意项不能学习为自动映射")
        if target.requires_entity_scope:
            if self.entity_scope not in EDUCATION_SCOPES:
                raise ValueError("教育映射必须明确属于本科、硕士或其他具体学历，不能按数组顺序匹配")
        elif self.entity_scope not in {"", "candidate", "preference", "application"}:
            raise ValueError("该档案字段不能绑定到教育或第三方经历范围")
        return self


class KnowledgeRecord(KnowledgeCreate):
    id: str
    site_scope: str
    semantic_key: str = ""
    created_at: datetime
    updated_at: datetime


class KnowledgeMatch(BaseModel):
    record: KnowledgeRecord
    score: float = Field(ge=0, le=1)
    exact: bool = False
    usable: bool = False
    reason: str
    retrieval_mode: Literal["hybrid", "keyword-only"] = "keyword-only"


def initialize() -> None:
    with storage._connection() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS application_knowledge (
            id TEXT PRIMARY KEY, user_id TEXT NOT NULL, site_scope TEXT NOT NULL,
            record_json TEXT NOT NULL, embedding_text TEXT NOT NULL,
            vector_model TEXT NOT NULL DEFAULT '', vector_json TEXT NOT NULL DEFAULT '[]',
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE INDEX IF NOT EXISTS application_knowledge_owner_scope
                     ON application_knowledge(user_id, site_scope)""")


def list_knowledge() -> list[KnowledgeRecord]:
    user_id = storage.current_user_id()
    initialize()
    with storage._connection() as conn:
        rows = conn.execute("SELECT record_json FROM application_knowledge WHERE user_id=? ORDER BY updated_at DESC",
                            (user_id,)).fetchall()
    return [KnowledgeRecord.model_validate_json(row["record_json"]) for row in rows]


def _document(record: KnowledgeRecord) -> str:
    # Only questions and schema names are embedded, never candidate answers.
    text = "；".join(filter(None, (record.question, *record.aliases, record.section,
                                 record.entity_scope, record.profile_path)))
    if record.kind == "rule":
        text += "；" + record.note[:800]
    return text[:2000]


def save_knowledge(payload: KnowledgeCreate) -> KnowledgeRecord:
    user_id = storage.current_user_id()
    initialize()
    scope = company_scope(payload.source_url)
    now = datetime.now(timezone.utc)
    fields = payload.model_dump()
    fields["source_url"] = _safe_source_url(payload.source_url)
    target = _TARGET_BY_PATH.get(payload.profile_path)
    record = KnowledgeRecord(**fields, id=str(uuid4()), site_scope=scope,
                             semantic_key=target.semantic_key if target else "",
                             created_at=now, updated_at=now)
    with storage._connection() as conn:
        rows = conn.execute("SELECT record_json FROM application_knowledge WHERE user_id=? AND site_scope=?",
                            (user_id, scope)).fetchall()
        for row in rows:
            previous = KnowledgeRecord.model_validate_json(row["record_json"])
            # Update only the same target. A competing target remains a second
            # document so retrieval can explicitly stop on the contradiction.
            keys = ("kind", "section", "profile_path", "entity_scope", "field_signature", "field_type")
            if (normalize_text(previous.question) == normalize_text(record.question)
                    and all(getattr(previous, key) == getattr(record, key) for key in keys)):
                record.id, record.created_at = previous.id, previous.created_at
                break
        if record.created_at == now:
            total = conn.execute("SELECT COUNT(*) FROM application_knowledge WHERE user_id=?", (user_id,)).fetchone()[0]
            if total >= MAX_RECORDS:
                raise ValueError(f"最多保存 {MAX_RECORDS} 条知识，请先删除过期或错误记录")
        conn.execute("""INSERT INTO application_knowledge
            (id,user_id,site_scope,record_json,embedding_text,updated_at) VALUES (?,?,?,?,?,?)
            ON CONFLICT(id) DO UPDATE SET record_json=excluded.record_json,
                embedding_text=excluded.embedding_text, vector_json='[]', vector_model='',
                updated_at=excluded.updated_at""",
            (record.id, user_id, scope, record.model_dump_json(), _document(record), now.isoformat()))
    return record


def delete_knowledge(record_id: str) -> bool:
    user_id = storage.current_user_id()
    initialize()
    with storage._connection() as conn:
        cursor = conn.execute("DELETE FROM application_knowledge WHERE id=? AND user_id=?", (record_id, user_id))
    return cursor.rowcount > 0


def _tokens(text: str) -> Counter[str]:
    tokens = re.findall(r"[a-z0-9_]+", text.casefold())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(phrase[index:index + 2] for index in range(max(1, len(phrase) - 1)))
    return Counter(tokens)


def _lexical_scores(query: str, documents: list[str]) -> list[float]:
    """Small-corpus BM25 with Chinese bigrams and exact Latin identifiers."""
    corpus = [_tokens(document) for document in documents]
    terms = set(_tokens(query))
    if not corpus or not terms:
        return [0.0] * len(corpus)
    average = sum(sum(item.values()) for item in corpus) / len(corpus) or 1
    document_frequency = Counter(term for item in corpus for term in item)
    scores = []
    for document in corpus:
        score = 0.0
        for term in terms:
            frequency = document[term]
            if frequency:
                inverse = math.log(1 + (len(corpus) - document_frequency[term] + .5) /
                                   (document_frequency[term] + .5))
                score += inverse * (frequency * 2.2) / (frequency + 1.2 *
                         (.25 + .75 * sum(document.values()) / average))
        scores.append(score)
    return scores


def _vectors(user_id: str, rows, queries: list[str]) -> tuple[list[tuple[float, ...]], list[tuple[float, ...]], bool]:
    try:
        vectors = [tuple(json.loads(row["vector_json"])) if row["vector_model"] == MODEL_NAME else () for row in rows]
        missing = [index for index, vector in enumerate(vectors) if not vector]
        values = _embed([rows[index]["embedding_text"] for index in missing] + queries, MODEL_NAME)
        if len(values) != len(missing) + len(queries) or any(not vector for vector in values):
            raise ValueError("embedding 结果不完整")
        with storage._connection() as conn:
            for index, vector in zip(missing, values[:len(missing)]):
                vectors[index] = vector
                conn.execute("""UPDATE application_knowledge SET vector_model=?, vector_json=?
                    WHERE id=? AND user_id=? AND updated_at=?""",
                    (MODEL_NAME, json.dumps(vector), rows[index]["id"], user_id, rows[index]["updated_at"]))
        return vectors, values[len(missing):], True
    except (ImportError, OSError, RuntimeError, ValueError):
        return [()] * len(rows), [()] * len(queries), False


def _field_question(field: PageField) -> str:
    return field.question_text or field.group_label or field.label


def _eligibility(record: KnowledgeRecord, field: PageField) -> tuple[bool, bool, str]:
    question = _field_question(field)
    section = field.section or " > ".join(field.section_path)
    exact_question = _meaningful_question(question) and normalize_text(question) in {
        normalize_text(record.question), *(normalize_text(alias) for alias in record.aliases)}
    exact_signature = bool(record.field_signature and record.field_signature == field.field_signature)
    exact = bool(exact_question or exact_signature)
    if record.section and normalize_text(record.section) != normalize_text(section):
        return False, False, "所在区块不同，仅供参考"
    if record.field_type and record.field_type != field.field_type:
        return False, False, "控件类型不同，仅供参考"
    key = semantic_key_for(field)
    scope = entity_scope_for(field, key)
    if record.entity_scope in EDUCATION_SCOPES and scope in EDUCATION_SCOPES and scope != record.entity_scope:
        # The same caption in two explicit degree blocks is not a conflict:
        # that other block's knowledge must not disable a correct local match.
        return False, False, "学历范围不同，禁止跨本科/硕士借用"
    if record.expires_at and record.expires_at <= datetime.now(timezone.utc):
        return exact, False, "知识已过期，请重新核对"
    if record.kind == "rule":
        return exact, False, "公司填写规则仅供参考，不能据此推断用户答案或执行操作"
    if _UNSAFE.search(" ".join((question, section, field.name))) or key.startswith("third_party."):
        return exact, False, "敏感信息、第三方联系人或法律同意项不允许自动映射"
    if record.entity_scope in EDUCATION_SCOPES:
        if scope != record.entity_scope and not exact_signature:
            return exact, False, "该题的学历范围尚不明确，请人工绑定具体经历"
    elif scope.startswith(("education:", "experience:", "project:", "third_party")):
        return exact, False, "题目对象与候选人基础资料不同，请重新确认"
    if not exact:
        return False, False, "语义相似不代表同一道题，需要人工确认映射"
    if not _meaningful_question(question) and not (exact_signature and record.field_type and record.section):
        return exact, False, "网页标题不完整，需相同字段签名、区块和控件类型才可复用人工校正"
    return True, True, "已命中本人确认的同公司、同问题映射；填写时读取主档案最新值"


def retrieve_knowledge(snapshot: BrowserSnapshot) -> dict[str, list[KnowledgeMatch]]:
    try:
        user_id = storage.current_user_id()
    except RuntimeError:
        # Pure plan tests and unauthenticated internal calls never fall back to
        # another user's data or a globally shared local-development account.
        return {}
    try:
        scope = company_scope(snapshot.url)
    except ValueError:
        return {}
    initialize()
    with storage._connection() as conn:
        rows = conn.execute("""SELECT * FROM application_knowledge WHERE user_id=? AND site_scope=?
                            ORDER BY updated_at DESC LIMIT ?""", (user_id, scope, MAX_RECORDS)).fetchall()
    if not rows or not snapshot.fields:
        return {}
    records = [KnowledgeRecord.model_validate_json(row["record_json"]) for row in rows]
    documents = [row["embedding_text"] for row in rows]
    fields = snapshot.fields[:200]
    queries = ["；".join(filter(None, (_field_question(field), field.section,
                                     field.entity_scope)))[:1000] for field in fields]
    vectors, query_vectors, dense = _vectors(user_id, rows, queries)
    result: dict[str, list[KnowledgeMatch]] = {}
    for field, query, query_vector in zip(fields, queries, query_vectors):
        lexical = _lexical_scores(query, documents)
        semantic = [sum(a * b for a, b in zip(vector, query_vector))
                    if len(vector) == len(query_vector) else 0.0 for vector in vectors]
        lex_rank = {index: rank + 1 for rank, index in enumerate(sorted(range(len(rows)), key=lambda i: lexical[i], reverse=True)) if lexical[index] > 0}
        sem_rank = {index: rank + 1 for rank, index in enumerate(sorted(range(len(rows)), key=lambda i: semantic[i], reverse=True)) if dense and semantic[index] >= .55}
        candidates: list[KnowledgeMatch] = []
        for index, record in enumerate(records):
            exact, usable, reason = _eligibility(record, field)
            if not exact and index not in lex_rank and index not in sem_rank:
                continue
            rank_score = ((1 / (10 + lex_rank[index]) if index in lex_rank else 0.0) +
                          (1 / (10 + sem_rank[index]) if index in sem_rank else 0.0)) * 5
            candidates.append(KnowledgeMatch(record=record, score=1.0 if exact else min(.89, rank_score),
                exact=exact, usable=usable, reason=reason, retrieval_mode="hybrid" if dense else "keyword-only"))
        usable_targets = {(item.record.profile_path, item.record.entity_scope) for item in candidates if item.usable}
        if len(usable_targets) > 1:
            for item in candidates:
                if item.usable:
                    item.usable = False
                    item.reason = "同一道题存在互相冲突的人工映射，请删除错误记录后重新确认"
        candidates.sort(key=lambda item: (item.usable, item.exact, item.score), reverse=True)
        if candidates:
            result[field.selector] = candidates[:MAX_MATCHES_PER_FIELD]
    return result
