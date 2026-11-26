# call-gemini-agy-subscription

在 Codex 中通过 Antigravity CLI（`agy`）与订阅版 Gemini 对话、续谈和操作项目，并按需查询本机 Codex 历史。Gemini 的原始回复返回当前聊天。

## 安装

需要 macOS 或 Linux、Python 3.10+，以及已安装并登录订阅账号的官方 `agy`。历史查询还需本机 `codex` CLI。将本仓库放到 Codex 技能目录：

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

## 脚本选项

命令示例和续谈规则见 [SKILL.md](SKILL.md#调用与续谈)。`scripts/call.py` 从标准输入接收消息，最多 512 KiB；大材料可让 Gemini 从项目文件或历史中读取。

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

`auto` 的可用性取决于 agy 版本、账号与平台，实际降级会保留在诊断中。

历史查询范围、工具参数和本机配置见 [references/history.md](references/history.md)。

## 会话文件

`session.json` 保存续谈配置；`turns/` 中逐轮保存 JSON 回执和 Gemini 原始回复。回执包含模型、会话 ID、工具调用及错误。不要公开提交会话目录；失败恢复方法见 [SKILL.md](SKILL.md#返回结果与处理失败)。

## 验证与打包

```sh
python3 -B -m unittest discover -s tests -v
python3 -B scripts/build_package.py
```
