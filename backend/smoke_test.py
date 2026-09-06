"""Run with: .venv/bin/python smoke_test.py"""
import asyncio
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from agents.usage import Usage
from fastapi.testclient import TestClient
from openai import APIConnectionError

from app import main, storage
from app.model_provider import normalize_proxy_response

os.environ["APP_AGENT_MODE"] = "rules"


async def verify_proxy_normalization() -> None:
    response = httpx.Response(
        200,
        headers={"content-type": "application/json"},
        request=httpx.Request("POST", "https://proxy.example/v1/responses"),
        json={
            "usage": {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            }
        },
    )
    await normalize_proxy_response(response)
    assert response.json()["usage"]["input_tokens_details"]["cache_write_tokens"] == 0


asyncio.run(verify_proxy_normalization())
assert Usage().input_tokens_details.cached_tokens == 0
assert logging.getLogger("openai.agents").level == logging.CRITICAL


class UnavailableExtractor:
    name = "unavailable-test-provider"

    async def parse(self, _: str):
        raise APIConnectionError(request=httpx.Request("POST", "https://proxy.example/v1/responses"))


with TemporaryDirectory() as temporary:
    root = Path(temporary)
    storage.DB_PATH = root / "test.db"
    main.UPLOAD_DIR = root / "uploads"

    with TestClient(main.app) as client:
        assert client.get("/api/health").json()["product"] == "Zhida"
        initial = client.get("/api/profile")
        assert initial.status_code == 200

        first_text = (
            "姓名：李春博\n性别：男\n年龄：24\n邮箱：first@example.com\n"
            "手机号：13800138000\n技能\nPython，FastAPI\n项目经历\nAgent 求职助手"
        ).encode()
        first = client.post("/api/resumes", files={"file": ("agent.txt", first_text, "text/plain")})
        assert first.status_code == 201, first.text
        first_json = first.json()
        assert first_json["profile"]["name"] == "李春博"
        assert first_json["evidence"]

        duplicate = client.post("/api/resumes", files={"file": ("copy.txt", first_text, "text/plain")})
        assert duplicate.status_code == 409

        profile = client.get("/api/profile").json()
        assert profile["email"] == "first@example.com"

        second_text = "姓名：李春博\n邮箱：new@example.com\n技能\nPydanticAI，React".encode()
        second = client.post("/api/resumes", files={"file": ("english.txt", second_text, "text/plain")})
        assert second.status_code == 201, second.text
        conflicts = client.get("/api/conflicts").json()
        email_conflict = next(item for item in conflicts if item["field_path"] == "email")
        resolved = client.post(f"/api/conflicts/{email_conflict['id']}/resolve", json={"choice": "incoming"})
        assert resolved.status_code == 200
        assert client.get("/api/profile").json()["email"] == "new@example.com"

        evidence_id = first_json["evidence"][0]["id"]
        reviewed = client.patch(
            f"/api/resumes/{first_json['id']}/evidence/{evidence_id}", json={"status": "confirmed"}
        )
        assert reviewed.status_code == 200
        assert reviewed.json()["evidence"][0]["status"] == "confirmed"

        assert client.get(f"/api/resumes/{first_json['id']}/download").status_code == 200
        assert client.post(f"/api/resumes/{first_json['id']}/parse").status_code == 200

        original_extractor = main.get_resume_extractor
        main.get_resume_extractor = lambda: UnavailableExtractor()
        try:
            failed_text = "姓名：网络故障测试\n邮箱：retry@example.com".encode()
            failed = client.post("/api/resumes", files={"file": ("retry.txt", failed_text, "text/plain")})
            assert failed.status_code == 503, failed.text
            failed_id = failed.json()["detail"]["resume_id"]
            retained = client.get(f"/api/resumes/{failed_id}").json()
            assert retained["status"] == "failed"
            assert retained["error_message"]
            assert (main.UPLOAD_DIR / f"{failed_id}.txt").exists()
        finally:
            main.get_resume_extractor = original_extractor
        retried = client.post(f"/api/resumes/{failed_id}/parse")
        assert retried.status_code == 200, retried.text
        assert retried.json()["status"] == "needs_review"

        assert client.get("/api/export").status_code == 200
        assert client.delete(f"/api/resumes/{first_json['id']}").status_code == 204
        assert client.get(f"/api/resumes/{first_json['id']}").status_code == 404

print("Zhida phase-one smoke test passed")
