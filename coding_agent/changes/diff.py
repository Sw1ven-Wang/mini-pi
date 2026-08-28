import difflib


def unified_diff(
    old: str,
    new: str,
    path: str,
) -> str:

    return "".join(
        difflib.unified_diff(
            old.splitlines(True),
            new.splitlines(True),
            fromfile=path,
            tofile=path,
        )
    )