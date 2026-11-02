"""Checks that run outside the model (no hooks needed).

verify_unit(unit)  -> (ok, report)   used by `ledger.py finish --status done`
  1. every declared output that looks like a path must exist and be non-empty
     (folder: at least one file; glob: at least one match);
  2. every command in the unit's `verify` field must exit 0. Commands run through
     memguard.py, so they get the safety deny list, the memory cap and a timeout.
fake_edges(units)  -> warnings              used by `ledger.py import-plan` / `add`
  a dependency whose outputs the unit never reads is probably a fake edge that makes
  work wait for nothing (wildcards and listed `edge_reasons` are exempt).
"""
from __future__ import annotations

import fnmatch
import glob
import re
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import ROOT, budget  # noqa: E402


def _looks_like_path(s: str) -> bool:
    s = s.strip()
    return bool(s) and not any(c.isspace() for c in s) and ("/" in s or "." in Path(s).name) \
        and not s.startswith(("http://", "https://"))


def check_outputs(outputs: list[str]) -> list[str]:
    problems = []
    for o in outputs or []:
        if not isinstance(o, str) or not _looks_like_path(o):
            continue
        p = ROOT / o
        if any(ch in o for ch in "*?["):
            if not glob.glob(str(p), recursive=True):
                problems.append(f"no file matches {o}")
        elif o.endswith("/") or p.is_dir():
            if not p.is_dir() or not any(f.is_file() for f in p.rglob("*")):
                problems.append(f"folder {o} is missing or empty")
        elif not p.is_file():
            problems.append(f"missing output {o}")
        elif p.stat().st_size == 0:
            problems.append(f"output {o} is empty")
    return problems


def run_verify(cmds, timeout: int) -> tuple[bool, list[str]]:
    cmds = [cmds] if isinstance(cmds, str) else list(cmds or [])
    report = []
    for c in cmds:
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("memguard.py")), "--timeout", str(timeout),
                            "--tail", "15", "--shell", c], cwd=ROOT, capture_output=True, text=True, errors="replace")
        tail = (r.stdout or r.stderr).strip().splitlines()[-12:]
        if r.returncode != 0:
            report.append(f"verify FAILED (exit {r.returncode}): {c}")
            report += ["    " + ln[:200] for ln in tail]
            return False, report
        report.append(f"verify ok: {c}")
    return True, report


def verify_unit(unit: dict) -> tuple[bool, list[str]]:
    timeout = budget().get("agents", {}).get("verify_timeout_s", 600)
    problems = check_outputs(unit.get("outputs", []))
    if problems:
        return False, ["outputs check FAILED: " + "; ".join(problems)]
    ok, rep = run_verify(unit.get("verify"), timeout)
    return ok, (["outputs check ok"] + rep)


def _norm(s: str) -> str:
    return s.strip().rstrip("/").lower()


def _job_root(p: str) -> str | None:
    m = re.match(r"(\.agents/cache/[^/]+)", p)
    return m.group(1) if m else None


def _reads(text: str, ins: list[str], out: str) -> bool:
    if out in text or any(i.startswith(out) or out.startswith(i) for i in ins if i):
        return True
    jr = _job_root(out)  # same knowledge-base job (pages -> summary.md, digests ...)
    if jr and (jr in text or any(i.startswith(jr) for i in ins)):
        return True
    for pat in re.findall(r"[\w./*-]*\*[\w./*-]*", text):  # globs such as .agents/cache/*/images.json
        if fnmatch.fnmatch(out, pat) or (_job_root(out) and fnmatch.fnmatch(_job_root(out) + "/x", pat.rsplit("/", 1)[0] + "/x")):
            return True
    return False


def fake_edges(units: dict | list) -> list[str]:
    items = units.values() if isinstance(units, dict) else units
    by_id = {u["id"]: u for u in items}
    warnings = []
    for u in by_id.values():
        ins = [_norm(x) for x in u.get("inputs", []) if isinstance(x, str)]
        text = (u.get("instructions", "") + " " + " ".join(ins)).lower()
        reasons = u.get("edge_reasons", {})
        for d in u.get("depends_on", []):
            if d.endswith("*") or d in reasons or d not in by_id:
                continue
            outs = [_norm(x) for x in by_id[d].get("outputs", []) if isinstance(x, str) and _looks_like_path(x)]
            if not outs:
                continue
            used = any(_reads(text, ins, o) for o in outs)
            if not used:
                warnings.append(f"FAKE_EDGE? {u['id']} depends on {d} but never reads its outputs ({', '.join(outs[:3])}). "
                                f"Remove the edge, list the output in inputs, or add edge_reasons.{d}.")
    return warnings
