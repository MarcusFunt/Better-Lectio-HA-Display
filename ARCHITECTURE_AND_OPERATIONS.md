# Better Lectio Display — Architecture and Operations

**Status:** Current architecture; the earlier Home Assistant architecture was retired on 2026-09-27

**Updated:** 2026-10-03

**Repository:** `MarcusFunt/Better-Lectio-HA-Display`

## Goal and boundaries

Display Lectio lessons for today and the next two days in `Europe/Copenhagen`, plus cancellations, assignments, and homework in that priority order. The display is an 800 × 480, one-bit e-paper panel on a private LAN. Private-calendar events are outside the current product scope.

The current runtime is a direct Lectio Gateway → Display Service → device pipeline. There is no Home Assistant service, integration, HACS dependency, HA API, or HA credential in the active application or Compose stack. The repository name retains the original HA wording.

```text
Lectio / manual MitID sign-in
           │
           ▼
Lectio Gateway ── private Compose API ──► Display Service
      │                                      │
      │ login, diagnostics, preview           │ authenticated device API
      ▼                                      ▼
  human browser                         XIAO ESP32-S3 firmware
                                      USB-provisioned settings
```

The gateway owns authentication, Lectio requests, parsing, normalization, and source caching. The display service owns the three-day model, source fallback, change review, rendering, bitmap storage, device registry, and device API. Firmware owns Wi-Fi, image polling, panel refresh, and the short-press acknowledgement action.

## Gateway and authentication

The gateway keeps the pinned `python-lectio` dependency behind an internal adapter. Its normalized endpoints are `/api/v1/status`, `/api/v1/schedule`, `/api/v1/assignments`, `/api/v1/homework`, and `/api/v1/cancellations`. Source responses carry `items` and per-source `sync` metadata. Requests use timezone-aware start times and an exclusive end. The default source-cache TTL is 300 seconds.

The operator opens `/auth/browser`, starts a temporary Playwright Chromium session, and completes Lectio/MitID sign-in manually. The page also supports student-ID entry, source diagnostics, the latest bitmap preview, and USB provisioning. The auth lifecycle service starts and stops the temporary browser after capture, cancellation, or timeout. The system does not automate MitID.

The gateway admin port 8000 binds to loopback by default. The display service calls `http://lectio-gateway:8000` over the private Compose network using `LECTIO_GATEWAY_URL`; the internal connection needs no gateway API token. Do not publish the admin API publicly. Tailscale Serve and ACLs are optional host-side remote-access configuration; the repository does not install or configure Tailscale.

## Display model and freshness

The display service polls the gateway every 30 seconds and requests status plus all four source envelopes for Copenhagen midnight through the exclusive midnight after the third visible date. The gateway cache can make the effective upstream refresh cadence about five minutes. Reducing the display polling interval alone does not bypass that cache.

Lessons use their normalized subject, start, end, teacher, and room fields on a shared timetable grid. Assignment, homework, and cancellation records use their normalized fields in the sidebar. Completed assignments and homework for lessons already past are omitted. Each source has an independent in-memory last-known-good item set and last-success time. The renderer shows stale status while retaining usable stale data. If the schedule is stale/incomplete, all sources are unavailable, or rendering fails, the current bitmap is left intact. The persisted bitmap is reloaded after restart; in-memory per-source fallback is rebuilt on the next successful poll.

Change review persists hashed item identities and fingerprints, not lesson titles or descriptions. Only fresh source results advance their baselines. The image shows the last acknowledgement time and pending change count. Acknowledgement is authenticated and bound to the exact content hash displayed; firmware uses a short button press to request it. The configured button mapping is a hardware candidate and requires confirmation on the actual board.

## Rendering, images, and device API

The display service renders a deterministic 800 × 480 monochrome BMP, stores it by SHA-256 content hash, and publishes a current-revision pointer. Previous images are retained by the image store. The browser diagnostics page exposes a validated preview without exposing Lectio item contents or device secrets.

The authenticated device endpoints are:

- `GET /device/v1/status`
- `GET /device/v1/display`
- `GET /device/v1/image/<hash>.bmp`
- `POST /device/v1/acknowledge`

Requests identify the device and use its random bearer credential. The registry stores a one-way credential hash, supports create/list/rotate/revoke, and tracks device contact. Firmware validates the content-addressed image, downloads only when its hash changes, and records the new revision only after a successful panel refresh. Service or network failures leave the previous e-paper image visible.

## Firmware and provisioning

The PlatformIO target is `lectio_s3` for the XIAO ESP32-S3 800 × 480 e-paper kit. Build and flash with `make firmware-build` and `make firmware-flash`, or directly with `pio run -d firmware -e lectio_s3` and `pio run -d firmware -e lectio_s3 -t upload`. A successful build confirms compile/link; it does not confirm flash or panel behavior.

Two runtime provisioning paths are available:

1. **Browser Web Serial:** `/auth/browser` can provision a previously flashed board in Chrome or Edge from `localhost` or an HTTPS page. The browser sends Wi-Fi settings directly over USB and does not send or save them in the gateway. The gateway registers the device through an internal authenticated call to the display service and waits for its authenticated contact. This path does not flash firmware.
2. **Host CLI:** `python -m tools.provision.provision_device --port COMx --server-url http://<host-lan-ip>:8001 --ssid <wifi-name> --name "Better Lectio display" --flash`. The module prompts for the Wi-Fi password without echoing it. Install its host dependency with `python -m pip install -r tools/provision/requirements.txt`. `--flash` builds and uploads the generic firmware before provisioning.

Both paths send per-device configuration over USB and store it in device NVS. Device traffic uses HTTP and bearer credentials on the private LAN. Keep port 8001 restricted to the intended network and do not expose it to the public Internet until transport protection is addressed.

## Compose deployment and persistence

Compose defines five services: `lectio-gateway`, `lectio-auth-lifecycle`, the on-demand `lectio-auth-browser`, `display-service`, and `display-diagnostics`. The temporary browser uses the `auth-browser` profile image and is started by the lifecycle service only when requested. The diagnostics sidecar reads the display data volume read-only and supplies the private bitmap preview to the gateway.

The gateway admin port 8000 and browser view port 6080 bind to loopback by default. The device API host port 8001 also defaults to loopback; set `DISPLAY_BIND_ADDRESS` to the Compose host's private LAN address for a device on the LAN, then restrict the port with the host firewall. Keep the Compose project name at its default when upgrading an existing install so named data volumes remain attached.

The three persistent named volumes are:

- `lectio-gateway-data`: private Lectio session and gateway source cache.
- `display-service-data`: image revisions, current bitmap, device registry, and hashed review state.
- `display-provisioning-auth`: a shared internal token, writable by display-service and read-only to the gateway.

Do not commit Lectio sessions, Wi-Fi settings, device secrets, provisioning tokens, or the local `.env` file. Old HA-only volumes from earlier versions are not mounted or read by the current stack; remove them only as a deliberate cleanup after confirming rollback is unnecessary.

## Operations and evidence

Use `make test`, `make lint`, `make compose-config`, and `make compose-build` for software checks. Build firmware with `make firmware-build`; flash with `make firmware-flash`. For host provisioning, install `tools/provision/requirements.txt` and run the Python module command above. On Windows, use the built-in browser flow or the module command if GNU Make is unavailable.

The gateway health endpoint reports process readiness. `/auth/diagnostics` reports authentication and per-source states, and the login page shows the newest bitmap preview. A healthy container does not establish a current Lectio session, fresh records, a newly generated bitmap, successful device provisioning, or panel output. The latest dated runtime audit is in `IMPLEMENTATION_PLAN.md`.

Physical flash, USB, Wi-Fi, panel/controller, button mapping, and network-recovery checks must be performed on the actual board. The CI firmware target and fixture/API tests do not substitute for that acceptance. The historical Home Assistant design specs under `docs/superpowers/specs/` are explicitly superseded and are retained as background only.
