---
name: call-gemini-agy-subscription
description: 用户点名时，通过本机 Antigravity CLI 与订阅版 Gemini 对话、续谈、委托项目任务或查询 Codex 历史，也支持重撰 GPT 已有回复。
---

# 通过 agy 调用 Gemini 订阅

把用户的问题或任务直接交给 Gemini，无需 GPT 先作答。用户指定了原话就保留原文；必要的项目路径、指代对象、已有结果和本轮限制另起一段补充。用户明确要求重写已有回答，或在完整回答后单独发送技能名时，使用下面的只读重撰入口。

## 转达与续谈

将 `SKILL_DIR` 换成当前技能目录，`PROJECT_DIR` 换成用户的项目根目录。为当前 Codex 聊天选择专属的回执目录，每轮使用一个不存在的新文件；不要让多个聊天共用“最后一次会话”文件。

```sh
python3 "$SKILL_DIR/scripts/relay.py" \
  --workspace "$PROJECT_DIR" --receipt "$TURN_RECEIPT" <<'MESSAGE'
请看这个项目的测试，找出失败原因。先告诉我原因。
MESSAGE
```

纯聊天用 `--no-project` 代替 `--workspace`。输入上限 512 KiB；长材料通过具体文件或历史 ID 按需读取。两个入口的模型选择顺序都是 `--model`、技能目录 `model.txt`、`agy` 当前选项，发消息前检查必须是 Gemini；均关闭额外 AI 点数和 API-key 调用入口。

接着问同一个 Gemini 对话时，优先使用上轮回执，保留原来的项目参数：

```sh
python3 "$SKILL_DIR/scripts/relay.py" \
  --workspace "$PROJECT_DIR" --resume-receipt "$PREVIOUS_RECEIPT" \
  --receipt "$NEXT_RECEIPT" <<'MESSAGE'
继续按刚才的方案修复并运行测试。
MESSAGE
```

`--resume-receipt` 核对工作区和历史服务，沿用会话 ID、模型与执行模式；明确要求更换时加 `--model` 或 `--execution-mode`。旧的 `--conversation ID` 仍可用，但调用前须自己确认原工作区。独立话题或换项目新开对话。同一 Gemini 会话串行调用。

转达使用官方 `auto` 模式和终端沙箱，可编辑项目、执行命令并评估需要额外权限的安全操作。需要保留原先仅自动执行沙箱内命令的行为时用 `--execution-mode accept-edits`。用户要求只读或先讨论时，须把限制明确传给 Gemini。转达入口不是只读入口。无界面 CLI 无法获得的确认会被拒绝，不会自动转交给 Codex 的审批弹窗。

成功后原样发送标准输出中的 Gemini 正文。回执保存会话 ID、模型、工具状态、参数、输出、错误及原始回复。涉及操作时，用这些证据和项目现场核对完成情况；退出码为零或 `result.status=SUCCESS` 不足以证明任务完成。失败、超时或中断后先检查回执及已有修改，不能自动重跑整项任务。必要的诊断单独说明。

## 查询 Codex 历史

需要历史时阅读 [references/history.md](references/history.md)，将用户给出的聊天 ID、关键词、项目或归档范围交给 Gemini。沿用现有只读 `local-codex-context`，服务名不同可用 `--history-server`。历史工具不是普通对话的必需依赖；服务未就绪时说明缺失范围，不声称已经读取。

## 重撰已有回答

把用户需求、已核实的结果和 GPT 完整原答交给只读入口：

```sh
python3 "$SKILL_DIR/scripts/rewrite.py" --workspace "$PROJECT_DIR" \
  --receipt "$TURN_RECEIPT" <<'REPLY'
## 用户需求
原始问题、后续修正和交付要求
## 已核实的结果
改动、关键输出、结论和仍待验证的事项
## 原稿
完整回答
REPLY
```

此入口在 macOS 只读沙箱中运行，只按需查询项目原件；无项目用 `--no-project`。`--artifact 文件` 附上工作区文件，`--verbatim 文本` 检查已核实原文是否保留，`--model` 指定本次 Gemini。结构化材料用 `--packet 文件.json`，字段为 `request`、`draft`，可选 `results`、`artifacts`、`verbatim`。

核对事实、数值和链接。有出入时，将具体证据和修订要求交回 Gemini 定稿，然后逐字发送标准输出中的正文。失败或仍无法核实时，说明原因，并将使用的 GPT 原稿明确标出。
