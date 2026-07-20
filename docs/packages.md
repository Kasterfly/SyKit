# Packages

Packages patch the SyKit tool folder itself: they can add new files, edit
existing ones, and remove files. Every install is recorded as a reversible
diff, so removing a package restores SyKit to the exact state it would be in
had the package never been added.

> [!WARNING]
> A package can replace executable SyKit source. Installing one grants it the
> same trust as running its code. Review the complete package and only install
> packages from sources you trust.

## Commands

```
python SyKit package add <source> [--yes] [--allow-core]
                                             Install a package (see Sources)
python SyKit package remove <id>             Uninstall as if it was never added
python SyKit package list                    Show installed packages in order
python SyKit package diff <id or *>          Show what a package changed
```

## Sources

`package add` accepts four kinds of source, checked in this order:

1. An existing local folder: `python SyKit package add path/to/package`
2. A GitHub spec: `python SyKit package add github:Owner/Repo[/subdir][@ref]`
3. An https tarball URL: `python SyKit package add https://host/pkg.tar.gz`
4. A package name: `python SyKit package add <name>[@ref]`

A local folder always wins over a package name with the same spelling; an
info line points out the ambiguity when that happens.

Bare names resolve against the official packages repo,
`Kasterfly/SyKit-Packages`. Without an explicit `@ref` the latest GitHub
release is used when one exists, otherwise the repo's default branch. The
`package-default-repo` setting can point names at a different repo (see
[Configuration](configuration.md)).

GitHub installs are pinned: SyKit first resolves the ref to an exact commit
through the GitHub API and then downloads the archive by that commit, so the
recorded provenance always describes the installed bytes. If the API is
unreachable, the install falls back to fetching the moving ref and says so
with a warning.

Remote downloads are https-only (including every redirect hop), capped in
size (`package-max-download-mb`, default 50 MB for both the download and the
extracted content), and extracted defensively: absolute paths, `..`
components, symlinks, hardlinks, device entries, Windows-reserved names, and
case-colliding paths are all rejected. URLs with embedded credentials are
rejected outright so they can never end up in install records.

## Pre-install analysis

Before anything is applied, every install prints a static report of what the
package does and asks for confirmation:

```
Package: aws-integration (aws)
Source: github:Kasterfly/SyKit-Packages/aws@v1.2.0 (tag, commit 4f2a91c0f2aa)
Origin: official packages repo Kasterfly/SyKit-Packages
Adds 3 files, edits 2 (1 core), removes 0. New content: 12.4 KB.

  CRITICAL  core-edit          edit package.py - modifies the package handler itself...
  WARNING   url                add sykit_aws/client.py - https://example-metrics.io
  WARNING   exec-call          add sykit_aws/setup.py - contains 'subprocess'...

1 critical, 2 warning(s), 0 info.
Installing a package grants it the same trust as running SyKit's own code.
Install? [y/N, d shows package content]
```

The default answer is No: an empty answer, a closed stdin, or an interrupted
prompt aborts without changing anything. Answering `d` prints the exact
content every operation introduces (added files, edit payloads, and inline
edit instructions), then asks again.

The analysis runs on effective operations. Payload text hidden inside edit
instruction files is scanned like any added file, and instruction files
themselves are never treated as content. The analyzer and the install use
one snapshot of the package; its content hash is verified again right before
applying.

Finding codes:

| Code | Severity | Fires when |
| --- | --- | --- |
| `core-edit` | critical | SyKit tool code is added, edited, or removed: `package.py`, `build.py`, `init.py`, `help.py`, `check_requirements.py`, `__main__.py`, the analyzer modules, or anything under `sykit/` |
| `config-edit` | critical | `sykit/config.json` changes; settings such as `package-default-repo` quietly persist into later installs |
| `ci-edit` | critical | anything under `.github/` changes; CI executes code on push |
| `deps-edit` | critical | `requirements.txt`, `requirements-dev.txt`, or `pyproject.toml` change |
| `dependency` | warning | the manifest declares runtime dependencies via `deps`; SyKit does not install them |
| `replace-file` | warning | an edit replaces a whole file instead of using an anchored edit |
| `remove` | warning | a file is removed |
| `url` | warning or info | URLs in introduced content; info for https URLs on a small allowlist (github.com, raw.githubusercontent.com, pypi.org, npmjs.com) and in documentation files; raw IP addresses always warn |
| `exec-call` | warning | `subprocess`, `os.system`, `os.popen`, `eval(`, `exec(`, `compile(`, `importlib`, `ctypes`, or `socket` in introduced Python/JS code |
| `script-file` | warning | an executable script (`.sh`, `.bash`, `.bat`, `.cmd`, `.ps1`) is added; the code rules above do not scan shell scripts |
| `env-read` | warning | introduced content reads environment variables; `SYKIT_SESSION_SECRET` is called out by name |
| `opaque-blob` | warning | long base64/hex literals or binary files; content hidden from review |
| `git-remote-config` | warning | `.gitmodules` or `.gitattributes` are added or edited |
| `editor-config` | warning | editor workspace files that can auto-run commands are added |

The rules are review assistance, not a sandbox: they catch accidents and
lazy attacks, not determined attackers. There is no sandboxing, signing, or
malware detection; the warning at the top of this page is still the actual
security model.

Flags:

- `--yes` skips the prompt, but only when there are no critical findings.
  Scripts and CI must pass it explicitly; a non-interactive install without
  it aborts.
- `--allow-core` is required, in addition to `--yes` or an interactive yes,
  whenever critical findings exist. Without it the install refuses even on
  an interactive yes.
- There is intentionally no flag, setting, or environment variable that
  disables the analysis, the prompt, or the critical gate.

Report output is sanitized before printing: control characters, bidi
overrides, and zero-width characters in package content are replaced.

## Updating SyKit with packages installed

SyKit is a cloned tool folder and packages patch it in place, so updating
the folder with `git pull` while packages are installed will conflict or
silently mismatch. Use the update command instead:

```
python SyKit update [source] [--yes] [--allow-unreleased]
```

It removes every installed package (removal restores the original files
exactly), replaces the core files with the fetched release, then
reapplies the stored copies of the packages in their original order and
reports exactly which ones no longer fit: a package whose `sykit-req`
exceeds the new version is refused with a message naming both versions,
and a package whose edits no longer anchor fails cleanly and stays
uninstalled; look for a newer release of that package and
`package add` it again.

Details:

- Without a source, the latest release of the `update-repo` tool setting
  (default `Kasterfly/SyKit`) is fetched and resolved to a full commit SHA.
  An API outage, rate limit, or unpinned result aborts before download.
- A branch source requires `--allow-unreleased` and must still resolve to a
  full commit SHA. A release tag, full SHA, or local SyKit tree is accepted
  without that flag. `--yes` never weakens these source checks.
- The prompt defaults to No and a closed stdin aborts; pass `--yes` for
  scripts. Same-version updates stop early; downgrades warn.
- `.git/` and `.packages/` are preserved; everything else in the tool
  folder is made equal to the release, and `sykit/config.json` is reset
  to the release template (a note lists tool settings that differed).
- Reapplied packages keep their recorded bytes and provenance; nothing
  is re-downloaded and there is no second review prompt, mirroring how
  removal reapplies later packages.

## Provenance

Each install records where its bytes came from inside the package's
`record.json` in `.packages/`:

```json
"source": {
    "spec": "github:Kasterfly/SyKit-Packages/aws@v1.2.0",
    "kind": "github",
    "resolved_sha": "4f2a91c0f2aa...",
    "ref_type": "tag",
    "content_hash": "sha256:..."
}
```

- `kind` is `local`, `github`, or `url` (URL installs also record the final
  post-redirect `final_url`).
- `resolved_sha` is the exact commit, or null when it could not be pinned.
- `content_hash` is computed from the resolved package folder exactly as it
  was analyzed and applied.
- `package list` shows each package's source.
- Records written by SyKit 0.3.0 and earlier store a plain string here; they
  keep working and are treated as local installs.

## Hosting packages

Any GitHub repository can host SyKit packages with zero coordination:

- A single package: put `SyKitPackage.json` (plus `add/`, `edit/`,
  `remove/`) at the repo root and share `github:You/YourRepo`.
- Several packages: one folder per package, plus an optional root
  `index.json`:

```json
{
    "packages": {
        "aws": { "path": "aws", "desc": "AWS integration" },
        "supabase": { "path": "supabase", "desc": "Supabase client" }
    }
}
```

Running `package add github:You/YourRepo` against a repo that has an
`index.json` but no root package prints the available packages and how to
install them. That file is the entire registry format; there is no central
submission or approval flow. Organizations can point bare names at an
internal repo with the `package-default-repo` setting.

## Creating a package

1. Make a folder.
2. Add a `SyKitPackage.json`:

```json
{
    "id": "my-package",
    "name": "My Package",
    "desc": "What it does.",
    "sykit-req": "0.4.1",
    "deps": ["some-dependency>=1.0,<2"],
    "package-req": ["some-other-id"],
    "credit": ["John Doe (https://example.com)"]
}
```

   - `id` **required**, letters, digits, `.`, `_`, `-` (must start with a
     letter or digit). IDs are matched without regard to case and may not use
     filesystem or SyKit metadata names such as `CON`, `index.json`, or
     `authors.md`. This is the name used by `remove` and `diff`.
   - `name`, `desc` **optional**, shown by `list`. Printed metadata may not
     contain terminal control characters.
   - `package-req` **optional**, package ids that must already be installed
     before this one can be added.
   - `sykit-req` **optional**, the minimum SyKit version the package
     needs, as `"X.Y.Z"`. Installing on an older SyKit fails up front
     with a clear message. Handlers before 0.4.1 reject manifests that
     use this key.
   - `deps` **optional**, a string or list of pip requirement strings the
     package's code needs at runtime (for example `"boto3>=1.34,<2"`).
     SyKit never installs dependencies: they are flagged in the
     pre-install report, recorded, shown by `list`, and printed after
     install so you can install them yourself.
   - `credit` **optional**, a string or list of strings naming the package's
     authors. Values may not contain terminal control characters. While the
     package is installed, they are listed in `.packages/authors.md` (removed
     again when no credited package remains).

3. Create `add/`, `edit/` and/or `remove/` folders inside it (only the ones
   you need). A `README`/`LICENSE` and hidden files are ignored; anything
   else at the top level is an error.

All paths inside a package mirror SyKit's own layout, relative to the SyKit
folder. Packages may not touch `.git/`, `.packages/`, or `__pycache__/`,
including aliases that differ only by letter case. Packages may not contain
symbolic links, and two operations whose paths differ only by letter case
are refused.

### `add/` new files

Every file under `add/` is created at the same path inside SyKit:

```
add/files/test.txt  ->  SyKit/files/test.txt
```

Adding a file that already exists is an error (use `edit/` for that).
Missing folders are created automatically and cleaned up again on removal.

### `edit/` changing existing files

An edit is a payload file plus an optional instruction file named
`<file>.json` next to it:

```
edit/files/frontend/index.html        <- content to insert (the payload)
edit/files/frontend/index.html.json   <- how to apply it
```

Without the `.json`, the payload simply **replaces the whole target file**.

The `.json` holds one edit object, or a list of them applied in order:

```json
[
    { "action": "insert-after", "anchor": "<head>" },
    { "action": "append", "content": "<!-- footer -->\n" }
]
```

- `action`: one of:
  - `replace-file`: replace the entire file
  - `append` / `prepend`: add content at the end / start of the file
  - `insert-after` / `insert-before`: insert content right after / before
    the first occurrence of `anchor`
  - `replace`: replace the first occurrence of `anchor` with the content
- `anchor`: literal text to search for (required for `insert-*` and
  `replace`; it is an error if the anchor is not found).
- `content`: optional inline text; when omitted, the payload file's
  content is used. Content is inserted verbatim (bring your own newlines).

Note: a file named `X.json` sitting next to a file named `X` inside `edit/`
is always treated as the instruction file for `X`, never as a payload.
Editing a file that does not exist in SyKit is an error.

### `remove/` deleting files

`remove/` contains one or more `.json` files, each a plain list of SyKit
paths to delete:

```json
["files/core/endpoints.mjs", "files/frontend/old.css"]
```

Only files can be listed (not folders), and they must exist.

## Viewing / undoing changes

When a package is added, its diff and its position in the install order are
stored as an entry in SyKit's `.packages/` folder (a copy of the package
source, before/after snapshots of every touched file, and a `record.json`).

- `python SyKit package diff <id>` prints the recorded unified diff;
  `diff *` prints every installed package in order.
- `python SyKit package remove <id>` reverses the diff. If the package is, say,
  3rd of 5, packages 5 and 4 are unwound first, then 3 is reversed, then 4
  and 5 are re-applied from their stored copies, so it is as if 3 was
  never there. Re-applying those stored copies does not prompt again: their
  bytes were reviewed and accepted at install time and cannot have changed.
- Removal fails up front if a remaining package lists the id in its
  `package-req`. If re-applying a later package fails for any other reason
  (for example its anchor text came from the removed package), the whole
  removal is rolled back and nothing changes.

A package cannot be added twice; remove it first, then add the new version.
