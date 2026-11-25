#!/usr/bin/env python3
"""经只读沙箱调用 agy 重撰中文回复。从标准输入读取需求、已核实的结果和原稿。"""
import argparse
import json
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile


HERE = Path(__file__).resolve().parent
TEXT_LIMIT = 192 * 1024
MEDIA_LIMIT = 2 * 1024**2
TOTAL_LIMIT = 4 * 1024**2
MEDIA = {".pdf", ".png", ".jpg", ".jpeg", ".webp"}
RAW = {".h5", ".hdf5", ".npy", ".npz", ".pkl", ".pickle", ".parquet", ".bin",
       ".db", ".sqlite", ".gz", ".xz", ".zip", ".tar", ".zst"}
DENIED = ["write_file(*)", "command(*)", "unsandboxed(*)", "mcp(*)",
          "read_url(*)", "execute_url(*)", "download_url(*)"]
PROBE = "请只回复 AGY_OK。"


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
        isinstance(data.get(k), str) and data[k].strip() for k in ("request", "draft", "results")
    ):
        raise ValueError("需要填写 request、results 和 draft 三个文本字段")
    paths = data.get("artifacts")
    if not isinstance(paths, list) or not all(isinstance(p, str) and p for p in paths):
        raise ValueError("artifacts 应为文件路径数组")
    literals = data.get("verbatim", [])
    if not isinstance(literals, list) or not all(isinstance(s, str) and s for s in literals):
        raise ValueError("verbatim 应为已核实的非空字符串数组")
    return {k: data[k] for k in ("request", "results", "draft", "artifacts")} | {"verbatim": literals}


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
    for path in dict.fromkeys(paths):
        src = (root / path).resolve(strict=True)
        if not src.is_relative_to(root) or not stat.S_ISREG(src.stat().st_mode):
            raise ValueError(f"产物须是工作区内的普通文件：{path}")
        parts = set(src.relative_to(root).parts)
        if parts & {".git", ".ssh", ".gnupg", ".aws", ".codex"} or any(
            p == ".env" or p.startswith(".env.") for p in parts
        ) or src.suffix.lower() in {".pem", ".key", ".p12"}:
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


def error_reason(message):
    flat = " ".join(str(message).split())
    lower = flat.lower()
    if "sandbox_apply" in lower:
        return "当前执行环境无法启动 macOS 只读沙箱"
    if any(x in lower for x in ("429", "quota", "rate limit", "resource_exhausted")):
        return "Google 返回额度或频率限制"
    if any(x in lower for x in ("401", "403", "authenticat", "sign in", "not logged")):
        return "Google 要求登录或当前账号没有访问权限"
    if any(x in lower for x in ("deadline exceeded", "timed out", "request timeout")):
        return "Google 请求超时"
    return (flat or "没有返回有效回复")[:240]


def call_agy(model, prompt, workspace, run, timeout):
    if model is not None and (len(model) > 120 or not re.fullmatch(r"[a-z0-9]+(?:[.-][a-z0-9]+)*", model)):
        raise ValueError("模型设置须使用 agy models 列出的模型标识")
    if not Path("/usr/bin/sandbox-exec").is_file():
        raise ValueError("此脚本需要 macOS 的 sandbox-exec 提供只读保护")
    search_dirs = [str(Path.home() / ".local/bin")]
    search_dirs.extend(p for p in os.get_exec_path() if os.path.isabs(p))
    agy = shutil.which("agy", path=os.pathsep.join(search_dirs))
    if not agy:
        raise ValueError("找不到官方 Antigravity CLI，请先安装并登录 agy")
    agy = os.path.realpath(agy)
    agent = workspace / ".agents/agents/codex-reply-reader.md"
    agent.parent.mkdir(parents=True)
    shutil.copyfile(HERE / "reader.md", agent)
    settings = {
        "agentMode": "", "toolPermission": "request-review", "allowNonWorkspaceAccess": False,
        "modelProvider": "", "useG1Credits": False,
        "permissions": {"allow": [], "ask": [], "deny": DENIED + [f"read_file({Path.home()})"]},
    }
    args = ["/usr/bin/sandbox-exec", "-D", f"AGY={agy}", "-D",
            f"RUN={run}", "-f", str(HERE / "readonly.sb"),
            agy, "--agent", "codex-reply-reader",
            "--disable-slash-commands", "--settings", json.dumps(settings),
            "--input-format", "stream-json", "--output-format", "stream-json",
            "--log-file", str(run / "cli.log"), "--print-timeout", f"{timeout}s"]
    if model is not None:
        args.extend(["--model", model])
    env = dict(os.environ, TMPDIR=str(run), AGY_CLI_DISABLE_AUTO_UPDATE="1")
    for key in ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH"):
        env.pop(key, None)
    message = json.dumps({"event": "user", "message": {"content": prompt}}, ensure_ascii=False)
    with subprocess.Popen(args, cwd=workspace, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as proc:
        try:
            stdout, stderr = proc.communicate((message + "\n").encode(), timeout=timeout + 5)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise ValueError(f"agy 在 {timeout} 秒内未完成") from None
    result = {}
    for line in stdout.splitlines():
        event = json.loads(line)
        if event.get("event") == "result":
            result = event["result"]
    if proc.returncode or result.get("status") != "SUCCESS":
        raise ValueError(error_reason(result.get("error") or stderr.decode(errors="replace")))
    reply = result.get("response")
    if not isinstance(reply, str) or not reply.strip():
        raise ValueError("agy 返回了空回复")
    return reply.strip()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    parser.add_argument("--artifact", action="append", default=[], metavar="FILE",
                        help="附上一个工作区文件的只读副本，可重复")
    parser.add_argument("--verbatim", action="append", default=[], metavar="TEXT",
                        help="检查一段已核实的原文是否保留，可重复")
    parser.add_argument("--packet", type=argparse.FileType("rb"), metavar="JSON",
                        help="兼容结构化输入，含 request、results、draft、artifacts 及可选 verbatim")
    parser.add_argument("--timeout", type=int, default=120, metavar="SECONDS")
    parser.add_argument("--probe", metavar="MODEL", help="只发送固定短句验证一个订阅内候选模型")
    args = parser.parse_args()
    if args.timeout <= 0:
        parser.error("--timeout 应为正数")
    model = args.probe
    model_file = HERE.parent / "model.txt"
    if model is None and model_file.exists():
        model = model_file.read_text(encoding="utf-8").strip() or None
    with tempfile.TemporaryDirectory(prefix="gemini-final-") as temp:
        base = Path(temp).resolve()
        workspace, run = base / "review", base / "runtime"
        workspace.mkdir()
        run.mkdir()
        prompt = PROBE
        if args.probe is None:
            packet = read_packet(args.packet or sys.stdin.buffer, json_file=args.packet is not None)
            packet["artifacts"] = evidence(args.workspace.resolve(strict=True),
                                           packet["artifacts"] + args.artifact, workspace)
            packet["verbatim"].extend(args.verbatim)
            prompt = "请依据以下材料重撰一份中文最终回复。\n" + json.dumps(packet, ensure_ascii=False)
        reply = call_agy(model, prompt, workspace, run, args.timeout)
        if args.probe is not None and reply != "AGY_OK":
            raise ValueError("候选模型未返回约定的验证文本")
        if args.probe is None:
            for literal in packet["verbatim"]:
                if literal not in reply:
                    print(f"核稿时按证据恢复未逐字保留的内容：{literal}", file=sys.stderr)
            if "file://" in reply or str(workspace) in reply:
                print("核稿时请把本机链接改为原始产物的绝对路径。", file=sys.stderr)
        print(reply)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        sys.exit(f"agy 重撰失败：{exc}")
