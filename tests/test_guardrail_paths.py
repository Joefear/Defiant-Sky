"""Default-CI isolation/provenance regressions; only generated mock installations."""
import importlib
import importlib.util
import json
import os
from pathlib import Path
import sys

import pytest

from server.evidence import Settings, detect
from server.governance import Governance
from server.guardrail_loader import ApprovedFinder, load_configured
from tools.guardrail_isolation import load_isolated_runtime, isolated_bridge_runner
from tools.prepare_guardrail_copy import prepare


@pytest.fixture(autouse=True)
def clean_modules(monkeypatch):
    before_finders = list(sys.meta_path)
    prior = {k: v for k, v in sys.modules.items() if k == "guardrail_core" or k.startswith("guardrail_core.")}
    for key in prior:
        monkeypatch.delitem(sys.modules, key)
    monkeypatch.setattr(sys, "dont_write_bytecode", True)
    yield
    for key in list(sys.modules):
        if key == "guardrail_core" or key.startswith("guardrail_core."):
            sys.modules.pop(key)
    sys.modules.update(prior)
    sys.meta_path[:] = before_finders


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
        "from pathlib import Path\nimport subprocess\nfrom . import audit\nPROJECT_ROOT=Path(__file__).resolve().parents[3]\n"
        + (f"POLICIES_ROOT=Path({str(outside)!r})\n" if redirect == "policy" else "POLICIES_ROOT=PROJECT_ROOT/'policies'\n")
        + (f"def get_bridge_command(): return ['node', {str(outside / 'bridge.js')!r}]\n" if redirect == "bridge" else
           "def get_bridge_command(): return ['node', str(PROJECT_ROOT/'packages/core/dist/bridge.js')]\n")
        + "def evaluate_request(request):\n    audit.AUDIT_LOG_PATH.write_text('EVALUATED')\n")
    for relative, content in [("packages/core/dist/bridge.js", "// mock"), ("packages/core/package.json", "{}"), ("policies/sda-ops-v1.yml", "id: sda-ops-v1")]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    return package.parent


@pytest.mark.parametrize("redirect", ["bridge", "audit", "policy", "workspace", "command_shape", "command_program"])
def test_redirected_runtime_fails_before_evaluation_or_audit(tmp_path, redirect):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "bridge.js").write_text("// must never run")
    (outside / "sda-ops-v1.yml").write_text("id: external")
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source, redirect, outside)
    evaluator_file = source / "packages/guardrail-core-python/guardrail_core/evaluator.py"
    if redirect == "workspace":
        with evaluator_file.open("a") as output: output.write(f"\nPROJECT_ROOT=Path({str(outside)!r})\n")
    if redirect in {"command_shape", "command_program"}:
        command = ["node", "--require", str(outside / "bridge.js")] if redirect == "command_shape" else ["other-node", str(outside / "bridge.js")]
        with evaluator_file.open("a") as output: output.write(f"\ndef get_bridge_command(): return {command!r}\n")
    prepare(source, copy)
    called = False
    with pytest.raises(ValueError):
        evaluator, _ = load_isolated_runtime(copy)
        called = True
        evaluator.evaluate_request(None)
    assert not called
    assert not (outside / "audit.jsonl").exists()
    assert not (copy / ".runtime").exists()


@pytest.mark.parametrize("damage", ["hash", "extra_code", "traversal", "source_overlap", "original", "linked_audit", "workspace"])
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
    if damage == "workspace": (copy / ".git").write_text("gitdir: unrelated-workspace")
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


@pytest.mark.parametrize("present", [True, False])
def test_competing_hook_cannot_redirect_lazy_import(tmp_path, monkeypatch, present):
    approved = installation(tmp_path / "approved")
    package = approved / "guardrail_core"
    (package / "evaluator.py").write_text("def evaluate_request(request):\n    from .lazy import value\n    return value\n")
    if present: (package / "lazy.py").write_text("value = 'approved'\n")
    marker = tmp_path / "outside-marker"
    foreign = tmp_path / "foreign.py"
    foreign.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('escaped')\nvalue='foreign'\n")
    class Competitor:
        calls = 0
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "guardrail_core.lazy":
                self.calls += 1
                return importlib.util.spec_from_file_location(fullname, foreign)
    competitor = Competitor()
    monkeypatch.setattr(sys, "meta_path", [competitor, *sys.meta_path])
    evaluator, _ = load_configured(approved)
    assert isinstance(sys.meta_path[0], ApprovedFinder)
    if present:
        assert evaluator.evaluate_request(None) == "approved"
    else:
        with pytest.raises(ImportError): evaluator.evaluate_request(None)
    assert competitor.calls == 0 and not marker.exists()
    load_configured(approved)
    assert sum(isinstance(f, ApprovedFinder) for f in sys.meta_path) == 1


def test_runtime_switch_requires_restart_even_after_modules_removed(tmp_path):
    approved = installation(tmp_path / "approved")
    other = installation(tmp_path / "other")
    load_configured(approved)
    for name in list(sys.modules):
        if name == "guardrail_core" or name.startswith("guardrail_core."): sys.modules.pop(name)
    with pytest.raises(ValueError, match="Restart"): load_configured(other)


def test_failed_first_load_does_not_pin_partial_installation(tmp_path):
    broken = installation(tmp_path / "broken")
    (broken / "guardrail_core/models.py").write_text("raise RuntimeError('broken fixture')\n")
    with pytest.raises(RuntimeError): load_configured(broken)
    assert not any(isinstance(f, ApprovedFinder) for f in sys.meta_path)
    assert "guardrail_core" not in sys.modules
    load_configured(installation(tmp_path / "valid"))


@pytest.mark.parametrize("variable", ["NODE_OPTIONS", "NODE_PATH"])
def test_node_overrides_rejected_before_import(tmp_path, monkeypatch, variable):
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source)
    prepare(source, copy)
    monkeypatch.setenv(variable, "external-test-value")
    with pytest.raises(ValueError, match="Unset NODE_OPTIONS"):
        load_isolated_runtime(copy)
    assert "guardrail_core" not in sys.modules and not (copy / ".runtime").exists()


@pytest.mark.parametrize("environment", ["inherited", "explicit_mixed_case"])
def test_node_preload_cannot_run_after_preflight(tmp_path, monkeypatch, environment):
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source)
    file = source / "packages/guardrail-core-python/guardrail_core/evaluator.py"
    with file.open("a") as output:
        output.write("\ndef evaluate_request(request):\n    return subprocess.run(get_bridge_command(), cwd=PROJECT_ROOT, capture_output=True, text=True, check=True)\n")
    (source / "packages/core/dist/bridge.js").write_text("console.log('isolated bridge ran');")
    prepare(source, copy)
    evaluator, _ = load_isolated_runtime(copy)
    marker, preload = tmp_path / "external-marker.txt", tmp_path / "preload.cjs"
    preload.write_text("require('node:fs').writeFileSync(" + json.dumps(str(marker)) + ", 'escaped');")
    options = "--require " + json.dumps(str(preload))
    if environment == "inherited":
        monkeypatch.setenv("NODE_OPTIONS", options)
        monkeypatch.setenv("NODE_PATH", str(tmp_path))
        result = evaluator.evaluate_request(None)
    else:
        env = dict(os.environ, NoDe_OpTiOnS=options, NoDe_PaTh=str(tmp_path))
        result = evaluator.subprocess.run(evaluator.get_bridge_command(), cwd=copy,
            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "isolated bridge ran" and not marker.exists()


def test_isolated_child_environment_preserves_unrelated_settings(tmp_path):
    bridge = tmp_path / "packages/core/dist/bridge.js"
    bridge.parent.mkdir(parents=True)
    bridge.write_text("// fake")
    calls = []
    run = isolated_bridge_runner(tmp_path, lambda *args, **kwargs: calls.append(kwargs))
    run(["node", str(bridge)], cwd=tmp_path, env={"PATH": "keep", "custom": "keep", "NoDe_OpTiOnS": "remove", "NODE_PATH": "remove"})
    assert calls[0]["env"] == {"PATH": "keep", "custom": "keep"}


def test_real_directory_link_is_rejected_before_import(tmp_path):
    source, copy = tmp_path / "source", tmp_path / "copy"
    installation(source)
    prepare(source, copy)
    destination = tmp_path / "protected-audits"
    destination.mkdir()
    link = copy / ".runtime"
    if sys.platform == "win32":
        import _winapi
        _winapi.CreateJunction(str(destination), str(link))
    else:
        link.symlink_to(destination, target_is_directory=True)
    try:
        with pytest.raises(ValueError): load_isolated_runtime(copy)
        assert "guardrail_core" not in sys.modules and list(destination.iterdir()) == []
    finally:
        # Remove only the link, never recursively traverse the protected target.
        if sys.platform == "win32": link.rmdir()
        else: link.unlink()
