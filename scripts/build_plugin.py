#!/usr/bin/env python3
"""Sync the self-contained Claude Code plugin package in ``plugin/``.

The marketplace entry (``.claude-plugin/marketplace.json``) points at
``./plugin``, so only that folder is copied into a user's plugin cache.  The
sources of truth stay where they are; this script copies them in:

  .claude-plugin/plugin.json      -> plugin/.claude-plugin/plugin.json
  commands/*.md                   -> plugin/commands/
  hooks/hooks.json                -> plugin/hooks/hooks.json
  scripts/agentcap_cc.py          -> plugin/scripts/agentcap_cc.py
  src/agentcap/<import closure>   -> plugin/src/agentcap/
  LICENSE                         -> plugin/LICENSE

The import closure is computed from the ``agentcap`` imports of
``scripts/agentcap_cc.py`` (module level and function level), plus files a
module in the closure loads by path (``Path(__file__).with_name("x.py")``,
``... / "data" / "x.json"``).

``plugin/.claude-plugin/icon.png`` is the one file kept only in ``plugin/``
(regenerate with ``--icon``, needs Pillow).

  python3 scripts/build_plugin.py           # sync (removes stray files)
  python3 scripts/build_plugin.py --check   # exit 1 if plugin/ is out of sync
  python3 scripts/build_plugin.py --icon    # re-render icon.png, then sync

Stdlib only (except ``--icon``).  Deterministic: same tree in, same bytes out.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
PKG = ROOT / "src" / "agentcap"
ENTRY = ROOT / "scripts" / "agentcap_cc.py"
ICON = Path(".claude-plugin") / "icon.png"   # relative to plugin/
KEEP = {ICON}                                 # plugin-only files sync must not delete

_WITH_NAME = re.compile(r"""with_name\(\s*["']([\w.]+\.py)["']\s*\)""")
_DATA = re.compile(r"""["']data["']\s*/\s*["']([\w.\-]+)["']""")


def _agentcap_imports(path: Path, *, in_package: bool) -> set[str]:
    """Top-level ``agentcap`` submodule names imported anywhere in *path*."""
    out: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), str(path))):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and in_package:
                if node.module:
                    out.add(node.module.split(".")[0])
                else:
                    out.update(a.name for a in node.names)
            elif node.level == 0 and node.module and node.module.split(".")[0] == "agentcap":
                parts = node.module.split(".")
                if len(parts) > 1:
                    out.add(parts[1])
                else:
                    out.update(a.name for a in node.names if a.name != "__version__")
            elif node.level > 1 or (node.level == 1 and not in_package):
                raise SystemExit(f"build_plugin: unsupported relative import in {path}")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                parts = alias.name.split(".")
                if parts[0] == "agentcap" and len(parts) > 1:
                    out.add(parts[1])
    return out


def import_closure(entry: Path = ENTRY) -> list[str]:
    """Sorted module names under src/agentcap that the plugin script needs."""
    seen: set[str] = set()
    todo = sorted(_agentcap_imports(entry, in_package=False))
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        mod = PKG / f"{name}.py"
        if not mod.is_file():
            raise SystemExit(f"build_plugin: agentcap.{name} is not a plain module in src/agentcap")
        seen.add(name)
        todo.extend(sorted(_agentcap_imports(mod, in_package=True) - seen))
    return sorted(seen)


def path_loaded_files(modules: list[str]) -> list[str]:
    """Package-relative files the closure loads by path (lifeline shims, data files)."""
    extra: set[str] = set()
    for name in modules:
        text = (PKG / f"{name}.py").read_text(encoding="utf-8")
        extra.update(_WITH_NAME.findall(text))
        extra.update(f"data/{f}" for f in _DATA.findall(text))
    for rel in extra:
        if not (PKG / rel).is_file():
            raise SystemExit(f"build_plugin: src/agentcap/{rel} referenced but missing")
    return sorted(extra)


def manifest() -> dict[Path, Path]:
    """plugin-relative destination -> source file (absolute)."""
    files: dict[Path, Path] = {
        Path(".claude-plugin/plugin.json"): ROOT / ".claude-plugin" / "plugin.json",
        Path("hooks/hooks.json"): ROOT / "hooks" / "hooks.json",
        Path("scripts/agentcap_cc.py"): ENTRY,
        Path("LICENSE"): ROOT / "LICENSE",
    }
    for md in sorted((ROOT / "commands").glob("*.md")):
        files[Path("commands") / md.name] = md
    modules = import_closure()
    pkg_files = ["__init__.py", *(f"{m}.py" for m in modules), *path_loaded_files(modules)]
    for rel in pkg_files:
        files[Path("src/agentcap") / rel] = PKG / rel
    return dict(sorted(files.items()))


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _on_disk() -> set[Path]:
    if not PLUGIN.exists():
        return set()
    return {p.relative_to(PLUGIN) for p in PLUGIN.rglob("*") if p.is_file() or p.is_symlink()}


def problems() -> list[str]:
    """Differences between plugin/ and what a sync would produce (empty = in sync)."""
    want = manifest()
    have = _on_disk()
    out: list[str] = []
    for rel, src in want.items():
        dest = PLUGIN / rel
        if rel not in have:
            out.append(f"missing: plugin/{rel}")
        elif dest.is_symlink() or _sha(dest) != _sha(src):
            out.append(f"stale: plugin/{rel} != {src.relative_to(ROOT)}")
    for rel in sorted(have - set(want) - KEEP):
        out.append(f"stray: plugin/{rel}")
    for rel in sorted(KEEP - have):
        out.append(f"missing: plugin/{rel} (run with --icon)")
    return out


def sync() -> list[str]:
    want = manifest()
    changed: list[str] = []
    for rel in sorted(_on_disk() - set(want) - KEEP):
        (PLUGIN / rel).unlink()
        changed.append(f"removed plugin/{rel}")
    # prune empty dirs (e.g. a __pycache__ left by a local run)
    for d in sorted((p for p in PLUGIN.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        if not any(d.iterdir()):
            d.rmdir()
    for rel, src in want.items():
        dest = PLUGIN / rel
        if dest.is_file() and not dest.is_symlink() and _sha(dest) == _sha(src):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        shutil.copyfile(src, dest)
        shutil.copymode(src, dest)
        changed.append(f"wrote plugin/{rel}")
    return changed


def render_icon(path: Path, size: int = 1024) -> None:
    """The AgentCap mark (rounded square + three bars, as on the landing page)."""
    from PIL import Image, ImageDraw  # optional dependency, only for --icon

    accent, paper = (15, 107, 92, 255), (251, 248, 244, 255)
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    u = size / 24  # landing SVG uses a 24x24 viewBox
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=round(5 * u), fill=accent)
    stroke = round(3 * u)
    for x, top in ((7, 13), (12, 6), (17, 11)):   # bars span 6..18: centred
        cx = x * u
        d.rounded_rectangle([cx - stroke / 2, top * u, cx + stroke / 2, 18 * u],
                            radius=stroke / 2, fill=paper)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path, format="PNG", optimize=True)


# ------------------------------------------------------------------ Codex plugin
# ``.agents/plugins/marketplace.json`` points Codex at ``./codex-plugin`` (Codex reads
# that file before ``.claude-plugin/marketplace.json``, so each agent gets its own
# package).  Sources: codex/plugin.json, codex/hooks.json, codex/skills/,
# scripts/agentcap_codex.py and its agentcap import closure.
CODEX_PLUGIN = ROOT / "codex-plugin"
CODEX_ENTRY = ROOT / "scripts" / "agentcap_codex.py"


def codex_manifest() -> dict[Path, Path]:
    files: dict[Path, Path] = {
        Path(".codex-plugin/plugin.json"): ROOT / "codex" / "plugin.json",
        Path("hooks/hooks.json"): ROOT / "codex" / "hooks.json",
        Path("scripts/agentcap_codex.py"): CODEX_ENTRY,
        Path("LICENSE"): ROOT / "LICENSE",
    }
    for f in sorted((ROOT / "codex" / "skills").rglob("*")):
        if f.is_file():
            files[Path("skills") / f.relative_to(ROOT / "codex" / "skills")] = f
    modules = import_closure(CODEX_ENTRY)
    for rel in ["__init__.py", *(f"{m}.py" for m in modules), *path_loaded_files(modules)]:
        files[Path("src/agentcap") / rel] = PKG / rel
    return dict(sorted(files.items()))


def _target_problems(dest_root: Path, want: dict[Path, Path], keep: set[Path], label: str) -> list[str]:
    have = ({p.relative_to(dest_root) for p in dest_root.rglob("*") if p.is_file() or p.is_symlink()}
            if dest_root.exists() else set())
    out: list[str] = []
    for rel, src in want.items():
        dest = dest_root / rel
        if rel not in have:
            out.append(f"missing: {label}/{rel}")
        elif dest.is_symlink() or _sha(dest) != _sha(src):
            out.append(f"stale: {label}/{rel} != {src.relative_to(ROOT)}")
    for rel in sorted(have - set(want) - keep):
        out.append(f"stray: {label}/{rel}")
    return out


def _target_sync(dest_root: Path, want: dict[Path, Path], label: str) -> list[str]:
    changed: list[str] = []
    have = ({p.relative_to(dest_root) for p in dest_root.rglob("*") if p.is_file() or p.is_symlink()}
            if dest_root.exists() else set())
    for rel in sorted(have - set(want)):
        (dest_root / rel).unlink()
        changed.append(f"removed {label}/{rel}")
    if dest_root.exists():
        for d in sorted((q for q in dest_root.rglob("*") if q.is_dir()), key=lambda q: len(q.parts), reverse=True):
            if not any(d.iterdir()):
                d.rmdir()
    for rel, src in want.items():
        dest = dest_root / rel
        if dest.is_file() and not dest.is_symlink() and _sha(dest) == _sha(src):
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.is_symlink():
            dest.unlink()
        shutil.copyfile(src, dest)
        shutil.copymode(src, dest)
        changed.append(f"wrote {label}/{rel}")
    return changed


def codex_problems() -> list[str]:
    return _target_problems(CODEX_PLUGIN, codex_manifest(), set(), "codex-plugin")


def codex_sync() -> list[str]:
    return _target_sync(CODEX_PLUGIN, codex_manifest(), "codex-plugin")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="report drift, change nothing")
    ap.add_argument("--icon", action="store_true", help="re-render plugin/.claude-plugin/icon.png")
    args = ap.parse_args(argv)
    if args.check:
        found = problems() + codex_problems()
        for line in found:
            print(line)
        print("plugin/ and codex-plugin/ are in sync" if not found
              else f"{len(found)} problem(s); run scripts/build_plugin.py")
        return 1 if found else 0
    if args.icon:
        render_icon(PLUGIN / ICON)
        print(f"rendered plugin/{ICON}")
    for line in sync() + codex_sync():
        print(line)
    leftover = problems() + codex_problems()
    for line in leftover:
        print(line, file=sys.stderr)
    return 1 if leftover else 0


if __name__ == "__main__":
    raise SystemExit(main())
