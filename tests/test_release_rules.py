"""发布范围裁决规则测试：逐条对应用题面六项要求。"""
import pytest

from app.errors import DomainError
from app.release.rules import (
    BLOCKED_NO_BASIS, BATCH_CONFLICT, IN_BATCH_WITHDRAWN as BATCH_WITHDRAWN,
    IN_BATCH_UPSERT as BATCH_UPSERT, CARRIED_OVER, adjudicate, confirm_release,
)


def ev(doc, version, op="upsert", payload=None, line=None):
    return {
        "doc_id": doc, "version": str(version), "op": op,
        "payload": payload if payload is not None else {"v": version},
        "_sources": [line if line is not None else 1],
    }


def state_map(result):
    return {d["doc_id"]: d["state"] for d in result["decisions"]}


# 要求1：本批明确撤回的不能回流（即便夹带迟到旧 upsert）
def test_withdrawn_never_returns():
    baseline = [ev("D1", 1, line=1)]
    batch = [ev("D1", 2, "delete", payload={}, line=2),
             ev("D1", 1, "upsert", line=3)]  # 迟到旧写
    result = adjudicate(baseline, batch)
    assert state_map(result)["D1"] == BATCH_WITHDRAWN
    online = {m["doc_id"] for m in result["online_scope"]}
    assert "D1" not in online
    assert result["metrics"]["batch_withdrawn"] == 1


# 要求2：未覆盖存量——有可复核延续依据才保留
def test_uncovered_carried_over_with_basis():
    baseline = [ev("D1", 1, line=1)]
    result = adjudicate(baseline, [], continuity={"D1": "SNAP-1"})
    d = state_map(result)["D1"]
    assert d == CARRIED_OVER
    member = next(m for m in result["online_scope"] if m["doc_id"] == "D1")
    assert member["source"] == "baseline" and member["source_lines"] == [1]
    assert "SNAP-1" in next(x for x in result["decisions"] if x["doc_id"] == "D1")["basis"]


# 要求3：未覆盖且无依据——阻断发布，不静默删、不静默留
def test_uncovered_without_basis_blocks():
    baseline = [ev("D1", 1, line=1), ev("D2", 1, line=2)]
    result = adjudicate(baseline, [], continuity={"D1": "SNAP-1"})
    assert state_map(result)["D2"] == BLOCKED_NO_BASIS
    assert result["releasable"] is False
    assert result["metrics"]["blocked"] == 1
    # 阻断文档不在在线范围
    assert "D2" not in {m["doc_id"] for m in result["online_scope"]}
    with pytest.raises(DomainError) as e:
        confirm_release(result)
    assert e.value.code == "RELEASE_BLOCKED" and e.value.status == 409


# 要求1+3 联合：撤回不回流，且有依据者延续，整批可发布
def test_clean_release_passes_gate():
    baseline = [ev("D1", 1, line=1), ev("D2", 1, line=2), ev("D3", 1, line=3)]
    batch = [ev("D1", 2, "delete", payload={}, line=4),
             ev("D3", 2, line=5)]
    result = adjudicate(baseline, batch, continuity={"D2": "SNAP-2"})
    assert result["releasable"] is True
    online = {m["doc_id"]: m for m in result["online_scope"]}
    assert set(online) == {"D2", "D3"}  # D1撤回、D2延续、D3本批上线
    assert online["D3"]["version"] == "2"
    gate = confirm_release(result)
    assert gate["released"] is True and gate["online_members"] == 2


# 要求4：重放同一批次，在线成员与范围指纹完全一致（不多文档、不换成员）
def test_replay_is_identical():
    baseline = [ev("D1", 1, line=1), ev("D2", 1, line=2)]
    batch = [ev("D1", 2, line=3)]
    r1 = adjudicate(baseline, batch, baseline_raw=b"b", batch_raw=b"x",
                    continuity={"D2": "SNAP-2"})
    r2 = adjudicate(baseline, batch, baseline_raw=b"b", batch_raw=b"x",
                    continuity={"D2": "SNAP-2"})
    assert r1["scope_sha256"] == r2["scope_sha256"]
    assert r1["online_scope"] == r2["online_scope"]
    assert r1["metrics"] == r2["metrics"]


# 成员一旦确认，输入顺序变化不改变范围指纹（集合语义，顺序无关）
def test_scope_independent_of_input_order():
    baseline = [ev("D1", 1, line=1), ev("D2", 1, line=2)]
    batch = [ev("D3", 1, line=3)]
    cont = {"D1": "S1", "D2": "S2"}
    a = adjudicate(baseline, batch, continuity=cont)
    b = adjudicate(list(reversed(baseline)), list(reversed(batch)), continuity=cont)
    assert a["scope_sha256"] == b["scope_sha256"]


# 成员指纹随内容变化：改换已确认成员会改变范围摘要（防偷换）
def test_scope_changes_if_member_content_changes():
    baseline = [ev("D1", 1, payload={"v": 1}, line=1)]
    a = adjudicate(baseline, [], continuity={"D1": "S"})
    baseline2 = [ev("D1", 1, payload={"v": 999}, line=1)]
    b = adjudicate(baseline2, [], continuity={"D1": "S"})
    assert a["scope_sha256"] != b["scope_sha256"]


# 同版本内容冲突必须阻断，不得任选其一上线
def test_same_version_conflict_blocks():
    baseline = []
    batch = [ev("D9", 1, payload={"a": 1}, line=1),
             ev("D9", 1, payload={"a": 2}, line=2)]
    result = adjudicate(baseline, batch)
    assert state_map(result)["D9"] == BATCH_CONFLICT
    assert result["releasable"] is False
    assert "D9" not in {m["doc_id"] for m in result["online_scope"]}


# 昨晚已是墓碑的文档即使未被本批覆盖，也不会被“延续”复活
def test_baseline_tombstone_not_resurrected():
    baseline = [ev("D7", 1, "delete", payload={}, line=1)]
    result = adjudicate(baseline, [], continuity={"D7": "SNAP-7"})
    assert "D7" not in {m["doc_id"] for m in result["online_scope"]}
    assert all(d["doc_id"] != "D7" for d in result["decisions"])


# 本批 upsert 上线（含新增文档）
def test_batch_upsert_online():
    result = adjudicate([], [ev("D8", 1, line=1)])
    assert state_map(result)["D8"] == BATCH_UPSERT
    assert result["metrics"]["online_members"] == 1


# 要求6：来源留痕——两份来源 sha256 与源行号可追回
def test_provenance_and_lineage():
    result = adjudicate([ev("D1", 1, line=7)], [ev("D2", 1, line=9)],
                        baseline_raw=b"base", batch_raw=b"incr",
                        continuity={"D1": "SNAP-1"})
    p = result["provenance"]
    assert len(p["baseline_input_sha256"]) == 64
    assert len(p["batch_input_sha256"]) == 64
    assert p["baseline_input_sha256"] != p["batch_input_sha256"]
    lines = {m["doc_id"]: m["source_lines"] for m in result["online_scope"]}
    assert lines == {"D1": [7], "D2": [9]}


def test_bad_continuity_shape():
    with pytest.raises(DomainError) as e:
        adjudicate([ev("D1", 1)], [], continuity=["not-a-dict"])
    assert e.value.code == "BAD_CONTINUITY"
