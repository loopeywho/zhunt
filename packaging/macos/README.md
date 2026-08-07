# macOS packaging

`build.sh` creates a signed-by-the-build-host-independent, unsigned installer
pair for the architecture of the Mac running it:

```sh
sh packaging/macos/build.sh
```

On Apple Silicon this produces `Zhunt-Setup-macos-arm64.dmg` and
`Zhunt-Setup-macos-arm64.pkg`. The package installs the self-contained CLI at
`/usr/local/bin/zhunt`; the daemon remains loopback-only by default and creates
`~/.zhunt/env` on first run. No provider or master keys are bundled.

This is not yet a Universal 2 build. An Intel build must be produced separately
on an x86_64 macOS runner and then combined and independently tested before the
website can call the download “macOS Universal”. The packages are unsigned in
this repository; public distribution needs a Developer ID signing and
notarization step with the project's Apple credentials.

The DMG contains the PKG and this instruction sheet. Verify an artifact before
sharing it:

```sh
shasum -a 256 dist/Zhunt-Setup-macos-*.dmg
pkgutil --check-signature dist/Zhunt-Setup-macos-*.pkg
```
