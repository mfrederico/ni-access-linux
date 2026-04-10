# NI Access for Linux

Run [Native Instruments](https://www.native-instruments.com) Native Access and your NI plugins on Linux. Browse your product library, download installers, and use your plugins in Bitwig, Ardour, or any Linux DAW via [yabridge](https://github.com/robbert-vdh/yabridge).

This project includes:
- **Native Access** running natively via Linux Electron (no Wine for the UI)
- **Node.js NTKDaemon** replacement that handles the ZMQ protocol
- **Web UI** for quick product browsing and downloads
- **Automatic download** with progress bar through Native Access

## Quick Start

### Prerequisites
- **Node.js** 18+ (`node --version`)
- **Steam** with Proton installed (any 9.0+)
- **yabridge** — [download from GitHub releases](https://github.com/robbert-vdh/yabridge/releases)
- **p7zip** — `sudo apt install p7zip-full`

### 1. Clone and install

```bash
git clone https://github.com/mfrederico/ni-access-linux.git
cd ni-access-linux

# Install daemon dependencies
cd daemon-test && npm install && cd ..

# Install Python dependencies (for web UI)
pip install requests
```

### 2. Set up Native Access

```bash
# Download the Windows installer
wget https://www.native-instruments.com/fileadmin/downloads/Native-Access_2.exe

# Extract it
7z x Native-Access_2.exe -o./ni-installer
7z x './ni-installer/$PLUGINSDIR/app-64.7z' -o./ni-installer/extracted

# Extract the Electron app
npx --yes @electron/asar extract ./ni-installer/extracted/resources/app.asar ./app-extracted

# Install Electron 40.6.0
npx --yes electron@40.6.0 --version
```

### 3. Patch Native Access for Linux

Apply these patches to `app-extracted/out/main/index.js`:

**Patch 1** — Add Linux path support (~line 118):
```javascript
      case "linux":
        return H.join(process.env.XDG_CONFIG_HOME ?? H.join(process.env.HOME ?? "", ".config"), ...t);
```

**Patch 2** — Add Linux public data path (~line 135):
```javascript
    case "linux":
      return H.join(process.env.XDG_DATA_HOME ?? H.join(process.env.HOME ?? "", ".local", "share"), e);
```

**Patch 3** — Add Linux shared documents path (~line 8788):
```javascript
    case "linux":
      return H.resolve(process.env.XDG_DATA_HOME ?? H.join(process.env.HOME ?? "", ".local", "share"));
```

**Patch 4** — Add `"linux"` to supported platforms (~line 8873):
```javascript
const ul = ["darwin", "win32", "linux"];
```

**Patch 5** — Platform lookup fallback (~line 8857):
```javascript
function _e(e) {
  const t = re.platform();
  dl(t);
  if (t === "linux") return e["linux"] ?? e["win32"] ?? e["darwin"];
  return e[t];
}
```

**Patch 6** — Force packaged mode on Linux (~line 10211):
```javascript
  if (w.isPackaged || process.platform === "linux") return Lp;
```

**Patch 7** — Add `xdg-open` for Linux (~line 13030):
```javascript
  return ["darwin", "win32", "linux"].includes(t) ? ao(
    `${t === "darwin" ? "open" : t === "linux" ? "xdg-open" : "start"} "${e}"`,
```

Then repack:
```bash
npx --yes @electron/asar pack ./app-extracted ./native-access-linux/resources/app.asar
cp -r ./ni-installer/extracted/resources/app.asar.unpacked ./native-access-linux/resources/
```

### 4. Run

```bash
# Terminal 1: Start the daemon
cd daemon-test && node ni_daemon.mjs

# Terminal 2: Start Native Access
~/.npm/_npx/*/node_modules/electron/dist/electron --no-sandbox ./native-access-linux/resources/app.asar
```

Or use the restart script:
```bash
bash daemon-test/restart.sh
```

## Installing Plugins (yabridge)

All NI plugins are installed via **Steam + Proton + yabridge** for full GUI support.

### One-time setup

```bash
# 1. Install yabridge to ~/.local/share/yabridge/
# Download from: https://github.com/robbert-vdh/yabridge/releases

# 2. Create Wine wrapper scripts pointing to Proton
mkdir -p ~/.local/bin

cat > ~/.local/bin/wine << 'EOF'
#!/bin/bash
PROTON="$HOME/.steam/steam/steamapps/common/Proton Hotfix/files"
export WINEDLLPATH="$PROTON/lib64/wine/x86_64-unix:$PROTON/lib/wine/x86_64-unix:$PROTON/lib/wine/i386-unix"
export LD_LIBRARY_PATH="$PROTON/lib64:$PROTON/lib:${LD_LIBRARY_PATH:-}"
exec "$PROTON/bin/wine" "$@"
EOF
chmod +x ~/.local/bin/wine

cat > ~/.local/bin/wineserver << 'EOF'
#!/bin/bash
PROTON="$HOME/.steam/steam/steamapps/common/Proton Hotfix/files"
export LD_LIBRARY_PATH="$PROTON/lib64:$PROTON/lib:${LD_LIBRARY_PATH:-}"
exec "$PROTON/bin/wineserver" "$@"
EOF
chmod +x ~/.local/bin/wineserver

# 3. Add yabridge VST3 path to Bitwig
# Settings > Plug-ins > Locations > add: ~/.vst3/yabridge
```

### Installing a plugin

1. **Download** via Native Access (click Install — downloads to `~/NI-Downloads/`)
2. **Unzip** — the daemon auto-extracts `.zip` files to `~/NI-Downloads/installers/`
3. **Add to Steam** — Library > Add Non-Steam Game > Browse to the `.exe` in `~/NI-Downloads/installers/`
4. **Set Proton** — Right-click > Properties > Compatibility > Force Proton 9.0 (or Hotfix)
5. **Run** — Click Play, complete the installer wizard
6. **Bridge** — Find the VST3 and sync yabridge:
   ```bash
   # Find where it installed
   find ~/.steam/steam/steamapps/compatdata -name "*.vst3" -path "*/VST3/*"
   
   # Add to yabridge
   yabridgectl add "<path>/drive_c/Program Files/Common Files/VST3"
   WINEPREFIX="<path>" yabridgectl sync
   ```
7. **Rescan** in Bitwig — your plugin appears with full GUI

## Web UI (Alternative)

For a lightweight experience without Native Access:

```bash
pip install requests
python3 ni_access.py
# Open http://localhost:6510
```

## Architecture

```
┌─────────────────────┐     ┌──────────────────┐
│   Native Access      │────▶│  Node.js Daemon   │
│   (Electron 40.6.0)  │◀────│  (ni_daemon.mjs)  │
│                       │     │                    │
│   Patched app.asar   │     │  ZMQ REQ/REP :5146 │
│   Linux platform     │     │  ZMQ PUB     :5563 │
└─────────────────────┘     └────────┬─────────┘
                                      │
                              ┌───────▼────────┐
                              │  NI Cloud API   │
                              │  auth/products/ │
                              │  downloads      │
                              └────────────────┘
```

## Project Structure

```
ni-access-linux/
├── ni_access.py           # Web UI for product browsing/downloads
├── daemon-test/
│   ├── ni_daemon.mjs      # Node.js NTKDaemon replacement
│   ├── package.json       # zeromq dependency
│   └── restart.sh         # Quick restart for development
└── README.md
```

## Known Limitations

- **Plugin installation** — Download is automated but installation requires adding the `.exe` as a Non-Steam Game (Proton's GUI needs Steam context)
- **Time remaining** — Download progress bar works but time estimate stays on "Calculating"
- **Some legacy products** — Older NI products may not appear (retired API entries)
- **Plugin activation** — Kontakt requires license activation through NI's servers; the daemon handles auth but full activation via kreator IPC is still in progress

## Contributing

Key areas that need help:

1. **Automated Proton installation** — Make `proton run` work for GUI installers without Steam
2. **Plugin activation** — Complete the kreator IPC protocol for license verification
3. **Content library management** — Extract and register Kontakt libraries from ISOs
4. **Distro testing** — Test on Fedora, Arch, etc.
5. **Progress bar refinement** — Time remaining estimate, smoother updates

## License

MIT

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Native Instruments GmbH. It is an independent tool that uses publicly available APIs to help Linux users access products they have purchased. You must own valid licenses for any NI products you use.
