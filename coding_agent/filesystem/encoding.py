"""Text encoding and newline utilities."""

from __future__ import annotations

from dataclasses import dataclass


UTF8_BOM = b"\xef\xbb\xbf"


@dataclass(frozen=True)
class TextFileData:
    content: str
    encoding: str
    newline: str
    has_bom: bool


def decode_text_file(data: bytes) -> TextFileData:
    """把磁盘原始 bytes 解码成内部统一文本。"""

    has_bom = data.startswith(UTF8_BOM)

    if has_bom:
        raw = data[len(UTF8_BOM):]
    else:
        raw = data

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError(
            "当前只支持 UTF-8 文本文件"
        ) from exc

    # 第一版直接拒绝 mixed newline。
    has_crlf = "\r\n" in text

    without_crlf = text.replace("\r\n", "")
    has_lf = "\n" in without_crlf

    if has_crlf and has_lf:
        raise ValueError(
            "暂不支持 mixed newline 文件"
        )

    newline = "CRLF" if has_crlf else "LF"

    # Agent 内部统一使用 LF。
    normalized = text.replace("\r\n", "\n")

    return TextFileData(
        content=normalized,
        encoding="utf-8",
        newline=newline,
        has_bom=has_bom,
    )
def encode_text_file(
    content: str,
    *,
    newline: str,
    has_bom: bool,
) -> bytes:
    """把内部 LF 文本恢复成磁盘 bytes。"""

    # 内部约定 content 使用 LF。
    if newline == "LF":
        text = content

    elif newline == "CRLF":
        text = content.replace("\n", "\r\n")

    else:
        raise ValueError(
            f"不支持的 newline: {newline}"
        )

    data = text.encode("utf-8")

    if has_bom:
        data = UTF8_BOM + data

    return data