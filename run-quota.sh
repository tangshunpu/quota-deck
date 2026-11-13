#!/bin/bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
exec sudo /usr/bin/python3 "$PWD/standalone.py" --quota
