# Rewrite Replies with Your Gemini Subscription

用 Google Antigravity CLI（`agy`）的订阅额度帮 Codex 重写中文最终回复。你点名后，订阅模型会结合原始问题、回复草稿和本轮产物重新组织答案，Codex 再核对数字、公式、代码、路径和结论。

## 安装

需要 macOS、Python 3.9 或更新版本，以及已登录 Google 账号的官方 `agy`。脚本使用 macOS 的 `sandbox-exec` 保护工作区。[Google Antigravity CLI 安装指南](https://antigravity.google/docs/getting-started?tab=cli)

将仓库克隆到 Codex 的个人技能目录。

```sh
mkdir -p "$HOME/.agents/skills"
git clone https://github.com/ye-kaifan/gemini-rewrite-with-subscription-quota.git \
  "$HOME/.agents/skills/gemini-rewrite-with-subscription-quota"
```

默认沿用 `agy` 当前的模型，额外付费点数已关闭。若要固定模型，用 `agy models` 查看候选标识，把订阅可用的模型标识单独写在本目录的 `model.txt` 中。

## 调用

收到回答后，可以单独发一条消息，让 Codex 重写上一条完整回答。

```text
$gemini-rewrite-with-subscription-quota
```

也可以在布置任务时点名，例如「跑完测试后，用 `$gemini-rewrite-with-subscription-quota` 根据代码改动和测试结果整理最终回复」。桌面端技能列表中的显示名是 **Rewrite Replies with Your Gemini Subscription**。

## 哪些内容会发给模型

脚本会发送任务需求、已核实的结果、原稿，以及所选文件的原路径和只读副本。大文件和原始数据只传文件信息，关键结果由 Codex 在本地核实后写入摘要。调用失败或超时时，Codex 会发出原稿，并用一句话说明原因。
