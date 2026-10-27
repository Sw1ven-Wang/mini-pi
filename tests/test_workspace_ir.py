"""Tests for Workspace Change IR."""

import pytest

from coding_agent.changes.ir import (
    ChangeSet,
    FileChange,
    TextReplacement,
)
from coding_agent.changes.workspace_ir import (
    CreateFileOp,
    DeleteFileOp,
    EditFileOp,
    MoveFileOp,
    WorkspaceChangeSet,
    text_change_set_to_workspace_change_set,
)
from coding_agent.filesystem.versions import FileVersion


def _version(digest="abc", size=10) -> FileVersion:
    return FileVersion(digest=digest, size_bytes=size)


def test_workspace_change_set_create():
    cs = WorkspaceChangeSet(
        operations=[
            CreateFileOp(path="src/new.py", content="x = 1\n")
        ]
    )

    op = cs.operations[0]

    assert isinstance(op, CreateFileOp)
    assert op.path == "src/new.py"
    assert op.content == "x = 1\n"


def test_workspace_change_set_edit():
    cs = WorkspaceChangeSet(
        operations=[
            EditFileOp(
                path="src/a.py",
                replacements=[
                    TextReplacement(
                        old_text="foo",
                        new_text="bar",
                    )
                ],
            )
        ]
    )

    assert isinstance(cs.operations[0], EditFileOp)


def test_workspace_change_set_delete():
    cs = WorkspaceChangeSet(
        operations=[
            DeleteFileOp(
                path="src/old.py",
                expected_version=_version(),
            )
        ]
    )

    assert isinstance(cs.operations[0], DeleteFileOp)


def test_workspace_change_set_move():
    cs = WorkspaceChangeSet(
        operations=[
            MoveFileOp(
                source="src/a.py",
                destination="src/b.py",
                expected_source_version=_version(),
            )
        ]
    )

    assert isinstance(cs.operations[0], MoveFileOp)


def test_workspace_change_set_discriminator():
    cs = WorkspaceChangeSet(
        operations=[
            CreateFileOp(path="a.py", content="1"),
            DeleteFileOp(path="b.py", expected_version=_version()),
            MoveFileOp(
                source="c.py",
                destination="d.py",
                expected_source_version=_version(),
            ),
            EditFileOp(
                path="e.py",
                replacements=[
                    TextReplacement(
                        old_text="x",
                        new_text="y",
                    )
                ],
            ),
        ]
    )

    kinds = [op.kind for op in cs.operations]

    assert kinds == [
        "create_file",
        "delete_file",
        "move_file",
        "edit_file",
    ]


def test_workspace_change_set_max_operations():
    with pytest.raises(ValueError):
        WorkspaceChangeSet(
            operations=[
                CreateFileOp(path=f"f{i}.py", content="1")
                for i in range(51)
            ]
        )


def test_old_change_set_adapter():
    old = ChangeSet(
        files=[
            FileChange(
                path="src/a.py",
                expected_version=None,
                replacements=[
                    TextReplacement(
                        old_text="foo",
                        new_text="bar",
                    )
                ],
            )
        ]
    )

    converted = text_change_set_to_workspace_change_set(old)

    assert len(converted.operations) == 1

    op = converted.operations[0]
    assert isinstance(op, EditFileOp)
    assert op.path == "src/a.py"
    assert op.replacements[0].old_text == "foo"
