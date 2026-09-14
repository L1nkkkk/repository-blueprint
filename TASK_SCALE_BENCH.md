# 任务：规模路径改造 + 10 万行级仓库基准验证

> 交给 Codex 执行。任务分两个阶段：**阶段 A（改造）在当前 session 完成；阶段 B（基准）必须另起一个全新的独立 session、并切换到 gpt-luna 模型执行**，避免改造过程的上下文污染基准判断。

## 背景

repository-blueprint 的骨架层（skeleton.py）已通过万行级仓库验收，但存在三处与"百万行目标"冲突的规模问题，以及若干并发/结构隐患。本任务要求修复主要问题，并在一个约 10 万行级的仓库上跑通基准，验证修复效果。

仓库路径：`<REPO_BLUEPRINT_PATH>`（repository-blueprint 本体，替换为执行机上的实际路径）
基准目标仓库：**mypy**（python/mypy，约 10-15 万行纯 Python）。执行阶段 B 时自行 clone：`git clone --depth 50 https://github.com/python/mypy.git`（保留最近 50 个提交，供增量测试用真实提交模拟改动），并记录 clone 到的 HEAD commit。不得替换为其他仓库；若 clone 失败，停下告知用户，不要自行选择替代仓库。

## 阶段 A：规模路径改造（当前 session）

### A1. FTS 定向 upsert（最高优先级）

现状：`refresh_search()` 每次 `DELETE FROM search` 后全量重灌；且 `submit()` 每提交一个语义 batch 都调用一次，等于每次提交摘要都重建全库 FTS 索引。

要求：
1. 将 FTS 维护改为按 `node_id` 的定向 delete + insert（FTS5 外部内容表或手动同步均可，选实现简单且正确的）。
2. `submit()` 只更新本 batch 涉及节点的 FTS 行。
3. `sync()` 只更新本次变更涉及节点（新增/删除/骨架变更/语义失效）的 FTS 行。
4. 全量 `refresh_search` 保留为显式的 `blueprint reindex` 修复命令，不在任何常规路径中自动触发。

### A2. 调用边子集重算

现状：`_resolve()` 每次 sync 都 `DELETE FROM edges` 后全项目重建全部调用边。

要求：
1. 只重算受影响子集：(a) 变更文件内所有 site 的出边；(b) 全局中"目标名字命中变更符号集合（新增/删除/改名的符号名）"的 sites 的边。
2. 名字→候选符号的映射维护为持久索引或加载一次的内存索引，禁止循环内逐 site 查库（同时修掉现有 `_resolve` 里逐 call site 查 enclosing symbol qualified_name 的 N+1 查询——nodes 已全量在内存）。
3. 边重算引起的"调用解析结果变化 → 相关节点标 suspect"的既有传播逻辑必须保持行为不变，用测试锁住。

### A3. document 瘦身：消除源码三副本

现状：`files.document` 存整文件 text、`nodes.document` 每符号存 `source_text`、`store.save_structure` 又写一份旧缓存，骨架库体积约为源码 3 倍以上。

要求：
1. `nodes.document` 删除 `source_text`，只保留 `start_byte/end_byte/body_sha` 等区间与哈希字段；读取源码一律走现有 `_disk_node` 的"按字节区间从磁盘现读 + 校验 hash"路径。
2. `files.document` 不再存全文 text，只保留 path、file_sha、size、语言等元数据。
3. 评估 `save_structure` 旧缓存写入是否仍必要；若仅为旧路径兼容，改为可选开关且默认关闭。
4. 提供一次性迁移逻辑（或明确声明需要重建库），保证已有库升级路径清晰。

### A4. 次要修复（在不显著扩大改动面的前提下顺带完成）

1. `claim()`：把逐节点磁盘读取与 hash 校验移到写事务外，事务内只做租约状态翻转 + 提交前 version 复验，缩短 `BEGIN IMMEDIATE` 写锁持有时间。
2. `sync()` 拆为四个可单测阶段函数：reconcile（哈希对账）、rebind（rename/身份保持）、invalidate（失效计算与传播）、enqueue（任务入队 + FTS 定向更新）。对外行为不变，事务边界可保持单事务。
3. `repo_map`：入度改为物化 degree 列（边变更时增量维护），去掉逐节点相关子查询；路径前缀过滤改为可走索引的形式（如 path 规范化 + `>= prefix AND < prefix||x'ff'` 或独立目录表）。
4. 在文档中如实记录已知精度损失：edges 主键含 `site_line`，同一行内多次调用同一目标会被合并。

### 阶段 A 验收

- 全部既有测试通过；A1/A2/A3 各配新增回归测试（至少覆盖：submit 后 FTS 可检索且未触发全量重建；sync 后边子集正确性等价于全量重算结果；瘦身后 `_disk_node` 读取与 hash 校验路径正确、hash 不匹配时的失败行为不变）。
- A2 需要一个"子集重算 vs 全量重算结果等价"的差分测试：随机改动若干文件后，两种方式产出的 edges 集合必须一致。

## 阶段 B：10 万行级基准（必须另起全新 session，使用 gpt-luna 模型）

**执行方式要求**：阶段 A 完成并提交后，另起一个全新的 Codex session，并将模型切换为 **gpt-luna**（干净上下文），只带本任务书阶段 B 部分 + 仓库路径进场。基准结论必须由新 session 独立产出，禁止复用阶段 A session 的记忆或中间判断。若当前环境无法切换到 gpt-luna 模型，停下并告知用户，不要用其他模型静默替代。

### B1. 基准环境与口径

1. 目标仓库：mypy（clone 方式见上文"背景"）。基准前记录：HEAD commit、总文件数、总行数（cloc 或等价工具）、语言构成。若 cloc 实测 Python 行数明显偏离 10 万行级（<6 万或 >20 万），按 B2 末尾规则按比例调整门禁并写明依据。
2. 每项指标至少跑 3 次取中位数，记录冷/热（首次 vs 重复）差异。
3. 记录骨架库文件体积，与源码体积对比给出比值。

### B2. 必测项目与门禁

| 项目 | 操作 | 门禁 |
|---|---|---|
| 全量构建 | `blueprint init` 基准仓库全仓 | 10 万行级 ≤ 5 分钟；无错误退出 |
| 库体积 | init 后 db 文件大小 | ≤ 源码体积的 1.2 倍（A3 生效的直接证据） |
| 增量小改 | 改 1 个文件（改 1 个函数体）后 `sync` | ≤ 5 秒；失效节点数与预期一致 |
| 增量中改 | 改 20 个文件后 `sync` | ≤ 30 秒 |
| 无关改动 | 改 1 个文件的注释/空白（cosmetic）后 `sync` | 语义层零失效（static_revalidation 生效） |
| 语义提交 | 连续 submit 10 个 batch | 每次 submit 耗时不随库大小线性增长（对比首尾 batch 耗时，差异 ≤ 2 倍；A1 生效的直接证据） |
| 符号查询 | `find_symbol` 精确 + 模糊各 20 次 | 中位数 ≤ 100ms |
| 调用链 | `callers`/`callees` depth=2，取入度 top 10 符号 | 中位数 ≤ 200ms |
| 全文检索 | `search` 10 个查询词 | 中位数 ≤ 200ms |
| repo_map | 仓库根 + 3 个子目录 | 每次 ≤ 1s（A4.3 生效的证据） |
| 并发混压 | 1 个 sync + 2 个并发 submit + 持续查询 | 无 database is locked 失败；查询 p95 ≤ 500ms |

门禁数值若明显不合理（如基准仓库实际是 30 万行），按比例调整并在报告中写明调整依据，不得静默放宽。

### B3. 交付物

1. `BENCH_100K.md`：环境口径、每项指标的原始数据（3 次全记录）与中位数、门禁 PASS/FAIL 判定、失败项的初步归因。
2. 基准脚本入库（可重复执行，禁止一次性手工操作出数）。
3. 更新 `SKELETON_ACCEPTANCE.md`：把"已验证范围"从万行级推进到 10 万行级，如实记录仍未覆盖的部分（百万行、多语言混合等）。

### B4. 纪律

- FAIL 即如实记录，禁止调整判据凑 PASS；每个 FAIL 给出归因假设即可，是否修复由用户决定。
- 基准过程中发现的新问题（非本次改造引入）单独列出，不混入门禁判定。
- 所有结论必须附可复核证据：命令、输出、时间戳。

## 执行顺序

1. 阶段 A：A1 → A2 → A3 → A4，每步跑全量测试后再进下一步，分步提交。
2. 阶段 A 全部完成后停下，向用户汇报改动摘要。
3. 用户确认后，另起新 session（模型切换为 gpt-luna）执行阶段 B。
