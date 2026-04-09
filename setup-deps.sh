#!/usr/bin/env bash
#
# NI Access for Linux — Dependency Setup
# Installs compatibility libraries needed by NI's native Linux plugins.
#
# Run once after first installing NI .deb packages:
#   sudo bash setup-deps.sh
#

set -euo pipefail

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'
log()  { echo -e "${GREEN}[OK]${NC} $*"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()  { echo -e "${RED}[ERROR]${NC} $*"; }
info() { echo -e "  $*"; }

if [[ $EUID -ne 0 ]]; then
    err "This script must be run as root: sudo bash setup-deps.sh"
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CACHE_DIR="/tmp/ni-deps-cache"
mkdir -p "$CACHE_DIR"

echo ""
echo "============================================================"
echo "  NI Access for Linux — Dependency Setup"
echo "============================================================"
echo ""

# ----------------------------------------------------------------
# 1. Install ni-plugin-info stub
# ----------------------------------------------------------------
if ! command -v ni-plugin-info &>/dev/null; then
    info "Installing ni-plugin-info stub..."
    if [[ -f "$SCRIPT_DIR/ni-plugin-info" ]]; then
        cp "$SCRIPT_DIR/ni-plugin-info" /usr/local/bin/ni-plugin-info
        chmod +x /usr/local/bin/ni-plugin-info
    elif [[ -f "$SCRIPT_DIR/ni-plugin-info.deb" ]]; then
        dpkg -i "$SCRIPT_DIR/ni-plugin-info.deb"
    else
        # Create inline
        cat > /usr/local/bin/ni-plugin-info << 'STUB'
#!/usr/bin/env python3
"""ni-plugin-info stub — satisfies NI .deb post-install scripts."""
import sys, os, json, hashlib
args = [a for a in sys.argv[1:] if not a.startswith('-')]
if len(args) < 2: sys.exit(0)
output_dir, plugin_path = args[0], args[1]
if not os.path.exists(plugin_path): sys.exit(1)
os.makedirs(output_dir, exist_ok=True)
name = os.path.basename(plugin_path).replace('.vst3','').replace('.so','')
info = {"name": name, "id": hashlib.md5(name.encode()).hexdigest(),
        "path": os.path.abspath(plugin_path), "arch": "x86_64-linux"}
safe = name.replace(' ','_').replace('/','_')
with open(os.path.join(output_dir, f"{safe}.json"), 'w') as f: json.dump(info, f, indent=2)
print(f"Registered: {name} -> {output_dir}/{safe}.json")
STUB
        chmod +x /usr/local/bin/ni-plugin-info
    fi
    log "ni-plugin-info installed"
else
    log "ni-plugin-info already installed"
fi

# ----------------------------------------------------------------
# 2. Install libssl 1.1 (needed by Kontakt, Reaktor, etc.)
# ----------------------------------------------------------------
if [[ ! -f /usr/lib/x86_64-linux-gnu/libssl.so.1.1 ]]; then
    info "Installing OpenSSL 1.1 compatibility library..."
    DEB="$CACHE_DIR/libssl1.1.deb"
    if [[ ! -f "$DEB" ]]; then
        curl -L -o "$DEB" \
            "http://archive.ubuntu.com/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2.24_amd64.deb" \
            2>/dev/null
    fi
    dpkg -i "$DEB" 2>/dev/null || true
    log "libssl 1.1 installed"
else
    log "libssl 1.1 already present"
fi

# ----------------------------------------------------------------
# 3. Install libxerces-c 3.1 (needed by Kontakt 8)
# ----------------------------------------------------------------
if [[ ! -f /usr/lib/x86_64-linux-gnu/libxerces-c-3.1.so ]]; then
    info "Installing xerces-c 3.1 compatibility library..."
    DEB="$CACHE_DIR/libxerces-c3.1.deb"
    if [[ ! -f "$DEB" ]]; then
        curl -L -o "$DEB" \
            "http://archive.ubuntu.com/ubuntu/pool/universe/x/xerces-c/libxerces-c3.1_3.1.3+debian-1_amd64.deb" \
            2>/dev/null
    fi
    # Extract just the .so — don't install via dpkg (avoids dependency chain)
    EXTRACT="$CACHE_DIR/xerces-extract"
    rm -rf "$EXTRACT"
    dpkg-deb -x "$DEB" "$EXTRACT"
    cp "$EXTRACT/usr/lib/x86_64-linux-gnu/libxerces-c-3.1.so" /usr/lib/x86_64-linux-gnu/
    log "libxerces-c 3.1 installed"

    # xerces-c 3.1 needs ICU 55 — extract that too
    if [[ ! -f /usr/lib/x86_64-linux-gnu/libicuuc.so.55 ]]; then
        info "Installing ICU 55 compatibility libraries (needed by xerces-c 3.1)..."
        ICU_DEB="$CACHE_DIR/libicu55.deb"
        if [[ ! -f "$ICU_DEB" ]]; then
            curl -L -o "$ICU_DEB" \
                "http://archive.ubuntu.com/ubuntu/pool/main/i/icu/libicu55_55.1-7ubuntu0.5_amd64.deb" \
                2>/dev/null
        fi
        ICU_EXTRACT="$CACHE_DIR/icu55-extract"
        rm -rf "$ICU_EXTRACT"
        dpkg-deb -x "$ICU_DEB" "$ICU_EXTRACT"
        cp "$ICU_EXTRACT"/usr/lib/x86_64-linux-gnu/libicu*.so.55* /usr/lib/x86_64-linux-gnu/
        log "ICU 55 installed"
    fi

    ldconfig
else
    log "libxerces-c 3.1 already present"
fi

# ----------------------------------------------------------------
# 4. Reconfigure any unconfigured NI packages
# ----------------------------------------------------------------
UNCONFIGURED=$(dpkg --audit 2>/dev/null | grep "^Package: ni-" | awk '{print $2}' || true)
if [[ -n "$UNCONFIGURED" ]]; then
    info "Reconfiguring NI packages: $UNCONFIGURED"
    dpkg --configure $UNCONFIGURED 2>/dev/null || true
    log "NI packages configured"
else
    log "All NI packages configured"
fi

# ----------------------------------------------------------------
# 5. Summary
# ----------------------------------------------------------------
echo ""
echo "============================================================"
echo -e "${GREEN}  Dependency setup complete!${NC}"
echo "============================================================"
echo ""
echo "  Installed plugins:"
find /usr/lib/vst -maxdepth 1 \( -name "*.vst3" -o -name "*.so" \) 2>/dev/null | while read p; do
    echo "    $(basename "$p")"
done
echo ""
echo "  Rescan plugins in Bitwig: Settings > Plug-ins > Rescan"
echo ""
