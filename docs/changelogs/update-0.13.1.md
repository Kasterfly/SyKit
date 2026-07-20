# SyKit 0.13.1 - Container CI Fix

SyKit 0.13.1 is a small patch for the generated-container GitHub Actions job.

## Fixed

- The container job invokes the repository checkout with `python . init` and
  `python . build`.
- The workflow no longer assumes that the checkout root contains another
  folder named `SyKit`.
- A regression test keeps both checkout-root commands in the workflow.

There are no runtime API, dependency, configuration, or persistent-state
changes in this patch.
