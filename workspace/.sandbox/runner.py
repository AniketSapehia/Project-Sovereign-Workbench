
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
