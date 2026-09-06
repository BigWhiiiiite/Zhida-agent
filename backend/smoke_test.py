"""Run with: .venv/bin/python smoke_test.py"""
import asyncio
import os
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from agents.usage import Usage
from fastapi.testclient import TestClient

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
        assert client.get("/api/export").status_code == 200
        assert client.delete(f"/api/resumes/{first_json['id']}").status_code == 204
        assert client.get(f"/api/resumes/{first_json['id']}").status_code == 404

print("Zhida phase-one smoke test passed")
