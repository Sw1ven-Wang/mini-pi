import asyncio
import os
from pathlib import Path
import sys

from coding_agent.process.runner import ProcessRunner


async def main():
    root = Path(sys.argv[1])
    script = Path(__file__).with_name("process_heartbeat.py")
    quote = lambda value: "'" + str(value).replace("'", "''") + "'"
    command = "& " + " ".join(quote(value) for value in (sys.executable, script, root))
    task = asyncio.create_task(ProcessRunner(parent_lifetime_guard=True).run(command, cwd=root, timeout_seconds=15))
    for _ in range(500):
        if task.done():
            raise AssertionError(await task)
        if (root / "heartbeat").exists():
            os._exit(86)
        await asyncio.sleep(0.01)
    raise AssertionError("child did not start")


if __name__ == "__main__":
    asyncio.run(main())
