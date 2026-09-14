# Stage B：mypy 修补轮验收

验收时间：2026-09-14（Asia/Shanghai）。目标仓库仍为 `https://github.com/python/mypy.git`，HEAD `2ee4f4f4631099201b192528ae48ef65e2c3c60c`。Python/Python stub 合计 265,447 行，时间门禁按 2.65447 倍调整：init 13.27 分钟、单文件 sync 13.27 秒、20 文件 sync 79.63 秒；查询、体积比和语义失效门禁不放宽。

## F1 产品修补

默认 `legacy_cache=False` 时不再预写入 `source_texts`；源码由磁盘区间读取并通过文件/node hash 校验。需要历史 source diff 的兼容路径显式使用 `legacy_cache=True`。全量测试：176 个通过、1 个既有跳过。修补前后 map.sqlite 仍由节点/边/索引主表占据绝大部分空间，体积门禁不是装置故障，仍判定为产品 FAIL。

## 门禁结果

| 门禁 | 修补轮三次原始数据/中位数 | 上轮 | 判定/失败类别 |
|---|---|---|---|
| 全仓 init | 45.897804s / 46.538459s / 46.541137s；中位数 46.538459s | 48.554663s | PASS；无失败 |
| map.sqlite ≤ 源码 1.2× | 203,046,912 / 20,943,439 = 9.695×；WAL 0 | 9.948× | FAIL；产品。F1 去除源码快照后仍由 nodes/edges/index 体积主导 |
| 单函数体 sync | 3.320878s / 3.345332s / 3.278859s；中位数 3.320878s；parsed=1 | 3.224013s；装置口径错误 | PASS；无失败 |
| 20 文件真实函数体改动 | 3.849471s / 3.766064s / 3.761112s；中位数 3.766064s；parsed=19，suspect=3 | 3.780123s；注释追加 | PASS；无失败。19 个实际可解析改动文件，1 个候选无可插入函数体语句，已保留原始数据 |
| 注释/空白改动 static_revalidation | 3.312392s / 3.343592s / 3.278886s；中位数 3.312392s；semantic_before=1，suspect=0 | UNVERIFIED；无 semantic 前置条件 | PASS；无失败。前置条件有效 |
| 连续 10 batch submit | 10/10 accepted；首批 6.9159ms，末批 6.9873ms，0.99×；预算 1,000,000 | 0 次；预算为 0 | PASS；上轮为装置故障，已修复 |
| find_symbol 精确/模糊，各 20 次 | 三轮中位数：精确 1.5225/1.5885/1.550ms；模糊 56.930/57.032/57.571ms | 1.4ms / 56ms，取样未固定 | PASS；无失败 |
| callers/callees depth=2，入度 top 10 | callers 11.094/10.729/10.810ms；callees 3.194/3.188/3.149ms | 非 top-10 取样 | PASS；无失败。口径已修正 |
| search 10 查询词 | 三轮中位数 3.493/3.323/3.323ms | 2ms；样本口径不同 | PASS；无失败 |
| repo_map 根及 3 子目录 | 根 0.337–0.340s；mypy 0.234–0.238s；mypyc 0.058–0.062s；unit 0.014–0.015s | 0.014–0.333s | PASS；无失败 |
| 并发混压：1 sync + 2 submit + 持续查询 | 2 submit accepted；sync 8,180.484ms；查询 4,176 次，p95 2.441ms；errors=[]；database_is_locked=false | 未执行 | PASS；无失败 |

## 原始数据与复核

修补轮完整原始数据：[BENCH_100K.raw.json](bench_fix_runs3/BENCH_100K.raw.json)、[BENCH_CONCURRENT.raw.json](bench_fix_runs3/BENCH_CONCURRENT.raw.json)。可重复脚本：[benchmark_100k.py](scripts/benchmark_100k.py)、[benchmark_concurrent.py](scripts/benchmark_concurrent.py)。

```powershell
$py='C:\Users\Link\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py scripts\benchmark_100k.py bench_mypy --output bench_fix_runs3 --repeats 3 --budget 1000000
& $py scripts\benchmark_concurrent.py bench_fix_runs3\init-3 --output bench_fix_runs3\BENCH_CONCURRENT.raw.json --budget 1000000
```

## 未覆盖范围

百万行仓库、多语言更大规模、sync 固定约 3 秒的全树 hash 对账优化、长期多进程压力、编译器级 C++ 精度仍未覆盖；edges 以 `site_line` 为主键导致同行重复调用合并的问题也保留记录。本轮没有把这些范围外事项混入门禁判定。
