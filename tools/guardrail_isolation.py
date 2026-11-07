"""Preflight a trusted runtime copy before any evaluator/bridge/audit invocation.

This prevents accidental path redirection; it is not a sandbox for hostile code.
"""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace

from server.guardrail_loader import checked_path, load_configured, validate_modules


def check_node_environment():
    if any(os.environ.get(name) for name in ("NODE_OPTIONS", "NODE_PATH")):
        raise ValueError("Unset NODE_OPTIONS and NODE_PATH before isolated Guardrail testing")


def isolated_bridge_runner(root, run):
    """Give every child an explicit clean environment, even after preflight."""
    def launch(command, **kwargs):
        if command != ["node", str(root / "packages/core/dist/bridge.js")]:
            raise ValueError("Unexpected isolated bridge command")
        if checked_path(kwargs.get("cwd", ""), root, exists=True) != root or kwargs.get("shell"):
            raise ValueError("Unexpected isolated bridge execution context")
        checked_path(command[1], root, exists=True)
        environment = dict(kwargs.get("env") if kwargs.get("env") is not None else os.environ)
        # Windows environment keys are case-insensitive, including an explicit
        # caller-supplied dict; strip all spellings rather than just uppercase.
        environment = {key: value for key, value in environment.items()
                       if key.upper() not in {"NODE_OPTIONS", "NODE_PATH"}}
        return run(command, **(kwargs | {"env": environment}))
    return launch


def verify_copy(directory):
    root = checked_path(directory, exists=True)
    if (root / ".git").exists():
        raise ValueError("Use a disposable copy, not a repository workspace")
    manifest = json.loads(checked_path(root / "source-manifest.json", root, exists=True).read_text())
    source = checked_path(manifest["source_root"], exists=True)
    if root.is_relative_to(source) or source.is_relative_to(root):
        raise ValueError("Disposable copy overlaps source installation")
    expected = set()
    for entry in manifest["files"]:
        relative = Path(entry["path"])
        path = checked_path(root / relative, root, exists=True)
        if relative.is_absolute() or path in expected:
            raise ValueError("Invalid copy manifest path")
        expected.add(path)
        if hashlib.sha256(path.read_bytes()).hexdigest() != entry["sha256"]:
            raise ValueError("Runtime copy hash mismatch")
    required = ["packages/guardrail-core-python/guardrail_core/__init__.py",
                "packages/guardrail-core-python/guardrail_core/evaluator.py",
                "packages/guardrail-core-python/guardrail_core/models.py",
                "packages/guardrail-core-python/guardrail_core/audit.py",
                "packages/core/dist/bridge.js", "packages/core/package.json", "policies/sda-ops-v1.yml"]
    if not {root / path for path in required}.issubset(expected):
        raise ValueError("Required runtime files missing from manifest")
    # Check directories before descending, including junctions and audit redirects.
    def walk(directory):
        for entry in directory.iterdir():
            checked_path(entry, root, exists=True)
            if entry.is_dir():
                walk(entry)
            elif entry != root / "source-manifest.json" and not entry.is_relative_to(root / ".runtime") and entry not in expected:
                raise ValueError("Unmanifested runtime file")
    walk(root)
    return root


def validate_execution_paths(root, evaluator, audit):
    package = root / "packages/guardrail-core-python/guardrail_core"
    validate_modules(package)
    if checked_path(evaluator.PROJECT_ROOT, root, exists=True) != root:
        raise ValueError("Evaluator workspace mismatch")
    checked_path(evaluator.POLICIES_ROOT, root, exists=True)
    checked_path(evaluator.POLICIES_ROOT / "sda-ops-v1.yml", root, exists=True)
    if checked_path(audit.PROJECT_ROOT, root, exists=True) != root:
        raise ValueError("Audit workspace mismatch")
    checked_path(audit.AUDIT_LOG_PATH, root)
    command = evaluator.get_bridge_command()
    if not isinstance(command, list) or len(command) != 2 or command[0] != "node":
        raise ValueError("Only the inspected compiled Node bridge is supported")
    bridge = checked_path(command[1], root, exists=True)
    if bridge != root / "packages/core/dist/bridge.js":
        raise ValueError("Unexpected compiled bridge")


def load_isolated_runtime(directory):
    check_node_environment()
    root = verify_copy(directory)  # Verify all code and destinations before import.
    if any(name == "guardrail_core" or name.startswith("guardrail_core.") for name in sys.modules):
        raise ValueError("Use a fresh import context for isolated Guardrail tests")
    evaluator, _ = load_configured(root / "packages/guardrail-core-python")
    audit = importlib.import_module("guardrail_core.audit")
    validate_execution_paths(root, evaluator, audit)  # Before evaluation or audit write.
    # Adapt this copy's evaluator only; never patch the global subprocess module
    # or edit Guardrail source/policies. The inspected runtime uses only run().
    evaluator.subprocess = SimpleNamespace(run=isolated_bridge_runner(root, evaluator.subprocess.run))
    return evaluator, audit
