"""Runs the bundled appletv-siri-voice bridge (Node.js) as a supervised child process.

The Docker image ships it in /opt/siri (https://github.com/marcusadolfsson/appletv-siri-voice,
Apache-2.0). The Apple TV knows it as a HomeKit "Voice Remote"; siri.py streams the phone's
mic to its control API, which only listens on localhost.

ATVB_SIRI=auto (default: on when /opt/siri/index.js and node exist) | 1 | 0
ATVB_SIRI_PINCODE  HomeKit setup code (default 031-45-154)
ATVB_SIRI_PORT     control API port (default 8477)
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
from pathlib import Path

LOG = logging.getLogger("atvbridge.siri")

DEFAULT_PINCODE = "031-45-154"


class SiriHost:
    def __init__(self, data_dir: Path, name: str) -> None:
        self.dir = Path(os.environ.get("ATVB_SIRI_DIR", "/opt/siri"))
        self.storage = data_dir / "siri"
        self.name = f"{name} Voice Remote"
        self.pincode = os.environ.get("ATVB_SIRI_PINCODE", DEFAULT_PINCODE)
        self.port = int(os.environ.get("ATVB_SIRI_PORT", 8477))
        mode = os.environ.get("ATVB_SIRI", "auto").lower()
        bundled = (self.dir / "index.js").exists() and shutil.which("node") is not None
        self.enabled = bundled if mode == "auto" else mode in ("1", "true", "yes", "on") and bundled
        self.running = False
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        if self.enabled and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def _run(self) -> None:
        self.storage.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "HAP_STORAGE": str(self.storage),
            "HAP_NAME": self.name,
            "HAP_PINCODE": self.pincode,
            "CTRL_BIND": "127.0.0.1",
            "CTRL_PORT": str(self.port),
        }
        delay = 2
        while True:
            try:
                self._proc = await asyncio.create_subprocess_exec(
                    "node", "index.js", cwd=self.dir, env=env,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
                )
                self.running = True
                LOG.info("Started Siri voice remote (HomeKit setup code %s)", self.pincode)
                assert self._proc.stdout is not None
                async for line in self._proc.stdout:
                    text = line.decode(errors="replace").strip()
                    if text:
                        LOG.info("%s", text)
                code = await self._proc.wait()
                LOG.warning("Siri voice remote exited with code %s; restarting in %ss", code, delay)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                LOG.warning("Could not start Siri voice remote: %s", exc)
            finally:
                self.running = False
            await asyncio.sleep(delay)
            delay = min(delay * 2, 60)

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        proc = self._proc
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
