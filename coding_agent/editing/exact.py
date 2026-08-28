"""Exact text replacement."""

from __future__ import annotations

from pydantic import BaseModel

from coding_agent.errors import (
    AmbiguousMatchError,
    InvalidArgumentError,
    NoMatchError,
    NoOpEditError,
    OverlappingEditsError,
)


class ExactReplacement(BaseModel):
    old_text: str
    new_text: str


class MatchedReplacement(BaseModel):
    old_text: str
    new_text: str
    start: int
    end: int


def _find_all_occurrences(
    text: str,
    target: str,
) -> list[int]:
    """返回 target 在 text 中的所有匹配起始位置。

    使用 start + 1 继续搜索，因此连重叠匹配也能够检测出来。
    """

    positions: list[int] = []

    start = 0

    while True:
        index = text.find(
            target,
            start,
        )

        if index == -1:
            break

        positions.append(index)

        start = index + 1

    return positions


def plan_exact_replacements(
    original: str,
    edits: list[ExactReplacement],
) -> list[MatchedReplacement]:
    """针对同一个 original snapshot 规划所有 exact replacements。

    此函数只做：

    - 参数检查
    - 唯一匹配
    - 记录原始 offset

    不真正修改 original。
    """

    matches: list[MatchedReplacement] = []

    for edit in edits:

        # old_text 不能为空
        if edit.old_text == "":
            raise InvalidArgumentError(
                "old_text 不能为空"
            )

        # 修改前后完全相同
        if edit.old_text == edit.new_text:
            raise NoOpEditError()

        positions = _find_all_occurrences(
            original,
            edit.old_text,
        )

        # 0 match
        if not positions:
            raise NoMatchError(
                edit.old_text
            )

        # multiple matches
        if len(positions) > 1:
            raise AmbiguousMatchError(
                old_text=edit.old_text,
                occurrences=len(positions),
            )

        start = positions[0]

        end = (
            start
            + len(edit.old_text)
        )

        matches.append(
            MatchedReplacement(
                old_text=edit.old_text,
                new_text=edit.new_text,
                start=start,
                end=end,
            )
        )

    return matches


def validate_no_overlap(
    matches: list[MatchedReplacement],
) -> None:
    """确保所有 replacement 的 original ranges 不发生重叠。"""

    indexed = list(
        enumerate(matches)
    )

    ordered = sorted(
        indexed,
        key=lambda item: item[1].start,
    )

    for (
        previous_index,
        previous,
    ), (
        current_index,
        current,
    ) in zip(
        ordered,
        ordered[1:],
    ):

        if current.start < previous.end:
            raise OverlappingEditsError(
                first_index=previous_index,
                second_index=current_index,
            )


def apply_planned_replacements(
    original: str,
    matches: list[MatchedReplacement],
) -> str:
    """根据已经规划好的 original offsets 执行 replacements。

    从最高 offset 向最低 offset 应用，
    避免前面的 replacement 改变后续位置。
    """

    result = original

    for match in sorted(
        matches,
        key=lambda item: item.start,
        reverse=True,
    ):
        result = (
            result[:match.start]
            + match.new_text
            + result[match.end:]
        )

    return result


def apply_exact_edits(
    original: str,
    edits: list[ExactReplacement],
) -> str:
    """完整执行一组 exact edits。"""

    matches = plan_exact_replacements(
        original,
        edits,
    )

    validate_no_overlap(
        matches
    )

    return apply_planned_replacements(
        original,
        matches,
    )


def line_number_at_offset(
    text: str,
    offset: int,
) -> int:
    """返回字符 offset 所在的 1-based 行号。"""

    if offset < 0:
        raise InvalidArgumentError(
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