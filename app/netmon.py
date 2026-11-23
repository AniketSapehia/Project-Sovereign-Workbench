"""
app/netmon.py — the Network Monitor.

This file is our PROOF of sovereignty. The problem statement says the system
must show "through logs or a visible network monitor, that no external calls
are made at any point". We do that in TWO independent ways:

  Layer 1 — Application log
      Every HTTP request our own Python code sends is recorded here
      (who it went to, when, and whether it was allowed).
      This answers: "what did the app TRY to contact?"

  Layer 2 — Operating-system scan
      We ask Windows itself (via the `psutil` library) for every open network
      connection belonging to our Python process AND to Ollama.
      This answers: "what is ACTUALLY connected right now?"
      This layer does not trust our own code — even a hidden library call
      would show up here, because Windows sees all connections.

A connection counts as "local" only if the other end is a loopback address
(127.x.x.x or ::1), i.e. this same machine. Anything else is flagged
"EXTERNAL" and shown in red on the web page.
"""

import ipaddress           # to check whether an IP address is loopback
import os                  # to get the ID number of our own process
import threading           # a lock, so two requests don't edit the log at once
from collections import deque   # a list that automatically drops old items
from datetime import datetime
from urllib.parse import urlparse

import psutil              # cross-platform library to inspect processes/sockets

import config


# ---------------------------------------------------------------------------
# Layer 1: the application request log
# ---------------------------------------------------------------------------

# deque(maxlen=200) keeps only the newest 200 entries, so memory never grows.
_request_log = deque(maxlen=200)
_log_lock = threading.Lock()

# Running totals since the app started (shown as counters on the page).
_totals = {"allowed": 0, "blocked": 0}


def is_allowed_host(host: str) -> bool:
    """Return True if `host` is on our allow-list (this machine only)."""
    return host in config.ALLOWED_HOSTS


def record_request(method: str, url: str) -> bool:
    """
    Write one outgoing request into the log and return whether it is allowed.

    Called by app/llm.py just BEFORE any request leaves our code.
    """
    host = urlparse(url).hostname or ""
    allowed = is_allowed_host(host)

    now = datetime.now().strftime("%H:%M:%S")
    entry = {
        "time": now,           # when this request (or the last repeat) happened
        "first_time": now,     # when the first request of a repeated group happened
        "count": 1,            # how many identical requests in a row
        "method": method,
        # We only store the host and path, never the request body,
        # so the log itself never contains confidential text.
        "target": f"{host}{urlparse(url).path}",
        "allowed": allowed,
    }

    with _log_lock:                    # only one thread may write at a time
        newest = _request_log[0] if _request_log else None

        # GROUPING (Phase 2.1): the page checks model status every 15 s,
        # which used to fill the log with identical "GET /api/tags" lines
        # and push the interesting ones (embed, chat) out of view.
        # If this request is identical to the newest entry, we increase that
        # entry's counter instead of adding a new line. NOTHING is hidden:
        # every request is still counted, and a different request (or a
        # blocked one) always starts a new line.
        if (newest is not None
                and newest["method"] == entry["method"]
                and newest["target"] == entry["target"]
                and newest["allowed"] == entry["allowed"]):
            newest["count"] += 1
            newest["time"] = now
        else:
            _request_log.appendleft(entry)  # newest first

        _totals["allowed" if allowed else "blocked"] += 1

    return allowed


# ---------------------------------------------------------------------------
# Layer 2: the operating-system connection scan
# ---------------------------------------------------------------------------

def _is_loopback(ip: str) -> bool:
    """True if `ip` points back to this same computer."""
    try:
        return ipaddress.ip_address(ip).is_loopback
    except ValueError:
        # Not a valid IP (should not happen) — treat as NOT local, to be safe.
        return False


def _watched_processes() -> dict:
    """
    Find the processes we want to watch. Returns {process_id: process_name}.

    - Always our own Python process (os.getpid()).
    - Its child processes (the Phase 3 sandbox).
    - Every process whose name contains a keyword from config,
      e.g. "ollama.exe" and "ollama app.exe".
    """
    watched = {os.getpid(): "workbench (python)"}

    # Phase 3: the Python sandbox runs AI-written code in a CHILD process of
    # the workbench. We watch those too, so even code the AI wrote is
    # covered by the monitor.
    try:
        for child in psutil.Process(os.getpid()).children(recursive=True):
            watched[child.pid] = "sandbox (python)"
    except psutil.Error:
        pass

    for proc in psutil.process_iter(["pid", "name"]):
        name = (proc.info.get("name") or "").lower()
        if any(word in name for word in config.WATCHED_PROCESS_KEYWORDS):
            watched[proc.info["pid"]] = proc.info["name"]

    return watched


def scan_connections() -> list:
    """
    Ask the operating system for every open internet connection, keep only
    those belonging to watched processes, and label each local/external.
    """
    watched = _watched_processes()
    results = []

    try:
        # kind="inet" = IPv4 and IPv6 connections (TCP and UDP).
        all_connections = psutil.net_connections(kind="inet")
    except psutil.AccessDenied:
        # On some systems this needs admin rights. We report it honestly
        # rather than pretending everything is fine.
        return [{"error": "Permission denied while scanning connections. "
                          "Try running the terminal as Administrator."}]

    for conn in all_connections:
        if conn.pid not in watched:
            continue                  # belongs to some other program — skip

        # A "listening" socket has no remote end yet (it is waiting for
        # someone to connect, e.g. Ollama waiting on port 11434).
        if not conn.raddr:
            if conn.status == psutil.CONN_LISTEN:
                local_ip = conn.laddr.ip
                results.append({
                    "process": watched[conn.pid],
                    "local": f"{conn.laddr.ip}:{conn.laddr.port}",
                    "remote": "(listening)",
                    "status": "LISTEN",
                    # Listening only on loopback = unreachable from outside.
                    "external": not _is_loopback(local_ip),
                })
            continue

        remote_ip = conn.raddr.ip
        results.append({
            "process": watched[conn.pid],
            "local": f"{conn.laddr.ip}:{conn.laddr.port}",
            "remote": f"{remote_ip}:{conn.raddr.port}",
            "status": conn.status,
            "external": not _is_loopback(remote_ip),
        })

    return results


# ---------------------------------------------------------------------------
# One combined report for the web page
# ---------------------------------------------------------------------------

def get_report() -> dict:
    """Bundle both layers into one dictionary that the web page displays."""
    connections = scan_connections()
    external = [c for c in connections if c.get("external")]

    with _log_lock:
        # dict(e) makes a copy of each entry, so the page never sees an
        # entry that is half-way through being updated.
        log_copy = [dict(e) for e in _request_log]
        totals_copy = dict(_totals)

    return {
        "checked_at": datetime.now().strftime("%H:%M:%S"),
        "requests": log_copy,
        "request_totals": totals_copy,
        "connections": connections,
        "external_connection_count": len(external),
        # The single headline answer: is anything leaving the machine?
        "sovereign": len(external) == 0 and totals_copy["blocked"] == 0,
    }
