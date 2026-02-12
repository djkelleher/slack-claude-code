"""Low-level Codex CLI process management with pexpect."""

import asyncio
import os
from pathlib import Path
from typing import Optional

import pexpect
from loguru import logger

from src.config import parse_model_effort

from .types import PTYSessionConfig


class CodexProcess:
    """Low-level pexpect wrapper for Codex CLI process.

    Handles spawning, I/O, and lifecycle of the pexpect child process.
    """

    def __init__(self, config: PTYSessionConfig) -> None:
        self.config = config
        self.child: Optional[pexpect.spawn] = None

    async def spawn(self) -> bool:
        """Spawn the Codex CLI process.

        Returns
        -------
        bool
            True if process spawned successfully.
        """
        cwd = Path(self.config.working_directory).expanduser()
        if not cwd.exists():
            cwd = Path.home()

        # Build command arguments for interactive mode with JSON output
        cmd = "codex"
        args = ["--json"]

        # Add sandbox mode
        if self.config.sandbox_mode:
            args.extend(["--sandbox", self.config.sandbox_mode])

        # Add approval mode
        if self.config.approval_mode:
            args.extend(["--ask-for-approval", self.config.approval_mode])

        # Add model if specified, parsing out effort suffix
        if self.config.model:
            base_model, effort = parse_model_effort(self.config.model)
            args.extend(["--model", base_model])
            if effort:
                args.extend(["-c", f'model_reasoning_effort="{effort}"'])

        # Add working directory
        args.extend(["--cd", str(cwd)])

        # Add any additional args
        if self.config.codex_args:
            args.extend(self.config.codex_args)

        # Set up environment
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["FORCE_COLOR"] = "1"
        env["COLUMNS"] = str(self.config.cols)
        env["LINES"] = str(self.config.rows)

        logger.info(f"Spawning Codex: {cmd} {' '.join(args)}")

        self.child = pexpect.spawn(
            cmd,
            args=args,
            cwd=str(cwd),
            env=env,
            encoding="utf-8",
            timeout=self.config.startup_timeout,
            dimensions=(self.config.rows, self.config.cols),
        )

        return True

    async def wait_for_ready(self) -> bool:
        """Wait for Codex to be ready to accept input.

        Returns
        -------
        bool
            True if ready, False on timeout/error.
        """
        loop = asyncio.get_running_loop()

        def read_until_ready():
            # Look for JSON output indicating session start or prompt ready
            patterns = [
                r'"type":\s*"session_start"',
                r'"session_id"',
                r'>\s*$',
                pexpect.TIMEOUT,
                pexpect.EOF,
            ]

            try:
                index = self.child.expect(patterns, timeout=self.config.startup_timeout)
                if index in (0, 1, 2):
                    logger.info(f"Codex ready (matched pattern {index})")
                    return True
                elif index == 3:
                    # Timeout - check if we got any output
                    if self.child.before:
                        logger.info("Codex timeout but got output, assuming ready")
                        return True
                    logger.warning("Codex startup timeout with no output")
                    return False
                else:
                    output = self.child.before or ""
                    logger.error(f"EOF during Codex startup. Output: {output!r}")
                    return False
            except pexpect.TIMEOUT:
                logger.warning("Codex startup timeout")
                return False
            except pexpect.EOF:
                output = self.child.before or ""
                logger.error(f"EOF during Codex startup - process died. Output: {output!r}")
                return False

        return await loop.run_in_executor(None, read_until_ready)

    async def flush_startup_output(self) -> str:
        """Flush any remaining startup output to clear the buffer.

        Returns
        -------
        str
            The flushed output.
        """
        await asyncio.sleep(0.3)
        output = ""
        try:
            while True:
                remaining = self.child.read_nonblocking(size=4096, timeout=0.1)
                if remaining:
                    output += remaining
                    logger.debug(f"Flushed {len(remaining)} chars of startup output")
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            pass
        return output

    def send(self, data: str) -> None:
        """Send data to the process.

        Parameters
        ----------
        data : str
            Data to send.
        """
        self.child.send(data)

    def sendline(self, data: str) -> None:
        """Send data followed by newline.

        Parameters
        ----------
        data : str
            Data to send.
        """
        self.child.sendline(data)

    def sendcontrol(self, char: str) -> None:
        """Send control character.

        Parameters
        ----------
        char : str
            Control character (e.g., 'c' for Ctrl+C).
        """
        self.child.sendcontrol(char)

    def read_nonblocking(self, size: int = 4096, timeout: float = 0.05) -> str:
        """Read available data from the PTY without blocking.

        Parameters
        ----------
        size : int
            Maximum bytes to read.
        timeout : float
            Timeout in seconds.

        Returns
        -------
        str
            Data read, or empty string if nothing available.

        Raises
        ------
        pexpect.EOF
            If process has terminated.
        """
        try:
            data = self.child.read_nonblocking(size=size, timeout=timeout)
            return data if data else ""
        except pexpect.TIMEOUT:
            return ""

    def is_alive(self) -> bool:
        """Check if process is still running."""
        return self.child is not None and self.child.isalive()

    @property
    def pid(self) -> Optional[int]:
        """Get the process ID."""
        if self.child is not None:
            return self.child.pid
        return None

    async def terminate(self) -> None:
        """Terminate the process gracefully, then forcefully if needed."""
        if not self.child or not self.child.isalive():
            return

        grace_period = self.config.stop_grace_period

        # Try sending /exit command
        try:
            self.child.sendline("/exit")
            await asyncio.sleep(grace_period)
        except Exception:
            pass

        # Try Ctrl+C
        if self.child.isalive():
            try:
                self.child.sendcontrol("c")
                await asyncio.sleep(grace_period)
            except Exception:
                pass

        # Force terminate
        if self.child.isalive():
            try:
                self.child.terminate(force=True)
            except Exception:
                pass

    def resize(self, rows: int, cols: int) -> None:
        """Resize the terminal window.

        Parameters
        ----------
        rows : int
            Number of rows.
        cols : int
            Number of columns.
        """
        if self.child:
            self.child.setwinsize(rows, cols)
            self.config.rows = rows
            self.config.cols = cols
