#!/usr/bin/env python3
"""Keep the data a pipeline step starts from: copy its inputs into <run>/steps/<NN_step>/input/ and list them in manifest.csv.

    snapshot.py <run>/steps/03_input_tables  PATH ...  [--ref BIG_PATH ...] [--note TEXT]

Each PATH (file or folder) is copied under input/ with its own name. On macOS (APFS) the copy is a clone, so it takes no
extra disk until a file changes. --ref paths (the 15 GB N-PORT database) are not copied: the manifest keeps their size,
change time and a hash of their first and last MB, enough to tell whether a rerun read the same file. Symlinks are followed.
manifest.csv: name, source, kind (copy | ref | missing), files, bytes, rows (lines of .csv files minus the header), sha256.
A step's folder is replaced when the step runs again, so it always shows what the latest run read.
"""
import argparse
import csv
import datetime as dt
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path


def sha(p: Path, whole: bool = True) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        if whole:
            for b in iter(lambda: f.read(1 << 20), b""):
                h.update(b)
        else:                                          # large reference file: first and last MB plus size
            h.update(f.read(1 << 20))
            size = p.stat().st_size
            f.seek(max(0, size - (1 << 20)))
            h.update(f.read())
            h.update(str(size).encode())
    return h.hexdigest()


def files(p: Path) -> list:
    return [p] if p.is_file() else sorted(x for x in p.rglob("*") if x.is_file())


def rows(fs: list) -> str:
    n, seen = 0, False
    for f in fs:
        if f.suffix.lower() == ".csv":
            seen = True
            with open(f, "rb") as fh:
                n += max(0, sum(1 for _ in fh) - 1)
    return str(n) if seen else ""


def digest(fs: list, base: Path) -> str:
    """One file: its sha256. A folder: sha256 over each file's relative path and sha256."""
    if len(fs) == 1 and fs[0] == base:
        return sha(base)
    h = hashlib.sha256()
    for f in fs:
        h.update(f"{f.relative_to(base)}\0{sha(f)}\n".encode())
    return h.hexdigest()


def clone(src: Path, dst: Path) -> None:
    if sys.platform == "darwin" and subprocess.run(["cp", "-cRL", str(src), str(dst)], capture_output=True).returncode == 0:
        return
    (shutil.copytree(src, dst, symlinks=False) if src.is_dir() else shutil.copy2(src, dst))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("step_dir")
    ap.add_argument("paths", nargs="*")
    ap.add_argument("--ref", action="append", default=[], help="large input recorded by size and partial hash, not copied")
    ap.add_argument("--note", default="", help="one line saved as note.txt (e.g. the command the step ran)")
    a = ap.parse_args()
    step = Path(a.step_dir)
    shutil.rmtree(step, ignore_errors=True)
    (step / "input").mkdir(parents=True)
    out, used = [], set()
    for kind, paths in (("copy", a.paths), ("ref", a.ref)):
        for s in paths:
            src = Path(s).expanduser()
            name, up = src.name, src.parent
            while name in used:                        # two inputs with one name: prefix parent folders until unique
                name, up = f"{up.name or 'root'}__{name}", up.parent
            used.add(name)
            if not src.exists():
                out.append(dict(name=name, source=str(src), kind="missing", files=0, bytes=0, rows="", sha256=""))
                continue
            real = src.resolve()
            if kind == "ref":
                st = real.stat()
                out.append(dict(name=name, source=str(real), kind=f"ref (not copied; changed {dt.datetime.fromtimestamp(st.st_mtime):%Y-%m-%d %H:%M})",
                                files=1, bytes=st.st_size, rows="", sha256="partial:" + sha(real, whole=False)))
                continue
            clone(real, step / "input" / name)
            fs = files(real)
            out.append(dict(name=name, source=str(real), kind="copy", files=len(fs), bytes=sum(f.stat().st_size for f in fs),
                            rows=rows(fs), sha256=digest(fs, real)))
    with open(step / "manifest.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["name", "source", "kind", "files", "bytes", "rows", "sha256"])
        w.writeheader()
        w.writerows(out)
    if a.note:
        (step / "note.txt").write_text(a.note.strip() + "\n", encoding="utf-8")
    miss = [r["name"] for r in out if r["kind"] == "missing"]
    print(f"snapshot {step.name}: {len(out) - len(miss)} inputs kept" + (f", missing: {', '.join(miss)}" if miss else ""), flush=True)


if __name__ == "__main__":
    os.umask(0o077)
    main()
