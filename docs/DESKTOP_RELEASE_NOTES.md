# Zhunt 0.2.0.dev0 desktop preview

Portable previews for Apple Silicon macOS and Linux x86_64. Python is bundled; no separate Python or uv install is needed. These are newly built archives, not the August legacy release.

- **Mac arm64:** extract the whole `Zhunt-Portable-macos-arm64.tar.gz` archive and open `zhunt/Zhunt.command`.
- **Linux x86_64:** extract `Zhunt-Setup-linux-x64.tar.gz` and run `./zhunt/Start-Zhunt.sh` in a terminal. Built on Ubuntu 22.04 WSL2 / glibc 2.35; requires glibc 2.35 or newer.
- **Windows:** new binary verification is pending because the QC Windows SSH host was unreachable. No Windows binary is included or advertised as tested in this release. Source installation remains available in the repository README. Integrated Windows terminals remain unsupported.
- **Android / iOS:** under development; no mobile downloads.

The launcher starts a loopback-only service and opens the workspace in your browser. Keep the launcher terminal open; Ctrl+C stops it. Provider keys are optional setup; availability and provider terms apply.

## Verified for these bytes

Both archives passed clean-HOME shipped-launcher startup, authenticated status and real API-driven Engineering PTY checks: shell file execution, cwd with spaces, controlling TTY, resize, interrupt and child cleanup. Mac workspace rendered in native Chrome without page errors. Linux verification used WSL2, not a native graphical Linux desktop. These checks do not certify browser terminal keyboard interaction. The source suite passed 765 tests (one existing Starlette/httpx deprecation warning).

| Archive | SHA-256 |
|---|---|
| `Zhunt-Portable-macos-arm64.tar.gz` | `c4366ff74ceab010c60b56726e95715bf1a143c17d7e4996b9b3b1d78733225e` |
| `Zhunt-Setup-linux-x64.tar.gz` | `17acb718543f2e7c8142e49a0ba12f3707504f2ac394add7e97e682cb2d3f369` |

Matching `.sha256` sidecars are included. On macOS use `shasum -a 256 -c ARCHIVE.sha256`; on Linux use `sha256sum -c ARCHIVE.sha256` in the download directory.

## Preview limitations

Unsigned and not notarized. Gatekeeper/quarantine approval flows and automatic browser handoff have not been certified; browser rendering was checked separately. No native Mac PKG/DMG is published: installation/uninstallation was not validated. Intel Mac, native Linux browser integration, browser terminal keyboard interaction, upgrade/uninstall lifecycle and full provider/chat/agent execution are not certified. No provider calls were made in these tests, and startup verification is not full product certification.

The Mac/Linux frozen-terminal dispatch repair is included; older baseline and legacy binaries do not inherit it. See `docs/DESKTOP_TERMINAL_REVIEW.md` for artifact-specific evidence and historical repair details.
