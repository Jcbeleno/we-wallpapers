#!/bin/sh
# Entorno de pruebas para hablar con el daemon de waywallen (protobuf sobre WebSocket).
# control.proto/filter.proto vienen de github.com/waywallen/waywallen (MIT, ver proto/LICENSE-waywallen).
set -e
cd "$(dirname "$0")"
ROOT="$(cd ../.. && pwd)"
python3 -m venv --system-site-packages "$ROOT/.venv"  # gi y Pillow vienen del sistema
"$ROOT/.venv/bin/pip" -q install protobuf websockets
mkdir -p wwpb
protoc -I proto --python_out=wwpb proto/control.proto proto/filter.proto
