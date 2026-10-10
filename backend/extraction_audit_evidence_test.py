"""Anonymous evidence checks through the real review path; no model/network/env files."""
import asyncio
import copy
import json
from types import SimpleNamespace
from unittest.mock import patch

from agents import ModelSettings

from app import extraction_audit as audit
from app.browser_models import BrowserSnapshot, PageField, QuestionEvidence
from app.page_observation import PageRegionObservation


def fixture(question="姓名", *, help_text=""):
    field = PageField(selector="#anonymous-question", question_text=question, label=question,
        label_source="explicit", help_text=help_text,
        question_candidates=[QuestionEvidence(text=question, source="explicit", owned=True)])
    source = BrowserSnapshot(session_id="anonymous", url="https://fixture.example.test/form",
        title="Anonymous", fields=[field])
    page = audit.audit_page(source)
    assert len(page["questions"]) == 1 and not page["questions"][0]["issues"]
    return page


async def review(page, quotes, *, regions=None):
    before = copy.deepcopy(page)
    region_before = {key: item.model_dump() for key, item in (regions or {}).items()}

    async def fake_run(agent, messages, max_turns):
        assert not agent.tools and agent.output_type is audit.AuditBatch and max_turns == 1
        supplied = json.loads(messages[0]["content"][0]["text"])
        assert supplied == page
        return SimpleNamespace(final_output=audit.AuditBatch(questions=[audit.QuestionReview(
            question_id=q["id"], interpretation=q["question_text"], control_kind=q["control_kind"],
            verdict="clear", issue="", next_observation="", evidence=list(quotes[q["id"]]))
            for q in page["questions"]]))

    with patch.dict("os.environ", {"APP_AGENT_PROMPT_JSON_MODELS": ""}), \
            patch.object(audit, "configured_model", return_value=("fixture", ModelSettings())), \
            patch.object(audit.Runner, "run", side_effect=fake_run):
        output = await audit._review_batch(page, "fixture", regions or {})
    assert page == before, "Evidence validation must not rewrite captured questions"
    assert {key: item.model_dump() for key, item in (regions or {}).items()} == region_before
    return output.questions


async def allowed(question, quotes, *, help_text="", regions=None):
    page = fixture(question, help_text=help_text)
    result = (await review(page, {"q1": quotes}, regions=regions))[0]
    assert result.verdict == "clear", (question, result.issue)
    assert result.evidence == quotes


async def rejected(page, quotes):
    result = (await review(page, {"q1": quotes}))[0]
    assert result.verdict == "conflict", (quotes, result.verdict)
    assert not result.evidence, "Invented or machine-only evidence must not survive"
    assert "无法对应原始观察" in result.issue


async def run():
    # Literal quotes/newlines are text, not the JSON escaping used in transport.
    title = '学校名称（请填写"完整名称"）'
    await allowed(title, [title])
    await allowed("开始日期\n至\n结束日期", ["开始日期 至 结束日期"])
    await allowed("姓名", ["姓名"])
    await allowed("姓名", ["与证件一致"], help_text="请填写与证件一致的姓名")

    # Do not grant semantic paraphrases or concatenate neighboring text leaves.
    await rejected(fixture("个人资料", help_text="填写全名"), ["个人资料填写全名"])
    await rejected(fixture("学校名称"), ["毕业院校"])
    await rejected(fixture("学校名称"), ["学校名称（必填）"])
    # Shared scope text is valid, but another question's title in the document
    # issue list cannot prove ownership of this question.
    shared = fixture("姓名")
    shared["extraction_report"]["limitations"].append("存在嵌入区域尚未读取")
    result = (await review(shared, {"q1": ["存在嵌入区域尚未读取"]}))[0]
    assert result.verdict == "clear"
    shared["extraction_report"]["issues"].append({"label": "其他题名", "reason": "缺口", "selector": "#other"})
    await rejected(shared, ["其他题名"])
    page = fixture()
    await rejected(page, [page["questions"][0]["id"]])
    await rejected(page, [page["questions"][0]["targets"][0]["selector"]])

    # A region's DOM AX tree itself is JSON stored inside an accessibility string.
    # Decode its content, but never treat tree ids/selectors/keys as webpage quotes.
    caption = '请填写"学历名称"\n及毕业时间'
    region = PageRegionObservation(selector="#anonymous-question", accessibility_source="dom_aria",
        accessibility=json.dumps([{"role": "label", "name": caption,
            "id": "anonymous-ax-id", "selector": "#anonymous-ax-target"}], ensure_ascii=False))
    await allowed("学历信息", ['请填写"学历名称" 及毕业时间'], regions={region.selector: region})
    region_page = fixture("学历信息")
    for machine_quote in ("anonymous-ax-id", "#anonymous-ax-target"):
        result = (await review(region_page, {"q1": [machine_quote]}, regions={region.selector: region}))[0]
        assert result.verdict == "conflict" and not result.evidence

    # Even an exact title and a clear model opinion cannot erase a local gap.
    page = fixture("学历信息")
    page["questions"][0]["issues"] = ["教育记录归属尚未确定"]
    result = (await review(page, {"q1": ["学历信息"]}))[0]
    assert result.verdict == "needs_observation" and result.evidence == ["学历信息"]
    assert "教育记录归属尚未确定" in result.issue

    # A valid quote stays visible when another submitted quote is fabricated.
    page = fixture("学校名称")
    result = (await review(page, {"q1": ["学校名称", "毕业院校"]}))[0]
    assert result.verdict == "conflict" and result.evidence == ["学校名称"]
    print("extraction_audit_evidence_test: OK (decoded literal quotes, no fabricated/cross-leaf/id evidence, local gaps retained)")


if __name__ == "__main__":
    asyncio.run(run())
