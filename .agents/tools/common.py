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

try:  # optional
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
def _tree_rss(proc) -> int:
    total = 0
    try:
        procs = [proc] + proc.children(recursive=True)
    except Exception:
        procs = [proc]
    for p in procs:
        try:
            total += p.memory_info().rss
        except Exception:
            pass
    return total


def self_tree_mb() -> float | None:
    if not psutil:
        return None
    return _tree_rss(psutil.Process(os.getpid())) / 1_048_576


def pid_tree_mb(pid: int) -> float | None:
    if not psutil:
        return None
    try:
        return _tree_rss(psutil.Process(pid)) / 1_048_576
    except Exception:
        return None


def vscode_mb(names: list[str] | None = None) -> float | None:
    """Sum RSS of all VS Code / Electron processes (renderer, ext host, helpers)."""
    if not psutil:
        return None
    names = names or budget().get("memory", {}).get("vscode_process_names", [])
    wanted = {n.lower() for n in names}
    total = 0
    for p in psutil.process_iter(["name", "memory_info"]):
        try:
            n = (p.info.get("name") or "").lower()
            if n in wanted or n.startswith("code helper"):
                mi = p.info.get("memory_info")
                if mi:
                    total += mi.rss
        except Exception:
            pass
    return total / 1_048_576


def workers_mb() -> float:
    """RSS of worker processes registered in state/workers.json (alive only)."""
    reg = load_json(STATE / "workers.json", {}) or {}
    total, alive = 0.0, {}
    for pid, meta in reg.items():
        mb = pid_tree_mb(int(pid))
        if mb is not None:
            total += mb
            alive[pid] = meta
    if psutil and len(alive) != len(reg):
        save_json(STATE / "workers.json", alive)
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
    m = budget().get("memory", {})
    total_budget = m.get("total_mb", 4096)
    vs = vscode_mb(m.get("vscode_process_names"))
    wk = workers_mb() if psutil else None
    used = (vs or 0) + (wk or 0)
    pct = round(100 * used / total_budget, 1) if psutil else None
    state = "unknown"
    if pct is not None:
        state = "PAUSE" if pct >= m.get("pause_at_pct", 90) else ("WARN" if pct >= m.get("resume_at_pct", 75) else "OK")
    return {
        "psutil": bool(psutil),
        "vscode_mb": round(vs, 0) if vs is not None else None,
        "workers_mb": round(wk, 0) if wk is not None else None,
        "total_budget_mb": total_budget,
        "used_pct": pct,
        "state": state,
        "vscode_over_soft_limit": bool(vs and vs > m.get("vscode_soft_limit_mb", 2560)),
    }


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
    pr = pr or pricing()
    return pr.get("agent_models", {}).get(agent, "sonnet")


def fmt_usd(x: float) -> str:
    return f"${x:,.2f}" if x >= 0.1 else f"${x:,.3f}"
