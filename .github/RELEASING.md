# Release Process

1. Run lint, format, unit, branch coverage, quick-start, browser, container,
   Python audit, and npm audit checks from a clean checkout.
2. Confirm the version in `sykit/__init__.py`, README status, `CHANGELOG.md`,
   detailed release note filename and title, tag, and GitHub release title all
   match `X.Y.Z`.
3. Confirm the install example clones the release tag, not `main`.
4. Regenerate dependency locks from their inputs and review all changes.
5. Create a signed annotated tag: `git tag -s X.Y.Z -m "SyKit X.Y.Z"`.
6. Push the tag only after the protected default branch contains the release
   commit and all required checks pass.
7. Create the GitHub release named `X.Y.Z`, use the detailed changelog as its
   body, and include checksums for any manually uploaded assets.
8. Mark the release immutable and confirm the repository rules protect the
   release tag pattern from deletion or movement.
9. Reinstall from the published tag and run the quick-start once more.

## 0.14.x soak and 1.0.0

- Treat 0.14.x as feature-frozen. A correction ships as a 0.14.x patch and
  restarts the active soak for the changed area.
- Require the supported-runtime matrix plus lint, audit, coverage, browser,
  and container jobs in repository rules before the final 0.14.x rehearsal.
- Keep release tags protected from update and deletion. Do not bypass a failed
  required check for a release.
- Publish 1.0.0 only when the final change from the soaked 0.14.x tree is
  stable version, status, support, changelog, and release metadata.
- Run official package compatibility against the exact release tag and record
  the compatible protected SyKit-Packages release in the release notes.

For release candidates, use `X.Y.Z-rc.N` for the Git tag and release title but
keep the source version rules documented for that prerelease cycle.
