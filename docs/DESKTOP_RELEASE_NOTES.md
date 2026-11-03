# Zhunt 0.2.0.dev0 desktop preview

Free local workspace with portable downloads for Apple Silicon macOS, Windows x64 and Linux x86_64. Python is bundled; no separate Python or uv install is needed.

- Mac: extract the whole archive and open `zhunt/Zhunt.command`.
- Windows: Extract All, then open `zhunt/Start-Zhunt.cmd`.
- Linux: extract and run `./zhunt/Start-Zhunt.sh` in a terminal. Requires glibc 2.35 or newer.

The launcher starts a loopback-only service and opens your workspace in your browser. Keep its terminal window open; Ctrl+C stops it. Add provider keys in the workspace if desired. Provider availability and terms apply; no live inference is promised by a startup test.

## Preview limits

Unsigned and not notarized: macOS and Windows may show security warnings. Review SHA-256 sidecars and only approve software you trust. Native OS quarantine/security-warning flows have not been certified.

Integrated Engineering terminals are not supported in these frozen portable previews. Use the source install on macOS/Linux for integrated terminals. Windows integrated terminals are not supported. No Intel Mac or Windows ARM package is included.

Fresh-home startup, authenticated local status and rendered workspace passed on Mac arm64 and Windows 11 x64 with native Chrome. Linux x64 WSL2 startup/API passed; its rendered UI was checked in Chrome on macOS through a loopback SSH tunnel, not in a native Linux desktop browser. This is not full agent/chat/provider or upgrade/uninstall certification.

Android and iOS are under development; there are no mobile downloads in this release. Desktop does not wait on mobile.
