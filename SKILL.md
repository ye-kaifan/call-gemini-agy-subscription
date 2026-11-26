---
name: call-gemini-agy-subscription
description: 用户点名时，经本机 agy 调用订阅版 Gemini，直接对话、连续协作、操作项目，并查询本机 Codex 历史。
---

# 通过 agy 调用 Gemini 订阅

将用户的问题或任务直接交给 Gemini，无需 GPT 先作答。保留用户原话，另起一段补充必要的项目路径、指代对象和本轮限制；用户要求只读或先讨论时准确转达。

## 调用与续谈

前提：Python 3.10+，官方 `agy` 已安装并登录可用的 Google 账号；查询历史还需本机可执行的 `codex` CLI。首次安装、未登录或依赖缺失时，按 [README.md](README.md#让-codex-帮你安装)完成配置与验证。

`SKILL_DIR` 指本技能目录。为当前 Codex 聊天选择独立的 `SESSION_DIR`，放在本地输出目录，避免公开提交。首次指定实际项目根目录：

```sh
python3 "$SKILL_DIR/scripts/call.py" \
  --session "$SESSION_DIR" --workspace "$PROJECT_DIR" <<'MESSAGE'
请查一下这个问题以前在哪条 Codex 会话中讨论过，再结合当前代码给出建议。
MESSAGE
```

继续同一个 Gemini 会话时，只需沿用同一个目录：

```sh
python3 "$SKILL_DIR/scripts/call.py" --session "$SESSION_DIR" <<'MESSAGE'
继续按刚才的方案修改并运行测试。
MESSAGE
```

同一目录自动续谈并沿用项目、模型和历史来源，不可并发调用；不同 Codex 聊天、更换项目或历史来源时使用新目录。首次纯聊天用 `--no-project` 代替 `--workspace`。

输入上限 512 KiB。大材料交给 Gemini 按需读取项目文件或历史。默认总时限 600 秒，长任务可用 `--timeout` 调整。

## 选项与历史

指定项目可供 Gemini 读写和运行命令。默认使用 agy 的 `auto` 模式及终端沙箱；先讨论方案可用 `--execution-mode plan`。用 `--model` 指定 Gemini 模型；未指定时沿用会话或本机选择。调用使用 Google 账号额度并关闭额外 AI 点数。完整参数及模型选择顺序见 [README.md](README.md#脚本选项)。

查历史时，将会话 ID、关键词或项目范围交给 Gemini。默认查询本机各项目未归档交互会话中的用户消息和助手最终回复。需要归档、分页或排查历史工具问题时，见 [references/history.md](references/history.md)。首轮可用 `--codex-home` 指定历史目录，或用 `--no-history` 关闭历史工具。

## 返回结果与处理失败

成功后原样发送标准输出中的 Gemini 正文；标准错误仅供诊断。项目任务还需核对会话目录 `turns/` 中的 JSON 回执和实际修改，不能只看退出码。中断、失败或超时后先检查已执行内容，再续做剩余步骤，避免重复操作。
