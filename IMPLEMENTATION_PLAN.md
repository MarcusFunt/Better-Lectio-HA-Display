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
- `docker` and `docker-compose` are unavailable in the execution environment. Compose YAML and intended service/volume/port boundaries are covered by Python tests; actual `docker compose config` and image builds are delegated to the new GitHub Actions workflow and must be confirmed there.

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
- `git diff --check` passed. The new tests parse the Compose YAML and check that services remain unpublished, but they do not replace Docker Compose's own parser/build; those checks remain pending CI.
- No live Lectio/MitID login, Home Assistant connection, Tailnet route, firmware build/flash, or physical display test was run. These behaviors are not part of Milestone 0.
- **Ruling:** keep `AGENTS.md`, `ARCHITECTURE_AND_OPERATIONS.md`, and `IMPLEMENTATION_PLAN.md` at the repository root, although Milestone 0's approximate tree suggests moving architecture documents into `docs/`. `AGENTS.md` requires reading and updating those canonical root paths, and the earlier user instruction made `IMPLEMENTATION_PLAN.md` the single progress log. The `docs/` directory is created for later supporting documents. Cost if wrong: the repository differs from the approximate layout until the agent contract is deliberately revised.
- **Ruling:** use `IMPLEMENTATION_PLAN.md` as the execution ledger rather than creating the separate SDD `progress.md`; `AGENTS.md` and the earlier user instruction require one canonical progress document. Cost if wrong: the SDD helper scripts cannot independently resume this milestone, but the repository's mandated evidence log remains complete.
- Milestone 0 is implemented locally but is not yet recorded as complete because the actual Compose parser and image builds must pass in CI.
- Final review: self-review (no subagent tool). The full staged diff was reviewed against Milestone 0 and `AGENTS.md`; no Critical or Important issues were found. Docker Compose parsing and image builds remain a CI gate.

### Next steps

1. Open a PR for this scaffold and verify that CI's Compose validation and image builds pass; fix any failures before marking Milestone 0 complete.
2. Proceed to Milestone 1: define the normalized Lectio domain models and adapter boundary, pin the selected `python-lectio` source, and add fixture-driven tests before implementing the Phase-1 browser flow.
3. Keep the first real MitID/Lectio session test explicitly pending until it is run against the user's account.
