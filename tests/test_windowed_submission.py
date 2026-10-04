"""按日期窗口送审后提交（POST /applications/audited-submissions）端到端测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app

PATH = "/applications/audited-submissions"


@pytest.fixture()
def client(tmp_path):
    app = create_app(str(tmp_path / "test.db"))
    with TestClient(app) as c:
        yield c


def rev(claim_number, version, claim_type="dependent", references=(1,),
        mode="all", text="文本", eff_from="2026-01-01", eff_to=None):
    return {
        "claim_number": claim_number,
        "version": version,
        "claim_type": claim_type,
        "references": list(references),
        "reference_mode": mode,
        "text": text,
        "effective_from": eff_from,
        "effective_to": eff_to,
    }


def submit_window(client, request_id, app_no, start, end, revisions):
    return client.post(PATH, json={
        "request_id": request_id,
        "application_number": app_no,
        "start_date": start,
        "end_date": end,
        "revisions": revisions,
    })


def submit_plain(client, request_id, app_no, revisions):
    return client.post("/applications", json={
        "request_id": request_id,
        "application_number": app_no,
        "revisions": revisions,
    })


def audit(client, app_no, target):
    return client.post("/audit", json={"application_number": app_no, "target_date": target})


# ---------- 窗口参数 ----------

def test_window_must_be_half_open(client):
    for start, end in [("2026-02-01", "2026-02-01"), ("2026-03-01", "2026-02-01")]:
        r = submit_window(client, "w", "CN-W", start, end,
                          [rev(1, 1, "independent", [])])
        assert r.status_code == 422
        assert "start_date" in r.text


# ---------- 成功路径：划段、自动封闭、一次性落库 ----------

def test_single_segment_pass_for_new_application(client):
    r = submit_window(client, "w1", "CN2026-100", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", [], text="一种装置"),
        rev(2, 1, "dependent", [1], text="如权利要求1所述"),
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["segments"] == [{
        "audit_date": "2026-01-01",
        "status": "pass",
        "effective_claim_count": 2,
        "issues": [],
    }]
    assert body["submission"]["stored_revisions"] == 2
    assert body["submission"]["auto_closed"] == []
    assert r.headers["X-Idempotent-Replay"] == "false"
    # 落库后 /audit 与 /applications 可见
    assert audit(client, "CN2026-100", "2026-06-01").json()["status"] == "pass"


def test_segments_split_at_effective_and_expiry_boundaries(client):
    # 1 v1 [01-01, 06-01) -> 1 v2 [06-01, 开放)；2 v1 [01-01, 09-01) -> 2 v2 [09-01, 开放)
    # 窗口内边界：01-01（窗口起日）、06-01（1 换版）、09-01（2 换版）
    r = submit_window(client, "w1", "CN2026-200", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
        rev(1, 2, "independent", [], text="改进装置", eff_from="2026-06-01"),
        rev(2, 2, "dependent", [1], text="新版从属", eff_from="2026-09-01"),
    ])
    assert r.status_code == 201, r.text
    body = r.json()
    assert [s["audit_date"] for s in body["segments"]] == [
        "2026-01-01", "2026-06-01", "2026-09-01"]
    assert all(s["status"] == "pass" for s in body["segments"])
    assert {s["effective_claim_count"] for s in body["segments"]} == {2}
    # 自动封闭在事务内被审查看到，落库后体现在提交结果中
    assert body["submission"]["auto_closed"] == [
        {"claim_number": 1, "closed_version": 1, "effective_to": "2026-06-01"},
        {"claim_number": 2, "closed_version": 1, "effective_to": "2026-09-01"},
    ]
    live = audit(client, "CN2026-200", "2026-03-01").json()
    assert {c["claim_number"]: c["version"] for c in live["effective_claims"]} == {1: 1, 2: 1}
    live = audit(client, "CN2026-200", "2026-10-01").json()
    assert {c["claim_number"]: c["version"] for c in live["effective_claims"]} == {1: 2, 2: 2}


def test_boundaries_outside_window_are_ignored_and_end_is_excluded(client):
    # 新版本 2027-06-01 生效，晚于止日 2027-01-01：止日不构成段起日，窗口只审 01-01
    r = submit_window(client, "w1", "CN2026-201", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
        rev(1, 2, "independent", [], eff_from="2027-06-01"),
    ])
    assert r.status_code == 201, r.text
    assert [s["audit_date"] for s in r.json()["segments"]] == ["2026-01-01"]


def test_window_clipped_into_middle_of_chain_still_segments(client):
    # 同一数据，窗口只覆盖中段：[2026-05-01, 2026-10-01) 内边界为 06-01、09-01 及窗口起日
    revisions = [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
        rev(1, 2, "independent", [], text="改进", eff_from="2026-06-01"),
        rev(2, 2, "dependent", [1], text="新版从属", eff_from="2026-09-01"),
    ]
    r = submit_window(client, "w1", "CN2026-202", "2026-05-01", "2026-10-01", revisions)
    assert r.status_code == 201, r.text
    assert [s["audit_date"] for s in r.json()["segments"]] == [
        "2026-05-01", "2026-06-01", "2026-09-01"]


# ---------- 失败路径：不通过 / 空段 / 最早失败日期 ----------

def test_segment_audit_fail_rolls_back_entire_batch(client):
    # claim 2 自 06-01 起引用不存在的 9：01-01 段通过，06-01 段失败
    r = submit_window(client, "f1", "CN2026-300", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [9], eff_from="2026-06-01"),
    ])
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "window_audit_failed"
    assert err["details"]["failed_date"] == "2026-06-01"
    assert "无效项 9" in err["details"]["reason"]
    assert err["details"]["issues"][0]["code"] == "INVALID_REFERENCE"
    assert err["details"]["segment_audit_dates"] == ["2026-01-01", "2026-06-01"]
    # 整批修订与 request_id 一并撤销
    assert client.get("/applications/CN2026-300").status_code == 404
    assert audit(client, "CN2026-300", "2026-06-01").status_code == 404


def test_earliest_failing_date_is_returned(client):
    # 06-01 段引用无效，09-01 段成环；只报最早的 06-01
    r = submit_window(client, "f1", "CN2026-301", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [9], eff_from="2026-06-01"),
        rev(2, 2, "dependent", [3], eff_from="2026-09-01"),
        rev(3, 1, "dependent", [2], eff_from="2026-09-01"),
    ])
    assert r.status_code == 422
    assert r.json()["error"]["details"]["failed_date"] == "2026-06-01"
    assert client.get("/applications/CN2026-301").status_code == 404


def test_empty_first_segment_reports_empty(client):
    # 唯一权利要求 06-01 才生效，窗口自 01-01 起：首段为空
    r = submit_window(client, "e1", "CN2026-302", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", [], eff_from="2026-06-01"),
    ])
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["failed_date"] == "2026-01-01"
    assert "没有任何有效权利要求" in details["reason"]
    assert client.get("/applications/CN2026-302").status_code == 404


def test_empty_segment_after_last_version_expiry_reports_empty(client):
    # 唯一权利要求区间 [01-01, 06-01) 已封闭且无后续版本；
    # 窗口 [2026-03-01, 2026-09-01) 在 06-01 段内没有任何有效权利要求。
    r = submit_window(client, "e2", "CN2026-303", "2026-03-01", "2026-09-01", [
        rev(1, 1, "independent", [], eff_from="2026-01-01", eff_to="2026-06-01"),
    ])
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["failed_date"] == "2026-06-01"
    assert details["segment_audit_dates"] == ["2026-03-01", "2026-06-01"]
    assert "没有任何有效权利要求" in details["reason"]
    assert client.get("/applications/CN2026-303").status_code == 404


def test_chain_validation_failure_does_not_window_audit_and_rolls_back(client):
    r = submit_window(client, "x1", "CN2026-309", "2026-01-01", "2027-01-01", [
        rev(1, 3, "independent", []),  # 版本跳跃
    ])
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
    assert client.get("/applications/CN2026-309").status_code == 404


# ---------- 回滚不影响旧证据包 ----------

def test_failed_windowed_submission_keeps_old_evidence_verifiable(client):
    submit_plain(client, "seed", "CN2026-400", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
    ])
    old = client.post("/audit/evidence",
                      json={"application_number": "CN2026-400", "target_date": "2026-03-01"})
    assert old.status_code == 201
    evidence_id = old.json()["evidence_id"]

    # 窗口提交失败（新版本在窗口内引入无效引用）-> 自动封闭随事务回滚
    r = submit_window(client, "bad-window", "CN2026-400", "2026-01-01", "2027-01-01", [
        rev(2, 2, "dependent", [9], eff_from="2026-06-01"),
    ])
    assert r.status_code == 422

    got = client.get(f"/audit/evidence/{evidence_id}").json()
    assert got["verified"] is True
    assert got["digest"] == old.json()["sha256"]
    # 现有数据未被封闭改写
    live = audit(client, "CN2026-400", "2026-03-01").json()
    assert live["effective_claims"][1]["effective_to"] is None

    # request_id 未被失败提交占用：修正内容后可同号重试成功
    retry = submit_window(client, "bad-window", "CN2026-400",
                          "2026-01-01", "2027-01-01", [
        rev(2, 2, "dependent", [1], eff_from="2026-06-01"),
    ])
    assert retry.status_code == 201, retry.text


# ---------- 幂等 / 冲突 ----------

def test_idempotent_replay_returns_first_success(client):
    body = {
        "request_id": "w9", "application_number": "CN2026-500",
        "start_date": "2026-01-01", "end_date": "2027-01-01",
        "revisions": [
            rev(1, 1, "independent", []),
            rev(2, 1, "dependent", [1]),
        ],
    }
    first = client.post(PATH, json=body)
    second = client.post(PATH, json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert first.headers["X-Idempotent-Replay"] == "false"
    assert second.headers["X-Idempotent-Replay"] == "true"
    state = client.get("/applications/CN2026-500").json()
    assert len(state["revisions"]) == 2  # 未重复写入


def test_same_request_id_different_payload_conflicts(client):
    body = {
        "request_id": "dup", "application_number": "CN2026-501",
        "start_date": "2026-01-01", "end_date": "2027-01-01",
        "revisions": [rev(1, 1, "independent", [])],
    }
    assert client.post(PATH, json=body).status_code == 201
    body["end_date"] = "2028-01-01"
    r = client.post(PATH, json=body)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "request_id_conflict"


def test_request_id_shared_with_plain_endpoint_conflicts(client):
    submit_plain(client, "shared", "CN2026-502", [rev(1, 1, "independent", [])])
    r = submit_window(client, "shared", "CN2026-502", "2026-01-01", "2027-01-01",
                      [rev(1, 2, "independent", [], eff_from="2026-06-01")])
    assert r.status_code == 409


# ---------- 原 POST /applications 行为不变 ----------

def test_plain_applications_still_unconditional(client):
    # 不经窗口审查：即使修订在某日引用无效，原接口照常 201 提交
    r = submit_plain(client, "p1", "CN2026-600", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [9]),
    ])
    assert r.status_code == 201
    assert r.headers["X-Idempotent-Replay"] == "false"
    assert "segments" not in r.json()
    assert audit(client, "CN2026-600", "2026-03-01").json()["status"] == "fail"


def test_windowed_commit_visible_to_plain_audit_and_state(client):
    submit_window(client, "w1", "CN2026-601", "2026-01-01", "2027-01-01", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
    ])
    state = client.get("/applications/CN2026-601").json()
    assert [r["claim_number"] for r in state["revisions"]] == [1, 2]
    assert audit(client, "CN2026-601", "2026-03-01").json()["status"] == "pass"


def test_window_over_existing_application_uses_all_boundaries(client):
    # 既有申请：1 v1、2 v1 自 2026-01-01 开放（经原 /applications 提交，不审查）
    submit_plain(client, "seed", "CN2026-700", [
        rev(1, 1, "independent", []),
        rev(2, 1, "dependent", [1]),
    ])
    # 窗口提交新版本：1 v2 自 06-01；窗口内边界同时包含旧版本起点 01-01
    r = submit_window(client, "w2", "CN2026-700", "2026-01-01", "2027-01-01", [
        rev(1, 2, "independent", [], text="改进", eff_from="2026-06-01"),
    ])
    assert r.status_code == 201, r.text
    assert [s["audit_date"] for s in r.json()["segments"]] == ["2026-01-01", "2026-06-01"]
    assert r.json()["submission"]["auto_closed"] == [
        {"claim_number": 1, "closed_version": 1, "effective_to": "2026-06-01"}]
    # 自动封闭随成功提交持久化
    live = audit(client, "CN2026-700", "2026-03-01").json()
    assert {c["claim_number"]: c["version"] for c in live["effective_claims"]} == {1: 1, 2: 1}
