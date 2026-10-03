# Page Archiver

Capture a public web page as self-contained HTML and a full-page PNG, with a
manifest containing MIME types, byte counts and SHA-256 checksums. No analytics.

The current CLI supports one-shot captures and local queue status. Durable hub
subscriptions, upload, background operation and backfill are in development.
The [design](docs/design.md) describes that integration; those routes and the
future capture viewer are not part of this release.

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

Credentials must never be put in Nix settings or a repository. Future hub
integration uses its own independently revocable consumer credential; a table
read grant never grants file access.

## Capture boundaries

Only public HTTP(S) destinations are allowed. The local capture proxy resolves
and pins each connection to a public IP, including redirects and resources.
The browser receives neither hub credentials nor a reused login profile.
Captures are bounded by elapsed time, transfer bytes and image dimensions.
Recognized CAPTCHA/login walls, partial HTTP responses, missing archive resources
and exceeded limits are failures. Loading placeholders and visibly busy content
also fail. These checks are heuristics; success does not prove that a site supplied
all of its content. Failed background fetch/XHR requests are counted in the manifest's
`page_request_failures` field, since a failed metrics request can leave content intact.

Large media-heavy pages can exceed the default 50 MiB artifact limit. Increase
`max_artifact_bytes` explicitly when retaining those pages is worth the storage.

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
