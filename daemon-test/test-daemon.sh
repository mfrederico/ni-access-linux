#!/bin/bash
# Test if the NTKDaemon runs and listens on ZMQ ports
# Returns 0 on success, 1 on failure
# Writes results to test-result.json

PROTON_DIR="$HOME/.steam/steam/steamapps/common/Proton 9.0 (Beta)/files"
PREFIX="$HOME/.steam/steam/steamapps/compatdata/3486537896/pfx"
DAEMON="$PREFIX/drive_c/Program Files/Native Instruments/NTK Daemon/NTKDaemon.exe"
RESULT_FILE="$(dirname "$0")/test-result.json"
LOG_FILE="$(dirname "$0")/daemon-test.log"

export WINEPREFIX="$PREFIX"
export PATH="$PROTON_DIR/bin:$PATH"
export WINEDLLPATH="$PROTON_DIR/lib64/wine/x86_64-unix:$PROTON_DIR/lib/wine/i386-unix"
export LD_LIBRARY_PATH="$PROTON_DIR/lib64:$PROTON_DIR/lib:${LD_LIBRARY_PATH:-}"

# Kill any existing daemon
"$PROTON_DIR/bin/wineserver" -k 2>/dev/null
sleep 2

# Run daemon
"$PROTON_DIR/bin/wine64" "$DAEMON" < /dev/null > "$LOG_FILE" 2>&1 &
PID=$!
sleep 12

# Check results
ALIVE=$(ps -p $PID > /dev/null 2>&1 && echo "true" || echo "false")
PORTS=$(ss -tlnp 2>/dev/null | grep -cE "5146|5563")
HAS_LOG=$(find "$PREFIX/drive_c" -name "daemon.log" -newer "$0" 2>/dev/null | head -1)
DAEMON_LOG=""
if [[ -n "$HAS_LOG" ]]; then
    DAEMON_LOG=$(tail -5 "$HAS_LOG" 2>/dev/null)
fi
CRASH_INFO=$(grep -E "Unhandled|page fault|fault on|Exception|STATUS_" "$LOG_FILE" 2>/dev/null | head -3)
LAST_CALL=$(grep "^00" "$LOG_FILE" 2>/dev/null | tail -5)

# Write result
cat > "$RESULT_FILE" << EOF
{
  "timestamp": "$(date -Iseconds)",
  "alive": $ALIVE,
  "ports_listening": $PORTS,
  "has_daemon_log": $([ -n "$HAS_LOG" ] && echo true || echo false),
  "daemon_log": $(echo "$DAEMON_LOG" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""'),
  "crash_info": $(echo "$CRASH_INFO" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""'),
  "last_wine_calls": $(echo "$LAST_CALL" | python3 -c "import json,sys; print(json.dumps(sys.stdin.read()))" 2>/dev/null || echo '""')
}
EOF

# Cleanup
"$PROTON_DIR/bin/wineserver" -k 2>/dev/null

if [[ "$ALIVE" == "true" && "$PORTS" -gt 0 ]]; then
    echo "SUCCESS: Daemon is running and listening!"
    exit 0
else
    echo "FAIL: alive=$ALIVE ports=$PORTS"
    [[ -n "$CRASH_INFO" ]] && echo "Crash: $CRASH_INFO"
    exit 1
fi
