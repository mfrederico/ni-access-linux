# NI Access for Linux

A lightweight web-based tool to browse, download, and install your [Native Instruments](https://www.native-instruments.com) products on Linux.

**No Wine required for the UI.** Several NI products (Kontakt 8, Reaktor 6, etc.) ship native Linux `.deb` installers — this tool lets you download and install them directly.

## Features

- Sign in with your existing Native Instruments account
- Browse all products you own with available downloads
- Native Linux (`.deb`) installers highlighted automatically
- Download via `aria2c` (multi-connection, resume support) or Python fallback
- One-click `.deb` installation
- Session persistence (stay logged in between restarts)

## Quick Start

```bash
# Install dependencies
pip install requests
sudo apt install aria2    # optional but recommended for fast downloads

# Run
python3 ni_access.py

# Open in browser
xdg-open http://localhost:6510
```

## Screenshot

After login, you'll see your product library with platform badges and download options:
- **Linux Native** (green) — `.deb` installer, installs directly
- **Windows/Wine** (red) — Windows installer, needs Wine + yabridge for DAW use

## How It Works

NI Access for Linux talks directly to the Native Instruments API:

1. **Authentication** — OAuth2 password grant via Auth0 (`auth.native-instruments.com`)
2. **Product listing** — REST API at `api.native-instruments.com`
3. **Downloads** — Metalink format with signed CDN URLs, compatible with `aria2c`

Your credentials are sent directly to NI's servers. Only the session token is stored locally (`~/.ni-access-token.json`).

## Using NI Plugins in a Linux DAW (Bitwig, Ardour, etc.)

### Native Linux plugins (.deb)
Some NI products ship native Linux builds. After downloading and installing the `.deb`, the plugins appear directly in your DAW.

### Windows-only plugins (via Wine + yabridge)
For products without native Linux builds:

1. Install [yabridge](https://github.com/robbert-vdh/yabridge) 
2. Download the Windows (PC) installer from this tool
3. Install under Wine: `wine installer.exe`
4. Run `yabridgectl sync`
5. Add `~/.vst3/yabridge` to your DAW's plugin locations

## Requirements

- Python 3.8+
- `requests` module (`pip install requests`)
- `aria2c` (optional, recommended: `sudo apt install aria2`)
- A Native Instruments account with purchased products

## License

MIT

## Disclaimer

This project is not affiliated with, endorsed by, or connected to Native Instruments GmbH. It is an independent tool that uses publicly available APIs to help Linux users access products they have purchased.
