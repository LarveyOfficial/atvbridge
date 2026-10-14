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

Protocol notes live in the code: `bridge/atvbridge/certs.py` (the "000000" pairing trick),
`gtv.py` (Google TV remote server), `ime.py` (keyboard), `rti.py` (Apple TV typing).

---

## 1. Deploy on Unraid

One container does everything: the Google TV remote bridge, the dashboard (which also hands out
the Android app) and the Siri voice remote. GitHub Actions builds it as
`ghcr.io/larveyofficial/atvbridge:latest` (amd64 + arm64) on every push to `main`.

It **must use host networking**: phones find it over mDNS, the Apple TV finds the Siri remote
over HomeKit/mDNS, and both connect straight to its ports (8090, 6466, 6467, 47129).

### a. Start the container

Unraid web terminal (the `>_` icon top right):

```sh
docker run -d --name atvbridge \
  --network host \
  --restart unless-stopped \
  -v /mnt/user/appdata/atvbridge:/data \
  ghcr.io/larveyofficial/atvbridge:latest
docker logs -f atvbridge     # Ctrl+C to stop following
```

It shows up in Unraid's Docker tab; **update** later with `docker pull ghcr.io/larveyofficial/atvbridge:latest`
followed by `docker rm -f atvbridge` and the `docker run` above (or Unraid's "check for updates").
**Prefer compose?** With the Docker Compose Manager plugin: Docker tab → Compose → Add New Stack,
paste [`unraid.yml`](unraid.yml), Compose Up.

**Keep your existing pairings** (optional): before the first start, copy `bridge/data/config.json`
and `bridge/data/server_key.pem` from this project into `/mnt/user/appdata/atvbridge/`. Otherwise
pair the Apple TV in the dashboard's setup wizard.

### b. Check it

Open `http://<unraid-ip>:8090`: Apple TV status and what's playing. The logs should show
`Connected to Apple TV`, `Advertising '<name>' on <unraid-ip>` and the Siri remote starting.

### c. Siri for the mic button (once)

On an iPhone/iPad/Mac in the same Apple Home as the Apple TV: **Home app → + → Add Accessory →
More options… → Voice Remote**, setup code **031-45-154** (also on the dashboard). After that the
Google TV remote's mic button talks to real Siri on the Apple TV; until then (or if Siri is
unavailable) the bridge recognizes speech itself (see §2). The Siri remote is
[appletv-siri-voice](https://github.com/marcusadolfsson/appletv-siri-voice) (Apache-2.0, not
affiliated with Apple), bundled and supervised by the bridge; its pairing lives in `/data/siri`.

### Settings

`-e NAME=value` on `docker run`:

| Variable | Default | Use |
|---|---|---|
| `ATVB_HOST_IP` | auto | IP advertised over mDNS. Set it if the logs show the wrong IP (several interfaces/bridges). |
| `ATVB_WEB_PORT` | 8090 | Dashboard + companion app port. |
| `ATVB_SIRI` | auto | `0` turns the bundled Siri voice remote off. |
| `ATVB_SIRI_PINCODE` | 031-45-154 | HomeKit setup code of the Siri remote. |
| `ATVB_SIRI_URL` | bundled | Use a separately run appletv-siri-voice instead. |
| `ATVB_WHISPER_MODEL` | base.en | Local speech fallback (downloaded once to `/data/models`, ~140 MB). `small.en` is more accurate, slower. |
| `ATVB_ONLINE_ARTWORK` | 1 | `0` = no YouTube/App Store artwork lookups. |
| `ATVB_LG_HOST` | (unset) | LG webOS TV for real volume; see below. |
| `ATVB_LOG_LEVEL` | INFO | `DEBUG` logs key presses and protocol messages. |

**LG TV volume:** the Apple TV changes volume with HDMI-CEC presses and can't read the level, so
phones get volume up/down only. An LG webOS TV reports the real level of its *own speakers*, live.
**Not with a soundbar on HDMI ARC** (tested): the TV's counter only follows presses that go through
the TV, while the Siri Remote sends CEC volume straight to the soundbar, so it drifts. Leave it off
in that setup.

---

## 2. Pair your phone as a remote

1. Phone on the same Wi-Fi as Unraid.
2. Open the Google TV remote: Quick Settings → **Remote** tile (add it via the pencil icon
   if missing), or Google Home app → your devices → the TV remote button.
3. Pick **Chomusuke** from the list.
4. Enter **000000** as the code. (The dashboard also shows the code. If you turned off
   "Easy pairing" it'll be a random code.)

D-pad, select, back, home, play/pause, volume, mute, power and the keyboard (type into the Apple
TV's search fields) all work. Holding a button long-presses it (e.g. hold home = Control Center);
holding an arrow repeats it.

**Mic button:** with the Siri container set up (§1 b2), your voice goes to Siri on the Apple TV.
Otherwise the bridge recognizes speech itself (Whisper, on the server; no cloud) and:

| You say | It does |
|---|---|
| anything, while a text field is focused | types it in |
| "pause", "play", "next episode", "go home", "volume up", "mute", "go to sleep" | that command |
| "open Netflix", "launch Disney Plus", or just "Hulu" | opens the app |
| anything else ("Stranger Things") | opens Search and types it |

The dashboard shows what was heard and what it did.

## 3. Install the companion app (media card)

On the phone, open the dashboard (`http://<unraid-ip>:8090`) → **Get the Android app**, then open
the downloaded file (allow your browser to install apps when asked). Tagged releases also have
the APK attached on GitHub.

1. Open **ATV Bridge**. It searches the network and fills in the bridge address,
   or you can type it, e.g. `192.168.0.10:8090`.
2. Tap **Start media controls** and allow notifications.
3. Tap **Allow running in the background** so Android doesn't kill the connection.

While the Apple TV is playing or paused you get the media card with artwork, a seek bar,
play/pause, next/previous and ±10 s. The phone's volume buttons control the Apple TV
(you can turn that off). The app reconnects automatically and restarts after a reboot.

### Building the APK

CI builds it (`.github/workflows/build.yml`) and puts it into the Docker image. Locally:

```sh
cd android
./gradlew assembleRelease        # → app/build/outputs/apk/release/app-release.apk
```

**Signing:** CI signs with the keystore in the repo secrets `ANDROID_KEYSTORE_BASE64`,
`ANDROID_KEYSTORE_PASSWORD`, `ANDROID_KEY_ALIAS`, `ANDROID_KEY_PASSWORD`. Locally, release builds use
`~/.android/atvbridge-release.properties` (`storeFile`, `storePassword`, `keyAlias`,
`keyPassword`). **Back up that keystore and its password**: every update must be signed with it.

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
- **Artwork**: some apps (YouTube) don't publish artwork to the Apple TV. The bridge then looks up the
  YouTube thumbnail (only when title and channel match) or uses the app's App Store icon. This sends the
  title/channel to YouTube and the app id to Apple; set `ATVB_ONLINE_ARTWORK=0` to turn it off.
