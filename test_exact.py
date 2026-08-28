from coding_agent.editing.exact import ExactReplacement, apply_exact_edits


def test_batch_exact_edit_success():
    original = (
        "def add(a, b):\n"
        "    return a - b\n"
        "\n"
        "print('old')\n"
    )

    result = apply_exact_edits(
        original,
        [
            ExactReplacement(
                old_text="return a - b",
                new_text="return a + b",
            ),
            ExactReplacement(
                old_text="print('old')",
                new_text="print('new')",
            ),
        ],
    )

    assert "return a + b" in result
    assert "print('new')" in result

def test_batch_uses_original_offsets():
    original = "AAA BBB CCC"

    result = apply_exact_edits(
        original,
        [
            ExactReplacement(
                old_text="AAA",
                new_text="AAAAAAAAAAAA",
            ),
            ExactReplacement(
                old_text="CCC",
                new_text="DDD",
            ),
        ],
    )

    assert result == (
        "AAAAAAAAAAAA BBB DDD"
    )