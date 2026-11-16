import asyncio
import os
import signal
import subprocess


async def terminate(process):
    if process.returncode is not None:
        return
    if os.name == "nt":
        killer = await asyncio.create_subprocess_exec(
            "taskkill",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await killer.wait()
    else:
        os.killpg(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 10)
    except TimeoutError:
        process.kill()
        await process.wait()


def group_options():
    return (
        {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        }
        if os.name == "nt"
        else {"start_new_session": True}
    )


async def run(argv, cwd, timeout=120, env=None):
    process = await asyncio.create_subprocess_exec(
        *map(str, argv),
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        **group_options(),
    )
    chunks = bytearray()
    truncated = False

    async def collect():
        nonlocal truncated
        while block := await process.stdout.read(65536):
            chunks.extend(block)
            if len(chunks) > 200000:
                truncated = True
                del chunks[:-200000]
        await process.wait()

    try:
        await asyncio.wait_for(collect(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        await terminate(process)
        raise
    return process.returncode, (
        "[OUTPUT TRUNCATED]\n" if truncated else ""
    ) + chunks.decode("utf-8", errors="replace")
