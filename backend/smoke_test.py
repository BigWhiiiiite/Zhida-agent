from fastapi.testclient import TestClient

from app.main import app


with TestClient(app) as client:
    health = client.get("/api/health")
    assert health.status_code == 200
    files = {
        "file": (
            "test.txt",
            "姓名：李春博\n性别：男\n年龄：24\n邮箱：test@example.com\n"
            "手机号：13800138000\n技能\nPython，FastAPI\n项目经历\nAgent 求职助手".encode(),
            "text/plain",
        )
    }
    created = client.post("/api/resumes", files=files)
    assert created.status_code == 201, created.text
    assert created.json()["profile"]["name"] == "李春博"
    resume_id = created.json()["id"]
    saved = client.patch(
        f"/api/resumes/{resume_id}",
        json={"label": "Agent 岗位简历", "profile": created.json()["profile"]},
    )
    assert saved.status_code == 200
    assert saved.json()["label"] == "Agent 岗位简历"
    assert any(item["id"] == resume_id for item in client.get("/api/resumes").json())

print("backend smoke test passed")
