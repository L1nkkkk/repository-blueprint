# Stage B: mypy 十万行级独立基准验收

验收时间：2026-09-14（Asia/Shanghai）。目标仓库仅为 `https://github.com/python/mypy.git`，浅克隆命令为 `git clone --depth 50 https://github.com/python/mypy.git bench_mypy`。HEAD：`2ee4f4f4631099201b192528ae48ef65e2c3c60c`。

## 规模与口径

Git 文件 1,922 个、全仓 632,224 行、原始字节 20,943,439；蓝图索引实际源码文件 1,481 个。Python/Python stub 合计 265,447 行，超过十万行基线，因此时间门禁按 2.65447 倍调整：初始化 13.27 分钟、单文件 sync 13.27 秒、20 文件 sync 79.63 秒。查询门禁、数据库体积比和语义失效要求不按规模放宽。完整语言构成与每次原始样本保存在 [BENCH_100K.raw.json](bench_100k_runs3/BENCH_100K.raw.json)。

## 结果

| 门禁 | 三次原始数据/中位数 | 结果 | 初步归因 |
|---|---|---|---|
| 全仓 init | 48.122483s / 48.554663s / 48.729469s；中位数 48.554663s | PASS | 远低于按规模调整后的 13.27 分钟 |
| map.sqlite ≤ 源码 1.2× | 208,347,136 bytes；源码 20,943,439 bytes；9.948×；WAL 0 | FAIL | 当前持久化图、归档/表/索引体积显著大于源码，需单独做存储压缩归因 |
| 单函数体 sync | 3.302374s / 3.219570s / 3.224013s；中位数 3.224013s；每次 parsed=1 | PASS | 失效节点数 0；本轮无既有语义记录，不能据此证明语义失效传播完整 |
| 20 文件 sync | 3.931507s / 3.758106s / 3.780123s；中位数 3.780123s；每次 parsed=20 | PASS | 未出现全仓重解析 |
| 注释/空白 sync | 3.342938s / 3.226095s / 3.283005s；中位数 3.283005s；suspect=0 | UNVERIFIED | 观察到零 suspect，但该基准图无预置 semantic 记录，static_revalidation 未形成可验证证据 |
| 连续 10 batch submit | claim 第一次 160.0269ms，状态 `budget_exhausted_or_idle`；submit 0 次 | FAIL | 初始化预算为 0，无法取得 10 个可提交 batch；不是静默改门禁 |
| find_symbol 精确/模糊，各 20 次 | 三轮中位数分别为 (1.516, 1.453, 1.3715)ms 与 (56.811, 55.4325, 56.161)ms | PASS | 均低于 100ms |
| callers/callees depth=2，各 20 次 | callers (3.134, 3.100, 2.985)ms；callees (2.950, 2.800, 2.915)ms | PASS | 均低于 200ms |
| search 10 查询词 | 三轮中位数 2.1375ms / 2.1225ms / 2.0045ms | PASS | 均低于 200ms |
| repo_map 根及 3 子目录 | 根 0.327817–0.332799s；mypy 0.229579–0.235185s；mypyc 0.056194–0.058571s；test-data/unit 0.014093–0.014861s | PASS | 均低于 1s |
| 并发混压 | 未执行 | FAIL | 本阶段没有形成可复核的 sync + 2 submit + 持续查询并发原始数据；不得用单线程结果替代 |

## 冷/热差异与复核

初始化三次连续运行从 48.122483s 到 48.729469s，首尾比 1.013×，未见明显冷/热放大。每项原始 query 样本、时间戳生成的 JSON、map 路径和脚本版本见 [bench_100k_runs3](bench_100k_runs3/)；脚本为 [benchmark_100k.py](scripts/benchmark_100k.py) 与 [benchmark_submit.py](scripts/benchmark_submit.py)。

复核命令：

```powershell
$py='C:\Users\Link\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
& $py scripts\benchmark_100k.py bench_mypy --output bench_100k_runs3 --repeats 3
& $py scripts\benchmark_submit.py bench_100k_runs3\init-3 --output bench_100k_runs3\submit.json
```

## 范围与既有问题

本次覆盖 Python/C/C++/stub 等混合全仓索引，且使用真实浅克隆提交；未覆盖百万行仓库、多语言规模更大时的线性趋势、长期多进程负载、真实外部模型调用和编译器级 C++ 语义精度。数据库体积 FAIL、预算为零导致 submit 无法开始、并发混压未执行，均作为独立既有/环境问题记录，不混入已通过门禁的结论。
