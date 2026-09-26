# Canonical Lectio Cache, HA Freshness, and Entity Configuration

**Date:** 2026-09-26
**Status:** Approved by the user on 2026-09-26; implementation has not started.

## Goals

1. Reuse Lectio schedule pages across gateway polls and across schedule, homework, and cancellation endpoints.
2. Keep Home Assistant entities available when they hold valid last-known data, while exposing whether that data is stale.
3. Let the display service resolve Home Assistant entities by configurable semantic roles, with the current entity IDs retained as defaults.

The existing public gateway response shapes, Home Assistant entity platforms, and display behavior remain compatible by default.

## Current behavior and constraints

- `LectioDataService` keys category cache entries by owner and exact UTC start/end timestamps. Small polling-window shifts therefore miss the cache.
- `LectioClient` fetches schedule pages by ISO week, but its homework and cancellation methods independently request schedule data. The gateway's separate endpoint operations do not share schedule pages.
- The HA coordinator retains prior category items after failures and tracks per-source sync state. Entity availability currently follows the latest coordinator update result, hiding that retained data after a failed poll.
- The display model has default entity IDs in code. It accepts multiple private calendar IDs through constructor injection, but other roles are fixed and deployment configuration does not expose these IDs.
- HA remains the display service's source of truth. The display service must not connect directly to Lectio.
- Preserve last-known-good data and the existing uncommitted display diagnostics work in the shared checkout.

## Design

### 1. Canonical schedule weeks and shared fetching

Add a shared, owner-scoped schedule-week store to the gateway data service. Each schedule page is cached using the Lectio owner identity plus ISO year and week. Week calculations and date boundaries use `Europe/Copenhagen`, matching school-calendar semantics. Each week has its own TTL and in-flight lock, so overlapping requests reuse data and a failed refresh can fall back to that week's last-known-good page.

The Lectio adapter exposes a single-week schedule fetch that returns normalized lessons for that week. The gateway resolves the weeks intersecting an incoming range once, then composes the endpoint result from those shared pages:

- Schedule returns the selected lessons filtered back to the caller's original half-open `[start, end)` interval. Its public response shape is unchanged.
- Assignments and homework use day-aligned ranges for gateway cache identity and upstream range fetching. For both, the start is floored to local midnight; the exclusive end is ceiled to the next local midnight when it is not already aligned. Homework is normalized against the shared schedule lessons.
- Cancellations are derived from the same shared schedule lessons and inherit the schedule data's freshness status. They do not trigger a second schedule fetch.

Homework and schedule can request different portions of a week, but a week page is the reusable primitive. Assignments and homework remain independently cached by owner and canonical local-day range.

If one or more required week refreshes fail, use that week’s last-known-good page and mark the composed result stale. If some weeks have usable data and other weeks have never been fetched successfully, return the usable lessons with stale sync metadata and identify the failed/incomplete refresh in the sync status. Return an error only when no usable schedule data exists for the requested interval. A successful empty week is usable data and must be distinguishable from a missing/failed week.

Existing persisted exact-range schedule entries may be read as a temporary fallback where their coverage can be established, but must not be promoted as a complete weekly page unless they cover the whole week. New writes use the canonical format. Cache schema/version migration must avoid interpreting partial-range records as full weeks.

### 2. Available with stale Home Assistant data

Availability is source-specific and based on whether that source has ever had a successful response, not whether the most recent coordinator poll succeeded:

- A category entity (calendar, todo, or cancellation sensor) is available after its category has succeeded at least once. A valid empty response counts as success.
- The session/gateway status entity is available after gateway status has been fetched successfully at least once.
- The last-sync entity is available after at least one data category has succeeded at least once.
- Before that source's first successful response, the corresponding entity remains unavailable.

On later poll failures, keep its last successful items and successful-sync timestamp. Mark its sync state stale or expired using the existing sync metadata conventions, and retain the current error/last-attempt diagnostics. A failure for one category does not make unrelated successful categories unavailable. Reconnection and a successful response replace the retained category data and restore fresh/valid sync status.

The availability properties must not be based solely on `coordinator.data is not None`: the coordinator's container may exist before that particular source has succeeded, and empty success must count as usable history. Tests will cover these cases and the existing failed-poll regression.

### 3. Configurable display entity roles

Introduce a typed entity configuration object for the display model with these defaults:

| Semantic role | Environment override | Default |
| --- | --- | --- |
| Lectio calendar | `HA_LECTIO_CALENDAR` | `calendar.lectio` |
| Private calendars | `HA_PRIVATE_CALENDARS` | `calendar.private` |
| Assignments todo | `HA_ASSIGNMENTS_TODO` | `todo.lectio_assignments` |
| Homework todo | `HA_HOMEWORK_TODO` | `todo.lectio_homework` |
| Cancellations sensor | `HA_CANCELLATIONS_SENSOR` | `sensor.lectio_cancellations` |

`HA_PRIVATE_CALENDARS` is a comma-separated list, allowing zero, one, or several calendars. An empty value disables private calendars; whitespace around IDs is trimmed. The configuration is loaded once at display-service startup and passed into `DisplayModelService`, rather than accessed as module-level protocol constants.

Validate each configured ID against its required HA domain and the existing entity-ID syntax before polling. Reject invalid values and duplicate IDs with a clear startup error. Preserve semantic source names in the display model so renderers do not treat a literal HA entity ID as a role. `HomeAssistantClient` continues to receive the resolved IDs for state/event lookups.

Add the defaults and overrides to `.env.example` and pass them through the display-service environment in Compose. Existing deployments with no overrides retain current behavior.

## Compatibility and operational behavior

- Gateway route paths and response envelopes remain unchanged; sync metadata may more explicitly report stale or incomplete composed results using the existing fields.
- Existing persistent cache files must either migrate safely or be ignored by the new weekly cache reader. No partial interval is treated as a complete week.
- All new cache records remain private-owner scoped and retain existing file-permission protections.
- Default HA entity IDs preserve current display behavior. Configuration validation happens at startup so a typo is reported before an empty display model is mistaken for valid data.
- No changes are made to the architecture boundary: Lectio access remains in the gateway and display reads only HA.

## Acceptance criteria

1. Schedule requests with different minutes but intersecting the same ISO weeks reuse the same week fetches; a week is fetched at most once concurrently per owner.
2. Overlapping schedule, homework, and cancellation requests share the same schedule pages. Assignment and homework ranges canonicalize to local day boundaries.
3. A failed refresh uses per-week last-known-good data and reports stale status; a successful empty week is not confused with failure; no usable pages yields an error.
4. Schedule results still honor the original half-open caller range, including week boundaries and daylight-saving transitions in Copenhagen.
5. HA category entities remain available after a failed poll if that source previously succeeded, including a successful empty result; entities without prior success remain unavailable. Source failures remain isolated.
6. The display service accepts custom entity IDs and several private calendar IDs, validates roles/domains, uses current IDs by default, and keeps model/rendering roles semantic.
7. Tests cover canonicalization, sharing, fallback, availability, configuration parsing/validation, and unchanged public API contracts.

## Validation scope

Run focused gateway, Home Assistant integration, display-service, and Compose configuration tests. Run the repository CI-relevant checks available for the changed Python and configuration scopes, then inspect the complete diff and `git diff --check`. Do not claim live Lectio, live Home Assistant, or container runtime validation unless those paths are actually exercised.

## Implementation decisions

- Persist weekly schedule pages in cache format version 2. Continue reading version 1 exact-range records as last-known-good fallback only when a record covers the complete local ISO week; never use a partial record as a complete page. Preserve private owner scoping and existing file protections.
- A composed schedule is `valid` only when every required week is usable and fresh. If any required week is stale or has no prior page but other weeks are usable, return the usable lessons as `stale`; return an error only when no week has usable data. For composed metadata, use the newest `last_attempt_at` and the oldest available successful-page timestamp as `last_successful_sync`.
- Parse environment values through a pure typed entity-config factory and inject the resulting configuration into `DisplayModelService`; do not read process environment from the model service.
