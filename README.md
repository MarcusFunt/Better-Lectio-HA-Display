# Better Lectio Display

A self-hosted, Lectio-only schedule for an 800 × 480 monochrome e-paper display. The repository name retains its earlier HA wording, but Home Assistant is no longer part of the running pipeline.

```text
Lectio / manual MitID sign-in
           │
           ▼
Lectio Gateway ── private Compose API ──► Display Service
     │                                      │
     │ diagnostics and bitmap preview       │ authenticated device API
     ▼                                      ▼
sign-in page                         USB-provisioned firmware
```

The gateway owns the Lectio session, normalization, and per-source cache. The display service requests the gateway's existing `/api/v1/status`, `/schedule`, `/assignments`, `/homework`, and `/cancellations` endpoints over the private Compose network. It renders today's Copenhagen schedule plus the next two days, prioritizes cancellations, assignments, and homework in the sidebar, and serves content-addressed bitmaps to registered devices. Private calendar events are not included.

## Setup

1. Copy `.env.example` to `.env`. Set `DISPLAY_BIND_ADDRESS` to the Compose host's private LAN address if a physical device must reach port 8001. Keep gateway admin port 8000 bound to loopback. Restrict LAN access to the device port with the host firewall.
2. Run `docker compose --profile auth-browser build` and `docker compose up -d`. The browser container is started on demand by the auth lifecycle service.
3. Open `http://localhost:8000/auth/browser` on the Compose host, or use Tailscale Serve with Tailnet ACLs for remote access. Start the temporary browser and complete Lectio/MitID sign-in yourself. Configure your numeric Lectio student ID if requested.
4. Check the Lectio source states and newest bitmap preview on that page. The display service retries the internal gateway every 30 seconds. An unavailable gateway does not erase the last rendered bitmap.
5. Build firmware with `make firmware-build`. Provision the board over USB with `make provision-device ARGS="--port COMx --server-url http://<host-lan-ip>:8001 --ssid <wifi-name> --name <device-name> --flash"`, adapting the port and address to the host. The provisioner prompts for the Wi-Fi password and writes the device credential over USB.

The display service uses `LECTIO_GATEWAY_URL=http://lectio-gateway:8000` internally. The Compose file supplies it; no gateway API token or external API port is needed for this connection. The Compose project retains its original default name to keep existing gateway and display data volumes attached after upgrade.

## Device and failure behavior

The device uses its USB-provisioned ID and bearer credential to call `/device/v1/status`, `/device/v1/display`, and `/device/v1/image/<hash>.bmp`. It downloads only changed image hashes and can acknowledge the exact revision displayed. A device or network failure leaves its prior e-paper image visible. The display service keeps per-source last-known-good records in memory, uses gateway sync freshness for change tracking, and retains the persisted bitmap across process restarts. The gateway persists its own source cache.

## Development

`make test` runs the repository and service tests. `make lint` runs Ruff. `make compose-config` validates Compose, and `make compose-build` builds all service images. The firmware build is `make firmware-build`.

See [ARCHITECTURE_AND_OPERATIONS.md](ARCHITECTURE_AND_OPERATIONS.md) for current decisions and [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for execution evidence. The earlier `trmnl_schedule/` prototype and `legacy/` units remain historical code and do not define the active pipeline.
