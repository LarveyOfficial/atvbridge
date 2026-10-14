# Apple TV ↔ Android Remote Bridge

Control an Apple TV from Android's **built-in Google TV remote** (Quick Settings
tile / Google Home app), and get the Apple TV's **now-playing card** on the
phone's lock screen and Quick Settings.

```
 Android phone                         Bridge (Docker, host network)            Apple TV
┌──────────────────────┐  TLS 6466/6467 ┌─────────────────────────────┐  pyatv  ┌──────────┐
│ Google TV remote     │ ─────────────► │ gtv.py   (fake Google TV)    │ ──────► │Companion │
│ (QS tile, Home app)  │  mDNS discover │                              │         │ 49153    │
├──────────────────────┤                │ appletv.py (pyatv wrapper)   │ ◄────── │AirPlay   │
│ ATV Bridge app       │ ◄── WS /ws ─── │ web.py   (dashboard + API)   │  push   │ 7000     │
│ (MediaSession card)  │ ── commands ─► │                              │         └──────────┘
└──────────────────────┘     8090       └─────────────────────────────┘
```

| Folder     | What it is |
|------------|------------|
| `bridge/`  | Python service (pyatv + Android TV Remote v2 server + web dashboard). Runs in Docker. |
| `android/` | Companion app: shows the media card, routes play/pause/seek/volume back to the bridge. |

See `HANDOFF.md` for protocol details (including the "000000" pairing trick).

---

## 1. Deploy the bridge on Unraid

The container **must use host networking**: phones find it over mDNS and connect
straight to ports 6466/6467/8090.

### a. Copy the files

From any computer on your network, copy the `bridge/` folder to the Unraid share
`appdata` so it ends up at `/mnt/user/appdata/atvbridge/src/`.

Then copy your existing pairing data so you don't have to pair with the Apple TV again:

```
bridge/data/config.json    →  /mnt/user/appdata/atvbridge/data/config.json
bridge/data/server_key.pem →  /mnt/user/appdata/atvbridge/data/server_key.pem
```

(Skip this to start fresh. The dashboard has a setup wizard for pairing the Apple TV.)

### b. Build and start the container

Open the Unraid web terminal (the `>_` icon in the top right) and run:

```sh
cd /mnt/user/appdata/atvbridge/src
docker build -t atvbridge .
docker run -d --name atvbridge \
  --network host \
  --restart unless-stopped \
  -v /mnt/user/appdata/atvbridge/data:/data \
  atvbridge
docker logs -f atvbridge     # Ctrl+C to stop following
```

You should see `Connected to Apple TV Chomusuke` and `Advertising 'Chomusuke' on <unraid-ip>`.
It then shows up in Unraid's Docker tab like any other container.

*Using the Compose Manager plugin instead?* Create a stack and paste
`bridge/docker-compose.yml`, changing the volume to `/mnt/user/appdata/atvbridge/data:/data`.

**Updating later:** copy the new `bridge/` files over `src/`, then
`docker rm -f atvbridge`, then run the `docker build` and `docker run` commands above again.

### c. Check it

Open `http://<unraid-ip>:8090`. You should see the Apple TV status and what's playing.

**Environment variables** (add `-e NAME=value` to `docker run`):

| Variable | Default | Use |
|---|---|---|
| `ATVB_HOST_IP` | auto | IP advertised over mDNS. Set this if Unraid has several interfaces/bridges and the logs show the wrong IP. |
| `ATVB_WEB_PORT` | 8090 | Dashboard + companion WebSocket port. |
| `ATVB_LOG_LEVEL` | INFO | `DEBUG` logs every key press and pairing message. |

---

## 2. Pair your phone as a remote

1. Phone on the same Wi-Fi as Unraid.
2. Open the Google TV remote: Quick Settings → **Remote** tile (add it via the pencil icon
   if missing), or Google Home app → your devices → the TV remote button.
3. Pick **Chomusuke** from the list.
4. Enter **000000** as the code. (The dashboard also shows the code. If you turned off
   "Easy pairing" it'll be a random code.)

D-pad, select, back, home, play/pause, volume, power and the keyboard (type into the Apple TV's
search fields) all work. Long-press on select/home works too (e.g. long-press home = Control Center).

## 3. Install the companion app (media card)

The APK is built from `android/` (see below). Copy `ATVBridge.apk` to the phone and open it
(allow "install unknown apps" for your file manager when asked).

1. Open **ATV Bridge**. It searches the network and fills in the bridge address,
   or you can type it, e.g. `192.168.0.10:8090`.
2. Tap **Start media controls** and allow notifications.
3. Tap **Allow running in the background** so Android doesn't kill the connection.

While the Apple TV is playing or paused you get the media card with artwork, a seek bar,
play/pause, next/previous and ±10 s. The phone's volume buttons control the Apple TV
(you can turn that off). The app reconnects automatically and restarts after a reboot.

### Building the APK

```sh
cd android
./gradlew assembleRelease        # → app/build/outputs/apk/release/app-release.apk
```

Needs JDK 17+ and the Android SDK (`local.properties` → `sdk.dir=...`).
On this ARM Googlebook VM, Google's x86-only `aapt2` runs through `box64`
(installed with apt; `~/.box64rc` keeps box64's messages off aapt2's stdout).

---

## Development

```sh
cd bridge
python3 -m venv ~/.cache/atvbridge-venv
~/.cache/atvbridge-venv/bin/pip install -r requirements.txt
~/.cache/atvbridge-venv/bin/python -m atvbridge          # uses ./data
~/.cache/atvbridge-venv/bin/python -m unittest -v        # end-to-end protocol tests
```

The tests drive the bridge with `androidtvremote2`, the same client library Home Assistant
uses for real Google TVs, against a fake Apple TV: pairing with 000000, random-code pairing,
key mapping, text input, app links, state pushes, reconnect after restart, revocation.

Note: the Android Terminal VM is NAT'ed (10.80.90.x), so phones can't reach a bridge running
*here*. Test with real phones on the Unraid deployment.

## Troubleshooting

- **Phone doesn't list the Apple TV**: check the logs for `Advertising ... on <ip>`. That IP must be
  Unraid's LAN IP (set `ATVB_HOST_IP` otherwise). The container must use `--network host`.
  Some routers block multicast between Wi-Fi and wired; the phone and server must be on the same subnet/VLAN.
- **Pairing says the code is wrong**: cancel and try again. The first attempt from a new phone
  can fall back to a random code (shown on the dashboard) if the phone skipped the remote port.
- **"Apple TV not found"**: the Apple TV's IP changed. Give it a DHCP reservation, or re-run setup
  from the dashboard ("Change Apple TV").
- **No artwork**: some apps (YouTube) don't publish artwork to the Apple TV's now-playing info.
