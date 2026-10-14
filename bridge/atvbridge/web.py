"""Web dashboard + JSON/WebSocket API (also used by the companion app)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import WSMsgType, web

from . import appletv as atvmod
from .gtv import host_ip

if TYPE_CHECKING:
    from .appletv import AppleTV
    from .gtv import GoogleTVServer
    from .store import Store

LOG = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"

MEDIA_COMMANDS = {"play_pause", "play", "pause", "stop", "next", "previous",
                  "skip_forward", "skip_backward", "seek", "set_volume", "volume_up",
                  "volume_down", "power_on", "power_off", "power_toggle",
                  "up", "down", "left", "right", "select", "menu", "home", "launch"}


class Web:
    def __init__(self, store: Store, appletv: AppleTV, gtv: GoogleTVServer) -> None:
        self.store = store
        self.appletv = appletv
        self.gtv = gtv
        self.sockets: set[web.WebSocketResponse] = set()
        self.pairing: atvmod.PairingSession | None = None
        self.pending_creds: dict[str, Any] = {}
        self._runner: web.AppRunner | None = None
        self.app = web.Application()
        r = self.app.router
        r.add_get("/", self.index)
        r.add_get("/api/state", self.get_state)
        r.add_post("/api/scan", self.scan)
        r.add_post("/api/pair/begin", self.pair_begin)
        r.add_post("/api/pair/finish", self.pair_finish)
        r.add_post("/api/settings", self.settings)
        r.add_delete("/api/phones/{fp}", self.delete_phone)
        r.add_post("/api/command", self.command)
        r.add_get("/api/artwork", self.artwork)
        r.add_get("/ws", self.ws)
        appletv.on_change(lambda _s: self.broadcast())
        gtv.on_change = self.broadcast

    async def start(self, port: int) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "0.0.0.0", port).start()
        LOG.info("Dashboard on http://%s:%s", host_ip(), port)

    async def stop(self) -> None:
        for ws in list(self.sockets):
            await ws.close()
        if self._runner:
            await self._runner.cleanup()

    # --- state ----------------------------------------------------------------------
    def snapshot(self) -> dict[str, Any]:
        cfg = self.store.appletv or {}
        connected = {s.fp for s in self.gtv.sessions}
        return {
            "type": "state",
            "name": self.store.display_name(),
            "appletv": self.appletv.state,
            "config": {
                "configured": bool(cfg.get("credentials")),
                "address": cfg.get("address"),
                "name": cfg.get("name"),
                "model": cfg.get("model"),
                "easy_pairing": self.store.data.get("easy_pairing", True),
                "display_name": self.store.data.get("display_name"),
            },
            "pairing": self.gtv.active_pairing,
            "phones": [
                {"fingerprint": fp, "name": p["name"], "last_ip": p.get("last_ip"),
                 "last_seen": p.get("last_seen"), "easy": p.get("server_e", 65537) != 65537,
                 "connected": fp in connected}
                for fp, p in self.store.phones.items()
            ],
            "remotes_connected": len(self.gtv.sessions),
            # Lets clients interpolate now_playing.position without trusting their own clock.
            "server_time": time.time(),
        }

    def broadcast(self) -> None:
        if not self.sockets:
            return
        data = json.dumps(self.snapshot())
        for ws in list(self.sockets):
            asyncio.ensure_future(ws.send_str(data))

    # --- handlers -------------------------------------------------------------------
    async def index(self, request: web.Request) -> web.StreamResponse:
        return web.FileResponse(STATIC / "index.html")

    async def get_state(self, request: web.Request) -> web.Response:
        return web.json_response(self.snapshot())

    async def scan(self, request: web.Request) -> web.Response:
        body = await _json(request)
        try:
            return web.json_response({"devices": await atvmod.scan(body.get("address") or None)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)

    async def pair_begin(self, request: web.Request) -> web.Response:
        body = await _json(request)
        address, protocol = body["address"], body["protocol"]
        if protocol == "companion":
            self.pending_creds = {}
        if self.pairing:
            await self.pairing.cancel()
        try:
            self.pairing = atvmod.PairingSession(address, protocol)
            await self.pairing.begin()
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=500)
        return web.json_response({"ok": True})

    async def pair_finish(self, request: web.Request) -> web.Response:
        body = await _json(request)
        if not self.pairing:
            return web.json_response({"error": "No pairing in progress"}, status=400)
        p = self.pairing
        try:
            creds = await p.finish(str(body["pin"]))
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": str(exc)}, status=400)
        finally:
            self.pairing = None
        self.pending_creds[p.protocol] = creds
        if {"companion", "airplay"} <= self.pending_creds.keys():
            conf = p.conf
            mac = None
            if conf is not None:
                mac = getattr(conf.device_info, "mac", None)
            self.store.data["appletv"] = {
                "address": p.address,
                "identifier": conf.identifier if conf else None,
                "name": conf.name if conf else None,
                "model": str(conf.device_info.model_str) if conf else None,
                "mac": mac,
                "credentials": dict(self.pending_creds),
            }
            self.store.save()
            await self.appletv.restart()
            await self.gtv.advertise()
            self.broadcast()
            return web.json_response({"ok": True, "done": True})
        return web.json_response({"ok": True, "done": False})

    async def settings(self, request: web.Request) -> web.Response:
        body = await _json(request)
        rename = False
        if "easy_pairing" in body:
            self.store.data["easy_pairing"] = bool(body["easy_pairing"])
        if "display_name" in body:
            self.store.data["display_name"] = body["display_name"] or None
            rename = True
        self.store.save()
        if rename:
            await self.gtv.advertise()
        self.broadcast()
        return web.json_response({"ok": True})

    async def delete_phone(self, request: web.Request) -> web.Response:
        self.store.phones.pop(request.match_info["fp"], None)
        self.store.save()
        for s in list(self.gtv.sessions):
            if s.fp == request.match_info["fp"]:
                s.close()
        self.broadcast()
        return web.json_response({"ok": True})

    async def command(self, request: web.Request) -> web.Response:
        body = await _json(request)
        return web.json_response(await self._command(body))

    async def _command(self, body: dict[str, Any]) -> dict[str, Any]:
        cmd = body.pop("command", None)
        body.pop("type", None)
        if cmd not in MEDIA_COMMANDS:
            return {"error": f"Unsupported command {cmd}"}
        LOG.debug("Web command %s %s", cmd, body or "")
        try:
            await self.appletv.command(cmd, **body)
            return {"ok": True}
        except Exception as exc:  # noqa: BLE001
            LOG.warning("Command %s failed: %s", cmd, exc)
            return {"error": str(exc)}

    async def artwork(self, request: web.Request) -> web.Response:
        art = self.appletv.artwork
        if not art:
            raise web.HTTPNotFound()
        return web.Response(body=art[0], content_type=art[1] or "image/jpeg",
                            headers={"Cache-Control": "max-age=3600"})

    async def ws(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=20)
        await ws.prepare(request)
        self.sockets.add(ws)
        await ws.send_str(json.dumps(self.snapshot()))
        try:
            async for msg in ws:
                if msg.type != WSMsgType.TEXT:
                    continue
                try:
                    body = json.loads(msg.data)
                except ValueError:
                    continue
                if body.get("type") == "command":
                    result = await self._command(body)
                    await ws.send_str(json.dumps({"type": "result", **result}))
        finally:
            self.sockets.discard(ws)
        return ws


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        return await request.json()
    except Exception:  # noqa: BLE001
        return {}
