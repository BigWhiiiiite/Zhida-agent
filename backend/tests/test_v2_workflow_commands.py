"""D1 command contracts use isolated PostgreSQL and never a browser or model."""
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.backend_v2.api import create_app
from app.backend_v2.redis_control import RateDecision
from app.backend_v2.repository import Conflict, NotFound, Repository, new_id
from app.backend_v2.settings import Settings


@pytest.fixture
def fixture_run(v2_db):
    owner = "workflow-commands-" + uuid4().hex
    repo = Repository(v2_db)
    repo.ensure_user(owner)
    selected = repo.save_resume(owner, name="合成 D1 简历", profile={})
    run = repo.create_run(owner, resume_revision_id=selected["revision_id"],
        application_url="https://recruit.synthetic.invalid/form", idempotency_key="command-test-request",
        workflow_version="langgraph-d1-v1")
    return repo, owner, run


def pause(db, owner, run_id, state="awaiting_browser"):
    with db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.application_runs SET status=:s WHERE user_id=:u AND id=:r"),
                   {"s": state, "u": owner, "r": run_id})


def confirmation(db, owner, run_id, semantic_key, revision=5):
    identity = new_id()
    with db.transaction(owner) as cx:
        cx.execute(text("""INSERT INTO zhida.confirmation_requests
            (id,user_id,run_id,question_revision,label,semantic_key,subject_id,scope_key,options,reason)
            VALUES(:id,:u,:r,:revision,:label,:semantic,'person','global','["是","否"]','missing_fact')"""),
            {"id": identity, "u": owner, "r": run_id, "revision": revision,
             "label": "合成问题 " + semantic_key, "semantic": semantic_key})
    return identity


def plan_with_action(db, owner, run_id, state="planned"):
    observation, question, plan, action = [new_id() for _ in range(4)]
    with db.transaction(owner) as cx:
        cx.execute(text("""INSERT INTO zhida.form_observations
            (id,user_id,run_id,document_id,observation_revision,coverage,evidence)
            VALUES(:id,:u,:r,'synthetic-document',1,'{}','{}')"""),
            {"id": observation, "u": owner, "r": run_id})
        cx.execute(text("""INSERT INTO zhida.questions
            (id,user_id,observation_id,label,section,control_type,evidence_refs)
            VALUES(:id,:u,:o,'合成姓名','基本资料','text','{}')"""),
            {"id": question, "u": owner, "o": observation})
        cx.execute(text("""INSERT INTO zhida.fill_plans
            (id,user_id,run_id,run_revision,observation_id,fact_revision_refs)
            VALUES(:id,:u,:r,1,:o,'[]')"""), {"id": plan, "u": owner, "r": run_id, "o": observation})
        cx.execute(text("""INSERT INTO zhida.action_ledger
            (action_id,user_id,run_id,plan_id,question_id,ordinal,action_type,target_ref,desired_value_ref,status)
            VALUES(:id,:u,:r,:p,:q,0,'fill_text','{}','{}',:state)"""),
            {"id": action, "u": owner, "r": run_id, "p": plan, "q": question, "state": state})
    return plan, action


def workflow_jobs(db, owner, run_id):
    with db.transaction(owner) as cx:
        return list(cx.execute(text("""SELECT expected_revision,payload FROM zhida.background_jobs
            WHERE user_id=:u AND aggregate_id=:r AND kind='workflow.advance' ORDER BY expected_revision"""),
            {"u": owner, "r": run_id}).mappings())


def test_create_run_pins_engine_and_same_idempotency_key_cannot_change_engine(v2_db, fixture_run):
    repo, owner, run = fixture_run
    assert run["workflow_version"] == "langgraph-d1-v1"
    values = dict(resume_revision_id=run["resume_revision_id"], application_url=run["application_url"],
                  idempotency_key="command-test-request")
    assert repo.create_run(owner, **values, workflow_version="langgraph-d1-v1")["id"] == run["id"]
    with pytest.raises(Conflict):
        repo.create_run(owner, **values)
    with pytest.raises(ValueError):
        repo.create_run(owner, **values, workflow_version="unknown-engine")


def test_resume_concurrent_click_consumes_one_revision_and_one_wakeup(v2_db, fixture_run):
    repo, owner, run = fixture_run
    pause(v2_db, owner, run["id"])
    plan, action = plan_with_action(v2_db, owner, run["id"])

    def click(_):
        try:
            return repo.resume_run(owner, run["id"], 1)
        except Conflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(click, range(2)))
    assert sum(result is not None for result in results) == 1
    result = next(result for result in results if result)
    assert result["run_revision"] == 2 and result["status"] == "queued"
    assert result["browser_actions_executed"] == 0
    jobs = workflow_jobs(v2_db, owner, run["id"])
    assert [job["expected_revision"] for job in jobs] == [1, 2]
    assert jobs[-1]["payload"] == {"reason": "reobserve"}
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT status FROM zhida.fill_plans WHERE id=:id"), {"id": plan}).scalar_one() == "invalidated"
        assert cx.execute(text("SELECT status FROM zhida.action_ledger WHERE action_id=:id"), {"id": action}).scalar_one() == "invalidated"


@pytest.mark.parametrize("state", ["unknown", "dispatched"])
def test_resume_preserves_uncertain_action_and_only_requests_reconciliation(v2_db, fixture_run, state):
    repo, owner, run = fixture_run
    pause(v2_db, owner, run["id"])
    _, action = plan_with_action(v2_db, owner, run["id"], state)
    result = repo.resume_run(owner, run["id"], 1)
    assert result["status"] == "reconciling" and result["resume_reason"] == "reconcile"
    assert workflow_jobs(v2_db, owner, run["id"])[-1]["payload"] == {"reason": "reconcile"}
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT status FROM zhida.action_ledger WHERE action_id=:id"), {"id": action}).scalar_one() == state


@pytest.mark.parametrize("state", ["queued", "running", "ready_for_review", "failed", "cancelled"])
def test_resume_does_not_restart_active_or_terminal_task(v2_db, fixture_run, state):
    repo, owner, run = fixture_run
    pause(v2_db, owner, run["id"], state)
    with pytest.raises(Conflict):
        repo.resume_run(owner, run["id"], 1)
    assert len(workflow_jobs(v2_db, owner, run["id"])) == 1


def test_resume_is_tenant_scoped_and_rejects_deleted_resume(v2_db, fixture_run):
    repo, owner, run = fixture_run
    other = "workflow-other-" + uuid4().hex
    repo.ensure_user(other)
    pause(v2_db, owner, run["id"])
    with pytest.raises(NotFound):
        repo.resume_run(other, run["id"], 1)
    with v2_db.transaction(owner) as cx:
        cx.execute(text("UPDATE zhida.resumes SET deleted_at=now() WHERE current_revision_id=:r"),
                   {"r": run["resume_revision_id"]})
    with pytest.raises(NotFound):
        repo.resume_run(owner, run["id"], 1)
    assert repo.get_run(owner, run["id"])["run_revision"] == 1


def test_partial_answers_are_durable_but_do_not_wake_worker(v2_db, fixture_run):
    repo, owner, run = fixture_run
    pause(v2_db, owner, run["id"], "awaiting_user")
    first = confirmation(v2_db, owner, run["id"], "synthetic.first")
    second = confirmation(v2_db, owner, run["id"], "synthetic.second")
    plan, action = plan_with_action(v2_db, owner, run["id"])
    answered = repo.answer(owner, run["id"], first, value="是", expected_revision=1, expected_question_revision=5)
    assert answered["run_revision"] == 2 and answered["pending_questions"] == 1
    assert answered["status"] == "awaiting_user"
    assert len(repo.facts(owner)) == 1
    assert len(workflow_jobs(v2_db, owner, run["id"])) == 1
    with pytest.raises(Conflict):
        repo.resume_run(owner, run["id"], 2)
    # A separately edited fact must not unpause unanswered questions either.
    repo.save_fact(owner, subject_id="person", semantic_key="synthetic.city", scope_key="global", value="北京")
    assert repo.get_run(owner, run["id"])["run_revision"] == 3
    assert repo.get_run(owner, run["id"])["status"] == "awaiting_user"
    assert len(workflow_jobs(v2_db, owner, run["id"])) == 1
    last = repo.answer(owner, run["id"], second, value="否", expected_revision=3, expected_question_revision=5)
    assert last["run_revision"] == 4 and last["status"] == "queued" and last["pending_questions"] == 0
    jobs = workflow_jobs(v2_db, owner, run["id"])
    assert [job["expected_revision"] for job in jobs] == [1, 4]
    assert jobs[-1]["payload"] == {"reason": "reobserve"}
    with v2_db.transaction(owner) as cx:
        assert cx.execute(text("SELECT status FROM zhida.action_ledger WHERE action_id=:id"), {"id": action}).scalar_one() == "invalidated"


@pytest.mark.parametrize("question_revision", [None, 4])
def test_d1_requires_current_question_revision_without_saving_stale_fact(v2_db, fixture_run, question_revision):
    repo, owner, run = fixture_run
    pause(v2_db, owner, run["id"], "awaiting_user")
    question = confirmation(v2_db, owner, run["id"], "synthetic.question")
    with pytest.raises(Conflict):
        repo.answer(owner, run["id"], question, value="是", expected_revision=1,
                    expected_question_revision=question_revision)
    assert repo.facts(owner) == []
    assert repo.get_run(owner, run["id"])["run_revision"] == 1


class AllowedControl:
    def rate_limit(self, **kwargs):
        return RateDecision(True, 10, 0)


@pytest.mark.parametrize("enabled", [False, True])
def test_api_pins_configured_workflow_and_resume_is_flag_gated(v2_db, enabled):
    cfg = Settings(database_url=str(v2_db.engine.url), cookie_secure=False, workflow_enabled=enabled)
    with TestClient(create_app(cfg, db=v2_db, control=AllowedControl())) as client:
        credentials = {"email": uuid4().hex + "@synthetic.invalid", "password": "SynthetiC-password!234"}
        assert client.post("/api/v2/auth/register", json=credentials).status_code == 201
        logged = client.post("/api/v2/auth/login", json=credentials)
        auth = {"Authorization": "Bearer " + logged.json()["token"]}
        selected = client.post("/api/v2/resumes", headers=auth, json={"name": "合成测试", "profile": {}}).json()
        created = client.post("/api/v2/runs", headers=auth, json={"resume_revision_id": selected["revision_id"],
            "application_url": "https://recruit.synthetic.invalid/form", "idempotency_key": "flag-test-request"})
        assert created.status_code == 202, created.text
        run = created.json()
        assert run["workflow_version"] == ("langgraph-d1-v1" if enabled else "foundation-v1")
        assert client.get("/api/v2/health").json()["langgraph_enabled"] is enabled
        pause(v2_db, run["user_id"], run["id"])
        resumed = client.post(f"/api/v2/runs/{run['id']}/resume", headers=auth, json={"expected_revision": 1})
        assert resumed.status_code == (202 if enabled else 409), resumed.text
        if enabled:
            assert resumed.json()["run_revision"] == 2
            assert client.post(f"/api/v2/runs/{run['id']}/resume", headers=auth,
                               json={"expected_revision": 1}).status_code == 409
