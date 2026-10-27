"""PowerShell and Bash parsers producing the shared shell IR.

The PowerShell bridge parses input with ``Parser.ParseInput`` and never executes
the inspected command. Bash uses the official tree-sitter Python bindings and
the tree-sitter-bash grammar.
"""

from __future__ import annotations

import atexit
import base64
import hashlib
import json
import queue
import re
import shlex
import shutil
import subprocess
import threading
from functools import lru_cache
from pathlib import Path
from typing import Protocol

from coding_agent.safety.shell_ir import (
    ParserStatus,
    ShellCommandNode,
    ShellKind,
    ShellParseResult,
    ShellRedirection,
)


class ShellParser(Protocol):
    shell: ShellKind

    def parse(self, command: str) -> ShellParseResult: ...


_PS_ALIASES = {
    "cat": "get-content",
    "cd": "set-location",
    "cp": "copy-item",
    "copy": "copy-item",
    "cpi": "copy-item",
    "curl": "invoke-webrequest",
    "del": "remove-item",
    "dir": "get-childitem",
    "echo": "write-output",
    "erase": "remove-item",
    "gc": "get-content",
    "gci": "get-childitem",
    "iwr": "invoke-webrequest",
    "irm": "invoke-restmethod",
    "ls": "get-childitem",
    "mi": "move-item",
    "move": "move-item",
    "mv": "move-item",
    "ni": "new-item",
    "pwd": "get-location",
    "ren": "rename-item",
    "rename": "rename-item",
    "ri": "remove-item",
    "rni": "rename-item",
    "rm": "remove-item",
    "rd": "remove-item",
    "rmdir": "remove-item",
    "sc": "set-content",
    "select": "select-object",
    "sls": "select-string",
    "type": "get-content",
}
_BASH_WRAPPERS = {
    "env", "time", "timeout", "nice", "nohup", "stdbuf", "command",
    "builtin", "noglob", "busybox", "toybox",
}
_FD_DUP_RE = re.compile(r"^\s*(?P<fd>\d*)\s*(?P<op>[<>]&)\s*(?P<target>\d+|-)\s*$")
_REDIRECT_RE = re.compile(r"(?P<fd>\d*)\s*(?P<op>&>|>>?|<<|<)\s*(?P<target>[^\s|;&]+)?")
_POWERSHELL_BRIDGE_SHA256 = (
    "62eefac99ff25a335ac3fcdff869f8883f1705b993671007740075d1e96553a2"
)
MAX_COMMAND_BYTES = 100_000


def _command_hash(command: str) -> str:
    return hashlib.sha256(command.encode("utf-8", errors="replace")).hexdigest()


def _executable_name(value: str | None) -> str:
    if not value:
        return ""
    name = value.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _literal(text: str) -> tuple[str, bool]:
    value = text.strip()
    dynamic = bool(re.search(r"\$|`|\$\(|<\(|>\(", value))
    try:
        parsed = shlex.split(value, posix=True)
        if len(parsed) == 1:
            value = parsed[0]
    except ValueError:
        dynamic = True
    return value, dynamic


def _redirection(text: str, start: int = 0, end: int = 0) -> ShellRedirection:
    duplicate = _FD_DUP_RE.match(text)
    if duplicate is not None:
        return ShellRedirection(
            operator="fd_dup", target=duplicate.group("target"),
            fd=duplicate.group("fd") or None, start=start, end=end,
        )
    match = _REDIRECT_RE.search(text)
    if match is None:
        return ShellRedirection(operator=text, dynamic=True, start=start, end=end)
    target = match.group("target")
    normalized, dynamic = _literal(target) if target else (None, True)
    return ShellRedirection(
        operator=match.group("op"), target=normalized, fd=match.group("fd") or None,
        dynamic=dynamic, start=start, end=end,
    )


def _unwrap_bash(
    executable: str | None,
    argv: list[str],
    assignments: dict[str, str],
) -> tuple[str | None, list[str], list[str], dict[str, str], bool]:
    """Remove well-known non-semantic wrappers without hiding their payload."""
    wrappers: list[str] = []
    dynamic = False
    remaining = list(argv)
    while _executable_name(executable) in _BASH_WRAPPERS:
        wrapper = _executable_name(executable)
        wrappers.append(wrapper)
        if wrapper == "env":
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
            while remaining and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", remaining[0]):
                key, value = remaining.pop(0).split("=", 1)
                assignments[key] = value
        elif wrapper == "timeout":
            while remaining and remaining[0].startswith("-"):
                option = remaining.pop(0)
                if option in {"-k", "--kill-after", "-s", "--signal"} and remaining:
                    remaining.pop(0)
            if remaining:  # duration
                remaining.pop(0)
        elif wrapper == "nice":
            while remaining and remaining[0].startswith("-"):
                option = remaining.pop(0)
                if option in {"-n", "--adjustment"} and remaining:
                    remaining.pop(0)
        else:
            while remaining and remaining[0].startswith("-"):
                remaining.pop(0)
        if not remaining:
            return None, remaining, wrappers, assignments, True
        executable = remaining.pop(0).lower()
        dynamic = dynamic or bool(re.search(r"\$|`", executable))
    return _executable_name(executable) or None, remaining, wrappers, assignments, dynamic


class PowerShellParser:
    shell = ShellKind.POWERSHELL

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or shutil.which("pwsh") or shutil.which("powershell")
        self.bridge = Path(__file__).with_name("powershell_ast_bridge.ps1")
        self._process: subprocess.Popen[str] | None = None
        self._responses: queue.Queue[str | None] | None = None
        self._reader_thread: threading.Thread | None = None
        self._lock = threading.Lock()
        atexit.register(self.close)

    def _start(self) -> subprocess.Popen[str]:
        if not self.executable:
            raise FileNotFoundError("PowerShell executable is unavailable")
        if self._process is None or self._process.poll() is not None:
            bridge_bytes = self.bridge.read_bytes().removeprefix(b"\xef\xbb\xbf")
            bridge_digest = hashlib.sha256(
                bridge_bytes.replace(b"\r\n", b"\n")
            ).hexdigest()
            if bridge_digest != _POWERSHELL_BRIDGE_SHA256:
                raise RuntimeError("PowerShell parser bridge integrity check failed")
            self._process = subprocess.Popen(
                [
                    self.executable, "-NoLogo", "-NoProfile", "-NonInteractive",
                    "-ExecutionPolicy", "Bypass", "-File", str(self.bridge),
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            process = self._process
            assert process.stdout is not None
            responses: queue.Queue[str | None] = queue.Queue()
            self._responses = responses

            def read_responses() -> None:
                assert process.stdout is not None
                for line in process.stdout:
                    responses.put(line)
                responses.put(None)

            self._reader_thread = threading.Thread(
                target=read_responses,
                name="mini-pi-powershell-parser",
                daemon=True,
            )
            self._reader_thread.start()
        return self._process

    @lru_cache(maxsize=128)
    def parse(self, command: str) -> ShellParseResult:
        digest = _command_hash(command)
        if not isinstance(command, str) or not command.strip():
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.ERROR,
                errors=["command is empty"], command_hash=digest,
            )
        if "\x00" in command or len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.ERROR,
                errors=["command exceeds parser input limits"], command_hash=digest,
            )
        try:
            with self._lock:
                process = self._start()
                assert process.stdin is not None and process.stdout is not None
                encoded = base64.b64encode(command.encode("utf-8")).decode("ascii")
                process.stdin.write(encoded + "\n")
                process.stdin.flush()
                assert self._responses is not None
                response = self._responses.get(timeout=5)
            if not response:
                raise RuntimeError("PowerShell parser bridge stopped unexpectedly")
            data = json.loads(base64.b64decode(response.strip()).decode("utf-8"))
        except Exception as exc:
            self.close()
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.UNAVAILABLE,
                errors=[type(exc).__name__], command_hash=digest,
            )

        errors = [str(item) for item in data.get("errors", [])]
        env_assignments = {
            str(item.get("left", ""))[5:]: str(item.get("right", ""))
            for item in data.get("assignments", [])
            if str(item.get("left", "")).lower().startswith("$env:")
        }
        commands: list[ShellCommandNode] = []
        all_redirections: list[ShellRedirection] = []
        for index, item in enumerate(data.get("commands", [])):
            elements = [str(part) for part in item.get("elements", [])]
            raw_name = item.get("name")
            executable = _executable_name(str(raw_name)) if raw_name else None
            if executable:
                executable = _PS_ALIASES.get(executable, executable)
            argv: list[str] = []
            dynamic = executable is None
            for raw in elements[1:]:
                value, item_dynamic = _literal(raw)
                argv.append(value)
                dynamic = dynamic or item_dynamic
            redirections = [
                _redirection(
                    str(redir.get("text", "")),
                    int(redir.get("start", 0)), int(redir.get("end", 0)),
                )
                for redir in item.get("redirects", [])
            ]
            all_redirections.extend(redirections)
            commands.append(ShellCommandNode(
                executable=executable,
                argv=argv,
                assignments=(env_assignments if index == 0 else {}),
                redirections=redirections,
                start=int(item.get("start", 0)), end=int(item.get("end", 0)),
                depth=int(item.get("depth", 0)), dynamic=dynamic,
            ))
        status = ParserStatus.ERROR if errors else (ParserStatus.OK if commands else ParserStatus.PARTIAL)
        return ShellParseResult(
            shell=self.shell, status=status, commands=commands,
            operators=[str(item) for item in data.get("operators", [])],
            redirections=all_redirections, errors=errors, command_hash=digest,
        )

    def close(self) -> None:
        process, self._process = self._process, None
        self._responses = None
        self._reader_thread = None
        if process is None or process.poll() is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            process.wait(timeout=1)
        except Exception:
            process.kill()


class BashParser:
    shell = ShellKind.BASH

    def __init__(self) -> None:
        self._parser = None
        self._load_error: str | None = None
        try:
            from tree_sitter import Language, Parser
            import tree_sitter_bash

            self._parser = Parser(Language(tree_sitter_bash.language()))
        except Exception as exc:  # pragma: no cover - dependency/platform fallback
            self._load_error = type(exc).__name__

    @lru_cache(maxsize=128)
    def parse(self, command: str) -> ShellParseResult:
        digest = _command_hash(command)
        if not isinstance(command, str) or not command.strip():
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.ERROR,
                errors=["command is empty"], command_hash=digest,
            )
        if "\x00" in command or len(command.encode("utf-8")) > MAX_COMMAND_BYTES:
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.ERROR,
                errors=["command exceeds parser input limits"], command_hash=digest,
            )
        if self._parser is None:
            return ShellParseResult(
                shell=self.shell, status=ParserStatus.UNAVAILABLE,
                errors=[self._load_error or "tree-sitter-bash unavailable"],
                command_hash=digest,
            )

        source = command.encode("utf-8")
        tree = self._parser.parse(source)
        root = tree.root_node
        errors: list[str] = []
        command_nodes = []
        operators: list[str] = []
        redirect_nodes = []

        def walk(node, depth: int = 0) -> None:
            if node.type in {"ERROR", "MISSING"} or node.is_missing:
                errors.append(f"{node.type}@{node.start_byte}:{node.end_byte}")
            if node.type == "command":
                command_nodes.append((node, depth))
            if "redirect" in node.type:
                redirect_nodes.append(node)
            if not node.is_named:
                token = source[node.start_byte:node.end_byte].decode("utf-8", "replace")
                if token in {"&&", "||", "|", "|&", ";", "&", "\n"}:
                    operators.append(token)
            nested = depth + (1 if node.type in {"subshell", "command_substitution"} else 0)
            for child in node.children:
                walk(child, nested)

        walk(root)
        all_redirections = [
            _redirection(
                source[node.start_byte:node.end_byte].decode("utf-8", "replace"),
                node.start_byte, node.end_byte,
            )
            for node in redirect_nodes
            if node.type in {"file_redirect", "heredoc_redirect", "herestring_redirect"}
        ]
        commands: list[ShellCommandNode] = []
        for node, depth in command_nodes:
            name_node = node.child_by_field_name("name")
            raw_name = (
                source[name_node.start_byte:name_node.end_byte].decode("utf-8", "replace")
                if name_node is not None else ""
            )
            executable, dynamic = _literal(raw_name)
            executable = executable.lower() if executable else None
            argv: list[str] = []
            assignments: dict[str, str] = {}
            for child in node.named_children:
                raw = source[child.start_byte:child.end_byte].decode("utf-8", "replace")
                if child.type == "variable_assignment" and "=" in raw:
                    key, value = raw.split("=", 1)
                    assignments[key] = value
                elif child != name_node and child.type not in {"file_redirect"}:
                    value, item_dynamic = _literal(raw)
                    argv.append(value)
                    dynamic = dynamic or item_dynamic

            executable, argv, wrappers, assignments, unwrap_dynamic = _unwrap_bash(
                executable, argv, assignments,
            )
            dynamic = dynamic or unwrap_dynamic

            local_redirections = [
                redir for redir in all_redirections
                if node.start_byte <= redir.start and redir.end <= node.end_byte
            ]
            commands.append(ShellCommandNode(
                executable=executable, argv=argv, assignments=assignments,
                wrappers=wrappers, redirections=local_redirections,
                start=node.start_byte, end=node.end_byte, depth=depth,
                dynamic=dynamic,
            ))
        for redirection in all_redirections:
            owners = [item for item in commands if item.start <= redirection.start]
            if owners:
                owner = max(owners, key=lambda item: item.start)
                if redirection not in owner.redirections:
                    owner.redirections.append(redirection)
        status = ParserStatus.ERROR if root.has_error else (ParserStatus.OK if commands else ParserStatus.PARTIAL)
        return ShellParseResult(
            shell=self.shell, status=status, commands=commands,
            operators=operators, redirections=all_redirections,
            errors=errors, command_hash=digest,
        )


_DEFAULT_POWERSHELL_PARSER = PowerShellParser()
_DEFAULT_BASH_PARSER = BashParser()


def parser_for(shell: ShellKind | str) -> ShellParser:
    kind = ShellKind(shell)
    if kind == ShellKind.POWERSHELL:
        return _DEFAULT_POWERSHELL_PARSER
    return _DEFAULT_BASH_PARSER
