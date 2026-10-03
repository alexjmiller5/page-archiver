# Durable Runner Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Continue the approved inline execution; deployment is a separate operator decision.

**Goal:** Turn the capture engine into an installable outbound subscription consumer with durable publication and explicit backfill.
**Architecture:** An async intake loop commits batches and pending ACKs to SQLite. One separate worker captures and publishes queued observations, preferring live events over backfill. Staged attempt bytes survive restart and upload retries; success metadata follows verification of both files.
**Tech Stack:** Python, httpx, SQLite, existing Playwright/SingleFile engine, Nix and Home Manager.
**Spec:** docs/design.md and the Life Data canonical hub capability/subscription/file fixtures. The approved generic contract is durable-pull-v1 / opaque-key-v1.

## Global Constraints

- No analytics, inbound listener, provider credentials or personal selectors in source.
- Hub URL is HTTPS; redirects are errors and never receive forwarded credentials.
- One credential resolution per process startup; environment or generic command seam.
- Poll at most 30 seconds, one request in flight; validate 100 events / 1 MiB before committing.
- Commit work and receipt before ACK. 429 retries use min(Retry-After,3600), at least 1 second, default 60. Transport/5xx uses 1..60 second jittered backoff. 401/403 halt with a sanitized actionable status.
- Single capture worker bounds concurrency and spool growth; live jobs precede backfill. Local pending-job capacity halts acceptance without ACK or partial batch commit.
- Conditional file creation only. Verify MIME, bytes and SHA-256 by independently authorized HEAD, including retry after 412. Never overwrite mismatched bytes.
- Use a new attempt ID for new bytes. Reuse the staged attempt on restart. All artifact fields and captured_at are null on failure. Successful metadata is insert-only and independently reconciled after ambiguous responses.
- User schema, selectors, credentials and subscription IDs are runtime state. Installed services run the Nix package, never the checkout.

## Review Focus

- Lost ACK/upload/metadata responses and process restart must preserve one logical capture and exact staged bytes.
- A wrong subscription, altered event payload, denied key or response with unexpected shape cannot be acknowledged or published.
- A slow capture or capped metadata write must not block intake until the explicit local queue bound.
- Source changes during backfill must survive through the subscription, and rerunning the same backfill observation must deduplicate.
- Credentials and server errors must not enter browser environment, logs, archived content or metadata.

### Task 1: Authenticated bounded hub client

**Files:** src/page_archiver/client.py, config.py; tests/test_client.py, test_config.py.
**Interfaces:** HubClient(settings, transport=None) is an async context manager. session(), subscription(), poll(), ack(delivery_id), rows(table,columns,where=None,after=None), insert(table,row), upload(key,path,mime,sha256), head(key) validate bounded responses. HubError(code,retry_after=None,fatal=False) contains no upstream text. retry_delay(headers) returns 1..3600 seconds.
- [ ] Write tests for one credential read, zero credential forwarding on redirects, wrong protocol/grants, bounded response streaming, cap headers including weeks/invalid/HTTP-date, transient errors, fatal auth, canonical keys, verified HEAD metadata.
- [ ] Run `just test tests/test_client.py tests/test_config.py`; expect missing client/new settings failures.
- [ ] Implement only the configured origin and generic credential seam; timeout held requests at 40 seconds. Store no credential on disk or in logs. Every path/key is canonical and encoded once.
- [ ] Rerun focused tests and `just check`; expect all pass. Commit and push feature branch.

### Task 2: Recoverable attempts and bounded intake

**Files:** src/page_archiver/state.py; tests/test_state.py.
**Interfaces:** accept_batch(body,max_pending_jobs) keeps its atomic contract. next_job() selects live before backfill, returns source event with job. begin_attempt(capture_id) persists attempt ID/time. save_outcome(attempt_id,outcome), finish_attempt(attempt_id), pending_attempt(), enqueue_backfill(subscription_id,table,row,column) support restart. status() includes queued/active/succeeded/failed and sanitized runtime state.
- [ ] Test queue overflow rollback/no ACK, two processes attempting ownership, restart before/after staged manifest, stable attempt/observation IDs, duplicate backfill and live priority, persisted terminal result.
- [ ] Run state tests; expect new methods/bounds failures.
- [ ] Add schema upgrades transactionally. A process-level state-directory lock permits one runner; short SQLite transactions never span awaits. Immutable jobs and outcomes keep event identity and exact source revision.
- [ ] Run `just test tests/test_state.py` and `just check`; expect pass. Commit.

### Task 3: Intake, capture and publication recovery

**Files:** src/page_archiver/runner.py, publication.py; tests/test_runner.py, test_publication.py.
**Interfaces:** Runner(settings,store,hub,capture_fn=capture,sleep=asyncio.sleep) exposes intake_once(), process_one(), run(). publish_attempt(hub,settings,job,attempt,outcome) uploads and verifies both artifacts then inserts terminal metadata, reconciling a preexisting identical row after uncertain commit.
- [ ] Test crash boundaries before/after local commit and ACK, lost ACK response, interrupted first/second upload, changed staged bytes, lost metadata response, failure artifact nulls, isolated auth, cap sleep ceiling, cancelled loops, single capture concurrency and intake during capture.
- [ ] Run runner/publication tests; expect missing implementation failures.
- [ ] Recover existing capture.json from the persisted attempt directory after crash, validate filenames/checksums/bytes against actual files, and never recapture into an existing directory. Publish failed attempts without artifact keys. Retry transient capture failures at most 3 attempts; blocked/login/invalid/partial outcomes are terminal until explicit recapture. Successful remote metadata and files remain retained after local spool cleanup.
- [ ] Run all Python checks/tests and mutation tests for ACK ordering and conditional publication; expect all pass. Commit.

### Task 4: Installed operations, backfill and service module

**Files:** src/page_archiver/main.py, backfill.py, config.py, nix/home-manager.nix, README.md, AGENTS.md; tests/test_cli.py, test_backfill.py.
**Interfaces:** watch runs both loops; run-once accepts/ACKs available events and processes one job; backfill enumerates subscription-selected columns using scoped rows and enqueues current observations; status reports durable outcomes; retrieve <attempt-id> --output <new-directory> downloads both verified files through independent file grants; retry <capture-id> starts an explicit new observation. Generic configuration requires subscription_id, capture_table and artifact_prefix for hub work. Service enable/credential environment options belong to the exported module.
- [ ] Test CLI errors without secrets, explicit retrieval with byte/checksum verification and no overwrite, backfill cursor restart and selection, invalid/empty/deleted URLs, repeat dedupe, concurrent source revisions, status counts and service command using the package.
- [ ] Implement commands and service definition with macOS launchd and Linux systemd through Home Manager. Document independent credential enrollment/rotation via environment or secure command, source subscription activation before backfill, exact metadata schema and replacement-machine reconciliation.
- [ ] Run `just test`, `just check`, `just build` and generic module evaluation. Run installed package from a temporary directory against the local hub and controlled capture fixture; prove event, ACK, two artifacts and metadata after forced interruptions.
- [ ] Fresh whole-branch review; fix demonstrated important defects with red/green tests. Publish tested changes and final fixture note. Prepare deployment and consumer setup for the operator's final approval; no live installation, hub merge or backfill before that approval.
