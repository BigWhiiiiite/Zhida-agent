"""Owner-scoped, process-local run receipts. Never retry writes on disconnect.

Receipts deliberately expire and do not survive a backend restart. Confirmed
facts live in the existing persistent answer store, not these transient plans.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass

from .browser_models import ApplicationAssistProgress


def safe_interrupt_reason(error):
    """Classify owned validation failures; never expose arbitrary exception text."""
    if isinstance(error, ValueError):
        for marker, message in (
            ('申请表仍在加载或选项尚未稳定', '检查申请表结构的等待时间已用完；这不代表网页一定未加载。请先只读核对当前表单。'),
            ('表单已加载，但读取网页选项超时', '申请表已加载，但读取下拉选项超时；尚未通过本次读取检查。'),
            ('关闭网页选项预览超时', '选项预览未能安全关闭；请在官网关闭下拉菜单后同步。'),
            ('申请表关键栏目或选项尚未稳定', '连续读取的栏目或选项仍有变化，未通过稳定性检查。'),
            ('读取选项时表单题目或记录归属已变化', '展开选项后，题目或记录归属发生变化；旧计划已停止。'),
            ('网页地址已变化', '招聘页地址发生变化；请先核对当前页面。'),
            ('网页已离开本次申请表', '招聘页已离开本次申请表；不会跟随跳转继续填写。'),
            ('档案或已确认答案发生变化', '简历、档案或答案发生变化；旧计划已停止。'),
            ('你已要求暂停', '已按你的要求暂停，下一次写入前停止。'),
        ):
            if marker in str(error):
                return message
    return '本轮处理已中断，具体原因未能安全分类。'


@dataclass
class Receipt:
    owner: str
    session: str
    fingerprint: str
    updated: float
    progress: ApplicationAssistProgress


class AssistRuns:
    def __init__(self):
        self.receipts = {}

    def get(self, run_id, owner, session):
        receipt = self.receipts.get(run_id)
        if receipt and receipt.progress.status != 'running' and time.monotonic()-receipt.updated >= 3600:
            self.receipts.pop(run_id, None)
            receipt = None
        if not receipt or (receipt.owner, receipt.session) != (owner, session):
            raise LookupError('本轮记录不存在或后端已重启；请重新读取网页，不会自动重复填写')
        return receipt.progress

    def begin(self, run_id, owner, session, payload):
        fingerprint = hashlib.sha256(payload.model_dump_json().encode()).hexdigest()
        old = self.receipts.get(run_id)
        if old:
            progress = self.get(run_id, owner, session)
            if old.fingerprint != fingerprint:
                raise ValueError('同一轮请求不能更换简历、授权或留空项')
            if progress.status == 'finished' and progress.result is not None:
                return progress, True
            raise ValueError('本轮仍在运行或已中断；请读取本轮进度，不会重复执行')
        if any(r.owner == owner and r.session == session and r.progress.status == 'running'
               for r in self.receipts.values()):
            raise ValueError('当前招聘页已有填写任务，请先等待或暂停该任务')
        now = time.monotonic()
        self.receipts = {k:r for k,r in self.receipts.items()
                         if r.progress.status == 'running' or now - r.updated < 3600}
        finished = sorted((r.updated,k) for k,r in self.receipts.items() if r.progress.status != 'running')
        for _, key in finished[:max(0, len(self.receipts)-63)]:
            self.receipts.pop(key, None)
        progress = ApplicationAssistProgress(run_id=run_id)
        self.receipts[run_id] = Receipt(owner, session, fingerprint, now, progress)
        return progress, False

    def emit(self, run_id, event):
        receipt = self.receipts[run_id]
        receipt.updated = time.monotonic()
        progress = receipt.progress
        progress.phase, progress.message = event.kind, event.message
        progress.events.append(event.model_copy(deep=True))

    def finish(self, run_id, result):
        receipt = self.receipts[run_id]
        receipt.updated = time.monotonic()
        receipt.progress.status = 'finished'
        receipt.progress.phase = result.status
        receipt.progress.message = result.message
        receipt.progress.result = result

    def interrupt(self, run_id, error=None):
        receipt = self.receipts[run_id]
        receipt.updated = time.monotonic()
        receipt.progress.status = 'interrupted'
        receipt.progress.phase = 'interrupted'
        receipt.progress.message = (safe_interrupt_reason(error) +
            ' 请先同步核对网页；不会自动重发填写，未最终提交。')


assist_runs = AssistRuns()
