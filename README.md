# 通过 agy 调用 Gemini 订阅

通过本机已登录的 Google Antigravity CLI（`agy`）转达问题或项目任务，将 Gemini 的实际回复原样带回 Codex。支持连续追问、项目文件修改与测试、按需查询 Codex 历史，也保留原来的只读重撰功能。

## 安装与使用

需要 Python 3.9+ 和已登录的官方 `agy`，本项目在 macOS 验证；只读重撰依赖 macOS 的 `sandbox-exec`。将完整的 `call-gemini-agy-subscription` 目录放入 `~/.codex/skills/`，或将该位置链接到源码目录，不要只复制单个脚本。[Antigravity CLI 安装与登录](https://antigravity.google/docs/getting-started?tab=cli)

技能显示名为「调用 Gemini 订阅（agy）」，保留显式调用策略。在 Codex 中选择它，或输入：

```text
$call-gemini-agy-subscription 让 Gemini 看看这个项目，先解释失败原因。
```

之后可以说「继续让它按刚才的方案修复并跑测试」。明确说「用 Gemini 重写上条回复」，或在完整回复后单独发送技能名，会使用只读重撰入口。

两个入口都按 `--model`、技能根目录的 `model.txt`、CLI 当前选择依次决定模型，并在发送消息前确认是 Gemini。用 `agy models` 查看可用标识。仅使用 Google 账号订阅，设置 `modelProvider=""`、`useG1Credits=false`，不在额度耗尽后改用额外 AI 点数。[官方设置说明](https://antigravity.google/docs/settings?tab=cli)

## 脚本调用

假定下列变量是已存在的绝对目录，`RUN_DIR` 是当前 Codex 聊天专属的回执目录。每轮文件名须唯一。

```sh
python3 "$SKILL_DIR/scripts/relay.py" --workspace "$PROJECT_DIR" \
  --receipt "$RUN_DIR/turn-01.json" <<'MESSAGE'
请说明这个项目的入口在哪里，先不要修改文件。
MESSAGE

python3 "$SKILL_DIR/scripts/relay.py" --workspace "$PROJECT_DIR" \
  --resume-receipt "$RUN_DIR/turn-01.json" --receipt "$RUN_DIR/turn-02.json" <<'MESSAGE'
继续结合入口函数举个例子。
MESSAGE
```

纯聊天用 `--no-project`。`--resume-receipt` 核对原工作区和历史服务，并沿用模型与执行模式；`--conversation ID` 保留兼容，但不具备事前工作区校验。换项目或独立话题应新开对话，同一 Gemini 会话顺序执行。

默认 `--execution-mode auto`，采用官方自动权限评估，终端首先在沙箱内运行。可选 `accept-edits` 保留仅自动编辑项目和执行沙箱内命令的行为。无界面模式无法弹出确认；被权限策略拒绝的工具可能伴随整轮成功状态返回。脚本保留这些诊断，不将其转化为“任务已完成”。[执行模式与无界面权限](https://antigravity.google/docs/cli/headless/) · [终端沙箱](https://antigravity.google/docs/sandbox?tab=cli)

重撰命令为 `scripts/rewrite.py`，支持 `--workspace`/`--no-project`、`--model`、`--artifact`、`--verbatim`、`--packet` 和 `--receipt`。输入格式与工作流程见 [SKILL.md](SKILL.md)。默认时限：转达 600 秒、重撰 240 秒，均包含模型准备。

## 回执、失败与历史依赖

标准输出只含 `result.response` 的 UTF-8 原文，诊断和续谈 ID 写入标准错误。`--receipt` 在调用前预留新文件，以当前用户独占读写权限原子更新；保存会话、实际配置、工具状态/参数/输出/错误、CLI 诊断和最终回复。检查失败、超时或中断后的回执和已修改文件，再决定续做哪一步；脚本不自动重跑项目操作。强制杀死进程或断电只能保留此前已经写入的检查点。

项目协作拥有指定工作区内的读写和命令执行能力，仍受 CLI、操作系统及宿主权限约束。只读重撰维持独立沙箱，禁止修改项目或执行项目命令。发出的消息和模型实际读取的材料会进入 Google 上下文；回执可能包含这些内容，不应放入公共仓库。

历史查询依赖另行安装的只读 `local-codex-context` MCP 服务；服务名可由 `--history-server` 指定。支持范围、普通/归档定位、旧格式漏检和分页见 [历史读取说明](references/history.md)。其他机器安装本技能后，还需单独接通该服务。未配置历史服务不影响普通对话。

## 验证与打包

在技能根目录运行离线回归，不调用 Google：

```sh
python3 -B -m unittest discover -s tests -v
```

macOS 上在允许启动 Seatbelt 的宿主环境中，连同真实只读策略一起验收：

```sh
RUN_MACOS_SANDBOX_TESTS=1 python3 -B -m unittest discover -s tests -v
```

本机维护使用 `/opt/anaconda3/bin/python -B`。真实 CLI 测试使用虚构项目和会话，与离线回归分开记录。

```sh
python3 -B scripts/build_package.py
```

该命令在技能目录旁生成 ZIP，按维护清单收录源码、说明和测试，核对内容与文件权限；不收录 `model.txt`、回执、日志、Git 元数据和缓存。
