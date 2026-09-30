from __future__ import annotations

import hashlib
import re
from urllib.parse import urlparse

from .browser_models import PageField


EDUCATION_LEVEL_ALIASES = {
    "high_school": ("高中", "中专", "high school", "secondary school"),
    "associate": ("大专", "专科", "associate", "college diploma"),
    "bachelor": ("本科", "学士", "undergraduate", "bachelor", "bsc", "beng"),
    "master": ("硕士", "研究生", "postgraduate", "master", "msc", "meng"),
    "doctorate": ("博士", "doctoral", "doctorate", "phd"),
}

EDUCATION_ATTRIBUTE_HINTS = (
    ("education.college", ("学院名称", "院系名称", "所属学院", "院系", "学院", "faculty", "college", "department")),
    ("education.school", ("学校名称", "院校名称", "毕业院校", "就读院校", "学校", "院校", "university", "school")),
    ("education.major", ("专业名称", "所学专业", "就读专业", "专业", "field of study", "major")),
    ("education.study_mode", ("培养方式", "学习形式", "就读方式", "全日制", "非全日制", "study mode", "training mode")),
    ("education.academic_system", ("学制", "修业年限", "program length", "academic system")),
    ("education.student_id", ("学号", "student id", "student number")),
    ("education.advisor", ("导师", "supervisor", "advisor")),
    ("education.laboratory", ("实验室", "实验室名称", "laboratory", "lab name")),
    ("education.research_direction", ("研究方向", "专业方向", "领域方向", "research direction", "specialization")),
    ("education.degree", ("最高学历", "学历层次", "学历", "学位", "education level", "degree")),
    ("education.location", ("就读地", "学校所在地", "院校所在地", "school location", "study location", "campus location")),
    ("education.start_date", ("入学时间", "入学日期", "开始时间", "start date", "enrollment date")),
    ("education.end_date", ("毕业时间", "毕业日期", "预计毕业", "结束时间", "graduation", "end date")),
    ("education.gpa", ("绩点", "gpa")),
    ("education.ranking", ("专业排名", "成绩排名", "ranking", "rank")),
)

EXPERIENCE_ATTRIBUTE_HINTS = (
    ("experience.organization", ("实习公司", "工作单位", "公司名称", "雇主", "employer", "company", "organization")),
    ("experience.department", ("所在部门", "实习部门", "部门", "department", "division")),
    ("experience.role", ("职位名称", "实习职位", "岗位名称", "职位", "job title", "position", "role")),
    ("experience.start_date", ("入职时间", "开始时间", "start date", "from date")),
    ("experience.end_date", ("离职时间", "结束时间", "end date", "to date")),
    ("experience.description", ("工作内容", "职责描述", "工作描述", "responsibilities", "job description")),
)

PROJECT_ATTRIBUTE_HINTS = (
    ("project.name", ("项目名称", "project name")),
    ("project.role", ("项目角色", "项目职责", "project role")),
    ("project.start_date", ("开始时间", "start date")),
    ("project.end_date", ("结束时间", "end date")),
    ("project.description", ("项目描述", "项目内容", "project description")),
)

SEMANTIC_HINTS = (
    ("third_party.contact", ("紧急联系人", "紧急联络人", "监护人", "推荐人", "证明人", "emergency contact", "guardian", "referee", "recommender")),
    ("candidate.government_id", ("身份证", "证件号码", "证件号", "护照号码", "护照号", "实名认证", "national id", "id number", "passport number")),
    ("candidate.english_name", ("英文姓名", "英文名", "english name", "name in english")),
    ("candidate.age", ("年龄", "周岁", "age")),
    ("candidate.birth_date", ("出生日期", "出生年月", "birth date", "date of birth")),
    ("candidate.email", ("电子邮箱", "电子邮件", "邮箱", "e-mail", "email")),
    ("candidate.phone", ("手机号码", "联系电话", "手机号", "手机", "telephone", "mobile", "phone")),
    ("candidate.wechat", ("微信号", "微信", "wechat", "weixin")),
    ("candidate.qq", ("qq号码", "qq号", "qq account", "qq")),
    ("candidate.github", ("github",)),
    ("candidate.linkedin", ("linkedin",)),
    ("candidate.website", ("个人网站", "作品集", "portfolio", "personal website")),
    ("candidate.gender", ("性别", "gender", "sex")),
    ("candidate.nationality", ("国籍", "citizenship", "nationality")),
    ("candidate.ethnicity", ("民族", "族别", "ethnicity", "ethnic group")),
    ("candidate.political_status", ("政治面貌", "政治身份", "political status", "political affiliation")),
    ("candidate.marital_status", ("婚姻状况", "婚姻状态", "marital status")),
    ("candidate.hukou_location", ("户籍所在地", "户口所在地", "户籍地", "户口地", "hukou", "household registration")),
    ("candidate.address", ("通讯地址", "联系地址", "现居住地址", "mailing address", "contact address")),
    ("candidate.country_region", ("国家/地区", "国家或地区", "所在国家", "country/region", "country or region", "country")),
    ("candidate.current_location", ("当前所处地", "当前所在地", "现居地", "居住地", "current location", "current city")),
    ("preference.work_location", ("期望工作城市", "期望城市", "意向城市", "工作地点志愿", "preferred location", "preferred city", "work city")),
    ("preference.interview_location", ("参加面试城市", "面试城市", "面试地点", "interview city", "interview location")),
    ("preference.business_group", ("感兴趣的事业群", "意向事业群", "事业群志愿", "preferred business group", "business group")),
    ("preference.relocation", ("接受其他城市分配", "服从城市分配", "接受调剂", "服从调剂", "willing to relocate", "relocation")),
    ("candidate.campus_type", ("应届生类型", "毕业生类型", "招聘对象", "校招类型", "campus candidate type")),
    ("candidate.skills", ("ai应用技能", "ai技能", "技术技能", "专业技能", "skill set", "technical skills", "skills")),
    ("candidate.languages", ("语言能力", "外语能力", "掌握语言", "language ability", "language skills", "languages")),
    ("candidate.name", ("候选人姓名", "真实姓名", "中文姓名", "full name", "legal name", "candidate name", "姓名")),
)

AUTOCOMPLETE_SEMANTICS = {
    "name": "candidate.name",
    "email": "candidate.email",
    "tel": "candidate.phone",
    "tel-national": "candidate.phone",
    "country": "candidate.country_region",
    "country-name": "candidate.country_region",
    "address-level2": "candidate.current_location",
    "url": "candidate.website",
}


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", value.casefold())


def field_text(field: PageField) -> str:
    # Nearby labels and help/context may contain sibling questions. They are
    # useful model evidence but must never supply another field's identity.
    return " ".join(filter(None, (
        *field.section_path, field.section, field.question_text, field.group_label, field.label,
        field.placeholder, field.name,
    ))).casefold()


def education_level_hint(value: str) -> str:
    text = value.casefold()
    # Abbreviations are tokens, not substrings (e.g. MSc must not match a
    # department name). 博士研究生 is doctoral, not a second master's claim.
    doctorate = bool(re.search(r"博士|\bdoctor(?:al|ate)?\b|\bph\.?d\.?\b", text))
    flags = {
        "high_school": bool(re.search(r"高中|中专|high school|secondary school", text)),
        "associate": bool(re.search(r"大专|专科|\bassociate\b|college diploma", text)),
        "bachelor": bool(re.search(r"本科|学士|\b(?:bachelor|undergraduate|bsc|bs|beng|b\.sc\.?|b\.s\.?)\b", text)),
        "master": bool(re.search(r"硕士|\b(?:master|msc|ms|meng|m\.sc\.?|m\.s\.?)\b", text))
                  or (not doctorate and "研究生" in text),
        "doctorate": doctorate,
    }
    matches = [level for level, matched in flags.items() if matched]
    return matches[0] if len(matches) == 1 else ""


def field_identity_text(field: PageField) -> str:
    question = field.question_text or field.label
    if field.label_source == "generated" and len(field.context) <= 100:
        # Retain the older single-question recovery path, but reject mixed
        # contexts such as '学校 / 期望城市' instead of picking the first match.
        context = field.context.casefold()
        keys = {key for key, hints in (*SEMANTIC_HINTS, *EDUCATION_ATTRIBUTE_HINTS)
                if any(hint in context for hint in hints)}
        if len(keys) == 1:
            question = context
    group = field.group_label if field.field_type in {"radio", "checkbox"} else ""
    return " ".join(filter(None, (question, group, field.name))).casefold()


def semantic_key_for(field: PageField) -> str:
    if field.semantic_key:
        return field.semantic_key
    text = field_text(field)
    own_text = field_identity_text(field)
    if any(hint in text for hint in SEMANTIC_HINTS[0][1]):
        return "third_party.contact"
    autocomplete = field.autocomplete.casefold().strip().split()[-1] if field.autocomplete.strip() else ""
    for key, hints in SEMANTIC_HINTS:
        if any((bool(re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", own_text))
                if hint.isascii() and hint.isalpha() and len(hint) <= 3 else hint in own_text)
               for hint in hints):
            return key
    education_context = any(hint in f"{text} {own_text}" for hint in (
        "教育", "学历", "学位", "本科", "硕士", "博士", "学院", "院系", "院校", "学校", "就读", "学校所在", "院校所在", "education", "academic",
        "school", "university", "major", "gpa", "graduat",
    ))
    if education_context:
        # Longest own-label match: 学校所在地 is a location, not a school name.
        matches = [(len(hint), key) for key, hints in EDUCATION_ATTRIBUTE_HINTS
                   for hint in hints if hint in own_text]
        if matches:
            return max(matches)[1]
    experience_context = any(hint in text for hint in (
        "实习", "工作经历", "任职经历", "职业经历", "employment history", "work experience",
        "internship", "employment record",
    ))
    if experience_context:
        for key, hints in EXPERIENCE_ATTRIBUTE_HINTS:
            if any(hint in own_text for hint in hints):
                return key
    project_context = any(hint in text for hint in ("项目经历", "项目经验", "project experience", "projects"))
    if project_context:
        for key, hints in PROJECT_ATTRIBUTE_HINTS:
            if any(hint in own_text for hint in hints):
                return key
    if autocomplete in AUTOCOMPLETE_SEMANTICS:
        return AUTOCOMPLETE_SEMANTICS[autocomplete]
    return "application.custom"


def entity_scope_for(field: PageField, semantic_key: str = "") -> str:
    if field.entity_scope:
        return field.entity_scope
    key = semantic_key or semantic_key_for(field)
    text = field_text(field)
    if key.startswith("education."):
        level = education_level_hint(text)
        if level:
            return f"education:{level}"
        if "最高" in text or "highest" in text:
            return "education:highest"
        if "当前" in text or "current education" in text:
            return "education:current"
        return "education:unspecified"
    if key.startswith("experience."):
        return "experience:current" if "当前" in text or "current" in text else "experience:unspecified"
    if key.startswith("project."):
        return "project:unspecified"
    if key == "third_party.contact":
        return "third_party"
    if key.startswith("candidate."):
        return "candidate"
    if key.startswith("preference."):
        return "preference"
    return "application"


def option_fingerprint(options: list[str]) -> str:
    normalized = sorted({normalize_text(option) for option in options if normalize_text(option)})
    return hashlib.sha256("|".join(normalized).encode("utf-8")).hexdigest()[:16] if normalized else ""


def field_signature_for(field: PageField, source_url: str, rank: int = 0) -> str:
    parsed = urlparse(source_url)
    site_scope = f"{(parsed.hostname or '').casefold()}{parsed.path.rstrip('/')}"
    semantic_key = semantic_key_for(field)
    entity_scope = entity_scope_for(field, semantic_key)
    identity = "|".join((
        site_scope,
        semantic_key,
        entity_scope,
        normalize_text(field.section),
        normalize_text(field.question_text or field.group_label or field.label),
        normalize_text(field.name),
        field.autocomplete.casefold(),
        field.field_type,
        # Lazy-loading options must not turn the same question into a new field.
        # Answer reuse checks option_fingerprint independently of this identity.
        str(rank),
    ))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def education_scope_label(scope: str) -> str:
    labels = {
        "education:high_school": "高中",
        "education:associate": "专科",
        "education:bachelor": "本科",
        "education:master": "硕士",
        "education:doctorate": "博士",
        "education:highest": "最高学历",
        "education:current": "当前就读",
    }
    return labels.get(scope, "未明确层次的教育经历")


def expected_input_for(field: PageField, semantic_key: str = "", entity_scope: str = "") -> str:
    key = semantic_key or semantic_key_for(field)
    scope = entity_scope or entity_scope_for(field, key)
    if key.startswith("education."):
        level = education_scope_label(scope)
        attribute = {
            "education.school": "学校/院校全称",
            "education.college": "学院或院系名称（不是学校名称）",
            "education.major": "专业名称",
            "education.degree": "学历或学位",
            "education.study_mode": "培养/学习方式（如全日制）",
            "education.academic_system": "学制或修业年限",
            "education.student_id": "学号",
            "education.advisor": "导师姓名",
            "education.laboratory": "实验室名称",
            "education.research_direction": "研究或专业方向",
            "education.location": "学校所在地",
            "education.start_date": "入学时间",
            "education.end_date": "毕业或预计毕业时间",
            "education.gpa": "绩点",
            "education.ranking": "成绩或专业排名",
        }.get(key, "教育信息")
        if scope == "education:unspecified":
            return f"请先确认该题对应本科、硕士或其他哪段经历，再填写{attribute}"
        return f"请填写{level}经历的{attribute}"
    if key.startswith("experience."):
        attribute = {
            "experience.organization": "公司或组织名称",
            "experience.department": "所属部门",
            "experience.role": "职位或岗位名称",
            "experience.start_date": "开始时间",
            "experience.end_date": "结束时间",
            "experience.description": "真实工作内容",
        }.get(key, "实习/工作信息")
        return (f"请填写当前任职经历的{attribute}" if scope == "experience:current" else
                f"请先确认该题对应哪段实习/工作经历，再填写{attribute}")
    if key.startswith("project."):
        attribute = {
            "project.name": "项目名称", "project.role": "项目角色",
            "project.start_date": "开始时间", "project.end_date": "结束时间",
            "project.description": "项目描述",
        }.get(key, "项目信息")
        return f"请先确认该题对应哪个项目，再填写{attribute}"
    hints = {
        "third_party.contact": "请填写网页指定的第三方联系人真实信息，不能使用候选人本人资料",
        "candidate.government_id": "请本人核对后填写真实证件信息；该答案只用于本次页面，不会被学习",
        "preference.interview_location": "请根据本次招聘网站提供的真实选项选择面试方式或城市",
        "preference.work_location": "请从网页真实选项中选择期望工作地点",
        "preference.business_group": "请从网页真实选项中选择意向事业群",
        "preference.relocation": "请确认是否接受调剂、轮岗或其他城市分配",
        "candidate.country_region": "请从网页真实选项中选择国家或地区",
        "candidate.skills": "请选择或填写你真实掌握的技能",
        "candidate.languages": "请选择或填写真实语言能力",
        "application.custom": "请根据招聘网页原问题填写；不确定时先定位到网页查看上下文",
    }
    return hints.get(key, "请填写与网页问题直接对应的真实信息")


def recognition_evidence_for(field: PageField) -> str:
    evidence: list[str] = []
    if field.section_path:
        evidence.append(f"区块“{' > '.join(field.section_path)}”")
    elif field.section:
        evidence.append(f"分区“{field.section}”")
    if field.group_label and (field.field_type in {"radio", "checkbox"}
                              or normalize_text(field.group_label) != normalize_text(field.label)):
        evidence.append(f"题组“{field.group_label}”")
    option_caption = normalize_text(field.option_label or field.option_value)
    display_label = field.question_text or field.label
    if display_label and not (field.field_type in {"radio", "checkbox"}
                              and option_caption and option_caption in normalize_text(display_label)):
        evidence.append(f"网页原题“{display_label}”")
    if field.field_type in {"radio", "checkbox"} and (field.option_label or field.option_value):
        evidence.append(f"网页选项“{field.option_label or field.option_value}”")
    if field.name and field.label_source in {"name", "unknown", "generated"}:
        evidence.append(f"网页字段名“{field.name}”")
    return "依据" + "、".join(evidence[:3]) if evidence else "网页没有提供足够的字段说明"


def enrich_fields(fields: list[PageField], source_url: str) -> list[PageField]:
    """Attach stable semantic identities without trusting fragile CSS selectors."""
    ranks: dict[str, int] = {}
    enriched: list[PageField] = []
    for field in fields:
        key = semantic_key_for(field)
        scope = entity_scope_for(field, key)
        rank_key = "|".join((key, scope, normalize_text(field.section),
                             normalize_text(field.question_text or field.group_label or field.label),
                             field.field_type))
        rank = ranks.get(rank_key, 0)
        ranks[rank_key] = rank + 1
        enriched.append(field.model_copy(update={
            "semantic_key": key,
            "entity_scope": scope,
            "field_signature": field_signature_for(field, source_url, rank),
            "signature_rank": rank,
            "expected_input": expected_input_for(field, key, scope),
            "recognition_evidence": recognition_evidence_for(field),
        }))
    return enriched
