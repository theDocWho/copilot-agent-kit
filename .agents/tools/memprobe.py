"""Process memory without hard dependencies.

Backends, tried in order (override with env KIT_MEM_BACKEND=psutil|win32|tasklist|proc|ps|none):
  psutil    if installed
  win32     Windows, ctypes only: Toolhelp32 snapshot (pid, parent, name) +
            K32GetProcessMemoryInfo working set. Needs no admin rights for your own
            processes (VS Code runs as you). Processes it may not open are skipped.
  tasklist  Windows fallback: `tasklist /FO CSV /NH` (no parent info -> no process trees)
  proc      Linux /proc
  ps        macOS / other Unix: `ps -axo pid=,ppid=,rss=,comm=`
  none      nothing works (locked-down machine) -> callers switch to static limits

list_procs() -> [(pid, ppid, name, rss_bytes)]; ppid is -1 and rss None when unknown.
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time

_CACHE: dict = {"t": 0.0, "procs": None, "backend": None}


# ------------------------------------------------------------------ backends
def _psutil():
    import psutil  # type: ignore
    out = []
    for p in psutil.process_iter(["pid", "ppid", "name", "memory_info"]):
        try:
            mi = p.info.get("memory_info")
            out.append((p.info["pid"], p.info.get("ppid") or -1, p.info.get("name") or "", mi.rss if mi else None))
        except Exception:
            pass
    return out


def _win32():
    import ctypes
    from ctypes import wintypes as wt

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    class PROCESSENTRY32W(ctypes.Structure):
        _fields_ = [("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wt.DWORD), ("cntThreads", wt.DWORD),
                    ("th32ParentProcessID", wt.DWORD), ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
                    ("szExeFile", wt.WCHAR * 260)]

    class PMC(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
        _fields_ = [("cb", wt.DWORD), ("PageFaultCount", wt.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t)]

    k32.CreateToolhelp32Snapshot.restype = wt.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
    k32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
    k32.OpenProcess.restype = wt.HANDLE
    k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
    k32.K32GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(PMC), wt.DWORD]
    k32.CloseHandle.argtypes = [wt.HANDLE]

    snap = k32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if not snap or snap == wt.HANDLE(-1).value:
        raise OSError(ctypes.get_last_error(), "CreateToolhelp32Snapshot failed")
    out = []
    try:
        e = PROCESSENTRY32W()
        e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = k32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            pid, rss = e.th32ProcessID, None
            h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
            if h:
                pmc = PMC()
                pmc.cb = ctypes.sizeof(PMC)
                if k32.K32GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                    rss = pmc.WorkingSetSize
                k32.CloseHandle(h)
            out.append((pid, e.th32ParentProcessID, e.szExeFile, rss))
            ok = k32.Process32NextW(snap, ctypes.byref(e))
    finally:
        k32.CloseHandle(snap)
    if not any(r for *_, r in out):
        raise OSError("no process memory readable")
    return out


def _tasklist():
    r = subprocess.run(["tasklist", "/FO", "CSV", "/NH"], capture_output=True, text=True, timeout=20,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    out = []
    for line in r.stdout.splitlines():
        cols = re.findall(r'"([^"]*)"', line)
        if len(cols) >= 5:
            kb = re.sub(r"\D", "", cols[4])  # "123,456 K" / "123.456 K" in any locale
            out.append((int(cols[1]), -1, cols[0], int(kb) * 1024 if kb else None))
    if not out:
        raise OSError("tasklist returned nothing")
    return out


def _proc():
    out = []
    page = os.sysconf("SC_PAGE_SIZE")
    for d in os.listdir("/proc"):
        if not d.isdigit():
            continue
        try:
            with open(f"/proc/{d}/stat", "rb") as f:
                st = f.read().decode(errors="replace")
            name = st[st.index("(") + 1: st.rindex(")")]
            fields = st[st.rindex(")") + 2:].split()
            ppid = int(fields[1])
            with open(f"/proc/{d}/statm") as f:
                rss = int(f.read().split()[1]) * page
            out.append((int(d), ppid, name, rss))
        except Exception:
            pass
    return out


def _ps():
    r = subprocess.run(["ps", "-axo", "pid=,ppid=,rss=,comm="], capture_output=True, text=True, timeout=20)
    out = []
    for line in r.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            out.append((int(parts[0]), int(parts[1]), os.path.basename(parts[3]), int(parts[2]) * 1024))
    if not out:
        raise OSError("ps returned nothing")
    return out


BACKENDS = {"psutil": _psutil, "win32": _win32, "tasklist": _tasklist, "proc": _proc, "ps": _ps}


def _order() -> list[str]:
    forced = os.environ.get("KIT_MEM_BACKEND")
    if forced:
        return [forced] if forced != "none" else []
    if sys.platform == "win32":
        return ["psutil", "win32", "tasklist"]
    if sys.platform.startswith("linux"):
        return ["psutil", "proc", "ps"]
    return ["psutil", "ps"]


def list_procs(max_age: float = 2.0):
    """Cached process list, or None if no backend works."""
    if _CACHE["procs"] is not None and time.time() - _CACHE["t"] < max_age:
        return _CACHE["procs"]
    errors = []
    for name in _order():
        try:
            procs = BACKENDS[name]()
            _CACHE.update(t=time.time(), procs=procs, backend=name)
            return procs
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {type(e).__name__}")
    _CACHE.update(t=time.time(), procs=None, backend="none", errors=errors)
    return None


def backend() -> str:
    list_procs()
    return _CACHE.get("backend") or "none"


def backend_errors() -> list[str]:
    return _CACHE.get("errors", [])


def tree_bytes(pid: int):
    """RSS of pid and all descendants (descendants only if the backend knows parents)."""
    procs = list_procs()
    if procs is None:
        return None
    kids: dict[int, list[int]] = {}
    rss = {}
    for p, pp, _, r in procs:
        kids.setdefault(pp, []).append(p)
        rss[p] = r
    if pid not in rss:
        return None
    total, stack, seen = 0, [pid], set()
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        total += rss.get(p) or 0
        stack += kids.get(p, [])
    return total


def names_bytes(names: list[str]):
    procs = list_procs()
    if procs is None:
        return None
    wanted = {n.lower() for n in names}
    return sum((r or 0) for _, _, n, r in procs if n.lower() in wanted or n.lower().startswith("code helper"))


def descendants(pid: int) -> list[int]:
    procs = list_procs(max_age=0)
    if not procs:
        return []
    kids: dict[int, list[int]] = {}
    for p, pp, _, _ in procs:
        kids.setdefault(pp, []).append(p)
    out, stack = [], list(kids.get(pid, []))
    while stack:
        p = stack.pop()
        out.append(p)
        stack += kids.get(p, [])
    return out


# ------------------------------------------------------------------ Windows hard cap
class WinJob:
    """Windows Job Object that hard-caps the memory of a process and all its children,
    and kills the whole tree when closed. ctypes only, no admin rights needed."""

    def __init__(self, limit_bytes: int):
        import ctypes
        from ctypes import wintypes as wt
        self.ct, self.wt = ctypes, wt
        self.k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class IO(ctypes.Structure):
            _fields_ = [(n, ctypes.c_ulonglong) for n in ("r", "w", "o", "rb", "wb", "ob")]

        class BASIC(ctypes.Structure):
            _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                        ("LimitFlags", wt.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                        ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wt.DWORD),
                        ("Affinity", ctypes.c_size_t), ("PriorityClass", wt.DWORD), ("SchedulingClass", wt.DWORD)]

        class EXT(ctypes.Structure):
            _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                        ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                        ("PeakJobMemoryUsed", ctypes.c_size_t)]

        self.EXT = EXT
        self.k32.CreateJobObjectW.restype = wt.HANDLE
        self.k32.OpenProcess.restype = wt.HANDLE
        self.h = self.k32.CreateJobObjectW(None, None)
        if not self.h:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        info = EXT()
        info.BasicLimitInformation.LimitFlags = 0x0200 | 0x2000  # JOB_MEMORY | KILL_ON_JOB_CLOSE
        info.JobMemoryLimit = limit_bytes
        if not self.k32.SetInformationJobObject(self.h, 9, ctypes.byref(info), ctypes.sizeof(info)):
            raise OSError(ctypes.get_last_error(), "SetInformationJobObject failed")

    def assign(self, pid: int) -> None:
        h = self.k32.OpenProcess(0x0001 | 0x0100, False, pid)  # PROCESS_TERMINATE | PROCESS_SET_QUOTA
        if not h:
            raise OSError(self.ct.get_last_error(), "OpenProcess failed")
        try:
            if not self.k32.AssignProcessToJobObject(self.h, h):
                raise OSError(self.ct.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            self.k32.CloseHandle(h)

    def peak_bytes(self) -> int:
        info = self.EXT()
        self.k32.QueryInformationJobObject(self.h, 9, self.ct.byref(info), self.ct.sizeof(info), None)
        return info.PeakJobMemoryUsed

    def close(self) -> None:  # kills anything still in the job
        if self.h:
            self.k32.CloseHandle(self.h)
            self.h = None


# ------------------------------------------------------------------ whole-machine memory
def system_memory():
    """{'total_mb', 'available_mb', 'available_pct', 'commit_pct'} or None.
    available = RAM the OS can hand out without swapping. commit_pct (Windows) = how close
    the machine is to its commit limit (RAM + page file); at 100% allocations fail and the
    desktop stalls, so it is the best 'about to lock up' signal there.
    Env KIT_SIM_AVAILABLE_MB overrides available memory (tests only)."""
    res = None
    forced = os.environ.get("KIT_MEM_BACKEND")
    try:
        if forced == "none":
            return None
        if forced in (None, "psutil") and sys.platform != "win32":
            try:
                import psutil  # type: ignore
                vm = psutil.virtual_memory()
                res = {"total_mb": vm.total / 1_048_576, "available_mb": vm.available / 1_048_576, "commit_pct": None}
            except ImportError:
                pass
        if res is None and sys.platform == "win32":
            import ctypes
            from ctypes import wintypes as wt

            class MS(ctypes.Structure):
                _fields_ = [("dwLength", wt.DWORD), ("dwMemoryLoad", wt.DWORD), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            m = MS()
            m.dwLength = ctypes.sizeof(MS)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
                res = {"total_mb": m.ullTotalPhys / 1_048_576, "available_mb": m.ullAvailPhys / 1_048_576,
                       "commit_pct": round(100 * (1 - m.ullAvailPageFile / m.ullTotalPageFile), 1) if m.ullTotalPageFile else None}
        if res is None and sys.platform == "win32" and forced in (None, "psutil"):
            try:
                import psutil  # type: ignore
                vm = psutil.virtual_memory()
                res = {"total_mb": vm.total / 1_048_576, "available_mb": vm.available / 1_048_576, "commit_pct": None}
            except ImportError:
                pass
        if res is None and os.path.exists("/proc/meminfo"):
            info = {}
            with open("/proc/meminfo") as f:
                for line in f:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0]) / 1024
            res = {"total_mb": info["MemTotal"], "available_mb": info.get("MemAvailable", info.get("MemFree", 0)),
                   "commit_pct": None}
        if res is None and sys.platform == "darwin":
            total = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True).stdout) / 1_048_576
            vm = subprocess.run(["vm_stat"], capture_output=True, text=True).stdout
            page = int(re.search(r"page size of (\d+)", vm).group(1))
            pages = {k: int(v) for k, v in re.findall(r"Pages (free|inactive|speculative|purgeable):\s+(\d+)", vm)}
            res = {"total_mb": total, "available_mb": sum(pages.values()) * page / 1_048_576, "commit_pct": None}
    except Exception:
        res = None
    if res is None:
        return None
    sim = os.environ.get("KIT_SIM_AVAILABLE_MB")
    if sim:
        res["available_mb"] = float(sim)
    res["available_pct"] = round(100 * res["available_mb"] / res["total_mb"], 1) if res["total_mb"] else None
    res["total_mb"], res["available_mb"] = round(res["total_mb"]), round(res["available_mb"])
    return res


def lower_priority() -> None:
    """Run this process (and the children it starts) below normal priority, so a heavy kit
    job never makes the desktop, VS Code or the mouse unresponsive."""
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL_PRIORITY_CLASS
        else:
            os.nice(10)
    except Exception:
        pass
