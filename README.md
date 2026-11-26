# call-gemini-agy-subscription

在 Codex 中直接使用订阅版 Gemini：GPT 转达你的问题或项目任务，Antigravity CLI 执行，再把 Gemini 的原始回复带回当前聊天。支持连续协作、项目文件读写与命令执行，以及跨项目查询本机保存的 Codex 历史。

这是独立技能。不需要旧版重写技能、`local-codex-context` 插件、独立桥接项目或常驻服务。

## 安装

需要 macOS 或 Linux、Python 3.10+，以及已安装并登录订阅账号的官方 `agy`。将本仓库放到 Codex 技能目录：

```sh
git clone https://github.com/ye-kaifan/call-gemini-agy-subscription.git   ~/.codex/skills/call-gemini-agy-subscription
```

也可解压分发 ZIP，将其中同名目录放到 `~/.codex/skills/`。使用自定义 `CODEX_HOME` 时放到它的 `skills/` 下。本技能保持显式调用，不自动介入普通任务。

## 在 Codex 中使用

```text
$call-gemini-agy-subscription 让 Gemini 看一下当前项目，查查我以前对这个问题的讨论，再给出修改建议。
```

接着说“继续让 Gemini 按方案修改并跑测试”，Codex 就会沿用同一个 Gemini 会话。也可以直接问问题，或要求它重写一段回答，不需要先由 GPT 作答。

## 脚本入口

```sh
python3 scripts/call.py --session /path/to/private-session   --workspace /path/to/project <<'MESSAGE'
查阅项目和相关 Codex 历史，解释测试失败的原因。
MESSAGE

python3 scripts/call.py --session /path/to/private-session <<'MESSAGE'
继续修复并运行测试。
MESSAGE
```

一个专属目录对应一个 Gemini 会话，保存会话 ID、设置和逐轮回执；同一目录禁止并发。新的项目或话题使用新目录。首次纯聊天使用 `--no-project`。不要把会话目录放进公开提交中。

| 参数 | 作用 |
| --- | --- |
| `--session DIR` | 必需；首次创建，后续自动续谈 |
| `--workspace DIR` | 首次默认当前目录；续谈沿用原项目 |
| `--no-project` | 首次无项目聊天 |
| `--model ID` | 本次指定订阅 Gemini 模型 |
| `--execution-mode auto/accept-edits/plan` | 首次默认 `auto`；后续沿用 |
| `--codex-home DIR` | 历史目录；默认 `CODEX_HOME` 或 `~/.codex` |
| `--no-history` | 首次不加载内置历史工具 |
| `--timeout SECONDS` | 整轮时限，默认 600 秒 |

模型选择顺序：本次参数、当前会话已选模型、本地 `model.txt`、agy 当前选择。`model.txt` 可选，不进入 Git 或分发包。调用使用订阅登录并关闭额外 AI 点数。

## 不依赖插件的历史读取

`history.py` 直接只读查询 Codex 的本地 SQLite 索引和历史正文，并提供列表、全文关键词搜索、按 ID 读取及分页。默认包含全部项目、普通和归档会话、子代理等来源。支持旧 JSONL、压缩 JSONL 和新版 SQLite 分页历史。

agy 原生项目 agent 从本次会话目录加载这段代码，以 MCP stdio 传递查询。MCP 是这里的通信协议，不是另一个需要安装的插件；该进程随调用启动和结束。配置仅写入 `--session` 指定目录，再通过 `--add-dir` 加入工作区。项目自身的配置照常生效，本技能运行所需的历史能力全部来自随包代码。

普通 JSONL 和 SQLite 只需 Python 标准库。压缩 JSONL 需要 Python 3.14 的 `compression.zstd`、`zstandard` 模块或 `zstd` 命令之一；缺少解码器时会明确报出未覆盖范围。其他设备、纯云端或已删除记录不在本机读取范围。[详细说明](references/history.md)

实现使用 agy 官方的 [MCP 工作区配置](https://antigravity.google/docs/mcp/)。项目操作使用其[执行模式](https://antigravity.google/docs/cli/modes/)，并保留 CLI 自身的权限处理。

## 回执与失败恢复

标准输出只含 Gemini 最终原文；诊断走标准错误。`session.json` 保存续谈配置，`turns/` 中逐轮保存 JSON 回执和原始回复。回执包含模型、会话 ID、工具状态、输入参数、输出及错误。

`SUCCESS` 仅说明 CLI 返回成功，项目任务是否完成应核对工具结果。被拒绝、超时或中断后先检查回执和已修改文件，再续做剩余步骤；脚本不自动重试项目任务。`auto` 的可用性取决于 agy 版本、账号与平台，实际降级会保留在诊断中。

## 验证与打包

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/build_package.py
```

测试只使用合成历史与模拟 CLI，涵盖长消息分页、全文搜索、归档、压缩历史、源数据只读、会话续谈、并发锁、错误回执和中断回收。真实调用使用虚构项目另行验证，不把用户聊天或账号信息作为验收材料。

新版统一入口是 `scripts/call.py`；旧的 `relay.py`、`rewrite.py` 和重写专用沙箱已移除。旧版本保留在 Git 历史中，不再为旧参数维持重复实现。
