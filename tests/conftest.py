from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from agentcap.devtools.fake_upstream import create_app as fake_app
from agentcap.ledger import Caps, Ledger
from agentcap.license import FREE_FEATURES, PRO_FEATURES
from agentcap.proxy import ProxyConfig, create_app


@pytest.fixture
def ledger(tmp_path: Path) -> Ledger:
    return Ledger(tmp_path / "ledger.sqlite3")


class Harness:
    def __init__(self, ledger: Ledger, *, features=FREE_FEATURES, cap="5", upstream=None, **kw: Any):
        self.events: list[dict[str, Any]] = []
        self.kills: list[dict[str, Any]] = []
        self.ledger = ledger
        self.config = ProxyConfig(
            run_id=kw.pop("run_id", "run-test"),
            caps=Caps(run_usd=Decimal(cap) if cap is not None else None,
                      hour_usd=kw.pop("hour_usd", None), day_usd=kw.pop("day_usd", None)),
            features=features,
            on_cap_exceeded=self.kills.append,
            on_loop_detected=self.kills.append,
            **kw,
        )
        transport = upstream or httpx.ASGITransport(app=fake_app())
        self.app = create_app(self.config, ledger, upstream_transport=transport, events=self.events.append)
        self.proxy = self.app.state.proxy

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://proxy")

    async def messages(self, body: dict[str, Any], headers: dict[str, str] | None = None, path="/anthropic/v1/messages"):
        hdrs = {"x-api-key": "sk-ant-test-SECRET", "anthropic-version": "2023-06-01"}
        hdrs.update(headers or {})
        async with self.client() as c:
            return await c.post(path, content=json.dumps(body), headers=hdrs)

    def types(self) -> list[str]:
        return [e.get("type") for e in self.events]

    def settled(self) -> list[dict[str, Any]]:
        return [e for e in self.events if e.get("type") == "request_settled"]


@pytest.fixture
def harness(ledger):
    def make(**kw):
        return Harness(ledger, **kw)
    return make


def body(model="claude-sonnet-5-5", max_tokens=1000, text="hello world " * 50, **extra):
    data = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": text}]}
    data.update(extra)
    return data


@pytest.fixture(autouse=True)
def _isolated_budget_config(tmp_path, monkeypatch):
    """Never let a developer's real budget.json leak into the suite."""

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
