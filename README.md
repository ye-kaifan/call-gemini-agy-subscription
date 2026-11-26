# call-gemini-agy-subscription

在 Codex 中通过 Antigravity CLI（`agy`）与 Gemini 对话、续谈和操作项目，并按需查询本机 Codex 历史。使用 Google 账号的订阅或免费额度，Gemini 的原始回复返回当前聊天。

## 让 Codex 帮你安装

把这段话发给运行在你自己电脑上的 Codex：

```text
请阅读 https://github.com/ye-kaifan/call-gemini-agy-subscription 的 README，
按首次安装流程检查环境、补齐依赖、安装 skill，并验证 Gemini 调用和续谈。
如果历史查询依赖可用，也验证历史工具；需要我登录或处理账号权限时告诉我具体步骤。
```

以下流程写给负责安装的 Codex：已满足的步骤直接跳过，缺少依赖时继续安装；遇到需要用户选择账号、登录或购买套餐的步骤时，说明需要用户做什么，完成后接着验证。以实际调用结果判断是否可用，不能只凭文件下载成功宣布安装完成。

## 1. 检查环境与账号

所有命令都在**将来实际运行这个技能的机器、操作系统用户和终端环境**中执行。远程服务器或容器不会自动拥有电脑上的登录状态与 Codex 历史。

| 检查项 | 要求与缺失时的处理 |
| --- | --- |
| 操作系统 | macOS 或 Linux；Windows 原生暂不支持，可在 WSL 的 Linux 环境内安装全部依赖 |
| Python | `python3 --version` 至少为 3.10；优先使用已有合适环境，否则通过本机包管理器或 [Python 官网](https://www.python.org/downloads/)安装。本技能运行时仅使用标准库，无需 `pip install` |
| 下载工具 | 检查 `git --version` 和 `curl --version`；缺少时用本机包管理器补齐。没有 Git 也可下载仓库 ZIP |
| Google 账号 | 能登录 Antigravity 并使用至少一个 Gemini 模型；按下面的账号情况处理 |
| Codex CLI | 历史查询需要；只使用 Gemini 对话和项目操作时可暂不安装 |

**没有付费 Gemini 会员不等于不能使用。** 官方目前为免费账号提供部分模型和额度；付费 Google AI Pro / Ultra 的模型与额度不同，试用资格也可能不同。先登录、查看账号实际可用的 Gemini 模型并试用，不要直接要求用户购买会员。已有会员则登录对应账号。具体权益以 [Antigravity 套餐说明](https://antigravity.google/docs/plans)为准。

这份指引使用个人 Google 账号登录。Google Workspace 套餐不自动等于个人 Antigravity 权益；组织账号遇到限制时，先让用户确认账号资格。本技能不需要 Gemini API key，并关闭额外 AI 点数消耗；不要为完成安装擅自切换到 API、Google Cloud 按量计费或购买套餐。

## 2. 安装并登录 Antigravity CLI

### 已安装时先检查

```sh
command -v agy
agy --version
agy --help
```

若 PATH 中找不到，检查 `~/.local/bin/agy`；找到后把所在目录加入当前终端 PATH，或用绝对路径执行下文命令。需要的是支持 `models`、`--input-format`、`--output-format` 的官方 CLI；仅安装 Antigravity 桌面应用或旧 IDE 启动命令，还不能证明这个 CLI 可用。

### 未安装时安装

按[官方 CLI 安装说明](https://antigravity.google/docs/getting-started?tab=cli)使用安装脚本。下面的参数保留现有 shell 别名和配置文件，PATH 只对当前终端生效：

```sh
AGY_INSTALLER="$(mktemp)"
curl -fsSL https://antigravity.google/cli/install.sh -o "$AGY_INSTALLER" &&
  bash "$AGY_INSTALLER" --skip-aliases --skip-path &&
  export PATH="$HOME/.local/bin:$PATH" &&
  agy --version
```

安装失败时先处理下载或系统依赖错误，不继续登录。`--skip-aliases`、`--skip-path` 的含义见[官方安装参数](https://antigravity.google/docs/install/#installation-flags)。技能也会直接查找 `~/.local/bin/agy`，无需为它改写全局 PATH。

### 未登录或登录失效时

在可交互的终端启动下面的命令。若已登录且账号正确，沿用现有登录状态：

```sh
env -u GEMINI_API_KEY -u GOOGLE_GEMINI_BASE_URL -u AGY_ADC_AUTH \
  agy --settings '{"modelProvider":"","useG1Credits":false}'
```

CLI 会尝试复用本机凭据；需要认证时，由用户在它打开的 Google 登录页面选择个人账号并完成授权。首次启动还可能要求选择界面设置和确认工作目录信任。Codex 应给用户明确的终端命令和待完成步骤，等待用户完成后继续；不要索取密码或把授权码粘贴到聊天中。详见[官方认证说明](https://antigravity.google/docs/install/)。

浏览器里登录过 Gemini 网页不代表 CLI 已登录。也不要仅因一次受限终端中的报错就退出已有账号；先核对终端是否能访问系统凭据和 agy 的本地数据目录。

### 选择当前账号可用的 Gemini 模型

登录完成后退出交互界面，再执行：

```sh
env -u GEMINI_API_KEY -u GOOGLE_GEMINI_BASE_URL -u AGY_ADC_AUTH \
  agy --settings '{"modelProvider":"","useG1Credits":false}' models
```

从实际输出中选择一个 `gemini-` 开头的模型 ID，记录为后文的 `GEMINI_MODEL`。有用户偏好时按偏好选择，否则选一个账号可用的 Gemini 模型；不要照抄作者的模型配置，也不要把显示名称当成模型 ID。没有可用模型或额度不足时，告知用户具体限制，待账号恢复后再验证。模型列表成功仍不代替后面的真实调用检查。

## 3. 安装 skill

先确认**当前 Codex 实际扫描的用户技能目录**。官方文档列出的用户目录为 `~/.agents/skills/`；部分宿主的内置 `skill-installer` 使用 `${CODEX_HOME:-$HOME/.codex}/skills/`。以当前宿主的技能列表或安装器规则为准，选一个位置，避免重复安装同名技能。参见 [Codex 技能目录说明](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

如果 Codex 提供 `skill-installer`，让它安装本仓库**根目录**：仓库 `ye-kaifan/call-gemini-agy-subscription`、路径 `.`、名称 `call-gemini-agy-subscription`，目标为刚确认的技能目录。也可直接使用 Git：

```sh
# 如果当前宿主使用其他技能目录，先调整这一行。
SKILLS_DIR="$HOME/.agents/skills"
SKILL_DIR="$SKILLS_DIR/call-gemini-agy-subscription"
mkdir -p "$SKILLS_DIR"
git clone https://github.com/ye-kaifan/call-gemini-agy-subscription.git "$SKILL_DIR"
test -f "$SKILL_DIR/SKILL.md"
python3 "$SKILL_DIR/scripts/call.py" --help
```

目标已存在时，先检查其来源、符号链接和本地改动，再决定更新；不要删除后重装。使用 ZIP 时，把包含 `SKILL.md` 的仓库根目录改名为 `call-gemini-agy-subscription`，放进已确认的技能目录，避免多套一层目录。

后续命令中的 `SKILL_DIR` 都指这个实际安装路径。提醒用户在下一轮消息中显式调用 `$call-gemini-agy-subscription`；未出现时刷新或重启 Codex。当前轮也可以直接执行已安装的脚本完成验证。

## 4. 准备 Codex 历史查询

先检查：

```sh
command -v codex
codex --version
codex app-server --help
```

仅安装 Codex 桌面应用不代表终端里一定能找到 `codex`。缺少时可按[官方 Codex CLI 指引](https://learn.chatgpt.com/docs/codex/cli#getting-started)安装；macOS / Linux 的官方独立安装方式为：

```sh
CODEX_INSTALLER="$(mktemp)"
curl -fsSL https://chatgpt.com/codex/install.sh -o "$CODEX_INSTALLER" &&
  sh "$CODEX_INSTALLER"
```

安装后重新检查命令路径。若 Codex 当前终端的 PATH 尚未更新，可在后续调用中设置 `CODEX_HISTORY_BIN` 为实际 CLI 的绝对路径。读取本地历史不启动 Codex 模型回答，无需为此另配 API key。

暂时不能安装 CLI，或用户不需要该功能时，仍可完成 Gemini 调用验证；报告“Gemini 可用，历史查询未启用/未验证”，并在新会话首轮使用 `--no-history`。没有历史的新用户仍可进行下一节的空目录检查。

## 5. 首次调用检查

### Gemini 调用与续谈

先确认 `SKILL_DIR` 是安装路径，并把下面的模型占位值替换成上一步获得的真实 ID。这两次请求会使用少量账号额度。检查使用独立临时目录，不读取项目或个人历史：

```sh
GEMINI_MODEL='替换为 agy models 返回的 Gemini 模型 ID'
CHECK_DIR="$(mktemp -d "${TMPDIR:-/tmp}/gemini-skill-check.XXXXXX")"

python3 "$SKILL_DIR/scripts/call.py" \
  --session "$CHECK_DIR/chat" --no-project --no-history \
  --model "$GEMINI_MODEL" <<'MESSAGE'
这是安装检查。不要调用任何工具，只回复 GEMINI_SKILL_OK。
MESSAGE

python3 "$SKILL_DIR/scripts/call.py" --session "$CHECK_DIR/chat" <<'MESSAGE'
上一条用户消息要求你回复的校验词是什么？不要调用工具，只回复那个词。
MESSAGE
```

两次正文都应为 `GEMINI_SKILL_OK`。检查 `chat/turns/` 的回执没有失败或权限错误，两轮使用同一个非空会话 ID。若首轮成功、续谈报 `conversation not found`，检查 agy 是否有权限保存本地会话数据；不要将另建会话算作续谈成功。

### 历史工具（需要此功能时）

使用空的临时 Codex 目录检查接口和 MCP 链路，不读取真实聊天：

```sh
mkdir -p "$CHECK_DIR/codex-home"
python3 "$SKILL_DIR/scripts/history.py" --home "$CHECK_DIR/codex-home" \
  list --args '{"limit":1}'

python3 "$SKILL_DIR/scripts/call.py" \
  --session "$CHECK_DIR/history-chat" --no-project \
  --codex-home "$CHECK_DIR/codex-home" --model "$GEMINI_MODEL" <<'MESSAGE'
这是空历史目录的安装检查。请实际调用 codex-history 的 list_codex_threads 工具，
如有 next_cursor 则继续查询；成功后报告会话数，失败则报告实际错误。只查询这个测试目录，不读取项目或真实聊天。
MESSAGE
```

本地查询应返回空 `threads` 和 `complete=true`；Gemini 回执中应有成功的历史 MCP 调用，结果为 0 条会话。不要仅凭 Gemini 自称“可以读取历史”判定通过。

`--no-history` 会绑定到会话，因此这里必须新建 `history-chat`，不能复用前面的 `chat`。空目录检查只验证连接，不代表已验证个人历史覆盖；正式使用时新建正常会话，省略测试用的 `--codex-home`，默认读取 `CODEX_HOME` 或 `~/.codex`。

安装结束时告知用户实际安装路径、所选模型、Gemini 调用/续谈/历史工具各自的结果，以及仍需用户完成的步骤。登录或账号资格尚未满足时，说明“文件已安装，调用尚未就绪”及具体原因。

## 常见安装问题

| 现象 | 处理方式 |
| --- | --- |
| 找不到 `agy`，或不支持流式参数 | 检查是否装成 IDE 启动器、安装了不同的 `gemini` 命令，或 CLI 过旧；使用官方 `agy`，必要时运行 `agy update` |
| 终端可用，Codex 中提示未登录或无法保存会话 | 检查两者是否同一系统用户、PATH 是否一致，以及凭据和 agy 数据目录的访问权限；按宿主正常审批机制授予该次调用所需权限 |
| 登录成功，但模型不可用、额度不足或返回 401/403/429 | 核对登录账号、`agy models` 的实际列表与额度；等待恢复或让用户处理账号资格，不自动改用额外付费通道 |
| 下载、登录或模型请求出现网络/TLS 错误 | 先确认该终端能访问 GitHub 和 Google 官方服务，再重试失败的步骤 |
| `codex` 不存在或历史接口不受支持 | 安装或更新 Codex CLI，检查 `CODEX_HISTORY_BIN`；暂不需要历史时创建 `--no-history` 会话 |
| 文件已安装，但 Codex 看不到技能 | 核对扫描目录、是否多套目录、是否被禁用；下一轮再检查，必要时重启 Codex |

## 日常使用

```text
$call-gemini-agy-subscription 让 Gemini 看一下当前项目，查查我以前对这个问题的讨论，再给出修改建议。
```

接着说“继续让 Gemini 按方案修改并跑测试”，Codex 就会沿用同一个 Gemini 会话。也可以直接问问题或要求重写，不需要 GPT 先作答。正常会话使用任务的本地输出目录保存，不沿用安装检查的临时目录。

## 脚本选项

命令示例和续谈规则见 [SKILL.md](SKILL.md#调用与续谈)。`scripts/call.py` 从标准输入接收消息，最多 512 KiB；大材料可让 Gemini 从项目文件或历史中读取。

| 参数 | 作用 |
| --- | --- |
| `--session DIR` | 必需；首次创建，后续自动续谈 |
| `--workspace DIR` | 首次默认当前目录；续谈沿用原项目 |
| `--no-project` | 首次无项目聊天 |
| `--model ID` | 本次指定账号可用的 Gemini 模型 |
| `--execution-mode auto/accept-edits/plan` | 首次默认 `auto`；后续沿用 |
| `--codex-home DIR` | 历史目录；默认 `CODEX_HOME` 或 `~/.codex` |
| `--no-history` | 首次不加载内置历史工具 |
| `--timeout SECONDS` | 整轮时限，默认 600 秒 |

模型选择顺序：本次参数、当前会话已选模型、本地 `model.txt`、agy 当前选择。`model.txt` 可选，不进入 Git 或分发包。`auto` 的可用性取决于 agy 版本、账号与平台，实际降级会保留在诊断中。

`session.json` 保存续谈配置；`turns/` 中逐轮保存 JSON 回执和 Gemini 原始回复。回执包含模型、会话 ID、工具调用及错误；不要公开提交会话目录。历史工具细节见 [references/history.md](references/history.md)，失败恢复方法见 [SKILL.md](SKILL.md#返回结果与处理失败)。
