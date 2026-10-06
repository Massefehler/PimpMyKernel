#!/usr/bin/env bash
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
cd -- "$project_dir"
if [[ -x "$project_dir/.venv/bin/python" ]]; then
    exec "$project_dir/.venv/bin/python" -m pimpmykernel "$@"
fi
exec /usr/bin/python3 -m pimpmykernel "$@"
