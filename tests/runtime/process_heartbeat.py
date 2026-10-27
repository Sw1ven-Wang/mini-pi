"""A subprocess fixture that makes orphan execution externally observable."""
import os
from pathlib import Path
import sys
import time

root = Path(sys.argv[1])
(root / "child-ready").write_text(str(os.getpid()))
while True:
    (root / "heartbeat").write_text(str(time.monotonic_ns()))
    time.sleep(0.03)
