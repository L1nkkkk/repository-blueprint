# 骨架改造验收记录

本页保留 aa113ad 版本的万行级实测数据。后续规模路径阶段 A 的实现与回归见 [SCALE_PROGRESS.md](SCALE_PROGRESS.md)。阶段 B 修补轮已对真实 `python/mypy` 浅克隆完成补测，详见 [BENCH_100K.md](BENCH_100K.md)；Stage C 已完成语义成本采样、边准确性抽样、suspect 预期矩阵和体积归因，详见 [BENCH_STAGE_C.md](BENCH_STAGE_C.md)。


验收日期：2026-09-14。环境：Windows，Python 3.12.14，Node.js 24.14.1，项目 requirements-parsers.txt 中锁定的 Tree-sitter 后端。基于 SKELETON_PLAN.md 的 M1–M4，验证工作区实际实现；性能数字为本机实测，不外推为所有仓库的保证。

## 结果

| 项目 | 验收证据 | 结果 |
|---|---|---|
| M1 持久骨架 | files/nodes/edges/FTS5 落盘；init 完整覆盖超过 100 个文件；精确 UTF-8 声明哈希；sample_repo 和万行真实仓库重复同步 | 通过 |
| M2 查询 | 五个 MCP 查询；词法遮蔽、同名多候选、未知目标、自递归、正反向三跳；每条调用边保留 confidence；真实仓库各查询 80 次 | 通过，P95 全部 < 100 ms |
| M3 增量 | 只解析变化文件；函数实现变更仅该节点 stale；契约变化只一跳 suspect；Python/四种原生语言格式化和文件精确重命名保留身份且旧路径重用不会合并节点；新候选改变解析结果也标 suspect | 通过 |
| M4 语义调度 | 按入度领取、预算预留/结算、并发互斥、租约过期重领、批次幂等、证据校验、整批回滚、脏源码拒绝；任意宿主通过 MCP 完成 claim/submit，摘要可被 FTS 查询 | 通过 |

最终 Python 回归：**167 个用例，166 通过、1 跳过、0 失败**。跳过项为现有 Windows 符号链接权限用例。新增 test_skeleton.py 共 17 个用例，全部通过。现有打包后 stdio MCP 和跨会话用例也通过。

前端回归：**48 个用例全部通过**。此次没有更改前端文件。现有工具数断言从 17 更新到 25；旧协议测试仍验证“未阅读不能完成”，改为匹配现有结构化校验错误，而不是已经过时的文案。

## 实测数据

| 数据集 | 源码行数 | 文件 | 节点 | 边 | 首次构建 | 重复同步 | 重解析文件 | 核心表一致 |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| examples/sample_repo | 35 | 4 | 11 | 17 | 0.2749 s | 0.0503 s | 0 | 是 |
| repository-blueprint 工作区 | 11,340 | 66 | 1,548 | 11,681 | 2.3696 s | 0.1917 s | 0 | 是 |

真实仓库明确排除 work、各级 __pycache__、codemap/web/vendor；64 个文件 parsed，2 个语言无后端的文件 unsupported，没有悄悄计为解析成功。两份数据集 doctor 都通过。

| 查询 P95 | sample（每项 5 次） | 真实仓库（每项 80 次） |
|---|---:|---:|
| find_symbol | 1.7073 ms | 1.9920 ms |
| callers，depth=3 | 2.7060 ms | 3.5888 ms |
| callees，depth=3 | 2.7224 ms | 3.8898 ms |
| search | 1.4752 ms | 5.2366 ms |
| repo_map，budget_tokens=2000 | 1.4155 ms | 19.0222 ms |

一致性比较的是 files/nodes/edges/search 按固定顺序导出的规范化内容 SHA-256，包括同一 HEAD 的 commit_id，不是可能包含 WAL、事务序号、预算和租约的 SQLite 容器文件。调用关系正确性由显式源码夹具断言验证，不声称 C++ 名称近似达到编译器精度。

## 复现

在仓库根目录运行，Python 应已安装 requirements-parsers.txt 中的依赖：

```text
python -m unittest discover -s tests -p "test_*.py"
node --test tests/*.test.cjs
python scripts/benchmark_skeleton.py examples/sample_repo
python scripts/benchmark_skeleton.py . --exclude work --exclude "**/__pycache__" --exclude codemap/web/vendor
```

基准脚本使用临时工程，不修改被测源码；可用 --output 保存 JSON 结果。脚本在一致性、doctor 或 P95 门槛失败时返回非零。使用说明与完整语义 batch 示例见 SKELETON.md；插件内结构参考文档已同步更新。

## 与初稿相比的实现选择和未验证范围

阶段 B 修补轮新增验证范围：真实 mypy HEAD `2ee4f4f4631099201b192528ae48ef65e2c3c60c`，1,481 个蓝图索引源码文件、Python/Python stub 265,447 行；默认关闭 legacy source snapshot、磁盘区间 hash 回退、10 个 semantic submit、带前置 semantic 记录的 static_revalidation、真实函数体增量、top-10 callers/callees，以及 sync + 双 submit + 持续查询并发混压均已取得原始数据。数据库大小仍为 9.695×，判定为产品 FAIL。

Stage C 新增验证范围：500 个真实 semantic claim/submit 的 cl100k_base token 成本、10%/30% 预算降级、20 条摘要质量抽检、固定种子三组各 10 个调用边样本、5 组 suspect 预期矩阵，以及 dbstat 逐表归因。普通组 callers recall 36.59% 为产品 FAIL；C3 五组全部匹配；C4 只提交 INTEGER 外键化方案，未改造实现。百万行、sync 固定底价优化、Python 以外语言实测、真实外部模型质量和 edges 同行调用合并仍未覆盖。完整原始证据见 [BENCH_STAGE_C.md](BENCH_STAGE_C.md)。

1. 全树文件哈希对账覆盖 Git 未提交、未跟踪和被忽略但纳入清单的源码，没有仅按 git diff 跳过磁盘核对。只重解析变化文件，但变更同步会重新核对项目调用解析与 FTS；不是完全 O(变更符号数) 的索引更新。
2. 保留旧整图任务协议与审阅完成规则。新增关系型 semantic_tasks/semantic_receipts 复用 claim/lease/batch 机制，单独服务节点语义，避免破坏现有 entity/scope 校验。CodexExecutor 与其他宿主使用同一接口，未启动模型进行成本实验。
3. 原生解析通过已有隔离进程并发调度；Python 继续使用标准库 AST。保留 2 MiB 单文件上限与可见 unavailable/partial 状态。
4. 纯更名保留历史身份；新建工程不保证复刻另一工程更名前的 ID 历史。格式化等价复核记录旧 hash，证据范围更新为新声明范围。
5. **百万行、长期多进程负载、真实外部模型计费和编译器级 C++ 精度尚未验证**。万行实测不能替代这些结论；它们不属于本次已经通过的量化验收。
