from coding_agent.filesystem.encoding import (
    decode_text_file,
    encode_text_file,
)


def test_decode_lf_utf8():
    data = b"hello\nworld\n"

    result = decode_text_file(data)

    assert result.content == "hello\nworld\n"
    assert result.newline == "LF"
    assert result.has_bom is False


def test_decode_crlf_normalizes_to_lf():
    data = b"hello\r\nworld\r\n"

    result = decode_text_file(data)

    assert result.content == "hello\nworld\n"
    assert result.newline == "CRLF"


def test_utf8_bom_is_preserved():
    data = b"\xef\xbb\xbfhello\n"

    decoded = decode_text_file(data)

    assert decoded.has_bom is True
    assert decoded.content == "hello\n"

    encoded = encode_text_file(
        decoded.content,
        newline=decoded.newline,
        has_bom=decoded.has_bom,
    )

    assert encoded == data


def test_crlf_round_trip():
    data = b"hello\r\nworld\r\n"

    decoded = decode_text_file(data)

    encoded = encode_text_file(
        decoded.content,
        newline=decoded.newline,
        has_bom=decoded.has_bom,
    )

    assert encoded == data