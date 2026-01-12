"""Low-level Claude Code process management with pexpect."""

import asyncio
import logging
import os
from pathlib import Path
from typing import Optional

import pexpect

from ..config import config
from .types import PTYSessionConfig

logger = logging.getLogger(__name__)


class ClaudeProcess:
    """Low-level pexpect wrapper for Claude Code process.

    Handles spawning, I/O, and lifecycle of the pexpect child process.
    """

    def __init__(self, session_config: PTYSessionConfig) -> None:
        self.session_config = session_config
        self.child: Optional[pexpect.spawn] = None

    async def spawn(self) -> bool:
        """Spawn the Claude Code process.

        Returns
        -------
        bool
            True if process spawned successfully.
        """
        cwd = Path(self.session_config.working_directory).expanduser()
        if not cwd.exists():
            cwd = Path.home()

        cmd = "claude"
        args = self.session_config.claude_args.copy() if self.session_config.claude_args else []

        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["FORCE_COLOR"] = "1"
        env["COLUMNS"] = str(self.session_config.cols)
        env["LINES"] = str(self.session_config.rows)

        self.child = pexpect.spawn(
            cmd,
            args=args,
            cwd=str(cwd),
            env=env,
            encoding="utf-8",
            timeout=self.session_config.startup_timeout,
            dimensions=(self.session_config.rows, self.session_config.cols),
        )

        return True

    async def wait_for_prompt(self) -> bool:
        """Wait for the initial Claude prompt to appear.

        Returns
        -------
        bool
            True if prompt was found, False otherwise.
        """
        loop = asyncio.get_running_loop()

        def read_until_prompt():
            patterns = [
                r">\s*(?:\x1b\[[0-9;]*[a-zA-Z])*\s*$",
                r">\s*$",
                r"\?\s*$",
                r"claude.*>\s*$",
            ]

            try:
                index = self.child.expect(patterns, timeout=self.session_config.startup_timeout)
                logger.info(f"Prompt matched pattern {index}")
                return True
            except pexpect.TIMEOUT:
                if self.child.before:
                    before_text = (
                        self.child.before
                        if isinstance(self.child.before, str)
                        else self.child.before.decode("utf-8", errors="replace")
                    )
                    logger.info(
                        f"No prompt match but got output ({len(before_text)} chars), assuming ready"
                    )
                    if "Claude" in before_text or ">" in before_text or "help" in before_text:
                        return True
                logger.warning("Startup timeout with no recognizable output")
                return False
            except pexpect.EOF:
                logger.error("EOF during startup - Claude process died")
                return False

        return await loop.run_in_executor(None, read_until_prompt)

    async def flush_startup_output(self) -> None:
        """Flush any remaining startup output to clear the buffer."""
        await asyncio.sleep(0.5)
        try:
            while True:
                remaining = self.child.read_nonblocking(size=4096, timeout=0.1)
                if remaining:
                    logger.info(f"Flushed {len(remaining)} chars of startup output")
        except pexpect.TIMEOUT:
            pass
        except pexpect.EOF:
            pass

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
        grace_period = config.timeouts.pty.stop_grace

        if self.child and self.child.isalive():
            self.child.sendline("/exit")
            await asyncio.sleep(grace_period)

            if self.child.isalive():
                self.child.sendcontrol("c")
                await asyncio.sleep(grace_period)

            if self.child.isalive():
                self.child.terminate(force=True)

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
            self.session_config.rows = rows
            self.session_config.cols = cols
