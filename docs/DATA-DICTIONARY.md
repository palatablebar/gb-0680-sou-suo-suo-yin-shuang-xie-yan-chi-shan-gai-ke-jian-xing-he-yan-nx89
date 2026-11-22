# 双写定义

event_id唯一，doc_id文本保留。side为source或index；版本正整数按Decimal比较并作为文本序列化。upsert要求JSON对象，delete忽略payload并产生墓碑。相同文档同端同版本不同内容全部隔离，存在未裁决冲突的主体不回落旧版本；同内容合并来源；较高版本覆盖低版本，迟到旧写不得覆盖墓碑。

双端最终版本、操作、文档分别核验。目标缺失、删除未确认、索引领先、陈旧索引、内容差异分开展示。source_time与visible_time带偏移；可见延迟按实际秒数。默认阈值120秒，等于阈值通过。已可见但超过阈值为延迟，尚无目标且等待时间超过阈值为缺失，等待中不冒充延迟。

对账expected=每文档应通过的1，reported=门禁通过1或未通过0，逐条保存visibility_state和gate原因，允许按文档复核。

业务ID和超大整数按文本保形；Decimal参与数量金额计算，对外序列化为字符串。哨兵集合定义在app/values.py，缺失值不能当零。所有时间带UTC偏移，先转UTC比较，业务日按指定地区划分。区间统一[start,end)。重复业务键保留第一条、完全相同行计重复；值冲突行隔离且保留原值。空文件、全空白、仅表头分开拒绝。

运行清单包括输入sha256、规则版本、参数、生成时刻与耗时。源行编号以CSV解析器的物理结束行号表示，引号内换行不增加记录数。输入超过4MB返回413，非法表头和类型返回422，失效版本复核返回409。

## 发布范围裁决输入（sample_data/release/）

基线与增量列：`doc_id,version,op,source_time,payload,note`；延续依据列：`doc_id,continuity_ref,detail`。doc_id 全程字符串保形，版本按 Decimal 比较、文本序列化，op 仅 upsert/delete。

裁决规则：增量批次不是全量切换。本批最高版本事件决定文档命运——delete 墓碑压制一切迟到旧 upsert，撤回文档绝不回流；upsert 按本批版本上线。本批未覆盖的昨晚在线文档不做静默假设：命中延续依据（continuity_ref 票据）才标记 CARRIED_OVER 保留并留痕，否则 BLOCKED_NO_BASIS 阻断；同文档同版本不同内容为 BATCH_CONFLICT 阻断。昨晚基线中的墓碑即使被延续依据命中也不复活。任一阻断项存在即 releasable=False，在线范围冻结、禁止确认发布（409），不产生半发布。

在线范围按成员 (doc_id, version, fingerprint) 集合内容寻址生成 scope_sha256；发布单 id 由基线、增量、延续依据三份 sha256 与规则版本派生，同源重放幂等，成员不多不换。页面 `/release` 与证据导出共用同一裁决结果；manifest 保存三份来源 sha256，每个成员保留来源端与源行号，可逐项追回。