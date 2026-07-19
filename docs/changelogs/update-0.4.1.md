# SyKit 0.4.1 - Manifest Keys Update

SyKit 0.4.1 adds two optional keys to `SyKitPackage.json` that make package
compatibility explicit, plus documentation for updating a SyKit folder that
has packages installed.

## Added

- **`sykit-req` manifest key:** the minimum SyKit version a package needs,
  as `"X.Y.Z"`. Installing on an older SyKit fails up front with a clear
  message naming both versions, instead of failing later with a cryptic
  anchor error. The requirement is shown in the pre-install report and
  recorded in the install record.
- **`deps` manifest key:** a string or list of pip requirement strings the
  package's code needs at runtime (for example `"boto3>=1.34,<2"`).
  Declared dependencies appear in the pre-install report as `dependency`
  warnings, are recorded in the install record, show up in
  `package list`, and are printed after install with a ready-to-run
  `python -m pip install` line. SyKit itself never installs them.
- **Docs:** a new "Updating SyKit with packages installed" section in
  `docs/packages.md` describing the remove, update, re-add flow.

## Upgrade notes

- Package handlers before 0.4.1 reject manifests that use the new keys
  with an "unknown keys" error. Published packages should only adopt
  `sykit-req` or `deps` once they are comfortable requiring SyKit 0.4.1
  or newer to install.
- Declared dependencies are informational: review them in the pre-install
  report and install them yourself. Removing a package does not uninstall
  its dependencies.
- Records written by earlier versions keep working; the new record fields
  simply do not exist there.
