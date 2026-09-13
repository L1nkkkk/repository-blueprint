# 代码蓝图：安装与 Agent 调用

这是独立的本地代码蓝图工具，包含通用阅读 Skill、标准 stdio MCP 工具和无限画布。Agent 在自己的宿主中使用模型和账号，通过 MCP 保存同一份图谱。支持生成 Claude Code、Gemini CLI 和通用 MCP 配置；Codex 插件与本机后台执行是可选入口。

## 通用 MCP 接入

需要 Python 3.10+，无需 Codex。基础工具和 Python 结构解析使用标准库；其余四种目标语言先运行 `python scripts/setup_parsers.py` 安装解析依赖。源码目录和通用发行包都提供 `scripts/run.py`。在工具目录运行下面任一命令；如果 Python 不在系统路径上，用它的完整路径启动：

```text
python scripts/run.py connect --client claude-code
python scripts/run.py connect --client gemini-cli
python scripts/run.py connect --client generic
```

命令只输出本机配置，不修改宿主设置。将输出中 `mcpServers.repository-blueprint` 合并到已有设置，保留其他服务。配置使用 Python 与工具脚本的绝对路径，之后可从任意仓库启动宿主。

更新工具后重连 MCP，使用 `blueprint_guide(topic="preparation")` 获取简明返回、已有结论复用和准备批次提交的用法。现有地图可以继续使用；无需重建。保存的准备批次跨连接保留，提交时仍检查租约、版本和源码；未保存的阅读会话不会转移到另一个 Agent。

| 宿主 | 配置位置 | 说明 |
| --- | --- | --- |
| Claude Code | 目标仓库根目录的 `.mcp.json` | 项目级 stdio 配置，重新加载后按宿主提示启用 |
| Gemini CLI | `~/.gemini/settings.json` 或目标仓库的 `.gemini/settings.json` | 合并顶层 `mcpServers`，按宿主提示重新加载 |
| 其他 MCP Agent | 宿主的本地 MCP 设置 | 使用 `command`、`args`、`env`；外层格式不同时只复制服务定义 |

配置格式参照 [Claude Code 官方 MCP 文档](https://code.claude.com/docs/en/mcp) 和 [Gemini CLI 官方 MCP 文档](https://geminicli.com/docs/tools/mcp-server/)。模板已通过本地 stdio 集成测试，尚未在实际 Claude Code / Gemini CLI 模型会话中完成阅读验收。

接入后向 Agent 说明仓库或已有地图的绝对路径，例如：

> 使用 repository-blueprint MCP，先读 blueprint_guide 的 workflow。继续这份已有地图：填写地图工程绝对路径。核对源码版本，查询已保存成果，再分批完成剩余阅读，保存依据并打开画布。

`workflow` 直接提供共同 Skill，不需要宿主支持 Skill 自动发现。首次分析则提供仓库绝对路径，让 Agent 用 `blueprint_init` 建立地图。Agent 实际调用工具后，才有读取和提交成果。

画布“Agent → 外部 MCP Agent”可以复制接入配置，并为当前节点、问题或全仓库生成接续说明。复制不会启动 Agent，也不把外部会话记成本机后台请求。

## 通用发行包

在源码工具目录打包：

```text
python scripts/build_portable.py --output work/portable/repository-blueprint --zip work/repository-blueprint-portable.zip
```

通用包包含运行代码、画布、Skill、协议文档和三个接入配置，不含 Codex 插件清单、Codex 安装器、Python、账号配置或分析工程。解压后保留整个 `repository-blueprint` 目录，使用本机 Python 重新运行包内的 `scripts/run.py connect --client ...`，生成适用于新位置的路径。打包时生成的 `connections/*.json` 仅对当时的输出位置与本机 Python 有效。

可在原输出目录使用 `--update` 更新同类包；不能在 Codex 插件目录上直接覆盖为通用包。压缩包使用新的输出文件名，已有压缩包不会被覆盖。

## Codex 可选安装

当前安装流程面向 Windows、本地 Codex CLI 和 Python 3.10+。安装程序优先使用 Codex 附带的 Python，并安装固定版本的本地语法解析包。

在本项目根目录或发行包解压后的 `repository-blueprint` 目录运行：

```powershell
./install.ps1
```

如运行环境不在默认位置，使用 `./install.ps1 -Python 'Python完整路径' -Codex 'Codex完整路径'`。安装程序使用 Codex 自带 Plugin Creator 辅助脚本注册个人插件；脚本缺失时会报告原因。

插件副本位于用户目录的 `plugins/repository-blueprint`，个人市场记录位于 `.agents/plugins/marketplace.json`。代码、画布和引用文档一起复制，安装后不依赖原项目目录。MCP 配置使用本机 Python 和这个插件副本的绝对路径，使用期间请保留该副本；在另一台机器上解压发行包后重新运行安装程序生成当地路径。

安装后在 Codex **新任务** 中选择“代码蓝图 / repository-blueprint”。新安装的 MCP 工具和 Skill 不会补入已经开始的任务。可以要求：

> 使用代码蓝图梳理这个仓库：填写仓库绝对路径。覆盖全仓库，分批阅读，保存源码证据、调用关系与数据流，并打开画布。

没有 Skill/MCP 工具时不要声称已调用插件；检查个人市场中的安装状态及 MCP 启动错误。

## 保存和继续

默认工程位于目标仓库的 `.codemap`，其中 `map.sqlite` 保存分析、任务和布局。也可以指定另一个绝对输出路径。已有工程不会被初始化覆盖。

后续任务中提供工程绝对路径，要求继续或打开即可。MCP 返回画布地址，Agent 使用宿主的浏览器打开。画布服务的寿命与该 MCP 连接一致；连接结束后再次调用即可启动新地址，已保存数据保留。

也可以独立启动已安装的画布，下面的 `MAP` 替换为工程绝对路径：

```powershell
& 'Python完整路径' "$env:USERPROFILE/plugins/repository-blueprint/scripts/run.py" serve 'MAP' --port 8765
```

`init` 只建立清单，`next` 只领取任务；真正的分析需要 Agent 阅读并提交。已有示例和 `review_sample.py` 固定重放不能代表自动分析了另一个仓库。

仓库代码修改后可在画布右上角“检查代码变化”中查看影响范围并登记新版本，再在“Agent”中继续全仓库梳理；也可让当前会话使用 `blueprint_update` 后继续领取任务。旧图谱、无关成果和阅读布局保留。变化预览包含分类理由及移动关联；“历史”提供版本对比和图谱恢复。

## 从画布使用本机 Codex 后台

在“Agent”中选择“本机 Codex 后台”，启用以下执行方式。外部 MCP Agent 的分析在其宿主中启动和停止。

选中节点后点击“让 Agent 回答”或“继续拆解”；顶部“Agent”可以继续全仓库队列，并显示真实工具执行记录和回答。请求写入工程后由独立阅读进程处理，关闭当前聊天或画布服务不会取消已经启动的阅读。页面重开后可以查看、暂停、继续或取消。已提交的图谱保留，取消不会撤销成果。

需要本机 Codex CLI 已登录。程序优先查找 `CODEMAP_CODEX`、系统路径和 Windows Codex 应用目录；找不到时保留请求并显示连接说明。使用自定义位置时，在启动画布前设置环境变量 `CODEMAP_CODEX` 为可执行文件完整路径。安装参数 `-Codex` 只用于安装流程，不会改写机器环境变量。Windows 创建隐藏进程；后台使用本机模型及推理设置，不另外索取 API Key。仅继承模型与推理强度，尚未适配用户自定义模型提供商和配置档案。

阅读器仅连接当前地图的受限 MCP：可读取、搜索、查询；分析请求可以领取自己的任务并提交校验后的批次。它不运行目标仓库程序、不修改源码、不加载其他应用连接。机器休眠会暂停实际执行；进程失联时保留成果并提示继续，不显示虚假运行进度。更多操作见 [工作台指南](WORKBENCH.md)。

## 其他 Agent

新版 MCP 自动记录通用审阅耗时，画布入口为“Agent → 审阅耗时”，也可调用 `blueprint_metrics` 查询。正在运行的 WorkBuddy 或其他 Agent 应先提交完当前批次，再切换新版包并重连；旧进程不能热补统计。换连接继续使用原地图目录，无需重新初始化。统计口径、导出与对比条件见 [工作台指南](WORKBENCH.md#审阅耗时与基线)。

模块阅读包通过通用 `blueprint_pack` 提供；`blueprint_next` 默认附带第一页。Agent 按返回的 `next_cursor` 继续，不必重复查询包里完整的记录或重读已提供的源码行。新接口无需额外安装依赖；更新包后重连 MCP，Codex 则在新任务中加载更新后的工具。

工具基于本地 stdio MCP，command 为本机 Python，args 为 `-u`、包内 `scripts/run.py` 的绝对路径、`mcp`。宿主可通过 `blueprint_guide(topic="workflow")` 读取规范。换 Agent 后复用原地图路径，查询已有证据和待办；每个阅读者使用自己的 worker 身份，遵守已有租约。图谱与已提交成果跨宿主共享，未提交的思考过程和聊天记录不共享。这不是远程 HTTP MCP。

## 更新与卸载

修改工具后再次运行本项目的 `install.ps1`。安装程序通过官方辅助脚本更新版本缓存标记，再重新安装个人插件。开启新任务使用更新后的工具。

更新从源码项目或新解压的发行包运行，不在个人市场中的已安装副本内运行安装脚本。发行包包含运行代码和安装脚本，不包含 Python 或任何待分析仓库。

在 Codex 插件管理页卸载，或运行 `codex plugin remove repository-blueprint@personal`（个人市场改过名称时使用实际名称）。卸载不会删除仓库里的分析工程。个人市场中的源副本仍可用于重新安装。

## 当前边界

包含整个仓库的清单和可保存阅读流程，不意味着所有语言语义或 UE 规模已经验证。大仓库分片、五种语言完整样本和性能验收仍在后续计划中。增量影响目前以 Python 函数体分析及保守回退为基础；检查器验证结构、指纹和证据位置，语义准确性仍需 Agent 阅读与复核。

MCP 实现依据 [stdio 传输](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports)、[连接生命周期](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle) 和 [工具协议](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)。插件使用 Codex Plugin Creator 当前支持的兼容格式。
# 本地结构解析依赖

Python 结构解析使用标准库。C++、C#、TS/TSX、JS/JSX 使用固定版本的 Tree-sitter 语法包。Codex 安装脚本会安装这些依赖；通用包解压或迁移到另一台机器后，用运行 MCP 的同一个 Python 执行 `python scripts/setup_parsers.py`，再执行 `python scripts/setup_parsers.py --check` 检查。需要可访问 PyPI；不支持预编译包的平台会明确失败，不自动编译目标仓库。

依赖缺失时，工具的其他功能与 Python 解析仍可使用，相关语言显示解析缺口。解析操作本身不联网、不执行目标仓库代码。MCP 配置仍使用该 Python 的绝对路径。
