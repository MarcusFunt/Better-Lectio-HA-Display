# Better Lectio HA Display — Architecture and Operational Decisions

**Status:** Architectural baseline  
**Date:** 2026-09-25  
**Repository:** `MarcusFunt/Better-Lectio-HA-Display`

This document records the architectural and operational decisions for the Better Lectio HA Display project. It is intended to be the source of truth for future implementation work unless a later decision explicitly supersedes it.

---

## 1. Project goal

The system is a permanently powered, local e-paper schedule display that combines:

- Lectio timetable data
- private calendar events managed through Home Assistant
- Lectio cancellations
- Lectio assignments
- Lectio homework

The displayed UI is a single chronological calendar spanning:

1. today
2. tomorrow
3. the day after tomorrow

A narrow right-hand sidebar shows prioritized Lectio information.

The display hardware runs custom firmware rather than the stock TRMNL firmware.

---

## 2. High-level architecture

The system is divided into three main application layers plus the display firmware layer.

```text
Lectio / MitID
      │
      ▼
┌─────────────────────────────┐
│ 1. Lectio Gateway           │
│                             │
│ - interactive login         │
│ - session persistence       │
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
│ - fetch HA state/events     │
│ - normalize display model   │
│ - prioritize sidebar        │
│ - render 800×480 1-bit BMP  │
│ - serve device API          │
└──────────────┬──────────────┘
               │ LAN
               ▼
┌─────────────────────────────┐
│ 3.5 Custom Display Firmware │
│                             │
│ - USB provisioned           │
│ - Wi-Fi                     │
│ - authenticated fetch       │
│ - image update              │
│ - always powered            │
└─────────────────────────────┘
```

---

## 3. Layer 1 — Lectio Gateway

### 3.1 Responsibility

The Lectio Gateway owns all Lectio-specific behavior.

It is responsible for:

- interactive authentication
- authenticated Lectio session persistence
- validating and refreshing session state where possible
- calling Lectio
- parsing Lectio data
- normalizing Lectio-specific data
- exposing a stable local API to Home Assistant
- detecting authentication failure and requesting re-login

The renderer must not contain Lectio scraping or authentication logic.

### 3.2 python-lectio decision

Use the `python-lectio` code lineage as the Lectio protocol/data-access foundation.

However, the rest of the application must not directly depend on `lectio.sdk`.

Instead, create an internal adapter such as:

```python
class LectioClient:
    async def get_schedule(...): ...
    async def get_assignments(...): ...
    async def get_homework(...): ...
    async def get_cancellations(...): ...
```

Reasons:

- BetterLectio uses the same `python-lectio` lineage for its Lectio backend/data access.
- BetterLectio currently vendors/forks that code rather than relying blindly on an untouched upstream package.
- Lectio is an unstable HTML/web integration target and may require small compatibility patches.

Therefore:

- start from upstream `python-lectio`
- pin the version/commit
- keep it behind the adapter
- permit a small maintained fork if necessary
- do not leak its raw object/data model into Home Assistant or the display service

### 3.3 Authentication is separate from python-lectio

`python-lectio` is used for authenticated Lectio data access.

It is **not** considered the owner of the MitID authentication experience.

The authentication subsystem must produce an authenticated Lectio session that can then be injected into the `python-lectio` adapter.

The common output should be conceptually equivalent to:

```python
class AuthenticatedLectioSession:
    school_id: str
    student_id: str
    cookies: list
    created_at: datetime
    last_verified_at: datetime
```

The rest of the Lectio Gateway should not care how that session was obtained.

---

## 4. MitID authentication strategy

Authentication will be implemented in three phases.

### Phase 1 — Playwright Chromium reference implementation

Use a temporary real Chromium browser controlled by Playwright.

Flow:

```text
authentication required
        ↓
start temporary Chromium
        ↓
open Lectio login
        ↓
user performs Lectio/MitID login manually
        ↓
wait for authenticated Lectio state
        ↓
capture resulting Lectio cookies/session
        ↓
validate with python-lectio
        ↓
persist authenticated session
        ↓
terminate Chromium
```

Key decisions:

- MitID itself is not automated.
- The user manually approves/authenticates.
- Chromium is temporary.
- Browser state is captured only after successful Lectio authentication.
- The resulting Lectio cookies/session are persisted for normal operation.
- Playwright is a reference implementation and recovery path, not part of ordinary data fetching.

### Phase 2 — inspect the actual authentication/session flow

Once Phase 1 works reliably:

- inspect redirects
- inspect cookie creation
- inspect state transitions
- determine exactly where Lectio considers the session authenticated
- identify the minimal required cookies/session state
- document the flow
- create regression fixtures around session import and validation

The goal is understanding, not automating MitID.

### Phase 3 — streamlined browser redirect flow

If technically feasible, replace the remote browser UI with a clean login route such as:

```text
/auth/login
```

The user's normal browser performs the redirects and returns to the gateway.

The gateway then stores the same `AuthenticatedLectioSession` abstraction.

The Playwright implementation remains available as a fallback, for example:

```text
/auth/browser-login
```

This gives the system a recovery path if Lectio changes its authentication flow.

---

## 5. Browser service isolation

The Playwright browser must not run permanently inside the normal Lectio Gateway process.

Use a separate on-demand service/container.

Suggested Compose-level services:

```text
lectio-gateway
lectio-auth-browser
lectio-auth-view
```

The browser service should be started only when authentication is required and stopped when authentication succeeds or is cancelled.

Reasons:

- Chromium crashes must not take down the gateway.
- Normal operation should remain lightweight.
- Authentication dependencies should remain isolated.
- The login subsystem can later be replaced without disturbing data access.

---

## 6. Layer 2 — Home Assistant

Home Assistant is the mandatory middle layer and the application-level source of truth consumed by the display renderer.

The renderer must not bypass Home Assistant for normal displayed data.

### 6.1 Lectio calendar

Expose Lectio schedule as a Home Assistant calendar entity:

```text
calendar.lectio
```

Each lesson should expose at least:

- start
- end
- subject
- teacher
- room
- stable source identifier where available

Conceptually:

```text
summary: Physics
start: 08:15
end: 09:50
location: F112
description: Teacher: Nielsen
```

### 6.2 Private calendar

Private events are created through Home Assistant's normal calendar UI.

A normal HA calendar entity, for example:

```text
calendar.private
```

is used for user-created private events.

The project must not create a separate calendar-management UI.

### 6.3 Assignments

Expose Lectio assignments as a read-only Home Assistant todo entity:

```text
todo.lectio_assignments
```

Each item should preserve as much useful structure as available:

- title
- description
- due date/datetime
- subject/course
- source identifier
- link where useful
- completion/submission state if it can be represented safely

The initial integration is read-only.

Marking an HA item complete must not submit or mutate Lectio unless that behavior is explicitly designed later.

### 6.4 Homework

Expose Lectio homework as a read-only Home Assistant todo entity:

```text
todo.lectio_homework
```

Typical item fields:

- lesson/subject
- homework description
- target lesson date/time
- source identifier
- Lectio link where useful

### 6.5 Cancellations

Expose cancellation state through Home Assistant sensors.

At minimum:

```text
sensor.lectio_cancellations
```

Suggested sensor state:

```text
number of currently relevant cancellations
```

Structured cancellation records should be available in attributes or another stable structured representation.

Additional useful entities:

```text
sensor.lectio_session_status
sensor.lectio_last_sync
```

A Home Assistant event entity for newly detected cancellations may be added later for automation/notification use.

---

## 7. Layer 3 — Display service

The display service is deliberately dumb with respect to Lectio.

It consumes Home Assistant entities/API data and transforms them into a display model.

Responsibilities:

- retrieve calendar events
- retrieve assignment todo items
- retrieve homework todo items
- retrieve cancellation sensor data
- normalize the data
- select relevant items
- apply sidebar priority
- lay out the UI
- render the bitmap
- serve the current image/device metadata
- authenticate display devices

It must not:

- log into Lectio
- scrape Lectio
- perform MitID flows
- know python-lectio details
- own Home Assistant calendar semantics

---

## 8. Display UI

### 8.1 Main schedule

The main schedule is one chronological view.

Do **not** split Lectio and private calendar events into separate columns.

The visible date range is:

1. today
2. tomorrow
3. day after tomorrow

Lectio lessons and private HA calendar events are merged chronologically.

### 8.2 Lesson fields

Each Lectio lesson block should display:

- subject
- teacher
- room

Time must also be visible as part of the chronological schedule.

Private events may have fewer fields and are rendered from the metadata available in Home Assistant.

### 8.3 Right sidebar

Reserve a narrow right-side strip/sliver for Lectio attention items.

The schedule should occupy roughly 80–85% of the width and the sidebar roughly 15–20%, subject to visual iteration.

The sidebar priority is fixed as:

```text
1. cancellations
2. assignments
3. homework
```

Within categories, sort by urgency/relevance.

Recommended policy:

```text
Cancellations:
- today
- tomorrow
- day after tomorrow

Assignments:
- overdue
- due today
- due tomorrow
- due day after tomorrow
- later

Homework:
- next lesson
- today
- tomorrow
- day after tomorrow
```

The exact item count should be configurable.

### 8.4 Output

Target output remains:

```text
800 × 480
1-bit monochrome bitmap
```

Content-addressed images remain desirable to avoid unnecessary device refreshes and to make "did the display change?" trivial.

---

## 9. Layer 3.5 — custom firmware

The device will not use stock TRMNL firmware as the final solution.

Use the upstream/open TRMNL firmware only as a hardware/driver reference or starting fork where useful.

The goal is project-owned firmware with a much simpler behavior model.

### 9.1 Device assumptions

The display:

- is local-only
- is always plugged in
- does not need battery optimization
- does not need deep sleep for power savings
- does not need Tailscale on-device
- can remain connected to Wi-Fi
- can check for updates frequently

### 9.2 Normal firmware loop

Conceptually:

```text
boot
 ↓
load provisioned config
 ↓
connect Wi-Fi
 ↓
authenticate to local display service
 ↓
check current content hash/version
 ↓
changed?
 ├─ no  → wait / retry later
 └─ yes
      ↓
   download image
      ↓
   validate
      ↓
   update e-paper
      ↓
   continue running
```

Because the display is permanently powered, update checks may happen much more frequently than the earlier battery-oriented design.

A starting poll interval in the tens-of-seconds range is acceptable.

A push/event mechanism may later replace polling if it materially improves behavior without adding fragility.

---

## 10. Device provisioning

Provisioning is USB-only.

Do not create an on-device captive portal for primary provisioning.

Build and provisioning are separate operations.

### 10.1 Firmware build

One reproducible generic firmware binary should be buildable without embedding per-device secrets.

Example workflows:

```text
make firmware
make flash PORT=/dev/ttyACM0
make provision PORT=/dev/ttyACM0
```

### 10.2 Provisioning flow

The provisioning tool should:

1. identify/connect to the device
2. optionally flash the current firmware
3. generate a device ID
4. generate a cryptographically secure per-device credential
5. register the device with the display service
6. write Wi-Fi configuration
7. write the device credential
8. reboot the device
9. verify that the device can authenticate
10. report success/failure clearly

### 10.3 Credential rule

Do **not** bake generated device passwords/tokens into firmware binaries.

Use a generic firmware build plus per-device runtime provisioning.

A strong random bearer credential over the local LAN is sufficient initially.

The design may later migrate to per-device asymmetric keys without changing the higher-level API.

---

## 11. Networking and remote access

### 11.1 Display network

The physical display is LAN-only.

It talks directly to the local display service over the LAN.

It does not need:

- public Internet exposure
- Tailscale
- remote roaming support
- DERP/WireGuard logic in firmware

### 11.2 Human/admin access

Remote access to management, authentication and configuration interfaces must use the user's Tailscale Tailnet.

Preferred pattern:

```text
remote user
   │
Tailscale
   │
Tailscale Serve / Tailnet ACLs
   │
localhost-bound service
   │
Docker
```

Do not expose admin/login ports directly to the public Internet.

No Tailscale Funnel is required for admin/auth paths.

### 11.3 Service binding

Where practical:

- bind internal admin services to localhost or private Docker networks
- expose them through Tailscale Serve
- use Tailnet identity/ACLs as the outer access boundary
- do not trust spoofable identity headers from arbitrary LAN clients

---

## 12. Deployment model

The project no longer targets a Raspberry Pi Zero or systemd-first deployment.

The target is a desktop/server host running Docker.

Use Docker Compose.

Suggested logical services:

```text
lectio-gateway
lectio-auth-browser
lectio-auth-view
display-service
home-assistant integration code
```

Home Assistant itself does not need to be owned by this repository unless the deployment specifically chooses to run HA in the same Compose project.

The default assumption is that this repository integrates with an existing Home Assistant instance.

---

## 13. Persistence

Persist at least:

### Lectio Gateway

- authenticated Lectio session/cookies
- school/student identifiers
- session validation timestamps
- source cache
- last known good Lectio data

### Display service

- device registry
- device credentials
- current display content hash
- current rendered image
- retained recent content-addressed images if useful
- last successful HA sync/render status

### Operational rule

A temporary source failure should not blank the display.

Keep last-known-good data and surface staleness in status/diagnostics.

---

## 14. Failure behavior

The system should degrade per source rather than fail all-or-nothing.

Examples:

- Lectio temporarily fails → retain last-known-good Lectio data
- private HA calendar still works → continue updating it
- assignments fail → schedule still renders
- cancellation sensor unavailable → omit/update sidebar appropriately
- renderer fails → keep serving previous valid bitmap
- login expires → continue serving stale Lectio-derived data while clearly reporting that reauthentication is required

No single upstream failure should erase otherwise valid information.

---

## 15. Observability

Each service should expose health/status data.

Minimum useful states:

### Lectio Gateway

- authenticated / login required
- last successful Lectio request
- last successful schedule sync
- last successful assignments sync
- last successful homework sync
- last successful cancellations sync
- current school/student
- browser auth active/inactive

### Home Assistant integration

- entity availability
- last update
- source error where relevant

### Display service

- HA connectivity
- last successful model build
- last render
- current content hash
- currently registered devices
- last device fetch

Operational logs must avoid leaking:

- MitID data
- Lectio passwords
- session cookies
- device secrets
- HA long-lived access tokens

---

## 16. Security decisions

- MitID authentication remains manual.
- Lectio browser sessions are sensitive secrets.
- Device credentials are generated with a cryptographically secure RNG.
- Device credentials are provisioned over USB.
- Device credentials are not embedded at compile time.
- Human remote access uses Tailscale.
- Admin/auth services are not publicly exposed.
- Secrets must not be committed to Git.
- Logs must redact credentials/tokens/cookies.
- The display itself is trusted only through its per-device credential, not merely by MAC address.

---

## 17. Explicitly rejected/retired design decisions

The following earlier design choices are superseded:

- Raspberry Pi Zero deployment
- systemd as the primary deployment model
- renderer directly logging into Lectio
- one raw ICS URL as the primary private-calendar source
- separate Lectio/calendar columns
- battery/deep-sleep-first firmware behavior
- stock TRMNL firmware as the intended final firmware
- MAC-only enrollment/authentication
- generating per-device secrets during firmware compilation

---

## 18. Decisions considered locked enough for implementation

The following are considered implementation baselines:

- Docker Compose deployment
- dedicated Lectio Gateway
- python-lectio lineage behind an adapter
- Playwright Chromium Phase-1 authentication
- Phase-2 auth-flow inspection
- Phase-3 streamlined login if feasible
- Playwright fallback retained
- Home Assistant as mandatory middle layer
- Lectio schedule exposed as HA calendar
- assignments exposed as HA todo
- homework exposed as HA todo
- cancellations exposed through HA sensor data
- private events created through normal HA calendar UI
- renderer consumes Home Assistant rather than Lectio directly
- chronological today/tomorrow/day+2 layout
- subject + teacher + room for lesson blocks
- sidebar priority: cancellations > assignments > homework
- custom firmware
- USB-only device provisioning
- generic firmware binary with runtime per-device credentials
- LAN-only display connectivity
- Tailscale for remote human/admin access
- mains-powered operation; battery optimization is irrelevant

Future changes should update this document rather than silently diverge from it.