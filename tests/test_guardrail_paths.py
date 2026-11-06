"""Default-CI isolation/provenance regressions; only generated mock installations."""
import importlib
import json
import os
from pathlib import Path
import sys

import pytest

from server.evidence import Settings, detect
from server.governance import Governance
from tools.guardrail_isolation import load_isolated_runtime
from tools.prepare_guardrail_copy import prepare


@pytest.fixture(autouse=True)
def clean_modules(monkeypatch):
    prior = {k: v for k, v in sys.modules.items() if k == "guardrail_core" or k.startswith("guardrail_core.")}
    for key in prior:
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    yield
    for key in list(sys.modules):
        if key == "guardrail_core" or key.startswith("guardrail_core."):
            sys.modules.pop(key)
    sys.modules.update(prior)


def installation(root, redirect=None, outside=None):
    package = root / "packages/guardrail-core-python/guardrail_core"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "models.py").write_text("class DecisionRequest: pass\n")
    (package / "audit.py").write_text(
        "from pathlib import Path\nPROJECT_ROOT=Path(__file__).resolve().parents[3]\n"
        + (f"AUDIT_LOG_PATH=Path({str(outside / 'audit.jsonl')!r})\n" if redirect == "audit" else
           "AUDIT_LOG_PATH=PROJECT_ROOT/'.runtime/audit.jsonl'\n"))
    (package / "evaluator.py").write_text(
        "from pathlib import Path\nfrom . import audit\nPROJECT_ROOT=Path(__file__).resolve().parents[3]\n"
        + (f"POLICIES_ROOT=Path({str(outside)!r})\n" if redirect == "policy" else "POLICIES_ROOT=PROJECT_ROOT/'policies'\n")
        + (f"def get_bridge_command(): return ['node', {str(outside / 'bridge.js')!r}]\n" if redirect == "bridge" else
           "def get_bridge_command(): return ['node', str(PROJECT_ROOT/'packages/core/dist/bridge.js')]\n")
        + "def evaluate_request(request):\n    audit.AUDIT_LOG_PATH.write_text('EVALUATED')\n")
    for relative, content in [("packages/core/dist/bridge.js", "// mock"), ("packages/core/package.json", "{}"), ("policies/sda-ops-v1.yml", "id: sda-ops-v1")]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package.parent


@pytest.mark.parametrize("redirect", ["bridge", "audit", "policy"])
def test_redirected_runtime_fails_before_evaluation_or_audit(tmp_path, redirect):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bridge.js").write_text("// must never run")
    (outside / "sda-ops-v1.yml").write_text("id: external")
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source, redirect, outside)
    prepare(source, copy)
    called = False
    with pytest.raises(ValueError):
        evaluator, _ = load_isolated_runtime(copy)
        called = True
        evaluator.evaluate_request(None)
    assert not called
    assert not (outside / "audit.jsonl").exists()
    assert not (copy / ".runtime").exists()


@pytest.mark.parametrize("damage", ["hash", "extra_code", "traversal", "source_overlap", "original", "linked_audit"])
def test_copy_rejected_before_import(tmp_path, monkeypatch, damage):
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source)
    prepare(source, copy)
    manifest = copy / "source-manifest.json"
    if damage == "hash":
        (copy / "packages/guardrail-core-python/guardrail_core/__init__.py").write_text("raise AssertionError('must not import')")
    if damage == "extra_code":
        (copy / "packages/guardrail-core-python/guardrail_core/extra.py").write_text("raise AssertionError('must not import')")
    if damage in {"traversal", "source_overlap"}:
        data = json.loads(manifest.read_text())
        if damage == "traversal": data["files"][0]["path"] = "../source/packages/core/package.json"
        else: data["source_root"] = str(copy)
        manifest.write_text(json.dumps(data))
    if damage == "original": copy = source
    if damage == "linked_audit":
        audit_dir = copy / ".runtime"
        audit_dir.mkdir()
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == audit_dir or original(self))
    with pytest.raises((ValueError, FileNotFoundError)):
        load_isolated_runtime(copy)
    assert "guardrail_core" not in sys.modules


def test_hardlinked_audit_destination_is_rejected(tmp_path):
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source)
    prepare(source, copy)
    original = tmp_path / "protected.jsonl"
    original.write_text("unchanged")
    (copy / ".runtime").mkdir()
    os.link(original, copy / ".runtime/audit.jsonl")
    with pytest.raises(ValueError): load_isolated_runtime(copy)
    assert original.read_text() == "unchanged" and "guardrail_core" not in sys.modules


def test_copy_refuses_cached_modules_before_import(tmp_path, monkeypatch):
    source, copy = tmp_path / "source", tmp_path / "copy"
    package = installation(source)
    prepare(source, copy)
    monkeypatch.syspath_prepend(str(package))
    importlib.import_module("guardrail_core")
    with pytest.raises(ValueError): load_isolated_runtime(copy)
    assert "guardrail_core.evaluator" not in sys.modules


@pytest.mark.parametrize("conflict", ["search_path", "cached", "cached_models", "missing", "empty", "linked"])
def test_configured_provenance_fails_closed(tmp_path, monkeypatch, now, records, conflict):
    approved = installation(tmp_path / "approved")
    other = installation(tmp_path / "other")
    monkeypatch.setenv("GUARDRAIL_PATH", str(approved))
    if conflict in {"search_path", "cached", "cached_models"}:
        monkeypatch.syspath_prepend(str(other))
    if conflict in {"cached", "cached_models"}:
        importlib.import_module("guardrail_core.models" if conflict == "cached_models" else "guardrail_core")
        monkeypatch.syspath_prepend(str(approved))
    if conflict == "missing": monkeypatch.setenv("GUARDRAIL_PATH", str(tmp_path / "missing"))
    if conflict == "empty": monkeypatch.setenv("GUARDRAIL_PATH", "")
    if conflict == "linked":
        original = Path.is_symlink
        monkeypatch.setattr(Path, "is_symlink", lambda self: self == approved / "guardrail_core" or original(self))
    result = Governance(clock=lambda: now).review(detect(records, now, Settings()))
    assert result["status"] == "UNAVAILABLE" and not result["action_authorized"]
    assert "guardrail_core.evaluator" not in sys.modules


def test_exact_configured_installation_loads_and_can_be_reused(tmp_path, monkeypatch):
    approved = installation(tmp_path / "approved")
    monkeypatch.setenv("GUARDRAIL_PATH", str(approved))
    for _ in range(2):
        gate = Governance()
        gate.load()
        assert gate.evaluate is not None and gate.request_type is not None
        assert Path(sys.modules["guardrail_core.evaluator"].__file__).is_relative_to(approved)
