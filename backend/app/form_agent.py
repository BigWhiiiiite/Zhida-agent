from __future__ import annotations

import json

from agents import Agent, Runner

from .browser_models import BrowserSnapshot, FormPlan
from .model_provider import configured_model
from .models import CandidateProfile


SYSTEM_PROMPT = """
你是职达的招聘表单映射 Agent。你只生成填写计划，不操作浏览器。

规则：
1. 每个 action.selector 必须原样复制页面字段提供的 selector。
2. 只使用候选人主档案中明确存在的信息，不猜测，不编造。
3. 姓名、邮箱、电话、城市、网站、教育和项目等明确字段可 fill/select/check。
4. 工作许可、签证担保、薪资、性别、族裔、残障、退伍军人、法律声明、同意条款等字段均 sensitive=true，action=ask_user，除非主档案存在完全明确且直接对应的答案。
5. 文件上传、验证码、密码、登录、最终提交按钮一律 skip。
6. select 的 value 必须是页面 options 中真实存在的完整文本；不能可靠匹配则 ask_user。
7. checkbox 只在含义明确且不是声明/同意/隐私确认时 check。
8. 找不到资料时 ask_user，并写明缺失问题。
9. 置信度低于 0.85 时不要自动填写。
10. 不得输出页面未提供的 selector。
"""


async def create_form_plan(snapshot: BrowserSnapshot, profile: CandidateProfile) -> FormPlan:
    model, settings = configured_model()
    agent = Agent(
        name="Zhida Form Mapper",
        instructions=SYSTEM_PROMPT,
        model=model,
        model_settings=settings,
        output_type=FormPlan,
    )
    prompt = json.dumps({
        "page": snapshot.model_dump(mode="json"),
        "candidate_profile": profile.model_dump(mode="json", exclude={"created_at", "updated_at"}),
    }, ensure_ascii=False)
    result = await Runner.run(agent, prompt, max_turns=3)
    plan = result.final_output
    if not isinstance(plan, FormPlan):
        raise RuntimeError("模型没有返回有效的表单填写计划")

    allowed = {field.selector for field in snapshot.fields}
    plan.actions = [action for action in plan.actions if action.selector in allowed]
    return plan
