"""发布裁决输入加载：解析基线/增量/延续依据三份 CSV。

沿用项目既有约定：utf-8-sig 优先、GB18030 回退；空文件/全空白/仅表头分开拒绝；
doc_id 全程字符串保形；业务键缺失隔离；同键完全相同计重复，冲突隔离。
"""
import csv
import io

from app.errors import DomainError
from app.modules.m1_streams.rules import decode
from app.reasons import reject

BATCH_COLUMNS = ["doc_id", "version", "op", "source_time", "payload", "note"]
CONTINUITY_COLUMNS = ["doc_id", "continuity_ref", "detail"]


def _parse_rows(raw, expected_header, label, dedupe_key="doc_id"):
    text, encoding = decode(raw)
    if not text:
        raise DomainError("EMPTY_FILE", "%s 为空文件" % label)
    if not text.strip():
        raise DomainError("BLANK_ONLY", "%s 全部为空白" % label)
    reader = csv.reader(io.StringIO(text, newline=""))
    header = next(reader, None)
    if header != expected_header:
        raise DomainError("BAD_HEADER", "%s 表头不匹配：%s" % (label, header))

    rows, bad = [], []
    seen = {}
    count = blank = repeated = duplicate = 0
    for cells in reader:
        count += 1
        line = reader.line_num
        if not cells or not any(c.strip() for c in cells):
            blank += 1
            continue
        if cells == header:
            repeated += 1
            continue
        row = {"_line": line, "_sources": [line]}
        if len(cells) != len(header):
            bad.append(reject(row, "BAD_COLUMNS", "%s 列数不匹配" % label))
            continue
        row.update(dict(zip(header, [x.strip() for x in cells])))
        if not row.get(dedupe_key):
            bad.append(reject(row, "EMPTY_VALUE", "%s 业务键为空" % label))
            continue
        key = row[dedupe_key]
        # 完全相同行计重复并折叠；同键不同值不在此隔离——事件流允许多版本，
        # 同版本内容冲突交由 release.rules 的版本裁决处理（M3 语义）。
        if key in seen:
            if cells == seen[key]:
                duplicate += 1
                continue
        seen[key] = cells
        rows.append(row)

    if count == 0:
        raise DomainError("HEADER_ONLY", "%s 仅有表头" % label)
    if not rows and not bad:
        raise DomainError("BLANK_ONLY", "%s 无有效记录" % label)
    return rows, bad, {
        "input": count, "accepted": len(rows), "quarantine": len(bad),
        "duplicate": duplicate, "blank": blank,
        "repeated_headers": repeated, "encoding": encoding, "label": label,
    }


def load_event_batch(raw, label):
    rows, bad, metrics = _parse_rows(raw, BATCH_COLUMNS, label)
    # 版本字段不能为空（哨兵由 number 拦截），op 仅允许 upsert/delete
    clean = []
    for row in rows:
        from app.values import number as _number  # 局部导入避免循环
        try:
            _number(row["version"])
        except DomainError:
            bad.append(reject(row, "EMPTY_VALUE", "%s 版本缺失" % label))
            continue
        if row["op"] not in {"upsert", "delete"}:
            bad.append(reject(row, "INVALID_OP", "%s 非法操作 %s" % (label, row["op"])))
            continue
        clean.append(row)
    return clean, bad, metrics


def load_continuity(raw):
    rows, bad, metrics = _parse_rows(raw, CONTINUITY_COLUMNS, "延续依据")
    mapping = {}
    for row in rows:
        if not row.get("continuity_ref"):
            bad.append(reject(row, "EMPTY_VALUE", "延续依据缺少票据引用"))
            continue
        mapping[row["doc_id"]] = row["continuity_ref"]
    return mapping, bad, metrics
