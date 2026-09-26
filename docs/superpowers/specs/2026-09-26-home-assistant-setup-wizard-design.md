# Home Assistant Setup Wizard on the Lectio Login Page

**Date:** 2026-09-26  
**Status:** Approved by the user on 2026-09-26
**Requested outcome:** Configure the largest practical share of Home Assistant setup from the existing Lectio login page.

## Goals

1. Keep Lectio sign-in and display/Home Assistant configuration together on the existing `/auth/browser` page.
2. Remove the routine need to edit `.env` for Home Assistant URL, long-lived access token, semantic entity IDs, or the scoped Lectio API token.
3. Keep Home Assistant as the owner of HACS installation, integration entries, and private calendar management.
4. Preserve loopback-only administration defaults, the read-only gateway API boundary, and last-known-good display behavior.

## Current behavior and constraints

- The gateway serves the Lectio sign-in and diagnostics page at `/auth/browser` on Compose host port `8000`, bound to loopback by default.
- A separate `lectio-ha-api` service publishes the scoped, read-only Lectio API on host port `8002`. Its bearer token currently comes from `LECTIO_HA_API_TOKEN` in `.env`.
- The display service reads `HOME_ASSISTANT_URL`, `HOME_ASSISTANT_TOKEN`, and five semantic entity roles from Compose environment variables. It loads those settings at process startup.
- Gateway, API, and display processes are separate containers. The display service needs the raw HA token to make authenticated requests; the gateway should not share its Lectio session volume with the display service.
- HACS can install the custom integration, but creating its HA config entry and creating private calendars belong in Home Assistant's own UI. The project must not ask for a Home Assistant administrator token or silently call HA's administrative APIs.
- Docker host port bindings and the host firewall are configured outside the gateway process. A web page in the container cannot safely rebind the published HA API port at runtime.

## Design

### 1. One setup surface

Extend the existing `/auth/browser` page with a clearly separated **Home Assistant setup** section alongside Lectio sign-in, student-ID setup, source diagnostics, and the bitmap preview. Keep the existing page URL and authentication actions. The section manages:

- the Home Assistant base URL used by the display service;
- the Home Assistant long-lived access token used by the display service;
- the semantic IDs for the Lectio calendar, private calendars, assignments to-do, homework to-do, and cancellation sensor;
- the gateway API URL that Home Assistant can reach, for the HACS config flow;
- creation and rotation of the scoped bearer token used by Home Assistant's Better Lectio integration.

The page also gives a short, ordered HACS/config-flow handoff with copy-ready API URL and token. The raw scoped token is shown only immediately after generation or rotation. The HA long-lived token is write-only after save: the UI reports configured/unconfigured and safe connectivity states, never the token value. Its field is blank on page load; submitting it blank keeps the current token, and an explicit disconnect action clears managed HA settings.

When the Compose host is bound to a private LAN address, use that address and configured host port as the suggested API URL. When it is loopback-bound or uses a wildcard address, clearly explain the limitation and let the operator enter the hostname/IP that HA will use after configuring a matching private bind in `.env`.

### 2. Configuration storage and service access

Use two dedicated named volumes, separate from the Lectio session volume and display image/device data:

| Volume | Contents | Gateway | Display service | `lectio-ha-api` | Diagnostics sidecar |
| --- | --- | --- | --- | --- | --- |
| HA display config | HA base URL, HA long-lived token, entity IDs | read/write | read-only | no mount | no mount |
| HA API auth | SHA-256 digest of scoped API token only | read/write | no mount | read-only | no mount |
| Gateway setup metadata | HA-reachable gateway API URL, no secrets | read/write | no mount | no mount | no mount |

The gateway and display containers already use UID/GID `10001`; files and directories use restrictive permissions, atomic replacement, and that shared service identity. The HA-reachable API URL is saved as non-secret metadata in the existing gateway data volume. The display service reloads the managed config on its next refresh cycle, validates the URL/entity roles, replaces its HA client when settings change, and closes the prior client. A bad new configuration marks setup status invalid and preserves the last rendered bitmap.

For upgrade compatibility, managed files take precedence over the existing environment settings. If no managed file exists, the display service and API retain their current environment-based behavior and the login page identifies that setup is still environment-managed. Saving through the page creates the managed file. Once the scoped-token digest file exists, it is authoritative; old environment credentials are no longer accepted.

### 3. Secret handling and authorization

- Generate scoped API tokens with a cryptographically secure random source and sufficient entropy. Store only a SHA-256 digest in the API-auth volume; compare digests with constant-time comparison. Reveal the raw token once in the successful create/rotate response and do not log or persist it.
- Rotating the scoped token immediately invalidates the old token. The page tells the user to update the existing Better Lectio integration entry in Home Assistant with the newly displayed token. The page does not change HA config entries.
- Store the HA long-lived token as plaintext only in the dedicated display-config volume because the display service must use it. The gateway can read it to write/update settings, which expands the gateway/login service's trusted-secret boundary. The display service receives the file read-only; the API proxy and diagnostics sidecar cannot read it.
- Accept setup mutations only through same-origin-checked POST routes on the existing loopback/Tailnet administration surface. Do not expose setup file access, tokens, or entity details through `/auth/diagnostics` or any device API.
- Validate the HA URL as an HTTP(S) base URL without embedded credentials, query, or fragment. Preserve authenticated request redirect rejection to avoid forwarding the bearer token to another host. Validate entity IDs with the existing typed role config before saving.
- Never place either token in logs, error details, diagnostics, HTML after the one-time scoped-token response, or test fixtures that resemble real credentials.

### 4. Home Assistant handoff and host-only settings

The login page guides the user through:

1. Add the repository to HACS as a custom **Integration**, install Better Lectio, and restart Home Assistant.
2. Add the Better Lectio integration in Home Assistant with the copy-ready gateway API URL and newly generated scoped token.
3. Save Home Assistant's URL, long-lived token, and entity IDs in the login-page setup section; use its status to confirm display-service connectivity and entity reads.
4. Create private calendars through Home Assistant's calendar UI.

The gateway UI remains on `127.0.0.1:8000` by default. When HA is on another host, the operator still sets `LECTIO_HA_API_BIND_ADDRESS`, `LECTIO_HA_API_HOST_PORT`, and a matching host firewall rule on the Compose host. The wizard suggests the configured listener address when it is directly usable and lets the user enter the URL HA will use when host naming differs. It explains the loopback limitation; it does not edit the host's `.env`, change Docker's host port mapping, configure a firewall, install HACS, or administer HA.

### 5. Failure and migration behavior

- An absent managed config is reported as not configured or environment-managed; the current environment-based setup continues to work until migrated.
- Invalid URL/entity configuration and rejected/unreachable HA credentials produce distinct safe setup states and never expose the submitted value.
- Config changes apply within one display refresh interval (30 seconds by default). Until a new valid render is available, continue serving the last valid bitmap.
- Token rotation does not weaken the API allowlist: only bearer-authenticated GETs for status, schedule, assignments, homework, and cancellations are forwarded.
- An empty private-calendar setting continues to disable that role; existing semantic defaults remain unchanged.

## Out of scope

- Installing HACS or restarting Home Assistant through an HA administrator API.
- Creating/editing private HA calendars or changing Lectio source data from this gateway.
- Changing Compose host bindings, host firewall rules, router settings, Tailscale Serve, or public networking from the web process.
- Replacing the existing Lectio sign-in/session flow, display model, renderer, or device API.
- Exposing the HA token in diagnostics or allowing it to be read back after save.

## Acceptance criteria

1. `/auth/browser` provides a single coherent place to enter display HA URL/token/entity roles, set the copy-ready gateway API URL for HA, and inspect the existing safe connection diagnostics.
2. Managed display settings persist across container restarts, take precedence over environment values, and reach the display service through a read-only, dedicated volume without sharing the Lectio-session volume.
3. The display service notices valid or invalid config updates within one refresh interval, validates roles, reports safe state, and preserves its last good image across configuration or HA failures.
4. The scoped API token can be generated/rotated from the same page, is returned only once, is persisted only as a digest, and authenticates only the existing GET allowlist. Rotation rejects the prior token.
5. Existing environment-based deployments continue to work until the user saves managed settings; once managed API auth exists, stale environment credentials cannot bypass token rotation.
6. The setup page gives accurate HACS and HA config-flow steps, clearly identifies which actions still happen in HA, and explains the `.env`/firewall requirement when HA is on another host.
7. Tests verify permission/storage boundaries, restart persistence, environment fallback and managed precedence, dynamic reload, blank-token preservation and explicit disconnect, one-time secret response/redaction, rotation, CSRF-origin checks, URL/entity validation, setup-state reporting, and unchanged API allowlisting.
8. No live HA, HACS, Tailnet, firewall, Lectio, or physical display verification is claimed unless that path is actually exercised.

## Validation scope

Run focused gateway setup/API tests, display-service config/reload tests, Compose and environment-template checks, then the repository and Home Assistant suites, lint, HACS validation, Compose parsing, and relevant image builds. Review the complete diff for secret leakage and ensure the two new volumes are mounted only for their intended service roles. Local tests and image builds do not establish live Home Assistant or HACS behavior.

## Design self-review

- The UI ownership matches the request without crossing into HA administration: the login page stores display settings and prepares the read-only API credential, while HA remains responsible for HACS/config entries and calendars.
- Secret boundaries are explicit and avoid sharing the Lectio session volume. Separate volumes keep the display token away from the API proxy and diagnostics sidecar; only its one-way digest reaches the API proxy.
- Host network binding is explicitly left outside the web service, consistent with loopback/Tailscale architecture and Docker port-publishing behavior.
- Compatibility precedence is defined: environment configuration remains active until a managed file is saved; after that, the managed file is authoritative. Token hash-file presence similarly closes the legacy environment-token path.
- Error handling preserves the existing bitmap and reveals only allowlisted setup states.
- Scope is one setup workflow across the existing gateway, scoped API, display runtime, Compose wiring, and tests. No placeholder decisions or live-integration claims remain.
