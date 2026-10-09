from __future__ import annotations

import os
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from cua import store
from cua.config import PolicyGuard, SecretProvider, Tenant, load_overlay, load_policy, load_profile
from cua.control import ControlChannel
from cua.evidence import RunLog
from cua.overlay import apply_overlay
from cua.redact import Redactor
from cua.replay import ReplayEngine, ReplayOptions
from cua.surface.web import WebSurface
from target_app import server as app

PORT = 18765
FIXTURE = Path(__file__).parent / "fixtures" / "read_balance.yaml"

os.environ.setdefault("CUA_SECRET_TEST_CORELINK_USERNAME", "teller1")
os.environ.setdefault("CUA_SECRET_TEST_CORELINK_PASSWORD", "demo-only-not-a-secret")


@pytest.fixture(scope="session")
def target_app():
    srv = app.make_server(PORT)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{PORT}"
    srv.shutdown()


@pytest.fixture(autouse=True)
def clean_app_state():
    app.STATE.faults = app.Faults()
    app.STATE.variant = "base"
    yield
    app.STATE.faults = app.Faults()
    app.STATE.variant = "base"


@pytest.fixture
def runs_dir(tmp_path):
    return tmp_path / "runs"


@pytest.fixture
def artifact_store(tmp_path, monkeypatch):
    root = tmp_path / "artifacts"
    monkeypatch.setattr(store, "STORE", root)
    return root


class Env:
    def __init__(self, base_url: str, runs_dir: Path, kind: str = "replay", operator: bool = False, debug_port: int | None = None):
        self.tenant = Tenant(id="test", base_url=base_url, profile="corelink-teller", secret_namespace="test")
        self.profile = load_profile("corelink-teller")
        self.policy = load_policy()
        self.redactor = Redactor()
        self.run = RunLog(kind, self.redactor, root=runs_dir, echo=False)
        self.guard = PolicyGuard(self.policy, [base_url], self.profile)
        self.secrets = SecretProvider("test", self.redactor)
        self.operator = operator
        self.debug_port = debug_port

    @contextmanager
    def surface(self):
        with WebSurface(self.guard, debug_port=self.debug_port) as s:
            self.channel = ControlChannel(self.run) if self.operator else None
            if self.channel:
                s.on_human_event(self.channel.record_human_event)
            yield s

    def replay(self, cap, params, overlay_rel=None, **opts):
        cap_t, prof_t = apply_overlay(cap, self.profile, load_overlay(overlay_rel))
        with self.surface() as s:
            eng = ReplayEngine(s, cap_t, prof_t, self.tenant, self.guard, self.run, self.secrets, self.channel,
                               ReplayOptions(human_timeout_s=60, **opts))
            return eng.run_replay(params)


@pytest.fixture
def env(target_app, runs_dir):
    def make(**kw) -> Env:
        return Env(target_app, runs_dir, **kw)

    return make


@pytest.fixture
def balance_cap():
    return store.load(FIXTURE)
