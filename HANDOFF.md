# Apple TV to Google TV / Android Remote Bridge – Project Handoff

## 1. Project Objective & Vision
Build a bridge service (targeted for Docker on an Unraid server) that allows Android devices (Pixel, Samsung, etc.) to natively control an Apple TV 4K:
1. **Android's Native TV Remote**: Controlled directly via Android's Quick Settings tile and the Google TV remote app using the **Android TV Remote v2** protocol. No custom app required for remote control, D-pad navigation, or keyboard typing.
2. **Native Android Media Controls**: Display the active media card (artwork, track info, seek bar, transport controls) on the Android lock screen and Quick Settings via a lightweight Android companion app exposing an Android `MediaSession` connected to the bridge over WebSockets.

---

## 2. Key Architecture & Protocol Discoveries

### Apple TV Protocol Stack (`pyatv`)
- **Companion Link** (Port `49153`):
  - Handles directional navigation (D-pad), select, back/menu, home, power, and keyboard text injection.
  - Requires one-time SRP PIN pairing.
- **AirPlay / MRP** (Port `7000`):
  - Handles real-time playback state updates (Playing, Paused, Stopped), metadata (Title, Artist, Album), position, duration, and album artwork.
  - Requires one-time AirPlay PIN pairing.

### Android TV Remote v2 Protocol (Ports 6466 & 6467)
- **Local Network Discovery**:
  - Announced via mDNS: `_androidtvremote2._tcp.local.` on port `6466`.
  - TXT Record: `bt=<MAC_ADDRESS>`.
- **TLS Transport**:
  - Mutual TLS with self-signed client certificates. Python’s standard `ssl` library cannot accept arbitrary client-generated certs, requiring `pyOpenSSL` with memory BIOs.
  - Stream data is framed using varint-length-delimited Protobuf messages.
- **Pairing ("Polo" Protocol, Port 6467)**:
  - Phone prompts the user for a 6-character hex PIN.
  - Client verifies locally:
    $$\text{SHA-256}(N_{\text{client}} \parallel e_{\text{client}} \parallel N_{\text{server}} \parallel e_{\text{server}} \parallel \text{PIN}[2:6])[0] == \text{PIN}[0:2]$$
  - **The "000000" Easy-Pairing Hack**: Because the client validates this hash before sending, arbitrary fixed PINs are rejected. By keeping the server's RSA modulus $N$ constant and dynamically searching for a matching public exponent $e$ tailored to the phone's certificate, the bridge makes the PIN `000000` cryptographically valid for any phone on the network.
- **Remote Commands (Port 6466)**:
  - Uses `remotemessage.proto`.
  - Bridges key codes (`KEYCODE_DPAD_UP`, `KEYCODE_ENTER`, `KEYCODE_BACK`, etc.) directly into Apple TV commands.
  - Supports IME keyboard injection (`RemoteImeBatchEdit` -> `text_set`).

### Why an Android Companion App is Needed for Media Controls
- Google TV's native lock screen / Quick Settings media card is driven by **Google Cast** through Google Play Services.
- Cast requires factory-signed Google hardware certificates. Emulating this without leaked keys is impossible and legally/technically fragile.
- **Solution**: A lightweight companion Android app running a foreground `MediaSessionService` connected to the bridge’s WebSocket (`/ws`). This triggers Android’s native media system UI identically, with transport controls routed directly back to the Apple TV.

---

## 3. Current Live Hardware & Pairing Status

- **Development Host**: Googlebook (Linux VM inside Android Terminal at `<vm-ip>`).
- **Target Deployment**: Docker on Unraid (`network_mode: host` required for mDNS reflection).
- **Target Apple TV**:
  - **Name**: `Chomusuke`
  - **Model**: Apple TV 4K (3rd gen), tvOS 27.2
  - **IP Address**: `<apple-tv-ip>`
  - **MAC Address**: `<apple-tv-mac>`
  - **Identifier**: `<apple-tv-identifier>`
- **Pairing State**: **Successfully paired!**
  - Companion and AirPlay credentials have been obtained and saved in `bridge/data/config.json`.
  - Live metadata queries verified (successfully pulled active YouTube video status and power state).

---

## 4. Codebase Layout

Location: `/storage/emulated/10/Documents/Antigravity/AppleTV Remote/`

```text
AppleTV Remote/
├── HANDOFF.md                   # This summary document
├── bridge/
│   ├── atvbridge/
│   │   ├── __init__.py          # Package init
│   │   ├── __main__.py          # Application runner & signal handling
│   │   ├── appletv.py           # pyatv wrapper: connection lifecycle, push metadata, commands
│   │   ├── certs.py             # Server RSA key derivation & exponent search for '000000' PIN
│   │   ├── gtv.py               # Android TV Remote v2 server (Ports 6466 & 6467, mDNS, key maps)
│   │   ├── store.py             # Persistent JSON storage for paired phones and Apple TV config
│   │   ├── tls.py               # pyOpenSSL async TLS transport & varint protobuf parser
│   │   ├── web.py               # aiohttp web server: REST API, WebSocket (/ws), artwork endpoint
│   │   └── static/              # Web dashboard frontend assets
│   ├── data/
│   │   └── config.json          # Live configuration containing Chomusuke's credentials
│   └── .gitignore
└── scratch/
    └── ref/                     # Extracted reference sources from androidtvremote2
```

---

## 5. Next Steps / Roadmap

1. **Web Dashboard Frontend (`bridge/atvbridge/static/index.html`)**:
   - Build a responsive web UI displaying:
     - Real-time Apple TV connection status, power, and now-playing metadata.
     - Live pairing requests and PIN codes.
     - Paired phone list with ability to revoke keys.
     - Configuration interface for initial setup or switching Apple TVs.
2. **Dockerization for Unraid**:
   - Author a `Dockerfile` and `docker-compose.yml`.
   - Set up dependencies: `pyatv`, `pyOpenSSL`, `aiohttp`, `zeroconf`, `protobuf`, `cryptography`.
   - Note for Unraid template: Container **must** run in `host` network mode so mDNS and ports 6466/6467/8090 are directly reachable by Android phones.
3. **Android Companion App**:
   - Develop a minimal Android app (Kotlin / Jetpack) that runs a background `MediaSessionService`.
   - Connects to `ws://<bridge-ip>:8090/ws`.
   - Maps playback events to Android's `MediaSessionCompat` / `MediaSession` to generate the native notification / lock screen media card.
   - Forwards transport controls (play, pause, skip, seek) back over WebSocket.
