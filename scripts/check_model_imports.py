#!/usr/bin/env python3
"""
Author: Murtaza Nuruddin
check_model_imports.py

Scans file(s) and validates Django model imports of the form:
  from apps.<app>.models import <Name>[, <Name>...]
  from apps.<app>.models.<submodule> import <Name>[, <Name>...]

For each imported name:
- Confirms it's defined in that app's model files (models.py or models/**/*.py)
- If not, searches other apps and reports where it's defined
- Ignores all other import styles

Usage:
  python check_model_imports.py path/to/file.py
  python check_model_imports.py path/to/dir --recursive
  python check_model_imports.py path/to/file.py another.py --json-out results.json

Notes:
- Assumes your apps live under a top-level "apps/" package, i.e. apps/<app_name>/...
- "Defined" means a real `class Name(` definition in one of the scanned model files.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# --- Regex: ONLY the import lines you asked to check ---
# Examples:
#   from apps.app1.models import Class1
#   from apps.app2.models import Class1, Class2
IMPORT_RE = re.compile(
    r"""
    ^\s*from\s+apps\.(?P<app>[A-Za-z_]\w*)\.models(?:\.(?P<submodule>[A-Za-z_]\w*))?\s+
    import\s+(?P<imports>.+?)\s*$
    """,
    re.VERBOSE,
)

# Captures class definitions: class Foo(
CLASS_DEF_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)

# Splits imported names while handling: "A, B as C, D"
# We'll extract original symbol name (left of "as" if present).
def parse_imported_names(imports_part: str) -> List[str]:
    # Remove parentheses continuation, e.g. import (A, B)
    s = imports_part.strip()
    if s.startswith("(") and s.endswith(")"):
        s = s[1:-1]

    # Drop inline comments
    s = s.split("#", 1)[0].strip()
    if not s:
        return []

    names: List[str] = []
    for chunk in s.split(","):
        c = chunk.strip()
        if not c:
            continue
        # handle "X as Y"
        base = c.split(" as ", 1)[0].strip()
        # ignore star imports
        if base == "*":
            names.append("*")
        else:
            # sanity check identifier-like
            if re.match(r"^[A-Za-z_]\w*$", base):
                names.append(base)
            else:
                # Keep it anyway for reporting
                names.append(base)
    return names


@dataclass(frozen=True)
class FoundLocation:
    app: str
    file: str


def shorten(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except Exception:
        return str(path)


def discover_app_dirs(project_root: Path) -> Dict[str, Path]:
    """
    Discover Django apps under project_root/apps/<app_name>.
    We assume a structure where each app directory is a Python package.
    """
    apps_root = project_root / "apps"
    if not apps_root.exists() or not apps_root.is_dir():
        raise SystemExit(
            f"Couldn't find an 'apps/' directory under {project_root}. "
            f"If your structure is different, adjust discover_app_dirs()."
        )

    app_dirs: Dict[str, Path] = {}
    for child in apps_root.iterdir():
        if not child.is_dir():
            continue
        if (child / "__init__.py").exists():
            app_dirs[child.name] = child
    return app_dirs


def iter_model_files(app_dir: Path) -> List[Path]:
    """
    Return model file candidates:
      <app>/models.py
      <app>/models/**/*.py  (if models is a package)
    """
    candidates: List[Path] = []
    models_py = app_dir / "models.py"
    if models_py.exists():
        candidates.append(models_py)

    models_pkg = app_dir / "models"
    if models_pkg.exists() and models_pkg.is_dir() and (models_pkg / "__init__.py").exists():
        for f in sorted(models_pkg.rglob("*.py")):
            candidates.append(f)

    # Dedup
    seen: Set[str] = set()
    out: List[Path] = []
    for f in candidates:
        rp = str(f.resolve())
        if rp not in seen:
            out.append(f)
            seen.add(rp)
    return out


def extract_class_names(py_file: Path) -> Set[str]:
    try:
        txt = py_file.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        txt = py_file.read_text(encoding="latin-1", errors="ignore")
    return set(CLASS_DEF_RE.findall(txt))


def build_class_index(project_root: Path) -> Dict[str, Dict[str, List[Path]]]:
    """
    { app_name: { class_name: [files...] } }
    """
    app_dirs = discover_app_dirs(project_root)
    index: Dict[str, Dict[str, List[Path]]] = {}

    for app_name, app_dir in sorted(app_dirs.items(), key=lambda x: x[0]):
        class_map: Dict[str, List[Path]] = {}
        for f in iter_model_files(app_dir):
            for cls in extract_class_names(f):
                class_map.setdefault(cls, []).append(f)
        index[app_name] = class_map

    return index


def collect_targets(paths: List[Path], recursive: bool) -> List[Path]:
    files: List[Path] = []
    for p in paths:
        if p.is_file():
            files.append(p)
        elif p.is_dir():
            if recursive:
                for f in p.rglob("*.py"):
                    # skip venv-ish paths
                    if any(part in {".venv", "venv", "env", "__pycache__", "node_modules"} for part in f.parts):
                        continue
                    files.append(f)
            else:
                for f in p.glob("*.py"):
                    files.append(f)
    # Dedup
    seen: Set[str] = set()
    out: List[Path] = []
    for f in files:
        rp = str(f.resolve())
        if rp not in seen:
            out.append(f)
            seen.add(rp)
    return sorted(out, key=lambda x: str(x))


def scan_file_for_imports(file_path: Path) -> List[dict]:
    """
    Return list of dicts:
      { line_no, raw_line, app, submodule, imported_names }
    Only matches the specific import style.
    """
    out: List[dict] = []
    try:
        lines = file_path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        lines = file_path.read_text(encoding="latin-1", errors="ignore").splitlines()

    for i, line in enumerate(lines, start=1):
        m = IMPORT_RE.match(line)
        if not m:
            continue
        app = m.group("app")
        submodule = m.group("submodule")
        imports_part = m.group("imports")
        names = parse_imported_names(imports_part)
        if not names:
            continue
        out.append(
            {
                "line_no": i,
                "raw_line": line.rstrip("\n"),
                "app": app,
                "submodule": submodule,
                "imported_names": names,
            }
        )
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", help="File(s) or directory(ies) to scan.")
    ap.add_argument("--project-root", default=".", help="Repo root (usually where manage.py is).")
    ap.add_argument("--recursive", action="store_true", help="If a directory is provided, scan *.py recursively.")
    ap.add_argument("--json-out", default=None, help="Write JSON results to this path.")
    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()
    targets = collect_targets([Path(p).resolve() for p in args.paths], args.recursive)
    if not targets:
        raise SystemExit("No Python files found for the given paths.")

    class_index = build_class_index(project_root)
    all_apps = sorted(class_index.keys())

    results = {
        "project_root": str(project_root),
        "files_scanned": [shorten(f, project_root) for f in targets],
        "matches": [],
        "summary": {"ok": 0, "bad": 0, "warnings": 0},
    }

    print("\n=== Checking model import lines ===")
    print(f"Project root: {project_root}")
    print(f"Apps indexed: {', '.join(all_apps)}\n")

    for f in targets:
        matches = scan_file_for_imports(f)
        if not matches:
            continue

        rel_file = shorten(f, project_root)
        print(f"\nFile: {rel_file}")

        for imp in matches:
            app = imp["app"]
            submodule = imp["submodule"]
            line_no = imp["line_no"]
            raw_line = imp["raw_line"]
            names = imp["imported_names"]

            record = {
                "file": rel_file,
                "line_no": line_no,
                "raw_line": raw_line,
                "app": app,
                "submodule": submodule,
                "imports": [],
            }

            # If app not indexed, warn
            if app not in class_index:
                print(f"  L{line_no}: WARNING - app '{app}' not found under apps/")
                print(f"       {raw_line}")
                results["summary"]["warnings"] += 1
                for name in names:
                    record["imports"].append(
                        {
                            "name": name,
                            "status": "WARNING (app not indexed)",
                            "found_in_expected_app": [],
                            "found_in_other_apps": [],
                        }
                    )
                results["matches"].append(record)
                continue

            print(f"  L{line_no}: {raw_line}")

            for name in names:
                # star import: can't validate safely
                if name == "*":
                    status = "WARNING (star import not validated)"
                    print(f"       - {name}: {status}")
                    results["summary"]["warnings"] += 1
                    record["imports"].append(
                        {
                            "name": name,
                            "status": status,
                            "found_in_expected_app": [],
                            "found_in_other_apps": [],
                        }
                    )
                    continue

                found_expected_files = class_index[app].get(name, [])
                found_expected = [shorten(p, project_root) for p in found_expected_files]

                found_elsewhere: List[FoundLocation] = []
                if not found_expected:
                    for other_app in all_apps:
                        if other_app == app:
                            continue
                        files = class_index.get(other_app, {}).get(name, [])
                        for pf in files:
                            found_elsewhere.append(
                                FoundLocation(app=other_app, file=shorten(pf, project_root))
                            )

                if found_expected:
                    status = "OK"
                    results["summary"]["ok"] += 1
                    print(f"       - {name}: OK (defined in apps.{app})")
                elif found_elsewhere:
                    status = "BAD (wrong app)"
                    results["summary"]["bad"] += 1
                    where = ", ".join([f"{loc.app}:{loc.file}" for loc in found_elsewhere[:5]])
                    print(f"       - {name}: BAD (not defined in apps.{app}; found in {where})")
                    if len(found_elsewhere) > 5:
                        print(f"         (+{len(found_elsewhere)-5} more locations)")
                else:
                    status = "BAD (not found)"
                    results["summary"]["bad"] += 1
                    print(f"       - {name}: BAD (not found in any indexed app)")

                record["imports"].append(
                    {
                        "name": name,
                        "status": status,
                        "found_in_expected_app": found_expected,
                        "found_in_other_apps": [
                            {"app": loc.app, "file": loc.file} for loc in found_elsewhere
                        ],
                    }
                )

            results["matches"].append(record)

    print("\n=== Summary ===")
    print(f"OK: {results['summary']['ok']}")
    print(f"BAD: {results['summary']['bad']}")
    print(f"Warnings: {results['summary']['warnings']}")

    if args.json_out:
        out_path = Path(args.json_out).resolve()
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"\nWrote JSON results to: {out_path}")


if __name__ == "__main__":
    main()
