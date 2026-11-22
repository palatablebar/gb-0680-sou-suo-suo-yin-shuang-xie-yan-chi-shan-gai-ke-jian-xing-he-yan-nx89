# 接口

模块GET使用仓内样例，模块POST可传csv或rows与context，直接计算当前规则。/api/runs使用完整流水线并原子保存。每个阶段返回rows、quarantine、metrics；后续阶段还返回累计隔离。/api/upload接收multipart文件file。/api/runs/<id>/archive只导出一份带清单与证据的CSV。

## 发布范围裁决（增量批次 → 今日唯一在线范围）

页面 `/release`；接口前缀 `/api/release`。输入三份 CSV：`baseline_csv`（昨晚在线基线）、`batch_csv`（今晚增量）、`continuity_csv`（可复核延续依据，可空）。

- `POST /api/release/preview`：仅裁决不落库，返回逐文档 decisions、online_scope、blocked、metrics、scope_sha256、manifest。
- `POST /api/release/runs`：裁决并按内容寻址幂等保存；同源重放返回同一 release_id、同一在线成员与指纹。
- `GET /api/release/runs`：发布单历史；`GET /api/release/runs/<id>`：详情。
- `POST /api/release/runs/<id>/sign`：body 含 version 与 decision（确认发布/退回）。存在阻断项时确认返回 409 RELEASE_BLOCKED；陈旧版本返回 409 STALE_VERSION。
- `GET /api/release/runs/<id>/archive`：导出证据 CSV，与页面共用同一裁决 payload，末行写入 scope_sha256 与在线成员数。

裁决状态：BATCH_UPSERT 本批上线、BATCH_WITHDRAWN 本批撤回不回流、CARRIED_OVER 凭延续依据保留、BLOCKED_NO_BASIS 未覆盖且无依据阻断、BATCH_CONFLICT 同版本内容冲突阻断。仅前两类与 CARRIED_OVER 进入 online_scope。
