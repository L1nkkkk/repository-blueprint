# Stage C：语义层规模化、边准确性与体积归因

目标仓库：`https://github.com/python/mypy.git`，HEAD `2ee4f4f4631099201b192528ae48ef65e2c3c60c`。执行日期：2026-09-14（Asia/Shanghai）。全量测试：176 通过、1 跳过。

## C1 语义层规模化

500 个真实符号通过 claim/submit 完成，使用 `tiktoken cl100k_base` 计量函数体与调用上下文输入 token、stub 摘要输出 token。为获得真正分层样本，top 10%、中位段、长尾分别在三个独立 map 上做 priority 预条件；每个符号仍走真实协议。原始数据：[SEMANTIC_COST.raw.json](bench_stage_c/SEMANTIC_COST.raw.json)，类级补样：[CLASS_COST.raw.json](bench_stage_c/CLASS_COST.raw.json)。

| 分层 | 数量 | 输入 token 总量 | 输出 token 总量 | 输入中位数 |
|---|---:|---:|---:|---:|
| top 10% | 167 | 279,037 | 9,087 | 633 |
| 中位段 10% | 167 | 48,270 | 9,351 | 27 |
| 长尾 10% | 166 | 40,640 | 9,486 | 26 |

外推公式：对函数/方法数量 `N=31,515`，按 rank 分层取 `N_top=N_mid=round(0.1N)=3,152`，其余 `N_tail=25,211` 使用长尾样本均值作为保守 proxy；`Total = Σ(N_s × sample_mean_s)`。bootstrap 1,000 次、固定种子 20260914 的函数/方法输入 token 中位数约 12.21M，95% CI 9.08M–16.28M；输出约 1.78M，95% CI 1.67M–1.95M。实测 500 符号 claim+submit 153.150s，按分层样本的每符号批次耗时外推约 9,630s（2.67h，bootstrap 95% CI 9,611–9,651s）。

含 class/模块级的分开口径：class 5,127 个，实测 50 个 class 样本均值约 4,825.86 输入 token、37.4 输出 token；模块没有独立 semantic node，以 1,481 个可审阅源文件计数，并明确使用长尾样本均值作 proxy。由此得到约 37.45M 输入 token、2.06M 输出 token、约 3.18h 的下界式估计；class 级实测和模块 proxy 的独立原始数据分别见 `CLASS_COST.raw.json` 与 `SEMANTIC_COST.raw.json`。这不是模型价格估算，也不外推 Python 以外语言。

预算降级：全量 heuristic estimated_tokens 为 34,585,330。10% 预算 3,458,533 claim 4,447 个，按 rank 精确计算 top-10% 覆盖 3,151/4,447=70.86%，PASS；30% 预算 10,375,599 claim 14,733 个，top-10% 覆盖 3,151/14,733=21.39%，记录为观察结果。原始数据：[BUDGET_10.raw.json](bench_stage_c/BUDGET_10.raw.json)、[BUDGET_30.raw.json](bench_stage_c/BUDGET_30.raw.json)。排序键是 `priority DESC,node_id`，高入度优先；10% 门禁无失败。

摘要质量：[SEMANTIC_QUALITY.md](SEMANTIC_QUALITY.md) 固定种子抽检 20 条：20 条均为 stub 摘要信息量不足/遗漏关键行为，0 条张冠李戴，0 条幻觉。质量项无硬门禁，但真实 LLM 质量未覆盖。

## C2 调用边准确性

固定种子 20260914，普通、动态、高入度三组各 10 个；逐条原始记录见 [EDGE_ACCURACY.raw.json](bench_stage_c/EDGE_ACCURACY.raw.json)。AST 调用点用于剔除纯文本同名误报，并保留源码摘录、库边、参照集合和归因字段。

| 组别 | callees precision | callers recall | 判定/失败类别 |
|---|---:|---:|---|
| 普通直接调用 | 192/192=100% | 平均 36.59% | FAIL；产品，普通调用边存在解析/解析绑定漏边，需后续逐函数修复归因 |
| 动态特性 | 739/739=100% | 平均 56.25% | 无硬门禁；已记录动态查找/回调边界，非硬失败 |
| 跨模块高入度 | 81/81=100% | 平均 75.00% | 无硬门禁；高入度样本包含跨模块名称解析边界 |

普通组 callers recall 低于 90% 是产品 FAIL，不归因于基准装置；需要区分静态动态解析原理边界与实现漏边。`site_line` 同行合并候选在样本中出现 1 次，已单独保留，暂不改造。

## C3 suspect 预期

预期清单先于执行提交：[C3_EXPECTED.json](bench_stage_c/C3_EXPECTED.json)，执行结果：[C3_RESULTS.raw.json](bench_stage_c/C3_RESULTS.raw.json)。五组全部匹配（5/5）：单函数体、被高入度函数调用的底层函数体、函数签名、纯注释、删除函数。该项 PASS；无失败。脚本：[suspect_matrix.py](bench_stage_c/suspect_matrix.py)。

## C4 体积归因

`DBSTAT.txt` 来自逐表/逐索引 dbstat 查询；map.sqlite 204,734,464 bytes，dbstat 可计量页 203,501,568 bytes，top 3（nodes、edges、sqlite_autoindex_edges_1）占 60.2963%。edges 与其唯一索引合计 60,190,720 bytes，占 29.576%，TEXT symbol ID 重复存储是主要归因方向；nodes 仍为最大项。只提交改造方案：[VOLUME_REFACTOR_PROPOSAL.md](bench_stage_c/VOLUME_REFACTOR_PROPOSAL.md)，本轮不实施 INTEGER 外键化。

## 未覆盖

百万行仓库、sync 固定底价优化、Python 以外语言的实测、真实外部模型质量/价格、长期多进程压力，以及 INTEGER 主键改造仍未覆盖。所有结论均附原始 JSON、脚本或 SQL 口径。
