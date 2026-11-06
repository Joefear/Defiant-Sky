"""Explicit Guardrail installations must never silently resolve to another package."""
import importlib
from importlib.machinery import PathFinder
import inspect
from pathlib import Path
import sys


def checked_path(value, root=None, *, exists=False):
    path = Path(value).absolute()
    if ".." in path.parts:
        raise ValueError("Parent traversal is not permitted")
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("Linked runtime paths are not permitted")
        if part.exists():
            info = part.lstat()
            if getattr(info, "st_file_attributes", 0) & 0x400:
                raise ValueError("Reparse points are not permitted")
            if part.is_file() and info.st_nlink > 1:
                raise ValueError("Hard-linked runtime files are not permitted")
    resolved = path.resolve(strict=exists)
    if root is not None and not resolved.is_relative_to(root):
        raise ValueError("Runtime path escapes approved directory")
    return resolved


def validate_modules(package):
    for name, module in tuple(sys.modules.items()):
        if name != "guardrail_core" and not name.startswith("guardrail_core."):
            continue
        if module is None:
            raise ValueError("Ambiguous cached Guardrail module")
        origin = checked_path(module.__file__, package, exists=True)
        if checked_path(module.__spec__.origin, package, exists=True) != origin:
            raise ValueError("Module origin mismatch")
        if hasattr(module, "__path__"):
            paths = list(module.__path__)
            if len(paths) != 1 or checked_path(paths[0], package, exists=True) != origin.parent:
                raise ValueError("Ambiguous package search path")


class ApprovedFinder:
    def __init__(self, directory, package):
        self.directory, self.package = directory, package

    def find_spec(self, fullname, path=None, target=None):
        if fullname != "guardrail_core" and not fullname.startswith("guardrail_core."):
            return None
        spec = PathFinder.find_spec(fullname, [str(self.directory)] if path is None else path)
        if spec is None or not spec.origin:
            raise ImportError("Guardrail module missing from approved installation")
        checked_path(spec.origin, self.package, exists=True)
        for location in spec.submodule_search_locations or []:
            checked_path(location, self.package, exists=True)
        return spec


def load_configured(directory):
    directory = checked_path(directory, exists=True)
    package = checked_path(directory / "guardrail_core", directory, exists=True)
    for filename in ("__init__.py", "evaluator.py", "models.py"):
        checked_path(package / filename, package, exists=True)
    validate_modules(package)  # Reject conflicting caches before any import.
    existing = PathFinder.find_spec("guardrail_core", sys.path)
    if existing is not None:
        if not existing.origin or checked_path(existing.origin, package, exists=True) != package / "__init__.py":
            raise ValueError("Configured Guardrail installation is shadowed")
    guards = [finder for finder in sys.meta_path if isinstance(finder, ApprovedFinder)]
    if len(guards) > 1 or any(finder.package != package for finder in guards):
        raise ValueError("Restart before switching the approved Guardrail installation")
    finder = guards[0] if guards else ApprovedFinder(directory, package)
    if guards:
        sys.meta_path.remove(finder)
    sys.meta_path.insert(0, finder)
    before = set(sys.modules)
    try:
        evaluator = importlib.import_module("guardrail_core.evaluator")
        models = importlib.import_module("guardrail_core.models")
        validate_modules(package)
        evaluate = getattr(evaluator, "evaluate", None) or evaluator.evaluate_request
        checked_path(inspect.getfile(evaluate), package, exists=True)
        checked_path(inspect.getfile(models.DecisionRequest), package, exists=True)
        return evaluator, models
    except Exception:
        # A failed first load must not leave a partial package or a new guard.
        if not guards:
            sys.meta_path.remove(finder)
            for name in set(sys.modules) - before:
                if name == "guardrail_core" or name.startswith("guardrail_core."):
                    sys.modules.pop(name, None)
        raise
    # Keep the guard first for subsequent lazy imports, including modules missing
    # from the approved installation: those raise rather than trying another hook.
    # Switching installations or mutating import hooks requires a process restart.
