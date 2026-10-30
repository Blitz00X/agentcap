"""One config for every cap: ``agentcap budget``.

Stored as JSON at ``$XDG_CONFIG_HOME/agentcap/budget.json`` (default
``~/.config/agentcap/budget.json``).  Values are defaults for ``agentcap run``;
an explicit flag or ``AGENTCAP_CAP`` always wins.

Scopes:
  task_usd   per ``agentcap run`` (one job / one agent session) in API dollars
  hour_usd   rolling hour across all runs (Pro)
  day_usd    rolling day across all runs (Pro)
  task_quota per job share of the Claude Pro/Max or ChatGPT quota, in % (Pro stops)
  window     quota window for task_quota: ``week`` or ``5h``
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass, fields
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any


def budget_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "agentcap" / "budget.json"


@dataclass(frozen=True)
class Budget:
    task_usd: Decimal | None = None
    hour_usd: Decimal | None = None
    day_usd: Decimal | None = None
    task_quota: float | None = None
    window: str | None = None

    def is_empty(self) -> bool:
        return all(getattr(self, f.name) is None for f in fields(self))


def parse_usd(value: Any, name: str) -> Decimal:
    try:
        d = Decimal(str(value).strip().lstrip("$"))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a dollar amount, got {value!r}") from exc
    if not d.is_finite() or d <= 0:
        raise ValueError(f"{name} must be greater than 0")
    return d


def parse_pct(value: Any, name: str) -> float:
    raw = str(value).strip().rstrip("%")
    try:
        pct = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a percentage like 5%, got {value!r}") from exc
    if not 0 < pct <= 100:
        raise ValueError(f"{name} must be between 0 and 100%")
    return pct


def _from_dict(data: dict[str, Any]) -> Budget:
    kw: dict[str, Any] = {}
    for key in ("task_usd", "hour_usd", "day_usd"):
        if data.get(key) is not None:
            kw[key] = parse_usd(data[key], key)
    if data.get("task_quota") is not None:
        kw["task_quota"] = parse_pct(data["task_quota"], "task_quota")
    if data.get("window") in ("week", "5h"):
        kw["window"] = data["window"]
    return Budget(**kw)


def load(path: Path | None = None) -> Budget:
    path = path or budget_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return _from_dict(data)
    except FileNotFoundError:
        return Budget()
    except (OSError, ValueError) as exc:
        print(f"agentcap: ignoring unreadable budget file {path} ({exc})", file=sys.stderr)
        return Budget()


def save(b: Budget, path: Path | None = None) -> Path:
    path = path or budget_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {k: (str(v) if isinstance(v, Decimal) else v) for k, v in asdict(b).items() if v is not None}
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    os.replace(tmp, path)
    os.chmod(path, 0o600)
    return path


def clear(path: Path | None = None) -> None:
    try:
        (path or budget_path()).unlink()
    except FileNotFoundError:
        pass


def describe(b: Budget) -> list[str]:
    if b.is_empty():
        return ["No budget set. Example: agentcap budget set --task-usd 2 --day-usd 20 --task-quota 5%"]
    out = []
    usd = lambda d: f"${d:.2f}"  # noqa: E731
    if b.task_usd is not None:
        out.append(f"task (per run/session)  {usd(b.task_usd)}  API $")
    if b.hour_usd is not None:
        out.append(f"hour (all runs, Pro)    {usd(b.hour_usd)}  API $")
    if b.day_usd is not None:
        out.append(f"day  (all runs, Pro)    {usd(b.day_usd)}  API $")
    if b.task_quota is not None:
        out.append(f"task quota (Pro stops)  {b.task_quota:g}% of {b.window or 'week'} subscription quota")
    return out
