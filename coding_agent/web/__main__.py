"""Launch the local Mini-Pi workbench."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from coding_agent.web.app import create_app
from coding_agent.web.config import ProviderRegistry, parse_allowed_root
from coding_agent.runtime.acceptance import AcceptanceContract


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local Mini-Pi workbench")
    parser.add_argument(
        "--allow-root",
        action="append",
        required=True,
        help='Allowed workspace parent as "Label=absolute-path"; repeatable',
    )
    parser.add_argument("--provider-config", type=Path, required=True)
    parser.add_argument(
        "--verify-command",
        default="uv run python -m pytest -q",
        help="Host-owned verification command displayed read-only in the UI",
    )
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--state-root", type=Path, help="Private runtime storage outside executable workspaces")
    parser.add_argument("--acceptance-config", type=Path, help="Host-owned AcceptanceContract JSON")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    roots = [parse_allowed_root(value) for value in args.allow_root]
    if len({root.id for root in roots}) != len(roots):
        parser.error("--allow-root values resolve to duplicate roots")
    providers = ProviderRegistry.from_file(args.provider_config.resolve(strict=True))
    static_dir = Path(__file__).resolve().parents[2] / "web" / "dist"
    app = create_app(
        roots=roots,
        providers=providers,
        verify_command=args.verify_command,
        static_dir=static_dir,
        state_root=args.state_root,
        acceptance_contract=(AcceptanceContract.model_validate_json(args.acceptance_config.read_bytes())
                             if args.acceptance_config else None),
    )
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)


if __name__ == "__main__":
    main()
