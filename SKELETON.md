# 持久骨架与语义层

`python -m codemap init REPO --budget 0`（插件内为 `scripts/run.py init`）扫描全仓库并持久化全部可解析符号，不启动模型。首次创建后立刻可通过 MCP 查询。现有工程可运行 `python -m codemap sync MAP --budget 20000` 升级或更新骨架。`python -m codemap doctor MAP` 检查外键、边端点、语义与任务哈希、FTS 一致性；有问题时退出码为 1。

## 命令与接口

- `init REPO [--output MAP] [--exclude GLOB] [--include GLOB] [--langs python,cpp] [--budget N]`：新建或重开同仓库的骨架。include/langs 仅筛选骨架层，exclude 是原有全仓库清单排除策略；既有工程的筛选配置由 sync 保留。
- `sync MAP [--budget N]`：全树哈希对账，只解析新增、内容或解析器版本变化的文件。默认预算为 0，每次显式调用为新一轮预算；已运行租约的预留属于此前一轮。
- `blueprint_find_symbol(project, name, fuzzy?, kind?, limit?)`：名称、限定名或精确 ID。重名时使用返回的 ID。
- `blueprint_callers` / `blueprint_callees(project, symbol, depth=1, limit=100)`：最多三跳，循环去重，每条边附带 confidence。目标不唯一时拒绝猜测。
- `blueprint_search(project, query, limit=100)`：FTS5 检索限定名、签名、最新摘要。查询按字面词组处理，不执行用户提供的 FTS 运算符。保留 `paths="*"` 参数时走原来的源码字面搜索；CLI `search` 仍是源码搜索。
- `blueprint_repo_map(project, path?, budget_tokens=2000)`：按调用入度选择路径、行号与关键签名。使用 UTF-8 字节数作为保守 token 上界，可能少用预算；返回 truncated。
- `blueprint_sync(project, budget=0)` / `blueprint_doctor(project)`：MCP 对应同步和检查。

五个查询工具直接读关系表，不领取任务、不执行代码、不读取整图 JSON。所有返回的摘要都嵌套在 `semantics` 内并带 `status`；尚未生成则为 null。摘要全文检索可能命中 stale/suspect 记录，调用者必须检查状态。查询反映最后一次同步的快照；外部文件修改后应先 sync。

## 可复现的机器事实

SQLite WAL 中新增 `files`、`nodes`、`edges`、`semantics` 和 FTS5 `search`，与原有图 JSON 共存。声明保存 UTF-8 解析输入的精确字节区间和 SHA-256；包含签名、装饰器与函数体，避免默认参数变化漏检。原文件指纹另存 `files.sha256`，适用于 BOM/UTF-16 的磁盘对账。时间字段在骨架表中采用内容版本键，避免墙钟时间污染确定性数据。

同一快照的核心四表（files/nodes/edges/search）的规范化有序内容可逐字节比较；SQLite 文件本身及预算、租约、历史记录不承诺字节相同。精确重命名沿用历史符号身份，因此从更名后的目录重新新建工程与保留更名前历史的工程，ID 可以不同。

调用解析采用词法范围、同文件限定名、全项目唯一名称候选。多候选保留 ambiguous，无候选保留 name 占位与 unresolved。`resolved_local` 是语法名称匹配，不是类型检查证明。对象属性、动态分派、宏和跨文件别名可能保持未解析；import 原文保留为 unresolved 边。未安装解析器、二进制、超过现有 2 MiB 上限的源码会明确记录 unavailable；语法恢复标 partial。

原生解析继续复用独立进程及单文件 15 秒超时，并发调度最多四个文件；Python AST 复用标准库。缓存同样提供给旧画布及阅读包，但语义完成状态不会被静态解析改变。

## 增量与失效

文件哈希和解析器版本是对账基准，commit_id 记录当前 HEAD（非 Git 目录为 null）。采用全树哈希核对而非仅依赖提交差异，覆盖未提交、未跟踪和被 Git 忽略但仍纳入清单的源码。该策略需要 O(文件内容) 的扫描；只有变化文件进入解析器。名字解析和 FTS 在变更同步中重新核对全项目，尚未实现编译器增量索引或子树复用。

- 未改动的声明保持原语义和任务。
- 实现变化：对应节点的摘要 stale，排队重读。
- 签名、装饰器、默认参数或导入变化：直接调用者 suspect，只传播一跳；导入变化还使本文件语义 stale。
- 格式、注释和行号变化：通过等价 AST/语法叶节点证明后保留语义有效，更新 hash 和证据范围，在 detail.static_revalidation 留下机器复核依据。此前租约失效，避免提交旧位置。
- 精确文件移动：沿用旧 file/node 身份。变更函数的外层类或包含函数也可能 stale，因为它们的字节范围包含该函数；这是包含关系，不是递归调用传播。
- 删除符号：移除其任务及挂接的语义，原有调用者按契约变化进行一跳复核；原有图历史不受影响。

## 任意宿主的语义闭环

`Executor` 定义 `claim(n)` 和 `submit(batch)`；`HostExecutor` 是可直接使用的实现，`CodexExecutor` 采用同一个协议，不解析 Codex CLI 私有输出。MCP 提供对应的 `blueprint_semantic_claim` / `blueprint_semantic_submit`，宿主实际阅读源码并生成内容；工具不启动模型。

1. 先用 sync 的 budget 开启本轮预算。
2. `semantic_claim(project, worker, n=1, lease_seconds=1800)` 按入度优先返回任务，包括 node_id、body_sha、lease_id、准确源码切片、签名、位置、调用上下文和 estimated_tokens。suspect 在同入度下优先级较低。
3. 阅读后提交下列 batch；证据必须引用任务节点、哈希及节点内的行范围。

```json
{
  "batch_id": "host-generated-unique-id",
  "results": [{
    "node_id": "symbol:...", "body_sha": "...", "lease_id": "...",
    "summary": "实际阅读后得到的职责摘要",
    "detail": {"role": "实际设计角色"},
    "evidence": [{"node_id": "symbol:...", "body_sha": "...", "start_line": 10, "end_line": 20}],
    "model": "实际宿主/模型", "used_tokens": 500
  }]
}
```

预算在领取时按源码长度和输出预留估算；宿主必须把实际使用量限制在任务预留内并准确报告，服务端拒绝超额声明，不能监控外部模型自身的计费。成功提交退回未用预留；超时不退回，因为模型可能已经消费。租约可被重新领取，旧租约拒绝提交。整批事务提交，hash、磁盘指纹、租约、证据、预算任一失败都不写入。重复 batch_id 与相同内容幂等，不同内容拒绝。

为保持旧协议兼容，节点语义任务使用关系表 `semantic_tasks` 与 `semantic_receipts`，沿用 claim/lease/batch 的约定；不把新节点硬塞入旧整图任务的 entity/scope 校验。原来的 next/prepare/commit 继续负责完整蓝图阅读、数据流和证据图。两类完成状态分别显示；节点摘要不冒充全仓库语义审阅完成。骨架 sync 不自动修改旧图快照；原有完整审阅流程仍使用 update 的预览/应用。

## 验收

可复现命令与实测结果见 [SKELETON_ACCEPTANCE.md](SKELETON_ACCEPTANCE.md)。设计来源见 [SKELETON_PLAN.md](SKELETON_PLAN.md)。百万行目标尚待独立规模测试。
