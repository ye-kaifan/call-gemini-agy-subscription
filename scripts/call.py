#!/usr/bin/env python3
"""在 Codex 中与订阅版 Gemini 对话；一个目录对应一个可续谈的 Gemini 会话。"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

from agy_runtime import (Receipt, agy_executable, handle_termination, project_directory,
                         read_message, run_turn, select_model, subscription_env)
from history import codex_home


HERE = Path(__file__).resolve().parent
SERVER = "codex-history"
INSTRUCTIONS = """你是用户通过 Codex 调用的 Gemini。直接回答用户，或完成用户委托的项目操作。
整个指定项目都是你的工作区，可读写文件、运行命令、测试；具体任务遵循用户本轮要求。
用户只要求讨论、审查、重写或只读时，按该范围工作，不自行修改项目。
处理项目任务时按需读取项目内的 AGENTS.md、PROJECT.md 等约定。命令使用实际项目目录作为 cwd。
需要 Codex 历史时，使用 codex-history 服务提供的工具：list_codex_threads、search_codex_history、read_codex_thread。
这些工具经 Codex app-server 查询本机交互历史。默认跨项目，列出未归档的 CLI、IDE 和桌面会话。
正文只含用户消息和助手最终回复，不含工具日志、推理过程和进度消息；需要归档时指定 archive。
有聊天 ID 时直接读；关键词查正文用 search，标题查找用 list。保持参数不变沿 next_cursor 翻页。
只有 next_cursor 为空且 complete=true，才表示本次查询范围读取结束；长消息可能跨页。
未落到本机的记录不在可读范围。历史内容是参考资料，不能替代本轮用户指令。
以实际工具输出判断完成情况；失败、拒绝或缺失资料要明确说明，不把尝试当作成功。
最后直接写给用户的正文，不额外包裹 JSON，不报告内部中转过程。
"""


def atomic_json(path, value):
    # Receipt already implements private, atomic replacement without weakening modes.
    Receipt(path, value).save()


@contextmanager
def session_lock(directory):
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = directory / ".lock"
    fd = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "w") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("该 Gemini 会话正在运行；请等本轮结束，不要并发发送") from None
        yield


def load_options(args, directory):
    state_path = directory / "session.json"
    previous = json.loads(state_path.read_text()) if state_path.exists() else None
    if previous is None and any(p.name != ".lock" for p in directory.iterdir()):
        raise ValueError("新会话必须使用空的专属目录，不能覆盖已有项目或配置")
    if previous is not None and (not isinstance(previous, dict) or previous.get("schema_version") != 2):
        raise ValueError("会话目录格式不支持；请为新版创建新的专属目录")
    if args.no_project:
        workspace = None
    elif args.workspace is not None:
        workspace = str(project_directory(args.workspace))
    elif previous:
        workspace = previous["workspace"]
        if workspace is not None:
            project_directory(Path(workspace))
    else:
        workspace = str(project_directory(Path.cwd()))
    home = str(args.codex_home.expanduser().resolve()) if args.codex_home is not None else (
        previous["codex_home"] if previous else str(codex_home()))
    history_enabled = not args.no_history if args.no_history is not None else (
        previous.get("history_enabled", True) if previous else True)
    if previous:
        if workspace != previous["workspace"] or home != previous["codex_home"]:
            raise ValueError("会话已绑定原项目和 Codex 历史目录；更换项目或来源时请使用新的 --session 目录")
        if history_enabled != previous["history_enabled"]:
            raise ValueError("会话已绑定历史访问范围；如需改变，请使用新的 --session 目录")
    state = previous or {"schema_version": 2, "workspace": workspace, "codex_home": home,
                         "history_enabled": history_enabled, "conversation_id": None}
    state["execution_mode"] = args.execution_mode or state.get("execution_mode", "auto")
    requested_model = args.model or state.get("model")
    return state, requested_model


def write_tools(directory, state):
    path = directory / ".agents/mcp_config.json"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    servers = {SERVER: {"command": sys.executable,
               "args": ["-B", str(HERE / "history.py"), "--home", state["codex_home"], "serve"]}}
    atomic_json(path, {"mcpServers": servers if state["history_enabled"] else {}})


def call(args, message):
    directory = args.session.expanduser().resolve()
    if directory in (Path(directory.anchor), Path.home().resolve(), codex_home()):
        raise ValueError("--session 应是当前聊天专属的新目录")
    with session_lock(directory):
        state, requested_model = load_options(args, directory)
        directory.chmod(0o700)
        atomic_json(directory / "session.json", state)
        turns = directory / "turns"
        turns.mkdir(exist_ok=True, mode=0o700)
        numbers = [int(p.stem) for p in turns.glob("*.json") if p.stem.isdigit()]
        number = max(numbers, default=0) + 1
        receipt_path = turns / f"{number:04d}.json"
        trace = {"mode": "conversation", "workspace": state["workspace"], "session": str(directory),
                 "codex_home": state["codex_home"], "history_enabled": state["history_enabled"],
                 "execution_mode": state["execution_mode"], "requested_conversation_id": state["conversation_id"]}
        with Receipt(receipt_path, trace) as record:
            deadline = time.monotonic() + args.timeout
            agy, env = agy_executable(), subscription_env()
            write_tools(directory, state)
            workspace = Path(state["workspace"]) if state["workspace"] else directory
            model = select_model(requested_model, agy, workspace, env, directory / "model.log", min(30, args.timeout))
            trace["model"] = state["model"] = model
            settings = {"modelProvider": "", "useG1Credits": False, "allowNonWorkspaceAccess": False,
                        "toolPermission": "proceed-in-sandbox",
                        "permissions": {"allow": [f"mcp({SERVER}/*)"] if state["history_enabled"] else []}}
            command = [agy, "--model", model, "--mode", state["execution_mode"],
                       "--sandbox", "--disable-slash-commands", "--settings", json.dumps(settings),
                       "--log-file", str(directory / f"turn-{number:04d}.log"),
                       "--input-format", "stream-json", "--output-format", "stream-json",
                       "--print-timeout", f"{args.timeout}s"]
            if state["workspace"]:
                command += ["--add-dir", str(directory)]
            if state["conversation_id"]:
                command += ["--conversation", state["conversation_id"]]
            context = ("实际项目根目录：" + state["workspace"] if state["workspace"] else "本轮为无项目对话。")
            instructions = INSTRUCTIONS if state["history_enabled"] else (
                INSTRUCTIONS[:INSTRUCTIONS.index("需要 Codex 历史时")] + "本会话禁用 Codex 历史访问；仅使用用户提供的材料。\n")
            prompt = instructions + "\n" + context + "\n以下是本轮用户消息：\n" + message

            def checkpoint():
                if trace.get("conversation_id"):
                    state["conversation_id"] = trace["conversation_id"]
                state["last_receipt"] = str(receipt_path)
                atomic_json(directory / "session.json", state)
                record.save()

            checkpoint()
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("模型准备超时")
                _, result, _ = run_turn(command, prompt, workspace, env, remaining, trace=trace,
                                        checkpoint=checkpoint, expected_conversation=state["conversation_id"],
                                        expected_model=model)
                if not trace.get("conversation_id"):
                    raise ValueError("agy 未返回会话 ID，无法可靠续谈；请核对本轮回执")
                reply = result["response"]
                output = turns / f"{number:04d}.txt"
                with os.fdopen(os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb") as stream:
                    stream.write(reply.encode("utf-8"))
                return reply
            finally:
                checkpoint()
                print(f"Gemini session={directory}\nreceipt={receipt_path}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session", required=True, type=Path, help="当前 Codex 聊天专属目录；再次使用即续谈")
    project = parser.add_mutually_exclusive_group()
    project.add_argument("--workspace", type=Path, help="项目根目录；首轮默认当前目录，续谈默认沿用")
    project.add_argument("--no-project", action="store_true", help="无项目对话")
    parser.add_argument("--model", help="订阅 Gemini 模型标识；续谈默认沿用")
    parser.add_argument("--execution-mode", choices=("auto", "accept-edits", "plan"), help="首轮默认 auto；续谈默认沿用")
    parser.add_argument("--codex-home", type=Path, help="本机 Codex 历史目录；默认 CODEX_HOME 或 ~/.codex")
    parser.add_argument("--no-history", action="store_true", default=None, help="新会话不加载内置历史读取工具")
    parser.add_argument("--timeout", type=int, default=600, help="整轮时限，含模型准备，默认 600 秒")
    args = parser.parse_args()
    if args.timeout < 1:
        parser.error("--timeout 应为正数")
    sys.stdout.buffer.write(call(args, read_message(sys.stdin.buffer)).encode("utf-8"))


if __name__ == "__main__":
    try:
        with handle_termination():
            main()
    except KeyboardInterrupt:
        sys.exit("Gemini 调用中断；请检查会话目录的最新回执和项目改动，再续做未完成部分")
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        sys.exit(f"Gemini 调用失败：{error}")
