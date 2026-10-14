"""Entry point: python -m atvbridge"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .appletv import AppleTV
from .gtv import GoogleTVServer
from .store import Store
from .web import Web


async def main() -> None:
    logging.basicConfig(
        level=os.environ.get("ATVB_LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = Store()
    appletv = AppleTV(store)
    gtv = GoogleTVServer(store, appletv)
    web = Web(store, appletv, gtv)

    appletv.start()
    await gtv.start()
    await web.start(int(os.environ.get("ATVB_WEB_PORT", 8090)))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    logging.getLogger(__name__).info("Shutting down")
    await web.stop()
    await gtv.stop()
    await appletv.stop()


if __name__ == "__main__":
    asyncio.run(main())
