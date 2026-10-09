# Page Archiver design

Page Archiver retains self-contained HTML and full-page PNG observations of public HTTP(S) pages. It is a generic consumer of a Soma hub, using only the hub URL and its own restricted credential. Source mappings are operator state, never compiled application defaults. There is no analytics integration or public listener.

The hub records exact selected old/new values atomically with accepted mutations. An outbound authenticated request holds for at most 30 seconds, returning a durable offered batch. The client persists every event, capture job and pending ACK receipt in a SQLite transaction before acknowledging the batch. Event plus source column identifies logical capture work. Lost responses and restarts must not lose or duplicate completed work.

The capture browser has a fresh profile, no hub credential and no reused login state. Public-network-only egress applies to navigation, redirects and subresources. Capture is bounded by elapsed time, bytes, viewport/page dimensions and per-host concurrency. Login walls, CAPTCHA and incomplete page bodies are explicit failures. Opt-in readable partial captures retain verified artifacts and warnings under a distinct status; they are never complete successes. Optional bounded PNG downscaling preserves the full page and unchanged HTML. Retained HTML is hostile data; retrieval must not execute it on the application origin.

Successful attempts publish immutable HTML/PNG files with MIME, byte count and SHA-256 through the hub file API, then terminal metadata. A retry of the same staged bytes reuses the attempt ID. Failed attempts have no successful artifact keys. Operational queue state stays in the application's standard state directory. Credentials use supported secure storage or a caller-provided environment/command seam.

Narrow table scopes, file prefixes and subscription-consume grants are independent. Table access alone never authorizes a file. Narrow clients are direct-API consumers, not partial replicas. A capped hub pauses event intake, ACK and metadata writes; the client retains state and retries after min(Retry-After, 3600 seconds). Source URLs survive delayed capture, but external historical content cannot be guaranteed.

Acceptance: archive a real commerce page and a manufacturer page, inspect both artifacts offline, verify restart/ACK/upload failure boundaries and scope denials, then permit configured backfill. Installation uses an app-owned Nix package and home-manager module. No provider credentials or machine-specific paths are required by the product.
