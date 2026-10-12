"""Real E2E cases, explicitly selected by run_e2e.py; no mocks or skips."""

import json
import uuid
from urllib.parse import quote

import pytest

from .environment import BoundaryUnverified, Deadline
from .harness import Case


def finish(case, task_id):
    case.wait(lambda: case.task_finished(task_id), "task terminal log")
    case.wait(case.drained, "broker drain")
    assert not case.proxy.errors


def sync(case):
    task_id = case.publish()
    finish(case, task_id)
    return task_id


def test_beat_triggers_first_sync_and_commits_snapshot__AC1(catalog_case):
    case = catalog_case
    case.start_worker()
    tick_id = case.complete_tick()
    syncs = case.observer.events("publish", "catalog.sync")
    assert len(syncs) == 1
    finish(case, syncs[0]["id"])
    case.assert_snapshot()
    assert case.observer.events("deliver", "runtime.tick", tick_id)
    assert case.observer.events("deliver", "catalog.sync", syncs[0]["id"])
    assert len(case.dav.requests) == 1
    assert case.dav.requests[0]["skipCount"] == 0
    assert case.dav.requests[0]["maxResultCount"] == 10


@pytest.mark.parametrize("age_seconds", [6 * 86400, 7 * 86400 - 60])
def test_recent_success_suppresses_sync_after_completed_tick__AC2(catalog_case, age_seconds):
    case = catalog_case
    seed_success(case, age_seconds)
    case.start_worker()
    case.complete_tick()
    assert not case.observer.events("publish", "catalog.sync")
    assert not case.dav.requests
    assert len(case.runs()) == 1
    age = case.infra.sql("SELECT extract(epoch FROM now()-finished_at) AS age "
                         "FROM catalog_sync_runs")[0]["age"]
    assert age < 7 * 86400, "Observation crossed not-due boundary"


def seed_success(case, age_seconds):
    case.infra.sql("INSERT INTO catalog_sync_runs "
                   "(source,triggered_by,status,started_at,finished_at) "
                   "VALUES ('dav_drugbank','scheduler','thanh_cong', "
                   "now()-(%s+1)*interval '1 second', now()-%s*interval '1 second')",
                   (age_seconds, age_seconds))


@pytest.mark.parametrize("age_seconds", [7 * 86400, 7 * 86400 + 60])
def test_seven_day_success_is_due__AC2(catalog_case, age_seconds):
    case = catalog_case
    seed_success(case, age_seconds)
    case.start_worker()
    case.complete_tick()
    syncs = case.observer.events("publish", "catalog.sync")
    assert len(syncs) == 1
    finish(case, syncs[0]["id"])
    assert case.success(2), "Due sync did not commit success"
    run = case.runs()[1]
    assert (run["records_inserted"], run["records_updated"], run["records_failed"]) == (10, 0, 0)
    assert len(case.medications()) == 10 and len(case.dav.requests) == 1


def test_running_source_suppresses_tick_and_duplicate_message__AC3(catalog_case):
    case = catalog_case
    case.dav.configure(hold=True)
    case.start_worker(concurrency=2)
    first = case.publish()
    case.wait(case.dav.entered.is_set, "held HTTP request", 20)
    case.wait(lambda: len(case.runs()) == 1, "running run committed", 20)
    assert case.runs()[0]["status"] == "dang_chay"
    case.complete_tick(beat=False)
    assert len(case.observer.events("publish", "catalog.sync")) == 1
    duplicate = case.publish()
    case.wait(lambda: case.task_finished(duplicate), "duplicate completed", 20)
    assert len(case.runs()) == len(case.dav.requests) == 1
    assert not case.medications()
    assert not case.dav.requests[0].get("hold_timeout")
    case.dav.release.set()
    finish(case, first)
    case.assert_snapshot()


def test_repeated_broker_sync_preserves_medication_ids__AC4(catalog_case):
    case = catalog_case
    case.start_worker()
    sync(case)
    first = case.assert_snapshot()
    sync(case)
    assert case.assert_snapshot(count=2) == first
    assert len(case.dav.requests) == 2


@pytest.mark.parametrize("status", [429, 500])
def test_transient_http_retries_then_succeeds__AC5(catalog_case, status):
    case = catalog_case
    case.dav.configure(statuses=[status, status, status, 200])
    case.start_worker()
    task_id = sync(case)
    assert_attempts(case, task_id, ["that_bai"] * 3 + ["thanh_cong"])
    run = case.runs()[-1]
    assert (run["records_inserted"], run["records_updated"], run["records_failed"]) == (10, 0, 0)
    assert len(case.medications()) == 10


def assert_attempts(case, task_id, statuses):
    assert [r["status"] for r in case.runs()] == statuses
    assert all(r["finished_at"] is not None for r in case.runs())
    assert all(r["error_details"] for r in case.runs() if r["status"] == "that_bai")
    assert len(case.dav.requests) == len(statuses)
    deliveries = case.observer.events("deliver", "catalog.sync", task_id)
    assert [r["retries"] for r in deliveries] == list(range(len(statuses)))
    assert {r["id"] for r in case.observer.events("publish", "catalog.sync")} == {task_id}


@pytest.mark.parametrize("status", [429, 500])
def test_transient_http_exhausts_four_attempts__AC5(catalog_case, status):
    case = catalog_case
    case.dav.configure(statuses=[status])
    case.start_worker()
    task_id = sync(case)
    # Backoff for a forbidden fifth attempt would be <=8 seconds. Observe a full
    # bounded window, retaining terminal task log, retry ledger and broker state.
    observe_no_more_attempts(case, 10)
    assert_attempts(case, task_id, ["that_bai"] * 4)
    assert not case.medications()


def observe_no_more_attempts(case, seconds):
    from threading import Event
    window = Deadline(seconds, parent=case.deadline)
    count = len(case.observer.events("publish", "catalog.sync"))
    while True:
        assert len(case.observer.events("publish", "catalog.sync")) == count
        try:
            Event().wait(window.remaining(0.1))
        except TimeoutError:
            break
    assert case.drained()


@pytest.mark.parametrize("status", [400, 404, 422])
def test_permanent_http_failure_has_no_retry__AC6(catalog_case, status):
    case = catalog_case
    case.dav.configure(statuses=[status])
    case.start_worker()
    task_id = sync(case)
    observe_no_more_attempts(case, 2)
    assert_attempts(case, task_id, ["that_bai"])
    assert not case.medications()


def test_unsupported_source_has_no_retry_or_run__AC6(catalog_case):
    case = catalog_case
    case.start_worker()
    task_id = case.publish(source="qd_5948")
    finish(case, task_id)
    observe_no_more_attempts(case, 2)
    assert not case.runs() and not case.medications() and not case.dav.requests
    assert len(case.observer.events("deliver", "catalog.sync", task_id)) == 1
    assert "unsupported catalog source" in case.worker.text()


@pytest.mark.parametrize("items", [[], [{"tenThuoc": "thiếu mã"}], [{"soDangKy": "thiếu tên"}]])
def test_no_valid_records_finishes_failed_without_retry__AC6(catalog_case, items):
    case = catalog_case
    case.dav.configure(document={"result": {"items": items, "totalCount": len(items)}})
    case.start_worker()
    task_id = sync(case)
    observe_no_more_attempts(case, 2)
    assert [r["status"] for r in case.runs()] == ["that_bai"]
    assert case.runs()[0]["finished_at"] and case.runs()[0]["error_details"]
    assert not case.medications()
    assert len(case.observer.events("deliver", "catalog.sync", task_id)) == 1


def test_worker_loss_before_commit_redelivers_and_recovers__AC7(catalog_case):
    case = catalog_case
    case.dav.configure(hold=True)
    case.start_worker()
    task_id = case.publish()
    case.wait(case.dav.entered.is_set, "fetch held", 20)
    assert len(case.runs()) == 1 and case.runs()[0]["status"] == "dang_chay"
    assert not case.medications()
    case.wait(lambda: case.unacked() == 1, "broker unacked before crash", 20)
    case.kill_worker_for(task_id)
    case.dav.release.set()
    case.start_worker()
    case.wait(lambda: any(e["redelivered"] for e in case.observer.events(
        "deliver", "catalog.sync", task_id)), "same-ID broker redelivery")
    finish(case, task_id)
    # Expected recovery. A dangling run is a product failure, never repaired here.
    assert all(r["status"] != "dang_chay" for r in case.runs()), "Run stuck after broker redelivery"
    assert len(case.medications()) == 10
    assert any(r["status"] == "thanh_cong" for r in case.runs())


def test_worker_loss_after_commit_before_ack_is_idempotent__AC8(catalog_case):
    case = catalog_case
    case.start_worker()
    task_id = str(uuid.uuid4())
    case.observer.arm(task_id)  # Must precede publish.
    case.publish(task_id=task_id)
    case.wait(lambda: case.observer.events("held_ack", task_id=task_id), "target ack held")
    if not case.success() or len(case.medications()) != 10:
        raise BoundaryUnverified("AC8 commit-before-ack boundary blocked: catalog sync did not "
                                 "commit success; inspect Worker/DB evidence "
                                 "before classifying AC8")
    first = case.assert_snapshot()
    case.wait(lambda: case.unacked() == 1, "committed task still unacked", 20)
    case.kill_worker_for(task_id)
    case.observer.targets.discard(task_id)
    case.start_worker()
    case.wait(lambda: any(e["redelivered"] for e in case.observer.events(
        "deliver", "catalog.sync", task_id)), "same-ID broker redelivery")
    finish(case, task_id)
    assert case.assert_snapshot(count=2) == first
    assert len(case.dav.requests) == 2


def test_local_execution_role_and_delivery_properties__AC9(catalog_case):
    case = catalog_case
    case.dav.configure(hold=True)
    case.start_worker()
    task_id = case.publish()
    case.wait(case.dav.entered.is_set, "fetch held", 20)
    roles = case.infra.sql("SELECT usename FROM pg_stat_activity WHERE application_name=%s",
                           (case.infra.run_id,))
    assert roles and {r["usename"] for r in roles} == {"worker_role"}
    case.wait(lambda: case.unacked() == 1, "unacked while processing", 20)
    queues = case.infra.api("GET", "/queues/" + quote(case.vhost, safe=""))
    assert next(q for q in queues if q["name"] == "celery")["durable"]
    delivery = case.observer.events("deliver", "catalog.sync", task_id)[0]
    assert delivery["delivery_mode"] == 2
    assert delivery["arg_count"] == 0
    assert delivery["kwarg_keys"] == ["source", "triggered_by"]
    assert delivery["kwargs"] == {"source": "dav_drugbank", "triggered_by": "scheduler"}
    runtime_line = next(line for line in case.worker.lines
                        if line.startswith("CATALOG_E2E_RUNTIME "))
    runtime = json.loads(runtime_line.split(" ", 1)[1])
    assert runtime == {"timezone": "Asia/Ho_Chi_Minh", "result_backend": None, "prefetch": 1}
    case.dav.release.set()
    finish(case, task_id)


def test_failure_cleanup_removes_only_owned_resources__AC10(catalog_infra):
    infra = catalog_infra
    sentinel = f"{infra.project}-sentinel-{uuid.uuid4().hex[:8]}"
    infra.api("PUT", "/vhosts/" + sentinel, {})
    sentinel_record = {"vhost": sentinel, "purpose": "outside-case sentinel", "cleaned": False}
    infra.manifest.setdefault("sentinels", []).append(sentinel_record)
    infra.write_manifest()
    case = Case(infra, "cleanup-fault")
    try:
        try:
            case.start()
            case.start_worker()
            case.dav.configure(hold=True)
            case.publish()
            case.wait(case.dav.entered.is_set, "cleanup fault held HTTP", 20)
            raise AssertionError("deliberate cleanup fault")
        except AssertionError as exc:
            assert str(exc) == "deliberate cleanup fault"
        finally:
            case.close()
        vhosts = {v["name"] for v in infra.api("GET", "/vhosts")}
        assert case.vhost not in vhosts and sentinel in vhosts
        assert all(p.process.poll() is not None for p in case.processes)
        assert not case.proxy.connections
        infra.guard_db()  # Shared suite infrastructure survived case cleanup.
    finally:
        infra.api("DELETE", "/vhosts/" + sentinel)
        sentinel_record["cleaned"] = True
        infra.write_manifest()
