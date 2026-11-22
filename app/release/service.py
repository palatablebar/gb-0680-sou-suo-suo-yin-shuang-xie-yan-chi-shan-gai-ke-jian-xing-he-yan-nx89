"""发布服务：编排 加载 -> 裁决 -> 幂等持久化 -> 发布确认/导出。

发布单按 (基线摘要, 增量摘要, 延续依据摘要, 规则版本) 内容寻址，
重复执行同一批次得到同一 release_id 与完全一致的在线成员，不多文档、不换成员。
"""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

from app.config import DATA_DIR
from app.errors import DomainError
from app.release.loader import load_continuity, load_event_batch
from app.release.rules import adjudicate, confirm_release

RULE_VERSION = "release-r1"


def _fingerprint(raw):
    return hashlib.sha256(raw).hexdigest()


def release_id(baseline_raw, batch_raw, continuity_raw):
    material = b"|".join([
        _fingerprint(baseline_raw).encode(),
        _fingerprint(batch_raw).encode(),
        _fingerprint(continuity_raw).encode(),
        RULE_VERSION.encode(),
    ])
    return hashlib.sha256(material).hexdigest()[:20]


def _connect():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DATA_DIR / "releases.db", timeout=20)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute(
        "CREATE TABLE IF NOT EXISTS releases ("
        "id TEXT PRIMARY KEY, payload TEXT NOT NULL, "
        "version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT '待发布', "
        "scope_sha256 TEXT NOT NULL)"
    )
    return db


def build_release(baseline_raw, batch_raw, continuity_raw=b""):
    """纯计算：不写库，返回完整发布裁决（页面预览与导出共用此入口）。"""
    base_rows, base_bad, base_metrics = load_event_batch(baseline_raw, "基线")
    batch_rows, batch_bad, batch_metrics = load_event_batch(batch_raw, "增量")
    continuity, cont_bad, cont_metrics = (
        load_continuity(continuity_raw) if continuity_raw.strip() else ({}, [], {})
    )

    quarantine = base_bad + batch_bad + cont_bad
    result = adjudicate(
        base_rows, batch_rows,
        baseline_raw=baseline_raw, batch_raw=batch_raw,
        continuity=continuity,
        params={"continuity_count": len(continuity)},
    )
    rid = release_id(baseline_raw, batch_raw, continuity_raw)
    result.update({
        "release_id": rid,
        "quarantine": quarantine,
        "intake": {
            "baseline": base_metrics, "batch": batch_metrics,
            "continuity": cont_metrics,
        },
        "manifest": {
            "baseline_input_sha256": _fingerprint(baseline_raw),
            "batch_input_sha256": _fingerprint(batch_raw),
            "continuity_input_sha256": _fingerprint(continuity_raw),
            "rule_version": RULE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        },
    })
    return result


def save_release(result):
    """幂等保存：同源重放 INSERT OR IGNORE，复用既有发布单，成员保持一致。"""
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO releases(id,payload,scope_sha256) VALUES (?,?,?)",
            (result["release_id"], json.dumps(result, ensure_ascii=False),
             result["scope_sha256"]),
        )
    return get_release(result["release_id"])


def execute_release(baseline_raw, batch_raw, continuity_raw=b""):
    return save_release(build_release(baseline_raw, batch_raw, continuity_raw))


def get_release(rid):
    with _connect() as db:
        row = db.execute(
            "SELECT payload,version,decision FROM releases WHERE id=?", (rid,)
        ).fetchone()
    if not row:
        raise DomainError("NOT_FOUND", "发布单不存在", 404)
    return {**json.loads(row[0]), "version": row[1], "decision": row[2]}


def latest_release():
    with _connect() as db:
        row = db.execute("SELECT id FROM releases ORDER BY rowid DESC LIMIT 1").fetchone()
    return get_release(row[0]) if row else None


def list_releases():
    with _connect() as db:
        return [
            {"release_id": i, "version": v, "decision": d, "scope_sha256": s}
            for i, v, d, s in db.execute(
                "SELECT id,version,decision,scope_sha256 FROM releases ORDER BY rowid DESC"
            )
        ]


def sign_release(rid, version, decision):
    """版本条件更新：陈旧确认返回 409；阻断单不允许确认发布。"""
    if decision not in {"确认发布", "退回"}:
        raise DomainError("BAD_DECISION", "发布决定无效")
    current = get_release(rid)
    if decision == "确认发布":
        confirm_release(current)  # 阻断项存在时抛 RELEASE_BLOCKED(409)
    with _connect() as db:
        cursor = db.execute(
            "UPDATE releases SET version=version+1,decision=? WHERE id=? AND version=?",
            (decision, rid, version),
        )
        if cursor.rowcount != 1:
            raise DomainError("STALE_VERSION", "版本已更新，请刷新", 409)
    return get_release(rid)
