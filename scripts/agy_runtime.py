"""Shared subscription CLI transport. stdout belongs exclusively to the reply."""
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from uuid import UUID


HERE = Path(__file__).resolve().parent
INPUT_LIMIT = 512 * 1024


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


def agy_executable():
    search_dirs = [str(Path.home() / ".local/bin")]
    search_dirs.extend(p for p in os.get_exec_path() if os.path.isabs(p))
    agy = shutil.which("agy", path=os.pathsep.join(search_dirs))
    if not agy:
        raise ValueError("找不到官方 Antigravity CLI，请先安装并登录 agy")
    return os.path.realpath(agy)


def subscription_env():
    env = dict(os.environ, AGY_CLI_DISABLE_AUTO_UPDATE="1")
    for key in ("GEMINI_API_KEY", "GOOGLE_GEMINI_BASE_URL", "AGY_ADC_AUTH"):
        env.pop(key, None)
    return env


def configured_model():
    model_file = HERE.parent / "model.txt"
    if model_file.is_file():
        return model_file.read_text(encoding="utf-8").strip() or None
    return None


def validate_model(model):
    if (not isinstance(model, str) or len(model) > 120
            or not re.fullmatch(r"gemini-[a-z0-9]+(?:[.-][a-z0-9]+)*", model)):
        raise ValueError("请用 --model 或 model.txt 指定 agy models 中的 Gemini 模型标识")


def select_model(requested, agy, workspace, env, log, timeout=30, *, command_prefix=()):
    model = requested or configured_model()
    if model is None:
        # /model resolves the current selection locally; it is not a model prompt.
        with subprocess.Popen(
            [*command_prefix, agy, "--settings", '{"modelProvider":"","useG1Credits":false}',
             "--log-file", str(log), "--print", "/model"], cwd=workspace, env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            start_new_session=True,
        ) as proc:
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except BaseException:
                stop_process(proc)
                proc.communicate()
                raise
        if proc.returncode or not stdout.strip():
            raise ValueError(error_reason(stderr.decode(errors="replace")))
        model = stdout.decode("utf-8").split()[0]
    validate_model(model)
    return model


def project_directory(path):
    root = path.expanduser().resolve(strict=True)
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
    if (not root.is_dir() or root in (Path(root.anchor), Path.home().resolve())
            or codex_home.is_relative_to(root) or root.is_relative_to(codex_home)):
        raise ValueError("--workspace 须指向具体项目，Codex 历史由内置读取器查询；纯对话用 --no-project")
    return root


def read_message(stream):
    raw = stream.read(INPUT_LIMIT + 1)
    if len(raw) > INPUT_LIMIT:
        raise ValueError("输入超过 512 KiB，请缩小本轮消息并按需读取项目或历史")
    message = raw.decode("utf-8")
    if not message.strip():
        raise ValueError("请从标准输入提供要转达的消息")
    return message


class Receipt:
    """Reserve a new private receipt before invoking the CLI; checkpoint evidence."""
    def __init__(self, path, trace):
        self.path = path.expanduser().absolute() if path is not None else None
        self.trace = trace

    def __enter__(self):
        if self.path is not None:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
        self.trace.update(schema_version=1, status="running", started_at=now())
        self.save()
        return self

    def save(self):
        if self.path is None:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=".agy-receipt-", delete=False) as stream:
                temporary = Path(stream.name)
                json.dump(self.trace, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def __exit__(self, kind, error, tb):
        self.trace.update(status="error" if kind else "success", finished_at=now())
        if kind:
            self.trace["error"] = str(error) or "调用中断"
        self.save()


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def stop_process(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    except PermissionError:
        # macOS sandboxing can report EPERM for a group whose child just exited.
        # Do not mask the actual CLI failure with that cleanup race.
        if proc.poll() is None:
            raise


@contextmanager
def handle_termination():
    def terminate(signum, frame):
        raise KeyboardInterrupt("收到终止信号；本轮可能已执行部分操作")
    previous = signal.signal(signal.SIGTERM, terminate)
    try:
        yield
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_turn(args, prompt, review_dir, env, timeout, *, trace=None, checkpoint=None,
             expected_conversation=None, expected_model=None):
    """Consume NDJSON as it arrives, keeping completed and in-flight tool evidence."""
    trace = trace if trace is not None else {}
    trace.update(tools=[], init={})
    trace.pop("result", None)
    tools, init, result = trace["tools"], {}, {}
    indices = {}
    outgoing = memoryview((json.dumps({"event": "user", "message": {"content": prompt}},
                                     ensure_ascii=False) + "\n").encode("utf-8"))
    deadline = time.monotonic() + timeout
    pending, diagnostics = bytearray(), bytearray()

    def consume(line):
        nonlocal init, result
        if not line.strip():
            return
        if len(line) > 16 * 1024**2:
            raise ValueError("agy 单个流事件超过 16 MiB")
        try:
            event = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise ValueError("agy 返回了无法解析的 stream-json 事件") from None
        if not isinstance(event, dict):
            raise ValueError("agy stream-json 事件须为 JSON 对象")
        kind = event.get("event")
        if kind not in ("init", "step_update", "result"):
            return  # Allow future informational event types.
        data = event.get(kind)
        if not isinstance(data, dict):
            raise ValueError(f"agy 的 {kind} 事件缺少对象载荷")
        ident = data.get("conversation_id") or event.get("conversation_id")
        if ident:
            try:
                if not isinstance(ident, str):
                    raise ValueError
                ident = str(UUID(ident))
            except ValueError:
                raise ValueError("agy 返回了无效的会话 ID") from None
            if expected_conversation and ident != expected_conversation:
                raise ValueError("agy 返回的会话 ID 与预期不符，请核查回执和项目现场")
            trace["conversation_id"] = ident
        if kind == "init":
            init = data
            trace["init"] = init
            if expected_model and init.get("model") and init["model"] != expected_model:
                raise ValueError("agy 实际使用的模型与指定模型不符")
        elif kind == "result":
            if result:
                raise ValueError("单轮调用收到了重复的 result 事件")
            result = data
            trace["result"] = result
        else:
            info = data.get("tool_info") or {}
            if not isinstance(info, dict):
                raise ValueError("agy 的 tool_info 须为 JSON 对象")
            name = info.get("name") or data.get("tool_name")
            if not name:
                return
            record = {"tool": name, "state": data.get("state"),
                      "parameters": info.get("parameters", {}), "error": info.get("error")}
            if "output" in info:
                record["output"] = info["output"]
            index = data.get("step_index")
            if isinstance(index, int):
                record["step_index"] = index
                if index in indices:
                    old = tools[indices[index]]
                    # Some CLI updates omit arguments/output already emitted earlier.
                    if "parameters" not in info:
                        record["parameters"] = old.get("parameters", {})
                    old.update(record)
                else:
                    indices[index] = len(tools)
                    tools.append(record)
            else:
                tools.append(record)
        if checkpoint:
            checkpoint()

    with subprocess.Popen(args, cwd=review_dir, env=env, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          start_new_session=True) as proc, selectors.DefaultSelector() as selector:
        for stream, events in ((proc.stdin, selectors.EVENT_WRITE),
                               (proc.stdout, selectors.EVENT_READ), (proc.stderr, selectors.EVENT_READ)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, events)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError(f"agy 在 {max(1, round(timeout))} 秒内未完成")
                for key, _ in selector.select(remaining):
                    stream = key.fileobj
                    if stream is proc.stdin:
                        try:
                            count = os.write(stream.fileno(), outgoing[:65536])
                            outgoing = outgoing[count:]
                        except BrokenPipeError:
                            outgoing = outgoing[len(outgoing):]
                        if not outgoing:
                            selector.unregister(stream)
                            stream.close()
                        continue
                    chunk = os.read(stream.fileno(), 65536)
                    if not chunk:
                        selector.unregister(stream)
                        if stream is proc.stdout and pending:
                            consume(bytes(pending))
                            pending.clear()
                        continue
                    if stream is proc.stderr:
                        diagnostics.extend(chunk)
                        # Keep a useful tail without letting noisy logs exhaust memory.
                        del diagnostics[:-65536]
                        trace["stderr"] = diagnostics.decode("utf-8", errors="replace")
                        continue
                    pending.extend(chunk)
                    while b"\n" in pending:
                        line, _, tail = pending.partition(b"\n")
                        pending[:] = tail
                        consume(line)
                    if len(pending) > 16 * 1024**2:
                        raise ValueError("agy 单个流事件超过 16 MiB")
            try:
                proc.wait(timeout=max(0.01, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ValueError(f"agy 在 {max(1, round(timeout))} 秒内未完成") from None
        except BaseException:
            stop_process(proc)
            proc.wait()
            raise
        finally:
            trace["returncode"] = proc.returncode
            if diagnostics:
                print(diagnostics.decode("utf-8", errors="replace"), end="", file=sys.stderr)
    if proc.returncode or result.get("status") != "SUCCESS":
        raise ValueError(error_reason(result.get("error") or diagnostics.decode(errors="replace")))
    if expected_conversation and trace.get("conversation_id") != expected_conversation:
        raise ValueError("agy 未返回预期的会话 ID，无法确认续谈结果")
    if result.get("denied_actions"):
        warning = "agy 有操作被权限策略拒绝，请检查回执中的 denied_actions 和工具结果。"
        trace.setdefault("warnings", []).append(warning)
        print(warning, file=sys.stderr)
    reply = result.get("response")
    if not isinstance(reply, str) or not reply.strip():
        if result.get("denied_actions"):
            raise ValueError("agy 的操作被权限策略拒绝，未生成回复；请检查回执中的 denied_actions 和工具结果")
        raise ValueError("agy 返回了空回复")
    return init, result, tools
