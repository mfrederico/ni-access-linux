# NI Access for Linux

Run [Native Instruments](https://www.native-instruments.com) Native Access and your NI plugins natively on Linux. Browse your product library, download installers, and use your plugins in Bitwig, Ardour, or any Linux DAW.

This project includes:
- **Native Access** running natively via Linux Electron (no Wine for the UI)
- **Python NTKDaemon** replacement that handles the ZMQ protocol
- **Web UI** for quick product browsing and downloads
- **Setup scripts** for dependency installation

## Quick Start

### 1. Clone and install dependencies

```bash
git clone https://github.com/mfrederico/ni-access-linux.git
cd ni-access-linux

pip install requests pyzmq
sudo apt install p7zip-full    # for extracting installers
sudo apt install aria2         # optional, faster downloads
```

### 2. Download and extract Native Access

```bash
# Download the Windows installer
wget https://www.native-instruments.com/fileadmin/downloads/Native-Access_2.exe

# Extract it
7z x Native-Access_2.exe -o./ni-installer
7z x './ni-installer/$PLUGINSDIR/app-64.7z' -o./ni-installer/extracted

# Extract and patch the Electron app
npx --yes @electron/asar extract ./ni-installer/extracted/resources/app.asar ./app-extracted
```

### 3. Patch Native Access for Linux

Apply the Linux platform patches to `app-extracted/out/main/index.js`:

**Patch 1** — Add Linux path support (~line 118): after the `case "win32":` return statement, add:
```javascript
      case "linux":
        return H.join(process.env.XDG_CONFIG_HOME ?? H.join(process.env.HOME ?? "", ".config"), ...t);
```

**Patch 2** — Add Linux public data path (~line 135): after the `case "win32":` return statement, add:
```javascript
    case "linux":
      return H.join(process.env.XDG_DATA_HOME ?? H.join(process.env.HOME ?? "", ".local", "share"), e);
```

**Patch 3** — Add Linux to shared documents path (~line 8788): after the `case "win32":` return statement, add:
```javascript
    case "linux":
      return H.resolve(process.env.XDG_DATA_HOME ?? H.join(process.env.HOME ?? "", ".local", "share"));
```

**Patch 4** — Add `"linux"` to the supported platforms array (~line 8873):
```javascript
const ul = [
  "darwin",
  "win32",
  "linux"
];
```

**Patch 5** — Make platform lookup fall back to win32 for Linux (~line 8857):
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
function mu() {
  if (w.isPackaged || process.platform === "linux") return Lp;
```

**Patch 7** — Add `xdg-open` for Linux (~line 13030):
```javascript
  return ["darwin", "win32", "linux"].includes(t) ? ao(
    `${t === "darwin" ? "open" : t === "linux" ? "xdg-open" : "start"} "${e}"`,
```

Then repack the asar:
```bash
npx --yes @electron/asar pack ./app-extracted ./native-access-linux/resources/app.asar
cp -r ./ni-installer/extracted/resources/app.asar.unpacked ./native-access-linux/resources/
```

### 4. Install Electron

```bash
npx --yes electron@40.6.0 --version   # downloads Electron 40.6.0
```

### 5. Start the daemon and Native Access

```bash
# Terminal 1: Start the Python NTKDaemon
python3 daemon-test/ni_daemon.py

# Terminal 2: Start Native Access
~/.npm/_npx/*/node_modules/electron/dist/electron --no-sandbox ./native-access-linux/resources/app.asar
```

Or use the restart script:
```bash
bash daemon-test/restart.sh
```

Native Access will open, show a login screen, and after you log in with your NI account, your product library will appear with names and artwork.

## Installing NI Plugins

### Native Linux plugins (.deb)

Several NI products ship native Linux `.deb` installers (Kontakt 8, Reaktor 6, FM8, and many effects). To install them:

```bash
# 1. Install the ni-plugin-info stub (required dependency)
sudo dpkg -i ni-plugin-info.deb

# 2. Install compatibility libraries (Ubuntu 24.04)
sudo bash setup-deps.sh

# 3. Download and install the .deb
#    (use the web UI at localhost:6510 or download via Native Access)
sudo dpkg --force-depends -i ~/NI-Downloads/Kontakt_8_Installer.deb
```

**Note:** The native Linux builds are headless audio engines — they work in your DAW but don't have a GUI. For the full GUI experience, use the Windows version via yabridge (below).

### Windows plugins via Steam + Proton + yabridge

This is the recommended approach for plugins with GUIs (Kontakt, Ozone, Massive, etc.):

#### Prerequisites
```bash
# Install yabridge from https://github.com/robbert-vdh/yabridge/releases
# Extract to ~/.local/share/yabridge/

# Install Proton via Steam (any version 9.0+)
# Steam > Settings > Compatibility > Enable Steam Play
```

#### Install a plugin

1. Download the Windows (PC) `.zip` installer using Native Access or the web UI
2. Unzip it: `unzip ~/NI-Downloads/Kontakt_7_Installer.zip -d /tmp/kontakt-install`
3. In Steam: **Library > Add a Game > Add a Non-Steam Game**
4. Browse to the `.exe` installer (e.g., `/tmp/kontakt-install/Kontakt 7 7.9.0 Setup PC.exe`)
5. Right-click the game > **Properties > Compatibility** > Force **Proton 9.0** (or Hotfix)
6. Click **Play** — the Windows installer GUI will appear
7. Complete the installation

#### Bridge with yabridge

```bash
# Find where the VST3 was installed
find ~/.steam/steam/steamapps/compatdata -name "*.vst3" -path "*/VST3/*"

# Add the VST3 directory to yabridge
yabridgectl add "/path/to/compatdata/XXXXX/pfx/drive_c/Program Files/Common Files/VST3"

# Create a Wine wrapper (point to the same Proton used for install)
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

# Sync yabridge
WINEPREFIX="/path/to/compatdata/XXXXX/pfx" yabridgectl sync

# Verify
yabridgectl status
```

#### Configure Bitwig

1. **Settings > Plug-ins > Locations** — add `~/.vst3/yabridge`
2. **Settings > Plug-ins** — enable "Allow all plugins"
3. Click **Rescan**
4. Your NI plugins should appear with full GUIs

## Web UI (Alternative)

For a lightweight experience without Native Access:

```bash
python3 ni_access.py
# Open http://localhost:6510
```

The web UI lets you log in, browse products, and download installers directly.

## Compatibility Libraries (Ubuntu 24.04)

NI's native Linux plugins need older library versions. The `setup-deps.sh` script handles this automatically:

```bash
sudo bash setup-deps.sh
```

It installs:
- **OpenSSL 1.1** — from Ubuntu 22.04 repos (coexists with OpenSSL 3)
- **xerces-c 3.1** — extracted from .deb (avoids ICU dependency chain)
- **ICU 55** — extracted from .deb (needed by xerces-c 3.1)
- **ni-plugin-info** — stub package that satisfies NI .deb dependencies

Also create the NI registry directory:
```bash
sudo mkdir -p /ni/shared
sudo chmod 777 /ni/shared
```

## Architecture

```
┌─────────────────────┐     ┌──────────────────┐
│   Native Access      │────▶│  Python Daemon    │
│   (Electron 40.6.0)  │◀────│  (ni_daemon.py)   │
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

- **Native Access** — NI's Electron app, patched for Linux platform support
- **Python Daemon** — Replaces the Windows NTKDaemon service; speaks protobuf over ZMQ
- **NI Cloud API** — Authentication (Auth0), product catalog, download URLs (metalink/CDN)

## Project Structure

```
ni-access-linux/
├── ni_access.py           # Web UI for product browsing/downloads
├── daemon-test/
│   ├── ni_daemon.py       # Python NTKDaemon replacement
│   └── restart.sh         # Quick restart script for development
├── ni-plugin-info         # Stub for NI .deb dependency
├── ni-plugin-info.deb     # Packaged stub
├── setup-deps.sh          # Compatibility library installer
└── install-ni-linux.sh    # Legacy Wine-based installer (deprecated)
```

## Known Limitations

- **Plugin activation** — Kontakt/Reaktor require license activation through the daemon. The Python daemon handles auth but full activation flow is still in progress.
- **Native Linux plugins have no GUI** — NI's Linux builds are headless audio engines. Use Windows versions via yabridge for the full GUI.
- **Download via Native Access** — "Install" button in NA is not yet wired to the download flow. Use the web UI (`ni_access.py`) for downloads in the meantime.
- **Some legacy products** — Older products (KORE PLAYER, original Ozone 8) may not appear as their API entries have been retired.

## Contributing

This project was built by reverse-engineering NI's public APIs and protocol. Key areas that need help:

1. **Download flow in daemon** — Wire up `startDeploymentsRequest` to actually download and install products
2. **More platform patches** — Test on different distros (Fedora, Arch, etc.)
3. **Plugin activation** — Complete the kreator IPC protocol for plugin license verification
4. **Content library management** — Extract and register Kontakt libraries from ISOs

## License

MIT

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Native Instruments GmbH. It is an independent tool that uses publicly available APIs to help Linux users access products they have purchased. You must own valid licenses for any NI products you use.
