# Better Lectio HA Display

A self-hosted, Home Assistant-centered schedule display for an 800×480 monochrome e-paper device.

The project is being redesigned around a layered architecture:

1. a **Lectio Gateway** that owns Lectio access and MitID-backed session handling;
2. **Home Assistant** as the mandatory calendar/task middle layer;
3. a **Display Service** that consumes Home Assistant data and renders the final bitmap;
4. **custom display firmware** that is built with the repository and provisioned over USB.

The physical display is local-only, permanently powered, and intended to stay connected to Wi-Fi. Remote human/admin access is provided through the user's Tailscale Tailnet rather than by exposing management services publicly.

> **Migration status:** the repository still contains code from the earlier TRMNL BYOS/Pi Zero prototype. That implementation is being replaced. Treat `ARCHITECTURE_AND_OPERATIONS.md` and `IMPLEMENTATION_PLAN.md` as the authoritative design and migration plan.

## Documentation

Before changing the repository, read:

- [AGENTS.md](AGENTS.md) — mandatory workflow rules for agents
- [ARCHITECTURE_AND_OPERATIONS.md](ARCHITECTURE_AND_OPERATIONS.md) — architectural and operational source of truth
- [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) — ordered implementation roadmap plus the canonical execution/evidence log

The implementation plan is the only Markdown document used for routine progress notes, findings, task outcomes, and next-step tracking.

## Intended architecture

```text
Lectio / MitID
      │
      ▼
┌─────────────────────────────┐
│ 1. Lectio Gateway           │
│                             │
│ - Playwright login          │
│ - Lectio session storage    │
│ - python-lectio adapter     │
│ - schedule                  │
│ - assignments               │
│ - homework                  │
│ - cancellations             │
└──────────────┬──────────────┘
               │ local API
               ▼
┌─────────────────────────────┐
│ 2. Home Assistant           │
│                             │
│ - calendar.lectio           │
│ - calendar.private          │
│ - todo.lectio_assignments   │
│ - todo.lectio_homework      │
│ - sensor.lectio_*           │
└──────────────┬──────────────┘
               │ HA API
               ▼
┌─────────────────────────────┐
│ 3. Display Service          │
│                             │
│ - normalize HA data         │
│ - prioritize sidebar        │
│ - render 800×480 1-bit BMP  │
│ - serve local device API    │
└──────────────┬──────────────┘
               │ LAN
               ▼
┌─────────────────────────────┐
│ 3.5 Custom Firmware         │
│                             │
│ - USB provisioned           │
│ - per-device credential     │
│ - persistent Wi-Fi          │
│ - authenticated image fetch │
└─────────────────────────────┘
```

## Lectio and MitID

Lectio access is isolated in the Lectio Gateway.

The project uses the `python-lectio` code lineage behind an internal adapter for schedule, homework, assignments, cancellations, and related Lectio data. Application code outside that adapter should not depend directly on `lectio.sdk` or raw Lectio HTML/data structures.

MitID authentication is deliberately separate from normal Lectio data access.

The planned authentication rollout is:

1. **Phase 1 — Playwright Chromium**
   - start a temporary Chromium session;
   - the user performs the real Lectio/MitID login manually;
   - capture the resulting authenticated Lectio cookies/session;
   - validate them through the Lectio adapter;
   - persist the session and terminate Chromium.

2. **Phase 2 — inspect the authenticated browser/session flow**
   - document redirects, cookies, expiry behavior, and the minimum session state required;
   - add regression coverage around browser-session import.

3. **Phase 3 — streamlined login if feasible**
   - replace normal remote-browser use with a clean `/auth/login` browser flow;
   - keep Playwright browser login as the recovery fallback.

MitID itself is not automated.

## Home Assistant

Home Assistant is the required application-level middle layer.

Lectio data is exposed through a custom HA integration approximately as:

```text
calendar.lectio
todo.lectio_assignments
todo.lectio_homework
sensor.lectio_cancellations
sensor.lectio_session_status
sensor.lectio_last_sync
```

Private events are created through the normal Home Assistant calendar UI, for example in:

```text
calendar.private
```

The Display Service reads Home Assistant rather than bypassing it with direct Lectio or ICS access.

Assignments and homework are initially read-only HA todo entities. Marking an HA item complete must not silently modify or submit anything in Lectio.

## Display layout

The display is a single chronological schedule covering:

- today;
- tomorrow;
- the day after tomorrow.

Lectio lessons and private Home Assistant events are merged onto the same timeline.

Lectio lesson blocks should show:

- time;
- subject;
- teacher;
- room.

A narrow right-hand sidebar is reserved for attention items with this fixed category priority:

```text
cancellations
    ↓
assignments
    ↓
homework
```

Within each category, nearer and more urgent items rank first.

The rendering target remains:

```text
800 × 480
1-bit monochrome
BMP
```

Content-addressed image versions are preferred so the device can avoid unnecessary e-paper refreshes.

## Display firmware

The final system does **not** use stock TRMNL firmware.

The upstream/open firmware may be used as a hardware-driver reference or starting fork, but the repository will own the firmware behavior.

The display is:

- LAN-only;
- always plugged in;
- not battery-optimized;
- expected to remain connected to Wi-Fi;
- free to check for new content frequently.

The normal firmware loop is intentionally small:

```text
connect Wi-Fi
     ↓
authenticate to local display service
     ↓
check content hash
     ↓
download only when changed
     ↓
validate bitmap
     ↓
refresh e-paper
```

A temporary network/server failure must leave the last valid image on the display.

## USB provisioning

Firmware compilation and device provisioning are separate operations.

The firmware binary must contain no per-device secret.

A USB provisioning tool will:

1. connect to the device;
2. optionally flash the current generic firmware;
3. generate a device ID;
4. generate a cryptographically secure per-device credential;
5. register that device with the Display Service;
6. write Wi-Fi configuration and the device credential over USB;
7. reboot;
8. verify authenticated communication.

The intended command surface is roughly:

```sh
make firmware
make flash PORT=/dev/ttyACM0
make provision PORT=/dev/ttyACM0
```

These commands describe the target workflow; they should not be assumed to exist until their implementation milestone is complete.

## Deployment and networking

The target deployment is **Docker Compose on a desktop/server host**, not a Raspberry Pi Zero/systemd deployment.

Logical services are expected to include:

```text
lectio-gateway
lectio-auth-browser
lectio-auth-view
display-service
```

The repository integrates with an existing Home Assistant instance by default; it does not need to own the HA deployment itself.

### LAN display traffic

The physical e-paper display communicates directly with the Display Service over the local network using its USB-provisioned per-device credential.

The device does not need Tailscale.

### Remote human/admin access

Management, diagnostics, and Lectio reauthentication should be reachable through the user's Tailscale Tailnet, preferably using Tailscale Serve and Tailnet ACLs.

Do not require:

- public router port forwarding;
- a public reverse proxy;
- Tailscale Funnel;
- public exposure of the MitID/Lectio login UI.

## Failure behavior

The new architecture is designed to degrade per source rather than fail all-or-nothing.

Examples:

- Lectio fails temporarily → keep last-known-good Lectio data;
- assignments fail → calendar rendering can still continue;
- Home Assistant private events change → they can still update independently;
- a render fails → continue serving the previous valid bitmap;
- Lectio authentication expires → retain stale Lectio-derived data and report that reauthentication is required.

Each service should expose enough health/status information to identify which layer is stale or failing.

## Security rules

- MitID login remains manual.
- Lectio browser cookies/session state are secrets.
- Home Assistant tokens, Lectio sessions, Wi-Fi credentials, and device secrets must never be committed.
- Logs must redact sensitive values.
- Device credentials are generated with a cryptographically secure RNG.
- Per-device credentials are provisioned over USB, not baked into firmware.
- Device authentication must not rely on MAC address alone.
- Remote administration uses Tailscale rather than public exposure.

## Current implementation status

The repository is in an architectural migration.

The existing `trmnl_schedule/`, `systemd/`, legacy `.env.example`, and related tests/configuration belong to the earlier prototype and do not define the target architecture.

The next implementation focus is the first vertical slice:

```text
Docker Compose
      ↓
Lectio Gateway
      ↓
temporary Playwright Chromium
      ↓
manual MitID login
      ↓
capture Lectio session
      ↓
python-lectio validation
      ↓
normalized schedule / homework / assignments / cancellations
```

Only after that path is proven against the real Lectio environment should substantial effort move into the Home Assistant integration, final renderer, device API, firmware, and USB provisioning.

See [IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md) for the full ordered milestones and the current execution log.
