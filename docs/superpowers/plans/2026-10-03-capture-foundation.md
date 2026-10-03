# Capture Foundation Implementation Plan

> Execute inline with superpowers:executing-plans; perform one fresh whole-branch review at the end.

**Goal:** Ship an installable CLI with durable local intake state and prove self-contained HTML/PNG capture before live hub integration.

**Architecture:** Python CLI and SQLite own local configuration, durable batches and artifacts. A fresh Chromium context runs pinned SingleFile Core and captures a PNG of the same page; a local egress proxy resolves and connects only to public addresses. Nix packages the application and browser support assets without consumer provider credentials.

**Tech Stack:** Python 3.13, uv, pydantic-settings, httpx, structlog, pytest, Ruff, Playwright, SingleFile Core, Bun asset build, Nix/uv2nix.

**Spec:** ../../design.md

## Global Constraints

- Public repository; no analytics.
- No source mappings, personal URLs or credentials in source control.
- State outside the checkout; credentials through secure storage or explicit seams.
- Local durable acceptance precedes ACK; every artifact fetch/upload has independent authorization.
- No public mini listener; capture browser holds no hub credential.
- Cap sleep ceiling is 3600 seconds; page capture defaults to 90 seconds and 50 MiB output per artifact.

## Review Focus

- Malformed or partly duplicated deliveries cannot partially commit or lose their ACK receipt (Task 1).
- Environment settings do not invoke a credential command during status/help or leak secrets in errors (Task 1).
- Private DNS results, rebinding, redirects and browser bypasses cannot reach local services (Task 2).
- A bot wall or exhausted capture limit is not reported as a successful archive (Task 3).
- Packaged installs contain the capture asset and can run from an unrelated working directory (Task 4).

### Task 1: CLI, settings and durable inbox

**Files:** src/page_archiver/{config,main,state}.py; tests/{test_config,test_state,test_cli}.py; pyproject.toml; justfile.
**Interfaces:** Settings resolves standard config/state paths and reads explicit endpoint/credential inputs. Store.accept_batch(batch) atomically stores an exact batch and deduplicated events; pending_ack() returns the persisted receipt; acknowledge(delivery_id) clears only that receipt. CLI status emits counts without reading credentials.
- [ ] Write tests for standard paths, secret redaction, endpoint validation, reopen-after-commit, duplicate delivery and rollback of malformed batches.
- [ ] Run just test; observe missing behavior failures.
- [ ] Implement validated settings, SQLite transactions and CLI status.
- [ ] Run just test and just check; expected all green.
- [ ] Commit the complete scaffold.

### Task 2: Public-network capture boundary

**Files:** src/page_archiver/network.py; tests/test_network.py.
**Interfaces:** public_addresses(host, port) returns only resolved global addresses, rejecting the entire answer set if any address is nonpublic. CaptureProxy exposes an ephemeral loopback HTTP proxy that dials validated IPs, tunnels CONNECT and refuses non-HTTP(S), credentials and prohibited destinations.
- [ ] Write tests for IPv4/IPv6 private, loopback, mapped, link-local, mixed DNS answers, redirects through proxy, bounded headers and shutdown.
- [ ] Observe failures, implement address-pinned proxy forwarding, run the focused tests green.
- [ ] Commit the boundary and tests.

### Task 3: Same-page HTML and PNG capture

**Files:** src/page_archiver/capture.py; browser/capture.js; scripts/build-browser.ts; package.json; tests/test_capture.py.
**Interfaces:** capture(url, destination, settings) returns typed artifact paths, MIME, byte counts, checksums and a classified outcome; it never logs request headers/cookies. Browser uses CaptureProxy, a fresh profile and explicit bounds. SingleFile creates HTML, then full-page screenshot captures the same loaded page state.
- [ ] Write failure tests for invalid URL, timeout, empty HTML, bot wall, oversized artifact and browser cleanup; integration fixture verifies embedded local assets and PNG dimensions.
- [ ] Observe failures, implement the engine and CLI capture command, run tests green.
- [ ] Run controlled real commerce/manufacturer captures; visually inspect PNG and offline HTML; record results outside source control.
- [ ] Commit the engine and verification tooling.

### Task 4: Portable packaging and published scaffold

**Files:** flake.nix; nix/home-manager.nix; README.md; AGENTS.md; .env.tpl; .github/workflows/check.yml.
**Interfaces:** packages.default exposes page-archiver; homeModules.default owns its launchd/systemd service, generic endpoint/settings and credential seam. No personal instance configuration in the app module.
- [ ] Build browser assets with pinned source and lockfile; build the Nix package.
- [ ] Run packaged status/help from a temporary directory; evaluate the module with generic fixture config.
- [ ] Add generic documentation and the approved shared-service contract; verify no template placeholders or runtime provider-auth coupling remain.
- [ ] Run full tests/static checks and critical-guard mutation checks; fresh final review.
- [ ] Publish public repository with description/topics, add profile entry and project catalog, and close completed tasks.

Subsequent separate hub and runner plans implement the approved scoped rows/capabilities, atomic outbox, long polling/ACK, retained-file publication and installation/backfill. The real capture proof is a gate before live wiring, not a claim that those later features already exist.
