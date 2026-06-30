# Security Policy

VoiceFlow is a fully **local, offline** application: audio is captured and
transcribed on your own machine and is never uploaded. We still take security
seriously — input hooks, clipboard access, on-demand downloads, and the
auto-update checker all touch sensitive surfaces.

## Supported versions

We provide security fixes for the **latest released minor version** on the
`main` line. Older versions are not patched; please upgrade to the latest
release.

| Version | Supported |
|---|---|
| Latest release (`main`) | ✅ |
| Older releases | ❌ — please upgrade |

## Reporting a vulnerability

**Please do not open a public GitHub issue for security vulnerabilities.**

Instead, report privately via one of:

1. **GitHub Security Advisories** (preferred): use the repository's
   **Security → Report a vulnerability** ("Report a vulnerability" /
   private vulnerability reporting) button to open a private advisory.
2. **Email:** `security@voiceflow.example` *(replace with the project's real
   security contact before launch)*.

Please include:

- A description of the issue and its potential impact.
- Steps to reproduce (a proof of concept if possible).
- Affected version(s), OS, and session type (X11/Wayland on Linux).
- Any suggested remediation, if you have one.

### What to expect

- **Acknowledgement** within **3 business days**.
- An initial **assessment** (severity, affected versions) within **7 business
  days**.
- We will keep you updated on progress and coordinate a disclosure timeline with
  you. We aim to ship a fix and publish an advisory within **90 days** of a valid
  report, sooner for high-severity issues.
- With your permission, we will **credit** you in the advisory and release notes.

Please give us a reasonable opportunity to remediate before any public
disclosure. We will not pursue legal action against good-faith security research
that respects this policy and avoids privacy violations, data destruction, and
service disruption.

## Security-relevant areas of VoiceFlow

If you're looking for where to investigate, these surfaces are the most
security-relevant:

- **Input hooks / global hotkeys** (`voiceflow/platform/*`) — low-level keyboard
  and mouse hooks.
- **Clipboard save/restore** (`platform/*` clipboard backends) — reads and
  rewrites arbitrary clipboard contents.
- **Synthetic paste** (`Paster` implementations) — injects keystrokes into the
  focused window; note the documented Windows UIPI/UAC behavior.
- **On-demand downloads** — model weights from Hugging Face and (on GPU opt-in)
  the NVIDIA CUDA runtime wheels.
- **Update checker** — queries the GitHub Releases API and surfaces a download
  link.

## Out of scope

- Vulnerabilities in third-party dependencies should be reported upstream first
  (we will help coordinate if VoiceFlow is affected).
- Issues requiring a pre-compromised machine, physical access, or a malicious
  local administrator.
- The inherent ability of a dictation tool to type into the focused window — that
  is its purpose; report only if it can be abused beyond the user's intent.
