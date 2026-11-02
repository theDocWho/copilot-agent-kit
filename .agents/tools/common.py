"""Shared helpers: paths, config, locking, memory measurement, cost model.

Stdlib only. psutil is optional but strongly recommended (pip install psutil);
without it memory is not measured and the tools fall back to static limits.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:  # optional; memprobe has fallbacks for every OS
    import psutil  # type: ignore
except Exception:  # pragma: no cover
    psutil = None


# ---------------------------------------------------------------- paths
def find_root() -> Path:
    """Workspace root = first parent (of cwd, then of this file) holding .agents/."""
    for start in (Path.cwd(), Path(__file__).resolve().parent):
        for p in [start, *start.parents]:
            if (p / ".agents" / "config").is_dir():
                return p
    return Path(__file__).resolve().parents[2]


ROOT = find_root()
AGENTS = ROOT / ".agents"
CONFIG = AGENTS / "config"
STATE = AGENTS / "state"
CACHE = AGENTS / "cache"
for _d in (STATE, CACHE):
    _d.mkdir(parents=True, exist_ok=True)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s).timestamp()
    except ValueError:
        return None


def rel(p: Path | str) -> str:
    try:
        return Path(p).resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return str(p)


# ---------------------------------------------------------------- json io
def load_json(path: Path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError as e:
        sys.exit(f"ERROR: {rel(path)} is not valid JSON: {e}")


def save_json(path: Path, data) -> None:
    """Atomic write (temp file + rename) so readers never see half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    for _ in range(20):  # Windows may briefly lock the target
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            time.sleep(0.1)
    os.replace(tmp, path)


def append_jsonl(path: Path, obj: dict, max_bytes: int = 20_000_000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.stat().st_size > max_bytes:
            path.replace(path.with_suffix(".old.jsonl"))
    except FileNotFoundError:
        pass
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def read_jsonl(path: Path, tail: int | None = None) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    if tail:
        lines = lines[-tail:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            pass
    return out


@contextmanager
def file_lock(path: Path, timeout: float = 30.0):
    """Cross-platform lock using O_EXCL lock file. Stale locks (>120 s) are broken."""
    lock = path.with_suffix(path.suffix + ".lock")
    start = time.time()
    while True:
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            break
        except FileExistsError:
            try:
                if time.time() - lock.stat().st_mtime > 120:
                    lock.unlink(missing_ok=True)
                    continue
            except FileNotFoundError:
                continue
            if time.time() - start > timeout:
                sys.exit(f"ERROR: could not lock {rel(path)} (another agent is writing). Retry.")
            time.sleep(0.15)
    try:
        yield
    finally:
        lock.unlink(missing_ok=True)


# ---------------------------------------------------------------- config
def budget() -> dict:
    return load_json(CONFIG / "budget.json", {})


def pricing() -> dict:
    return load_json(CONFIG / "pricing.json", {})


def turn_budget_calls(b: dict | None = None) -> int:
    a = (b or budget()).get("agents", {})
    return int(a.get("max_requests_setting", 150) * a.get("turn_budget_pct", 60) / 100)


# ---------------------------------------------------------------- memory
# memprobe works without psutil (Windows ctypes / tasklist, Linux /proc, macOS ps).
sys.path.insert(0, str(Path(__file__).resolve().parent))
import memprobe  # noqa: E402


def self_tree_mb() -> float | None:
    b = memprobe.tree_bytes(os.getpid())
    return None if b is None else b / 1_048_576


def pid_tree_mb(pid: int) -> float | None:
    b = memprobe.tree_bytes(pid)
    return None if b is None else b / 1_048_576


def vscode_mb(names: list[str] | None = None) -> float | None:
    """Sum memory of all VS Code / Electron processes (renderer, extension host, helpers)."""
    names = names or budget().get("memory", {}).get("vscode_process_names", [])
    b = memprobe.names_bytes(names)
    return None if b is None else b / 1_048_576


def workers_mb() -> float | None:
    """Memory of worker processes registered in state/workers.json (alive only)."""
    reg = load_json(STATE / "workers.json", {}) or {}
    procs = memprobe.list_procs()
    if procs is None:
        return None
    live = {p for p, *_ in procs}
    total, alive = 0.0, {}
    for pid, meta in reg.items():
        if int(pid) in live:
            total += (memprobe.tree_bytes(int(pid)) or 0) / 1_048_576
            alive[pid] = meta
    if len(alive) != len(reg):
        try:
            save_json(STATE / "workers.json", alive)
        except OSError:
            pass
    return total


def register_worker(pid: int, label: str) -> None:
    with file_lock(STATE / "workers.json"):
        reg = load_json(STATE / "workers.json", {}) or {}
        reg[str(pid)] = {"label": label, "started": now_iso()}
        save_json(STATE / "workers.json", reg)


def unregister_worker(pid: int) -> None:
    with file_lock(STATE / "workers.json"):
        reg = load_json(STATE / "workers.json", {}) or {}
        reg.pop(str(pid), None)
        save_json(STATE / "workers.json", reg)


def memory_snapshot() -> dict:
    """state: OK / WARN / PAUSE, or UNMEASURED when no backend can read memory
    (then every tool falls back to the static limits in budget.json memory.static_*)."""
    m = budget().get("memory", {})
    total_budget = m.get("total_mb", 4096)
    vs = vscode_mb(m.get("vscode_process_names"))
    wk = workers_mb()
    measurable = vs is not None
    used = (vs or 0) + (wk or 0)
    pct = round(100 * used / total_budget, 1) if measurable else None
    if pct is None:
        state = "UNMEASURED"
    else:
        state = "PAUSE" if pct >= m.get("pause_at_pct", 90) else ("WARN" if pct >= m.get("resume_at_pct", 75) else "OK")
    return {
        "backend": memprobe.backend(),
        "vscode_mb": round(vs, 0) if vs is not None else None,
        "workers_mb": round(wk, 0) if wk is not None else None,
        "total_budget_mb": total_budget,
        "used_pct": pct,
        "state": state,
        "vscode_over_soft_limit": bool(vs and vs > m.get("vscode_soft_limit_mb", 2560)),
    }


def static_limits() -> dict:
    """Conservative limits used when memory cannot be measured."""
    m = budget().get("memory", {})
    return {"parallel": m.get("static_max_parallel_subagents", 2), "concurrency": m.get("static_crawl_concurrency", 2)}


# ---------------------------------------------------------------- command guard (works without hooks)
DENY_CMD = [
    (r"\brm\s+-[a-z]*r[a-z]*f?\s+(/|~|\*|\.\.?|\$HOME)/?(\s|$|;|&)", "recursive delete of root/home/cwd"),
    (r"\bRemove-Item\b.*-Recurse.*\s([A-Z]:\\?|~|\\)(\s|$)", "recursive delete of a drive/home"),
    (r"\b(del|erase|rd|rmdir)\s+/s\b", "recursive delete"),
    (r"\bgit\s+push\b.*(--force|-f)\b", "force push"),
    (r"\bgit\s+(reset\s+--hard|clean\s+-[a-z]*f)", "discarding local work"),
    (r"(^|[;&|]\s*)\s*(format\s+[a-z]:|mkfs|diskpart|shutdown|reboot|Stop-Computer|Restart-Computer)\b", "system-level command"),
    (r"(curl|wget|iwr|Invoke-WebRequest|irm)[^|]*\|\s*(sh|bash|iex|powershell|pwsh)", "piping a download into a shell"),
    (r"(echo|printf|Write-(Output|Host)|cat|type)\b.*\$(env:)?\{?\w*(TOKEN|SECRET|PASSWORD|PASSWD|COOKIE|API_?KEY|CREDENTIAL)",
     "printing a secret into the chat context"),
    (r"(^|[;&|]\s*)\s*(printenv|env|set|Get-ChildItem\s+env:|gci\s+env:|dir\s+env:)\s*($|[;&|])",
     "dumping all environment variables (may contain secrets)"),
]


def deny_reason(command: str) -> str | None:
    import re
    for pat, why in DENY_CMD:
        if re.search(pat, command, re.I | re.M):
            return why
    return None


def log_call(tool: str, detail: str = "") -> None:
    """Record a kit tool invocation (hook-free telemetry for cost.py / status.py)."""
    try:
        append_jsonl(STATE / "calls.jsonl", {"ts": now_iso(), "t": time.time(), "tool": tool, "detail": detail[:160]})
    except OSError:
        pass


# ---------------------------------------------------------------- cost model
def unit_cost(model_key: str, calls: float, work_tokens: float, out_tokens: float, pr: dict | None = None) -> dict:
    """Token + USD estimate for one agent run using the shared cost model.

    input  = calls * (base + 0.5 * work)   (context grows roughly linearly)
    of which cache_hit_ratio is billed at cache_read, the rest at cache_write
    output = out_tokens + calls * tool_call_overhead
    """
    pr = pr or pricing()
    a = pr.get("assumptions", {})
    m = pr["models"][model_key]
    base = a.get("base_context_tokens", 12000)
    hit = a.get("cache_hit_ratio", 0.75)
    overhead = a.get("tool_call_overhead_out_tokens", 120)
    calls = max(1.0, float(calls))
    inp = calls * (base + 0.5 * work_tokens)
    out = out_tokens + calls * overhead
    cached, fresh = inp * hit, inp * (1 - hit)
    usd = (cached * m["cache_read"] + fresh * m.get("cache_write", m["input"]) + out * m["output"]) / 1e6
    res = {"in_tokens": int(inp), "out_tokens": int(out), "usd": round(usd, 4),
           "credits": round(usd / pr.get("credit_usd", 0.01), 1)}
    lg = pr.get("legacy_premium_requests", {})
    if lg.get("enabled"):
        res["premium_requests"] = round(calls * lg.get("multipliers", {}).get(model_key, 1), 2)
    return res


def model_for_agent(agent: str, pr: dict | None = None) -> str:
    """Model key for cost maths. 'inherit' = the agent runs on the chat's model
    (pricing.json chat_model), which is how Planner/Reviewer avoid the subagent cost-tier error."""
    pr = pr or pricing()
    m = pr.get("agent_models", {}).get(agent, "sonnet")
    return pr.get("chat_model", "opus") if m == "inherit" else m


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 0.1 else f"${x:,.3f}"


# ---------------------------------------------------------------- shared job store
def open_job_db(job: str):
    """SQLite store shared by crawl.py, confluence.py and convert.py so batch.py can
    batch any source the same way. One row per page/part: url, file, chars, status."""
    import sqlite3
    jd = CACHE / job
    (jd / "pages").mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(jd / "crawl.sqlite", check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""CREATE TABLE IF NOT EXISTS urls(
        url TEXT PRIMARY KEY, depth INT, status TEXT, http INT, title TEXT, file TEXT,
        chars INT, err TEXT, ts TEXT)""")
    db.execute("CREATE INDEX IF NOT EXISTS ix_status ON urls(status, depth)")
    db.execute("CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)")
    db.commit()
    return db


def auth_header_from_env(var: str | None) -> str | None:
    """Authorization header built from an env var, so secrets never touch disk or chat.

    'Bearer x' / 'Basic x' -> used as is;  'user:token' -> Basic base64;  anything else -> Bearer.
    """
    if not var:
        return None
    val = os.environ.get(var)
    if not val:
        sys.exit(f"ERROR: environment variable {var} is empty or not set.")
    if val.startswith(("Bearer ", "Basic ")):
        return val
    if ":" in val:
        import base64
        return "Basic " + base64.b64encode(val.encode()).decode()
    return "Bearer " + val


def write_page(job: str, key: str, title: str, text: str, meta: dict | None = None) -> tuple[str, int]:
    """Write one markdown page with front matter; returns (relative path, chars)."""
    import hashlib
    f = CACHE / job / "pages" / f"{hashlib.sha1(key.encode()).hexdigest()[:16]}.md"
    fm = {"source": key, "title": title[:200], "fetched": now_iso(), "chars": len(text), **(meta or {})}
    head = "\n".join(f"{k}: {str(v).replace(chr(10), ' ')[:300]}" for k, v in fm.items())
    f.write_text(f"---\n{head}\n---\n\n{text}\n", encoding="utf-8")
    return rel(f), len(text)


def split_text(text: str, max_chars: int) -> list[str]:
    """Split long text into <= max_chars parts on paragraph boundaries (no silent truncation)."""
    if len(text) <= max_chars:
        return [text]
    parts, cur, size = [], [], 0
    for para in text.split("\n\n"):
        while len(para) > max_chars:  # a single giant paragraph
            if cur:
                parts.append("\n\n".join(cur)); cur, size = [], 0
            parts.append(para[:max_chars]); para = para[max_chars:]
        if size + len(para) + 2 > max_chars and cur:
            parts.append("\n\n".join(cur)); cur, size = [], 0
        cur.append(para); size += len(para) + 2
    if cur:
        parts.append("\n\n".join(cur))
    return parts


# every kit CLI run is one agent tool call: log it (hook-free call counting)
_script = Path(sys.argv[0]).name if sys.argv and sys.argv[0] else ""
if _script.endswith(".py") and _script not in ("memguard.py", "hooks.py", "status.py", "doctor.py", "common.py"):
    log_call(_script[:-3], " ".join(sys.argv[1:3]))
