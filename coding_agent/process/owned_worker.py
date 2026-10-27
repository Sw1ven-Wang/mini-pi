"""Fixed Windows ownership bridge. No user command runs before the stdin gate.

The host attaches this idle process to its kill-on-close Job, durably records
ownership, then sends JSON on stdin. Descendants therefore inherit ownership;
attaching an already-running shell would miss children spawned before attachment.
"""

import json
import subprocess
import sys


def main():
    payload = sys.stdin.buffer.read(1024 * 1024 + 1)
    if not payload:  # Parent died before establishing ownership.
        return 125
    if len(payload) > 1024 * 1024:
        return 125
    value = json.loads(payload)
    return subprocess.call([value["executable"], *value["args"], value["command"]],
                           stdin=subprocess.DEVNULL, shell=False)


if __name__ == "__main__":
    sys.exit(main())
