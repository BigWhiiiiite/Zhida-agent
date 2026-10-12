"""A: real PostgreSQL transaction, scope, version and restart contracts."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.backend_v2.db import Database
from app.backend_v2.dispatch import get_handlers
from app.backend_v2.repository import Conflict, NotFound, Repository, new_id
from app.backend_v2.workers import WorkerDispatcher


@pytest.fixture
def owner(v2_db):
    user = "repo-test-" + uuid4().hex
    Repository(v2_db).ensure_user(user)
    return user


def resume(db, user, *, title="Agent版"):
    return Repository(db).save_resume(user, name=title, profile={
        "education": [{"school": "合成大学", "degree": "本科", "college": "计算机学院"},
                      {"school": "合成大学", "degree": "硕士", "college": "人工智能学院"}],
        "projects": [{"name": "合成智能体项目", "description": "开发 Agent 工具调用与RAG检索"}]})


def run(db, user, revision, key="same-request-key"):
    return Repository(db).create_run(user, resume_revision_id=revision,
        application_url="https://recruit.example.invalid/form", idempotency_key=key)


def test_resume_entities_stay_separate_across_degree_and_revision(v2_db, owner):
    repo = Repository(v2_db)
    first = resume(v2_db, owner)
    identities = [item["_entity_id"] for item in first["profile"]["education"]]
    assert len(set(identities)) == 2
    changed = first["profile"]
    changed["education"].reverse()
    second = repo.save_resume(owner, resume_id=first["id"], expected_revision=1, name="调整排序", profile=changed)
    assert [item["_entity_id"] for item in second["profile"]["education"]] == identities[::-1]
    with pytest.raises(Conflict):
        repo.save_resume(owner, resume_id=first["id"], expected_revision=1, name="过期写入", profile={})
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT count(*) FROM zhida.resume_revisions WHERE resume_id=:id"),
                          {"id": first["id"]}).scalar() == 2
        assert cx.execute(text("SELECT count(*) FROM zhida.experience_entities WHERE resume_id=:id"),
                          {"id": first["id"]}).scalar() == 3


def test_resume_owner_and_same_resume_fk_are_enforced(v2_db, owner):
    first, second = resume(v2_db, owner), resume(v2_db, owner, title="后端版")
    other = "repo-other-" + uuid4().hex
    Repository(v2_db).ensure_user(other)
    with pytest.raises(NotFound):
        run(v2_db, other, first["revision_id"])
    with pytest.raises(NotFound):
        Repository(v2_db).save_resume(owner, resume_id=second["id"], expected_revision=1,
                                     name="错误串用", profile=first["profile"])
    with pytest.raises(IntegrityError):
        with v2_db.transaction(owner) as cx:
            cx.execute(text("UPDATE zhida.resumes SET current_revision_id=:rev WHERE id=:id"),
                       {"rev": second["revision_id"], "id": first["id"]})


def test_repeated_and_concurrent_run_request_is_one_durable_task(v2_db, owner):
    selected = resume(v2_db, owner)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: run(v2_db, owner, selected["revision_id"]), range(2)))
    assert results[0]["id"] == results[1]["id"]
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("""SELECT count(*) FROM zhida.background_jobs
            WHERE user_id=:u AND kind='workflow.advance'"""), {"u": owner}).scalar() == 1
    with pytest.raises(Conflict):
        run(v2_db, owner, selected["revision_id"], "different-request")
    # A new connection pool simulates API restart; state/event sequence survives.
    fresh = Database(v2_db.engine.url.render_as_string(hide_password=False))
    try:
        saved = Repository(fresh).get_run(owner, results[0]["id"])
        assert saved["status"] == "queued"
        assert Repository(fresh).events(owner, saved["id"])[0]["seq"] == 1
    finally:
        fresh.close()


def test_fact_correction_revokes_pending_plans_and_synchronizes_rag_source(v2_db, owner):
    repo = Repository(v2_db)
    active = run(v2_db, owner, resume(v2_db, owner)["revision_id"])
    observation, plan, question = new_id(), new_id(), new_id()
    with v2_db.transaction(owner) as cx:
        cx.execute(text("""INSERT INTO zhida.form_observations
            VALUES(:id,:u,:r,'document-1',1,'{}','{}')"""), {"id": observation, "u": owner, "r": active["id"]})
        cx.execute(text("""INSERT INTO zhida.questions(id,user_id,observation_id,label,section,control_type,evidence_refs)
            VALUES(:id,:u,:o,'可实习日期','本人','date','{}')"""), {"id": question, "u": owner, "o": observation})
        cx.execute(text("""INSERT INTO zhida.fill_plans(id,user_id,run_id,run_revision,observation_id,fact_revision_refs)
            VALUES(:id,:u,:r,1,:o,'[]')"""), {"id": plan, "u": owner, "r": active["id"], "o": observation})
        cx.execute(text("""INSERT INTO zhida.action_ledger
            (action_id,user_id,run_id,plan_id,question_id,ordinal,action_type,target_ref,desired_value_ref)
            VALUES(:id,:u,:r,:p,:q,0,'set_date','{}','{}')"""),
            {"id": new_id(), "u": owner, "r": active["id"], "p": plan, "q": question})
    first = repo.save_fact(owner, subject_id="person", semantic_key="availability", scope_key="global",
                           value="2027-02-01", question="最早可实习时间")
    second = repo.save_fact(owner, subject_id="person", semantic_key="availability", scope_key="global",
                            value="2027-03-01", expected_revision=1)
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT status FROM zhida.fill_plans WHERE id=:id"), {"id": plan}).scalar() == "invalidated"
        source = cx.execute(text("SELECT * FROM rag.knowledge_sources WHERE id=:id"),
                            {"id": "fact:" + first["id"]}).mappings().one()
        assert source["revision_no"] == 2
        records = cx.execute(text("SELECT records FROM rag.source_revisions WHERE id=:id"),
                             {"id": source["current_revision_id"]}).scalar()
        assert records[0]["source_path"] == "fact:" + second["revision_id"]
        assert "2027-03-01" in records[0]["text"]
    assert repo.get_run(owner, active["id"])["run_revision"] == 3
    with pytest.raises(Conflict):
        repo.save_fact(owner, subject_id="person", semantic_key="availability", scope_key="global", value="bad", expected_revision=1)


def test_confirmation_restarts_with_versioned_answer_and_no_browser_work(v2_db, owner):
    repo = Repository(v2_db)
    active = run(v2_db, owner, resume(v2_db, owner)["revision_id"])
    question_id = new_id()
    with v2_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.application_runs SET status='awaiting_user' WHERE id=:r"), {"r": active["id"]})
        cx.execute(text("""INSERT INTO zhida.confirmation_requests
            (id,user_id,run_id,question_revision,label,semantic_key,subject_id,scope_key,options,reason)
            VALUES(:id,:u,:r,1,'是否可提前实习','availability.early','person','global','["是","否"]','missing_fact')"""),
            {"id": question_id, "u": owner, "r": active["id"]})
    with pytest.raises(ValueError):
        repo.answer(owner, active["id"], question_id, value="可能", expected_revision=1)
    answered = repo.answer(owner, active["id"], question_id, value="是", expected_revision=1)
    assert answered["revision_no"] == 1
    with pytest.raises(Conflict):
        repo.answer(owner, active["id"], question_id, value="否", expected_revision=1)
    pending = repo.get_run(owner, active["id"])
    assert pending["status"] == "queued" and not pending["questions"]
    with v2_db.transaction(owner) as cx:
        event = cx.execute(text("""SELECT current_event_id FROM zhida.background_jobs
            WHERE user_id=:u AND kind='workflow.advance' AND expected_revision=2"""), {"u": owner}).scalar_one()
    outcome = WorkerDispatcher(v2_db, handlers=get_handlers(v2_db)).dispatch(event_id=event, user_id=owner)
    assert outcome["status"] == "awaiting_browser"
    assert repo.events(owner, active["id"])[-1]["payload"]["browser_actions_executed"] == 0


def test_unconfirmed_revoked_and_opaque_scoped_facts_do_not_become_global_vectors(v2_db, owner):
    repo = Repository(v2_db)
    fact = repo.save_fact(owner, subject_id="person", semantic_key="skill", scope_key="global", value="了解",
                          confirmation="unconfirmed", question="技能程度")
    assert repo.facts(owner) == []
    repo.save_fact(owner, subject_id="person", semantic_key="skill", scope_key="global", value="熟悉",
                   expected_revision=1, confirmation="confirmed", question="技能程度")
    repo.save_fact(owner, subject_id="person", semantic_key="skill", scope_key="global", value="熟悉",
                   expected_revision=2, confirmation="revoked")
    assert repo.facts(owner) == []
    repo.save_fact(owner, subject_id="person", semantic_key="skill", scope_key="global", value="精通",
                   expected_revision=3, confirmation="confirmed")
    assert repo.facts(owner)[0]["value"] == "精通"
    scoped = repo.save_fact(owner, subject_id="project-1", semantic_key="role", scope_key="entity:project-1",
                            value="核心开发", question="项目角色")
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT id FROM rag.knowledge_sources WHERE id=:id"),
                          {"id": "fact:" + scoped["id"]}).first() is None


def test_same_question_retains_separate_education_subjects_and_latest_answers(v2_db, owner):
    repo = Repository(v2_db)
    for subject, value in (("bachelor", "计算机学院"), ("master", "人工智能学院")):
        repo.save_fact(owner, subject_id=subject, semantic_key="college", scope_key="global", value=value, question="学院名称")
    query = {"question": "学院名称", "scope_key": "global", "semantic_key": "college"}
    assert repo.remembered_answer(owner, subject_id="bachelor", **query)["value"] == "计算机学院"
    assert repo.remembered_answer(owner, subject_id="master", **query)["value"] == "人工智能学院"
    repo.save_fact(owner, subject_id="master", semantic_key="college", scope_key="global", value="信息学院", expected_revision=1)
    assert repo.remembered_answer(owner, subject_id="master", **query)["value"] == "信息学院"
    repo.save_fact(owner, subject_id="master", semantic_key="college", scope_key="global", value="信息学院",
                   confirmation="revoked", expected_revision=2)
    assert repo.remembered_answer(owner, subject_id="master", **query) is None


def test_fact_site_scope_is_normalized_before_identity(v2_db, owner):
    repo = Repository(v2_db)
    first = repo.save_fact(owner, subject_id="person", semantic_key="city", scope_key="site: EXAMPLE.COM.",
                           value="北京", question="意向城市")
    changed = repo.save_fact(owner, subject_id="person", semantic_key="city", scope_key="site:example.com",
                             value="上海", expected_revision=1)
    assert first["id"] == changed["id"]
    answer = repo.remembered_answer(owner, subject_id="person", semantic_key="city",
                                    scope_key="site:EXAMPLE.COM", question="意向城市")
    assert answer["value"] == "上海"
    assert repo.remembered_answer(owner, subject_id="person", semantic_key="city",
                                   scope_key="site:elsewhere.invalid", question="意向城市") is None
