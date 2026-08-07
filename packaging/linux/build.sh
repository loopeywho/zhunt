#!/bin/sh
set -eu

if [ "$(uname -s)" != "Linux" ]; then
    echo "Zhunt Linux packages must be built on Linux." >&2
    exit 1
fi

ARCH=$(uname -m)
if [ "$ARCH" != "x86_64" ]; then
    echo "This preview build targets Linux x86_64; detected $ARCH." >&2
    exit 1
fi

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
BUILD_ROOT="$ROOT/.linux-build"
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
"$PY" -m pip install "$ROOT" "pyinstaller>=6.10,<7"

"$PY" -m PyInstaller \
    --noconfirm \
    --clean \
    --onedir \
    --name zhunt \
    --distpath "$DIST" \
    --workpath "$WORK" \
    --specpath "$WORK" \
    --collect-all zhunt \
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
    "$ROOT/packaging/linux/entrypoint.py"

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
cp "$ROOT/packaging/linux/README.md" "$PACKAGE/README-linux.md"

ARCHIVE="$OUTPUT/Zhunt-Setup-linux-x64.tar.gz"
tar -C "$DIST" -czf "$ARCHIVE" zhunt
sha256sum "$ARCHIVE" | awk '{print $1 "  " $2}' > "$ARCHIVE.sha256"

echo "Built Linux x86_64 package:"
echo "  $ARCHIVE"
