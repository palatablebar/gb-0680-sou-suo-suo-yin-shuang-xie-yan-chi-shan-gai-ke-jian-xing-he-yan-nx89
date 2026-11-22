"""发布证据导出：页面与导出共用同一份裁决 payload，保证判定一致。"""
import csv
import io
import json


def build_release_archive(release):
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    meta = release["manifest"]
    writer.writerow([
        "release_id", "baseline_input_sha256", "batch_input_sha256",
        "continuity_input_sha256", "rule_version", "scope_sha256",
        "releasable", "kind", "doc_id", "version", "state",
        "online", "source", "source_lines", "basis_or_reason", "evidence",
    ])
    common = [
        release["release_id"], meta["baseline_input_sha256"],
        meta["batch_input_sha256"], meta["continuity_input_sha256"],
        meta["rule_version"], release["scope_sha256"],
        release["releasable"],
    ]
    for d in release["decisions"]:
        writer.writerow(common + [
            "decision", d["doc_id"], d.get("version", ""), d["state"],
            d["online"], d.get("source", ""),
            json.dumps(d.get("source_lines", []), ensure_ascii=False),
            d.get("basis") or d.get("detail") or d.get("reason", ""),
            json.dumps(d, ensure_ascii=False, default=str),
        ])
    for q in release.get("quarantine", []):
        writer.writerow(common + [
            "quarantine", q.get("doc_id", ""), q.get("version", ""),
            q.get("reason", ""), False, "input",
            json.dumps(q.get("_sources", []), ensure_ascii=False),
            q.get("detail", ""), json.dumps(q, ensure_ascii=False, default=str),
        ])
    # 结尾再写一行在线范围指纹，便于外部核验“今日唯一在线范围”
    writer.writerow(common + [
        "scope", "", "", "ONLINE_SCOPE", True, "derived", "[]",
        "online_member_count=%d" % release["metrics"]["online_members"],
        release["scope_sha256"],
    ])
    return "﻿" + stream.getvalue()
