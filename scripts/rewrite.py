#!/usr/bin/env python3
"""经只读沙箱调用 agy 重撰中文回复。从标准输入读取需求、已核实的结果和原稿。"""
import argparse
import fnmatch
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time

from agy_runtime import (Receipt, agy_executable,
                         handle_termination, project_directory, run_turn,
                         select_model, subscription_env)


HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 192 * 1024
MEDIA_LIMIT = 2 * 1024**2
TOTAL_LIMIT = 4 * 1024**2
MEDIA = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
RAW = {".h5", ".hdf5", ".npy", ".npz", ".pkl", ".pickle", ".parquet", ".bin",
       ".db", ".sqlite", ".gz", ".xz", ".zip", ".tar", ".zst"}
PRIVATE_NAMES = {".git", ".ssh", ".gnupg", ".aws", ".codex", ".agent", ".agents", ".gemini"}
KEY_SUFFIXES = {".pem", ".key", ".p12"}
DENIED = ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)",
          "read_url(*)", "execute_url(*)", "download_url(*)"]
PROBE = "请只回复 AGY_OK。"
QUERY_PREFIX = "AGY_PROJECT_QUERY "


def read_packet(stream, *, json_file=False):
    raw = stream.read(512 * 1024 + 1)
    if len(raw) > 512 * 1024:
        raise ValueError("输入材料超过 512 KiB，请使用小型摘要和文件清单")
    if not raw.strip():
        raise ValueError("请提供原始需求、已核实的结果和原稿")
    if not json_file and not raw.lstrip().startswith(b"{"):
        return {"material": raw.decode("utf-8"), "artifacts": [], "verbatim": []}
    data = json.loads(raw)
    if not isinstance(data, dict) or not all(
        isinstance(data.get(k), str) and data[k].strip() for k in ("request", "draft")
    ):
        raise ValueError("需要填写用户需求 request 和 GPT 完整原答 draft")
    results = data.get("results", "")
    if not isinstance(results, str):
        raise ValueError("results 应为文本")
    paths = data.get("artifacts", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("artifacts 应为文件路径数组")
    literals = data.get("verbatim", [])
    if not isinstance(literals, list) or not all(isinstance(s, str) and s for s in literals):
        raise ValueError("verbatim 应为已核实的非空字符串数组")
    return {"request": data["request"], "draft": data["draft"], "results": results,
            "artifacts": paths, "verbatim": literals}


def excluded_pattern(root, suffixes):
    """Build the same path rule for Python and the macOS sandbox's POSIX regex."""
    names = [re.escape(name) for name in sorted(PRIVATE_NAMES)] + [r"[.]env([.][^/]*)?"]
    extensions = []
    for suffix in sorted(suffixes):
        extensions.append("".join(f"[{c}{c.upper()}]" if c.isalpha() else re.escape(c)
                                  for c in suffix))
    return (rf"^{re.escape(str(root))}/(.*/)?"
            rf"(({'|'.join(names)})(/|$)|[^/]*({'|'.join(extensions)})$)")


def copy_artifact(src, dest, budget):
    size = src.stat().st_size
    ext = src.suffix.lower()
    if ext in RAW or size > min(budget, MEDIA_LIMIT if ext in MEDIA else TEXT_LIMIT):
        return None, "仅传文件信息（原始数据或大小超过上限）"
    with src.open("rb") as f:
        before = os.fstat(f.fileno())
        content = f.read(size + 1)
        after = os.fstat(f.fileno())
    if before.st_size != size or len(content) != size or before.st_mtime_ns != after.st_mtime_ns:
        raise ValueError(f"取证期间文件有变动：{src}")
    if ext not in MEDIA:
        try:
            content.decode("utf-8")
        except UnicodeDecodeError:
            return None, "仅传文件信息（非 UTF-8 文本）"
        if b"\x00" in content:
            return None, "仅传文件信息（二进制）"
    target = dest.with_suffix(ext if ext in MEDIA else ".txt")
    target.write_bytes(content)
    target.chmod(0o400)
    return target, ""


def evidence(root, paths, folder):
    records = []
    budget = TOTAL_LIMIT
    protected = re.compile(excluded_pattern(root, KEY_SUFFIXES))
    for path in dict.fromkeys(paths):
        original = root / path
        src = original.resolve(strict=True)
        if not src.is_relative_to(root) or not stat.S_ISREG(src.stat().st_mode):
            raise ValueError(f"产物须是工作区内的普通文件：{path}")
        if protected.search(str(original)) or protected.search(str(src)):
            raise ValueError(f"所选路径含私密配置或仓库元数据：{path}")
        dest = folder / f"artifact-{len(records) + 1:03}"
        item = {"original": str((root / path).absolute()), "bytes": src.stat().st_size}
        snapshot, reason = copy_artifact(src, dest, budget)
        if snapshot:
            budget -= snapshot.stat().st_size
            item["snapshot"] = str(snapshot)
        else:
            item["note"] = reason
            print(f"{src}：{reason}", file=sys.stderr)
        records.append(item)
    return records


def contains_text(directory_fd, name, needle, deadline):
    # A search must not follow links or block on a FIFO that appears during a scan.
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        previous = b""
        for chunk in iter(lambda: stream.read(65536), b""):
            if time.monotonic() >= deadline:
                raise TimeoutError
            if b"\0" in chunk:
                return False
            if needle in previous + chunk:
                return True
            if len(needle) > 1:
                previous = (previous + chunk)[1-len(needle):]
    return False


def query_project(root, query, deadline):
    """Return paths, without putting matching file contents into the conversation."""
    root = root.resolve(strict=True)
    op, term, path = query.get("op"), query.get("query", ""), query.get("path", ".")
    if op not in ("list", "find", "search") or not isinstance(path, str) or not isinstance(term, str):
        raise ValueError("查询需要 op（list、find 或 search）及项目内目录 path")
    if op != "list" and not 0 < len(term) <= 512:
        raise ValueError("请给出简短的文件名模式或检索词")
    excluded = re.compile(excluded_pattern(root, KEY_SUFFIXES | RAW))
    requested = root / path
    directory = requested.resolve(strict=True)
    if not directory.is_relative_to(root) or excluded.search(str(requested)) or excluded.search(str(directory)):
        raise ValueError("查询目录超出允许的项目范围")
    if not directory.is_dir():
        raise ValueError("path 须为目录")
    matches = []
    needle = term.encode("utf-8")
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    scope_fd = os.open(root, flags)
    try:
        # Anchor traversal to the chosen directory even if a parent link changes during the scan.
        for component in directory.relative_to(root).parts:
            child = os.open(component, flags, dir_fd=scope_fd)
            os.close(scope_fd)
            scope_fd = child
        for current, dirs, files, directory_fd in os.fwalk(".", dir_fd=scope_fd, follow_symlinks=False):
            folder = directory / current
            dirs[:] = sorted(name for name in dirs if not excluded.search(str(folder / name))
                             and not (folder / name).is_symlink())
            directories = set(dirs)
            for name in dirs + sorted(files):
                if time.monotonic() >= deadline:
                    return {"paths": matches, "partial": "搜索用时已满，可缩小 path"}
                item = folder / name
                if excluded.search(str(item)) or item.is_symlink():
                    continue
                if op == "find" and not fnmatch.fnmatchcase(name, term):
                    continue
                if op == "search":
                    if name in directories:
                        continue
                    try:
                        if not contains_text(directory_fd, name, needle, deadline):
                            continue
                    except TimeoutError:
                        return {"paths": matches, "partial": "搜索用时已满，可缩小 path"}
                    except OSError:
                        continue
                matches.append(item.relative_to(root).as_posix() + ("/" if name in directories else ""))
                if len(matches) >= 80:
                    return {"paths": matches, "partial": "已列出前 80 项，可缩小 path 或细化关键词"}
            if op == "list":
                break
    finally:
        os.close(scope_fd)
    return {"paths": matches}


def call_agy(model, prompt, review_dir, run, timeout, *, project_root=None, receipt=None, verbatim=()):
    trace = {"mode": "rewrite", "workspace": str(project_root) if project_root else None,
             "tools": [], "turns": []}
    with Receipt(receipt, trace) as record:
        deadline = time.monotonic() + timeout
        return _rewrite(model, prompt, review_dir, run, deadline, timeout,
                        project_root, verbatim, trace, record)


def _rewrite(model, prompt, review_dir, run, deadline, timeout, project_root, verbatim, trace, record):
    if not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("此脚本需要 macOS 的 sandbox-exec 提供只读保护")
    agy = agy_executable()
    agent = review_dir / ".agents/agents/codex-reply-reader.md"
    agent.parent.mkdir(parents=True)
    shutil.copyfile(HERE / "reader.md", agent)
    deny = list(DENIED)
    if project_root is None:
        deny.append(f"read_file({Path.home()})")
    settings = {
        "agentMode": "", "toolPermission": "request-review", "allowNonWorkspaceAccess": False,
        "modelProvider": "", "useG1Credits": False,
        "permissions": {"allow": [], "ask": [], "deny": deny},
    }
    # This path does not exist in dialogue-only and probe runs.
    protected_root = project_root or (run / "no-project")
    excluded = excluded_pattern(protected_root, KEY_SUFFIXES | RAW)
    sandbox = ["/usr/bin/sandbox-exec", "-D", f"AGY={agy}", "-D", f"EXCLUDED={excluded}", "-D",
               f"RUN={run}", "-f", str(HERE / "readonly.sb")]
    env = dict(subscription_env(), TMPDIR=str(run))
    model = select_model(model, agy, review_dir, env, run / "model.log",
                         min(30, max(0.01, deadline - time.monotonic())), command_prefix=sandbox)
    trace["model"] = model
    record.save()
    args = [*sandbox, agy, "--agent", "codex-reply-reader",
            "--disable-slash-commands", "--settings", json.dumps(settings),
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--log-file", str(run / "cli.log"), "--print-timeout", f"{timeout}s"]
    if project_root is not None:
        args.extend(["--add-dir", str(project_root)])
    args.extend(["--model", model])
    reads = trace["tools"]
    query_count = 0
    revised = False
    next_prompt = prompt
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ValueError(f"agy 在 {timeout} 秒内未完成")
        turn = {}
        trace["turns"].append(turn)
        def checkpoint():
            trace["conversation_id"] = turn.get("conversation_id")
            record.save()
        try:
            init, result, steps = run_turn(args, next_prompt, review_dir, env, remaining,
                                          trace=turn, checkpoint=checkpoint, expected_model=model)
        finally:
            reads.extend(turn.get("tools", []))
            if "result" in turn:
                trace["result"] = turn["result"]
            trace["agent"] = turn.get("init", {}).get("agent")
        reply = result["response"]
        if project_root is not None and reply.strip().startswith(QUERY_PREFIX):
            if query_count >= 8:
                raise ValueError("Gemini 连续查询八次后仍未定稿")
            query = json.loads(reply.strip()[len(QUERY_PREFIX):])
            if not isinstance(query, dict):
                raise ValueError("项目查询须为 JSON 对象")
            try:
                found = query_project(project_root, query, min(deadline, time.monotonic() + 15))
            except (ValueError, OSError) as exc:
                found = {"error": str(exc)}
            reads.append({"tool": "project_query", "origin": "Gemini", "model_response": reply,
                          "parameters": query, "result": found, "conversation_id": result.get("conversation_id"),
                          "error": found.get("error")})
            next_prompt += ("\n这是你刚才发起的项目查询及其结果。paths 相对于 project_root，"
                            "请打开相关原件核实，再作答。\n"
                            + json.dumps({"query": query, "result": found}, ensure_ascii=False))
            query_count += 1
            continue
        corrections = [f"请逐字保留 {literal}" for literal in verbatim if literal not in reply]
        if re.search(r"\]\(<?file://", reply) or str(review_dir) + "/" in reply:
            corrections.append("本机文件链接请用 [名称](/原件绝对路径)，目标从 / 开始。")
        if not corrections:
            break
        if revised:
            raise ValueError("Gemini 修订后仍未通过核对：" + " ".join(corrections))
        revised = True
        next_prompt += "\n请根据核对意见重新输出完整正文。\n" + json.dumps(
            {"Gemini 上一稿": reply, "核对意见": corrections}, ensure_ascii=False)
    return reply


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--workspace", type=Path, default=Path.cwd(), help="供 Gemini 按需检索的项目目录")
    scope.add_argument("--no-project", action="store_true", help="纯对话，只传需求、结果和原答")
    parser.add_argument("--artifact", action="append", default=[], metavar="FILE",
                        help="附上一个工作区文件的只读副本，可重复")
    parser.add_argument("--verbatim", action="append", default=[], metavar="TEXT",
                        help="检查一段已核实的原文是否保留，可重复")
    parser.add_argument("--packet", type=argparse.FileType("rb"), metavar="JSON",
                        help="结构化输入，需有 request 与 draft，可选 results、artifacts、verbatim")
    parser.add_argument("--receipt", type=Path, metavar="NEW_FILE",
                        help="将实际工具调用和 Gemini 返回的正文保存到新 JSON 文件")
    parser.add_argument("--timeout", type=int, default=240, metavar="SECONDS",
                        help="本次改写（含按需查询及修订）的总时限，默认 240 秒")
    parser.add_argument("--model", help="本次使用的订阅 Gemini 模型标识")
    parser.add_argument("--probe", metavar="MODEL", help="只发送固定短句验证一个订阅内候选模型")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 应为正数")
    if args.probe and args.model:
        parser.error("--probe 和 --model 不能同时使用")
    model = args.probe or args.model
    with tempfile.TemporaryDirectory(prefix="gemini-final-") as temp:
        base = Path(temp).resolve()
        review_dir, run = base / "review", base / "runtime"
        review_dir.mkdir()
        run.mkdir()
        prompt = PROBE
        project_root = None
        verbatim = []
        if args.probe is None:
            packet = read_packet(args.packet or sys.stdin.buffer, json_file=args.packet is not None)
            paths = packet["artifacts"] + args.artifact
            if args.no_project and paths:
                parser.error("--no-project 不能附加项目文件")
            if not args.no_project:
                project_root = project_directory(args.workspace)
                packet["project_root"] = str(project_root)
                packet["artifacts"] = evidence(project_root, paths, review_dir)
            packet["verbatim"].extend(args.verbatim)
            verbatim = packet["verbatim"]
            prompt = "请依据以下材料重撰一份中文最终回复。\n" + json.dumps(packet, ensure_ascii=False)
        reply = call_agy(model, prompt, review_dir, run, args.timeout,
                         project_root=project_root, receipt=args.receipt, verbatim=verbatim)
        if args.probe is not None and reply.strip() != "AGY_OK":
            raise ValueError("候选模型未返回约定的验证文本")
        sys.stdout.buffer.write(reply.encode("utf-8"))


if __name__ == "__main__":
    try:
        with handle_termination():
            main()
    except KeyboardInterrupt:
        sys.exit("agy 重撰中断；请检查回执")
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        sys.exit(f"agy 重撰失败：{exc}")
