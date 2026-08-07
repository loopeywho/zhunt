# Linux x86_64 packaging

The Linux preview is a portable x86_64 `.tar.gz` bundle. It does not require a
system Python installation, but it is intentionally not a distro-specific
`.deb` or `.rpm` package yet.

Build on a Linux x86_64 host with Python 3.12:

```sh
sh packaging/linux/build.sh
```

Extract the archive and run the local setup or daemon:

```sh
tar -xzf dist/Zhunt-Setup-linux-x64.tar.gz
./zhunt/zhunt setup
```

Verify the extracted package before distributing it:

```sh
sh packaging/linux/verify.sh ./zhunt
```

The bundle contains no provider keys or master key. Credentials are generated
or entered at runtime under `~/.zhunt`. The current preview targets Linux
x86_64; an ARM64 build and distro-native packages require separate runners and
validation.
