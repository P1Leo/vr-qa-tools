#!/bin/sh
# Start VR Log Inspector on Linux: ./start_vr_log_inspector.sh
cd "$(dirname "$0")" || exit 1
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    exec "$c" vr_log_inspector.py "$@"
  fi
done
echo "Python 3.8 or newer was not found. Install it with your package manager, e.g.: sudo apt install python3"
exit 1
