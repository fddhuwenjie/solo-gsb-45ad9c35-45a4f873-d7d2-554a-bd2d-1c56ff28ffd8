"""POST /applications/window-audit（按日期窗口送审后提交）端到端测试。"""
import pytest
from fastapi.testclient import TestClient

from app.main import create_app


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


def submit(client, request_id, app_no, revisions):
    return client.post("/applications", json={
        "request_id": request_id,
        "application_number": app_no,
        "revisions": revisions,
    })


def window_submit(client, request_id, app_no, revisions, start, end):
    return client.post("/applications/window-audit", json={
        "request_id": request_id,
        "application_number": app_no,
        "revisions": revisions,
        "start_date": start,
        "end_date": end,
    })


def audit(client, app_no, target):
    return client.post("/audit", json={"application_number": app_no, "target_date": target})


# ---------- 成功路径 ----------

def test_window_submit_ok_segments_and_commit(client):
    """一次新申请：两个独立权利要求，6/1 起 v2 替换 v1，窗口被划为两段且全部通过。"""
    r = window_submit(client, "w1", "CN-W1", [
        rev(1, 1, "independent", [], eff_from="2026-01-01", eff_to="2026-06-01"),
        rev(1, 2, "independent", [], text="改进", eff_from="2026-06-01"),
        rev(2, 1, "independent", [], text="另一个", eff_from="2026-03-01"),
    ], "2026-01-01", "2026-09-01")
    assert r.status_code == 201, r.text
    body = r.json()
    assert [s["audit_date"] for s in body["segments"]] == [
        "2026-01-01", "2026-03-01", "2026-06-01"]
    assert [s["effective_claims"] for s in body["segments"]] == [1, 2, 2]
    assert all(s["status"] == "pass" for s in body["segments"])
    assert body["stored_revisions"] == 3
    assert r.headers["X-Idempotent-Replay"] == "false"

    # 修订确实已提交：自动重建出两个版本
    march = audit(client, "CN-W1", "2026-03-02").json()
    assert {c["claim_number"]: c["version"] for c in march["effective_claims"]} == {1: 1, 2: 1}
    july = audit(client, "CN-W1", "2026-07-01").json()
    assert {c["claim_number"]: c["version"] for c in july["effective_claims"]} == {1: 2, 2: 1}


def test_window_submit_auto_closes_open_version(client):
    """窗口提交同样自动封闭既有开放版本，封闭信息出现在响应中。"""
    submit(client, "seed", "CN-W2", [
        rev(1, 1, "independent", [], text="装置"),
        rev(2, 1, "dependent", [1]),
    ])
    r = window_submit(client, "w2", "CN-W2", [
        rev(1, 2, "independent", [], text="改进装置", eff_from="2026-06-01"),
    ], "2026-01-01", "2026-09-01")
    assert r.status_code == 201, r.text
    assert r.json()["auto_closed"] == [
        {"claim_number": 1, "closed_version": 1, "effective_to": "2026-06-01"}]


def test_end_date_excluded_from_segments(client):
    """止日是开区间端点：恰好落在 end_date 的边界不单独成段。"""
    r = window_submit(client, "w3", "CN-W3", [
        rev(1, 1, "independent", [], eff_from="2026-01-01", eff_to="2026-06-01"),
        rev(1, 2, "independent", [], eff_from="2026-06-01"),
    ], "2026-01-01", "2026-06-01")
    assert r.status_code == 201, r.text
    assert [s["audit_date"] for s in r.json()["segments"]] == ["2026-01-01"]


# ---------- 失败回滚 ----------

def test_empty_segment_rejected_and_rolled_back(client):
    """首段无任何有效权利要求：拒绝且整批（含 request_id 与申请）不落库。"""
    r = window_submit(client, "w4", "CN-W4", [
        rev(1, 1, "independent", [], eff_from="2026-03-01"),
    ], "2026-01-01", "2026-09-01")
    assert r.status_code == 422
    err = r.json()["error"]
    assert err["code"] == "window_audit_rejected"
    assert err["details"]["failed_date"] == "2026-01-01"
    assert err["details"]["reason"] == "EMPTY_SEGMENT"

    assert client.get("/applications/CN-W4").status_code == 404
    # 同一 request_id 可原样重试（失败未占用 request_id）
    retry = window_submit(client, "w4", "CN-W4", [
        rev(1, 1, "independent", [], eff_from="2026-01-01"),
    ], "2026-01-01", "2026-09-01")
    assert retry.status_code == 201


def test_audit_failure_in_later_segment_returns_earliest_failure(client):
    """首段通过；6/1 起权利要求 2 换版后引用不存在的 9，该段失败。"""
    r = window_submit(client, "w5", "CN-W5", [
        rev(1, 1, "independent", [], eff_from="2026-01-01", eff_to="2026-06-01"),
        rev(1, 2, "independent", [], eff_from="2026-06-01"),
        rev(2, 1, "dependent", [1], eff_from="2026-01-01"),  # 开放版本在 6/1 被自动封闭
        rev(2, 2, "dependent", [9], eff_from="2026-06-01"),  # 引用不存在的编号
    ], "2026-01-01", "2026-09-01")
    assert r.status_code == 422
    details = r.json()["error"]["details"]
    assert details["failed_date"] == "2026-06-01"
    assert details["reason"] == "AUDIT_FAILED"
    codes = {i["code"] for i in details["issues"]}
    assert "INVALID_REFERENCE" in codes

    # 整批回滚：申请不存在，且 6/1 当日也查不到这批数据
    assert client.get("/applications/CN-W5").status_code == 404
    assert audit(client, "CN-W5", "2026-06-01").status_code == 404


def test_rollback_leaves_old_data_and_evidence_replayable(client):
    """失败回滚不影响既有修订（开放版本保持开放），旧证据包仍可照常复验。"""
    submit(client, "seed", "CN-W6", [
        rev(1, 1, "independent", [], text="装置"),
        rev(2, 1, "dependent", [1]),
    ])
    ev = client.post("/audit/evidence",
                     json={"application_number": "CN-W6", "target_date": "2026-03-01"})
    assert ev.status_code == 201
    evidence_id = ev.json()["evidence_id"]

    # 提交一个在窗口内失效引用的批次：6/1 段 claim 1 换版（旧开放版本被自动封闭），
    # 新增 claim 3 引用不存在的编号 9 → 该段审查不通过
    bad = window_submit(client, "w6", "CN-W6", [
        rev(1, 2, "independent", [], text="改进装置", eff_from="2026-06-01"),
        rev(3, 1, "dependent", [9], eff_from="2026-06-01"),
    ], "2026-06-01", "2026-12-31")
    assert bad.status_code == 422
    assert bad.json()["error"]["details"]["failed_date"] == "2026-06-01"

    # 旧状态原封不动：v1 仍开放，审查通过
    before = audit(client, "CN-W6", "2026-03-01").json()
    assert before["status"] == "pass"
    state = client.get("/applications/CN-W6").json()
    assert len(state["revisions"]) == 2
    assert all(r["effective_to"] is None for r in state["revisions"])

    # 旧证据包复验不受影响
    replay = client.get(f"/audit/evidence/{evidence_id}").json()
    assert replay["verified"] is True
    assert replay["snapshot"]["target_date"] == "2026-03-01"


def test_chain_validation_failure_rolls_back(client):
    """版本链本身不合法时按原 /applications 规则拒绝（422），不落任何数据。"""
    r = window_submit(client, "w7", "CN-W7", [
        rev(1, 1, "independent", []),
        rev(2, 3, "dependent", [1]),  # 版本跳跃
    ], "2026-01-01", "2026-09-01")
    assert r.status_code == 422
    assert client.get("/applications/CN-W7").status_code == 404


# ---------- 幂等与冲突 ----------

def test_window_idempotent_replay(client):
    body = {
        "request_id": "w8",
        "application_number": "CN-W8",
        "revisions": [
            rev(1, 1, "independent", [], eff_from="2026-01-01"),
            rev(2, 1, "dependent", [1], eff_from="2026-01-01"),
        ],
        "start_date": "2026-01-01",
        "end_date": "2026-09-01",
    }
    first = client.post("/applications/window-audit", json=body)
    second = client.post("/applications/window-audit", json=body)
    assert first.status_code == second.status_code == 201
    assert first.json() == second.json()
    assert second.headers["X-Idempotent-Replay"] == "true"
    state = client.get("/applications/CN-W8").json()
    assert len(state["revisions"]) == 2  # 未重复写入


def test_window_same_request_id_changed_payload_conflict(client):
    base = {
        "request_id": "w9",
        "application_number": "CN-W9",
        "revisions": [rev(1, 1, "independent", [], eff_from="2026-01-01")],
        "start_date": "2026-01-01",
        "end_date": "2026-09-01",
    }
    assert client.post("/applications/window-audit", json=base).status_code == 201
    changed = {**base, "end_date": "2026-10-01"}
    r = client.post("/applications/window-audit", json=changed)
    assert r.status_code == 409


def test_request_id_shared_with_plain_applications_conflicts(client):
    """request_id 跨端点复用且负载不同仍报冲突。"""
    body = {
        "request_id": "shared",
        "application_number": "CN-W10",
        "revisions": [rev(1, 1, "independent", [], eff_from="2026-01-01")],
    }
    assert client.post("/applications", json=body).status_code == 201
    r = client.post("/applications/window-audit",
                    json={**body, "start_date": "2026-01-01", "end_date": "2026-09-01"})
    assert r.status_code == 409


# ---------- 入参校验 ----------

def test_start_must_precede_end(client):
    r = window_submit(client, "w11", "CN-W11", [
        rev(1, 1, "independent", [], eff_from="2026-01-01"),
    ], "2026-09-01", "2026-09-01")
    assert r.status_code == 422
    r = window_submit(client, "w12", "CN-W12", [
        rev(1, 1, "independent", [], eff_from="2026-01-01"),
    ], "2026-10-01", "2026-09-01")
    assert r.status_code == 422


# ---------- 原端点行为不变 ----------

def test_plain_applications_still_unchecked(client):
    """原 POST /applications 不做窗口审查：含失效引用的批次照常提交。"""
    r = submit(client, "p1", "CN-P1", [
        rev(1, 1, "independent", [], eff_from="2026-06-01"),  # 1~5 月并不存在
        rev(2, 1, "dependent", [1], eff_from="2026-01-01"),
    ])
    assert r.status_code == 201
    issues = audit(client, "CN-P1", "2026-03-01").json()["issues"]
    assert [i["code"] for i in issues] == ["INVALID_REFERENCE"]
