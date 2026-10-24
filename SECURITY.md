# Security Policy

## Supported versions

Security fixes are provided for the latest release line (`0.1.x`). Older releases may work but
are not actively supported.

| Version | Supported |
|---|---|
| latest `0.1.x` | ✅ |
| older releases | ❌ |

## Reporting a vulnerability

**Do not open a public issue for security-sensitive reports.**

Please report privately via GitHub:

1. Open the repository's **Security** tab.
2. Click **Report a vulnerability** (GitHub Security Advisories), or
3. If advisories are unavailable, contact the maintainer through the GitHub profile
   [@ZJW11398925](https://github.com/ZJW11398925).

Include:

- affected version (`/retrieval/ping` reports it),
- Zotero version and OS,
- a minimal reproduction (ideally without private documents),
- impact assessment.

You can expect an acknowledgement within 72 hours and a coordinated disclosure timeline for
confirmed issues.

## Security model

Retrieval Index is designed as a **local-only** tool:

- It registers HTTP endpoints on Zotero's built-in local server (default `127.0.0.1:23119`).
  Anyone who can reach that port can query the index, so do not expose it to untrusted networks.
- The index under the Zotero profile contains **text extracted from your library**. Treat it as
  sensitive as the library itself.
- The plugin does not collect telemetry and has no remote update channel of its own; releases are
  distributed via GitHub Releases.
- Optional helpers (`tools/retrieval_proxy.py`, if you add one) are not part of the supported
  attack surface and should be restricted to trusted hosts if used.

## Hardening tips

- Keep Zotero and the plugin up to date.
- Do not run the local server on a public interface.
- If multiple users share a machine, protect the Zotero profile directory with OS permissions.
- Review helper scripts before running them; they operate on local files.
