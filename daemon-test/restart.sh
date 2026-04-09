#!/bin/bash
# Kill old processes and restart daemon + Native Access
# Run this after making changes to ni_daemon.py

pkill -9 -f ni_daemon.py 2>/dev/null
pkill -9 -f "electron.*app.asar" 2>/dev/null
sleep 2

# Start daemon
python3 ~/Downloads/ni-access-linux/daemon-test/ni_daemon.py > /tmp/ni-daemon-test.log 2>&1 &
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
grep -iE "version|daemon|deploy|login|error|success|initialized" ~/.local/share/Electron/logs/native-access.log 2>/dev/null | tail -20
