"""runlog.py - one plain-text trace of everything a run does.

`runs/<session>/transcript.jsonl` is the machine record of the agent loop and
`steps.log` is the pipeline's one-line-per-milestone tally. Neither is what you
want at 2am when a run went sideways: the first is JSON with the reasoning and
the HTTP layer missing, the second is eight lines. This writes the third thing -
`runs/<session>/run.log`, a chronological, greppable text trace where every line
is timestamped and every payload is present in full.

What lands in it:

  * everything printed to the terminal, verbatim, ANSI stripped (the console is
    teed, so nothing that reaches a human is missing from the file);
  * the HTTP layer the console never shows - request size, latency, token usage,
    finish_reason, every retry and why it fired;
  * every tool call with its full arguments and its full result, timed;
  * approvals, guardrail refusals, compactions, reconnect waits, tracebacks.

Two things it deliberately does NOT do. It does not buffer indefinitely - the
file is opened line-buffered and flushed per event, because the trace matters
most for the run that died mid-turn. And it does not log secrets or base64
image payloads: redact() strips Authorization headers, key-shaped values and
data: URIs, which also keeps a two-image vision turn from writing 3MB of base64.

Events before the run folder exists (argument parsing, preflight) are held in
memory and flushed on attach(), so the file starts at the true beginning of the
run rather than at the first moment it had somewhere to live.
"""

from __future__ import annotations

import atexit
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_DATA_URI = re.compile(r"(data:[a-z]+/[a-z0-9.+-]+;base64,)([A-Za-z0-9+/=]+)")
_LONG_B64 = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/=]{300,}")
_BEARER = re.compile(r"(Bearer\s+)[^\s\"']+", re.I)
_KEYISH = re.compile(
    r"((?:api[_-]?key|apikey|access[_-]?token|auth[_-]?token|password|secret|"
    r"fal_key|qwen_api_key|refmatch_api_key)[\"']?\s*[:=]\s*[\"']?)"
    r"([^\s\"',}]{6,})",
    re.I,
)


def strip_ansi(s: str) -> str:
    return _ANSI.sub("", s)


def redact(text: str) -> str:
    """Remove secrets and base64 blobs, keeping a note of what was there.

    Image payloads are the volume problem, not the secrecy one: a single
    compare_images turn carries two ~700KB base64 strings, and writing those to
    the trace makes it unreadable and unusably large. Both are replaced by their
    size, which is the only part anyone debugging ever wanted.
    """
    if not text:
        return text
    text = _DATA_URI.sub(lambda m: f"{m.group(1)}<{len(m.group(2))} b64 chars elided>", text)
    text = _LONG_B64.sub(lambda m: f"<{len(m.group(0))} b64 chars elided>", text)
    text = _BEARER.sub(r"\1<redacted>", text)
    text = _KEYISH.sub(r"\1<redacted>", text)
    return text


# ---------------------------------------------------------------------------
# Trace
# ---------------------------------------------------------------------------

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}

# Per block, not per file. Full tool output is already spooled verbatim to
# runs/<session>/tool_output/, so the trace can afford a ceiling; without one a
# single `find /` in bash writes a hundred megabytes here.
MAX_BLOCK = 200_000


class Trace:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.level = LEVELS["DEBUG"]
        self.enabled = True
        self._fh = None
        self._pending: list[str] = []
        self._lock = threading.RLock()
        self._t0 = time.time()
        self._teed = False
        self._real_stdout = None
        self._real_stderr = None
        self._console_comp = "console"
        self._closed = False

    # -- lifecycle ---------------------------------------------------------
    def attach(self, path: Path, level: str = "DEBUG", header: dict | None = None) -> Path:
        """Open the trace file and flush everything buffered so far."""
        with self._lock:
            self.path = Path(path)
            self.level = LEVELS.get(str(level).upper(), LEVELS["DEBUG"])
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._fh = self.path.open("a", buffering=1, encoding="utf-8", errors="replace")
            started = datetime.fromtimestamp(self._t0).strftime("%Y-%m-%d %H:%M:%S")
            self._fh.write(f"\n{'=' * 78}\n")
            self._fh.write(f"# run.log - full trace, started {started}\n")
            for k, v in (header or {}).items():
                self._fh.write(f"#   {k:<12} {redact(str(v))}\n")
            self._fh.write(f"{'=' * 78}\n")
            for line in self._pending:
                self._fh.write(line)
            self._pending.clear()
            self._fh.flush()
        atexit.register(self.close)
        return self.path

    def close(self, footer: str | None = None) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self.untee()
            if self._fh:
                if footer:
                    self._fh.write(f"# {footer}\n")
                self._fh.write(f"# trace closed after {time.time() - self._t0:.1f}s\n")
                self._fh.flush()
                self._fh.close()
                self._fh = None

    # -- writing -----------------------------------------------------------
    def _emit(self, text: str) -> None:
        with self._lock:
            if self._fh:
                self._fh.write(text)
            elif self.enabled:
                # Pre-attach. Bounded so a preflight that loops forever cannot
                # eat memory on the way to a file it never gets.
                if len(self._pending) < 5000:
                    self._pending.append(text)

    def event(self, level: str, comp: str, msg: str, body: str | None = None,
              **fields) -> None:
        if not self.enabled or LEVELS.get(level, 10) < self.level:
            return
        now = time.time()
        stamp = datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S.") + \
            f"{int((now % 1) * 1000):03d}"
        extra = "".join(f" {k}={_fmt_field(v)}" for k, v in fields.items()
                        if v is not None)
        line = (f"{stamp} | +{now - self._t0:7.1f}s | {level:<5} | {comp:<9} | "
                f"{redact(strip_ansi(str(msg)))}{redact(extra)}\n")
        if body:
            line += _block(body)
        self._emit(line)

    def debug(self, comp, msg, body=None, **f): self.event("DEBUG", comp, msg, body, **f)
    def info(self, comp, msg, body=None, **f): self.event("INFO", comp, msg, body, **f)
    def warn(self, comp, msg, body=None, **f): self.event("WARN", comp, msg, body, **f)
    def error(self, comp, msg, body=None, **f): self.event("ERROR", comp, msg, body, **f)

    def exception(self, comp: str, msg: str) -> None:
        self.event("ERROR", comp, msg, body=traceback.format_exc())

    def rule(self, title: str) -> None:
        self._emit(f"\n{'-' * 78}\n-- {title}\n{'-' * 78}\n")

    @contextmanager
    def timed(self, comp: str, msg: str, level: str = "DEBUG", **fields):
        """Log a start line, then an end line carrying the elapsed time."""
        self.event(level, comp, f"{msg} - start", **fields)
        t0 = time.time()
        try:
            yield
        except BaseException as e:
            self.event("ERROR", comp, f"{msg} - failed after {time.time() - t0:.2f}s",
                       body=traceback.format_exc(), error=f"{type(e).__name__}: {e}")
            raise
        else:
            self.event(level, comp, f"{msg} - done", secs=round(time.time() - t0, 2))

    # -- console capture ---------------------------------------------------
    def tee(self) -> None:
        """Mirror stdout/stderr into the trace.

        Everything the run prints - the banner, the model's own text, the
        progress lines, a child process writing through us - lands in the file
        with a timestamp, so the trace is a superset of the terminal rather
        than a parallel account of it.
        """
        if self._teed:
            return
        self._real_stdout, self._real_stderr = sys.stdout, sys.stderr
        sys.stdout = _Tee(sys.stdout, self, lambda: self._console_comp)
        sys.stderr = _Tee(sys.stderr, self, lambda: "stderr")
        self._teed = True

    def untee(self) -> None:
        if not self._teed:
            return
        for s in (sys.stdout, sys.stderr):
            if isinstance(s, _Tee):
                s.drain()
        sys.stdout, sys.stderr = self._real_stdout, self._real_stderr
        self._teed = False

    @contextmanager
    def console_component(self, comp: str):
        """Label teed console output while some sub-phase owns the terminal."""
        prev = self._console_comp
        self._console_comp = comp
        try:
            yield
        finally:
            if isinstance(sys.stdout, _Tee):
                sys.stdout.drain()
            self._console_comp = prev


def _fmt_field(v) -> str:
    s = str(v)
    if len(s) > 300:
        s = s[:300] + f"…(+{len(str(v)) - 300})"
    return f'"{s}"' if (" " in s or not s) else s


def _block(body: str) -> str:
    body = redact(strip_ansi(str(body)))
    if len(body) > MAX_BLOCK:
        body = (body[: MAX_BLOCK // 2] +
                f"\n… [{len(body) - MAX_BLOCK} chars elided from the trace] …\n" +
                body[-MAX_BLOCK // 2:])
    return "".join(f"    | {l}\n" for l in body.splitlines()) or "    | (empty)\n"


class _Tee:
    """A text stream that writes through and logs whole lines as it sees them."""

    def __init__(self, stream, trace: Trace, comp):
        self._s = stream
        self._t = trace
        self._comp = comp
        self._buf = ""

    # The harness colours output only when stdout is a terminal, and asks the
    # stream itself - so these have to delegate, or teeing silently strips
    # every colour from the console.
    def isatty(self): return self._s.isatty()
    def fileno(self): return self._s.fileno()
    def flush(self): self._s.flush()
    def writable(self): return True
    def readable(self): return False
    def seekable(self): return False

    @property
    def encoding(self): return getattr(self._s, "encoding", "utf-8")

    @property
    def errors(self): return getattr(self._s, "errors", None)

    def write(self, s: str) -> int:
        self._s.write(s)
        self._buf += s
        # \r ends a line for our purposes: progress counters rewrite in place,
        # and a trace that waited for \n would hold the whole countdown as one
        # unterminated fragment and then log it as a single line.
        while True:
            i = min((j for j in (self._buf.find("\n"), self._buf.find("\r")) if j >= 0),
                    default=-1)
            if i < 0:
                break
            self._log(self._buf[:i])
            self._buf = self._buf[i + 1:]
        return len(s)

    def writelines(self, lines):
        for l in lines:
            self.write(l)

    def drain(self) -> None:
        if self._buf:
            self._log(self._buf)
            self._buf = ""

    def _log(self, line: str) -> None:
        line = strip_ansi(line).rstrip()
        if not line.strip():
            return          # blank spacers and the \r-clear trick, not content
        comp = self._comp() if callable(self._comp) else self._comp
        self._t.event("INFO", comp, line)

    def __getattr__(self, name):
        return getattr(self._s, name)


# ---------------------------------------------------------------------------
# The one shared instance
# ---------------------------------------------------------------------------

trace = Trace()


# ---------------------------------------------------------------------------
# Subprocesses
# ---------------------------------------------------------------------------

def stream_subprocess(cmd, cwd=None, env=None, comp="subproc") -> int:
    """Run a child, mirroring its output to this terminal AND the trace.

    subprocess.run() with inherited fds writes straight to fd 1 and bypasses
    the tee entirely, which is how step 0's output stayed out of the trace. We
    read it ourselves instead - over a pty when we have a terminal, so the child
    still believes it is talking to one and keeps its colours, and over a plain
    pipe when we do not.
    """
    trace.info(comp, "spawn", cmd=" ".join(str(x) for x in cmd), cwd=str(cwd or os.getcwd()))
    t0 = time.time()
    use_pty = sys.stdout.isatty()
    try:
        if use_pty:
            rc = _stream_pty(cmd, cwd, env)
        else:
            rc = _stream_pipe(cmd, cwd, env)
    except Exception as e:
        trace.exception(comp, f"child failed to run: {type(e).__name__}: {e}")
        raise
    trace.info(comp, "exit", code=rc, secs=round(time.time() - t0, 2))
    return rc


def _stream_pty(cmd, cwd, env) -> int:
    import pty
    mfd, sfd = pty.openpty()
    try:
        p = subprocess.Popen(cmd, cwd=cwd and str(cwd), env=env, stdin=subprocess.DEVNULL,
                             stdout=sfd, stderr=sfd, close_fds=True)
    finally:
        os.close(sfd)
    try:
        while True:
            try:
                chunk = os.read(mfd, 8192)
            except OSError:         # the pty closes with the child
                break
            if not chunk:
                break
            sys.stdout.write(chunk.decode("utf-8", "replace"))
            sys.stdout.flush()
    finally:
        os.close(mfd)
    return p.wait()


def _stream_pipe(cmd, cwd, env) -> int:
    p = subprocess.Popen(cmd, cwd=cwd and str(cwd), env=env, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1, errors="replace")
    for line in p.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
    p.stdout.close()
    return p.wait()
