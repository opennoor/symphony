#!/usr/bin/env python3
"""Generate captured launchers pinned to the reviewed package's exact contents."""

import argparse
import ast
import base64
import gzip
import io
import hashlib
import json
import shlex
from pathlib import Path
import zlib


PLUGIN = Path(__file__).resolve().parents[1]


def bootstrap(root=PLUGIN):
    paths = [*root.glob("symphony/*.py"), *root.glob("agents/*.md"),
             *root.glob("commands/*.md"), *root.glob("skills/**/*.md"),
             root / "profiles.json", root / "model-policy.json",
             root / "scripts/symphony_hook.py", root / "scripts/check_activation.py"]
    expected = sorted(path.relative_to(root).as_posix() for path in paths)
    aggregate = hashlib.sha256()
    for relative in expected:
        aggregate.update(relative.encode() + b"\0" + hashlib.sha256((root / relative).read_bytes()).digest())
    tree = ast.parse((root / "scripts/bootstrap_runtime.py").read_text())
    tree.body = [node for node in tree.body if not (isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant))]
    source = "EXPECTED = " + repr(expected) + "\nDIGEST = " + repr(aggregate.hexdigest()) + "\n" + ast.unparse(tree)
    encoded = base64.b64encode(zlib.compress(source.encode(), level=9)).decode()
    return "import base64,zlib;exec(zlib.decompress(base64.b64decode('" + encoded + "')))"


def generated(root=PLUGIN):
    code = bootstrap(root)
    # The reviewed Windows command embeds this source; execution policy never
    # needs to permit an unsigned file or a script from a disappeared cache.
    relay = (root / "scripts/codex_hook.ps1").read_text()
    documents = {}
    for provider, filename, variable in (("codex", "codex.json", "PLUGIN_ROOT"),
                                          ("claude", "hooks.json", "CLAUDE_PLUGIN_ROOT")):
        path = root / "hooks" / filename
        document = json.loads(path.read_text())
        if provider == 'claude':
            # Root execution before admission must be observed as well as
            # Agent launches. Discovery and control tools remain available.
            for group in document['hooks']['PreToolUse']:
                group['matcher'] = 'Agent|SendMessage|Bash|PowerShell|Write|Edit|NotebookEdit'
        # Capture bootstrap once outside the encoded relay on both hosts.
        # Base64-expanding it again wastes the native Windows command budget.
        binding = "$b = [Environment]::GetEnvironmentVariable('SYMPHONY_CAPTURED_BOOTSTRAP')"
        source = relay.replace("$b = '__SYMPHONY_BOOTSTRAP__'", binding)
        buffer = io.BytesIO()
        # Stored DEFLATE avoids differing zlib/zlib-ng compression heuristics.
        # The complete relay still fits cmd.exe's bounded command length.
        with gzip.GzipFile(fileobj=buffer, mode='wb', mtime=0, compresslevel=0) as archive:
            archive.write(source.replace('__SYMPHONY_PROVIDER__', provider).encode())
        payload = base64.b64encode(buffer.getvalue()).decode()
        # Codex can parse this command through an outer PowerShell before CMD.
        # Inline the stream so its double-quoted argument has no $ variables
        # for that outer shell to expand before the inner PowerShell starts.
        wrapper = ("iex ([IO.StreamReader]::new([IO.Compression.GZipStream]::new("
                   "[IO.MemoryStream]::new([Convert]::FromBase64String('" + payload + "')),"
                   "[IO.Compression.CompressionMode]::Decompress))).ReadToEnd()")
        prefix = 'powershell.exe -NoProfile -NonInteractive -Command '
        capture = "[Environment]::SetEnvironmentVariable('SYMPHONY_CAPTURED_BOOTSTRAP','" + code.replace("'", "''") + "');"
        windows = 'cmd.exe /c ' + prefix + '"' + capture + wrapper + '"'
        if len(windows) > 8170:
            raise ValueError("Windows hook launcher exceeds cmd.exe's 8191-character limit")
        command = 'python3 -I -c "' + code + '" "${' + variable + '}" ' + provider
        if provider == "claude":
            command = ('(export SYMPHONY_CAPTURED_BOOTSTRAP=' + shlex.quote(code)
                       + '; if [ "${OS:-}" = Windows_NT ]; then ' + prefix + shlex.quote(wrapper)
                       + '; else python3 -I -c "$SYMPHONY_CAPTURED_BOOTSTRAP" "${'
                       + variable + '}" ' + provider + '; fi)')
            # Reserve room for Bash's native invocation and escaped quotes.
            if len(command) + 256 > 8170:
                raise ValueError("Claude hook launcher exceeds the Windows native command limit")
        for groups in document["hooks"].values():
            for group in groups:
                for hook in group["hooks"]:
                    hook["command"] = command
                    if provider == "codex":
                        hook["commandWindows"] = windows
        documents[path] = json.dumps(document, indent=2) + "\n"
    return documents


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    stale = False
    for path, content in generated().items():
        if args.check:
            if path.read_text() != content:
                print(f"::error::{path.name} launcher is stale; run scripts/generate_hooks.py")
                stale = True
        else:
            path.write_text(content)
    return int(stale)


if __name__ == "__main__":
    raise SystemExit(main())
