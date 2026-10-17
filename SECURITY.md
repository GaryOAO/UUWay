# Security policy

## Supported versions

Security fixes are applied to the latest release and to the `main` branch when
the issue affects current code. Older releases may not receive a backport.

## Reporting a vulnerability

Please do **not** open a public issue for a suspected vulnerability. Use
[GitHub's private vulnerability reporting form](https://github.com/GaryOAO/UUWay/security/advisories/new).
If that form is unavailable, contact `62834599+GaryOAO@users.noreply.github.com`
with the subject `UUWay security report`.

Include, where possible:

- the affected release or commit;
- the component and a clear description of the impact;
- reproduction steps or a minimal proof of concept;
- relevant logs with credentials, tokens, and personal data removed; and
- any proposed mitigation.

Please allow the maintainer reasonable time to investigate and coordinate a
fix before public disclosure. Do not send passwords, private keys, or complete
UU account data in a report.

## Scope

Reports about UUWay's installer, update path, native services, package,
runtime selection, or repository automation are in scope. Vulnerabilities in
the official UU Remote client, Wine, the Linux distribution, GPU drivers, or
other upstream projects should be reported to their respective maintainers;
please mention them here only when they create a concrete UUWay impact.
