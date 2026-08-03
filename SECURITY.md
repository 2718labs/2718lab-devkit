# Security policy

## Supported line

Security fixes are made on the current `main` branch and then included in the
next versioned release. Historical prereleases are not backported unless a
maintainer explicitly says so.

## Report a vulnerability

Use GitHub's private vulnerability reporting flow for this repository. If that
is unavailable, open a minimal public issue only when it cannot expose an
exploit, credentials, host-private handles, workspace paths, receipts, or raw
quota data. Do not publish a proof of concept that bypasses a host boundary.

Useful reports include the affected version/commit, a minimal safe
reproduction, expected versus actual behavior, impact, and a suggested
mitigation. Maintainers will acknowledge, assess, and coordinate a fix before
public disclosure.

## Scope

High-priority issues include unsafe lifecycle start/refill/recovery, forged
acceptance or host evidence, cross-project data leakage, unsafe cache cleanup,
credential exposure, and package allowlist bypasses. The local host application
and third-party review services are separate systems; report their defects to
their respective owners as well.
