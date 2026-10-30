"""Subscription plan prices used by `agentcap report savings`.

Edit here, or override without touching code by writing a JSON list to
``$XDG_CONFIG_HOME/agentcap/plans.json`` (same fields; entries replace the
bundled plan with the same name).  Prices are USD per month, before tax, as
listed on the vendor's public pricing page on the date in ``checked``.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path


@dataclass(frozen=True)
class Plan:
    name: str
    vendor: str  # "claude" | "codex"
    usd_per_month: Decimal
    source: str
    checked: str = "2026-10-30"


PLANS: tuple[Plan, ...] = (
    # https://claude.com/pricing (checked 2026-10-30): Pro $20 billed monthly ($17/mo annual),
    # Max "from $100" (5x); Max 20x $200 (same page: Max 20x includes $200/mo API credits)
    Plan("Claude Pro", "claude", Decimal("20"), "https://claude.com/pricing"),
    Plan("Claude Max 5x", "claude", Decimal("100"), "https://claude.com/pricing"),
    Plan("Claude Max 20x", "claude", Decimal("200"), "https://claude.com/pricing"),
    # Plus $20/mo: https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus
    Plan("ChatGPT Plus", "codex", Decimal("20"),
         "https://help.openai.com/en/articles/6950777-what-is-chatgpt-plus"),
    # Pro tiers $100 / $200 / $500 per month (monthly only):
    # https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers
    Plan("ChatGPT Pro 100", "codex", Decimal("100"),
         "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers"),
    Plan("ChatGPT Pro 200", "codex", Decimal("200"),
         "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers"),
    Plan("ChatGPT Pro 500", "codex", Decimal("500"),
         "https://help.openai.com/en/articles/9793128-about-chatgpt-pro-tiers"),
)


def plans_override_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = Path(base).expanduser() if base else Path.home() / ".config"
    return root / "agentcap" / "plans.json"


def load_plans() -> list[Plan]:
    plans = {p.name: p for p in PLANS}
    path = plans_override_path()
    if path.is_file():
        try:
            for item in json.loads(path.read_text(encoding="utf-8")):
                plan = Plan(
                    name=str(item["name"]),
                    vendor=str(item["vendor"]),
                    usd_per_month=Decimal(str(item["usd_per_month"])),
                    source=str(item.get("source", "user override")),
                    checked=str(item.get("checked", "user override")),
                )
                plans[plan.name] = plan
        except (OSError, ValueError, KeyError, TypeError, ArithmeticError) as exc:
            print(f"agentcap: ignoring {path} ({exc})", file=sys.stderr)
    return list(plans.values())
