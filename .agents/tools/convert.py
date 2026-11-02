#!/usr/bin/env python3
"""Convert documents into the knowledge base: Word, PDF, PowerPoint, Excel, diagrams, text.

  convert.py --job kb-hld --inputs docs/hld/ "specs/*.docx" infra/network.pdf
  convert.py --job kb-sec-att --inputs .agents/cache/kb-sec/attachments     # Confluence attachments
  convert.py --job kb-hld                                                  # resume / pick up changed files

Output: one markdown page per document (long ones split into parts, never truncated) in
.agents/cache/<job>/pages/, rows in the shared job store so `batch.py --job <job>` batches
them for Haiku digests exactly like crawled pages. Images are not converted: they are
listed in .agents/cache/<job>/images.json for the diagram step (diagram-to-dsl skill or a
vision-capable model).

Formats (stdlib unless noted):
  .docx .pptx .xlsx .vsdx      zip + XML (text, tables as rows, slides/sheets/pages in order)
  .drawio / mxfile .xml        node labels, edges "A -> B : label", containers (trust zones)
  .svg                         text labels
  .pdf                         pypdf if installed (pip install pypdf), else `pdftotext` CLI
  .html .htm                   same extractor as the crawler
  .md .txt .rst .adoc .csv .json .yaml .yml .tf .hcl .toml .ini .cfg .properties .xml
  images (.png .jpg .jpeg .gif .webp .bmp .tif)  -> images.json

Time-sliced and resumable: unchanged files (same size + mtime) are skipped on re-runs.
One file is processed at a time and the run stops early if memory hits the pause level.
"""
from __future__ import annotations

import argparse
import base64
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import (CACHE, ROOT, STATE, append_jsonl, budget, memory_snapshot, now_iso,  # noqa: E402
                    open_job_db, register_worker, rel, save_json, split_text, unregister_worker, write_page)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}
TEXT_EXT = {".md", ".txt", ".rst", ".adoc", ".csv", ".json", ".yaml", ".yml", ".tf", ".hcl", ".toml",
            ".ini", ".cfg", ".properties", ".conf"}
MAX_FILE_BYTES = 200_000_000
MAX_SHEET_ROWS = 2000


def local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


# ------------------------------------------------------------------ office formats
def docx_text(path: Path) -> str:
    """Paragraphs in order; tables as '| a | b |' rows; headers/footers appended."""
    out: list[str] = []
    with zipfile.ZipFile(path) as z:
        names = [n for n in ["word/document.xml"] + sorted(n for n in z.namelist()
                 if re.match(r"word/(header|footer)\d*\.xml$", n)) if n in z.namelist()]
        for name in names:
            with z.open(name) as f:
                para: list[str] = []
                cell: list[str] = []
                row: list[str] = []
                depth = 0
                for ev, el in ET.iterparse(f, events=("start", "end")):
                    t = local(el.tag)
                    if ev == "start":
                        if t == "tbl":
                            depth += 1
                        continue
                    if t == "t" and el.text:
                        para.append(el.text)
                    elif t == "tab":
                        para.append("\t")
                    elif t == "p":
                        txt = "".join(para).strip()
                        para = []
                        if depth:
                            if txt:
                                cell.append(txt)
                        elif txt:
                            out.append(txt)
                    elif t == "tc":
                        row.append(" ".join(cell))
                        cell = []
                    elif t == "tr":
                        if any(row):
                            out.append("| " + " | ".join(row) + " |")
                        row = []
                    elif t == "tbl":
                        depth -= 1
                    if t in ("p", "tr", "tbl"):
                        el.clear()
    return "\n\n".join(out)


def pptx_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        slides = sorted((n for n in z.namelist() if re.match(r"ppt/slides/slide\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for i, name in enumerate(slides, 1):
            texts = [el.text for el in ET.fromstring(z.read(name)).iter() if local(el.tag) == "t" and el.text]
            notes_name = f"ppt/notesSlides/notesSlide{re.findall(r'\d+', name)[-1]}.xml"
            notes = []
            if notes_name in z.namelist():
                notes = [el.text for el in ET.fromstring(z.read(notes_name)).iter() if local(el.tag) == "t" and el.text]
            block = f"## Slide {i}\n" + "\n".join(texts)
            if notes:
                block += "\n\nNotes: " + " ".join(notes)
            out.append(block)
    return "\n\n".join(out)


def xlsx_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        shared = []
        if "xl/sharedStrings.xml" in z.namelist():
            for si in ET.fromstring(z.read("xl/sharedStrings.xml")):
                shared.append("".join(el.text or "" for el in si.iter() if local(el.tag) == "t"))
        names = {}
        if "xl/workbook.xml" in z.namelist():
            for i, sh in enumerate(el for el in ET.fromstring(z.read("xl/workbook.xml")).iter() if local(el.tag) == "sheet"):
                names[f"xl/worksheets/sheet{i + 1}.xml"] = sh.get("name", f"Sheet{i + 1}")
        sheets = sorted((n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n)),
                        key=lambda n: int(re.findall(r"\d+", n)[-1]))
        for name in sheets:
            out.append(f"## Sheet: {names.get(name, name)}")
            rows = 0
            with z.open(name) as f:
                cur: list[str] = []
                for ev, el in ET.iterparse(f, events=("end",)):
                    t = local(el.tag)
                    if t == "c":
                        v = next((x.text for x in el if local(x.tag) == "v"), None)
                        is_t = next((x for x in el if local(x.tag) == "is"), None)
                        if el.get("t") == "s" and v is not None:
                            v = shared[int(v)] if int(v) < len(shared) else v
                        elif is_t is not None:
                            v = "".join(x.text or "" for x in is_t.iter() if local(x.tag) == "t")
                        cur.append((v or "").strip())
                        el.clear()
                    elif t == "row":
                        if any(cur):
                            out.append(" | ".join(cur))
                            rows += 1
                        cur = []
                        el.clear()
                        if rows >= MAX_SHEET_ROWS:
                            out.append(f"... (truncated at {MAX_SHEET_ROWS} rows)")
                            break
    return "\n".join(out)


def vsdx_text(path: Path) -> str:
    out = []
    with zipfile.ZipFile(path) as z:
        pages = sorted(n for n in z.namelist() if re.match(r"visio/pages/page\d+\.xml$", n))
        for i, name in enumerate(pages, 1):
            texts = []
            for el in ET.fromstring(z.read(name)).iter():
                if local(el.tag) == "Text":
                    t = "".join(el.itertext()).strip()
                    if t:
                        texts.append(t)
            out.append(f"## Page {i}\n" + "\n".join(f"- {t}" for t in texts))
    return "\n\n".join(out)


# ------------------------------------------------------------------ diagrams
def strip_html(s: str) -> str:
    s = re.sub(r"<br\s*/?>", " / ", s or "", flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    return re.sub(r"\s+", " ", urllib.parse.unquote(s.replace("&nbsp;", " "))).strip()


def drawio_text(path: Path) -> str:
    root = ET.parse(path).getroot()
    diagrams = root.findall(".//diagram") if local(root.tag) == "mxfile" else [root]
    out = []
    for d in diagrams:
        model = d.find(".//mxGraphModel") if d is not root else root
        if model is None and (d.text or "").strip():  # compressed: base64 -> raw deflate -> urlencoded
            try:
                raw = zlib.decompress(base64.b64decode(d.text.strip()), -15).decode("utf-8")
                model = ET.fromstring(urllib.parse.unquote(raw))
            except Exception:
                out.append(f"## {d.get('name', 'diagram')}\n(could not decode compressed diagram)")
                continue
        if model is None:
            continue
        cells = {c.get("id"): c for c in model.iter() if local(c.tag) == "mxCell"}
        # UserObject/object wrappers carry the label
        for obj in model.iter():
            if local(obj.tag) in ("UserObject", "object"):
                inner = next((c for c in obj if local(c.tag) == "mxCell"), None)
                if inner is not None:
                    inner.set("value", obj.get("label", ""))
                    inner.set("id", obj.get("id"))
                    cells[obj.get("id")] = inner
        label = lambda cid: strip_html(cells[cid].get("value", "")) if cid in cells else ""  # noqa: E731
        nodes, edges, groups = [], [], []
        for cid, c in cells.items():
            style = c.get("style", "") or ""
            if c.get("edge") == "1":
                s, t = label(c.get("source")) or c.get("source", "?"), label(c.get("target")) or c.get("target", "?")
                edges.append(f"- {s} -> {t}" + (f" : {label(cid)}" if label(cid) else ""))
            elif c.get("vertex") == "1" and label(cid):
                parent = c.get("parent")
                inside = label(parent) if parent in cells and cells[parent].get("vertex") == "1" else ""
                kind = "container" if ("container=1" in style or "swimlane" in style or "group" in style) else "node"
                (groups if kind == "container" else nodes).append(
                    f"- {label(cid)}" + (f"  (in: {inside})" if inside else "") + (f"  [{style.split(';')[0]}]" if style else ""))
        out.append(f"## Diagram: {d.get('name', path.stem)}\n### Containers / zones\n" + ("\n".join(groups) or "- (none)")
                   + "\n### Elements\n" + ("\n".join(nodes) or "- (none)") + "\n### Connections\n" + ("\n".join(edges) or "- (none)"))
    return "\n\n".join(out)


def svg_text(path: Path) -> str:
    texts = []
    for _, el in ET.iterparse(path):
        if local(el.tag) in ("text", "tspan", "title", "desc"):
            t = "".join(el.itertext()).strip()
            if t and t not in texts[-1:]:
                texts.append(t)
        el.clear()
    return "\n".join(f"- {t}" for t in texts)


# ------------------------------------------------------------------ pdf / html / text
def pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
        reader = PdfReader(str(path))
        out = []
        for i, page in enumerate(reader.pages, 1):
            t = (page.extract_text() or "").strip()
            if t:
                out.append(f"## Page {i}\n{t}")
        if out:
            return "\n\n".join(out)
        return "(no extractable text: scanned PDF? needs OCR)"
    except ImportError:
        pass
    if shutil.which("pdftotext"):
        r = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True, text=True, timeout=300)
        return r.stdout.strip() or "(no extractable text: scanned PDF? needs OCR)"
    raise RuntimeError("PDF support needs `pip install pypdf` (or the pdftotext CLI)")


def html_text(path: Path) -> str:
    from crawl import TextExtractor
    ex = TextExtractor(path.as_uri(), 10 ** 9, 0)
    ex.feed(path.read_text(encoding="utf-8", errors="replace"))
    ex.close()
    return ex.text()


def is_mxfile(path: Path) -> bool:
    with open(path, "rb") as f:
        head = f.read(400).lower()
    return b"<mxfile" in head or b"<mxgraphmodel" in head


def convert(path: Path) -> tuple[str, str]:
    """Return (kind, text). kind 'image' means listed, not converted."""
    ext = path.suffix.lower()
    if ext in IMAGE_EXT:
        return "image", ""
    if ext == ".docx":
        return "docx", docx_text(path)
    if ext == ".pptx":
        return "pptx", pptx_text(path)
    if ext == ".xlsx":
        return "xlsx", xlsx_text(path)
    if ext == ".vsdx":
        return "vsdx", vsdx_text(path)
    if ext == ".drawio" or (ext == ".xml" and is_mxfile(path)):
        return "drawio", drawio_text(path)
    if ext == ".svg":
        return "svg", svg_text(path)
    if ext == ".pdf":
        return "pdf", pdf_text(path)
    if ext in (".html", ".htm"):
        return "html", html_text(path)
    if ext in TEXT_EXT or ext == ".xml":
        return "text", path.read_text(encoding="utf-8", errors="replace")
    if ext in (".doc", ".ppt", ".xls", ".vsd"):
        raise RuntimeError(f"legacy binary {ext}: save as {ext}x first")
    raise RuntimeError(f"unsupported type {ext}")


def expand_inputs(items: list[str]) -> list[Path]:
    files: list[Path] = []
    for it in items:
        matches = glob.glob(it, recursive=True) or [it]
        for m in matches:
            p = Path(m)
            if not p.is_absolute():
                p = (Path.cwd() / p)
            if p.is_dir():
                files += [f for f in sorted(p.rglob("*")) if f.is_file() and not f.name.startswith((".", "~$"))]
            elif p.is_file():
                files.append(p)
    seen, out = set(), []
    for f in files:
        if f.resolve() not in seen:
            seen.add(f.resolve())
            out.append(f.resolve())
    return out


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--job", required=True)
    p.add_argument("--inputs", nargs="*", help="files, folders or globs (first run; later runs reuse them)")
    p.add_argument("--time-slice", type=int)
    a = p.parse_args()
    cfg = budget().get("crawl", {})
    max_chars = cfg.get("max_text_chars", 40000)
    slice_s = a.time_slice or cfg.get("time_slice_s", 240)
    db = open_job_db(a.job)
    row = db.execute("SELECT v FROM meta WHERE k='convert'").fetchone()
    st = json.loads(row[0]) if row else {"inputs": [], "seen": {}}
    if a.inputs:
        st["inputs"] = sorted(set(st["inputs"]) | set(a.inputs))
    if not st["inputs"]:
        sys.exit("Give --inputs on the first run of a job.")
    files = expand_inputs(st["inputs"])
    images_path = CACHE / a.job / "images.json"
    images = {i["path"]: i for i in (json.loads(images_path.read_text()) if images_path.exists() else [])}
    progress = STATE / "progress" / f"crawl-{a.job}.jsonl"
    import memprobe
    memprobe.lower_priority()
    register_worker(os.getpid(), f"convert:{a.job}")
    start, status = time.time(), "complete"
    counts = {"converted": 0, "unchanged": 0, "images": 0, "failed": 0}
    try:
        print(f"convert {a.job}: {len(files)} files, slice={slice_s}s", flush=True)
        for f in files:
            if time.time() - start > slice_s:
                status = "time_slice"; break
            if memory_snapshot().get("state") == "PAUSE":
                status = "memory_pause"; break
            key = rel(f)
            sig = f"{f.stat().st_size}:{int(f.stat().st_mtime)}"
            if st["seen"].get(key) == sig:
                counts["unchanged"] += 1
                continue
            try:
                if f.stat().st_size > MAX_FILE_BYTES:
                    raise RuntimeError("file over 200 MB")
                kind, text = convert(f)
                if kind == "image":
                    images[key] = {"path": key, "bytes": f.stat().st_size, "source_dir": rel(f.parent)}
                    counts["images"] += 1
                    db.execute("INSERT OR REPLACE INTO urls(url, depth, status, title, ts) VALUES(?,0,'image',?,?)",
                               (key, f.name, now_iso()))
                else:
                    db.execute("DELETE FROM urls WHERE url = ? OR url LIKE ?", (key, key + "#part%"))
                    parts = split_text(text.strip(), max_chars) if text.strip() else []
                    for i, part in enumerate(parts, 1):
                        k = key if len(parts) == 1 else f"{key}#part{i}"
                        t = f.name if len(parts) == 1 else f"{f.name} (part {i}/{len(parts)})"
                        fpath, n = write_page(a.job, k, t, part, {"type": kind, "path": key})
                        db.execute("INSERT OR REPLACE INTO urls(url, depth, status, title, file, chars, ts) "
                                   "VALUES(?,0,'done',?,?,?,?)", (k, f.name, fpath, n, now_iso()))
                    if not parts:
                        db.execute("INSERT OR REPLACE INTO urls(url, depth, status, title, err, ts) VALUES(?,0,'skipped',?,?,?)",
                                   (key, f.name, "no text", now_iso()))
                    counts["converted"] += 1
                st["seen"][key] = sig
            except Exception as e:  # noqa: BLE001
                counts["failed"] += 1
                db.execute("INSERT OR REPLACE INTO urls(url, depth, status, title, err, ts) VALUES(?,0,'failed',?,?,?)",
                           (key, f.name, f"{type(e).__name__}: {str(e)[:150]}", now_iso()))
            db.execute("INSERT OR REPLACE INTO meta VALUES('convert', ?)", (json.dumps(st),))
            db.commit()
        save_json(images_path, sorted(images.values(), key=lambda x: x["path"]))
        total = dict(db.execute("SELECT status, COUNT(*) FROM urls GROUP BY status").fetchall())
        fails = [r[0] + ": " + (r[1] or "") for r in db.execute("SELECT url, err FROM urls WHERE status='failed' LIMIT 5")]
        summary = {"job": a.job, "status": status, "this_run": counts, "store": total,
                   "images_manifest": rel(images_path) if images else None, "failed_examples": fails,
                   "seconds": round(time.time() - start)}
        summary["next"] = (f"re-run: python .agents/tools/convert.py --job {a.job}" if status != "complete"
                           else f"python .agents/tools/batch.py --job {a.job} --add-units")
        append_jsonl(progress, {"ts": now_iso(), "event": "slice_end", "done": total.get("done", 0),
                                "queued": 0, "failed": total.get("failed", 0), **{k: v for k, v in summary.items() if k != "store"}})
        print(json.dumps(summary))
    finally:
        unregister_worker(os.getpid())


if __name__ == "__main__":
    main()
