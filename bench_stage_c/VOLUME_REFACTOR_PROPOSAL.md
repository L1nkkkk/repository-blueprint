# C4 体积改造方案（仅供决策，本轮不实施）

## 归因证据

`DBSTAT.txt` 来自 `SELECT name, SUM(pgsize) FROM dbstat GROUP BY name ORDER BY 2 DESC`。修补轮 map.sqlite 为 204,734,464 bytes，dbstat 可计量页 203,501,568 bytes；top 3 为：

| 对象 | bytes | 占 dbstat |
|---|---:|---:|
| nodes | 62,513,152 | 30.7188% |
| edges | 30,375,936 | 14.9266% |
| sqlite_autoindex_edges_1 | 29,814,784 | 14.6509% |

`edges` 表及其唯一索引合计 60,190,720 bytes，占 dbstat 29.576%；其中 `src_id`/`dst_id` 是 24 位十六进制 symbol ID 的 TEXT 值，存在每条边重复存储。`idx_edges_dst` 另占 17,014,784 bytes。`source_texts` 已关闭且不再是主要来源。

## 建议方案

新增整数 surrogate key：在 `nodes` 保留 `id TEXT UNIQUE` 作为外部稳定身份，同时增加 `node_rowid INTEGER PRIMARY KEY`；`edges.src_rowid`、`edges.dst_rowid` 改为整数外键，唯一约束改为 `(src_rowid,dst_rowid,kind,site_line)`。查询边时通过一次节点映射恢复稳定 symbol ID。

影响面包括：建图与增量重绑、`skeleton.calls` 的 callers/callees、`update_search`、`degree` 触发器、`doctor` 完整性检查、边准确性导出、历史迁移与所有 SQL 索引。语义任务、evidence 和外部 API 继续使用 TEXT symbol ID，迁移层负责双向映射。

## 迁移策略与收益验证

1. 新增 schema version，不原地改旧表；由旧 `edges` 分批填充 `edges_v2`，按 node ID 建映射表。
2. 在同一事务内建立新索引、校验边数量/唯一键/degree/查询结果，再切换表名。
3. 保留只读回滚快照；旧数据库可通过离线迁移工具恢复。
4. 用本轮 327K edges 的真实 map 重跑 dbstat、查询延迟、增量 sync 和并发混压。

理论收益只对 edges 的两个 TEXT 主键列及相关索引生效，不能把全部 203MB 按比例扣除；`nodes` 仍是最大项，预期总体降幅需要实测，不能在本轮拍脑袋承诺。
