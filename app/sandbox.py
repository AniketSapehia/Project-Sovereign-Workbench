"""
app/sandbox.py — running Python code written by the AI, safely.

The problem statement asks for "code execution in a sandbox" and "a coding
task run and verified in a sandbox". We also use the sandbox for every
calculation, because code does arithmetic reliably and small models don't.

How the sandbox protects the computer (four layers):

  1. SEPARATE PROCESS
     The code runs in a new Python process, never inside the web server.
     If it crashes or hangs, the workbench is unaffected.

  2. TIME LIMIT
     The process is killed after SANDBOX_TIMEOUT_SECONDS (config.py), so an
     endless loop can't freeze the laptop.

  3. AUDIT HOOK (the main guard)
     Python 3.8+ lets a program install an "audit hook": a function that
     Python calls BEFORE sensitive actions (opening a file, creating a
     network socket, starting a program...). Our hook raises an error to
     block:
        - any network access              (sovereignty)
        - starting other programs         (no "format C:" via os.system)
        - writing, deleting or renaming files OUTSIDE the workspace folder
     Once installed, an audit hook cannot be removed by the code it watches.

  4. CLEAN ENVIRONMENT
     Python starts in "isolated mode" (-I): it ignores environment settings
     and the user's personal packages. Proxy settings are not passed on.
     Its temp folder is inside the workspace.

Honest limitation (say this in the viva): this is a solid prototype sandbox,
not a security boundary against a determined attacker — very low-level
tricks could get around a Python-level hook. In production, the same code
would run inside a locked-down container or virtual machine with no network
card at all. The interface (run code -> get output) stays the same.
"""

import subprocess
import sys
import time
import uuid
from pathlib import Path

import config

# ---------------------------------------------------------------------------
# The runner: a tiny script that installs the audit hook, then runs the
# AI's code. It is written into the workspace each time the sandbox runs.
# ---------------------------------------------------------------------------
RUNNER_SOURCE = r'''
import os, sys

# Folder the code may write in (passed by the workbench as argument 1).
_ROOT = os.path.normcase(os.path.realpath(sys.argv[1]))

# Flags that mean "this file is being opened for writing".
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_CREAT | os.O_TRUNC

# Actions that are always refused.
_BLOCKED = {
    # network
    "socket.__new__", "socket.connect", "socket.bind", "socket.getaddrinfo",
    "socket.gethostbyname", "socket.sendto", "socket.sendmsg",
    "urllib.Request", "http.client.connect", "webbrowser.open",
    # starting other programs
    "subprocess.Popen", "os.system", "os.exec", "os.spawn", "os.posix_spawn",
    "os.startfile", "os.fork", "os.forkpty", "os.kill", "os.killpg",
    "_winapi.CreateProcess",
    # calling raw operating-system functions through ctypes
    "ctypes.call_function",
}

# Actions on paths that are allowed only INSIDE the workspace.
_PATH_EVENTS = {
    "os.remove", "os.rename", "os.replace", "os.rmdir", "os.mkdir",
    "os.chmod", "os.chown", "os.symlink", "os.link", "os.truncate",
    "os.utime", "shutil.rmtree", "shutil.move",
}


def _inside(path):
    """True if `path` is the workspace folder or something inside it."""
    try:
        p = os.path.normcase(os.path.realpath(os.fspath(path)))
    except Exception:
        return False
    return p == _ROOT or p.startswith(_ROOT + os.sep)


def _deny(what):
    raise PermissionError(f"Sandbox: {what} is not allowed")


def _hook(event, args):
    if event in _BLOCKED:
        if event.startswith(("socket", "urllib", "http", "webbrowser")):
            _deny("network access")
        _deny(f"'{event}'")
    if event == "open":
        path, _mode, flags = args
        if isinstance(path, int) or path is None:
            return                       # an already-open file handle
        if flags & _WRITE_FLAGS and not _inside(path):
            _deny(f"writing outside the workspace ({path})")
    elif event in _PATH_EVENTS:
        for value in args:
            if isinstance(value, (str, bytes, os.PathLike)) and not _inside(value):
                _deny(f"changing files outside the workspace ({value})")


sys.addaudithook(_hook)

# Run the AI's code (argument 2) with the workspace as current folder.
os.chdir(_ROOT)
_job = sys.argv[2]
with open(_job, encoding="utf-8") as _f:
    _code = _f.read()
del _f

import ast

# Like a Jupyter notebook: if the LAST line is just a value (e.g. "t_life"),
# show it even without print(). We split it off and evaluate it separately.
_tree = ast.parse(_code, "ai_code.py")
_last = None
if _tree.body and isinstance(_tree.body[-1], ast.Expr):
    _last = ast.Expression(_tree.body.pop().value)

_ns = {"__name__": "__main__"}
exec(compile(_tree, "ai_code.py", "exec"), _ns)
if _last is not None:
    _value = eval(compile(_last, "ai_code.py", "eval"), _ns)
    if _value is not None:
        print(repr(_value))

# Phase 3.1: list every simple variable and its value at the end. Small
# models often forget print(); this way the results (and any wrong value,
# such as a mixed-up variable) are always visible to the user and the model.
_shown = []
for _name, _val in _ns.items():
    if _name.startswith("_") or isinstance(_val, bool):
        continue
    if isinstance(_val, float):
        # Phase 6.9: round(1e-9, 6) printed "0.0", which hid a tolerance value
        # in testing. Very small numbers are now shown in scientific notation.
        if _val != 0 and abs(_val) < 1e-4:
            _shown.append(f"  {_name} = {_val:.3g}")
        else:
            _shown.append(f"  {_name} = {round(_val, 6)}")
    elif isinstance(_val, int) or (isinstance(_val, str) and len(_val) <= 80):
        _shown.append(f"  {_name} = {_val!r}")
if _shown:
    print("\n[Variables after running]")
    print("\n".join(_shown[:40]))
'''


def _list_files(folder: Path) -> set:
    """All files in the workspace, ignoring the sandbox's own scratch folder."""
    return {p.relative_to(folder).as_posix()
            for p in folder.rglob("*")
            if p.is_file() and ".sandbox" not in p.parts}


def run_python(code: str) -> dict:
    """
    Run `code` in the sandbox and return what happened:
        {"ok", "exit_code", "output", "timed_out", "seconds", "new_files"}
    `output` is everything the code printed (stdout + errors).
    """
    workspace = config.WORKSPACE_DIR
    scratch = workspace / ".sandbox"          # runner + job files live here
    temp = scratch / "tmp"                    # the code's temp folder
    temp.mkdir(parents=True, exist_ok=True)

    runner = scratch / "runner.py"
    runner.write_text(RUNNER_SOURCE, encoding="utf-8")
    job = scratch / f"job_{uuid.uuid4().hex[:8]}.py"
    job.write_text(code, encoding="utf-8")

    # A minimal environment: Windows needs SYSTEMROOT for some basics;
    # everything else (including proxy settings) is left out on purpose.
    import os
    env = {k: v for k, v in os.environ.items()
           if k.upper() in {"SYSTEMROOT", "WINDIR", "PATH"}}
    env["TEMP"] = env["TMP"] = env["TMPDIR"] = str(temp)

    before = _list_files(workspace)
    started = time.time()
    timed_out = False
    try:
        result = subprocess.run(
            # -I isolated mode, -B don't write .pyc files, -X utf8 = UTF-8 text
            [sys.executable, "-I", "-B", "-X", "utf8", str(runner), str(workspace), str(job)],
            cwd=str(workspace),
            env=env,
            capture_output=True,
            timeout=config.SANDBOX_TIMEOUT_SECONDS,
            # On Windows, don't flash a black console window.
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        output = (result.stdout + result.stderr).decode("utf-8", errors="replace")
        exit_code = result.returncode
    except subprocess.TimeoutExpired as error:
        timed_out = True
        partial = (error.stdout or b"") + (error.stderr or b"")
        output = partial.decode("utf-8", errors="replace") + \
            f"\n[Stopped: the code ran longer than {config.SANDBOX_TIMEOUT_SECONDS} seconds]"
        exit_code = -1
    finally:
        job.unlink(missing_ok=True)

    # Make tracebacks point at "ai_code.py" lines only, hiding our runner.
    output = output.replace(str(runner), "sandbox_runner.py")
    if len(output) > config.SANDBOX_MAX_OUTPUT_CHARS:
        output = output[:config.SANDBOX_MAX_OUTPUT_CHARS] + "\n[... output cut short ...]"

    return {
        "ok": exit_code == 0 and not timed_out,
        "exit_code": exit_code,
        "output": output.strip() or "(the code printed nothing — use print() to show results)",
        "timed_out": timed_out,
        "seconds": round(time.time() - started, 2),
        "new_files": sorted(_list_files(workspace) - before),
    }
