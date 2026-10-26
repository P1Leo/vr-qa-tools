#!/bin/bash
# Double-click to start VR Log Inspector on macOS.
cd "$(dirname "$0")" || exit 1
PY=""
for c in python3 python; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' >/dev/null 2>&1; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "  Python 3.8 or newer was not found."
  echo "  Install it from https://www.python.org/downloads/macos/ (or: brew install python) and run this again."
  echo
  open "https://www.python.org/downloads/macos/"
  read -r -p "Press Enter to close this window."
  exit 1
fi
echo "Starting VR Log Inspector. Keep this window open while you use it; close it to stop the tool."
export VRLI_LAUNCHER=1
while true; do
  "$PY" vr_log_inspector.py "$@"
  status=$?
  [ $status -eq 75 ] || break      # 75 = restart after an update
done
if [ $status -ne 0 ]; then
  echo
  echo "  VR Log Inspector stopped with an error. Read the message above."
  read -r -p "Press Enter to close this window."
fi
exit $status
