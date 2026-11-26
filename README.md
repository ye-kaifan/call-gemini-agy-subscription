# call-gemini-agy-subscription

在 Codex 中直接使用订阅版 Gemini：GPT 转达你的问题或项目任务，Antigravity CLI 执行，再把 Gemini 的原始回复带回当前聊天。支持连续协作、项目文件读写与命令执行，以及跨项目查询本机保存的 Codex 历史。

## 安装

需要 macOS 或 Linux、Python 3.10+，以及已安装并登录订阅账号的官方 `agy`。历史查询还需本机 `codex` CLI，验证版本为 `0.164.0`。将本仓库放到 Codex 技能目录：

```sh
git clone https://github.com/ye-kaifan/call-gemini-agy-subscription.git \
  ~/.codex/skills/call-gemini-agy-subscription
```

也可解压分发 ZIP，将其中同名目录放到 `~/.codex/skills/`。使用自定义 `CODEX_HOME` 时放到它的 `skills/` 下。本技能保持显式调用，不自动介入普通任务。

## 在 Codex 中使用

```text
$call-gemini-agy-subscription 让 Gemini 看一下当前项目，查查我以前对这个问题的讨论，再给出修改建议。
```

接着说“继续让 Gemini 按方案修改并跑测试”，Codex 就会沿用同一个 Gemini 会话。也可以直接问问题，或要求它重写一段回答，不需要先由 GPT 作答。

## 脚本入口

```sh
python3 scripts/call.py --session /path/to/private-session \
  --workspace /path/to/project <<'MESSAGE'
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

## Codex 历史读取

`history.py` 通过本机 Codex app-server 查询历史，提供列表、正文关键词搜索、按 ID 读取及分页。默认列出各项目未归档的 CLI、IDE 和桌面交互会话；正文保留用户消息与助手最终回复。归档按需查询，列表和搜索不包含子代理与后台执行来源。

`history.py` 是本技能实现的本地读取程序。`call.py` 在会话目录的 `.agents/mcp_config.json` 中将它注册为 `codex-history` 服务，再通过 `--add-dir` 将该目录加入 agy 工作区。Gemini 选择查询工具，agy 调用这个 Python 程序，将查询结果交回 Gemini。

查询时按需启动 `codex app-server`，通过标准输入输出通信，结束时回收进程。技能调用历史查询接口，不恢复 Codex 会话或启动模型回答。存储格式由 Codex 处理。若 `codex` 不在 PATH，可将环境变量 `CODEX_HISTORY_BIN` 设为 CLI 的绝对路径；不需要保持 Codex 桌面窗口打开。[详细说明](references/history.md)

实现使用 agy 官方的 [MCP 工作区配置](https://antigravity.google/docs/mcp/)和 Codex 的 [app-server 查询接口](https://developers.openai.com/codex/app-server)。项目操作使用 agy 的[执行模式](https://antigravity.google/docs/cli/modes/)，并保留 CLI 自身的权限处理。

## 回执与失败恢复

标准输出只含 Gemini 最终原文；诊断走标准错误。`session.json` 保存续谈配置，`turns/` 中逐轮保存 JSON 回执和原始回复。回执包含模型、会话 ID、工具状态、输入参数、输出及错误。

`SUCCESS` 仅说明 CLI 返回成功，项目任务是否完成应核对工具结果。被拒绝、超时或中断后先检查回执和已修改文件，再续做剩余步骤；脚本不自动重试项目任务。`auto` 的可用性取决于 agy 版本、账号与平台，实际降级会保留在诊断中。

## 验证与打包

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/build_package.py
```

离线测试使用模拟 app-server 和 agy，涵盖交互信息筛选、长消息分页、正文搜索、归档、接口错误、进程回收、会话续谈及并发锁。真实 Codex CLI 与 Gemini 联调使用合成会话和虚构项目，不把用户聊天或账号信息作为验收材料。
