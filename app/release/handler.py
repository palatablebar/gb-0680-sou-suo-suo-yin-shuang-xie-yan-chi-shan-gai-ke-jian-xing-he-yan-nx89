"""发布范围裁决 HTTP 接口。

POST /api/release/preview  仅裁决不落库（页面预览）
POST /api/release/runs     裁决并幂等保存发布单
GET  /api/release/runs     发布单历史
GET  /api/release/runs/<id> 发布单详情
POST /api/release/runs/<id>/sign  确认发布/退回（阻断单确认返回 409）
GET  /api/release/runs/<id>/archive 导出证据 CSV（与页面同一判定）
"""
from flask import Blueprint, jsonify, request

from app.errors import DomainError
from app.release import service
from app.release.archive import build_release_archive

bp = Blueprint("release", __name__)


def _three_inputs(payload):
    """从请求中取三份输入：优先给原始 CSV 文本，否则给样例名。"""
    def pick(key):
        val = payload.get(key)
        if val is None:
            return None
        if isinstance(val, str):
            return val.encode()
        raise DomainError("BAD_CSV", "%s 需要 CSV 文本" % key)

    baseline = pick("baseline_csv")
    batch = pick("batch_csv")
    continuity = pick("continuity_csv") or b""
    if baseline is None or batch is None:
        raise DomainError("MISSING_INPUT", "需要 baseline_csv 与 batch_csv")
    return baseline, batch, continuity


@bp.route("/api/release/preview", methods=["POST"])
def preview():
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    baseline, batch, continuity = _three_inputs(payload)
    return jsonify(service.build_release(baseline, batch, continuity))


@bp.route("/api/release/runs", methods=["GET", "POST"])
def runs():
    if request.method == "GET":
        return jsonify({"releases": service.list_releases()})
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        raise DomainError("BAD_BODY", "请求需要对象")
    baseline, batch, continuity = _three_inputs(payload)
    return jsonify(service.execute_release(baseline, batch, continuity))


@bp.route("/api/release/runs/<rid>", methods=["GET"])
def detail(rid):
    return jsonify(service.get_release(rid))


@bp.route("/api/release/runs/<rid>/sign", methods=["POST"])
def sign(rid):
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload.get("version"), int):
        raise DomainError("BAD_VERSION", "请提供版本号")
    return jsonify(
        service.sign_release(rid, payload["version"], payload.get("decision"))
    )


@bp.route("/api/release/runs/<rid>/archive", methods=["GET"])
def archive(rid):
    release = service.get_release(rid)
    from flask import current_app
    return current_app.response_class(
        build_release_archive(release), mimetype="text/csv",
        headers={"Content-Disposition": 'attachment; filename="release-scope.csv"'},
    )
