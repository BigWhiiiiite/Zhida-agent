"""Pure anonymous target/job mismatch checks. No browser, network or model."""
from app.application_models import ApplicationWorkflowState
from app.browser_models import ApplicationTarget
from app.target_identity import target_identity_blocker


def state(*, source="https://fixture.zhiye.com/custom/campusdetail?jobAdId=one",
          url="https://fixture.zhiye.com/form?jobAdId=one", target="系统研发工程师",
          observed="系统研发工程师（2027校招）(T10001)", stage="application_form"):
    return ApplicationWorkflowState(session_id="anonymous", url=url, title="不能作为岗位证据",
        stage=stage, job_title=observed, target=ApplicationTarget(job_title=target, source_url=source))


def run():
    for stage in ("job_detail", "profile_form", "application_form", "review"):
        changed = state(url="https://fixture.zhiye.com/form?jobAdId=two", stage=stage)
        original = changed.model_dump()
        assert "岗位编号" in target_identity_blocker(changed)
        assert changed.model_dump() == original
        assert "职位名称" in target_identity_blocker(state(observed="前端开发工程师", stage=stage))
    assert not target_identity_blocker(state())
    assert not target_identity_blocker(state(source="https://fixture.zhiye.com:443/custom/detail?JOBADID=one"))
    assert not target_identity_blocker(state(
        source="https://fixture.zhiye.com/custom/detail?jobAdId=ABCD1234-AAAA-BBBB-CCCC-EEEEFFFFFFFF",
        url="https://fixture.zhiye.com/form?jobAdId=abcd1234-aaaa-bbbb-cccc-eeeeffffffff"))
    assert "岗位编号" in target_identity_blocker(state(
        source="https://fixture.zhiye.com/#/job/one", url="https://fixture.zhiye.com/#/job/two"))
    # Explicit source identity continues to protect even an absent/broad title.
    assert "岗位编号" in target_identity_blocker(state(target="AI", observed="", url="https://fixture.zhiye.com/form?jobAdId=two"))
    # Missing/ambiguous identifiers, cross-origin namespaces and login stages
    # must not manufacture a same-job or different-job conclusion.
    for source, url in (
        ("", "https://fixture.zhiye.com/form?jobAdId=two"),
        ("https://fixture.zhiye.com/jobs", "https://fixture.zhiye.com/form?jobAdId=two"),
        ("https://fixture.zhiye.com/?jobAdId=one&jobAdId=two", "https://fixture.zhiye.com/form?jobAdId=three"),
        ("https://fixture.zhiye.com/?jobAdId=one", "https://login.example.test/form?jobAdId=two"),
        ("https://fixture.zhiye.com/?jobAdId=one", "https://fixture.zhiye.com:8443/form?jobAdId=two"),
        ("https://fixture.zhiye.com:invalid/?jobAdId=one", "https://fixture.zhiye.com/form?jobAdId=two"),
    ):
        assert not target_identity_blocker(state(source=source, url=url))
    for stage in ("homepage", "job_list", "unknown", "auth_required", "verification_required", "registration_required"):
        assert not target_identity_blocker(state(stage=stage, observed="完全不同的销售管理岗",
            url="https://fixture.zhiye.com/login?jobAdId=two"))
    for target, observed in (
        ("", "系统研发工程师"), ("系统研发工程师", ""),
        ("AI", "应用开发工程师（2027校招）"), ("Agent", "系统研发工程师"),
        ("算法", "机器学习开发工程师"), ("系统研发工程师", "职位详情"),
        ("系统研发工程师", "技术中心—高级系统研发工程师（北京）"),
        ("系统研发工程师（北京）", "系统研发工程师"),
        ("系统研发岗", "系统研发工程师"),
        ("2027届校招-系统研发工程师（北京）(J89168)", "你正在投递职位：系统研发工程师(北京)【2027校招】(T10001)"),
        ("AI Agent 开发工程师", "人工智能智能体开发工程师"),
        ("Software Engineer", "软件开发工程师"),
    ):
        assert not target_identity_blocker(state(source="", target=target, observed=observed)), (target, observed)
    assert "职位名称" in target_identity_blocker(state(source="", target="后端开发工程师", observed="前端开发工程师"))
    assert "职位名称" in target_identity_blocker(state(source="", target="C++开发工程师", observed="C#开发工程师"))
    assert "职位名称" in target_identity_blocker(state(source="", target="System Software Engineer", observed="Sales Manager"))
    print("target_identity_test: OK (same-origin IDs, explicit title conflicts, normalization, broad/missing/login gates, no mutation)")


if __name__ == "__main__":
    run()
