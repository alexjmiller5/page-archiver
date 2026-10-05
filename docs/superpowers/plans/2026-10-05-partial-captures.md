# Partial captures and bounded screenshots

> For agentic workers: use superpowers:executing-plans to implement this plan task by task.

**Goal:** Retain readable incomplete pages with explicit warnings and optionally fit long-page PNGs within the pixel budget without changing HTML fidelity.

**Architecture:** Keep the existing capture, immutable publication and retrieval pipeline. Opt-in partial retention adds a distinct terminal `partial` status with both verified artifacts and a safe missing-resource count warning. Page-level loading/auth/HTTP failures remain failures without artifacts. Screenshot downscaling is opt-in, bounded to at least half resolution in each dimension, and applies only when full resolution exceeds the configured pixel budget.

**Tech stack:** Python, Playwright/CDP, SQLite, Life Data row/file APIs, Nix.

- [x] Add failing real-browser tests for retained missing assets versus rejected incomplete page bodies, and bounded full-page scaling with unchanged HTML.
- [x] Implement settings and capture behavior; annotate retained HTML and metadata; preserve strict defaults.
- [x] Test and implement partial publication, idempotent recovery, local state accounting and retrieval warnings. Extend metadata fixture and documentation.
- [x] Update the operator-owned catalog through Life CLI, refresh documentation and coordinate reader compatibility.
- [ ] Run meaningful regression tests, static/package/system checks; publish source and verify CI. Install opt-in settings through Nix.
- [ ] Retry affected historical failures; verify retained complete/partial artifacts offline and finish the original recovery queue.
