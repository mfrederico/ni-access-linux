#!/bin/bash
# Kill old processes and restart daemon + Native Access

pkill -9 -f ni_daemon 2>/dev/null
pkill -9 -f "electron.*app.asar" 2>/dev/null
sleep 2

# Start Node.js daemon
cd "$(dirname "$0")"
if [ -f ni_daemon.mjs ]; then
    node ni_daemon.mjs > /tmp/ni-daemon-test.log 2>&1 &
    echo "Started Node.js daemon"
else
    python3 ni_daemon.py > /tmp/ni-daemon-test.log 2>&1 &
    echo "Started Python daemon"
fi
sleep 2

if ss -tlnp | grep -q 5146; then
    echo "Daemon ready on 5146/5563"
else
    echo "ERROR: Daemon failed to start"
    cat /tmp/ni-daemon-test.log
    exit 1
fi

# Start Native Access
~/.npm/_npx/2bdc30518a6e5da9/node_modules/electron/dist/electron --no-sandbox ~/Downloads/t/native-access-linux/resources/app.asar > /tmp/na-electron.log 2>&1 &
sleep 8

echo "=== Daemon log ==="
tail -20 /tmp/ni-daemon-test.log
echo ""
echo "=== NA log (key lines) ==="
grep -iE "version|daemon|deploy|login|error|success|initialized|subscriber|STARTUP|Heartbeat" ~/.local/share/Electron/logs/native-access.log 2>/dev/null | tail -20
