"""发布范围裁决：以昨晚在线基线 + 今晚增量批次，形成今日唯一在线范围。

裁决原则（与 docs/DATA-DICTIONARY.md 的事件语义一致）：
1. 本批显式撤回（delete 墓碑）一律不回流，即便昨晚基线在线、即便增量里夹带迟到旧 upsert。
2. 本批显式 upsert 的文档按本批最新版本上线。
3. 本批未覆盖的存量文档不做静默假设：
   - 持有可复核延续依据（基线在线快照 + 延续声明/未见撤回证据）才保留，并标记为延续；
   - 无延续依据的未覆盖文档一律阻断发布（BLOCKED），不得静默沿用，也不得静默删除。
4. 任一项阻断即整批发不通过（releasable=False），在线范围冻结为上一版，不产生半发布。
5. 同一输入（基线摘要+增量摘要+参数+规则版本）重放得到完全一致的成员与摘要，
   既不多出文档，也不改换已确认成员（幂等、内容寻址）。
6. 昨日基线与本批来源的 sha256、源行号全部进入结论，可逐项追回。
"""
import hashlib
import json
from collections import OrderedDict
from decimal import Decimal

from app.errors import DomainError
from app.values import number

# 成员判定结论
IN_BATCH_UPSERT = "BATCH_UPSERT"        # 本批明确上线
IN_BATCH_WITHDRAWN = "BATCH_WITHDRAWN"  # 本批明确撤回（墓碑，不回流）
CARRIED_OVER = "CARRIED_OVER"           # 未覆盖，凭延续依据保留
BLOCKED_NO_BASIS = "BLOCKED_NO_BASIS"   # 未覆盖且无延续依据，阻断
BATCH_CONFLICT = "BATCH_CONFLICT"       # 本批同版本内容冲突，阻断

# 最终在线成员只允许前两类 + 延续类；撤回类与阻断类不得进入 online_scope
ONLINE_STATES = {IN_BATCH_UPSERT, CARRIED_OVER}


def _sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


def _doc_fingerprint(row):
    """文档内容指纹：op+version+payload，保证同版本不同内容可被识别为冲突。"""
    payload = row.get("payload")
    if not isinstance(payload, str):
        payload = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(
        json.dumps([row.get("op"), str(row.get("version")), payload],
                   ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _normalize_batch(rows, source_label):
    """把一批事件归一为 doc_id -> 该批的最终裁决事件，隔离同版本冲突。

    返回 (winners, conflicts)。winners 为 OrderedDict，conflicts 为阻断条目列表。
    复用 M3 的语义：同 doc 同版本不同内容 -> 冲突；最高版本胜出；墓碑拦截迟到旧写。
    """
    grouped = OrderedDict()
    for row in rows:
        grouped.setdefault(row["doc_id"], []).append(row)

    winners = OrderedDict()
    conflicts = []
    for doc_id, events in grouped.items():
        # 同版本内容冲突检测
        per_version = {}
        conflict_versions = set()
        for ev in events:
            per_version.setdefault(str(ev["version"]), set()).add(_doc_fingerprint(ev))
        for ver, fps in per_version.items():
            if len(fps) > 1:
                conflict_versions.add(ver)
        if conflict_versions:
            item = {
                "doc_id": doc_id,
                "state": BATCH_CONFLICT,
                "online": False,
                "version": sorted(conflict_versions, key=lambda v: number(v))[-1],
                "reason": "SAME_VERSION_CONFLICT",
                "detail": "同文档同版本存在不同内容，无法裁决",
                "conflict_versions": sorted(conflict_versions, key=lambda v: number(v)),
                "source_lines": [ln for ev in events for ln in ev.get("_sources", [ev.get("_line", 0)])],
                "source": source_label,
            }
            conflicts.append(item)
            continue
        # 最高版本胜出（delete/upsert 由该最高版本事件决定，墓碑自然压制迟到旧 upsert）
        winner = max(events, key=lambda ev: number(ev["version"]))
        winners[doc_id] = winner
    return winners, conflicts


def _scope_fingerprint(members):
    """对在线成员集合做内容寻址，成员或其指纹任意变动都会改变摘要。"""
    canonical = json.dumps(
        sorted((m["doc_id"], m["version"], m["fingerprint"]) for m in members),
        ensure_ascii=False, sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def adjudicate(baseline_rows, batch_rows, *, baseline_raw=b"", batch_raw=b"",
               continuity=None, params=None):
    """形成今日唯一在线范围。

    :param baseline_rows: 昨晚在线基线事件（已通过 M1-M3 归一的行，含 doc_id/version/op/payload/_sources）
    :param batch_rows: 今晚增量批次事件
    :param baseline_raw / batch_raw: 两份来源的原始字节，用于留痕 sha256
    :param continuity: 可复核延续依据，映射 doc_id -> 依据说明（票据/快照引用）。
                       未覆盖文档只有命中该映射才允许 CARRIED_OVER，否则阻断。
    :return: 裁决结论 dict（含 online_scope、blocked、metrics、来源留痕、范围指纹）
    """
    params = params or {}
    continuity = continuity or {}
    if not isinstance(continuity, dict):
        raise DomainError("BAD_CONTINUITY", "延续依据需要为 doc_id -> 依据 的对象")

    base_winners, _ = _normalize_batch(baseline_rows, "baseline")
    batch_winners, batch_conflicts = _normalize_batch(batch_rows, "batch")

    # 基线中昨晚仍在线（非墓碑）的文档，才是可被“延续”的存量
    online_baseline = OrderedDict(
        (doc, ev) for doc, ev in base_winners.items() if ev.get("op") != "delete"
    )

    decisions = []
    blocked = list(batch_conflicts)
    decisions.extend(batch_conflicts)  # 冲突项同入裁决明细，保证逐文档可复核

    # 1) 本批覆盖的文档：显式撤回不回流；显式 upsert 上线
    for doc_id in sorted(batch_winners, key=lambda d: str(d)):
        ev = batch_winners[doc_id]
        lines = ev.get("_sources", [ev.get("_line", 0)])
        if ev.get("op") == "delete":
            decisions.append({
                "doc_id": doc_id, "state": IN_BATCH_WITHDRAWN,
                "online": False, "version": str(ev["version"]),
                "fingerprint": _doc_fingerprint(ev),
                "basis": "本批显式撤回墓碑，禁止回流",
                "source_lines": lines, "source": "batch",
            })
        else:
            decisions.append({
                "doc_id": doc_id, "state": IN_BATCH_UPSERT,
                "online": True, "version": str(ev["version"]),
                "fingerprint": _doc_fingerprint(ev),
                "basis": "本批显式上线",
                "payload": ev.get("payload"),
                "source_lines": lines, "source": "batch",
            })

    batch_docs = set(batch_winners)
    # 2) 本批未覆盖的存量：要么有延续依据，要么阻断
    for doc_id in sorted(online_baseline, key=lambda d: str(d)):
        if doc_id in batch_docs:
            continue
        base_ev = online_baseline[doc_id]
        lines = base_ev.get("_sources", [base_ev.get("_line", 0)])
        if doc_id in continuity:
            decisions.append({
                "doc_id": doc_id, "state": CARRIED_OVER,
                "online": True, "version": str(base_ev["version"]),
                "fingerprint": _doc_fingerprint(base_ev),
                "basis": "未覆盖存量，凭延续依据保留：%s" % continuity[doc_id],
                "continuity_ref": continuity[doc_id],
                "payload": base_ev.get("payload"),
                "source_lines": lines, "source": "baseline",
            })
        else:
            item = {
                "doc_id": doc_id, "state": BLOCKED_NO_BASIS,
                "online": False, "version": str(base_ev["version"]),
                "fingerprint": _doc_fingerprint(base_ev),
                "reason": "UNCOVERED_NO_BASIS",
                "detail": "本批未覆盖且无延续依据，无法判定未变化或漏送，阻断发布",
                "source_lines": lines, "source": "baseline",
            }
            decisions.append(item)
            blocked.append(item)

    # 在线成员仅由“本批上线 + 合规延续”构成；撤回与阻断绝不进入
    online_members = [
        {
            "doc_id": d["doc_id"], "version": d["version"],
            "state": d["state"], "fingerprint": d["fingerprint"],
            "source": d["source"], "source_lines": d["source_lines"],
        }
        for d in decisions if d["state"] in ONLINE_STATES
    ]
    scope_sha = _scope_fingerprint(online_members)

    metrics = {
        "baseline_online": len(online_baseline),
        "batch_docs": len(batch_winners),
        "batch_upsert": sum(d["state"] == IN_BATCH_UPSERT for d in decisions),
        "batch_withdrawn": sum(d["state"] == IN_BATCH_WITHDRAWN for d in decisions),
        "carried_over": sum(d["state"] == CARRIED_OVER for d in decisions),
        "blocked": len(blocked),
        "online_members": len(online_members),
        "releasable": len(blocked) == 0,
        "scope_sha256": scope_sha,
    }
    provenance = {
        "baseline_input_sha256": _sha256_bytes(baseline_raw),
        "batch_input_sha256": _sha256_bytes(batch_raw),
        "rule_version": "release-r1",
        "parameters": params,
    }
    return {
        "decisions": decisions,
        "online_scope": online_members,
        "blocked": blocked,
        "metrics": metrics,
        "provenance": provenance,
        "scope_sha256": scope_sha,
        "releasable": len(blocked) == 0,
    }


def confirm_release(result):
    """发布闸门：存在任何阻断项时拒绝确认，避免半发布。"""
    if not result.get("releasable"):
        raise DomainError(
            "RELEASE_BLOCKED",
            "存在 %d 项阻断（无延续依据或批次冲突），在线范围冻结，禁止发布"
            % len(result.get("blocked", [])),
            409,
        )
    return {
        "released": True,
        "scope_sha256": result["scope_sha256"],
        "online_members": len(result["online_scope"]),
    }
