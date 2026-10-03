# Hub setup and capture metadata v1

The hub must implement `row_api: v1`, `subscriptions: durable-pull-v1` and
`files: opaque-key-v1`. Page Archiver is a direct API consumer; it never enrolls
as a schema-replaying replica. The hub implementation is proposed in
[Life Data PR #5](https://github.com/alexjmiller5/life-data/pull/5).
These additions require deployment before a production runner can connect.

An operator creates the table and catalog through the installed Life CLI,
activates a subscription, and independently mints the consumer credential.
The archiver never holds an administrator credential. The following names are
examples; table names, sources and artifact prefix are runtime configuration.

```sh
life table create captures \
  'capture_id:text!' 'event_id:text!' 'subscription_id:text!' \
  'source_table:text!' 'source_row_id:text!' 'source_column:text!' \
  'source_url:text!' 'observed_source_revision:json!' \
  'attempted_at:datetime!' 'captured_at:datetime' \
  'status:select!(succeeded|blocked|failed|unsupported)' \
  'failure_code:text' 'failure_detail:text' \
  'html_key:text' 'html_mime:text' 'html_bytes:int' 'html_sha256:text' \
  'png_key:text' 'png_mime:text' 'png_bytes:int' 'png_sha256:text'
life sql "CREATE UNIQUE INDEX captures_success_event_column ON captures(event_id,source_column) WHERE status='succeeded'"
life sync
```

Add the table's owner, purpose, consumers and column descriptions to the catalog
using `life sql`, then refresh documentation with `life doc`. Its owner is Page
Archiver; readers use the hub APIs. File keys are ordinary text. A future file
catalog type or capture viewer is not required. Avoid enforced SQL rules,
derivations, custom triggers and cross-table side effects on a table writable
by a restricted consumer. The hub fails closed on those unsafe write policies.

The machine-readable [metadata fixture](../tests/fixtures/capture-metadata-v1.json)
is checked against the publisher and gives exact success/failure fields.

The Life table command supplies `id`, `created_at`, `updated_at`, `deleted_at`
and `hub_at`. The publisher supplies a stable attempt UUID as `id`; it never
supplies `hub_at`. All submitted dates use UTC milliseconds ending in `Z`.

| Fields | Meaning |
| --- | --- |
| `capture_id` | Stable logical capture identity for subscription, event and column |
| `event_id`, `subscription_id` | Original durable event and owning subscription |
| `source_table`, `source_row_id`, `source_column` | Original selected source location |
| `source_url` | Exact accepted URL, including an invalid URL recorded as a failure |
| `observed_source_revision` | JSON text containing `updated_at` and `hub_at` from the observation |
| `attempted_at`, `captured_at` | Attempt start and successful capture time; capture time is null on failure |
| `status` | `succeeded`, `blocked`, `failed` or `unsupported` |
| `failure_code`, `failure_detail` | Stable local failure code and reserved nullable detail; both null on success |
| `html_key`, `png_key` | Canonical opaque hub keys, never URLs or credentials |
| `<kind>_mime`, `<kind>_bytes`, `<kind>_sha256` | Exact MIME, byte count and lowercase SHA-256 for HTML or PNG |

Every successful row contains both complete artifact groups. Every failed row
has both groups and `captured_at` null. Metadata is inserted once per attempt.
A retry of publication reuses the same attempt ID and bytes; a new browser
capture gets a new attempt ID. Transient capture failures have at most three
automatic attempts. Explicit `retry` creates a new observation of the current
source row, with its current revision.

Artifact keys are `<prefix><capture-id>/<attempt-id>/page.html` and `page.png`.
PUT sends `If-None-Match: *`, MIME, byte count and `X-Content-SHA256`.
A 412 is reconciled through HEAD. Both remote objects must match MIME, bytes and
SHA-256 before a success row is inserted. A conflict stops the runner and retains
its local attempt for inspection; it never replaces an existing object.

Create a subscription with the administrator API:

```json
{
  "label": "Page captures",
  "sources": [{"table": "articles", "columns": ["url"]}],
  "start": "now"
}
```

POST this to `/v1/subscriptions`. The returned opaque ID goes in the archiver's
configuration. The dedicated consumer requires these exact grants, substituting
the chosen names, prefix and subscription ID:

```text
tables:read:articles
tables:read:captures
tables:write:captures
files:read:captures/
files:write:captures/
subscriptions:consume:<subscription-id>
```

Mint that token through the hub's supported token interface and deliver it to
the consumer's secure credential storage. It must not share another caller's
credential. Revoking or rotating this token affects this consumer only. A Life
UI reader gets its own table and file read grants; table access grants no file
access. A capture table shared across source tables requires explicit combined
source access. Future restricted views need row-level source authorization.

Activate the subscription before backfill so mutations during the scan also
remain in the durable outbox. Start the runner, then run `page-archiver backfill`.
A scan queues current observations; its `queued` result does not mean all pages
have been captured. Use `status` to track queue drain and terminal failures.

The outbox persists URLs and accepted revisions, including rapid A-to-B changes.
It cannot guarantee that a remote website still serves A's original content when
fetched. A capped hub pauses event/ACK/metadata access. The runner sleeps at most
one hour between cap retries and uses jittered 1-60 second transport backoff.
There is no webhook, public listener or delivery cron.
