# Browser readiness and incomplete sections

**Goal:** Capture readable pages when noncritical assets stall and support a fresh windowed browser for servers rejecting headless navigation.

**Requirements:**

- Navigation shall wait for the document to become available; existing content/font/resource readiness and capture bounds still apply.
- When explicitly configured, capture shall use a fresh windowed browser within the same deadline and public-network proxy boundary. No stored browser profiles or credentials are reused. A graphical session is required.
- While visible sections remain busy, opt-in partial retention shall require a visible primary heading and at least 200 visible content characters outside busy regions and navigation/footer/sidebar content. Busy primary landmarks or headings remain rejected.
- A retained partial shall warn separately about missing resource requests and unfinished sections using safe positive counts in HTML and metadata. Both artifacts remain verified and captured_at is present.
- New unreported loading regions shall prevent publication. Strict capture defaults and failure-without-artifacts semantics remain intact.

- [x] Reproduce navigation and readiness failures; add regression tests.
- [x] Implement readiness, browser mode, partial warnings and publication validation.
- [x] Verify browser regressions, mutation checks, full tests, package/system builds and independent review.
- [x] Refresh consumer documentation/catalog, publish and install; verify live retained artifacts and finish targeted retries.
