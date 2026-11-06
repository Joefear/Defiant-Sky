"""Copy only runtime code/policy to a new disposable directory; never import source.

Usage: python tools/prepare_guardrail_copy.py SOURCE_ROOT NEW_DISPOSABLE_DIRECTORY
The destination must not exist or overlap the source. No audit history is copied.
"""
import hashlib
import json
from pathlib import Path
import shutil
import sys


def prepare(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    if destination.exists() or destination.is_relative_to(source) or source.is_relative_to(destination):
        raise ValueError("Use a new disposable directory outside the installed repository")
    files = list((source / "packages/guardrail-core-python/guardrail_core").rglob("*.py"))
    files += list((source / "packages/core/dist").rglob("*.js"))
    files += [source / "packages/core/package.json", source / "policies/sda-ops-v1.yml"]
    if not (source / "packages/core/dist/bridge.js").is_file():
        raise ValueError("Installed compiled bridge missing; do not rebuild the owner's repository")
    manifest = []
    for path in files:
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(source):
            raise ValueError("Runtime path escapes source")
        relative = path.relative_to(source)
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        manifest.append({"path": relative.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    (destination / "source-manifest.json").write_text(json.dumps({"source_root": str(source), "files": manifest}, indent=2))
    print(f"Copied {len(manifest)} runtime files. Audit output will be isolated under the copy.")


if __name__ == "__main__":
    prepare(*sys.argv[1:])
