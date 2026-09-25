# Better Lectio HA Display — Implementation Plan

**Status:** Implementation roadmap  
**Date:** 2026-09-25  
**Target repository:** `MarcusFunt/Better-Lectio-HA-Display`

This plan implements the architecture defined in `ARCHITECTURE_AND_OPERATIONS.md`.

The order is deliberately chosen to validate the riskiest integration boundaries early and to prevent the renderer, Home Assistant integration, and firmware from becoming coupled to temporary authentication details.

---

# 1. Implementation principles

Use the following principles throughout the rewrite:

- establish service boundaries before adding features
- isolate Lectio/MitID behavior from the display path
- keep `python-lectio` behind an adapter
- make Home Assistant the renderer-facing source of truth
- keep the renderer deterministic and easy to test
- keep browser authentication replaceable
- keep firmware build and device provisioning separate
- preserve last-known-good data on partial failures
- add regression tests at every layer boundary
- do not preserve obsolete Pi/systemd architecture merely for compatibility

---

# 2. Target repository structure

Refactor toward approximately:

```text
.
├── docker-compose.yml
├── .env.example
├── Makefile
├── README.md
├── docs/
│   ├── ARCHITECTURE_AND_OPERATIONS.md
│   ├── IMPLEMENTATION_PLAN.md
│   ├── AUTHENTICATION.md
│   ├── HOME_ASSISTANT.md
│   ├── FIRMWARE.md
│   └── OPERATIONS.md
│
├── services/
│   ├── lectio-gateway/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   ├── app/
│   │   │   ├── api/
│   │   │   ├── auth/
│   │   │   ├── lectio/
│   │   │   ├── models/
│   │   │   ├── persistence/
│   │   │   └── main.py
│   │   └── tests/
│   │
│   ├── lectio-auth-browser/
│   │   ├── Dockerfile
│   │   ├── playwright/
│   │   └── scripts/
│   │
│   └── display-service/
│       ├── Dockerfile
│       ├── pyproject.toml
│       ├── app/
│       │   ├── ha/
│       │   ├── model/
│       │   ├── rendering/
│       │   ├── devices/
│       │   ├── api/
│       │   └── main.py
│       └── tests/
│
├── home-assistant/
│   └── custom_components/
│       └── better_lectio/
│           ├── __init__.py
│           ├── manifest.json
│           ├── config_flow.py
│           ├── coordinator.py
│           ├── calendar.py
│           ├── todo.py
│           ├── sensor.py
│           └── diagnostics.py
│
├── firmware/
│   ├── platformio.ini
│   ├── src/
│   ├── include/
│   └── test/
│
└── tools/
    └── provision/
        ├── pyproject.toml
        ├── provision.py
        └── tests/
```

Exact names may evolve, but the boundaries should remain.

---

# 3. Milestone 0 — repository cleanup and migration scaffolding

## Goals

- stop extending the old monolithic layout
- preserve useful renderer/device code where appropriate
- create the new service boundaries
- keep the repository buildable during migration

## Tasks

1. Create:
   - `services/lectio-gateway`
   - `services/display-service`
   - `home-assistant/custom_components/better_lectio`
   - `firmware`
   - `tools/provision`
   - `docs`

2. Move the architectural documents into `docs/`.

3. Add a root `docker-compose.yml`.

4. Add `.env.example` with placeholders only.

5. Add a root `Makefile` or equivalent task runner.

Suggested commands:

```text
make test
make lint
make compose-up
make compose-down
make firmware
make flash PORT=...
make provision PORT=...
```

6. Retire Raspberry Pi/systemd-specific files or move them into a clearly marked legacy folder until deletion.

7. Preserve current rendering tests where they remain applicable.

## Exit criteria

- Docker Compose parses successfully.
- Each service has an independent package/build definition.
- Root documentation describes the new architecture.
- CI can run per-service tests.

---

# 4. Milestone 1 — Lectio session/data adapter

Do this before browser authentication so the browser flow has a concrete session-validation target.

## 4.1 Create Lectio adapter interface

Define a stable application-facing interface.

Example:

```python
class LectioClient:
    async def validate_session(self) -> bool: ...
    async def get_schedule(self, start, end): ...
    async def get_assignments(self, start, end): ...
    async def get_homework(self, start, end): ...
    async def get_cancellations(self, start, end): ...
```

Do not expose raw `python-lectio` result structures outside this module.

## 4.2 Add normalized domain models

Define internal typed models for:

```text
LectioLesson
LectioAssignment
LectioHomework
LectioCancellation
AuthenticatedLectioSession
LectioSyncStatus
```

A lesson should include at least:

```text
id
start
end
subject
teacher
room
status
source_url/source_id when available
```

An assignment should include at least:

```text
id
title
description
due
subject
status
source_url
```

Homework should include at least:

```text
id
subject
description
target_lesson_start
source_url
```

Cancellation should include at least:

```text
id
original_lesson/reference
start/end
subject
teacher
room
reason/details if available
```

## 4.3 Integrate python-lectio

- start with a pinned upstream version/commit
- wrap cookie/session import
- implement the four core data calls
- identify missing fields/bugs
- keep compatibility patches local to the adapter
- create a fork only when actually necessary

## 4.4 Add fixture-driven parser tests

Create sanitized saved-response fixtures for:

- schedule
- homework
- assignments
- cancelled lessons
- malformed/changed HTML
- expired session

## Exit criteria

- all four data categories normalize successfully from fixtures
- application code outside `lectio/` does not import `lectio.sdk`
- a serialized authenticated session can instantiate/restore the adapter
- session validation has a reliable success/failure result

---

# 5. Milestone 2 — Phase-1 Playwright MitID/Lectio authentication

This is the highest-risk functional milestone.

## 5.1 Auth state machine

Implement explicit states:

```text
UNCONFIGURED
LOGIN_REQUIRED
STARTING_BROWSER
WAITING_FOR_USER
AUTHENTICATED
SESSION_EXPIRED
AUTH_FAILED
```

Persist current status and last transition time.

## 5.2 Temporary browser service

Create `lectio-auth-browser`.

Requirements:

- Playwright
- Chromium
- isolated profile per auth attempt
- lifecycle controlled by gateway
- no permanent browser process when unused
- hard timeout for abandoned sessions
- cleanup on failure

## 5.3 Remote browser viewing

Provide a temporary browser UI through a private path accessible over the Tailnet.

Possible implementation:

```text
Chromium
  ↓
Xvfb
  ↓
x11vnc
  ↓
noVNC/websockify
  ↓
Tailscale Serve
```

Keep this component replaceable.

Do not expose the VNC/noVNC port publicly.

## 5.4 Successful-login detection

Use Playwright to detect an authenticated Lectio state.

Prefer robust indicators such as:

- authenticated Lectio URL
- known logged-in element
- logout link
- expected user/student page state

Do not depend on timing alone.

## 5.5 Cookie/session extraction

On successful login:

1. read browser context cookies
2. normalize them
3. determine school ID/student ID
4. construct `AuthenticatedLectioSession`
5. create the python-lectio adapter from it
6. run a harmless authenticated validation request
7. persist only if validation succeeds

## 5.6 Session storage

Initially use a local persistent volume with strict permissions.

Store:

- cookie/session payload
- school ID
- student ID
- created timestamp
- last validated timestamp

Do not store:

- screenshots containing sensitive authentication data
- MitID secrets
- browser profile after successful extraction unless proven necessary

## 5.7 Gateway auth API/UI

Implement at least:

```text
GET  /auth/status
POST /auth/start
POST /auth/cancel
POST /auth/logout
GET  /auth/browser
```

The UI should clearly show:

- authenticated
- authentication required
- browser starting
- waiting for user
- validation failed
- session expired

## Exit criteria

A real end-to-end manual test succeeds:

```text
fresh system
→ start login
→ remote Chromium opens Lectio
→ user completes MitID
→ gateway captures cookies
→ Chromium stops
→ python-lectio validates
→ schedule data can be fetched without Chromium
```

This milestone must be proven with the user's real Lectio environment before proceeding as "complete".

---

# 6. Milestone 3 — Lectio Gateway API and resilience

## 6.1 Stable local API

Expose normalized endpoints, for example:

```text
GET /api/v1/status
GET /api/v1/schedule?start=...&end=...
GET /api/v1/assignments?start=...&end=...
GET /api/v1/homework?start=...&end=...
GET /api/v1/cancellations?start=...&end=...
```

These are internal APIs intended for the Home Assistant integration.

## 6.2 Caching

Add separate caches per source category.

Do not use one global all-or-nothing cache.

Track:

```text
last_attempt
last_success
is_stale
error
data
```

for each source.

## 6.3 Failure isolation

A failure in one category must not invalidate others.

Example:

```text
schedule: fresh
assignments: stale
homework: fresh
cancellations: fresh
```

should still return usable data.

## 6.4 Reauthentication trigger

When session validation or Lectio requests indicate authentication failure:

- mark `LOGIN_REQUIRED`
- retain last-known-good data
- expose status
- make remote login available

## Exit criteria

- API is stable and typed
- per-source stale fallback works
- auth expiry does not erase cached data
- health/status endpoints explain failures

---

# 7. Milestone 4 — Home Assistant custom integration

This milestone makes HA the mandatory middle layer.

## 7.1 Config flow

Create a normal HA config flow.

Configuration should include:

```text
Lectio Gateway URL
optional API credential if used
refresh behavior
```

The gateway will normally be local/private.

## 7.2 Data coordinator

Implement a coordinator that fetches:

- schedule
- assignments
- homework
- cancellations
- gateway session/sync status

Use independent source freshness where possible.

## 7.3 Calendar entity

Create:

```text
calendar.lectio
```

Support date-range queries.

Map:

```text
subject → summary
room → location
teacher → description/structured metadata
```

Preserve stable IDs where HA allows.

## 7.4 Assignments todo entity

Create:

```text
todo.lectio_assignments
```

Read-only initially.

Map due date/time and structured metadata.

Reject or disable write actions unless intentionally implemented later.

## 7.5 Homework todo entity

Create:

```text
todo.lectio_homework
```

Read-only initially.

Use target lesson date/time as due information where sensible.

## 7.6 Sensors

Create at least:

```text
sensor.lectio_cancellations
sensor.lectio_session_status
sensor.lectio_last_sync
```

Cancellation records should be available in a structured form.

## 7.7 Diagnostics

Implement HA diagnostics with secret redaction.

Include:

- entity status
- gateway reachability
- last sync
- stale/fresh state
- current integration version

## 7.8 Tests

Test:

- entity creation
- coordinator failure handling
- date-range schedule queries
- todo normalization
- cancellation sensor behavior
- auth-expired state
- diagnostics redaction

## Exit criteria

From Home Assistant alone, the user can see:

- Lectio calendar
- assignments
- homework
- cancellations
- session health

And private events can be created through normal HA calendar functionality.

---

# 8. Milestone 5 — display model

Before rendering pixels, define the renderer input.

## 8.1 Home Assistant client

Display service reads only from HA.

Retrieve:

- `calendar.lectio`
- configured private calendar entity/entities
- `todo.lectio_assignments`
- `todo.lectio_homework`
- `sensor.lectio_cancellations`

Do not add a direct Lectio fallback.

## 8.2 Display model

Create a deterministic typed model such as:

```text
DisplayModel
├── generated_at
├── days[]
│   ├── date
│   └── events[]
└── sidebar[]
```

Event:

```text
id
start
end
title
teacher?
room?
source
all_day
```

Sidebar item:

```text
id
kind = cancellation | assignment | homework
priority
title
subtitle
due/start
source
```

## 8.3 Date window

Always build:

```text
today
tomorrow
day after tomorrow
```

using `Europe/Copenhagen`.

## 8.4 Merge behavior

Merge private and Lectio calendar events onto one chronological time axis.

Do not render source-specific columns.

## 8.5 Sidebar selector

Implement priority as domain code:

```text
cancellations > assignments > homework
```

Then urgency within categories.

Make max visible items configurable.

## Exit criteria

Given the same HA fixture input, the display model is deterministic and snapshot-testable.

---

# 9. Milestone 6 — renderer rewrite

## 9.1 Layout

Target:

```text
~80–85% main schedule
~15–20% sidebar
```

Main schedule:

- today
- tomorrow
- day after tomorrow
- chronological ordering

Lectio lesson rows/blocks show:

- time
- subject
- teacher
- room

Sidebar:

- cancellations
- assignments
- homework

## 9.2 Rendering constraints

Output:

```text
800×480
1-bit monochrome
BMP
```

Avoid grayscale assumptions.

Use fonts that are legally redistributable or system-provided in the container.

## 9.3 Overflow handling

Define deterministic overflow rules.

Examples:

- collapse low-priority sidebar items first
- abbreviate long teacher/room strings
- truncate with ellipsis
- preserve today over later days when space becomes constrained
- avoid unreadably small text

## 9.4 Content hashing

Hash the canonical rendered content/image.

Produce content-addressed filenames such as:

```text
<hash>.bmp
```

Only publish a new display revision when content actually changes.

## 9.5 Visual regression tests

Create golden/snapshot images for:

- normal school day
- dense day
- no events
- long subject names
- mixed private + Lectio events
- cancellation-heavy sidebar
- assignment-heavy sidebar
- homework fallback
- stale-data indicator if included

## Exit criteria

Renderer output matches the new UI and passes pixel/snapshot regression tests.

---

# 10. Milestone 7 — new local device API

Remove dependence on TRMNL cloud/BYOS semantics where they no longer help.

## 10.1 Device endpoints

A minimal API could be:

```text
GET /device/v1/status
GET /device/v1/display
GET /device/v1/image/<hash>.bmp
```

`/device/v1/display` should return enough information for the firmware to decide whether an update is needed:

```json
{
  "content_hash": "...",
  "image_url": "/device/v1/image/....bmp",
  "generated_at": "...",
  "next_check_seconds": 30
}
```

## 10.2 Device authentication

Use:

```text
device ID
+
random per-device bearer secret
```

Reject unknown/revoked credentials.

Do not rely on MAC address as authentication.

## 10.3 Device registry

Persist:

```text
device_id
credential_hash
name
created_at
last_seen
revoked
firmware_version
last_content_hash
```

Store a one-way hash of the server-side credential where practical.

## Exit criteria

A test client can:

- authenticate
- obtain current content metadata
- download image
- be revoked
- fail authentication correctly

---

# 11. Milestone 8 — custom firmware

## 11.1 Start from hardware knowledge, not cloud behavior

Inspect/fork upstream TRMNL firmware only for:

- board definitions
- e-paper driver setup
- display initialization
- Wi-Fi hardware handling
- known hardware quirks

Remove unnecessary cloud-specific behavior.

## 11.2 Firmware modules

Suggested modules:

```text
config
wifi
device_auth
display_api
image_fetch
epaper
state
diagnostics
version
```

## 11.3 Boot behavior

Because the device is mains-powered:

- boot
- connect Wi-Fi
- stay connected
- poll/check frequently
- update only when content hash changes
- remain available for diagnostics

No battery-first deep-sleep architecture is required.

## 11.4 Image update safety

Use:

1. fetch metadata
2. compare hash
3. fetch image only when changed
4. validate size/format
5. optionally validate checksum
6. update panel
7. record applied hash

Never clear the display because the network is temporarily unavailable.

## 11.5 Diagnostics

Expose over serial at minimum:

```text
firmware version
device ID
Wi-Fi status
server URL
last HTTP result
current content hash
last successful update
```

Do not print the full device secret.

## Exit criteria

A flashed but unprovisioned device boots into a safe provisioning-required state, and a provisioned device can display server content reliably for repeated update cycles.

---

# 12. Milestone 9 — USB provisioning tool

## 12.1 Provisioning protocol

Implement a simple USB serial provisioning protocol.

Commands should support:

```text
identify
write-config
read-nonsecret-status
reboot
factory-reset
```

Avoid returning secrets after provisioning unless absolutely necessary.

## 12.2 Provision tool flow

`make provision PORT=...` should:

1. connect
2. identify board
3. optionally verify firmware compatibility
4. generate device ID
5. generate secure random secret
6. create device record in display service
7. send:
   - Wi-Fi SSID
   - Wi-Fi password
   - display-service LAN URL
   - device ID
   - device secret
8. reboot
9. wait for device registration/heartbeat
10. verify a successful authenticated display request

## 12.3 Recovery

Support:

```text
rotate credential
re-provision Wi-Fi
revoke device
factory reset
```

Credential rotation should avoid reflashing firmware.

## Exit criteria

A fresh board can go from USB connection to working display with one provisioning command.

---

# 13. Milestone 10 — Tailscale-backed operations

This affects human/admin access, not display traffic.

## 13.1 Tailscale Serve

Document exposing:

- Lectio login/status UI
- service diagnostics
- optional admin UI

through Tailscale Serve.

Keep the underlying admin services bound to localhost/private interfaces where possible.
## 13.2 ACL assumptions

Document expected Tailnet ACL access.

Do not assume every Tailnet member should automatically get Lectio admin access.

## 13.3 No public exposure

Do not require:

- router port forwarding
- public reverse proxy
- Tailscale Funnel

for normal administration.

## Exit criteria

The user can be away from home, join the Tailnet, and:

- inspect status
- trigger Lectio reauthentication
- complete MitID login
- inspect display/service health

without public Internet exposure of the services.

---

# 14. Milestone 11 — Phase-2 auth-flow analysis

Only do this after Phase 1 has been proven with real authentication.

## Tasks

Capture and document:

- login entry URL
- redirect chain
- domains involved
- callback points
- cookies before authentication
- cookies after authentication
- cookies actually required by python-lectio
- relationship between browser session and Lectio session
- logout/expiry behavior

Do not record or commit real sensitive values.

Create sanitized state-transition documentation.

Add tests around:

```text
browser cookies → AuthenticatedLectioSession
AuthenticatedLectioSession → python-lectio session
expired cookies → LOGIN_REQUIRED
```

## Exit criteria

The authentication flow is understood well enough to decide whether Phase 3 is feasible without browser streaming.

---

# 15. Milestone 12 — Phase-3 streamlined login

Proceed only if Phase 2 shows that the browser flow can be safely simplified.

## Goal

Replace normal use of remote Chromium with:

```text
/auth/login
```

opened in the user's normal browser.

## Requirements

- real MitID remains manual
- no credential scraping
- same `AuthenticatedLectioSession` result
- same validation path
- Playwright fallback remains available

## Exit criteria

Normal reauthentication no longer requires the remote browser UI, while `/auth/browser-login` can still recover the system if Lectio changes.

---

# 16. Milestone 13 — operational hardening

## Add health checks

Compose health checks for:

```text
lectio-gateway
display-service
auth-browser when active
```

## Add restart policies

Use sensible container restart policies for always-on services.

Do not restart-loop the temporary browser service forever on auth failure.

## Add metrics/status

At minimum make visible:

```text
Lectio authenticated?
last Lectio sync
HA reachable?
last HA fetch
last render
current image hash
last device request
firmware version
```

## Logging

- structured logs where useful
- timestamps
- request correlation IDs for cross-service operations
- secret redaction
- no browser cookie dumps

## Backups

Document backing up:

```text
Lectio session state
device registry
display-service state
Home Assistant integration configuration
```

Do not treat generated BMPs as critical backup data.

---

# 17. Milestone 14 — CI

CI should include:

## Python services

- formatting
- lint
- type checking where adopted
- unit tests
- fixture parser tests
- API tests
- renderer snapshot tests

## Home Assistant integration

- manifest validation
- unit tests
- HA-style checks where practical

## Firmware

- PlatformIO compile
- build for actual target board
- static checks
- host-side unit tests where possible

## Provision tool

- unit tests
- protocol parser tests
- mock serial integration tests

## Docker

- build all images
- validate Compose configuration

The repository should never merge code that breaks firmware compilation if firmware and server are intended to ship together.

---

# 18. Suggested implementation PR sequence

Keep changes reviewable.

### PR 1 — Architecture scaffold
- docs
- Compose skeleton
- new directories
- CI skeleton
- retire Pi-first assumptions

### PR 2 — Lectio adapter
- normalized models
- python-lectio wrapper
- fixtures/tests

### PR 3 — Phase-1 Playwright authentication
- browser service
- auth state machine
- session extraction/validation
- Tailnet-only browser docs

### PR 4 — Lectio Gateway API/resilience
- normalized endpoints
- per-source cache
- stale fallback
- health/status

### PR 5 — Home Assistant integration
- calendar
- todo assignments
- todo homework
- cancellation/session sensors

### PR 6 — display model
- HA client
- three-day merge
- sidebar priority rules

### PR 7 — renderer rewrite
- chronological layout
- sidebar
- snapshot tests

### PR 8 — device API
- registry
- credentials
- content-hash protocol

### PR 9 — firmware baseline
- fork/reference upstream hardware code
- Wi-Fi
- device API
- BMP rendering
- continuous mains-powered operation

### PR 10 — USB provisioning
- serial protocol
- credential generation
- server registration
- end-to-end self-test

### PR 11 — Tailscale operations
- Serve configuration/docs
- hardened service binding
- remote auth/status workflow

### PR 12 — authentication Phase 2
- sanitized flow analysis
- regression fixtures
- design decision for Phase 3

### PR 13 — authentication Phase 3
- streamlined `/auth/login`
- retain browser fallback

---

# 19. End-to-end acceptance criteria

The architecture is considered functionally implemented when all of the following are true.

## Authentication

- A fresh installation reports Lectio login required.
- The user can start a browser-login session remotely over the Tailnet.
- The user completes MitID manually.
- The gateway captures and validates the Lectio session.
- Chromium terminates afterwards.
- Normal Lectio sync proceeds without a browser process.

## Home Assistant

HA exposes:

```text
calendar.lectio
todo.lectio_assignments
todo.lectio_homework
sensor.lectio_cancellations
sensor.lectio_session_status
```

A private calendar event added through normal HA Calendar appears in the display model.

## Display

The rendered 800×480 monochrome UI shows:

- today
- tomorrow
- day after tomorrow
- merged chronological Lectio/private events
- subject
- teacher
- room
- right sidebar

Sidebar order is:

```text
cancellations
assignments
homework
```

## Firmware

- firmware compiles in CI
- the same generic firmware binary can be used for multiple devices
- per-device secrets are provisioned later over USB
- the display authenticates over LAN
- unchanged content does not cause unnecessary e-paper refresh
- changed content is applied automatically
- temporary server/network failure leaves the previous valid image visible

## Operations

- services run under Docker Compose
- remote human access uses Tailscale
- admin/auth services are not directly exposed publicly
- stale source data is retained
- logs do not leak secrets
- health/status endpoints identify the failing layer

---

# 20. First implementation focus

The first engineering pass should stop after proving this vertical slice:

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
GET normalized schedule/homework/assignments/cancellations
```

Do not spend significant time polishing the renderer or firmware before this vertical slice works against the real Lectio account.

Once that succeeds, the highest-risk unknown in the architecture is removed and the remaining work becomes normal integration and product engineering.

# 21. Agent execution log

This section is the canonical running record for agent evidence, completed work, outcomes, and next steps. Agents must update this section for every repository task as required by `AGENTS.md`.

## 2026-09-25 — Establish agent workflow and documentation baseline

### Evidence and findings

- Before this documentation PR, the repository's default branch contained one Markdown file: `README.md`.
- The current `README.md` still documents the superseded first implementation: Raspberry Pi Zero deployment, systemd services, stock TRMNL/Seeed firmware, direct `python-lectio` login in the renderer/server, and an optional raw ICS feed. It was intentionally left unchanged in this task because routine agent progress must not be spread across multiple Markdown files.
- The architecture agreed after the initial implementation is captured in `ARCHITECTURE_AND_OPERATIONS.md`, including Docker Compose deployment, a dedicated Lectio Gateway, Home Assistant as the mandatory middle layer, Playwright-based Phase-1 MitID/Lectio authentication, custom firmware, USB-only provisioning, LAN-only display connectivity, and Tailscale-backed remote human/admin access.
- A dedicated root `AGENTS.md` is necessary so future agents cannot begin from the stale README alone or create fragmented progress/handoff files.
- Branch verification against `main` showed the documentation branch was ahead with only the intended Markdown additions: `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, and `IMPLEMENTATION_PLAN.md`. No runtime source/configuration files were changed.

### Tasks completed

- Added `AGENTS.md` with mandatory recursive reading of every `.md` file before repository work.
- Defined `IMPLEMENTATION_PLAN.md` as the single canonical Markdown location for ongoing execution evidence, findings, completed-task records, outcomes, and next steps.
- Added a Markdown write policy: progress/status/handoff notes go only into `IMPLEMENTATION_PLAN.md`; other Markdown files are read-only unless the user explicitly requests a documentation/architecture change.
- Added explicit requirements to distinguish real integration/hardware verification from fixture or mocked tests and to avoid claiming checks that were not actually run.
- Added `ARCHITECTURE_AND_OPERATIONS.md` and this `IMPLEMENTATION_PLAN.md` to the repository so the new agent rules have an authoritative architecture and roadmap to read.

### How it went

- This is a documentation-only governance change; no runtime code, Docker configuration, Home Assistant integration, firmware, or deployment behavior was changed.
- No application tests were required for the content itself. Validation consisted of comparing the branch against `main` and confirming that the branch contains the four expected Markdown files: existing `README.md` plus the three new documents.
- During validation, the first upload of `IMPLEMENTATION_PLAN.md` was found to have been truncated because the source-file reader returned only its first 1,000 lines. The file was reconstructed from both source ranges before the PR was opened. This is exactly the kind of implementation evidence this execution log is intended to preserve.
- The stale `README.md` remains a known inconsistency with the new architecture. Because the user specifically requested that routine agent updates only touch the implementation plan, it is not rewritten opportunistically here; a future explicit documentation task can retire or replace its old setup instructions.

### Next steps

1. Begin Milestone 0 / PR 1 architecture scaffolding from the implementation sequence above.
2. Before that implementation starts, read every `.md` file recursively as required by `AGENTS.md`.
3. Build the Docker Compose/service skeleton without preserving Pi/systemd assumptions merely for compatibility.
4. Continue updating this execution log on every task with concrete evidence, completed work, outcome, and next actions.

## 2026-09-25 — Rewrite stale project documentation

### Evidence and findings

- The repository contains four Markdown files on this branch: `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, `IMPLEMENTATION_PLAN.md`, and `README.md`.
- All four Markdown files were read recursively before editing, in accordance with `AGENTS.md`.
- `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, and the implementation roadmap already match the updated design.
- `README.md` was the only stale project document. It still presented the retired prototype as the active architecture: Raspberry Pi Zero deployment, systemd services, stock TRMNL/Seeed firmware, direct Lectio login from the old server, raw ICS calendar input, and TRMNL BYOS endpoints.
- The runtime repository is still in migration: existing `trmnl_schedule/`, `systemd/`, legacy environment configuration, and related tests are old implementation artifacts. Documentation must describe that fact rather than claiming the replacement stack already exists.

### Tasks completed

- Rewrote `README.md` to describe the current layered architecture:
  - dedicated Lectio Gateway;
  - phased Playwright → inspected flow → streamlined-login authentication strategy;
  - `python-lectio` lineage behind an adapter;
  - Home Assistant as the mandatory middle layer;
  - HA calendar/todo/sensor entity model;
  - chronological today/tomorrow/day+2 display with cancellations > assignments > homework sidebar priority;
  - Docker Compose deployment;
  - custom always-powered LAN-only firmware;
  - USB-only runtime provisioning with per-device credentials;
  - Tailscale for remote human/admin access.
- Removed obsolete README instructions for Raspberry Pi Zero, systemd installation, raw ICS configuration, stock TRMNL setup, battery-oriented polling, and old BYOS endpoints.
- Added an explicit migration-status section so readers can distinguish the target architecture from the legacy runtime code that remains in the repository.
- Added links from the README to `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, and this implementation plan.

### How it went

- Documentation now presents one consistent architecture. No architectural decisions were changed.
- This was a documentation-only task. No runtime code, Compose file, Home Assistant integration, firmware, provisioning tool, or external service was modified or tested.
- The README intentionally labels target commands such as `make firmware` and `make provision` as planned workflows rather than claiming they are already implemented.
- The previous execution-log note that the README was a known stale inconsistency is superseded by this entry; it remains useful history explaining why the rewrite was necessary.

### Next steps

1. Keep this PR focused on the documentation/governance baseline and review the rewritten README together with the architecture and implementation plan.
2. After merge, begin Milestone 0 by creating the Docker Compose/service/package scaffolding and retiring the old Pi/systemd runtime assumptions in code/configuration.
3. Preserve the explicit distinction between target architecture and verified implementation state until each milestone is actually completed.
4. Continue recording all implementation evidence, outcomes, and next actions in this execution log.

## 2026-09-25 — Milestone 0: repository scaffolding

### Evidence and findings

- Work started from `main` at `233185c97f6972ca055d80e1c6095bfdb3c4f610`; the checkout was clean and the repository contains four Markdown files at its root. All Markdown files were read before implementation, as required by `AGENTS.md`.
- The existing runtime was still the TRMNL/Pi prototype: root `trmnl_schedule/`, systemd units, a root `requirements.txt`, and `.env.example` entries for a Lectio username/password and raw ICS URL.
- The baseline suite passed after installing the existing requirements into a local virtual environment: 14 tests passed.
- GitHub Actions run `36120783896` for PR #3 passed tests, lint, `docker compose config`, and both service image builds. PR #3 merged as `43741de1c6b22060027f316916e3387d67560112`; the later Docker run recorded by Marcus also confirmed both containers healthy.

### Tasks completed

- Added independent Python packages and Dockerfiles for `services/lectio-gateway` and `services/display-service`. Each currently exposes only a named `/health` endpoint; Lectio access, Home Assistant data, display rendering, and device APIs remain for their planned milestones.
- Added a root `docker-compose.yml` with separate services, persistent named volumes, restart policies, health checks, and internal-only ports. It publishes no host ports by default; later API/device milestones must add only the access paths they require.
- Replaced the old `.env.example` values with target-stack settings and an empty Home Assistant token placeholder. No Lectio password or raw ICS setting is present.
- Added `Makefile` targets for development installation, tests, lint, and Compose config/build/up/down. Added per-service health tests and regression checks for Compose boundaries and the environment template.
- Added `.github/workflows/ci.yml` to run the existing prototype tests, new service tests, lint, Compose configuration validation, and service image builds.
- Moved the old systemd units and their dependency list into `legacy/systemd/` and `legacy/requirements.txt`. Updated `README.md` to label these files as prototype artifacts and describe the current scaffold accurately.
- Added empty structure markers for `docs/`, the Home Assistant integration, firmware, and the provisioning tool. No Home Assistant behavior, firmware board configuration, or USB provisioning protocol was guessed ahead of its milestone.

### How it went

- TDD checks first failed because the Compose file and health modules were absent and because the old environment template still exposed the retired login/ICS settings. After the implementation, `make test` passed with 20 tests, `make lint` passed, and `pip wheel --no-deps` built both service packages successfully.
- The first wheel build created setuptools `build/` files inside each service source tree; those generated files were removed from the change and `build/` is now ignored.
- `git diff --check` passed. The new tests cover service/volume/port boundaries; GitHub Actions run `36120783896` passed Docker Compose's native parser and both image builds.
- No live Lectio/MitID login, Home Assistant connection, Tailnet route, firmware build/flash, or physical display test was run. These behaviors are not part of Milestone 0.
- **Ruling:** keep `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, and `IMPLEMENTATION_PLAN.md` at the repository root, although Milestone 0's approximate tree suggests moving architecture documents into `docs/`. `AGENTS.md` requires reading and updating those canonical root paths, and the earlier user instruction made `IMPLEMENTATION_PLAN.md` the single progress log. The `docs/` directory is created for later supporting documents. Cost if wrong: the repository differs from the approximate layout until the agent contract is deliberately revised.
- **Ruling:** use `IMPLEMENTATION_PLAN.md` as the execution ledger rather than creating the separate SDD `progress.md`; `AGENTS.md` and the earlier user instruction require one canonical progress document. Cost if wrong: the SDD helper scripts cannot independently resume this milestone, but the repository's mandated evidence log remains complete.
- Milestone 0 is complete. PR #3 merged as `43741de1c6b22060027f316916e3387d67560112`, and CI run `36120783896` passed its Docker Compose and image build checks.
- Final review: self-review (no subagent tool). The scaffold diff was reviewed against Milestone 0 and `AGENTS.md`; no Critical or Important issues were found.

### Next steps

1. Milestone 0's scaffold PR and hosted CI checks are complete.
2. Proceed to Milestone 1: define the normalized Lectio domain models and adapter boundary, pin the selected `python-lectio` source, and add fixture-driven tests before implementing the Phase-1 browser flow.
3. Keep the first real MitID/Lectio session test explicitly pending until it is run against the user's account.

## 2026-09-25 — Start the Docker scaffold locally

### Evidence and findings

- Read all four repository Markdown files recursively before making changes, as required by `AGENTS.md`.
- The working tree was clean at the start of this task. Docker Engine 29.7.2 and Docker Compose 5.4.0 are installed and available.
- `docker-compose.yml` already defines the `lectio-gateway` and `display-service` containers, persistent named data volumes, container health checks, and internal-only port exposure. It does not publish host ports, matching the current architecture and README.
- `docker compose config --quiet` completed successfully. Both service images built from their local Dockerfiles, and `docker compose up --build --detach` started both services.
- `docker compose ps` reported both containers `Up` and `healthy`. A direct request to each container's `/health` endpoint returned HTTP 200.
- The new services currently implement health endpoints only. Lectio login/data access, Home Assistant integration, rendering, and device APIs are not implemented yet. No `.env` file or Home Assistant token was needed to start this scaffold.

### Tasks completed

- Started the local Compose project with `docker compose up --build --detach`.
- Confirmed Compose configuration, successful image builds, container health, and both in-container health responses.
- Left application configuration and host port exposure unchanged; no runtime source/configuration files were modified.

### How it went

- Local Docker setup succeeded with both containers healthy and their data persisted in named volumes.
- No project test suite was run. No Lectio/MitID, Home Assistant, Tailnet, firmware, or physical display integration was exercised.
- The containers are reachable only on the Compose network; Compose currently publishes no ports to Windows, so the health endpoints are not available directly at `localhost`.

### Next steps

1. Use `docker compose down` from the repository root when the local scaffold should be stopped; named volumes are retained by default.
2. Continue with Milestone 1 to implement and validate the Lectio adapter before adding authentication and user-facing host access.
3. Keep this local run separate from claims that the full display application is operational; only the health-only service scaffold is running.


## 2026-09-25 — Milestone 1: Lectio session/data adapter

### Evidence and findings

- Started from the current `main` after the scaffold merge (`e92471a19065de35b37f7a32b75d3b1ae3aa6b2d`). The earlier scaffold CI run 36120783896 passed its Compose parser and both service image builds.
- Selected and pinned `python-lectio==1.31.0`. PyPI lists 1.31.0 as the latest release; the upstream repository's main commit `002661bed43132129106f92a150897d7fe323f5c` has the same package version in `setup.py`. Sources: [PyPI 1.31.0](https://pypi.org/project/python-lectio/1.31.0/), [upstream source](https://github.com/BetterLectio/python-lectio/tree/main).
- Upstream source inspection found that its cookie importer feeds a combined domain/path string to `requests` as the cookie domain. Its schedule parser also omits the day/date association when returning its normalized module list. The adapter locally reconstructs the cookie jar from browser attributes and parses the schedule day columns so lesson timestamps remain dated.
- The upstream repository labels its license AGPL-3.0. The project has not assessed distribution or licensing implications.
- Parser fixtures are synthetic, sanitized shapes based on upstream selectors/SDK outputs; they are not captured from the user's Lectio account. No account credentials or personal data are included.

### Tasks completed

- Added the exact `python-lectio` pin plus explicit HTTP, HTML parser, and timezone database dependencies to the gateway package.
- Added typed Pydantic models for lessons, assignments, homework, cancellations, restorable authenticated sessions, cookies, and sync status. Cookie values use `SecretStr`; explicit serialization is required to reveal a session for persistence.
- Added an async `LectioClient` boundary with session validation, date-range schedule queries, assignments/detail enrichment, homework-to-lesson correlation, cancellations, and typed expired-session/changed-response errors.
- Added a local Lectio schedule parser that restores dates from ISO-week day columns and retains lesson status, teacher, room, source ID, and cancellation details.
- Added synthetic fixture tests for schedule, assignments, homework, cancellations, malformed markup, expired sessions, cookie restoration, and assignment detail enrichment.

### How it went

- TDD began with a red collection failure because the adapter package did not exist. GitHub Actions run `36126714981` passed all 34 tests, lint, Compose validation, and both image builds. `make install-dev` also installed the exact pinned dependency and editable gateway/display packages successfully.
- GitHub Actions run `36126714981` verified the final branch contents after the source-ID enrichment refinement; the full checks job succeeded.
- No live Lectio account/session was available. The synthetic fixtures establish parser behavior against the inspected structure, but real account validation and current page compatibility remain untested.
- Self-review found no Critical or Important issue in the implementation. A live MitID/Lectio test remains explicitly pending for the authentication milestone.
- Milestone 1's implementation and CI exit criteria are complete on PR #5; the live authenticated-account compatibility check remains pending.

### Next steps

1. Hosted CI run `36126714981` passed tests, lint, Compose parsing, and service image builds; PR #5 remains open for review.
2. Review the pinned dependency's AGPL-3.0 implications before distributing the combined application/image.
3. Continue with Milestone 2 only after the adapter PR is reviewed; validate session handling against the user's real Lectio account during the browser-auth milestone.

## 2026-09-25 — Pull latest changes and restart local Compose services

### Evidence and findings

- Read all repository Markdown files recursively before acting. The working tree was clean and `main` tracked `origin/main` at `e92471a` before the pull.
- `git pull --ff-only` completed successfully and reported `Already up to date`; no upstream commits needed merging.
- The Compose project contains the `lectio-gateway` and `display-service` containers with persistent named volumes.

### Tasks completed

- Rebuilt images and force-recreated both containers with `docker compose up --build --detach --force-recreate`.
- Confirmed `docker compose ps` reports both services `Up` and `healthy` after recreation.
- Confirmed both in-container `/health` requests returned HTTP 200.

### How it went

- Pull and restart succeeded. Existing named volumes were retained.
- No project tests or external Lectio, Home Assistant, Tailnet, firmware, or hardware integrations were run. The containers still provide only the scaffold health endpoints.

### Next steps

1. Continue implementation at Milestone 1 when application behavior is next requested.
2. The local Compose services remain running; stop them with `docker compose down` when no longer needed.

## 2026-09-25 — Retry origin fetch, pull, and restart

### Evidence and findings

- The earlier `git pull --ff-only` used a stale `origin/main` tracking ref and reported no changes. A fresh `git fetch origin` advanced `origin/main` from `e92471a` to `7a5ba3e`.
- The fetched merge includes commits `1940ba7` and `195d9a5` for the Lectio adapter and its CI progress, plus merge commit `7a5ba3e`.
- The update added the typed Lectio adapter, parser fixtures, and gateway dependencies. The app's API surface remains the scaffold health endpoint; the adapter is not yet exposed through new HTTP routes.
- The local execution-log entry from the earlier restart was stashed before fast-forwarding. Applying it conflicted because upstream also appended to `IMPLEMENTATION_PLAN.md`; the conflict was resolved with both entries retained.

### Tasks completed

- Fast-forwarded `main` to `7a5ba3e` with `git pull --ff-only origin main`.
- Rebuilt and force-recreated both containers with `docker compose up --build --detach --force-recreate`. The gateway image installed the new pinned `python-lectio==1.31.0` dependency.
- Confirmed both containers are `Up` and `healthy`; both in-container `/health` requests returned HTTP 200.

### How it went

- The retry succeeded. Compose retained the named data volumes.
- No project test suite or live Lectio/MitID, Home Assistant, Tailnet, firmware, or hardware integration was run. Gateway health confirms process startup, not live Lectio compatibility.

### Next steps

1. Proceed to Milestone 2 only after review of the adapter implementation; real authenticated Lectio validation remains pending.
2. Assess the pinned dependency's AGPL-3.0 implications before distributing the combined image.
3. The local Compose services remain running; stop them with `docker compose down` when no longer needed.

## 2026-09-25 — Milestone 2 implementation (partial): Phase-1 manual browser authentication

### Evidence and findings

- Read all four repository Markdown files recursively before continuing implementation, as required by `AGENTS.md`.
- The repository is at `7a5ba3e` (`origin/main`) with the Lectio adapter already merged. Existing local changes from the authentication implementation and the staged progress entry were preserved.
- `docker compose up --build --detach --force-recreate` built the gateway, display, and new authentication browser images and recreated the Compose services successfully.
- `docker compose ps` reports all three services `Up` and `healthy`. Gateway endpoints `/health`, `/auth/status`, and `/auth/browser` returned HTTP 200. The gateway reached the browser controller's `/health` endpoint with HTTP 200, and the noVNC `vnc.html` page returned HTTP 200.
- Host bindings are restricted to `127.0.0.1:8000` for the gateway and `127.0.0.1:6080` for the browser view. The browser control API on port 8765 is not published to the host.
- `git diff --check` completed without whitespace errors. No project test suite was run. No Lectio/MitID login was performed; live session acceptance remains unverified pending the user's manual login.

### Tasks completed

- Added a separate Playwright browser service that opens an isolated, non-persistent headed Chromium context on demand at the Lectio homepage and exposes it through a local noVNC view.
- Added gateway authentication routes and a local sign-in page with start, cancel, logout, and status actions. The current page is loopback-only; Tailnet access remains for the later operations milestone. MitID interaction remains manual; the implementation does not fill credentials or approve authentication prompts.
- Added cookie candidate extraction, session validation through the existing `LectioClient`, private session/status persistence, and authenticated-session timestamps.
- Added Compose networking and loopback-only host ports, runtime dependencies, lint discovery, environment defaults, and updated existing Compose structure assertions.

### How it went

- Image build and local container startup succeeded. Health and route responses confirm the services are running, but they do not establish compatibility with a real Lectio account. Milestone 2 is partial until the real-account exit criteria are met.
- The Chromium context is created on request and closed after success, cancellation, or timeout. The controller/noVNC sidecar container itself currently remains running while idle. This differs from `ARCHITECTURE_AND_OPERATIONS.md`, which requires the browser service/container to start for authentication and stop afterward. Container-level lifecycle control has not been added; doing so without a broader controller would require a privileged Docker control path. Keep this discrepancy visible and resolve it before treating the architecture's lifecycle requirement as complete.
- `README.md` still reflects the earlier health-only scaffold, but `AGENTS.md` restricts routine progress/documentation changes to this implementation plan. The README was not edited.
- No test suite was run. Real MitID/Lectio authentication, session persistence with real cookies, Home Assistant, Tailscale, firmware, and hardware remain unverified.

### Next steps

1. Open `http://localhost:8000/auth/browser`, start browser login, and manually complete Lectio/MitID authentication; confirm the gateway validates and persists the session.
2. Resolve the browser sidecar container-lifecycle discrepancy without exposing an unnecessarily privileged Docker API to the gateway.
3. Add Tailnet access to the browser view as part of Milestone 10, keeping it private and preserving loopback-only defaults meanwhile.
4. Continue with the next planned data-sync milestone after session validation, keeping the real-account check separate from fixture-based evidence.

## 2026-09-25 — Run project tests before manual sign-in

### Evidence and findings

- The user requested the project test suite before signing in. The documented Make target runs `python -m pytest -q tests services/lectio-gateway/tests services/display-service/tests`.
- The host interpreter is Python 3.11.4, below the gateway/display packages' declared Python 3.12 minimum, and its global environment lacked the legacy `icalendar` dependency and the installed service packages. The host run therefore failed during collection and was not treated as a valid suite result.
- The same suite was run in a disposable container based on the project's Python 3.12 gateway image. The repository was copied from a read-only bind mount into the disposable container, and `requirements-dev.txt` plus both service test extras were installed there.
- The first Python 3.12 run collected 34 tests and found one failure in `test_session_json_restores_cookie_without_leaking_repr`: `AuthenticatedLectioSession.to_json()` passed the newly added timestamps as Python `datetime` values to `json.dumps`.
- After the serialization fix, the same suite completed with `34 passed in 0.59s`.

### Tasks completed

- Updated `AuthenticatedLectioSession.to_json()` to serialize non-cookie fields with Pydantic's JSON mode, while continuing to reveal cookie values only in this explicit persistence method.
- Rebuilt and force-recreated the gateway service so the running container includes the fix. `docker compose ps` reports all services healthy; `/health` and `/auth/status` return HTTP 200, with auth state `LOGIN_REQUIRED`.

### How it went

- The initial host run exposed an incomplete local Python environment, not a code-level test result. Running in the supported Python 3.12 environment produced one actionable failure; the fix passed the full suite.
- No live Lectio/MitID login was attempted. The sign-in flow is now ready for the user's manual step.

### Next steps

1. Open `http://localhost:8000/auth/browser`, start browser login, and manually complete Lectio/MitID authentication.
2. Confirm the gateway validates the real session and persists it before treating Milestone 2's end-to-end criteria as complete.

## 2026-09-25 — Fix Lectio sign-in cookie capture

### Evidence and findings

- The embedded Chromium view was showing the Lectio site, but the auth controller stayed in `waiting_for_user` and did not emit a session candidate. The gateway therefore had nothing to validate; this was upstream of gateway session validation.
- `BrowserControl` requested cookies only for `https://www.lectio.dk`. Playwright can omit host-only cookies scoped to `lectio.dk` from a URL-filtered request. A regression test models that behavior and verifies the candidate still contains only Lectio-domain cookies.
- The focused regression test failed against the URL-filtered implementation and passed after switching to an unfiltered browser-context cookie read. Candidate construction continues to filter cookies to `lectio.dk` and its subdomains.
- The complete project test suite, including the new auth-browser tests, passed in the supported Python 3.12 container: `35 passed in 0.59s`.
- Rebuilt and force-recreated `lectio-auth-browser` and `lectio-gateway`. `docker compose ps` reports all three services healthy. Gateway `/health` returned `ok`, `/auth/status` returned `LOGIN_REQUIRED`, the local sign-in page returned HTTP 200, and the auth-browser's internal `/health` returned `ok` with browser state `idle`.

### Tasks completed

- Changed cookie retrieval to inspect the full browser context, leaving Lectio-domain filtering in candidate construction.
- Added `services/lectio-auth-browser/tests/test_control.py` and included the auth-browser package/tests in `Makefile`'s `install-dev` and `test` targets.
- Rebuilt the local containers with the fix. The previous temporary browser session was reset during recreation.

### How it went

- The regression test reproduced the missed-cookie condition before the implementation change and passed afterward; the full suite is green.
- Service startup and local route health are verified. A real Lectio/MitID login and gateway validation remain unverified because the user must complete the manual sign-in flow.
- No credential entry or authentication approval was performed by the agent.

### Next steps

1. Start the login flow again at `http://localhost:8000/auth/browser` and manually complete Lectio/MitID authentication.
2. Confirm the controller emits a candidate and the gateway validates and persists the real session.

## 2026-09-25 — Implementation-plan progress assessment

### Evidence and findings

- Read all repository Markdown files recursively before assessing status, as required by `AGENTS.md`.
- The plan has 15 milestones, numbered 0 through 14. The execution log records Milestones 0 and 1 as complete. Milestone 2 (Phase-1 browser authentication) remains partial because the real Lectio/MitID sign-in and resulting session validation have not been confirmed.
- Current gateway code includes the Lectio adapter and authentication routes/session handling. The display service still exposes only `/health`; no Home Assistant integration, display model/renderer, device API, firmware, or provisioning implementation is present.
- The worktree is on `main` at `e6fa1eb` and contains local changes to `IMPLEMENTATION_PLAN.md`, `Makefile`, auth-browser cookie retrieval, and `tests/test_compose_scaffold.py`, plus an untracked `services/lectio-auth-lifecycle/` package. The changed Compose assertions expect a lifecycle service/profile/network arrangement that is not yet present in `docker-compose.yml`; the package is also not included in the Makefile or wired into the gateway/browser flow, so this remains in-progress work and does not yet close the logged browser-container lifecycle gap.
- Compose and CI scaffolding are already present, but the broader operational-hardening and CI milestone requirements remain largely future work.

### Tasks completed

- Compared the roadmap and execution log with the current repository tree, relevant service entrypoints, Compose configuration, and worktree changes.
- Recorded the progress estimate: 2 of 15 milestones are complete, Milestone 2 is underway, and Milestones 3–14 remain. This is only a raw milestone count: Milestones 0–2 contain unusually challenging, risk-heavy foundation work, so 2/15 understates effort completed. The plan has no estimates by effort, so a weighted completion percentage would be speculative.

### How it went

- This was a read-only status assessment apart from this required execution-log entry. No application code was changed and no tests were run.
- The first major integration gate is still real account sign-in and session validation. Even after that, gateway API/resilience, Home Assistant, display model/rendering, device API, firmware, USB provisioning, and operations work remain.

### Next steps

1. Complete a manual real Lectio/MitID login and verify that the gateway validates and persists the session.
2. Finish and integrate a least-privilege browser-container lifecycle approach; the current untracked prototype is not yet active in Compose or the authentication flow.
3. Proceed to Milestone 3 (normalized gateway API, per-source sync/cache, and stale-data resilience), then continue through the HA/display/device milestones.

## 2026-09-25 — Weighted progress estimate

### Evidence and findings

- The roadmap does not contain effort estimates, so this is a judgment-based estimate rather than measured project accounting.
- Assumed relative effort weights for Milestones 0–14, summing to 100%: M0 8%, M1 12%, M2 15%, M3 8%, M4 10%, M5 5%, M6 7%, M7 5%, M8 9%, M9 5%, M10 4%, M11 3%, M12 2%, M13 4%, M14 3%.
- Updated worktree evidence: after the initial assessment, uncommitted changes wired the browser lifecycle service into Compose, the gateway auth flow, Makefile, and CI. Those changes are present but have not been verified in this assessment; real-account sign-in remains unproven.
- Estimated completion within those weights: M0 100%, M1 100%, M2 85% (auth flow and lifecycle wiring are implemented in the worktree, but real-account validation and checks remain), M13 30% (Compose health/restart foundations and lifecycle service exist), and M14 45% (baseline tests/lint/Compose/image CI, now including auth-browser/lifecycle coverage, are configured). Other milestone scopes are treated as 0% for this estimate.
- Updated weighted result: 35.3%, rounded to about 35%. Given the subjective weights and completion assumptions, communicate this as roughly 33–38%, not a precise metric.

### Tasks completed

- Added an effort-weighted estimate in response to the user's correction that Milestones 0–2 are disproportionately challenging.

### How it went

- This is a planning estimate only; no code or milestone status changed and no tests were run.
- The estimate reflects meaningful early risk reduction while keeping the real Lectio authentication proof as an open gate and recognizing that the major HA, rendering, device, firmware, and provisioning layers remain.

### Next steps

1. Replace these heuristic weights with effort estimates if a reliable project-level completion percentage is needed.
2. Keep the real-account Phase-1 auth validation as the next major risk gate.

## 2026-09-25 — Complete isolated auth-browser lifecycle wiring

### Evidence and findings

- Final review caught that Docker SDK's `ports: {"8765/tcp": None}` publishes the browser control API on a random host port. Removed that mapping. A live Docker inspection now shows only `6080/tcp` bound to `127.0.0.1:6080`; the browser API remains available to gateway containers on the private runtime network.
- Serialized lifecycle start and stop routes with an app-scoped `asyncio.Lock`. A concurrent stop now waits for an in-progress container start to finish before removing it.
- Gateway cleanup now retries a failed lifecycle stop once and logs a final error without including cookie/session values. The lifecycle service also removes any leftover browser during startup and shutdown.
- Added regression coverage for the host port mapping, overlapping start/stop route calls, retry after a transient cleanup error, and logging after persistent cleanup failure.
- The full supported Python 3.12 suite passed: `43 passed in 1.10s`. Ruff's selected repository lint rules passed. Compose configuration parsed successfully. The first lint run caught an unused `pytest` import in the new gateway test; removing it made the subsequent lint run clean.
- Rebuilt and recreated `lectio-auth-lifecycle` and `lectio-gateway`. Live `POST /auth/start` reached `WAITING_FOR_USER`; the browser exposed only loopback noVNC; `POST /auth/cancel` returned `LOGIN_REQUIRED` and the browser container was removed.
- No real Lectio or MitID sign-in was performed, and no credentials were entered. Milestone 2 remains partial until a user completes sign-in and the gateway validates and persists that real session.

### Tasks completed

- Completed Compose, CI, Makefile, and gateway integration for an on-demand browser container managed by a narrow lifecycle API.
- Kept the Docker socket mount on the lifecycle supervisor only. The gateway has no Docker socket mount; it reaches the supervisor over the control network and the browser over the runtime network. The supervisor's Docker socket access remains a high-impact trust boundary: compromise of that container could control the Docker host.
- Removed host publication of the browser API and serialized lifecycle operations to close the review findings.
- Added bounded cleanup retry and error logging; retained startup cleanup as recovery for an orphaned browser.
- Updated the canonical execution log here rather than creating separate progress or handoff Markdown, in accordance with `AGENTS.md`.

### How it went

- Unit and fixture coverage, lint, Compose parsing, image builds, and the live start/cancel container path all completed successfully. The live path verified service integration and port exposure only; it did not verify Lectio authentication.
- The Ruff command explicitly selects `E4,E7,E9,F,I` to keep this repository's existing baseline stable under the current Ruff release. The broader default Ruff rule set is not being enforced by this change.
- The control network is not marked `internal`; review treated that as a minor hardening opportunity, with the Docker socket remaining the dominant supervisor risk. It is deferred from this lifecycle fix.

### Next steps

1. Open `http://localhost:8000/auth/browser`, start browser login, and manually complete Lectio/MitID authentication; confirm candidate capture, gateway validation, and session persistence.
2. Keep Milestone 2 partial until that real-account flow succeeds. Then proceed to Milestone 3: normalized gateway APIs, per-source sync/cache, and stale-data resilience.

## 2026-09-25 — Investigate signed-in browser not detected

### Evidence and findings

- The user-provided screenshot shows the Lectio student dashboard inside the temporary browser, while the gateway status remains `WAITING_FOR_USER` with null school/student IDs and no reported error.
- A live read of `/auth/status` confirmed the same state. The auth-browser container is running; its recent logs show successful `/session/status` polling but no browser error.
- `BrowserControl._make_candidate()` currently requires numeric `LastLoginExamno` and `LastLoginElevId` cookies. The status API does not reveal whether those names are missing, differently named, or holding values in another format, so the exact capture failure is unresolved.
- The browser API is intentionally not host-published; a direct lookup from the Windows host did not resolve the container DNS name. No cookie values were retrieved, copied, logged, or changed.

### Tasks completed

- Compared the screenshot's visible login state with gateway/browser runtime status and the candidate-extraction requirements.
- Left the active temporary browser session running and did not alter Lectio state.

### How it went

- This was read-only diagnosis. The Lectio page appears signed in, but the gateway has not received a candidate and has not begun session validation or persistence. Milestone 2 remains partial.
- Determining cookie metadata safely requires a redacted diagnostic in the auth-browser and restarting its current temporary browser container; that will end this in-progress browser context and require another manual sign-in.

### Next steps

1. After the user authorizes ending the current temporary browser context, add diagnostics that report only identity-cookie presence/format and Lectio-cookie count, rebuild/recreate auth-browser, then have the user sign in again.
2. Use the redacted diagnostics to fix candidate extraction, then validate and persist the session through the gateway.

## 2026-09-25 — Add redacted browser cookie diagnostics

### Evidence and findings

- The screenshot still shows a signed-in Lectio dashboard while gateway capture requires numeric `LastLoginExamno` and `LastLoginElevId` cookie values. Until a redacted check inspects the browser context, the exact capture failure remains unknown.
- The previously active auth-browser container was already absent from Docker when this work resumed, so its temporary browser context had ended before any action in this turn. The user approved restarting and diagnosing; no prior live browser was available to terminate.
- Added an internal `GET /session/diagnostics` endpoint that reports browser state, whether a context is available, the Lectio cookie count, and each expected identity cookie's status (`missing`, `non_numeric`, `numeric`, or `unavailable`). It does not return cookie names or values and is hidden from the OpenAPI schema.
- The auth-browser API port remains unpublished to the host. After starting the new browser, Docker port inspection showed only noVNC on `127.0.0.1:6080`.

### Tasks completed

- Added `BrowserControl.diagnostics()` and the redacted diagnostics route.
- Added route tests for numeric, non-numeric, missing, and unavailable cookie states, including assertions that synthetic cookie values do not appear in the response.
- Rebuilt `better-lectio-auth-browser:local` and started a fresh login flow through the gateway; the start response was `STARTING_BROWSER` and the managed browser container is running.

### How it went

- The first new route test failed as expected with HTTP 404 before the endpoint was implemented. The auth-browser test file then passed (4 tests).
- The first full-suite runner lacked the root project's legacy dev dependencies, so test collection stopped on missing Pillow, Flask, and PyYAML. With `requirements-dev.txt` and all service test extras installed, the complete suite passed: `46 passed in 2.43s`. Ruff selected rules passed. `docker compose --profile auth-browser config --quiet` passed, and the updated browser image built successfully.
- No real cookie values were retrieved, returned, logged, copied, or modified. The new route has only been exercised with synthetic test cookies. No Lectio/MitID sign-in or real gateway validation has been performed by the agent.
- The new temporary browser is awaiting the user's manual sign-in. Milestone 2 remains partial pending redacted runtime diagnosis, candidate capture, gateway validation, and persistence.

### Next steps

1. Have the user manually complete Lectio/MitID sign-in in the fresh browser at `http://localhost:8000/auth/browser`.
2. Query only `/session/diagnostics` after sign-in. Use the returned categories to identify and test the smallest candidate-extraction change if the expected cookies are absent or non-numeric.
3. If capture succeeds, verify gateway authentication and persistence without printing or recording cookie values; keep Milestone 2 partial until that real flow succeeds.

## 2026-09-25 — Accept authenticated default student schedule without identity cookie

### Evidence and findings

- The user-provided screenshot shows the signed-in student schedule at the exact default route `SkemaNy.aspx`, with no query string. Redacted `/session/diagnostics` reported six Lectio cookies, both expected identity cookies missing, a numeric school ID in the page path, no student ID in the path, and no candidate available.
- The installed pinned `python-lectio` SDK requires both identity cookies during construction and uses a student ID for assignments and homework. The gateway already validates sessions by requesting and recognizing the student schedule page, and its status model already represents student ID as optional.
- The existing implementation-plan entry above assumed a student ID could be recovered from a schedule URL. The latest screenshot disproves that assumption for the default schedule route; this entry supersedes that diagnostic direction.

### Tasks completed

- Candidate capture now permits a missing student ID only when the current page is the trusted HTTPS Lectio default student schedule route (`/lectio/{numeric-school}/SkemaNy.aspx`) or its `type=elev` form without an explicit student/teacher ID. Student/teacher schedule URLs and unrelated hosts remain rejected when identity cannot be established.
- Redacted diagnostics now include `default_schedule_url` and continue to disclose only category/status values, cookie count, and candidate availability.
- The gateway session model accepts a missing student ID. Its schedule requests use the authenticated default schedule route and week selector without synthesizing an `elevid`; SDK setup restores the actual browser cookies and only adds the school identity cookie. Assignment/homework SDK calls fail with a clear adapter error when the student ID is unavailable.
- Added regression tests for default-route candidate capture, diagnostics, no-ID session restoration, schedule URL formation and validation, and explicit failures for ID-dependent resources.
- Rebuilt the auth-browser and gateway images, cancelled the previous temporary browser, recreated the gateway, and started a fresh browser flow. The start response was `STARTING_BROWSER`.

### How it went

- The new candidate test first failed because `_make_candidate()` rejected the default schedule with no student ID. After the implementation, the full repository suite passed: `54 passed in 2.50s`; Ruff selected rules passed; `docker compose --profile auth-browser config --quiet` passed; `git diff --check` passed; both updated images built successfully.
- The gateway restart was authorized by the user's prior “restart and diagnose” instruction. A new temporary browser is running, but real Lectio/MitID sign-in, gateway validation, and session persistence have not yet been observed. No cookie values or numeric school/student identifiers were retrieved, logged, copied, or recorded.
- Milestone 2 remains partial. In no-student-ID mode, schedule access is implemented; assignments and homework remain unavailable until a student ID is exposed by a supported source.

### Next steps

1. Have the user complete Lectio/MitID sign-in in the fresh temporary browser and open the default student Skema page.
2. Query only redacted `/session/diagnostics`, then verify gateway authentication and persisted-session file existence without reading session contents.
3. Keep Milestone 2 partial until the live schedule-session path validates and persists successfully; then continue Milestone 3.

## 2026-09-25 — Browser opened the student schedule directory

### Evidence and findings

- The latest user-provided screenshot shows Lectio's `FindSkema.aspx?type=elev` page titled “Vis skema for elev,” which is a student-directory/search route rather than the signed-in account's own `SkemaNy.aspx` schedule page. The screenshot contains student names; they were not transcribed or inspected.
- Redacted live diagnostics report `WAITING_FOR_USER`, six Lectio cookies, missing expected identity cookies, a numeric school ID in the current page path, no student ID in the URL, `default_schedule_url: false`, and `candidate_available: false`.
- Gateway authentication remains `WAITING_FOR_USER`; the session has not been validated or persisted.

### Tasks completed

- Compared the screenshot route with the allowlisted own-schedule route and checked only the redacted diagnostics and gateway state.
- No code changes were needed: the current behavior correctly declines to create a candidate from a student-directory page.

### How it went

- The browser appears to have reached a Lectio page, but it is the directory view rather than the account's own schedule. No student names, cookie values, or numeric identifiers were returned, copied, or written to this plan.
- Milestone 2 remains partial pending navigation to the account's own schedule, capture, validation, and persistence.

### Next steps

1. In the temporary browser, use Lectio's own `Skema` navigation to open the signed-in account's schedule; do not select a person from “Vis skema for elev.”
2. Confirm the address is the default `SkemaNy.aspx` route, then recheck redacted diagnostics for `default_schedule_url: true` and `candidate_available: true`.
3. Verify gateway authentication and session-file existence without reading session contents.

## 2026-09-25 — Live Lectio session validated and persisted

### Evidence and findings

- After the user navigated to the account's own Skema page, the gateway's `/auth/status` reported `AUTHENTICATED`.
- The gateway implementation validates a candidate with `LectioClient.validate_session()`, saves the verified session, assigns it to the active manager, and only then sets `AUTHENTICATED` (`services/lectio-gateway/src/lectio_gateway/auth/manager.py`).
- A filesystem metadata check confirmed `/var/lib/better-lectio/lectio-session.json` exists. The file was not opened or read.
- A subsequent attempt to query auth-browser diagnostics could not resolve that container's DNS name. No session values or identifiers were needed to confirm the gateway result.

### Tasks completed

- Confirmed real Lectio session validation and persistence from the gateway's authenticated state and session-file existence.
- No cookie values, session contents, school/student identifiers, or student names were retrieved, logged, copied, or recorded.

### How it went

- The default schedule flow now completes end to end: manual login, candidate capture, gateway schedule validation, private persistence, and authenticated state. This is real account integration evidence, not a fixture or mocked test.
- The authenticated session does not expose a student ID, so schedule access is supported while assignments and homework remain explicitly unavailable in this mode.
- Milestone 2's authentication objective is complete. Continue with Milestone 3: normalized gateway APIs, per-source sync/cache, and stale-data resilience.

### Next steps

1. Proceed with Milestone 3 implementation using the persisted session without displaying or logging its contents.
2. Keep assignments and homework marked unavailable until a supported source provides a student ID.

## 2026-09-25 — Clarify student ID capture sources

### Evidence and findings

- `BrowserControl` currently obtains a student ID only from a numeric `LastLoginElevId` cookie or a numeric `elevid` query on the trusted Lectio student schedule URL. The earlier redacted diagnostics showed those sources absent before the successful authentication flow.
- The gateway later reported `AUTHENTICATED`, but that check was deliberately filtered to state and session-file existence only. It did not establish whether the saved session's optional student ID is present. This entry corrects the previous entry's unverified statement that the authenticated session lacks the ID.
- Inspection of the installed `python-lectio` 1.31.0 source confirms its cookie constructor reads both `LastLoginExamno` and `LastLoginElevId`. Its PyPI documentation mentions `client.elevId` for SDK login but also identifies its docs as belonging to an older branch that may not work ([PyPI](https://pypi.org/project/python-lectio/1.31.0/)).
- The SDK's `informationer()` implementation requests `FindSkemaAdv.aspx` and iterates the page's select options. That path can enumerate other students, so it was not called or used as an identity lookup.

### Tasks completed

- Compared current candidate extraction with the pinned SDK's cookie-session requirements and documented safe versus broad lookup paths.
- Kept the actual ID and cookie values private; did not open the persisted session file or print gateway status identifiers.

### How it went

- Authentication and persistence are proven. Student-ID presence remains unknown because the prior runtime checks intentionally did not inspect that field. The directory screenshot is not evidence that the session lacks or contains an ID.
- No code changes were made in this investigation.

### Next steps

1. Add or use a redacted presence-only check for whether the persisted session has a student ID; never return the value.
2. If absent, use a new temporary login and capture only the user's own individual schedule URL if it exposes `elevid`; do not scrape the school-wide student directory.
3. Enable student-specific resources only after the ID source is captured and validated.

## 2026-09-25 — Diagnose missing Lectio identity cookies

### Evidence and findings

- The user-provided screenshot shows the signed-in Lectio navigation on a Help page, while gateway status remains `WAITING_FOR_USER`.
- Queried only the internal redacted `/session/diagnostics` route. It reports an available browser context, six Lectio-domain cookies, and both expected identity cookie names (`LastLoginExamno`, `LastLoginElevId`) as missing. No cookie names or values were returned by the route.
- `BrowserControl._make_candidate()` required those two cookies, so it could not create a candidate from this signed-in session. The screenshot's Help URL has the school ID in its `/lectio/{school}/...` path but no student ID.
- A public Lectio integration documents the student schedule URL shape as `SkemaNy.aspx?type=elev&elevid=...` ([repository](https://github.com/BjornGrylls/lectio-skema-til-.ics-kalender)). This is a third-party implementation reference; the actual account flow still needs live validation.

### Tasks completed

- Added a fallback that derives the school ID only from a numeric path segment on `https://www.lectio.dk/lectio/...`, and derives the student ID only from a numeric `elevid` query on that host's student `SkemaNy.aspx?type=elev` page. Numeric identity cookies keep precedence when present.
- Extended redacted diagnostics to report the identity format available in the current page URL and whether a candidate can be formed, without returning the URL or identifiers.
- Added tests covering the fallback and rejection of other hosts, teacher schedules, and unrelated pages.

### How it went

- The new schedule-URL test first failed because candidate extraction did not accept a page URL, then passed after the fallback was implemented.
- The complete repository suite passed: `48 passed in 2.58s`. Ruff selected rules passed, and Compose configuration parsed successfully.
- The currently running browser still uses the prior image. No live candidate, gateway validation, or persistence has been observed for the URL fallback yet. Rebuilding/restarting will end this authenticated temporary browser context; the user will need to sign in again and open the student Skema page once.
- No cookie values or student/school identifiers were retrieved, logged, copied, or written to this plan.

### Next steps

1. Build the updated auth-browser image, restart the temporary browser, and wait for manual Lectio/MitID sign-in.
2. Have the user open the student Skema page once; inspect only redacted `/session/diagnostics` and gateway auth state.
3. Keep Milestone 2 partial until candidate capture, real session validation, and persistence succeed.

## 2026-09-25 — Recheck implementation progress and live authentication state

### Evidence and findings

- Re-read the repository Markdown context and checked the current tree. `main` is at `490fcdc`; the working tree was clean at the start of this recheck.
- Since the earlier estimate, six commits landed after `e6fa1eb`, including isolated on-demand auth-browser lifecycle management, cookie diagnostics, default-schedule identity handling, and the live-authentication record.
- `docker compose ps` showed the gateway, display service, and auth-lifecycle supervisor up and healthy. A read-only request to `/health` returned `ok`; `/auth/status` returned `AUTHENTICATED`. No session file contents, cookie values, or identity values were read.
- The latest execution evidence records successful real Lectio login, candidate capture, gateway schedule-session validation, and persisted session metadata. This satisfies Milestone 2's real-account authentication gate and supersedes the earlier `WAITING_FOR_USER` diagnosis for the current runtime state.
- The authenticated account's session does not expose a student ID. The schedule path is supported, but assignments and homework remain unavailable in this mode.
- The service tree still has no normalized `/api/v1` Lectio data API/cache, Home Assistant custom integration, new display model/renderer, device API, custom firmware, or USB provisioning tool. The display service still only exposes `/health`.

### Tasks completed

- Rechecked committed history, current service tree, execution-log evidence, Compose service health, gateway health, and current gateway auth state.
- Updated the effort-weighted estimate using the previous heuristic weights: Milestones 0–2 are now counted complete (35 weighted points); partial hardening and CI foundations contribute about 2.55 more points. Revised estimate: 37.55%, rounded to about 38%, with a rough uncertainty band of 35–42%.

### How it went

- This was a status recheck. No application code changed and no test suite was run. The Compose and auth-state requests were read-only runtime checks.
- The revised estimate better reflects the completed high-risk login work. It is still a judgment-based effort model, not a measured schedule, and about 62% of modeled work remains.

### Next steps

1. Start Milestone 3: implement normalized gateway data endpoints, per-source sync/cache state, stale-data fallback, and auth-expiry behavior.
2. Preserve the current limitation explicitly: assignments/homework need a supported student-ID source before they can be made available for this account.
3. Continue with Home Assistant integration, display model/rendering, device API, firmware, USB provisioning, Tailnet operations, and the remaining hardening/CI scope.

## 2026-09-25 — Add redacted student-ID availability check

### Evidence and findings

- The new `GET /auth/diagnostics` response contains only `student_id_available: true|false`; it never serializes the identifier or session cookies and is hidden from the OpenAPI schema.
- Synthetic tests confirmed both `true` and `false` responses and verified that the synthetic student ID and cookie value are absent from the response body.
- After rebuilding and recreating the gateway, the live check returned `false` while the separately filtered auth state remained `AUTHENTICATED`. The saved session is valid but currently has no student ID. No session file contents, cookie values, or identifier values were read or printed.

### Tasks completed

- Added the redacted diagnostics route and tests for available/missing ID cases.
- Rebuilt and restarted only the gateway; its persisted session remained authenticated.

### How it went

- The full repository suite passed: `56 passed in 2.74s`; Ruff selected rules and Compose configuration passed; the gateway image built successfully; `git diff --check` passed.
- The check resolved the uncertainty without exposing the ID. Student-specific resources still require a supported source to capture the user's own ID.

### Next steps

1. If student-specific resources are needed, start a fresh temporary browser login and capture the user's own individual schedule URL only if it exposes `elevid`.
2. Do not scrape or query the school-wide student directory; preserve the redacted boolean-only diagnostic.

## 2026-09-25 — Add manual Lectio student-ID configuration

### Evidence and findings

- The user found their own student ID and asked how to configure it manually. No ID was shared with or entered by the agent.
- The gateway's Lectio client validates the supplied ID by requesting its student schedule with the stored session and recognizing the expected schedule page structure. A successful response demonstrates schedule access through that session; it does not independently establish identity ownership.
- Before changing the service, recursively read the repository Markdown context and inspected the gateway auth manager, Lectio client, session model, and existing gateway tests.

### Tasks completed

- Added a local numeric ID field to `/auth/browser` and a same-origin `POST /auth/student-id` action. The browser clears the field after submission and displays only a generic outcome.
- Added gateway validation and private session persistence. The manager saves the ID only after Lectio accepts the schedule request. Rejected IDs do not replace the stored session.
- Redacted `student_id` from `/auth/status` and auth action responses; those responses expose only `student_id_available`.
- Added synthetic tests for accepted and rejected IDs, session persistence, and absence of the identifier and cookie values from responses.
- Rebuilt and recreated only the gateway container, preserving the persisted session volume.

### How it went

- The full repository suite passed: `58 passed in 2.54s`. Ruff selected rules, Compose configuration, and `git diff --check` passed.
- The gateway image built and the recreated container is healthy. Live checks showed `AUTHENTICATED`, `student_id_available: false`, the manual entry present on `/auth/browser`, and no `student_id` property in `/auth/status`.
- Tests mocked Lectio's schedule validation. The user's actual ID has not been entered, so live validation of that ID and student-specific resources remain unverified.

### Next steps

1. The user can enter their own ID at `http://localhost:8000/auth/browser` and select **Save and check**; the page reports success without returning the number.
2. Once the boolean reports that an ID is available, validate student-specific resources against the user's Lectio session.

## 2026-09-25 — Implement Milestone 3 Lectio data API and cache

### Evidence and findings

- The restarted gateway was healthy and `/api/v1/status` reported `AUTHENTICATED` with `student_id_available: true`; the identifier itself was not returned.
- A live assignments request initially returned 502. A redacted diagnosis showed that the pinned `python-lectio` version parsed the assignment list but raised `AttributeError` on a group-assignment detail page whose expected table was absent. The list row still contained the normalized core assignment data.
- After making detail-description enrichment optional, live requests for 2026-09-25 through 2026-09-28 returned HTTP 200 and fresh (`valid`, not stale) sync state for all four sources: schedule (2 items), assignments (3), homework (0), and cancellations (0). Only counts and sync metadata were printed; no student identifiers, assignment content, URLs, or cookies were exposed.

### Tasks completed

- Added versioned normalized endpoints for schedule, assignments, homework, and cancellations, plus a redacted API status response with per-source sync metadata.
- Added a bounded, per-source/range and per-account cache with a five-minute default TTL, private persistent storage, last-good stale fallback, cache restoration after gateway restart, and per-source error status.
- Added safe handling for missing authentication, missing student ID, expired sessions, upstream failures, invalid ranges, and session changes during requests. Expired sessions remain marked expired when reauthentication is cancelled.
- Made assignment detail enrichment best-effort so a changed optional detail page does not discard the normalized assignment list; added a regression test for this live failure.
- Built and restarted only the gateway, preserving its persistent data volume.

### How it went

- CI-equivalent checks in Python 3.12 passed: `80 passed in 2.87s`; selected Ruff checks passed. Compose configuration validation, gateway image build, live health check, and redacted live requests also passed.
- A preliminary Windows Python 3.11 test run was not representative: it lacked the pinned `python-lectio` package and reported Windows-specific file-mode and fixture-encoding differences. Final test evidence is from the Python 3.12 environment used by CI.
- The live assignment-list request was verified against the authenticated Lectio session. Optional descriptions may remain empty when Lectio's detail markup is not understood; core assignment list data still succeeds.

### Next steps

1. Start Milestone 4 by implementing the Home Assistant integration that consumes the gateway's normalized API and sync status.
2. Keep device display rendering, device API/firmware, provisioning, and Tailnet work in their planned later milestones.

## 2026-09-25 — Implement Milestone 4 Home Assistant integration

### Evidence and findings

- The gateway exposes `/api/v1/status` and normalized schedule, assignments, homework, and cancellation endpoints. Status reveals only student-ID availability, not the identifier. The integration consumes this API over the local gateway URL and stores no Lectio credentials or student ID.
- Home Assistant 2026.9.3 requires Python 3.14.2 or newer; its integration tests ran under Python 3.14.7. The repository's existing services remain on their Python 3.12 test environment.
- The architecture keeps the private calendar HA-managed. This integration adds the Lectio calendar only; users can create their separate private calendar through HA's normal UI.

### Tasks completed

- Added the `better_lectio` custom integration with a URL-verifying config flow, refresh-interval options, shared-session gateway client, and per-source polling coordinator with sanitized error handling and last-good data.
- Added a date-range calendar, read-only assignment and homework to-do lists, cancellation count, auth/session state and last-sync sensors. Student IDs and raw upstream error text are not exposed.
- Added explicit allowlist-based redacted diagnostics and English config-flow translations.
- Added coordinator, API, config-flow, entity, manifest, normalization, and diagnostics-redaction tests, plus a Python 3.14/Home Assistant 2026.9.3 CI job and a dedicated `test-home-assistant` Make target.
- Fixed review findings for gateway availability propagation, options-flow compatibility, exact calendar-range errors, stale range refresh, and malformed sync/auth state values.

### How it went

- Home Assistant tests: `23 passed` with five deprecation warnings originating in Home Assistant/backoff dependencies. Ruff passed for the integration and tests.
- Existing repository tests: `80 passed`; repository Ruff checks passed. `docker compose config --quiet`, `docker compose --profile auth-browser build`, and `git diff --check` passed.
- The HA tests use mocked HTTP and real HA modules; they do not represent a live Home Assistant setup or a real gateway-to-HA session. No HA instance was available for UI setup or live entity validation.
- A broad initial pytest invocation collected the HA suite in the Python 3.12 service container and failed collection because Home Assistant is intentionally installed only in the dedicated Python 3.14 environment. The documented service test target then passed in its intended environment.

### Next steps

1. Install the custom component in the user's Home Assistant instance, configure the gateway URL, and verify calendar, to-do, sensor, and diagnostics behavior against the running gateway.
2. Keep the private calendar HA-managed and separate from the Lectio calendar.
3. Continue with the next planned display-model and rendering milestone after live HA validation.

## 2026-09-25 — Recheck after gateway API and Home Assistant work

### Evidence and findings

- Re-read repository Markdown context. At the start of this recheck, `main` was at `3fd115d` and the working tree was clean.
- Since the previous `490fcdc` snapshot, five commits added a redacted student-ID availability check, manual student-ID configuration, the resilient Lectio data API/cache, and the Home Assistant custom integration.
- The current Compose gateway, display service, and auth-lifecycle supervisor are up and healthy. Read-only `/auth/status` and `/api/v1/status` checks report authenticated session state and all four Lectio sources `valid` and not stale. No assignment, homework, calendar item content, student ID, or cookie value was requested or displayed during this check.
- The execution log records successful real Lectio requests for schedule, assignments, homework, and cancellations after the user configured the student ID. Milestone 3's API/cache checks passed (`80 passed` in the Python 3.12 service environment), including live source freshness and stale-cache coverage.
- Milestone 4's custom integration, calendar, read-only to-do entities, sensors, coordinator, config flow, diagnostics, and tests are present. Its HA suite passed (`23 passed` under Python 3.14) and lint passed. A live Home Assistant setup/entity check has not been performed, so its acceptance remains partially unverified.
- The display service still only exposes `/health`; no new display model/renderer, device API, custom firmware, or USB provisioner is present. Tailscale operations, full operational hardening, and full CI scope also remain unfinished.

### Tasks completed

- Rechecked current commits, source tree, milestone execution entries, Compose health, and redacted live gateway/source status.
- Updated the same heuristic effort model: M0–M3 complete (43 weighted points), M4 at about 85% (8.5 points; live HA validation pending), and existing M13/M14 foundations at about 30%/60% (3 points combined). Revised estimate: 54.5%, rounded to about 55%, with a rough 52–60% uncertainty range.

### How it went

- This was a read-only progress check apart from this execution-log entry. No application files changed and no tests were run in this recheck. The recorded test results above are from the recent implementation entries, not new runs here.
- The earlier 38% estimate is superseded by the gateway API/cache and HA implementation commits. The estimate remains subjective because the plan has no effort estimates.

### Next steps

1. Validate the custom integration in a live Home Assistant instance and resolve any setup/entity issues found there.
2. Proceed to Milestone 5: build the deterministic three-day display model from HA calendar, todo, and cancellation data.
3. Then implement the renderer, device API, firmware, USB provisioning, Tailnet operations, and remaining hardening/CI work.

## 2026-09-25 — Implement Milestone 5 display model

### Evidence and findings

- Read all repository Markdown and inspected the M5 contract, the existing `better_lectio` calendar/todo/sensor attributes, Compose settings, and display-service test setup before implementation.
- Home Assistant's REST service responses provide calendar `events` and to-do `items`; the existing integration also exposes per-source sync freshness on each Lectio-backed entity. The display service reads these Home Assistant entities only and does not call Lectio directly.
- Review of the first implementation pass found four edge cases confirmed with focused regressions: Python compares same-zone datetimes by wall time through the autumn repeated hour; homework descriptions were dropped from the sidebar; old homework items could consume the limited sidebar; and cached HA entity data was marked valid without consulting its `sync` status. All four were corrected.

### Tasks completed

- Added a small asynchronous Home Assistant REST client for calendar events, outstanding to-do items, cancellation sensor attributes, and Lectio entity freshness metadata. Requests use bearer authentication, bounded timeouts, reject redirects, validate entity IDs, and sanitize errors.
- Added immutable display model dataclasses and a deterministic model builder for today plus the next two Copenhagen dates. It merges Lectio and configured private calendars, preserves all-day dates, places overnight events on each affected day, and orders timed events by UTC instant across daylight-saving fall-back.
- Added fixed sidebar ordering (cancellations, assignments, homework), urgency ordering within categories, completion filtering, configurable item limits, homework relevance filtering for remaining lessons in the three-day window, and homework descriptions as subtitles.
- Added a model service that fetches HA data concurrently, preserves per-source last-good data, carries upstream `valid`/`stale`/`expired`/`error` metadata, and reports sanitized source failures independently.
- Added tests for the typed model, deterministic timeline and sidebar selection, DST boundaries, multiple private calendars, stale source fallback, authenticated REST request shapes, and sanitized client errors. Added `aiohttp` as the display-service HTTP dependency.

### How it went

- Focused regressions first failed on each newly found behavior, then passed after the fixes.
- Repository Python 3.12 suite passed: `94 passed in 3.87s`. Ruff passed. `docker compose config --quiet` passed. `docker compose build display-service` succeeded.
- The Home Assistant REST behavior is exercised with a local mocked HTTP server and HA entity data fixtures. No live Home Assistant API or physical display was available for this milestone, so external entity IDs and live rendering remain unverified.
- The display model is implemented as a reusable service layer; the existing display app still exposes only `/health`. Renderer and device API wiring remain in Milestone 6 and later.

### Next steps

1. Implement Milestone 6's renderer against the typed `DisplayModel`, preserving the single merged timeline and fixed sidebar hierarchy.
2. Wire the renderer and model service into the display-service lifecycle and device-facing API in their planned milestones.
3. Validate source entity IDs and freshness behavior against a live Home Assistant instance before claiming end-to-end integration verification.
