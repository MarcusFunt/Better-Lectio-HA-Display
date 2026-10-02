# Better Lectio Display

A self-hosted Lectio timetable for an 800 × 480 monochrome e-paper display. The current runtime sends data directly from the Lectio Gateway to the Display Service. It contains no Home Assistant service or integration; the repository name preserves its original project name.

```text
Lectio / manual MitID sign-in
           │
           ▼
Lectio Gateway ── private Compose API ──► Display Service
     │                                      │
     │ sign-in, diagnostics, preview         │ authenticated device API
     ▼                                      ▼
human browser                         XIAO ESP32-S3 firmware
                                      USB-provisioned settings
```

The gateway owns the Lectio session, parsing, normalization, and per-source cache. The display service requests schedule, assignments, homework, and cancellations over the private Compose network. It renders today and the next two days in Copenhagen time, with cancellations, assignments, and homework in that sidebar order. Private-calendar events are excluded.

## Setup

1. Copy `.env.example` to `.env`. Set `DISPLAY_BIND_ADDRESS` to the Compose host's private LAN address so the display can reach port 8001. Restrict that port with the host firewall to the intended local network. The gateway admin page remains loopback-bound on port 8000.
2. Build the optional browser image and start Compose:

   ```sh
   docker compose --profile auth-browser build
   docker compose up -d
   ```

   The gateway, display service, diagnostics sidecar, and auth lifecycle service start with Compose. The temporary Chromium browser starts when requested from the login page.
3. On the Compose host, open `http://localhost:8000/auth/browser`. Start the temporary browser and complete Lectio/MitID sign-in yourself. Enter your numeric Lectio student ID on the page if requested. The gateway retains the authenticated session and source cache in its data volume.
4. Check the four Lectio source states and the newest bitmap preview. The display service retries every 30 seconds; the gateway's source cache defaults to a five-minute TTL. Wait for fresh source timestamps before relying on the preview.
5. Build and flash the `lectio_s3` firmware to the XIAO ESP32-S3:

   ```sh
   make firmware-build
   make firmware-flash
   ```

6. Provision the flashed board using one of these paths:

   - **Browser:** Open `/auth/browser` in Chrome or Edge on `localhost` or an HTTPS origin. Connect the board over USB, enter its device-reachable display URL and Wi-Fi name/password, and choose **Connect to USB device and provision**. Web Serial sends the Wi-Fi settings directly to the board; the page registers its device credential and waits for authenticated service contact. Browser provisioning does not flash firmware.
   - **Host CLI:** Install the host provisioning dependency and run the module command. `--flash` builds and flashes the firmware before provisioning; the Wi-Fi password is prompted for without echo.

     ```sh
     python -m pip install -r tools/provision/requirements.txt
     python -m tools.provision.provision_device --port COMx --server-url "http://<host-lan-ip>:8001" --ssid "<wifi-name>" --name "Better Lectio display" --flash
     ```

     On macOS/Linux, replace `COMx` with the board's serial device. The host CLI is also available as `make provision-device ARGS="..."` when GNU Make is installed.

The display service uses `LECTIO_GATEWAY_URL=http://lectio-gateway:8000` inside Compose. This private connection requires no gateway token or external API port. The Compose project retains its original default name so existing gateway and display data volumes remain attached during upgrades.

## Device and failure behavior

The device uses its provisioned ID and bearer credential with `/device/v1/status`, `/device/v1/display`, `/device/v1/image/<hash>.bmp`, and `/device/v1/acknowledge`. It downloads only changed image hashes, validates the bitmap, and acknowledges the exact displayed revision. The screen shows when it was last acknowledged and the number of pending plan changes. A short button press requests acknowledgement; verify the button mapping on the actual board.

The display service retains per-source last-known-good data in memory, reports stale data in the image, and keeps the last persisted bitmap across failed refreshes and restarts. The device keeps its prior e-paper image during service or network failures. Device traffic uses HTTP on the private LAN; keep port 8001 off public interfaces.

## Development

```sh
make install-dev
make test
make lint
make compose-config
make compose-build
make firmware-build
```

`make test` runs the Python service and provisioning tests. `make lint` runs the configured Ruff checks. The firmware build uses PlatformIO. For provisioning from Python, install `tools/provision/requirements.txt` first. The built-in CLI and browser flow require a connected USB board for hardware verification.

See [ARCHITECTURE_AND_OPERATIONS.md](ARCHITECTURE_AND_OPERATIONS.md) for current service and deployment boundaries, and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the active completion path and dated evidence. The earlier `trmnl_schedule/` prototype and `legacy/` units remain historical code and do not define the current runtime.
