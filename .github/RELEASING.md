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

For release candidates, use `X.Y.Z-rc.N` for the Git tag and release title but
keep the source version rules documented for that prerelease cycle.
