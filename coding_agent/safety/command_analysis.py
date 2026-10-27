"""AST-backed, conservative shell command classification."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, Field

from coding_agent.safety.shell_ir import ParserStatus, ShellKind, ShellParseResult
from coding_agent.safety.shell_parsers import ShellParser, parser_for


class CommandRisk(str, Enum):
    READ_ONLY = "read_only"
    BUILD_TEST = "build_test"
    WRITE = "write"
    NETWORK = "network"
    INSTALL = "install"
    DESTRUCTIVE = "destructive"
    OPAQUE = "opaque"
    UNKNOWN = "unknown"


class CommandOperation(BaseModel):
    risk: CommandRisk
    rule_id: str
    executable: str | None = None
    argv: list[str] = Field(default_factory=list)
    target: str | None = None
    start: int = Field(default=0, ge=0)
    end: int = Field(default=0, ge=0)
    hard_deny: bool = False


class CommandAnalysis(BaseModel):
    command_present: bool
    risk: CommandRisk
    matched_rules: list[str] = Field(default_factory=list)
    operations: list[CommandOperation] = Field(default_factory=list)
    parse_result: ShellParseResult | None = None
    heuristic: bool = False


_PRECEDENCE = {
    CommandRisk.READ_ONLY: 1,
    CommandRisk.BUILD_TEST: 2,
    CommandRisk.UNKNOWN: 3,
    CommandRisk.OPAQUE: 4,
    CommandRisk.WRITE: 5,
    CommandRisk.NETWORK: 6,
    CommandRisk.INSTALL: 7,
    CommandRisk.DESTRUCTIVE: 8,
}

_READ_ONLY = {
    "get-childitem", "get-content", "get-location", "select-string",
    "select-object", "measure-object", "test-path", "write-output",
    "rg", "ls", "dir", "pwd", "cat", "head", "tail", "wc", "which",
    "where", "where.exe", "printenv", "env", "echo", "printf", "type",
}
_WRITE = {
    "set-content", "add-content", "out-file", "copy-item", "new-item",
    "rename-item", "set-item", "clear-content", "tee", "touch", "mkdir",
    "cp", "install",
}
_DESTRUCTIVE = {
    "remove-item", "move-item", "rename-item", "rm", "rmdir", "mv",
    "del", "erase", "shred", "unlink", "rename",
}
_NETWORK = {
    "invoke-webrequest", "invoke-restmethod", "start-bitstransfer", "curl",
    "curl.exe", "wget", "ssh", "scp", "sftp", "nc", "ncat", "telnet",
}
_OPAQUE = {"invoke-expression", "iex", "xargs", "eval", "source", "."}
def _basename(executable: str | None) -> str:
    if not executable:
        return ""
    value = executable.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return value[:-4] if value.endswith(".exe") else value


def _git_subcommand(argv: list[str]) -> str:
    value_options = {
        "-c", "--git-dir", "--work-tree", "--namespace",
        "--super-prefix", "--config-env", "--exec-path",
    }
    index = 0
    while index < len(argv):
        item = argv[index]
        if item.lower() in value_options:
            index += 2
            continue
        if item.startswith("-"):
            index += 1
            continue
        return item.lower()
    return ""


def _encoded_command_flag(value: str) -> bool:
    candidate = value.lower().split(":", 1)[0].split("=", 1)[0]
    return (
        candidate in {"-e", "-ec"}
        or (
            len(candidate) >= len("-enc")
            and candidate.startswith("-enc")
            and "-encodedcommand".startswith(candidate)
        )
    )


def _uv_test_entry(argv: list[str]) -> bool:
    """Recognize the actual executable after a small set of inert run flags.

    In particular, a later positional argument named 'pytest' says nothing
    about the program uv will execute. Unknown/value-taking run flags ask.
    """
    if argv[:1] != ["run"]:
        return False
    remaining = argv[1:]
    while remaining and remaining[0] in {"--no-sync", "--offline", "--frozen", "--locked"}:
        remaining = remaining[1:]
    if remaining[:1] == ["--"]:
        remaining = remaining[1:]
    return remaining[:1] == ["pytest"] or remaining[:3] in (
        ["python", "-m", "pytest"], ["python3", "-m", "pytest"],
    )


def _git_read_risk(argv: list[str], subcommand: str) -> tuple[CommandRisk, str]:
    options = {item.split("=", 1)[0] for item in argv if item.startswith("-")}
    # Git accepts unambiguous long-option abbreviations and attached -c values.
    if any(option.startswith("--") and "--output".startswith(option) for option in options):
        return CommandRisk.WRITE, "git-output-write"
    external = {"--config-env", "--exec-path", "--ext-diff", "--textconv", "--paginate", "--open-files-in-pager"}
    subcommand_index = next(
        (index for index, item in enumerate(argv) if item.lower() == subcommand), len(argv),
    )
    if (
        any(option.startswith("-c") for option in options)
        or any(option.startswith("--") and any(flag.startswith(option) for flag in external) for option in options)
        or "-p" in argv[:subcommand_index]
    ):
        return CommandRisk.OPAQUE, "git-external-command"
    return CommandRisk.READ_ONLY, f"git-read:{subcommand}"


def _operation(node) -> CommandOperation:
    executable = _basename(node.executable)
    argv = list(node.argv)
    lower = [item.lower() for item in argv]

    if executable in _DESTRUCTIVE:
        return CommandOperation(
            risk=CommandRisk.DESTRUCTIVE, rule_id=f"destructive:{executable}",
            executable=executable, argv=argv, start=node.start, end=node.end,
            hard_deny=True,
        )
    if executable == "git":
        subcommand = _git_subcommand(argv)
        if subcommand == "reset" and any(
            item == "--hard" or item.startswith("--hard=") for item in lower
        ):
            return CommandOperation(
                risk=CommandRisk.DESTRUCTIVE, rule_id="git-reset-hard",
                executable=executable, argv=argv, start=node.start, end=node.end,
                hard_deny=True,
            )
        if subcommand == "clean" and any(
            item == "--force" or (item.startswith("-") and "f" in item[1:])
            for item in lower
        ):
            return CommandOperation(
                risk=CommandRisk.DESTRUCTIVE, rule_id="git-clean-force",
                executable=executable, argv=argv, start=node.start, end=node.end,
                hard_deny=True,
            )
        if subcommand == "push" and any(
            item == "-f"
            or (item.startswith("-") and not item.startswith("--") and "f" in item[1:])
            or item == "--force"
            or item.startswith("--force-with-lease")
            or item.startswith("+")
            for item in lower
        ):
            return CommandOperation(
                risk=CommandRisk.DESTRUCTIVE, rule_id="git-force-push",
                executable=executable, argv=argv, start=node.start, end=node.end,
                hard_deny=True,
            )
        if subcommand in {"fetch", "pull", "push", "clone", "remote"}:
            return CommandOperation(
                risk=CommandRisk.NETWORK, rule_id=f"git-network:{subcommand}",
                executable=executable, argv=argv, start=node.start, end=node.end,
            )
        if subcommand in {"status", "diff", "log", "show", "rev-parse"}:
            risk, rule = _git_read_risk(argv, subcommand)
            if node.dynamic and risk == CommandRisk.READ_ONLY:
                risk, rule = CommandRisk.UNKNOWN, "dynamic:git"
            return CommandOperation(
                risk=risk, rule_id=rule,
                executable=executable, argv=argv, start=node.start, end=node.end,
            )
        if subcommand in {
            "restore", "checkout", "switch", "commit", "merge", "rebase", "add"
        }:
            return CommandOperation(
                risk=CommandRisk.WRITE, rule_id=f"git-write:{subcommand}",
                executable=executable, argv=argv, start=node.start, end=node.end,
            )

    if executable in {"powershell", "pwsh"} and any(
        _encoded_command_flag(item) for item in lower
    ):
        return CommandOperation(
            risk=CommandRisk.DESTRUCTIVE, rule_id="encoded-command",
            executable=executable, argv=argv, start=node.start, end=node.end,
            hard_deny=True,
        )
    if executable in _OPAQUE:
        hard = executable in {"invoke-expression", "iex"}
        return CommandOperation(
            risk=CommandRisk.DESTRUCTIVE if hard else CommandRisk.OPAQUE,
            rule_id=f"opaque:{executable}", executable=executable, argv=argv,
            start=node.start, end=node.end, hard_deny=hard,
        )
    if executable in {
        "python", "python3", "node", "bash", "sh", "zsh", "powershell", "pwsh"
    }:
        opaque_flags = {"-c", "-e", "-command"}
        if any(
            item in opaque_flags or _encoded_command_flag(item) for item in lower
        ):
            return CommandOperation(
                risk=CommandRisk.OPAQUE, rule_id=f"interpreter-code:{executable}",
                executable=executable, argv=argv, start=node.start, end=node.end,
            )

    if executable in {"pip", "pip3"} and lower[:1] == ["install"]:
        risk, rule = CommandRisk.INSTALL, "package-install:pip"
    elif executable in {"python", "python3"} and len(lower) >= 3 and lower[:3] == ["-m", "pip", "install"]:
        risk, rule = CommandRisk.INSTALL, "package-install:pip"
    elif executable == "uv" and lower[:1] in (["add"], ["sync"]):
        risk, rule = CommandRisk.INSTALL, "package-install:uv"
    elif executable in {"npm", "pnpm", "yarn"} and lower[:1] and lower[0] in {"install", "i", "add"}:
        risk, rule = CommandRisk.INSTALL, f"package-install:{executable}"
    elif executable in {"winget", "choco", "apt", "apt-get", "dnf", "yum", "brew"} and "install" in lower:
        risk, rule = CommandRisk.INSTALL, f"package-install:{executable}"
    elif executable in _NETWORK:
        risk, rule = CommandRisk.NETWORK, f"network:{executable}"
    elif executable in _WRITE or (
        executable == "sed"
        and any("i" in item[1:] for item in lower if item.startswith("-"))
    ):
        risk, rule = CommandRisk.WRITE, f"filesystem-write:{executable}"
    elif executable == "find" and "-delete" in lower:
        risk, rule = CommandRisk.DESTRUCTIVE, "find-delete"
    elif executable == "find" and any(
        item in {"-exec", "-execdir", "-ok", "-okdir"} for item in lower
    ):
        risk, rule = CommandRisk.OPAQUE, "find-exec"
    elif executable in {"pytest", "ruff", "dotnet", "cargo", "go"}:
        allowed = (
            executable == "pytest"
            or (executable == "ruff" and lower[:1] == ["check"] and "--fix" not in lower)
            or (executable == "dotnet" and lower[:1] == ["test"])
            or (executable == "cargo" and lower[:1] in (["test"], ["check"]))
            or (executable == "go" and lower[:1] == ["test"])
        )
        risk, rule = (
            (CommandRisk.BUILD_TEST, f"build-test:{executable}")
            if allowed else (CommandRisk.UNKNOWN, f"unvalidated:{executable}")
        )
    elif executable in {"python", "python3"} and len(lower) >= 2 and lower[:2] in (
        ["-m", "pytest"], ["-m", "compileall"],
    ):
        risk, rule = CommandRisk.BUILD_TEST, "build-test:python-module"
    elif executable == "uv" and _uv_test_entry(argv):
        risk, rule = CommandRisk.BUILD_TEST, "build-test:uv"
    elif executable == "uv" and lower[:1] == ["run"]:
        risk, rule = CommandRisk.OPAQUE, "unvalidated:uv-run"
    elif executable in {"npm", "pnpm", "yarn"} and lower[:1] == ["test"]:
        # Package-manager scripts are project-defined arbitrary code, not a
        # parameter-validated test runner.  They must go through approval/AUTO.
        risk, rule = CommandRisk.OPAQUE, f"package-script:{executable}"
    elif executable == "rg" and any(
        item.split("=", 1)[0] in {"--pre", "--hostname-bin"} for item in argv
    ):
        risk, rule = CommandRisk.OPAQUE, "rg-external-command"
    elif executable in _READ_ONLY:
        risk, rule = CommandRisk.READ_ONLY, f"read-only:{executable}"
    else:
        risk, rule = CommandRisk.UNKNOWN, f"unknown:{executable or 'dynamic'}"

    if node.dynamic and _PRECEDENCE[CommandRisk.UNKNOWN] > _PRECEDENCE[risk]:
        risk, rule = CommandRisk.UNKNOWN, f"dynamic:{executable or 'command'}"
    return CommandOperation(
        risk=risk, rule_id=rule, executable=executable or None, argv=argv,
        start=node.start, end=node.end,
        hard_deny=(risk == CommandRisk.DESTRUCTIVE),
    )


def analyze_command(
    command: str,
    *,
    shell: ShellKind | str = ShellKind.POWERSHELL,
    parser: ShellParser | None = None,
) -> CommandAnalysis:
    if not isinstance(command, str) or not command.strip():
        return CommandAnalysis(command_present=False, risk=CommandRisk.UNKNOWN)

    parse_result = (parser or parser_for(shell)).parse(command)
    operations = [_operation(node) for node in parse_result.commands]

    for redirection in parse_result.redirections:
        if redirection.operator not in {"<", "<<", "<<<", "fd_dup"}:
            operations.append(CommandOperation(
                risk=CommandRisk.WRITE,
                rule_id="filesystem-write:redirection",
                target=redirection.target,
                start=redirection.start,
                end=redirection.end,
            ))

    if parse_result.status != ParserStatus.OK:
        operations.append(CommandOperation(
            risk=CommandRisk.UNKNOWN,
            rule_id=f"parser-{parse_result.status.value}",
        ))
    if not operations:
        operations.append(CommandOperation(
            risk=CommandRisk.UNKNOWN, rule_id="no-command-node",
        ))

    risk = max((item.risk for item in operations), key=_PRECEDENCE.__getitem__)
    return CommandAnalysis(
        command_present=True,
        risk=risk,
        matched_rules=[item.rule_id for item in operations if item.risk == risk],
        operations=operations,
        parse_result=parse_result,
        heuristic=parse_result.status in {ParserStatus.UNAVAILABLE, ParserStatus.ERROR},
    )
