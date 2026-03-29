#!/usr/bin/env python3
"""
Author: Murtaza Nuruddin

check_model_class_locations.py

Checks whether configured Django model classes are defined in their expected apps.

The script scans a Django project for app directories, indexes class definitions
found in `models.py` and `models/*.py`, and verifies each `app.Model` target in
`TARGETS`. If a class is not found in the expected app, it searches other apps
and reports mismatches.

Usage:
  python check_model_class_locations.py --project-root . --json-out results.json

Notes:
- Place this file at your Django repo root (near manage.py), or pass --project-root.
- Detects classes defined in:
    <app>/models.py
    <app>/models/*.py   (if models is a package)
- Searches all apps (directories containing apps.py) under the project root.
- App discovery is heuristic-based and looks for `apps.py` or `models.py` + `__init__.py`
- Class detection is regex-based, so review results if your project uses unusual patterns
- Output is printed to the console and can optionally be written as JSON
"""

from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple


# ---------- CONFIG: your targets ----------
TARGETS = [
    "app1.Class1",
    "app2.Class1",
    "app3.Class1",
]
# ----------------------------------------


CLASS_DEF_RE = re.compile(r"^\s*class\s+([A-Za-z_]\w*)\s*\(", re.MULTILINE)


@dataclass(frozen=True)
class FoundLocation:
    app: str
    file: str


def split_target(label: str) -> Tuple[str, str]:
    if "." not in label:
        raise ValueError(f"Invalid target (expected app.Model): {label}")
    app, model = label.split(".", 1)
    if not app or not model:
        raise ValueError(f"Invalid target (expected app.Model): {label}")
    return app, model


def is_django_app_dir(dir_path: Path) -> bool:
    """Heuristic: a Django app usually has apps.py (or sometimes models.py only)."""
    if not dir_path.is_dir():
        return False
    if (dir_path / "apps.py").exists():
        return True
    # Fallback heuristic: treat as app if it has models.py and __init__.py
    if (dir_path / "models.py").exists() and (dir_path / "__init__.py").exists():
        return True
    return False


def discover_apps(project_root: Path) -> List[Path]:
    """Find candidate Django apps under project_root (one level deep + nested)."""
    apps: List[Path] = []
    # Walk but prune common noisy dirs
    prune = {".git", ".venv", "venv", "env", "__pycache__", "node_modules", "static", "media", "migrations"}
    for root, dirs, files in os.walk(project_root):
        # prune
        dirs[:] = [d for d in dirs if d not in prune and not d.startswith(".")]
        p = Path(root)

        if is_django_app_dir(p):
            apps.append(p)

    # De-duplicate by resolved path
    uniq: Dict[str, Path] = {}
    for a in apps:
        uniq[str(a.resolve())] = a
    return sorted(uniq.values(), key=lambda x: str(x))


def iter_model_files(app_dir: Path) -> List[Path]:
    """Return model file candidates: models.py plus models/*.py if models is a package."""
    candidates: List[Path] = []
    models_py = app_dir / "models.py"
    if models_py.exists():
        candidates.append(models_py)

    models_pkg = app_dir / "models"
    if models_pkg.exists() and models_pkg.is_dir() and (models_pkg / "__init__.py").exists():
        for f in sorted(models_pkg.rglob("*.py")):
            # skip __init__.py (it won't usually define models, but could; include it anyway if you want)
            if f.name == "__pycache__":
                continue
            candidates.append(f)

    # De-dup
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


def build_index(app_dirs: List[Path]) -> Dict[str, Dict[str, List[Path]]]:
    """
    Index structure:
      { app_name: { class_name: [files...] } }
    """
    index: Dict[str, Dict[str, List[Path]]] = {}

    for app_dir in app_dirs:
        app_name = app_dir.name
        files = iter_model_files(app_dir)
        class_map: Dict[str, List[Path]] = {}

        for f in files:
            for cls in extract_class_names(f):
                class_map.setdefault(cls, []).append(f)

        index[app_name] = class_map

    return index


def shorten(path: Path, project_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(project_root.resolve()))
    except Exception:
        return str(path)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--project-root", default=".", help="Repo root (usually where manage.py is).")
    ap.add_argument("--json-out", default=None, help="Optional: write results JSON to this path.")
    args = ap.parse_args()

    project_root = Path(args.project_root).resolve()
    if not project_root.exists():
        raise SystemExit(f"Project root not found: {project_root}")

    app_dirs = discover_apps(project_root)
    if not app_dirs:
        raise SystemExit(
            "No Django apps discovered. Ensure --project-root points to your repo root and apps have apps.py."
        )

    index = build_index(app_dirs)

    # Convenience: list all apps
    all_apps = sorted(index.keys())

    results = {
        "project_root": str(project_root),
        "apps_discovered": all_apps,
        "targets": [],
    }

    for label in TARGETS:
        target_app, target_cls = split_target(label)

        entry = {
            "target": label,
            "expected_app": target_app,
            "class": target_cls,
            "found_in_expected_app": [],
            "found_in_other_apps": [],
            "status": None,
        }

        # Check expected app first
        if target_app in index and target_cls in index[target_app]:
            entry["found_in_expected_app"] = [
                shorten(p, project_root) for p in index[target_app][target_cls]
            ]

        # If not in expected app, search elsewhere
        if not entry["found_in_expected_app"]:
            found_elsewhere: List[FoundLocation] = []
            for app in all_apps:
                if app == target_app:
                    continue
                files = index.get(app, {}).get(target_cls, [])
                for f in files:
                    found_elsewhere.append(FoundLocation(app=app, file=shorten(f, project_root)))

            entry["found_in_other_apps"] = [
                {"app": loc.app, "file": loc.file} for loc in found_elsewhere
            ]

        # Determine status
        if entry["found_in_expected_app"]:
            entry["status"] = "OK (in expected app)"
        elif entry["found_in_other_apps"]:
            entry["status"] = "MISMATCH (found in other app(s))"
        else:
            entry["status"] = "NOT FOUND"

        results["targets"].append(entry)

    # Pretty print summary
    print("\n=== Django model class location check ===")
    print(f"Project root: {project_root}")
    print(f"Apps discovered ({len(all_apps)}): {', '.join(all_apps)}\n")

    for t in results["targets"]:
        print(f"- {t['target']}: {t['status']}")
        if t["found_in_expected_app"]:
            for f in t["found_in_expected_app"]:
                print(f"    ✓ expected app file: {f}")
        if t["found_in_other_apps"]:
            for loc in t["found_in_other_apps"]:
                print(f"    ↪ found elsewhere: app={loc['app']} file={loc['file']}")
        if t["status"] == "NOT FOUND":
            print("    ✗ not found in any scanned app model files")
        print()

    if args.json_out:
        out_path = Path(args.json_out).resolve()
        out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
        print(f"Wrote JSON results to: {out_path}")


if __name__ == "__main__":
    main()
