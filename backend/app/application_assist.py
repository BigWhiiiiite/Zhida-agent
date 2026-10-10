"""Bounded preparation loop. No login, navigation, declarations or submission.

Unlike a one-off script, every retry observes a fresh page, skips verified
matches, and uses the currently bound resume. Only exact registered adapters
may create repeatable sections. New empty records receive an explicit identity
anchor before the ordinary mapper fills their attributes.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
import re
from pathlib import Path

from .browser_models import (ApplicationAssistEvent, ApplicationAssistIssue, ApplicationAssistRequest, ApplicationRecordCoverage,
    ApplicationAssistResult, ExecutePlanRequest, FillAction)
from .field_semantics import semantic_key_for
from .form_agent import (_education_resolutions, build_form_review,
                        comparison_group_key, create_local_form_plan)
from .repeated_records import resolve_repeated_records
from .form_field_policy import formal_employment_only, is_declaration
from .execution_safety import ExecutionTargetChanged, execution_phase_label


KINDS = {"education": ("education", "school"), "internships": ("experience", "organization"),
         "projects": ("project", "name")}
STAGES = {"profile_form", "application_form", "review"}


def model_budget_seconds() -> float:
    """Respect existing provider budgets, with an outer finite journey limit.

    A fixed short orchestration deadline would silently undo the longer model
    timeouts users configured for slow proxies. Never read a dotenv file here.
    """
    def configured(key, default):
        try:
            value = float(os.getenv(key, str(default)))
            return max(10, min(value, 300)) if math.isfinite(value) else default
        except ValueError:
            return default
    return min(600, configured("APP_FORM_MODEL_TIMEOUT_SECONDS", 180)
               + configured("APP_FORM_FALLBACK_TIMEOUT_SECONDS", 120) + 15)


def safe_actions(review):
    fields = {field.selector: field for field in review.snapshot.fields}
    return [action for action in review.plan.actions
        if action.selector in fields and action.action in {"fill", "select", "check"}
        and action.confidence >= .85 and (not action.sensitive or action.user_confirmed)
        and not is_declaration(fields[action.selector])
        and fields[action.selector].field_type not in {
            "file", "hidden", "password", "section-button", "button", "submit", "reset"}]


def record_text_actions(review, inventory):
    """Complete grounded text inside vetted records before unrelated widgets.

    Allocation alone is not a complete experience. Keep this separate batch
    narrowly inside the adapter's record keys; calendars, choices, declarations
    and ambiguous records continue through the ordinary guarded workflow.
    """
    keys = {key for item in inventory if item['kind'] in KINDS
            for key in item['record_keys']}
    fields = {field.selector:field for field in review.snapshot.fields}
    return [action for action in safe_actions(review)
        if action.action == 'fill'
        and (field := fields[action.selector]).container_key in keys
        and field.field_type in {'text', 'textarea'} and not field.readonly
        and semantic_key_for(field).startswith(('education.', 'experience.', 'project.'))]


def observation_key(snapshot):
    # DOM markers are transient; values and questions determine progress.
    rows = [(f.container_key, semantic_key_for(f), f.question_text or f.label,
             f.field_type, f.current_value, f.options) for f in snapshot.fields]
    return hashlib.sha256(json.dumps([snapshot.url, rows], ensure_ascii=False).encode()).hexdigest()


def _record_groups(snapshot, prefix):
    result = {}
    for field in snapshot.fields:
        if (field.container_key and field.field_type != 'section-button'
                and not formal_employment_only(field)
                and field.section not in {'个人信息', '基本信息'}
                and field.entity_scope != 'education:highest'
                and semantic_key_for(field).startswith(prefix + ".")):
            result.setdefault(field.container_key, []).append(field)
    return result


def _empty_record(fields):
    return not any(f.current_value.strip() and f.current_value.strip().casefold() not in {
        'false' if f.field_type in {'checkbox', 'radio'} else '', '请选择', '请选择一项', '未选择'
    } for f in fields)


def _allocation(snapshot, profile, kind, record_keys=None, exclude_key=''):
    """Reserve only a genuinely unused source, not an ordinal website guess."""
    prefix, _ = KINDS[kind]
    records = getattr(profile, kind)
    groups = _record_groups(snapshot, prefix)
    if record_keys is not None:
        groups = {key:fields for key,fields in groups.items() if key in record_keys}
    bindings = (_education_resolutions(snapshot, profile) if kind == "education"
                else resolve_repeated_records(snapshot, profile))
    used = set()
    for key, fields in groups.items():
        if key == exclude_key or _empty_record(fields):
            continue  # genuinely empty owned groups can be explicitly allocated
        resolved = [bindings.get(field.selector) for field in fields]
        candidates = [item.record if kind == "education" else item[1]
                      for item in resolved if item is not None]
        candidates = [item for item in candidates if item is not None]
        if not candidates:
            # Ambiguous old records must be reviewed before adding possible duplicates.
            return None
        source_indices = {i for i, record in enumerate(records)
                          if any(record is candidate for candidate in candidates)}
        if len(source_indices) != 1 or used.intersection(source_indices):
            return None
        used.update(source_indices)
    remaining = [record for i, record in enumerate(records) if i not in used]
    return remaining[0] if remaining else None


def _coverage(snapshot, profile, inventory):
    rows = []
    for kind, (prefix, anchor) in KINDS.items():
        sources = getattr(profile, kind)
        item = next((r for r in inventory if r['kind'] == kind), None)
        groups = _record_groups(snapshot, prefix)
        if item is not None:
            groups = {key:fields for key,fields in groups.items() if key in item['record_keys']}
        bindings = (_education_resolutions(snapshot, profile) if kind == 'education'
                    else resolve_repeated_records(snapshot, profile))
        claims = []
        ambiguous = bool(item and set(groups) != set(item['record_keys']))
        for fields in groups.values():
            if _empty_record(fields):
                continue
            candidates = [bindings.get(f.selector) for f in fields]
            resolved = [c.record if kind == 'education' else c[1] for c in candidates if c is not None]
            ids = {i for i, record in enumerate(sources) if any(record is c for c in resolved)}
            if len(ids) != 1:
                ambiguous = True
            else:
                claims.append(next(iter(ids)))
        if len(set(claims)) != len(claims):
            ambiguous = True
        used = set(claims)
        rows.append(ApplicationRecordCoverage(kind=kind, label={'education':'教育经历',
            'internships':'实习经历', 'projects':'项目经历'}[kind], source_total=len(sources),
            website_records=item['record_count'] if item else len(groups), matched_records=len(used),
            missing_names=[str(getattr(record, anchor)) for i, record in enumerate(sources) if i not in used],
            ambiguous=ambiguous, can_expand=bool(item and item['selector'])))
    return rows


def _field_identity(field):
    return tuple(getattr(field, key) for key in ('selector', 'field_type', 'field_signature',
        'question_text', 'label', 'name', 'semantic_key', 'entity_scope', 'container_key',
        'control_group_key', 'section', 'group_label', 'option_label', 'option_value')) + (
            tuple(field.options),
            tuple((e.text, e.source) for e in field.question_candidates if e.owned),
            tuple(sorted(field.constraints.model_dump().items())),
            tuple(field.required_evidence),
        )


def _retain_execution_failures(review, failed_controls):
    """A matching display value cannot erase a failed widget commit.

    Bind failures to the observed question, not a reusable DOM selector. A
    later verified execution clears its failure; a changed question does not
    inherit it. No raw error or applicant answer is copied into the summary.
    """
    failed_groups = {comparison_group_key(field) for field in review.snapshot.fields
                     if failed_controls.get(field.selector) == _field_identity(field)}
    for item in review.comparisons:
        if item.status == "matched" and item.key in failed_groups:
            item.status = "manual_review"
            item.recommendation = (
                "网页显示值相符，但本轮控件提交或选中状态未通过核验；"
                "不计为填写成功，请重新核对该控件")
            review.summary.matched -= 1
            review.summary.manual_review += 1
    return review


def _language_allocation(snapshot, profile):
    """Explicitly assign one empty language record from compatible name memory.

    Never infer a language from IELTS or choose between two remaining languages.
    Task composition has already filtered the memories by owner, CV and tenant.
    Proficiency memory is usable only AFTER the committed type is re-observed.
    """
    from .form_agent import _normalized, _saved_answer_match
    groups = _record_groups(snapshot, 'language')
    used = {f.current_value.strip() for fields in groups.values() for f in fields
            if semantic_key_for(f) == 'language.name' and f.current_value.strip()}
    empty = [fields for fields in groups.values() if _empty_record(fields)]
    if len(empty) != 1:
        return None
    fields = empty[0]
    anchors = [f for f in fields if semantic_key_for(f) == 'language.name'
               and f.field_type in {'combobox', 'select-one'} and f.options]
    if len(anchors) != 1:
        return None
    field = anchors[0]
    candidates = set()
    for memory in profile.application_answer_memory:
        if (memory.semantic_key != 'language.name' or memory.value in used
                or memory.value not in field.options
                or memory.entity_scope != 'language:'+memory.value
                or memory.normalized_question != _normalized(field.question_text or field.label)
                or not memory.option_fingerprint):
            continue
        # An old signature of an unspecified slot is not this named record's
        # identity. Require semantic/question/options matching of named memory.
        bound = field.model_copy(update={'entity_scope':memory.entity_scope,'field_signature':''})
        if _saved_answer_match(bound,profile).value == memory.value:
            candidates.add(memory.value)
    if len(candidates) != 1:
        return None
    return FillAction(selector=field.selector,label=field.label,action='select',
        value=next(iter(candidates)),confidence=1,user_confirmed=True,
        value_source='同简历同网站已确认的语言类型，向唯一空语言记录明确分配')


async def prepare_application(service, session_id, request: ApplicationAssistRequest,
                              profile, *, guard, model_plan, stamp_plan,
                              resume_path: Path | None = None, pending_sections=(), on_progress=None):
    events = []
    rounds = 0
    review = None
    check = None
    coverage_blockers = []
    inventory = []
    deferred = {}
    model_calls = 0

    def event(kind, message, completed=0, failed=0, issues=()):
        item = ApplicationAssistEvent(kind=kind, message=message, completed=completed,
                                     failed=failed, issues=list(issues))
        events.append(item)
        if on_progress:
            on_progress(item)

    event('observe', '正在读取当前申请表和真实控件选项；尚未开始填写')
    snapshot = await service.settled_snapshot(session_id)
    initial_url = snapshot.url

    async def observe():
        guard()
        state = await service.workflow_state(session_id)
        if state.navigation_blocker:
            raise ValueError(state.navigation_blocker)
        if state.stage not in STAGES or state.url != initial_url:
            raise ValueError("网页已离开本次申请表，已停止；不会跟随跳转继续填写")
        latest = await service.settled_snapshot(session_id)
        guard()
        if latest.url != initial_url:
            raise ValueError("网页地址已变化，请重新确认目标")
        if not latest.fields:
            raise ValueError("尚未读取到可核对的申请字段，请等待网页加载后重新同步")
        fields = {f.selector:f for f in latest.fields}
        if any(selector not in fields or _field_identity(fields[selector]) != identity
               for selector,identity in deferred.items()):
            raise ValueError("本次留空的题目或记录归属已变化，已停止旧选择；请重新核对")
        return latest

    def finish(status, message):
        return ApplicationAssistResult(status=status, message=message, snapshot=snapshot,
            review=review, pre_submit=check, events=events, rounds=rounds,
            record_coverage=_coverage(snapshot, profile, inventory), model_calls=model_calls)

    async def record_inventory():
        reader = getattr(service, 'record_inventory', None) or service.expandable_sections
        return await reader(session_id)

    def apply_deferrals(plan):
        if not deferred:
            return plan
        for action in plan.actions:
            if action.selector in deferred:
                action.action, action.value, action.needs_model = 'skip', '', False
                action.resolution_source, action.value_source = 'blocked', '本次明确留空'
                action.reason = action.review_hint = '本人要求本次暂不填写；不清空已有值，必填检查仍然保留'
        from .form_routing import refresh_summary
        return refresh_summary(plan, snapshot)

    async def seed(fields, record, kind):
        prefix, anchor = KINDS[kind]
        anchors = [f for f in fields if semantic_key_for(f) == prefix+'.'+anchor
                   and f.field_type in {'text', 'textarea'}]
        if len(anchors) != 1 or not _empty_record(fields):
            raise ValueError('待分配记录不是唯一空组，已停止，避免覆盖或串填')
        value = getattr(record, anchor)
        if not value:
            raise ValueError('所选简历缺少学校、企业或项目名称，请先补充')
        action = FillAction(selector=anchors[0].selector, label=anchors[0].label,
            action='fill', value=value, confidence=1, value_source='本次所选简历向已验证空记录的明确分配')
        guard()
        result = await service.execute(session_id, ExecutePlanRequest(actions=[action]), before_action=guard)
        if result.failed or result.verified != 1:
            raise ValueError('记录身份字段未回读成功，已停止，避免跨经历填写')

    try:
        snapshot = await observe()
        by_selector = {f.selector:f for f in snapshot.fields}
        for choice in request.deferred_fields:
            actual = by_selector.get(choice.selector)
            if (not actual or _field_identity(actual) != _field_identity(choice)
                    or is_declaration(actual) or actual.field_type in {'file','hidden','password','section-button'}):
                raise ValueError('本次留空选择与当前题目不一致，尚未填写，请重新核对')
            deferred[choice.selector] = _field_identity(actual)
        if request.defer_government_id:
            deferred.update({f.selector:_field_identity(f) for f in snapshot.fields
                             if semantic_key_for(f) == 'candidate.government_id'})
        if deferred:
            event('deferred', f'保留本人明确留空的{len(deferred)}项；不代表必填检查通过')
        if pending_sections:
            event("review_required", "所选简历仍有待核验资料：" + "、".join(pending_sections))
            return finish("needs_user", "请先到简历资料库确认上述资料；不会把待核验内容当事实填写")
        event("observe", "已确认当前是申请表；不会导航、注册、签署声明或提交")
        if snapshot.extraction_report:
            report = snapshot.extraction_report
            event("extraction", f"当前文档识别{report.question_count}道题，{report.verified_questions}道原题有归属证据，"
                  f"{report.unclear_questions}道需核实题干，{report.options_pending_questions}道选项或层级待补读；不代表整表完整")
        event('retrieve', '使用本次简历、主档案和同范围已确认记忆；不同学历、经历及他人资料分别核对')
        if request.allow_site_parse:
            guard()
            if any(f.field_type == "file" and f.current_value for f in snapshot.fields):
                event("upload_skipped", "官网已有附件，保留现有草稿；如需更换请使用单独上传入口")
            elif resume_path:
                imported = await service.import_resume_with_site_parser(session_id, resume_path, True)
                event("site_parse", imported.message, imported.changed_fields)
                snapshot = imported.snapshot
                if imported.status == "needs_user_action":
                    return finish("needs_user", imported.message)
                snapshot = await observe()
            else:
                return finish("blocked", "未找到已选简历原件，未上传，也未改动网页")

        # Allocate genuine empty records from a vetted inventory first. This
        # is an explicit assignment to an empty slot, not inference by ordinal.
        for _ in range(8):
            inventory = await record_inventory()
            chosen = next(((item,key,fields) for item in inventory if item['kind'] in KINDS
                for key,fields in _record_groups(snapshot, KINDS[item['kind']][0]).items()
                if key in item['record_keys'] and _empty_record(fields)
                and not any(f.selector in deferred and semantic_key_for(f)==
                    KINDS[item['kind']][0]+'.'+KINDS[item['kind']][1] for f in fields)
                and _allocation(snapshot,profile,item['kind'],item['record_keys'],key) is not None), None)
            if not chosen:
                break
            item,key,fields = chosen
            record = _allocation(snapshot,profile,item['kind'],item['record_keys'],key)
            await seed(fields,record,item['kind'])
            event('allocate', f"已将所选简历的一条{item['label']}明确分配到网页空记录", 1)
            snapshot = await observe()

        # Create at most eight records; inventory must come from a vetted adapter.
        for _ in range(8):
            inventory = await record_inventory()
            missing = [item for item in inventory if item["kind"] in KINDS
                       and item["record_count"] < len(getattr(profile, item["kind"]))]
            if not missing:
                break
            candidate = missing[0]
            if not candidate['selector']:
                coverage_blockers.append('官网没有可核验的新增经历入口，不能将缺少的记录算作完成')
                break
            record = _allocation(snapshot, profile, candidate["kind"], candidate['record_keys'])
            if record is None:
                coverage_blockers.append("已有经历无法唯一对应简历，请先核对，避免新增重复经历")
                break
            prefix, anchor = KINDS[candidate["kind"]]
            before = set(_record_groups(snapshot, prefix))
            guard()
            snapshot = await service.expand_missing_section(session_id, candidate)
            guard()
            groups = _record_groups(snapshot, prefix)
            added = set(groups) - before
            if len(added) != 1:
                return finish("partial", "新增栏目的记录边界无法验证，已停止，不会重复点击")
            fields = groups[added.pop()]
            if not _empty_record(fields):
                return finish("partial", "新增记录并非唯一空组，已停止，不会覆盖现有资料")
            # Explicitly allocate this NEW empty group, then rely on the normal
            # source-grounded resolver. Never seed an old ambiguous group.
            await seed(fields,record,candidate['kind'])
            event("expand", f"已安全增加一条{candidate['label']}并核验所属记录", 1)
            snapshot = await observe()

        inventory = await record_inventory()
        if any(item["kind"] in KINDS and item["record_count"] < len(getattr(profile, item["kind"]))
               for item in inventory):
            coverage_blockers.append("部分经历尚未展开，已达到安全边界或本轮展开上限")

        # Verify a complete grounded text record as its own batch. An unrelated
        # picker later in the page must not prevent role/content from being
        # attempted or erase this batch's already completed readback receipt.
        record_review = build_form_review(snapshot,
            apply_deferrals(stamp_plan(create_local_form_plan(snapshot, profile))))
        text_actions = record_text_actions(record_review, inventory)
        if text_actions:
            guard()
            event('execute', f'先补齐已核实经历记录中的{len(text_actions)}项文本；日期和其他栏目单独处理')
            started = asyncio.get_running_loop().time()
            def record_progress(index, phase):
                elapsed = int(asyncio.get_running_loop().time() - started)
                event('control', f'经历文本第{index}/{len(text_actions)}项：{execution_phase_label(phase)}'
                      f'（本批已用{elapsed}秒）；尚未通过整批最终核对')
            written = await service.execute(session_id, ExecutePlanRequest(actions=text_actions),
                before_action=guard, on_progress=record_progress)
            failures = {}
            issues = []
            fields = {field.selector:field for field in snapshot.fields}
            actions = {action.selector:action for action in text_actions}
            for item in written.results:
                if item.status == 'failed' or (item.status == 'filled' and not item.verified):
                    failures[item.selector] = _field_identity(fields[item.selector])
                    message = item.message or '经历文本未能通过网页回读核验'
                    value = str(actions[item.selector].value)
                    if value:
                        message = message.replace(value, '已确认的目标值')
                    issues.append(ApplicationAssistIssue(label=item.label, message=message[:240]))
            event('fill', f'经历文本独立回读：成功{written.verified}项，失败{written.failed}项；不代表整条经历或整表完成',
                  written.verified, written.failed, issues)
            snapshot = await observe()
            if failures:
                review = _retain_execution_failures(build_form_review(snapshot,
                    apply_deferrals(stamp_plan(create_local_form_plan(snapshot, profile)))), failures)
                return finish('partial', '经历文本仍有提交或回读失败；已填内容保留，先核对这些字段，不重复填写其他栏目')

        language = _language_allocation(snapshot,profile)
        if language and language.selector not in deferred:
            guard()
            assigned = await service.execute(session_id,ExecutePlanRequest(actions=[language]),before_action=guard)
            if assigned.failed or assigned.verified != 1:
                return finish('partial','已确认语言类型未回读成功；不会把其他语言的能力填到此记录')
            event('allocate','已将本人确认的语言类型分配到唯一空语言记录，能力仍按已选语言核对',1)
            snapshot = await observe()

        seen = set()
        analysed_questions = set()
        model_actions = {}
        model_identities = {}
        failed_attempts = set()
        failed_controls = {}
        model_deadline = None

        def attempt_key(action):
            field = next((f for f in snapshot.fields if f.selector == action.selector), None)
            return (action.selector, field.question_text if field else '',
                    field.container_key if field else '',
                    semantic_key_for(field) if field else '',
                    tuple(field.options) if field else ())

        def remaining_actions(current_review):
            # A dependency may expose new options, in which case retry is useful.
            # An unchanged failed control must not monopolize the next round or
            # prevent the model from analysing unrelated ambiguous questions.
            return [a for a in safe_actions(current_review) if attempt_key(a) not in failed_attempts]

        for _ in range(request.max_rounds):
            snapshot = await observe()
            key = observation_key(snapshot)
            repeated = key in seen
            seen.add(key)
            plan = apply_deferrals(stamp_plan(create_local_form_plan(snapshot, profile)))
            review = build_form_review(snapshot, plan)
            actions = remaining_actions(review)
            pending = {a.selector for a in plan.actions if a.needs_model and a.selector not in deferred}
            new_questions = {f.selector for f in snapshot.fields if f.selector in pending
                             and _field_identity(f) not in analysed_questions}
            can_analyse = request.use_model and model_calls < 2 and bool(new_questions)
            event('decide', f'第{rounds+1}轮核对：{len(actions)}项有确定依据，{len(new_questions)}项新的疑难题目')
            if repeated and not can_analyse:
                event("stopped", "页面没有变化，停止重复尝试；请查看未完成项")
                break
            if not actions and can_analyse:
                model_calls += 1
                if model_deadline is None:
                    model_deadline = asyncio.get_running_loop().time() + model_budget_seconds()
                remaining_budget = model_deadline - asyncio.get_running_loop().time()
                if remaining_budget <= 0:
                    event('model_unavailable', '本轮模型分析时间预算已用完；已填内容保留')
                    break
                analysed_questions.update(_field_identity(f) for f in snapshot.fields if f.selector in new_questions)
                model_snapshot = observation_key(snapshot)
                event('model', f'正在调用配置模型分析第{model_calls}批疑难题目；不会猜测缺失的个人事实')
                try:
                    # Keep record anchors as context, but do not ask the model
                    # the same unresolved question again unless its identity or
                    # actual options changed after a dependency was filled.
                    model_view = snapshot.model_copy(update={'fields':[f for f in snapshot.fields
                        if f.selector not in deferred and (f.selector not in pending or f.selector in new_questions)]})
                    plan = await asyncio.wait_for(model_plan(model_view), timeout=remaining_budget)
                except asyncio.TimeoutError:
                    event("model_unavailable", "模型分析超时；已填内容保留，未用猜测值补齐")
                    break
                except Exception:
                    event("model_unavailable", "模型分析暂未完成；已填内容保留，请检查剩余问题")
                    break
                snapshot = await observe()
                if observation_key(snapshot) != model_snapshot:
                    event("stopped", "模型分析期间网页发生变化，旧建议已丢弃，请重新核对")
                    break
                plan = apply_deferrals(plan)
                model_actions.update({a.selector:a for a in plan.actions
                                      if a.resolution_source == 'model'})
                model_identities.update({f.selector:_field_identity(f) for f in model_view.fields})
                review = build_form_review(snapshot, plan)
                actions = remaining_actions(review)
                event("model", "已让模型分析疑难项；填写值仍由档案证据和安全规则决定")
            if not actions:
                break
            guard()
            event('execute', f'正在执行{len(actions)}项有依据的填写；每项都要回读核验')
            started = asyncio.get_running_loop().time()
            def control_progress(index, phase):
                # No answer, selector, exception body or DOM is copied into the
                # progress stream. This is a stage, not a success count.
                elapsed = int(asyncio.get_running_loop().time() - started)
                event('control', f'第{index}/{len(actions)}项：{execution_phase_label(phase)}'
                      f'（本批已用{elapsed}秒）；尚未通过整批最终核对')
            result = await service.execute(session_id, ExecutePlanRequest(actions=actions), before_action=guard,
                                           on_progress=control_progress)
            rounds += 1
            issues = []
            by_selector = {a.selector: a for a in actions}
            for item in result.results:
                if item.status == "failed" or (item.status == "filled" and not item.verified):
                    action = by_selector.get(item.selector)
                    if action:
                        failed_attempts.add(attempt_key(action))
                        field = next((f for f in snapshot.fields if f.selector == item.selector), None)
                        if field:
                            failed_controls[item.selector] = _field_identity(field)
                    message = item.message or "未能通过网页回读核验"
                    # Progress history needs causes, not attempted personal values.
                    if action and isinstance(action.value, str) and action.value:
                        message = message.replace(action.value, "已确认的目标值")
                    issues.append(ApplicationAssistIssue(label=item.label, message=message[:240]))
                elif item.verified:
                    failed_controls.pop(item.selector, None)
            event("fill", f"第{rounds}轮：回读成功{result.verified}项，失败{result.failed}项",
                  result.verified, result.failed, issues[:50])
            # Never resend old selectors blindly. The next bounded iteration
            # snapshots again, obtains new options and skips all matching values.
            event('verify', '重新读取网页，检查实际保存值及新增的联动题目；不盲目重发旧计划')
            # The next loop iteration (or the mandatory final observation)
            # performs this fresh read. Avoid scanning/probing the same long
            # form twice consecutively; no identity or execution guard is lost.

        snapshot = await observe()
        final_plan = apply_deferrals(stamp_plan(create_local_form_plan(snapshot, profile)))
        if model_actions:
            # Preserve already grounded model mappings only while the same
            # field identity exists; no new model-generated values are added.
            by_selector = {a.selector: a for a in model_actions.values()
                           if a.resolution_source == "model" and a.action in {"fill", "select", "check"}}
            identities = {f.selector: _field_identity(f)
                          for f in snapshot.fields}
            for i, action in enumerate(final_plan.actions):
                if (action.needs_model and action.selector in by_selector
                        and identities.get(action.selector) == model_identities.get(action.selector)):
                    final_plan.actions[i] = by_selector[action.selector]
        review = _retain_execution_failures(build_form_review(snapshot, final_plan), failed_controls)
        event('verify', '正在进行最终回读、必填检查及各段经历覆盖核对；尚未提交')
        check = await service.pre_submit_check(session_id)
        latest = await observe()
        if observation_key(latest) != observation_key(snapshot):
            snapshot, review, check = latest, None, None
            return finish("partial", "最终核对期间网页发生变化，旧核对结果已作废；请重新读取当前页面")
        declarations = {f.selector for f in snapshot.fields if is_declaration(f)}
        fields = {f.selector:f for f in snapshot.fields}
        ignored_optional = {a.selector for a in final_plan.actions if a.action == 'skip'
            and a.selector in fields and not fields[a.selector].required
            and (fields[a.selector].field_type == 'file' or a.value_source == '所选简历没有可填写的奖项')}
        unresolved = [c for c in review.comparisons if c.status != "matched"
                      and c.selector not in declarations|set(deferred)|ignored_optional]
        missing = [item for item in check.required_missing if item.selector not in declarations]
        inventory = await record_inventory()
        coverage = _coverage(snapshot, profile, inventory)
        if any(row.missing_names or row.ambiguous for row in coverage):
            coverage_blockers.append('简历记录尚未完整、唯一地对应到网页，请查看教育/实习/项目覆盖明细')
        if coverage_blockers:
            event("coverage", "；".join(dict.fromkeys(coverage_blockers)))
        if check.human_challenges or check.validation_errors:
            return finish("needs_user", "官网仍有验证或校验提示，请先在网页处理，已填内容保留")
        if snapshot.extraction_report and snapshot.extraction_report.capture_status == "partial":
            event("extraction", "当前文档仍有未读取区域、未展开栏目或未映射控件，不能将已见字段完成视为整表完成")
            return finish("partial", "本轮已核对当前读取到的字段，但页面覆盖仍有缺口，不等于已全部填写；请先查看信息提取报告，尚未提交")
        if not unresolved and not missing and not coverage_blockers:
            return finish("ready_for_review", "已完成当前已识别资料的填写与核对；请本人检查附件和声明，尚未提交申请")
        if (not unresolved and not coverage_blockers and missing
                and all(item.selector in deferred for item in missing)):
            return finish('needs_user', '其余资料已完成本轮填写与核对；仅本人明确留空的必填项仍缺失，不能提交')
        if any(failed_controls.get(field.selector) == _field_identity(field) for field in snapshot.fields):
            return finish("partial", "仍有控件提交或选中状态未通过核验；已填内容保留，不将显示值相符计为填写成功")
        if safe_actions(review):
            return finish("partial", "仍有未验证字段，已停止本轮有限尝试；已填正确内容保留，可重新核对后继续")
        if any(action.needs_model for action in review.plan.actions):
            return finish("partial", "还有疑难字段尚未解析完成；已填内容保留，可再次分析或按网页原题补充")
        return finish("needs_user", "剩余问题需要你补充、确认或核对；不会让模型猜测个人事实")
    except (ValueError, LookupError) as exc:
        if isinstance(exc, ExecutionTargetChanged) and exc.attempted_issues:
            event("write_interrupted",
                  f"中断前已处理{exc.attempted_count}项，其中{exc.provisional_matches}项曾回读匹配；"
                  "整批最终核对未完成，不计为本轮完成，请先重新核对已填内容",
                  issues=exc.attempted_issues)
        event("stopped", str(exc)[:240])
        return finish("blocked", str(exc)[:240])
