# browser.py --> headless chrome we can drive, over the devtools protocol on a pipe
#
# --remote-debugging-pipe: chrome reads commands on fd 3 and writes replies on fd 4, each one a
# json message ending in \0. no debugging port to collide with, no websocket library needed.
import contextlib
import json
import os
import shlex
import shutil
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

# chrome for testing / chromium / google chrome, whichever is around
CANDIDATES = [
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]


class BrowserError(RuntimeError):
    pass


def find_chrome(explicit: str | None = None) -> str:
    for candidate in [explicit, os.environ.get("RTPSCOPE_CHROME"), *CANDIDATES]:
        if not candidate:
            continue
        path = shutil.which(candidate) or (candidate if Path(candidate).is_file() else None)
        if path:
            return path
    raise BrowserError(
        "no chrome/chromium found. install one, or point RTPSCOPE_CHROME (or --chrome) at it"
    )


def chrome_flags(user_data_dir: str, headless: bool = True) -> list[str]:
    flags = [
        "--remote-debugging-pipe",
        f"--user-data-dir={user_data_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--mute-audio",
        "--window-size=1280,800",
        # media without a person clicking "allow" --> chrome's fake mic + camera
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        "--autoplay-policy=no-user-gesture-required",
        # a hidden/headless tab must keep sending 30 fps, not get throttled
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        # real host ips in candidates instead of .local names --> no mdns needed, and the
        # candidate pair in the csv says which address the media actually used
        "--disable-features=WebRtcHideLocalIpsWithMdns",
        "--disable-dev-shm-usage",
    ]
    if headless:
        flags.append("--headless=new")
    # chrome refuses to start as root with its sandbox on (docker, ci, some vms)
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        flags.append("--no-sandbox")
    return flags


class _Process:
    """just enough of Popen for a posix_spawn'd child (spawn places fds without forking python)."""

    def __init__(self, pid: int):
        self.pid = pid
        self.returncode: int | None = None

    @classmethod
    def spawn(cls, argv: list[str], file_actions: list[tuple]) -> "_Process":
        # own process group --> ctrl-c in the terminal goes to us, and we close chrome cleanly
        # ourselves (setpgroup works on glibc and macos, setsid doesn't everywhere)
        try:
            pid = os.posix_spawn(
                argv[0], argv, dict(os.environ), file_actions=file_actions, setpgroup=0
            )
        except OSError as err:
            raise BrowserError(f"couldn't start {argv[0]}: {err}") from err
        return cls(pid)

    def poll(self) -> int | None:
        if self.returncode is None:
            try:
                pid, status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                self.returncode = -1
                return self.returncode
            if pid:
                self.returncode = os.waitstatus_to_exitcode(status)
        return self.returncode

    def wait(self, timeout_s: float) -> bool:
        deadline = time.monotonic() + timeout_s
        while self.poll() is None:
            if time.monotonic() > deadline:
                return False
            time.sleep(0.05)
        return True

    def kill(self) -> None:
        if self.poll() is None:
            # chrome leads its own process group --> take its gpu/renderer children down too
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(self.pid, signal.SIGKILL)


class Chrome:
    """one chrome process, one tab. use as a context manager so it always gets killed."""

    def __init__(
        self,
        name: str,
        binary: str | None = None,
        headless: bool = True,
        log_path: Path | None = None,
        timeout_s: float = 15,
        extra_args: list[str] | None = None,
    ):
        if sys.platform == "win32":
            raise BrowserError("the runner needs linux or macos (it talks to chrome over a pipe)")
        self.name = name
        self.binary = find_chrome(binary)
        self.headless = headless
        self.log_path = log_path
        self.extra_args = list(extra_args or [])
        self.timeout_s = timeout_s
        self.session: str | None = None
        self._profile = tempfile.mkdtemp(prefix=f"rtpscope-{name}-")
        self._next_id = 0
        self._lock = threading.Lock()
        self._pending: dict[int, dict[str, Any]] = {}
        self._waiters: dict[int, threading.Event] = {}
        self._closed = threading.Event()
        self.console: list[str] = []
        self.proc: _Process | None = None

    # ---- process

    def start(self) -> "Chrome":
        import fcntl  # posix only --> imported here so the module still loads on windows

        # parent writes commands into cmd_write, reads replies from res_read
        cmd_read, cmd_write = os.pipe()
        res_read, res_write = os.pipe()
        argv = [self.binary, *chrome_flags(self._profile, self.headless), *self.extra_args]
        log_fd = os.open(self.log_path or os.devnull, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        # move chrome's ends above 9 first --> placing them on 3 and 4 can't clobber each other
        high_read = fcntl.fcntl(cmd_read, fcntl.F_DUPFD, 10)
        high_write = fcntl.fcntl(res_write, fcntl.F_DUPFD, 10)
        try:
            self.proc = _Process.spawn(
                argv,
                [
                    (os.POSIX_SPAWN_OPEN, 0, os.devnull, os.O_RDONLY, 0),
                    (os.POSIX_SPAWN_DUP2, log_fd, 1),
                    (os.POSIX_SPAWN_DUP2, log_fd, 2),
                    (os.POSIX_SPAWN_DUP2, high_read, 3),
                    (os.POSIX_SPAWN_DUP2, high_write, 4),
                ],
            )
        finally:
            for fd in (cmd_read, res_write, high_read, high_write, log_fd):
                os.close(fd)
        self._out = os.fdopen(cmd_write, "wb", buffering=0)
        self._in = os.fdopen(res_read, "rb", buffering=0)
        threading.Thread(target=self._read_loop, name=f"cdp-{self.name}", daemon=True).start()
        try:
            version = self.call("Browser.getVersion")
        except BrowserError as err:
            self.close()
            raise BrowserError(
                f"chrome ({self.binary}) didn't start: {err}. "
                f"command: {shlex.join(argv)}"
                + (f" -- see {self.log_path}" if self.log_path else "")
            ) from err
        self.version = version.get("product", "")
        target = self.call("Target.createTarget", {"url": "about:blank"})
        attached = self.call(
            "Target.attachToTarget", {"targetId": target["targetId"], "flatten": True}
        )
        self.session = attached["sessionId"]
        self.page("Page.enable")
        self.page("Runtime.enable")
        return self

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            with contextlib.suppress(Exception):
                self.call("Browser.close", timeout_s=3)
            if not self.proc.wait(timeout_s=5):
                self.proc.kill()
                self.proc.wait(timeout_s=5)
        for stream in ("_out", "_in"):
            with contextlib.suppress(Exception):
                getattr(self, stream).close()
        shutil.rmtree(self._profile, ignore_errors=True)

    def __enter__(self) -> "Chrome":
        return self.start()

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- protocol

    def _read_loop(self) -> None:
        buffer = b""
        try:
            while chunk := self._in.read(65536):
                buffer += chunk
                while b"\0" in buffer:
                    raw, buffer = buffer.split(b"\0", 1)
                    self._dispatch(json.loads(raw))
        except (OSError, ValueError):
            pass
        finally:
            self._closed.set()
            with self._lock:
                for event in self._waiters.values():
                    event.set()

    def _dispatch(self, message: dict[str, Any]) -> None:
        if "id" in message:
            with self._lock:
                self._pending[message["id"]] = message
                waiter = self._waiters.get(message["id"])
            if waiter:
                waiter.set()
            return
        # page console errors are handy when a join fails
        if message.get("method") == "Runtime.exceptionThrown":
            details = message["params"]["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text", "")
            self.console.append(f"exception: {text}")
        elif message.get("method") == "Runtime.consoleAPICalled":
            params = message["params"]
            if params.get("type") in ("error", "warning"):
                args = " ".join(
                    str(a.get("value", a.get("description", ""))) for a in params["args"]
                )
                self.console.append(f"{params['type']}: {args}")

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        session: str | None = None,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        if self._closed.is_set():
            raise BrowserError(f"{self.name}: chrome is gone")
        with self._lock:
            self._next_id += 1
            msg_id = self._next_id
            event = self._waiters[msg_id] = threading.Event()
        message: dict[str, Any] = {"id": msg_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        try:
            self._out.write(json.dumps(message).encode() + b"\0")
        except OSError as err:
            raise BrowserError(f"{self.name}: can't talk to chrome ({err})") from err
        event.wait(timeout_s or self.timeout_s)
        with self._lock:
            self._waiters.pop(msg_id, None)
            reply = self._pending.pop(msg_id, None)
        if reply is None:
            why = "chrome exited" if self._closed.is_set() else "timed out"
            raise BrowserError(f"{self.name}: {method} {why}")
        if "error" in reply:
            raise BrowserError(f"{self.name}: {method}: {reply['error'].get('message')}")
        return reply.get("result", {})

    def page(self, method: str, params: dict[str, Any] | None = None, **kw: Any) -> dict[str, Any]:
        return self.call(method, params, session=self.session, **kw)

    def evaluate(self, expression: str, timeout_s: float | None = None) -> Any:
        """run js in the tab and return its (json-able) value; promises are awaited."""
        result = self.page(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            timeout_s=timeout_s,
        )
        if "exceptionDetails" in result:
            details = result["exceptionDetails"]
            text = details.get("exception", {}).get("description") or details.get("text")
            raise BrowserError(f"{self.name}: js error: {text}")
        return result.get("result", {}).get("value")

    def wait_for(self, expression: str, timeout_s: float, what: str) -> Any:
        deadline = time.monotonic() + timeout_s
        while True:
            value = self.evaluate(expression)
            if value:
                return value
            if time.monotonic() > deadline:
                raise BrowserError(f"{self.name}: timed out waiting for {what}")
            time.sleep(0.2)
