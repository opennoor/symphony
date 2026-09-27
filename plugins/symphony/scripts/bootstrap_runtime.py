"""Embedded in reviewed hook commands; retain that exact runtime across updates.

EXPECTED and DIGEST are embedded by generate_hooks.py, never read from disk.
"""

import hashlib
import os
from pathlib import Path
import runpy
import shutil
import sys
import tempfile


def verified(root, snapshot=False):
    if root.is_symlink():
        return False
    if snapshot and root.exists():
        entries = list(root.rglob("*"))
        if any(path.is_symlink() for path in entries):
            return False
        if {path.relative_to(root).as_posix() for path in entries if path.is_file()} != set(EXPECTED):
            return False
    aggregate = hashlib.sha256()
    for relative in EXPECTED:
        path = root / relative
        try:
            path.resolve().relative_to(root.resolve())
            if path.is_symlink():
                return False
            aggregate.update(relative.encode() + b"\0" + hashlib.sha256(path.read_bytes()).digest())
        except (OSError, ValueError):
            return False
    return aggregate.hexdigest() == DIGEST


def main():
    source = Path(sys.argv[1]).resolve()
    provider = sys.argv[2]
    base = Path(os.environ.get("SYMPHONY_RUNTIME_DIR", Path.home() / ".symphony" / "runtimes"))
    # ponytail: retain snapshots; remove old ones only after their sessions exit.
    target = base / DIGEST
    if not verified(target, snapshot=True):
        if target.exists():
            raise RuntimeError("retained Symphony runtime changed; refusing to execute it")
        if not verified(source):
            raise RuntimeError("reviewed Symphony runtime is missing or changed; reload the plugin")
        base.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = Path(tempfile.mkdtemp(prefix=".pin-", dir=base))
        try:
            for relative in EXPECTED:
                destination = temporary / relative
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / relative, destination)
            if not verified(temporary, snapshot=True):
                raise RuntimeError("Symphony runtime changed while retaining it; reload the plugin")
            try:
                temporary.rename(target)
            except OSError:
                # Another trusted hook may have finished the same snapshot.
                if not verified(target, snapshot=True):
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    os.environ["SYMPHONY_PROVIDER"] = provider
    os.environ["SYMPHONY_PLUGIN_ROOT"] = str(source)
    os.environ["SYMPHONY_PINNED_RUNTIME"] = str(target)
    if "-c" in sys.orig_argv:
        os.environ["SYMPHONY_BOOTSTRAP_CODE"] = sys.orig_argv[sys.orig_argv.index("-c") + 1]
    # Bytecode caches could execute bytes outside the reviewed source hashes.
    sys.dont_write_bytecode = True
    checker = len(sys.argv) > 3 and sys.argv[3] == "--check-activation"
    script = target / "scripts" / ("check_activation.py" if checker else "symphony_hook.py")
    if checker:
        sys.argv = [str(script), "--plugin-root", str(source)]
    runpy.run_path(str(script), run_name="__main__")


try:
    main()
except (OSError, RuntimeError) as error:
    print(f"Symphony launcher: {error}", file=sys.stderr)
    raise SystemExit(1)
