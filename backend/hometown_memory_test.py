"""Offline real-storage regression: personal hometown survives CV/site changes."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app import storage
from app.application_knowledge import company_scope
from app.browser_models import BrowserSnapshot, PageField
from app.form_agent import _direct_profile_value, create_local_form_plan
from app.models import CandidateProfile, ResumeProfile, ResumeRecord
from app.profile_service import save_application_answer
from app.region_facts import hometown_for_question, merge_confirmed_hometown, region_parts
from app.task_profile import compose_task_profile


def run():
    # Synthetic data only. Never touch the developer's actual profile/database.
    full = "河北省保定市莲池区"
    parts = region_parts(full)
    assert (parts.province, parts.city, parts.district) == ("河北省", "保定市", "莲池区")
    assert region_parts("内蒙古自治区呼和浩特市新城区").province == "内蒙古自治区"
    assert region_parts("北京市朝阳区").city == "北京市"
    assert not region_parts("北京").province  # do not invent an unprovided hierarchy
    assert not region_parts("河北省保定市某街道").province
    assert hometown_for_question(full, "籍贯") == full
    assert hometown_for_question(full, "籍贯省份") == "河北省"
    assert hometown_for_question(full, "籍贯城市") == "保定市"
    assert hometown_for_question(full, "籍贯区县") == "莲池区"
    assert hometown_for_question("河北省", "籍贯城市") == ""
    assert merge_confirmed_hometown(full, "河北省", "籍贯") == full
    assert merge_confirmed_hometown(full, "保定市", "籍贯城市") == full
    assert merge_confirmed_hometown(full, "河北", "籍贯省份") == full
    assert merge_confirmed_hometown(full, "山东省", "籍贯省份") == "山东省"
    assert merge_confirmed_hometown(full, "石家庄市", "籍贯城市") == "石家庄市"

    with TemporaryDirectory(prefix="zhida-hometown-") as directory, \
            patch.object(storage, "DATA_DIR", Path(directory)), \
            patch.object(storage, "DB_PATH", Path(directory) / "test.db"):
        token = storage.set_current_user("fixture-user-a")
        try:
            storage.initialize()
            storage.get_profile()
            storage.save_profile(ResumeProfile(location="上海市"))
            now = datetime.now(timezone.utc)
            records = [ResumeRecord(id=i, filename=i + ".pdf", label=i, profile=ResumeProfile(),
                parser="fixture", status="completed", created_at=now, updated_at=now)
                for i in ("cv-agent", "cv-backend")]
            for record in records:
                storage.create_pending_resume(record.id, record.filename, record.filename, record.label,
                    "fixture", "中文", 0, record.id, "")
                storage.replace_parse_result(record.id, record.profile, "fixture", [], "")
            first_url = "https://tenant-a.zhiye.com/form"
            second_url = "https://tenant-b.zhiye.com/form"
            save_application_answer("籍贯", "", full, semantic_key="candidate.hometown",
                entity_scope="candidate", resume_id=records[0].id, source_url=first_url)
            # Reload the persisted profile, not a same-process conversational value.
            master = storage.get_profile()
            assert master.hometown == full and master.location == "上海市"
            for record in records:
                task = compose_task_profile(master, record, company_scope(second_url))
                assert not task.application_answer_memory  # old site's answers don't leak
                for title, expected in (("籍贯", full), ("籍贯省份", "河北省"),
                                        ("籍贯城市", "保定市"), ("籍贯区县", "莲池区")):
                    field = PageField(selector="#native", label=title, question_text=title,
                        label_source="explicit", semantic_key="candidate.hometown", required=True)
                    assert _direct_profile_value(field, task) == (expected, "主档案.hometown")
                    snapshot = BrowserSnapshot(session_id="fixture", url=second_url, title="匿名表单", fields=[field])
                    action = create_local_form_plan(snapshot, task).actions[0]
                    assert action.action == "fill" and action.value == expected and not action.needs_model
                city = PageField(selector="#native-city", label="籍贯城市", question_text="籍贯城市",
                    label_source="explicit", semantic_key="candidate.hometown", field_type="select-one",
                    options=["保定", "上海市"])
                snapshot = BrowserSnapshot(session_id="fixture", url=second_url, title="匿名表单", fields=[city])
                assert create_local_form_plan(snapshot, task).actions[0].value == "保定"
            # Coarser choices on the next website must retain known descendants.
            save_application_answer("籍贯省份", "", "河北", semantic_key="candidate.hometown",
                resume_id=records[1].id, source_url=second_url)
            assert storage.get_profile().hometown == full
            save_application_answer("业务组偏好", "", "基础平台", resume_id=records[0].id, source_url=first_url)
            task = compose_task_profile(storage.get_profile(), records[1], company_scope(second_url))
            assert all(item.question != "业务组偏好" for item in task.application_answer_memory)
            other = storage.set_current_user("fixture-user-b")
            try:
                assert storage.get_profile().hometown == ""
                assert storage.get_resume(records[0].id) is None
            finally:
                storage.reset_current_user(other)
        finally:
            storage.reset_current_user(token)
    print("hometown_memory_test: OK (persistent personal fact, province/city/district, CV/site reuse, finer facts retained, user isolation)")


if __name__ == "__main__":
    run()
