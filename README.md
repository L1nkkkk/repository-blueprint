# Repository Blueprint · 代码蓝图

Local, evidence-backed code maps for MCP agents. Explore repository structure, call contexts, and data flows on an interactive canvas.

用 AI 逐模块阅读整个仓库，整理成可分层探索、追踪数据并核对源码的蓝图画布。

这是独立的本地工具：不同 Agent 按同一份阅读规范，通过标准 stdio MCP 保存文件清单、阅读队列、源码依据和图谱。Claude Code、Gemini CLI 等宿主可直接接入，规范也可通过 `blueprint_guide(topic="workflow")` 读取，无需安装 Codex。Codex 插件和画布中的本机 Codex 后台执行是可选入口。

## 从 GitHub 获取

```text
git clone https://github.com/L1nkkkk/repository-blueprint.git
cd repository-blueprint
```

需要 Python 3.10+。下面的样例画布可在没有 AI 账号的情况下运行；分析自己的仓库时，再连接支持 MCP 的 Agent。

## 安装给 Agent 使用

通用接入只需要 Python 3.10+ 和支持本地 stdio MCP 的 Agent。在项目根目录运行 `python scripts/run.py connect --client claude-code` 或 `--client gemini-cli`，获取使用本机绝对路径的配置。画布“Agent → 外部 MCP Agent”也可以复制配置及当前节点或整个工程的接续说明。接续不会自动启动 Agent。

运行 `python scripts/build_portable.py --output work/portable/repository-blueprint` 可打包独立的 Skill、16 个 MCP 工具和画布，不需要 Codex 安装器；换位置后重新运行包内的 `scripts/run.py connect` 生成当地配置。Codex 用户仍可用 `./install.ps1` 安装个人插件。具体配置位置、调用和继续已有工程见 [安装与调用指南](INSTALL.md)。

审阅默认采用简明状态和任务返回；`blueprint_context` 核对已有结论的来源是否变化，`blueprint_prepare` 按实际读过的行号补齐 ID、指纹和引用，再按准备批次 ID 提交。全仓库队列、函数细节和源码校验保持不变。用法见 [高效阅读指南](skills/repository-blueprint/references/preparation.md)。这些改动减少传输与重复整理，实际模型耗时还需同条件测量。

领取任务现在默认附带模块阅读包：实际源码、声明结构、关联文件线索、已保存的完整结论和待办一起返回。按 `next_cursor` 用 `blueprint_pack` 续读；也可以指定文件、节点或已有任务主动取包。包内实际返回的完整源码行可直接用于当前 MCP 连接的证据准备，完整有效的旧记录无需重复查询。工具按字符预算分页，超长行、缺失索引和关联范围限制会明确列出。阅读包由本地工具生成，不调用额外模型，也不改变审阅完成状态。详见 [模块阅读包](skills/repository-blueprint/references/reading-pack.md)。

## 打开可运行版本

在本项目目录使用 Python 3.10 或更新版本。基础功能和 Python 结构解析无需额外依赖；C++、C#、TS、JS 结构解析先执行 `python scripts/setup_parsers.py`：

```text
python examples/review_sample.py --output work/sample-canvas
python -m codemap serve work/sample-canvas --port 8765
```

打开程序输出的本地网址。首次命令重放已逐行核对的固定样本成果，校验源码指纹并走实际任务、提交与存储流程；它不是通用代码分析器。工程目录已经存在时直接运行第二条命令。

Windows 可运行 `./launch.ps1`，自动准备同一个样本并启动画布。也可传 `-Repository "仓库目录"` 建立自己的清单，或传 `-MapPath "已有工程目录"` 继续浏览。`-Python` 指定解释器，`-Port` 指定端口。

仓库中的 `examples/reviewed-map*.json` 是不含本机源码位置的静态画布测试样本；上面的样例命令会在你的机器上创建可读取源码的工程。运行地图、审阅日志、账号配置和打包产物不纳入 Git。

顶部“文件 / 详情”可以分别收起、展开左右侧栏；拖动侧栏与画布之间的分隔条调整宽度，双击恢复默认宽度，也可聚焦分隔条用方向键微调。宽度与收起状态保存在当前工程中，窄屏使用浮层。选中节点或连线会打开对应详情。源码支持 C/C++、Python、C#、TypeScript、JavaScript 等语言的语法配色，并跟随明暗主题；原文、行号和依据范围保持一致。配色在本地完成，无需联网。

左侧文件清单单击可选中并定位：清单项与画布对应的文件、函数或折叠模块同步高亮，右侧显示文件详情；双击或点击“展开文件内部”再进入文件。当前层没有对应节点时，会定位到该文件所在目录。选择新的文件会替换高亮，点击画布节点也会同步标记其来源文件。

顶部“Agent”选择外部 MCP Agent 或本机 Codex 后台。外部方式生成接续说明，本机后台方式保存真实请求与回答。选择节点后可“让 Agent 回答”或“继续拆解”，也可继续全仓库剩余队列。“覆盖”分别展示文件阅读、已整理场景、未纳入场景的函数和待确认关系。“历史”比较归档与当前图谱，以及已保存源码文本的差异；恢复会先归档当前版本、保留布局，并按现有源码重新核对过期内容。操作与边界见 [分析工作台指南](WORKBENCH.md)。

“流程阅读”以一个共同入口组织同一任务的多个输入。颜色表示任务及其子流程，输入、计算结果和返回值共用任务颜色，具体数据通过端口名称和版本区分。顶部图例以色点和任务名称紧凑排列，悬停或点击在详情中查看已保存的入口职责。点击图例或彩色连线高亮该任务的线路，也可同时选择多个任务。实际关系用实线曲线与实心箭头表示，箭头保持可读的屏幕尺寸；待核对状态显示在图例计数、连线提示及详情中。点击输入或端口仍能单独追踪数据；“恢复任务全部线路”清除数据聚焦。“查看整条流程”回到根流程，不同请求或场景使用各自的入口与颜色。

“关系核对”保留按调用分层或全部上下文的原始关系入口。点击数据连线查看源码依据并添加可拖动的整理点。画布右上角的“一键整理”重排当前节点、清除当前连线的手动整理点并重新布线，保留锁定节点；“撤销整理”恢复整理前的位置、走线点和视角。选中任务时，整理后视角优先适配选中范围。流程、子流程和核对视图各自保存位置、缩放和整理点。

数据连线上有沿曲线移动的箭头，跟随任务和数据聚焦，仅用于说明传递方向。右上角“暂停流动／开启流动”控制动效并保存偏好；系统设置为减少动态效果时默认关闭。动画不表示代码正在执行，也不表示实际耗时。

“拓扑总览”覆盖全仓库，可按文件夹、文件、类型与作用域、架构模块或源码实体归组，分别查看调用、数据流、控制流和引用依赖。展开组内拓扑可逐步查看成员；折叠的内部关系仍能核对源码。调用环只提示可能递归；未记录控制关系时明确显示尚无控制流依据。

## 使用自己的仓库

```text
python -m codemap init "仓库目录" --output "新的地图工程目录"
python -m codemap status "地图工程目录" --check-snapshot
python -m codemap serve "地图工程目录"
```

初始化后会看到真实目录和文件节点，所有纳入的文件保留阅读待办。已安装的插件提供 [repository-blueprint Skill](skills/repository-blueprint/SKILL.md) 和 MCP 读取/提交工具，Agent 可按指引分批整理。下面保留源码开发环境的命令入口。

```text
python -m codemap next MAP --worker current-reader
python -m codemap read MAP src/example.cpp --start 1 --limit 160
python -m codemap search MAP SymbolName
python -m codemap commit MAP batch.json
python -m codemap export MAP new-result.json
```

暂停、续接、执行有效期维护与批次字段详见 [Skill 命令指南](skills/repository-blueprint/references/commands.md)。源码读取和搜索不会自动增加分析进度。

## 开发和测试

安装语言解析器后运行 Python 测试；画布逻辑测试还需要 Node.js：

```text
python scripts/setup_parsers.py
python -m unittest discover -s tests
node --test tests/canvas.test.cjs tests/topology.test.cjs tests/workflow.test.cjs tests/reader.test.cjs
```

本地开发的历史验证见 [验证记录](VALIDATION.md)。文中 `work/` 路径指开发机上的验收资料，不包含在公开仓库中；可通过上面的命令重新验证当前源码。

## 阅读入口

- [整体设计](DESIGN.md)：全仓库目标与画布交互。
- [完整使用流程](USER_FLOW.md)：启动、浏览、追问、暂停续接与更新。
- [首版交付规格](V1_SCOPE.md)：组件、里程碑、验收范围与后续实施计划。
- [节点视觉规范](NODE_DESIGN.md)：种类、角色、状态与颜色的区分，以及浅色、深色主题。
- [图谱格式](GRAPH_FORMAT.md)：实体、关系、上下文、源码依据和进度记录。
- [任务协议](TASK_PROTOCOL.md)：分批结果、事务提交、重复请求与恢复。
- [Skill 工作规范](SKILL_SPEC.md)：产品层的 AI 阅读与整理约定；[正式 Skill](skills/repository-blueprint/SKILL.md)提供当前可执行用法。
- [验证记录](VALIDATION.md)：协议测试、真实样本阅读与浏览器检查。

## 运行基础验证

参考实现使用 Python 3.10 或更新版本，原生语法解析测试需要先安装 `requirements-parsers.txt` 中的固定依赖。在本项目目录执行：

```text
python -m unittest discover -s tests -v
python -m codemap examples/sample-map.json --source-root examples/sample_repo
python -m codemap examples/reviewed-map-v1.json --source-root examples/sample_repo
```

画布的数据投影、布线、拓扑和流程测试使用 Node.js 18 或更新版本：`node --test tests/canvas.test.cjs tests/topology.test.cjs tests/workflow.test.cjs`。Node.js 仅用于开发测试，启动阅读器仍只需 Python。

`sample-map.json` 是保留诊断模块、测试、工具与关系核对待办的协议样例，验证结果为 `partial`。`reviewed-map-v1.json` 是完整五文件样本的已核对成果，覆盖四个源码文件、构建文件和两次不同的调用位置。两者都不是任意仓库自动分析的证明。

如需重新生成该协议示例：

```text
python examples/build_example.py
```

程序会读取随项目提供的示例文件并生成固定结构的图谱。这是样例生成器，不提供任意仓库的代码理解能力。

## 已实现的基础能力

- 节点种类目录与 JSON 图谱校验，包括端口方向、调用上下文、源码指纹与证据位置。
- 领取任务、处理过期执行身份、分批合并、重复提交识别和完成状态计算。
- 使用 SQLite 事务一起提交图谱与任务状态，单独保存用户布局和颜色。
- 覆盖异常提交、并发领取、恢复和布局保留的基础测试。
- 全目录扫描、语言与角色分类、内容指纹、清单缺口、分段源码读取和文字搜索。
- 本地提取 Python / C++ / C# / TS / JS 的结构、参数和位置；缓存复用、画布结构节点及 `blueprint_index` 查询，Agent 可以用 `symbol_id` 提交职责与依据，免写结构字段。语法结果与语义审阅进度独立；用画布“解析结构”或 CLI `index` 分批覆盖整个清单。
- 真实阅读任务领取、续期、暂停、恢复、阻碍重试和提交前的源码依据核对。
- 读取真实成果的本地画布：层级节点、分调用上下文的端口和关系、任务颜色与用途图例、多选任务、单份数据追踪和源码详情。
- 按调用展开的数据接口与内部边界、保留依据的同值中转合并、全展开核对、依赖布局、障碍绕行与可拖动整理点。
- 全仓库拓扑归组与逐组展开，保留内部关系和跨组数据身份，区分可能递归、控制环、返回与写回。
- 共同流程入口、多输入归组、逐层子流程和输入到派生结果的追踪；灰色归属线与真实数据传递分开显示。
- 可安装的个人插件、stdio MCP 工具及命令入口；已验证 Codex 实际连接、读取、提交和打开画布。
- 拖动、锁定、平移、缩放、小地图和返回原视图；浅色/深色/系统主题与布局保存到工程。
- 原工程内的源码更新：预览修改/新增/删除、按已记录依赖确定影响范围、归档旧图谱、局部重读与过期记录核对。画布右上角“检查代码变化”可打开预览；Agent 使用 `blueprint_update` 后继续领取任务。
- 画布调用本机 Codex 执行真实阅读，持久保存问题、结果及执行记录；支持后台分批接续、暂停、继续与取消。节点上下文仍可复制。
- 分开统计文件阅读、场景整理、场景外函数和未确认关系；文件已读不代表所有运行路径已覆盖。
- 按语法与已记录调用细化影响范围；唯一相同内容的移动自动保留身份，修改内容的移动可明确关联。历史图谱可对比和恢复。

目前数据库保存完整图谱文档，尚无大型图谱分片与性能验证。分析内容的准确性仍需要实际源码阅读和核对，格式校验不会证明语义正确。

## 当前边界

- 通用接入使用本地 stdio MCP，已验证独立发行包、多协议握手、读取提交及跨客户端接续。Claude Code、Gemini CLI 的配置依照各自官方文档生成，尚未在这两个真实宿主中完成模型阅读验收；不提供远程 HTTP MCP。模型与登录由宿主管理。
- 画布后台执行目前适配本机 Codex，使用其模型设置和账号额度；其他 MCP Agent 通过接续说明在自身宿主中执行。外部会话的思考过程、回答和暂停控制不会自动同步到 Codex 请求记录。全仓库性能、会话复用和总耗时预算仍待优化。
- 函数体级影响收窄目前针对 Python；其他语言仅在能保守确认注释/格式变化时收窄，否则按已有依赖重读。未记录的间接依赖仍需 Agent 搜索核对，不是完整多语言语义解析器。
- 历史记录保存图谱和能验证指纹的源码文本；早期版本、超过读取上限或无法解码的来源可能没有文本。恢复只恢复图谱，不能回退仓库代码。重名或相同内容的多份复制不自动猜测移动关系。
- 文件读取支持 UTF-8 与带 BOM 的 UTF-16，上限 2 MiB。超过上限或编码不支持的源码仍是必需待办。链接和 Windows 重解析点不会被跟随，并记录为清单缺口。
- 默认跳过 `.git`、`.hg`、`.svn`、`.codemap` 元数据；`--exclude` 可重复指定显式排除。不会隐式按 `.gitignore` 缩小范围。
- 当前画布加载整图后按层显示，每层最多 150 个节点，文件列表展示前 200 项，可通过搜索收窄。UE、大图分片及五种语言的深度语义能力尚未验证；识别扩展名不等于语义支持。
