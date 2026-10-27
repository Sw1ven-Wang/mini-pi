"""Offline runtime maintenance. Stop the service first; writer locking is mandatory."""

import argparse
import json
from pathlib import Path

from coding_agent.filesystem.workspace import Workspace
from coding_agent.runtime.kernel import RuntimeKernel, workspace_identity
from coding_agent.runtime.maintenance import RuntimeMaintenance, restore_backup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--state-root", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("inspect")
    backup = commands.add_parser("backup")
    backup.add_argument("--destination", type=Path, required=True)
    restore = commands.add_parser("restore")
    restore.add_argument("--source", type=Path, required=True)
    compact = commands.add_parser("compact")
    compact.add_argument("--backup-directory", type=Path, required=True)
    prune = commands.add_parser("prune")
    prune.add_argument("--keep-days", type=int, default=30)
    prune.add_argument("--max-blob-bytes", type=int)
    prune.add_argument("--apply", action="store_true")
    prune.add_argument("--backup-directory", type=Path)
    args = parser.parse_args()
    workspace = Workspace(args.workspace)
    if args.state_root.resolve().is_relative_to(workspace.root):
        parser.error("state must be outside workspace")
    if args.command == "restore":
        if args.source.resolve().is_relative_to(workspace.root):
            parser.error("private backup source must be outside workspace")
        restore_backup(args.source, args.state_root / workspace_identity(workspace.root))
        return
    if not (args.state_root / workspace_identity(workspace.root) / "runtime.db").is_file():
        parser.error("no existing runtime database; use the service's exact --state-root")
    kernel = RuntimeKernel(workspace, state_root=args.state_root)
    try:
        kernel.mark_interrupted()  # Exclusive writer lock proves no live host executor.
        maintenance = RuntimeMaintenance(kernel.store)
        if args.command == "inspect":
            result = maintenance.inspect()
        elif args.command == "backup":
            if args.destination.resolve().is_relative_to(workspace.root):
                parser.error("private backup must be outside workspace")
            result = maintenance.backup(args.destination)
        elif args.command == "compact":
            if args.backup_directory.resolve().is_relative_to(workspace.root):
                parser.error("private backup must be outside workspace")
            result = maintenance.compact(backup_directory=args.backup_directory)
        else:
            options = dict(keep_days=args.keep_days, max_blob_bytes=args.max_blob_bytes)
            if args.apply:
                if not args.backup_directory or args.backup_directory.resolve().is_relative_to(workspace.root):
                    parser.error("prune --apply requires a private backup outside workspace")
                result = maintenance.prune(backup_directory=args.backup_directory, **options)
            else:
                result = maintenance.retention_plan(**options)
        print(json.dumps(result, ensure_ascii=False))
        if result.get("quota_met") is False:
            raise SystemExit(2)
    finally:
        kernel.close()


if __name__ == "__main__":
    main()
