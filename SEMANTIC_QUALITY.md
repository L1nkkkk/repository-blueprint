# C1 语义摘要质量抽检

固定随机种子：20260914。抽取自 `bench_stage_c/SEMANTIC_COST.raw.json` 的 500 条真实 submit 记录，随机抽 20 条。摘要生成器是允许使用的本地 stub；因此本抽检同时验证“是否覆盖源码关键行为”，而不是把 stub 当成真实 LLM。

| # | symbol/path | 对照结果 | 错误类型 |
|---:|---|---|---|
| 1 | `59caab863a3a0d94f43d3cb5` / multiprocessing/sharedctypes.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 2 | `e20e00891b99608f22951464` / multiprocessing/sharedctypes.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 3 | `5ac17f0cf086d14643b65de7` / mypy/semanal.py | 源码有实现，摘要没有处理逻辑 | 遗漏关键行为 |
| 4 | `59ffb5f0bfbafeb5bc030557` / subprocess.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 5 | `e1a843225c845786f36e9ccf` / random.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 6 | `598a995c72d1badefd36c682` / math/__init__.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 7 | `5a85bebf55e68c3081a60600` / urllib/request.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 8 | `e210d1ad2c0b74ba81d78ffd` / mypyc/irbuild/builder.py | 源码有实现，摘要没有状态变化/返回条件 | 遗漏关键行为 |
| 9 | `e289e38e021b0e4d6589daf0` / symtable.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 10 | `00173ad9bbc1cdc312680ba8` / mypy/solve.py | 源码有实现，摘要没有求解策略/失败条件 | 遗漏关键行为 |
| 11 | `e20fd722da70f81e487ee3af` / builtins.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 12 | `598b66d2c8e10caccfb1a037` / _contextvars.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 13 | `e211d158857cae22ae26bfb9` / fixtures/len.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 14 | `e20f7d1509670afd4a0af4f3` / importlib/metadata/__init__.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 15 | `5acf5d6663b6aea003bc120a` / os/__init__.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 16 | `0b9a4904574e541465e98957` / librt_strings.c | C 源码未被 stub 摘要解释 | 遗漏关键行为 |
| 17 | `e2754b1bcd5ca6a8e92b5ba4` / asyncio/base_events.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 18 | `e2a0f5d32bc29ed404b0e1de` / encodings/cp864.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 19 | `5a5791ca9490beb61a0b1e8` / fixtures/alias.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |
| 20 | `e1bb1ba15351d74447352837` / token.pyi | 签名可对应，未提供行为说明 | 遗漏关键行为 |

结论：20/20 均属于“遗漏关键行为/摘要信息量不足”，0 条张冠李戴，0 条可归类为幻觉。该结果是 stub 摘要器的质量结果，不改变 C1 性能成本门禁；真实 LLM 摘要质量仍未覆盖。
