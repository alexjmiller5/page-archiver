# Capture discovery contract

The installed CLI exposes JSON `list`, `search` and `coverage`. The README defines
the user interface and interpretation limits. These commands use the existing
scoped row API and require no hub schema or route changes.

Implementation: `discovery.py` owns bounded pagination, literal matching, history
selection and coverage aggregation. `pending_snapshot` opens an existing queue
with SQLite `mode=ro` and query-only mode, verifies its consumer identity, reads
one transaction and closes it. CLI dispatch reaches discovery before constructing
the mutating `Store`. Reuse the existing `HubClient` session and error handling.

Validation plan: drive commands with synthetic hub pages; check stable continuation,
source/status/URL filters, repeated pages, scan-budget failures, URL replacement,
deleted sources, duplicate URLs across records, success/partial precedence, active
retries, unavailable queue identity and hub cap/denial/outage behavior. Run the full
browser suite after generating bundled browser assets, static checks and the Nix
package build. Mutate retained precedence, queue-identity admission and scan bounds
to prove those behavior tests fail. No private source rows belong in fixtures.

The output is a CLI report, not a new Iris protocol. A viewer consumes an
explicit validated attempt using its independently authorized file reader; it
must not infer access to the consumer's local queue from capture metadata.
