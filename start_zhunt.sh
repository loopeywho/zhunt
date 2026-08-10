#!/bin/bash
set -a
source /Users/loopey/Documents/Zhunt/.venv/bin/activate
source /Users/loopey/.zhunt/env
set +a
PYTHONPATH=""
exec /Users/loopey/Documents/Zhunt/.venv/bin/zhunt serve --port 4000 --host 127.0.0.1