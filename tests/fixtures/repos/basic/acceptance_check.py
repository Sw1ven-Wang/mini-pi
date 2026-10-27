from pathlib import Path


def test_output_is_fixed():
    assert Path("input.txt").read_text(encoding="utf-8").strip() == "fixed"
