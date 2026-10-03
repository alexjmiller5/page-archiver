# Page Archiver

Public, generic Python application for durable web-page capture. No analytics.
Source selectors and personal URLs are runtime state, never repository content.

## Development

Use uv through just; the environment lives outside an iCloud checkout.
`just test`, `just check`, `just fmt`, `just run --help`, `just build`.
Write behavior tests before implementation. Run capture fixtures with a fresh
browser; never attach to a user's logged-in browser or forward hub credentials.
Do not classify blocked/login/partial pages as successful captures.

## Ownership

The application owns its standard state directory, SQLite queue and capture spool.
Life Data is an approved shared service, accessed only through its supported row,
subscription and file APIs with an independently revocable consumer credential.
It owns retained captures and metadata. Removal of this consumer preserves retained
archives. No backing storage bindings, platform tokens or another app's credentials.
A browser profile never receives hub auth or stored user cookies.

## Packaging and installation

The app exports a Nix package and a generic home-manager module. Installed services
run packaged code, never a working tree. Credential input is a secure-storage,
environment or command seam configured by the operator. Deployment requires an
explicit operator decision after package checks; tests and public source publication
are not a runtime deployment.

## Plans

The generic architecture is docs/design.md; the consumer contract is docs/hub-setup.md.
The runner plan is docs/superpowers/plans/2026-10-03-durable-runner.md. Hub changes
use the hub's canonical fixtures and generated contracts. Do not pretend unfinished routes
or a future Life UI viewer have shipped.
