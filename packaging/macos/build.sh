#!/bin/sh
set -eu

if [ "$(uname -s)" != "Darwin" ]; then
    echo "Zhunt macOS packages must be built on macOS." >&2
    exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
ARCH=$(uname -m)
case "$ARCH" in
    arm64|x86_64) ;;
    *) echo "Unsupported macOS architecture: $ARCH" >&2; exit 1 ;;
esac

BUILD_ROOT="$ROOT/.macos-build"
VENV="$BUILD_ROOT/venv"
DIST="$BUILD_ROOT/dist"
WORK="$BUILD_ROOT/work"
OUTPUT="$ROOT/dist"

rm -rf "$BUILD_ROOT"
mkdir -p "$BUILD_ROOT" "$OUTPUT"

PYTHON=${PYTHON:-python3.12}
"$PYTHON" -m venv "$VENV"
PY="$VENV/bin/python"
"$PY" -m pip install --upgrade pip
"$PY" -m pip install "$ROOT[desktop]" "pyinstaller>=6.10,<7"

"$PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name zhunt \
    --distpath "$DIST" \
    --workpath "$WORK" \
    --specpath "$WORK" \
    --collect-all zhunt \
    --collect-all pystray \
    --collect-all PIL \
    --collect-all litellm \
    --collect-all fastapi \
    --collect-all uvicorn \
    --collect-all typer \
    --collect-all ruamel.yaml \
    --collect-all yaml \
    --collect-all tiktoken \
    --hidden-import tiktoken_ext.openai_public \
    --add-data "$ROOT/models.yaml:zhunt" \
    --hidden-import uvicorn.logging \
    --hidden-import uvicorn.loops.auto \
    --hidden-import uvicorn.protocols.http.auto \
    --hidden-import uvicorn.protocols.websockets.auto \
    "$ROOT/packaging/windows/entrypoint.py"

PACKAGE="$DIST/zhunt"
if [ ! -x "$PACKAGE/zhunt" ]; then
    echo "PyInstaller did not produce the zhunt executable." >&2
    exit 1
fi
if [ ! -f "$PACKAGE/_internal/zhunt/models.yaml" ] && [ ! -f "$PACKAGE/zhunt/models.yaml" ]; then
    echo "PyInstaller output is missing zhunt/models.yaml." >&2
    exit 1
fi

cp "$ROOT/README.md" "$PACKAGE/README.md"
cp "$ROOT/LICENSE" "$PACKAGE/LICENSE"

PAYLOAD="$BUILD_ROOT/payload"
mkdir -p "$PAYLOAD/usr/local/lib/zhunt" "$PAYLOAD/usr/local/bin"
cp -R "$PACKAGE"/. "$PAYLOAD/usr/local/lib/zhunt/"
cp "$ROOT/packaging/macos/uninstall.sh" "$PAYLOAD/usr/local/bin/zhunt-uninstall"
chmod 755 "$PAYLOAD/usr/local/bin/zhunt-uninstall"
cat > "$PAYLOAD/usr/local/bin/zhunt" <<'EOF'
#!/bin/sh
exec /usr/local/lib/zhunt/zhunt "$@"
EOF
chmod 755 "$PAYLOAD/usr/local/bin/zhunt"

PKG="$OUTPUT/Zhunt-Setup-macos-$ARCH.pkg"
pkgbuild \
    --root "$PAYLOAD" \
    --identifier com.kindredwildcat.zhunt \
    --version 0.1.0 \
    --install-location / \
    "$PKG"

DMG_STAGE="$BUILD_ROOT/dmg"
mkdir -p "$DMG_STAGE"
cp "$PKG" "$DMG_STAGE/"
cp "$ROOT/packaging/macos/README.md" "$DMG_STAGE/README.md"
DMG="$OUTPUT/Zhunt-Setup-macos-$ARCH.dmg"
hdiutil create \
    -volname "Zhunt macOS $ARCH" \
    -srcfolder "$DMG_STAGE" \
    -ov \
    -format UDZO \
    "$DMG" >/dev/null

(cd "$OUTPUT" && shasum -a 256 "$(basename "$PKG")") > "$PKG.sha256"
(cd "$OUTPUT" && shasum -a 256 "$(basename "$DMG")") > "$DMG.sha256"

echo "Built macOS $ARCH packages:"
echo "  $DMG"
echo "  $PKG"
