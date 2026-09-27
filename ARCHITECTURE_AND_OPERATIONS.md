# Better Lectio Display — Architecture and Operations

**Status:** Current architectural baseline, superseding the Home Assistant design of 2026-09-25
**Updated:** 2026-09-27
**Repository:** `MarcusFunt/Better-Lectio-HA-Display`

## Goal and boundaries

Show normalized Lectio lessons for today, tomorrow, and the following day in Copenhagen local time. Show cancellations, assignments, and homework in a narrow sidebar with that category priority. The output is an 800 × 480 one-bit BMP for a permanently powered, LAN-connected display. Private-calendar events are intentionally excluded.

The gateway owns Lectio authentication, scraping, parsing, normalization, and caching. The display service owns the three-day model, per-source fallback, change review, rendering, bitmap storage, and authenticated device API. Firmware owns Wi-Fi connectivity, image fetching, and panel updates. The display service never logs in to Lectio or handles MitID.

```text
Lectio / manual MitID
        │
        ▼
Lectio Gateway  ── GET /api/v1/* on private Compose network ──► Display Service
      │                                                       │
      │ login and preview                                     │ content-addressed BMP
      ▼                                                       ▼
  human browser                                       USB-provisioned firmware
```

The gateway and display service remain separate Compose services. The gateway already waits for the display diagnostics sidecar, which waits for the display service. The display service therefore has no Compose `depends_on` edge to the gateway and retries it on its existing 30-second refresh loop.

## Gateway and authentication

The gateway keeps the `python-lectio` lineage behind its internal adapter so other application layers consume normalized records rather than raw Lectio responses. Its unchanged source endpoints are `/api/v1/status`, `/api/v1/schedule`, `/api/v1/assignments`, `/api/v1/homework`, and `/api/v1/cancellations`. Source requests use aware `start` and exclusive `end` datetimes; source responses contain `items` plus per-source `sync` metadata. The gateway persists the authenticated session and its last-known-good source cache.

The user starts a temporary Playwright Chromium browser from `/auth/browser` and completes Lectio/MitID login manually. The auth browser is isolated and stopped after session capture. The page also offers student-ID setup, source diagnostics, and a preview of the newest rendered bitmap. It stores no display-to-gateway API credential. MitID automation is outside the design.

The gateway admin API stays loopback-bound on host port 8000. The display service calls `http://lectio-gateway:8000` inside Compose, configured by `LECTIO_GATEWAY_URL`. This internal request is deliberately credential-free. Do not publish that API on a public interface; remote human access should use Tailscale Serve and Tailnet ACLs.

## Display model and freshness

At each refresh, the display client requests gateway status and the four source envelopes for Copenhagen midnight through midnight after the third visible day. It validates response shapes and timestamps before accepting items. The status request is diagnostic; each source envelope carries its own freshness. Gateway HTTP and connection failures are mapped to safe error codes without logging Lectio response content.

The service maps lesson `subject`, `start`, `end`, `teacher`, and `room` directly into a chronological timeline. Assignment `title` and `due`, homework `subject`, `description`, and `target_lesson_start`, and cancellation `subject`, `reason`, and `start` form the sidebar. Completed assignments and homework for lessons already in the past are omitted. The renderer has no private-calendar input.

Each source has an independent in-memory last-known-good item set and last-successful-sync timestamp. A failed source keeps its prior items, marks that source stale, and does not create false deletion notices. A stale or expired schedule can omit an entire ISO week, so it never replaces a complete bitmap. Other stale sources can render cached items with a stale indicator. The persistent change review stores hashes of item identities and fingerprints rather than titles or descriptions. Only fresh sources advance its baseline; device acknowledgement is tied to the exact content hash shown. On migration, retired HA entity keys are pruned from the review state.

The rendered image is stored as a content-addressed bitmap in the display data volume. On a first schedule failure, a complete gateway outage, or a render error, the service keeps its previous image and retries on the next loop. A restart reloads the persisted bitmap even though per-source in-memory fallback starts empty. This protects the physical display from being blanked by an upstream failure.

## Device API and firmware

The display service exposes authenticated `/device/v1/status`, `/device/v1/display`, `/device/v1/image/<hash>.bmp`, and `/device/v1/acknowledge` endpoints. Device IDs and cryptographically random bearer credentials are registered in the display data volume. The USB provisioning tool optionally flashes generic firmware, registers the device, writes Wi-Fi settings and credentials over serial, reboots, and checks authenticated contact. Secrets are never compiled into firmware or authenticated by MAC address alone.

The device checks the content hash, downloads only a changed BMP, validates it, refreshes the panel, and can acknowledge the revision it displayed. It retains its previous e-paper image through local network and service failures. Physical panel wiring, button mapping, USB provisioning, MitID, and live Lectio behavior require separate hardware or external checks; software tests and image builds alone do not verify them.

## Compose deployment and persistence

The active Compose services are `lectio-gateway`, `lectio-auth-lifecycle`, on-demand `lectio-auth-browser`, `display-service`, and `display-diagnostics`. The diagnostics sidecar reads the display bitmap volume and provides the preview to the gateway. Its port is private to Compose. The gateway's host port 8000 and browser-view port 6080 bind to loopback by default. The device API host port 8001 also defaults to loopback; set `DISPLAY_BIND_ADDRESS` to the host's private LAN address for a physical device and restrict it with the host firewall.

The two persistent volumes are `lectio-gateway-data` and `display-service-data`. The default Compose project name retains its historical spelling so deployed volumes remain attached during this migration. Retired HA token/config volumes are no longer mounted. Old HA settings and tokens are not read by the new services. Do not commit Lectio sessions, Wi-Fi settings, device credentials, or any other secret.

Remote human/admin access goes through Tailscale rather than public router forwarding, a public reverse proxy, or Funnel. The display itself needs only LAN access to the device API and does not need Tailscale.

## Operations and evidence

Use `make test`, `make lint`, `make compose-config`, and `make compose-build` for software validation. Firmware uses `make firmware-build`; `make provision-device` invokes the Python provisioning module and forwards `ARGS`. The gateway health endpoint checks process readiness; source sync state and bitmap preview provide more useful operational evidence. A healthy service does not imply a current Lectio session, fresh source records, a visible panel image, or successful USB provisioning.

`IMPLEMENTATION_PLAN.md` is the canonical dated execution log and preserves the old HA implementation records as history. The two earlier HA design specs under `docs/superpowers/specs/` are superseded. Future changes to this architecture should update this document explicitly.
