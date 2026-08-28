"""Changed-range calculation."""

from __future__ import annotations

from coding_agent.changes.models import ChangedRange
from coding_agent.editing.exact import MatchedReplacement


def _line_number(
    text: str,
    offset: int,
) -> int:
    """将字符 offset 转换成 1-based 行号。"""

    if offset < 0:
        raise ValueError(
            "offset 不能小于 0"
        )

    return (
        text.count(
            "\n",
            0,
            offset,
        )
        + 1
    )


def build_changed_ranges(
    *,
    path: str,
    original: str,
    new_content: str,
    matches: list[MatchedReplacement],
) -> list[ChangedRange]:
    """根据 original snapshot 中的匹配位置生成修改行范围。"""

    ranges: list[ChangedRange] = []

    # 前面的 replacement 长度发生变化后，
    # 后面的 replacement 在新文件中的 offset 会随之变化。
    cumulative_delta = 0

    ordered = sorted(
        matches,
        key=lambda item: item.start,
    )

    for match in ordered:
        # -------------------------
        # old 文件位置
        # -------------------------

        old_start = match.start
        old_end = match.end

        old_start_line = _line_number(
            original,
            old_start,
        )

        old_end_line = _line_number(
            original,
            max(
                old_start,
                old_end - 1,
            ),
        )

        # -------------------------
        # new 文件位置
        # -------------------------

        new_start = (
            match.start
            + cumulative_delta
        )

        new_end = (
            new_start
            + len(match.new_text)
        )

        new_start_line = _line_number(
            new_content,
            new_start,
        )

        new_end_line = _line_number(
            new_content,
            max(
                new_start,
                new_end - 1,
            ),
        )

        ranges.append(
            ChangedRange(
                path=path,
                old_start_line=old_start_line,
                old_end_line=old_end_line,
                new_start_line=new_start_line,
                new_end_line=new_end_line,
            )
        )

        cumulative_delta += (
            len(match.new_text)
            - len(match.old_text)
        )

    return ranges