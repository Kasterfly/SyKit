# SyKit 0.6.0 - Tool Update Update

SyKit 0.6.0 adds `python SyKit update`: one command that updates the
SyKit tool folder to a new release without losing installed packages.

## Added

- **`python SyKit update [source] [--yes]`:** removes every installed
  package (removal restores a clean core), replaces the core files with
  the fetched release, then reapplies the stored copies of the packages
  in order. The final report says exactly what happened to each package:
  reapplied, refused because its `sykit-req` exceeds the new version
  (named explicitly), or failed because its edits no longer anchor.
  Failed packages stay uninstalled; look for a newer release and
  `package add` it again.
- **Sources:** no source means the latest release of the `update-repo`
  tool setting (default `Kasterfly/SyKit`), falling back to the default
  branch. A tag, branch, or commit of that repo also works, as does a
  local folder holding a SyKit tree (offline updates). GitHub refs are
  pinned to exact commits with the same machinery, https rules, and
  size caps as remote package installs.
- **`update-repo` tool setting:** the GitHub repo updates come from.

## Behavior notes

- The prompt defaults to No; a closed stdin aborts; `--yes` is for
  scripts. Same-version updates stop early; downgrades print a warning.
- `.git/` and `.packages/` are preserved. Everything else in the tool
  folder is made equal to the release, so stale files from older
  versions disappear. `sykit/config.json` is reset to the release
  template and a note lists tool settings that differed.
- Reapplied packages keep their recorded bytes and provenance; nothing
  is re-downloaded and there is no second review prompt, mirroring how
  removal reapplies later packages.
- If replacing the core fails, the previous core is restored and the
  packages are reapplied; the tool is never left half-updated.

## Upgrade notes

- Run updates from a new process after this one lands: the running
  0.5.0 tool does not know the command yet.
- Projects are untouched: rebuild deployed apps with
  `python SyKit build` after updating so the new runtime lands in
  `built/`, and re-run `python SyKit init` when a release adds new
  `sykit/` modules.
