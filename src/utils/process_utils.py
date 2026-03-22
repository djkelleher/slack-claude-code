"""Shared subprocess lifecycle helpers."""

import asyncio
import signal

from loguru import logger


async def terminate_process_safely(
    process: asyncio.subprocess.Process,
    timeout: float = 5.0,
) -> None:
    """Interrupt a process safely, falling back to terminate and kill if needed."""
    if process.returncode is not None:
        return

    process.send_signal(signal.SIGINT)
    try:
        await asyncio.wait_for(process.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            process.kill()
            try:
                await asyncio.wait_for(process.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                logger.warning("Process did not respond to kill signal")
