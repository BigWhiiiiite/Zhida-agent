"""Opt-in real provider probe using only synthetic applicant/form data.

This is NOT an ATS test. It never opens a browser, touches a user DB, or reads a
resume. Only the configured model provider may be contacted; the local .env is
loaded by the normal product factory and no credentials/errors are printed.
Run explicitly with --allow-configured-model; excluded from offline tests.
"""
import argparse
import asyncio
import json
import time
from unittest.mock import patch

from app.browser_models import BrowserSnapshot, PageField
from app.form_agent import create_form_plan
from app.models import CandidateProfile


async def run():
    page=BrowserSnapshot(session_id="synthetic-live-probe",url="https://fixture.example.test/application",
        title="匿名字段映射测试，不是真实招聘网页",fields=[PageField(selector="#notification",
        label="通知接收地址",question_text="通知接收地址",context="请填写本人电子邮箱，用于接收本次申请通知。",
        field_type="text",required=True)])
    profile=CandidateProfile(name="合成测试人",email="synthetic-candidate@example.test")
    started=time.monotonic()
    with patch("sqlite3.connect",side_effect=AssertionError("No applicant database in a live synthetic probe")), \
         patch("app.application_knowledge.retrieve_knowledge",return_value={}):
        try:
            result=await asyncio.wait_for(create_form_plan(page,profile),timeout=180)
            action=next((item for item in result.actions if item.selector=="#notification"),None)
            verified=bool(action and action.resolution_source=="model" and action.action=="fill"
                          and action.value==profile.email and action.confidence>=.85)
            print(json.dumps({"status":"grounded_model_mapping_passed" if verified else "mapping_not_verified",
                "latency_seconds":round(time.monotonic()-started,2),
                "resolution_source":action.resolution_source if action else "missing",
                "action":action.action if action else "missing", "grounded_value_matches":verified,
                "uses_synthetic_data_only":True,"browser_opened":False},ensure_ascii=False))
            return verified
        except Exception as error:
            print(json.dumps({"status":"probe_failed","error_type":type(error).__name__,
                "latency_seconds":round(time.monotonic()-started,2),"uses_synthetic_data_only":True},ensure_ascii=False))
            return False


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-configured-model",action="store_true")
    args=parser.parse_args()
    if not args.allow_configured_model:
        parser.error("This makes a model request. Pass --allow-configured-model explicitly.")
    raise SystemExit(0 if asyncio.run(run()) else 1)
