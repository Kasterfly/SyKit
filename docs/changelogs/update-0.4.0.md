# SyKit 0.4.0 - Package Handler Update

SyKit 0.4.0 upgrades the package system: every install now shows a static
pre-install analysis with severity tiers and a confirmation prompt, and
packages can be installed directly from GitHub or any https tarball URL with
provenance pinned to an exact commit.

## Added

- **Pre-install analysis:** `package add` prints a report of what the
  package does before anything is applied: files added, edited (with core
  files called out), and removed, plus findings in three tiers.
  `critical` findings cover SyKit tool code, `sykit/config.json`, CI
  workflows, and dependency files. `warning` findings cover whole-file
  replacements, removals, URLs, exec-style calls, added scripts,
  environment reads (`SYKIT_SESSION_SECRET` by name), opaque blobs, git
  config files, and auto-run editor files. `info` covers allowlisted and
  documentation URLs.
- **Confirmation prompt:** the default answer is No, `d` shows the full
  content a package introduces, and a closed stdin aborts. `--yes` skips
  the prompt only when there are no critical findings; `--allow-core` is
  additionally required when critical findings exist. There is no flag,
  setting, or environment variable that disables the analysis or the
  prompt.
- **Remote sources:** `package add` accepts
  `github:Owner/Repo[/subdir][@ref]`, an https tarball URL, or a bare
  package name. Names resolve against the official
  `Kasterfly/SyKit-Packages` repo (latest release, then default branch).
  Any repo can host packages: a root `SyKitPackage.json` for a single
  package, or one folder per package with an optional `index.json` that
  `package add github:Owner/Repo` prints as a listing.
- **Provenance:** every install records its source spec, kind, resolved
  commit, ref type, and a content hash of the exact folder that was
  analyzed and applied. `package list` shows each package's source.
- **Settings:** `package-default-repo` points bare names at another repo
  (for org-internal package repos); `package-max-download-mb` caps
  download and extraction size. Both are read from the SyKit tool's own
  `sykit/config.json`.

## Security changes

- GitHub installs resolve the ref to an exact commit first and download the
  archive by that commit, so recorded provenance always describes the
  installed bytes. Installs from moving branches are labeled with a
  warning.
- Downloads are https-only, including every redirect hop, with capped
  redirects and a streaming size limit. URLs with embedded credentials are
  rejected so they can never land in `.packages/` records.
- Archives are extracted defensively: absolute paths, `..` components,
  symlinks, hardlinks, device entries, Windows-reserved names, and
  case-colliding paths are rejected, and size and entry-count caps apply.
- Local packages get the same strictness: symbolic links in a package are
  rejected, and packages whose operations collide ignoring case are
  refused.
- Analyzer and diff output is sanitized before printing: control
  characters, bidi overrides, and zero-width characters are replaced, since
  package content is untrusted text.
- The analysis and the install operate on one snapshot; the package content
  hash is verified between the prompt and the apply step.

## Upgrade notes

- The warning system is review assistance, not a sandbox. The rule is
  unchanged: installing a package grants it the same trust as running its
  code.
- Existing install records from 0.3.0 and earlier keep working; they list,
  diff, and remove as local installs.
- Scripted installs must pass `--yes` explicitly (and `--allow-core` when a
  package intentionally changes SyKit core files); non-interactive installs
  without them abort.
- Future updates that edit SyKit tool code (like this one does) will ask
  for `--allow-core` once 0.4.0 is installed.
