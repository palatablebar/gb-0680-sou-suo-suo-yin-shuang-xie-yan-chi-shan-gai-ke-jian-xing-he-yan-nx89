"""发布裁决页面路由：与导出共用 service.build_release 的同一份判定。"""
from flask import Blueprint, render_template

from app.config import ROOT, TITLE
from app.release import service

bp = Blueprint("release_page", __name__)

RELEASE_SAMPLE = ROOT / "sample_data" / "release"

STATE_LABELS = {
    "BATCH_UPSERT": "本批上线",
    "BATCH_WITHDRAWN": "本批撤回·不回流",
    "CARRIED_OVER": "延续保留",
    "BLOCKED_NO_BASIS": "阻断·无延续依据",
    "BATCH_CONFLICT": "阻断·版本冲突",
}


@bp.route("/release", methods=["GET"])
def page():
    # 页面回放仓内样例；实际上传与发布走 /api/release/*，判定入口与页面完全一致
    result = service.build_release(
        (RELEASE_SAMPLE / "baseline.csv").read_bytes(),
        (RELEASE_SAMPLE / "increment.csv").read_bytes(),
        (RELEASE_SAMPLE / "continuity.csv").read_bytes(),
    )
    return render_template(
        "release.html", title=TITLE, result=result,
        state_labels=STATE_LABELS, history=service.list_releases(),
    )
