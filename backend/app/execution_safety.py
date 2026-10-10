"""Safe interruption receipts, not a successful/finally verified execution.

Keep only question labels and outcome categories. Never copy entered values,
selectors, raw exceptions, credentials or page HTML into progress events.
"""
from .browser_models import ApplicationAssistIssue


IDENTITY_PARTS = (
    ("url", "网页地址"), ("stage", "申请阶段"), ("job_id", "岗位标识"),
    ("job_title", "岗位标题"), ("page_step_current", "表单步骤"),
    ("entry_confirmation_required", "入口确认状态"),
)

EXECUTION_PHASES = {
    'target_check': '写入前页面身份检查',
    'resolve': '定位当前题目控件',
    'popup_check': '检查当前题目已有面板',
    'open_popup': '展开当前题目的面板',
    'read_options': '读取当前题目的选项',
    'search_options': '在当前题目中检索选项',
    'select_option': '选择已核实的目标选项',
    'date_precision': '核实当前日历格式',
    'write_value': '写入已确认资料',
    'read_value': '读取当前题目实际值',
    'settle': '等待页面稳定后复核',
    'final_verify': '整批最终回读',
}


def execution_phase_label(phase):
    return EXECUTION_PHASES.get(phase, '执行阶段尚未分类')


def execution_identity_changes(before, after):
    # Options, field counts and values may change within one reactive form.
    # This describes the same existing guard, without loosening its boundary.
    return [label for key, label in IDENTITY_PARTS
            if getattr(before, key) != getattr(after, key)]


def interrupted_failure_reason(message):
    """Expose fixed diagnostic categories, never arbitrary exception text.

    Losing the cause of an earlier failed widget when a later navigation stops
    the batch made every field look like the same generic interruption.
    """
    for prefix, reason in (
        ('当前日历尚无经过验证的日期输入方式', '日历输入方式尚未支持，未强写只读日期'),
        ('只读日历中尚未找到唯一、可选的目标日期', '只读日历尚未定位到唯一可选的目标日期'),
        ('当前日历输入不可操作', '日历输入不可操作，未强写日期'),
        ('当前日历精度与计划不同', '日历格式与计划不一致，旧日期计划停止'),
        ('日历格式尚未核实', '日历格式尚未核实，没有填写'),
        ('日期输入未形成对应的日历选中状态', '日期没有形成真实日历选中状态'),
        ('日历选中状态与输入回读不一致', '日历选中状态与显示值不一致'),
        ('未能唯一核实当前控件展开的面板', '未能核实当前题目自己的选项面板'),
        ('当前级联层级无法唯一对应已确认路径', '地区层级无法唯一匹配，未代选其他地区'),
        ('提供的路径未到最终可选层级', '地区路径缺少官网要求的下级信息'),
    ):
        if str(message).startswith(prefix):
            return reason + '；整批最终核对未完成，需重新核对'
    return '中断前未通过回读；整批最终核对未完成，需重新核对'


class ExecutionTargetChanged(ValueError):
    def __init__(self, message, *, results=(), current_label="", current_phase=""):
        super().__init__(message)
        attempted = [row for row in results if row.status in {"filled", "failed"}]
        self.attempted_count = len(attempted)
        self.provisional_matches = sum(row.verified and row.status == "filled" for row in attempted)
        self.attempted_issues = [ApplicationAssistIssue(
            label=row.label[:160],
            message=("中断前回读匹配，但整批最终核对未完成，需重新核对"
                     if row.verified and row.status == "filled" else
                     interrupted_failure_reason(row.message)),
        ) for row in attempted]
        if current_label:
            self.attempted_issues.append(ApplicationAssistIssue(
                label=current_label[:160],
                message=("安全检查在处理此题时停止，无法确认当前结果，需重新核对"
                         + ("；停点：" + execution_phase_label(current_phase) if current_phase else ""))))
