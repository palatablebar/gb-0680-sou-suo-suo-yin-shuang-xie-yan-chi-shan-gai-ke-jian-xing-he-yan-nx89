"""发布服务与 HTTP 端到端测试。"""
import csv
import io

import pytest

from app.config import ROOT
from app.release import service

RELEASE_DIR = ROOT / "sample_data" / "release"


def _raw(name):
    return (RELEASE_DIR / name).read_bytes()


@pytest.fixture(autouse=True)
def isolated_release_db(tmp_path, monkeypatch):
    monkeypatch.setattr(service, "DATA_DIR", tmp_path)


@pytest.fixture
def inputs():
    return _raw("baseline.csv"), _raw("increment.csv"), _raw("continuity.csv")


def test_sample_scenario_adjudication(inputs):
    result = service.build_release(*inputs)
    states = {d["doc_id"]: d["state"] for d in result["decisions"]}
    # DOC-001 本批上线；DOC-002 撤回不回流（迟到旧写被压制）
    assert states["DOC-001"] == "BATCH_UPSERT"
    assert states["DOC-002"] == "BATCH_WITHDRAWN"
    # DOC-004/005 凭延续依据保留；DOC-006 无依据阻断；DOC-009 冲突阻断
    assert states["DOC-004"] == "CARRIED_OVER"
    assert states["DOC-005"] == "CARRIED_OVER"
    assert states["DOC-006"] == "BLOCKED_NO_BASIS"
    assert states["DOC-009"] == "BATCH_CONFLICT"
    # DOC-007 昨晚墓碑不复活；DOC-008 新增上线
    online = {m["doc_id"] for m in result["online_scope"]}
    assert "DOC-007" not in online and "DOC-002" not in online
    assert "DOC-008" in online
    assert result["releasable"] is False  # 因 DOC-006 与 DOC-009 阻断


def test_idempotent_same_batch_same_release(inputs):
    r1 = service.execute_release(*inputs)
    r2 = service.execute_release(*inputs)
    assert r1["release_id"] == r2["release_id"]
    assert r1["scope_sha256"] == r2["scope_sha256"]
    assert r1["online_scope"] == r2["online_scope"]
    assert len(service.list_releases()) == 1  # 不产生重复发布单


def test_blocked_release_cannot_be_signed(inputs):
    r = service.execute_release(*inputs)
    with pytest.raises(service.DomainError) as e:
        service.sign_release(r["release_id"], r["version"], "确认发布")
    assert e.value.code == "RELEASE_BLOCKED"


def test_resolve_block_then_release(inputs):
    baseline, batch, _ = inputs
    # 补齐 DOC-006 延续依据，并移除冲突文档，得到可发布单（新内容寻址 -> 新单号）
    cont = _raw("continuity.csv") + "DOC-006,SNAP-20261121-DOC006,补录依据\n".encode()
    clean_batch = b"\n".join(
        line for line in batch.splitlines() if not line.startswith(b"DOC-009")
    ) + b"\n"
    r = service.execute_release(baseline, clean_batch, cont)
    assert r["releasable"] is True
    signed = service.sign_release(r["release_id"], r["version"], "确认发布")
    assert signed["decision"] == "确认发布" and signed["version"] == 2


def test_stale_sign_409(inputs):
    baseline, batch, _ = inputs
    # 构造一个可发布单：移除冲突文档并补齐 DOC-006 延续依据
    clean_batch = b"\n".join(
        line for line in batch.splitlines() if not line.startswith(b"DOC-009")
    ) + b"\n"
    cont = _raw("continuity.csv") + "DOC-006,SNAP-20261121-DOC006,补录依据\n".encode()
    r = service.execute_release(baseline, clean_batch, cont)
    service.sign_release(r["release_id"], 1, "确认发布")
    with pytest.raises(service.DomainError) as e:
        service.sign_release(r["release_id"], 1, "退回")
    assert e.value.code == "STALE_VERSION" and e.value.status == 409


def test_archive_matches_page_decision(inputs):
    from app.release.archive import build_release_archive
    r = service.execute_release(*inputs)
    csv_text = build_release_archive(r)
    rows = list(csv.reader(io.StringIO(csv_text.lstrip("﻿"))))
    header = rows[0]
    idx = {name: i for i, name in enumerate(header)}
    decision_rows = [x for x in rows[1:] if x[idx["kind"]] == "decision"]
    # 导出行的判定与内存裁决逐文档一致
    exported = {x[idx["doc_id"]]: x[idx["state"]] for x in decision_rows}
    inmem = {d["doc_id"]: d["state"] for d in r["decisions"]}
    assert exported == inmem
    # 范围指纹与在线成员数写入末行
    scope_row = [x for x in rows[1:] if x[idx["kind"]] == "scope"][0]
    assert scope_row[idx["evidence"]] == r["scope_sha256"]
    assert scope_row[idx["basis_or_reason"]] == \
        "online_member_count=%d" % r["metrics"]["online_members"]


# ---------------- HTTP ----------------

def test_http_preview_and_runs(client, inputs):
    baseline, batch, cont = inputs
    body = {"baseline_csv": baseline.decode(), "batch_csv": batch.decode(),
            "continuity_csv": cont.decode()}
    r = client.post("/api/release/preview", json=body)
    assert r.status_code == 200 and r.get_json()["releasable"] is False
    r2 = client.post("/api/release/runs", json=body)
    assert r2.status_code == 200
    rid = r2.get_json()["release_id"]
    assert client.get("/api/release/runs/" + rid).status_code == 200
    assert client.get("/api/release/runs").get_json()["releases"][0]["release_id"] == rid


def test_http_blocked_sign_409(client, inputs):
    baseline, batch, cont = inputs
    r = client.post("/api/release/runs", json={
        "baseline_csv": baseline.decode(), "batch_csv": batch.decode(),
        "continuity_csv": cont.decode()}).get_json()
    resp = client.post("/api/release/runs/%s/sign" % r["release_id"],
                       json={"version": 1, "decision": "确认发布"})
    assert resp.status_code == 409 and resp.get_json()["code"] == "RELEASE_BLOCKED"


def test_http_archive_endpoint(client, inputs):
    baseline, batch, cont = inputs
    r = client.post("/api/release/runs", json={
        "baseline_csv": baseline.decode(), "batch_csv": batch.decode(),
        "continuity_csv": cont.decode()}).get_json()
    resp = client.get("/api/release/runs/%s/archive" % r["release_id"])
    assert resp.status_code == 200 and resp.mimetype == "text/csv"
    assert r["scope_sha256"] in resp.data.decode()


def test_http_missing_input_422(client):
    resp = client.post("/api/release/runs", json={"baseline_csv": "x"})
    assert resp.status_code == 422 and resp.get_json()["code"] == "MISSING_INPUT"


def test_http_empty_file_rejected(client):
    resp = client.post("/api/release/preview",
                       json={"baseline_csv": "", "batch_csv": "a,b\n1,2"})
    assert resp.status_code == 422 and resp.get_json()["code"] == "EMPTY_FILE"


def test_release_page_renders_same_scope(client, inputs):
    resp = client.get("/release")
    assert resp.status_code == 200
    baseline, batch, cont = inputs
    api = client.post("/api/release/preview", json={
        "baseline_csv": baseline.decode(), "batch_csv": batch.decode(),
        "continuity_csv": cont.decode()}).get_json()
    # 页面展示的范围指纹与 API 裁决一致
    assert api["scope_sha256"] in resp.data.decode()
    # 阻断文档在页面可见
    assert "DOC-006" in resp.data.decode()
