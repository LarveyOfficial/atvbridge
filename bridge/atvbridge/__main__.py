"""Entry point: python -m atvbridge"""

from __future__ import annotations

import asyncio
import logging
import os
import signal

from .appletv import AppleTV
from .gtv import GoogleTVServer
from .lgtv import LGTV
from .siri import SiriRelay
from .sirihost import SiriHost
from .store import Store
from .voice import VoiceAssistant
from .web import Web


async def main() -> None:
    logging.basicConfig(
        level=(os.environ.get("ATVB_LOG_LEVEL") or "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    store = Store()
    appletv = AppleTV(store)
    tv = LGTV(store.data_dir)
    tv.on_change = appletv.update_volume_source
    appletv.tv = tv
    gtv = GoogleTVServer(store, appletv)
    web = Web(store, appletv, gtv)
    siri_host = SiriHost(store.data_dir, store.display_name())
    siri_url = os.environ.get("ATVB_SIRI_URL") or (siri_host.url if siri_host.enabled else "")
    voice = VoiceAssistant(appletv, store.data_dir, SiriRelay(siri_url))
    gtv.on_voice_begin = voice.begin
    gtv.on_voice_chunk = voice.chunk
    gtv.on_voice = voice.handle
    voice.on_result = web.broadcast
    web.voice = voice
    web.siri_host = siri_host

    appletv.start()
    tv.start()
    siri_host.start()
    voice.preload()
    await gtv.start()
    await web.start(int(os.environ.get("ATVB_WEB_PORT") or 8090))

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    logging.getLogger(__name__).info("Shutting down")
    await web.stop()
    await gtv.stop()
    await appletv.stop()
    await tv.stop()
    await siri_host.stop()
    await voice.siri.close()


if __name__ == "__main__":
    asyncio.run(main())
