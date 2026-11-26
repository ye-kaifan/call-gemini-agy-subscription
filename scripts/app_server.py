"""Small stdio client for Codex's history-reading app-server methods."""
import json
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import time


READ_METHODS = {"thread/list", "thread/read", "thread/items/list", "thread/turns/list"}
FRAME_LIMIT = 32 * 1024 * 1024


class RpcError(ValueError):
    def __init__(self, method, error):
        self.code = error.get("code")
        self.message = str(error.get("message", error))
        super().__init__(f"Codex {method} 失败（{self.code}）：{self.message[:600]}")


def codex_executable():
    override = os.environ.get("CODEX_HISTORY_BIN")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute() or not path.is_file() or not os.access(path, os.X_OK):
            raise ValueError("CODEX_HISTORY_BIN 须为可执行 Codex CLI 的绝对路径")
        return str(path.resolve())
    search = os.pathsep.join(p for p in os.get_exec_path() if os.path.isabs(p))
    executable = shutil.which("codex", path=search)
    if not executable:
        raise ValueError("历史查询需要 Codex CLI；请安装 codex 或设置 CODEX_HISTORY_BIN")
    return os.path.realpath(executable)


class AppServer:
    """One lazily started process per reader; no thread resume or model turns."""
    def __init__(self, home, *, executable=None, timeout=20):
        self.home = Path(home).expanduser().resolve()
        self.executable = executable
        self.timeout = timeout
        self.proc = None
        self.selector = None
        self.buffer = b""
        self.stderr = b""
        self.sequence = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def start(self):
        if self.proc is not None:
            return
        if not self.home.is_dir():
            raise ValueError("Codex 历史目录不存在：" + str(self.home))
        self.proc = subprocess.Popen(
            [self.executable or codex_executable(), "app-server", "--listen", "stdio://"],
            cwd=self.home, env=dict(os.environ, CODEX_HOME=str(self.home)),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
        )
        self.selector = selectors.DefaultSelector()
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            os.set_blocking(stream.fileno(), False)
        self.selector.register(self.proc.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(self.proc.stderr, selectors.EVENT_READ, "stderr")
        try:
            result = self._exchange("initialize", {
                "clientInfo": {"name": "gemini_codex_history", "version": "3.0.0"},
            })
            actual = result.get("codexHome")
            if actual is not None and Path(actual).resolve() != self.home:
                raise ValueError("Codex app-server 返回的历史目录与请求不一致")
            self._send({"method": "initialized"}, time.monotonic() + self.timeout)
        except BaseException:
            self.close()
            raise

    def request(self, method, params):
        if method not in READ_METHODS:
            raise ValueError("历史服务仅允许会话查询接口")
        self.start()
        try:
            return self._exchange(method, params)
        except RpcError:
            raise
        except BaseException:
            self.close()
            raise

    def _send(self, message, deadline):
        data = memoryview((json.dumps(message, ensure_ascii=False) + "\n").encode())
        self.selector.register(self.proc.stdin, selectors.EVENT_WRITE, "stdin")
        try:
            while data:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("Codex app-server 发送请求超时")
                for key, _ in self.selector.select(remaining):
                    if key.data == "stdin":
                        try:
                            data = data[os.write(key.fd, data):]
                        except BlockingIOError:
                            pass
                    else:
                        self._receive(key)
        finally:
            self.selector.unregister(self.proc.stdin)

    def _receive(self, key):
        try:
            chunk = os.read(key.fd, 65536)
        except BlockingIOError:
            return
        if not chunk:
            self.selector.unregister(key.fileobj)
            if key.data == "stdout":
                detail = self.stderr.decode(errors="replace")[-800:]
                raise ValueError("Codex app-server 连接已关闭：" + detail)
            return
        if key.data == "stderr":
            self.stderr = (self.stderr + chunk)[-65536:]
        else:
            self.buffer += chunk
            if len(self.buffer) > FRAME_LIMIT:
                raise ValueError("Codex app-server 单条响应超过 32 MiB，请缩小查询范围")

    def _exchange(self, method, params):
        self.sequence += 1
        ident = self.sequence
        deadline = time.monotonic() + self.timeout
        self._send({"id": ident, "method": method, "params": params}, deadline)
        while True:
            while b"\n" in self.buffer:
                line, self.buffer = self.buffer.split(b"\n", 1)
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    raise ValueError("Codex app-server 返回无效 JSON") from None
                if not isinstance(message, dict):
                    raise ValueError("Codex app-server 响应不是对象")
                if "method" in message:
                    if "id" in message:
                        self._send({"id": message["id"], "error": {
                            "code": -32601, "message": "History client does not handle server requests",
                        }}, deadline)
                    continue  # Notifications do not carry query results.
                if message.get("id") != ident:
                    raise ValueError("Codex app-server 响应 ID 不匹配")
                if isinstance(message.get("error"), dict):
                    raise RpcError(method, message["error"])
                result = message.get("result")
                if not isinstance(result, dict):
                    raise ValueError("Codex app-server 缺少有效 result")
                return result
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(f"Codex {method} 查询超时")
            for key, _ in self.selector.select(remaining):
                self._receive(key)

    def close(self):
        if self.proc is None:
            return
        proc, self.proc = self.proc, None
        if self.selector is not None:
            self.selector.close()
            self.selector = None
        proc.stdin.close()
        try:
            proc.wait(timeout=1)
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                proc.wait(timeout=1)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        finally:
            proc.stdout.close()
            proc.stderr.close()
            self.buffer = b""
