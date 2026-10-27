"""Tests for unified Change IR models."""

from coding_agent.changes.ir import (
    ChangeSet,
    FileChange,
    TextReplacement,
)
from coding_agent.filesystem.versions import FileVersion


def test_change_set_validation():
    change_set = ChangeSet(
        files=[
            FileChange(
                path="src/a.py",
                replacements=[
                    TextReplacement(
                        old_text="foo()",
                        new_text="bar()",
                    )
                ],
            )
        ]
    )

    assert len(change_set.files) == 1
    assert change_set.files[0].path == "src/a.py"
    assert change_set.files[0].replacements[0].old_text == "foo()"


def test_file_change_expected_version():
    version = FileVersion(
        digest="abc123",
        size_bytes=10,
    )

    change = FileChange(
        path="src/a.py",
        expected_version=version,
        replacements=[
            TextReplacement(
                old_text="foo()",
                new_text="bar()",
            )
        ],
    )

    assert change.expected_version is not None
    assert change.expected_version.digest == "abc123"


def test_multiple_replacements():
    change = FileChange(
        path="src/a.py",
        replacements=[
            TextReplacement(old_text="a", new_text="A"),
            TextReplacement(old_text="b", new_text="B"),
            TextReplacement(old_text="c", new_text="C"),
        ],
    )

    assert len(change.replacements) == 3
