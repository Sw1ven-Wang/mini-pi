from pydantic import BaseModel
from pydantic import Field
from coding_agent.filesystem.versions import FileVersion


class ChangedRange(BaseModel):
    """一次修改在旧文件和新文件中的行范围。"""

    path: str

    old_start_line: int
    old_end_line: int

    new_start_line: int
    new_end_line: int


class MutationResult(BaseModel):
    path: str

    old_version: FileVersion | None = None
    new_version: FileVersion | None = None

    diff: str = ""

    changed_ranges: list[ChangedRange] = Field(
        default_factory=list
    )
    checkpoint_id: str | None = None
    changed: bool = True