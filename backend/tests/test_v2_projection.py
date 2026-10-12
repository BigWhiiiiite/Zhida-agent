import json
import uuid

from sqlalchemy import text

from app.backend_v2.workbench_projection import preserve_entities, sync_user


def test_workbench_entity_identity_never_matches_by_position():
    previous = {"education": [
        {"school": "甲大学", "degree": "本科", "college": "旧学院", "_entity_id": "bachelor"},
        {"school": "甲大学", "degree": "硕士", "college": "研究院", "_entity_id": "master"}]}
    current = {"education": [
        {"school": "甲大学", "degree": "硕士", "college": "新研究院"},
        {"school": "甲大学", "degree": "本科", "college": "新学院"}]}
    result = preserve_entities(current, previous)
    assert [x["_entity_id"] for x in result["education"]] == ["master", "bachelor"]
    ambiguous = {"education": [current["education"][0], current["education"][0]]}
    assert all("_entity_id" not in item for item in preserve_entities(ambiguous, previous)["education"])
    assert all("_entity_id" not in item for item in current["education"])
    repeated = {"projects": [{"name": "重复名称", "start_date": "2022-01", "_entity_id": "old"}]}
    changed = {"projects": [{"name": "重复名称", "start_date": "2025-01"}]}
    assert "_entity_id" not in preserve_entities(changed, repeated)["projects"][0]


def test_workbench_projection_atomic_versions_and_deletion(v2_db):
    user, old = "proj-" + uuid.uuid4().hex, uuid.uuid4().hex
    profile = {"education": [{"school": "示例大学", "degree": "硕士", "college": "计算学院"}],
               "projects": [{"name": "合成项目", "description": "实现智能体工具调用与检索"}]}
    with v2_db.transaction(user) as cx:
        cx.execute(text("INSERT INTO zhida.users(id) VALUES(:u)"), {"u": user})
        cx.execute(text("""INSERT INTO workbench.resumes
            (id,user_id,filename,label,profile_json,parser,created_at,updated_at)
            VALUES(:id,:u,'sample.txt','合成简历',:p,'test','2026-10-11','2026-10-11')"""),
            {"id": old, "u": user, "p": json.dumps(profile)})
        sync_user(cx, user, {"resumes"})
        mapping = cx.execute(text("SELECT * FROM workbench.projections WHERE user_id=:u"), {"u": user}).mappings().one()
        core = mapping["core_id"]
        entity = mapping["payload"]["profile"]["education"][0]["_entity_id"]
        sync_user(cx, user, {"resumes"})
        assert cx.execute(text("SELECT count(*) FROM zhida.resume_revisions WHERE user_id=:u"), {"u": user}).scalar() == 1
        profile["education"][0]["college"] = "新学院"
        cx.execute(text("UPDATE workbench.resumes SET profile_json=:p WHERE id=:id AND user_id=:u"),
                   {"p": json.dumps(profile), "id": old, "u": user})
        sync_user(cx, user, {"resumes"})
        mapped = cx.execute(text("SELECT payload FROM workbench.projections WHERE user_id=:u"), {"u": user}).scalar_one()
        assert mapped["profile"]["education"][0]["_entity_id"] == entity
        assert cx.execute(text("SELECT count(*) FROM zhida.resume_revisions WHERE user_id=:u"), {"u": user}).scalar() == 2
        assert cx.execute(text("SELECT count(*) FROM zhida.outbox_events WHERE user_id=:u"), {"u": user}).scalar() >= 2
        cx.execute(text("DELETE FROM workbench.resumes WHERE id=:id AND user_id=:u"), {"id": old, "u": user})
        sync_user(cx, user, {"resumes"})
        assert cx.execute(text("SELECT deleted_at IS NOT NULL FROM zhida.resumes WHERE id=:id"), {"id": core}).scalar()
        assert cx.execute(text("SELECT count(*) FROM rag.knowledge_sources WHERE user_id=:u AND revoked_at IS NULL"), {"u": user}).scalar() == 0


def test_profile_projection_reorder_and_empty_profile_deletion(v2_db):
    user = "profile-proj-" + uuid.uuid4().hex
    profile = {"education": [{"school": "A", "degree": "本科"}, {"school": "B", "degree": "硕士"}]}
    with v2_db.transaction(user) as cx:
        cx.execute(text("INSERT INTO zhida.users(id) VALUES(:u)"), {"u": user})
        cx.execute(text("""INSERT INTO workbench.candidate_profiles
            (id,user_id,profile_json,created_at,updated_at) VALUES(:u,:u,:p,'2026-01-01','2026-01-01')"""),
            {"u": user, "p": json.dumps(profile)})
        sync_user(cx, user, {"candidate_profiles"})
        first = cx.execute(text("SELECT payload FROM workbench.projections WHERE user_id=:u"), {"u": user}).scalar_one()
        profile["education"].reverse()
        cx.execute(text("UPDATE workbench.candidate_profiles SET profile_json=:p WHERE user_id=:u"),
                   {"p": json.dumps(profile), "u": user})
        sync_user(cx, user, {"candidate_profiles"})
        second = cx.execute(text("SELECT payload FROM workbench.projections WHERE user_id=:u"), {"u": user}).scalar_one()
        assert [r["_entity_id"] for r in first["profile"]["education"]] == list(reversed([r["_entity_id"] for r in second["profile"]["education"]]))
    empty = "empty-profile-" + uuid.uuid4().hex
    with v2_db.transaction(empty) as cx:
        cx.execute(text("INSERT INTO zhida.users(id) VALUES(:u)"), {"u": empty})
        cx.execute(text("""INSERT INTO workbench.candidate_profiles
            (id,user_id,profile_json,created_at,updated_at) VALUES(:u,:u,'{}','2026-01-01','2026-01-01')"""), {"u": empty})
        sync_user(cx, empty, {"candidate_profiles"})
        cx.execute(text("DELETE FROM workbench.candidate_profiles WHERE user_id=:u"), {"u": empty})
        sync_user(cx, empty, {"candidate_profiles"})
        assert cx.execute(text("SELECT deleted_at IS NOT NULL FROM workbench.projections WHERE user_id=:u"), {"u": empty}).scalar()
