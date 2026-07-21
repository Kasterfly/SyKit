# Security Policy

## Supported versions

SyKit 0.14.x is the patch-only release-candidate line. Security fixes are
provided for its latest patch while the 1.0 compatibility candidate soaks.
Older beta lines should update before reporting behavior that is already fixed.

After 1.0.0, the latest 1.0.x patch receives best-effort security fixes while
the stable line remains supported. Any end-of-support date will be announced in
advance; no response or fix-time SLA is offered.

| Version | Security fixes |
| --- | --- |
| 0.14.x | Yes |
| 0.13.x and older | No |

## Private reports

Do not open a public issue for a suspected vulnerability. Use the repository's
private vulnerability report form:

https://github.com/Kasterfly/SyKit/security/advisories/new

Include the affected SyKit version, operating system, Python and Node versions,
deployment shape, reproduction steps, and expected impact. Remove secrets,
session cookies, API keys, and personal data from evidence.

The maintainer will acknowledge a complete report when practical, coordinate a
fix and disclosure, and credit the reporter unless anonymity is requested. No
response-time guarantee is offered while SyKit remains a side project.
