# Page Archiver

Capture a public web page as self-contained HTML and a full-page PNG, with a
manifest containing MIME types, byte counts and SHA-256 checksums. No analytics.

The CLI supports individual captures and a durable outbound Life Data subscription
consumer. It stores a recoverable local queue and publishes immutable files plus
capture metadata. Hub setup is described in [the contract](docs/hub-setup.md).
The future capture viewer is separate work.

## Install and capture

```sh
nix profile install github:alexjmiller5/page-archiver
page-archiver capture https://example.com/ --output ./saved-page
page-archiver status
```

Linux packages supply Chromium. On macOS, install Chrome through your system's
package configuration, or set `PAGE_ARCHIVER_BROWSER_EXECUTABLE` to an installed
Chromium-compatible executable. Each capture launches a disposable, logged-out
browser. It does not attach to an existing browser or need account enrollment.

The output directory must not exist. On success it contains `page.html`,
`page.png` and `capture.json`. Failures return a nonzero exit code and a JSON
status without successful artifact paths. Existing captures are never replaced.

## Configuration

Settings load from `$XDG_CONFIG_HOME/page-archiver/config.json` (default
`~/.config/page-archiver/config.json`). Environment variables beginning with
`PAGE_ARCHIVER_` override that file. For example:

```json
{
  "capture_timeout": 90,
  "max_artifact_bytes": 52428800
}
```

The default state directory is `~/Library/Application Support/page-archiver` on
macOS and `$XDG_STATE_HOME/page-archiver` on Linux (default `~/.local/state`).
`state_dir` and `browser_executable` are configurable. State never lives in the
checkout. No credentials are needed for one-shot public-page capture.

Home Manager can install the package and generate the same configuration:

```nix
{
  imports = [ inputs.page-archiver.homeModules.default ];
  programs.page-archiver = {
    enable = true;
    settings.capture_timeout = 90;
  };
}
```

## Background operation

Configure an HTTPS hub origin, `subscription_id`, `capture_table` and
`artifact_prefix`, plus either `PAGE_ARCHIVER_HUB_TOKEN` from your process's secure
environment or `credential_command`, an array of executable and arguments. The
command prints the independently minted consumer token and runs once at startup.
Use an absolute executable path for background services. Its secure-storage
access and environment must be configured independently of your interactive shell.
Never put the token in config.json, Nix, source control or command-line arguments.

```sh
page-archiver watch
page-archiver backfill
page-archiver status
page-archiver run-once
page-archiver retry <capture-id>
page-archiver retrieve <attempt-id> --output ./retained-page
```

`watch` long-polls for up to 30 seconds while one separate worker captures pages.
Live jobs precede backfill. Intake commits events and a delivery receipt before
ACK, and pauses when the local queue reaches `max_pending_jobs` (default 10,000).
If one delivery alone exceeds the configured bound, the runner stops with
`delivery_exceeds_queue_capacity`; raise `max_pending_jobs` and restart. The
default 10,000 exceeds the protocol maximum of 1,600 jobs per delivery.
Backfill checkpoints each completed page; rerunning resumes an interrupted scan,
or starts a fresh scan after completion. Repeated observations deduplicate.
Run it alongside the watcher so queued work can drain. A failure exits nonzero;
rerun after resolving the reported code. `run-once` performs one intake and one
capture without becoming a service.

The Home Manager module owns the service definition:

```nix
programs.page-archiver = {
  enable = true;
  service.enable = true;
  settings = {
    hub_url = "https://hub.example.test";
    subscription_id = "<subscription-id>";
    capture_table = "captures";
    artifact_prefix = "captures/";
    credential_command = [ "/path/to/secure-credential-reader" ];
  };
};
```

The service executes the Nix package. Nonsecret environment preparation belongs
in `service.environment`. macOS uses launchd; Linux uses a user systemd service.
Changing Nix settings changes the service definition, so activation reloads the
runner with the new configuration even when the package has not changed.
On macOS the job starts in the user's GUI domain, where the browser is available.
Network failures retry inside the process. Authentication or integrity failures
halt with a stable status code and exit 78; repair the credential/configuration
and restart the service through the host's service manager. `status` is offline
and does not fetch credentials. Service startup failures also return a sanitized
JSON error on standard output. macOS service output is in
`~/Library/Logs/page-archiver.log`; Linux uses the user journal.

On macOS, a credential command can read an independently enrolled generic
password with `/usr/bin/security find-generic-password -s <service> -a <account> -w`.
Enroll through the desktop session using the hub token API and native Keychain;
pass the issued token to `security -i` through stdin, never an argument or file.
An SSH session may be unable to access the login Keychain even when the GUI
launchd service can. Verify enrollment in the service's actual launch context.

Rotation means updating this consumer's secure credential, restarting it, then
revoking its old token through the hub. On a replacement machine, enroll a new
independent consumer credential. Preserve the private state directory if continuing
the same queue: SQLite, WAL and spool together, with the service stopped. A new
empty queue cannot recover already ACKed work by polling. Keep the old state for
reconciliation, inspect retained metadata, and explicitly backfill or recapture
missing observations. Do not reuse a state directory for a different hub,
subscription, table or artifact prefix.

Retrieval checks independent file authorization and verifies actual downloaded
bytes against metadata. It requires a new output directory, writes HTML, PNG and
metadata, and never opens HTML automatically. Retained files remain in the hub
when the runner is disabled or its local staged bytes are cleaned up.

## Capture boundaries

Only public HTTP(S) destinations are allowed. The local capture proxy resolves
and pins each connection to a public IP, including redirects and resources.
The browser receives neither hub credentials nor a reused login profile.
Captures are bounded by elapsed time, transfer bytes and image dimensions.
Recognized CAPTCHA/login walls, partial HTTP responses and exceeded limits are
failures. Missing archive resources fail by default or produce explicitly marked
partial copies when retention is enabled. Loading placeholders and visibly busy content
also fail. These checks are heuristics; success does not prove that a site supplied
all of its content. Failed background fetch/XHR requests are counted in the manifest's
`page_request_failures` field, since a failed metrics request can leave content intact.
Unused CSS rules are removed before fetching their assets, so missing images used
only by absent elements do not invalidate a complete page. Missing retained images
and styles prevent a complete-success classification.
Initially empty pages get a bounded wait for rendered text. In-flight content
requests get up to five seconds to finish before capture; background long polls
do not prevent an otherwise complete page from being saved. Serialization runs
in an isolated browser world; where supported, the safe HTML parser handles
Trusted Types pages without changing their security policy. Site scripts are
excluded from serialization without rewriting their text, including scripts in shadow
DOM. Final cleanup uses a fresh offline document so declarative shadow content
survives parsing. The retained page contains no runnable scripts.

Large media-heavy pages can exceed the default 50 MiB artifact limit. Increase
`max_artifact_bytes` explicitly when retaining those pages is worth the storage.
Full-resolution screenshots have a separate `max_screenshot_pixels` budget
(50 million by default, configurable up to 200 million). Raise it for long pages
when the capturing machine has enough memory. `min_screenshot_scale` defaults to
1 (full resolution only). Set it to 0.5 to permit a full-page PNG at no less than
half resolution per dimension when needed to fit the pixel budget. HTML layout
and fidelity are unchanged; pages needing a greater reduction remain too large.

`retain_partial` defaults to false. When enabled, readable finished pages with
missing images/fonts or other resource requests retain both artifacts with a
separate `partial` status, a visible HTML warning, and a safe count warning in
metadata and `retrieve` output. Login gates, HTTP failures, blank pages and
stuck loading screens still produce no artifacts. `status` counts partial
captures separately from complete successes and failures.

A capture records what the external site serves when fetched. It cannot recover
content that changed or disappeared before capture. Video/audio and embedded
frames are omitted. Interactive behavior is not retained.

Archived HTML remains hostile content even though scripts are removed and a
restrictive CSP is embedded. Use the PNG for previews. Any HTML viewer must use
an opaque-origin sandbox, deny network access and navigation, and expose no
application bridge. Opening the original site is a separate explicit action.

## Develop

```sh
bun install --frozen-lockfile
bun run scripts/build-browser.ts
just test
just check
just build
```

Python dependencies are locked with uv; its environment lives outside the
checkout. Bun bundles pinned SingleFile Core for the browser. Nix builds the
same source without network access during compilation. Browser tests exercise
real Chromium against controlled local fixtures through a test-only DNS seam,
then reopen the result offline. They need an installed browser.

Licensed under AGPL-3.0-or-later. The browser bundle includes
[SingleFile Core](https://github.com/gildas-lormeau/single-file-core) under the
same license; its embedded third-party notices accompany the bundle.
