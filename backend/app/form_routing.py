"""Rules -> grounded model mapping -> specific human questions.

The model proposes a profile pointer and a quote, never authoritative personal
facts. Only server-resolved values can become executable actions.
"""
from __future__ import annotations

import re

from .browser_models import BrowserSnapshot, FillAction, FormPlan, FormRoutingSummary, PageField
from .field_semantics import education_level_hint, entity_scope_for, normalize_text, semantic_key_for
from .models import CandidateProfile


AUTOMATIC = {"fill", "select", "check"}
VAGUE = {"是", "否", "男", "女", "yes", "no", "true", "false", "基本信息", "个人信息", "其他信息", "教育经历", "请选择", "请输入", "选择"}
ROOT_EXTRAS = {
    "english_name": ("英文姓名", "candidate.english_name"),
    "age": ("年龄", "candidate.age"),
    "birth_date": ("出生日期", "candidate.birth_date"),
    "summary": ("个人简介", "candidate.summary"),
}
CREDENTIALS = re.compile(r"密码|验证码|校验码|登录|password|passcode|captcha|verification.?code|\botp\b", re.I)


def profile_targets() -> dict[str, tuple[str, str]]:
    from .application_knowledge import mapping_targets

    return {**{item.path: (item.label, item.semantic_key) for item in mapping_targets()}, **ROOT_EXTRAS}


def meaningful_question(value: str) -> bool:
    text = value.strip().strip("* :：")
    return bool(text and text.casefold() not in VAGUE and not re.match(
        r"^(未识别|未命名|字段\s*\d|field\s*\d|input\s*\d)", text, re.I))


def _question(field: PageField) -> str:
    return field.question_text or field.group_label or field.label


def _hard_boundary(field: PageField) -> str:
    from . import form_agent as mapper

    text = mapper._field_text(field)
    if field.field_type in {"password", "hidden", "file", "section-button"} or CREDENTIALS.search(text):
        return "账号凭据、验证码、附件和展开操作需要使用专门入口，不由字段模型处理"
    if mapper._is_third_party(field):
        return "请提供该联系人的真实资料，不能使用你本人的姓名或电话"
    if any(hint in text for hint in mapper.SENSITIVE_HINTS):
        return "这是敏感信息或承诺事项，需要你本人确认，模型不能替你决定"
    if field.knowledge_block_reason and not field.knowledge_block_reason.startswith(("仅检索到", "已找到参考规则")):
        return field.knowledge_block_reason
    return ""


def _reliable_identity(field: PageField) -> bool:
    if field.knowledge_id or field.autocomplete:
        return True
    question = _question(field)
    if re.search(r"拼音|\bfirst name\b|\blast name\b", question, re.I):
        return False  # a full name must not be inserted into a name component
    if not meaningful_question(question):
        return False
    if field.field_type == "radio" and field.control_group_key and field.group_label == question:
        return True
    return field.label_source not in {"nearby", "preceding", "generated", "name"}


def _has_question_evidence(field: PageField) -> bool:
    return any(meaningful_question(value) for value in
               (_question(field), field.help_text, field.context, field.placeholder))


def question_for_user(field: PageField) -> str:
    if meaningful_question(_question(field)):
        kind = semantic_key_for(field).split('.', 1)[0]
        if kind=='language':
            language=entity_scope_for(field).removeprefix('language:')
            return f"语言能力{('（'+language+'）') if language!='unspecified' else ''}：{_question(field)}"
        if kind in {'experience', 'project'}:
            section = field.section or {'experience':'实习/工作经历', 'project':'项目经历'}[kind]
            return f'{section}：{_question(field)}'
        return _question(field)
    section = field.section or " / ".join(field.section_path) or "当前页面"
    kind = "选择题" if field.options or field.field_type in {"radio", "checkbox", "combobox"} else "输入项"
    return f"请核对“{section}”中的{kind}（第 {field.ordinal or 1} 项，原题未完整读取）"


def refresh_summary(plan: FormPlan, snapshot: BrowserSnapshot) -> FormPlan:
    fields = {field.selector: field for field in snapshot.fields}
    groups: dict[str, list[FillAction]] = {}
    for action in plan.actions:
        field = fields.get(action.selector)
        key = (f"radio:{field.control_group_key}" if field and field.field_type == "radio"
               and field.control_group_key else action.selector)
        groups.setdefault(key, []).append(action)
    summary = FormRoutingSummary()
    for actions in groups.values():
        if any(item.needs_model for item in actions):
            summary.model_pending += 1
        elif any(item.action == "ask_user" for item in actions):
            summary.needs_user += 1
        elif any(item.resolution_source == "model" and item.value_source for item in actions):
            summary.model_resolved += 1
        elif any(item.value_source and item.value != "" for item in actions):
            summary.rules_ready += 1
    plan.routing_summary = summary
    plan.missing_questions = list(dict.fromkeys(
        item.review_question or item.label for item in plan.actions
        if item.action == "ask_user" and not item.needs_model
    ))
    return plan


def route_local_plan(plan: FormPlan, snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    from . import form_agent as mapper

    fields = {field.selector: field for field in snapshot.fields}
    for action in plan.actions:
        field = fields[action.selector]
        action.review_question = question_for_user(field)
        action.review_hint = action.reason
        action.needs_model = False
        if action.action == "ask_user" and action.resolution_source == "user":
            action.value = ""
            continue  # Missing personal precision cannot be supplied by a model.
        action.resolution_source = "rules"
        boundary = _hard_boundary(field)
        confirmed_gender = (semantic_key_for(field) == "candidate.gender" and action.user_confirmed
                            and action.value_source == "已确认主档案.gender" and not action.sensitive)
        # Trusted memory has been manually bound before, unlike proximity labels.
        trusted_memory = bool(field.knowledge_id or "已确认答案记忆" in action.value_source)
        if action.action in AUTOMATIC and action.confidence >= .85 and (
                _reliable_identity(field) or trusted_memory) and not action.sensitive and (
                not boundary or confirmed_gender):
            continue
        if action.action == "skip" and action.value_source:
            continue  # an unchosen member of a resolved radio group
        direct, _ = mapper._direct_profile_value(field, profile)
        known = semantic_key_for(field) != "application.custom"
        if field.field_type in {"file", "hidden", "password"} or CREDENTIALS.search(mapper._field_text(field)):
            action.action, action.value, action.resolution_source = "skip", "", "blocked"
        elif boundary:
            action.action, action.value, action.resolution_source = "ask_user", "", "user"
            action.review_hint = boundary
        elif _reliable_identity(field) and known and not direct:
            action.action, action.value, action.resolution_source = "ask_user", "", "user"
            action.review_hint = action.reason or "主档案还没有这一项资料，请补充后再填写"
        elif not _has_question_evidence(field):
            action.action, action.value, action.resolution_source = "ask_user", "", "user"
            action.review_hint = "网页没有提供完整题干；请点击定位查看这是什么问题，不需要猜测答案"
        else:
            action.action, action.value = "ask_user", ""
            action.resolution_source, action.needs_model = "model", True
            action.review_hint = "程序证据不足，交给模型结合原题和选项分析；现在不需要你填写"
        action.reason = action.review_hint
        action.user_confirmed = False
    refresh_summary(plan, snapshot)
    counts = plan.routing_summary
    plan.page_summary = (f"分层处理：{counts.rules_ready} 项有确定依据，"
                         f"{counts.model_pending} 项交给模型分析，{counts.needs_user} 项需要本人提供资料或确认。")
    return plan


def model_catalog() -> list[dict[str, str]]:
    return [{"profile_path": path, "label": label, "semantic_key": key,
             "scope_requirement": "必须提供明确的 education:学历层级" if path.startswith("education.") else ""}
            for path, (label, key) in profile_targets().items()]


def grounded_model_action(proposal: FillAction, field: PageField,
                          snapshot: BrowserSnapshot, profile: CandidateProfile) -> FillAction:
    from . import form_agent as mapper

    def ask(reason: str, title: str = "") -> FillAction:
        return FillAction(selector=field.selector, label=title or question_for_user(field),
                          action="ask_user", confidence=1, resolution_source="user",
                          review_question=title or question_for_user(field), review_hint=reason, reason=reason)

    boundary = _hard_boundary(field)
    if boundary:
        return ask(boundary)
    quote = proposal.question_evidence.strip()
    sources = [_question(field), field.help_text, field.context, field.placeholder]
    grounded_quote = meaningful_question(quote) and any(
        normalize_text(quote) in normalize_text(source) for source in sources if source)
    title = (question_for_user(field) if _reliable_identity(field) else quote) if grounded_quote else ""
    if not grounded_quote:
        return ask("模型未能提供可核对的网页原题依据；请定位该字段确认问题")
    # Evidence is data, not permission: a recovered question may reveal a
    # sensitive subject that the incomplete DOM label had concealed.
    contextual_field = field.model_copy(update={"question_text": quote, "semantic_key": "", "entity_scope": ""})
    if reason := _hard_boundary(contextual_field):
        return ask(reason, title)
    if proposal.action not in AUTOMATIC:
        return ask("模型已读到原题，但还不能可靠对应到主档案；请确认这项的真实答案", title)
    if proposal.confidence < .9:
        return ask("模型判断仍不够确定，请核对该题及对应资料后确认", title)
    target = profile_targets().get(proposal.profile_path)
    if not target:
        return ask("模型没有指出有效的主档案字段，已阻止使用生成或猜测的内容", title)
    _, semantic = target
    quoted_key = semantic_key_for(contextual_field)
    if quoted_key not in {"application.custom", semantic}:
        return ask("模型选取的档案字段与它引用的网页证据不一致，请核对原题", title)
    if semantic in {"candidate.gender", "candidate.ethnicity", "candidate.political_status", "candidate.marital_status"}:
        return ask("这是需要本人确认的信息，模型不能通过改写题目替你确定", title)
    if proposal.profile_path in {"name", "english_name"} and re.search(
            r"拼音|\bfirst name\b|\blast name\b", quote, re.I):
        return ask("该项要求拼音或姓名的一部分，不能直接填入完整姓名；请确认准确写法", title)
    original = semantic_key_for(field)
    # Clear own-label identities cannot be reinterpreted just to find a value.
    if _reliable_identity(field) and original not in {"application.custom", semantic}:
        return ask("模型建议的档案字段与网页原题含义不一致，请人工核对", title)
    scope = proposal.entity_scope
    if not semantic.startswith("education.") and scope not in {"", "candidate", "preference", "application"}:
        return ask("模型给出的资料主体范围不明确，请核对这项属于谁", title)
    if semantic.startswith("education."):
        requested = scope.removeprefix("education:")
        explicit = entity_scope_for(field, original)
        quoted_level = education_level_hint(quote)
        if requested not in mapper.DEGREE_RANK or (
                explicit.startswith("education:") and explicit not in {"education:unspecified", scope}) or (
                quoted_level and quoted_level != requested) or not (
                explicit == scope or quoted_level == requested):
            return ask("无法从该题证据确定本科/硕士等层次，不能跨教育经历取值", title)
    elif entity_scope_for(field, original).startswith(("education:", "experience:", "project:", "third_party")):
        return ask("该题属于一段具体经历，不能借用候选人基础资料", title)
    enriched = field.model_copy(update={"question_text": quote, "label_source": "model-grounded",
        "semantic_key": semantic, "entity_scope": scope, "knowledge_block_reason": ""})
    if field.field_type in {"radio", "checkbox"}:
        enriched.group_label = quote
    mapped_snapshot = snapshot.model_copy(update={"fields": [
        enriched if item.selector == field.selector else item for item in snapshot.fields]})
    if semantic == "candidate.summary":
        value, source = profile.summary, "主档案.summary"
    else:
        resolution = mapper._education_resolutions(mapped_snapshot, profile).get(field.selector)
        value, source = mapper._direct_profile_value(enriched, profile, resolution)
    if not value:
        return ask(f"已识别为“{target[0]}”，但主档案没有可唯一使用的值；请补充这项资料", title)
    if semantic == "candidate.summary":
        compiled = FillAction(selector=field.selector, label=title, action="fill", value=value,
                              value_source=source, confidence=.95)
    else:
        compiled = next(item for item in mapper._local_safe_plan(mapped_snapshot, profile).actions
                        if item.selector == field.selector)
    # Empty/ambiguous options cannot be cured by an LLM claiming confidence.
    if compiled.action not in AUTOMATIC and not (compiled.action == "skip" and compiled.value_source):
        return ask(compiled.reason or "档案值不能可靠匹配当前控件，请核对网页真实选项", title)
    compiled.resolution_source = "model"
    compiled.confidence = min(compiled.confidence, proposal.confidence)
    compiled.needs_model = False
    compiled.profile_path = proposal.profile_path
    compiled.entity_scope = scope
    compiled.question_evidence = quote
    compiled.review_question = compiled.label = title
    compiled.review_hint = f"模型根据网页“{quote}”定位到{target[0]}；填入值由代码读取主档案，填写后回读"
    compiled.reason = compiled.review_hint
    return compiled


def merge_grounded_plan(local: FormPlan, proposed: FormPlan, snapshot: BrowserSnapshot,
                         profile: CandidateProfile) -> FormPlan:
    fields = {field.selector: field for field in snapshot.fields}
    proposals: dict[str, list[FillAction]] = {}
    for item in proposed.actions:
        proposals.setdefault(item.selector, []).append(item)
    merged = []
    for action in local.actions:
        if not action.needs_model:
            merged.append(action)
            continue
        options = proposals.get(action.selector, [])
        if len(options) == 1:
            merged.append(grounded_model_action(options[0], fields[action.selector], snapshot, profile))
        else:
            action.needs_model, action.resolution_source = False, "user"
            action.review_hint = action.reason = "模型没有给出唯一、可验证的对应关系，请核对该项"
            merged.append(action)
    local.actions = merged
    radio_groups: dict[str, list[FillAction]] = {}
    for action in merged:
        field = fields[action.selector]
        if field.field_type == "radio" and field.control_group_key:
            radio_groups.setdefault(field.control_group_key, []).append(action)
    for actions in radio_groups.values():
        model_actions = [item for item in actions if item.resolution_source == "model"]
        targets = {(item.profile_path, item.entity_scope) for item in model_actions if item.profile_path}
        checked = [item for item in actions if item.action == "check" and item.value is True]
        if model_actions and (len(targets) > 1 or len(checked) > 1):
            for item in actions:
                item.action, item.value, item.needs_model = "ask_user", "", False
                item.resolution_source, item.user_confirmed = "user", False
                item.review_hint = item.reason = "模型对同一道单选题给出了冲突的对应关系，请核对原题后选择一个答案"
    refresh_summary(local, snapshot)
    local.page_summary = (f"规则已确定 {local.routing_summary.rules_ready} 项，"
                          f"模型有据映射 {local.routing_summary.model_resolved} 项；"
                          f"剩余 {local.routing_summary.needs_user} 项需要你核对。")
    return local


def model_failed(plan: FormPlan, snapshot: BrowserSnapshot) -> FormPlan:
    for action in plan.actions:
        if action.needs_model:
            action.needs_model, action.resolution_source = False, "user"
            action.review_hint = action.reason = "模型服务暂时未能完成分析；确定项不受影响，本项请人工核对或稍后重试"
    return refresh_summary(plan, snapshot)
