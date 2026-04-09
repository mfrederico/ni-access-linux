#!/usr/bin/env bash
#
# Native Instruments Plugin Installer for Linux + Bitwig
# Uses: Proton (via Steam), yabridge, yabridgectl
#
# Prerequisites:
#   1. Download Native Access 2 installer from:
#      https://www.native-instruments.com/fileadmin/downloads/Native-Access_2.exe
#
#   2. Install 7z (p7zip) to extract the installer:
#      Ubuntu/Debian: sudo apt install p7zip-full
#      Fedora:        sudo dnf install p7zip p7zip-plugins
#      Arch:          sudo pacman -S p7zip
#
#   3. Extract the .exe installer:
#      7z x Native-Access_2.exe -o./ni-installer
#      7z x './ni-installer/$PLUGINSDIR/app-64.7z' -o./ni-installer/extracted
#
#   4. Install Proton via Steam (version 10.0+ recommended)
#
#   5. Install yabridge:
#      https://github.com/robbert-vdh/yabridge/releases
#
# NOTE: First run will take several minutes while Wine initializes the prefix
#       and installs Mono. This is normal — subsequent runs will be fast.
#
# This script:
#   1. Verifies prerequisites (7z, Proton, yabridge)
#   2. Creates a dedicated Wine prefix using Proton
#   3. Installs VC++ redistributables
#   4. Installs Native Access into the Wine prefix
#   5. Configures yabridge to bridge NI VST3 plugins to Bitwig
#   6. Creates a launcher script for Native Access
#

set -euo pipefail

# ============================================================================
# Configuration — edit these if needed
# ============================================================================
WINEPREFIX="${WINEPREFIX:-$HOME/.wine-ni}"
PROTON_VERSION="${PROTON_VERSION:-}"  # auto-detected if empty
YABRIDGE_PATH="${YABRIDGE_PATH:-}"   # auto-detected if empty

# Where the extracted NI installer lives (this folder)
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
EXTRACTED_DIR="$SCRIPT_DIR/extracted"
NA_EXE="$EXTRACTED_DIR/Native Access.exe"
VCREDIST="$SCRIPT_DIR/VC_redist.x64.exe"

# VST paths inside the prefix
VST3_DIR="$WINEPREFIX/drive_c/Program Files/Common Files/VST3"
VST2_DIR="$WINEPREFIX/drive_c/Program Files/Common Files/VST2"
NI_DIR="$WINEPREFIX/drive_c/Program Files/Native Instruments"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log()   { echo -e "${GREEN}[OK]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
err()   { echo -e "${RED}[ERROR]${NC} $*"; }
info()  { echo -e "${CYAN}[INFO]${NC} $*"; }

# ============================================================================
# Helper: set up Proton environment variables
# Must be called after find_proton sets PROTON_DIR
# ============================================================================
setup_proton_env() {
    WINE="$PROTON_DIR/files/bin/wine"
    WINE64="$PROTON_DIR/files/bin/wine64"
    WINESERVER="$PROTON_DIR/files/bin/wineserver"

    # Critical: Proton's wine needs to find itself for sub-processes (wineboot, reg.exe, etc.)
    export PATH="$PROTON_DIR/files/bin:$PATH"

    # Wine DLL search paths (Proton 9 uses lib64/, Proton 10 uses lib/)
    export WINEDLLPATH="$PROTON_DIR/files/lib64/wine/x86_64-unix:$PROTON_DIR/files/lib/wine/x86_64-unix:$PROTON_DIR/files/lib/wine/i386-unix"

    # Proton shared libraries
    export LD_LIBRARY_PATH="${PROTON_DIR}/files/lib64:${PROTON_DIR}/files/lib:${LD_LIBRARY_PATH:-}"

    # Windows environment variables needed by Electron/Node apps
    export windir="C:\\windows"
    export SystemRoot="C:\\windows"
}

# ============================================================================
# Step 0: Verify extracted files exist
# ============================================================================
step_verify_files() {
    info "Checking prerequisites..."

    if ! command -v 7z &>/dev/null; then
        err "7z (p7zip) is not installed."
        err "  Ubuntu/Debian: sudo apt install p7zip-full"
        err "  Fedora:        sudo dnf install p7zip p7zip-plugins"
        err "  Arch:          sudo pacman -S p7zip"
        exit 1
    fi
    log "7z found"

    info "Checking extracted installer files..."

    if [[ ! -f "$NA_EXE" ]]; then
        err "Native Access.exe not found at: $NA_EXE"
        err "Download Native-Access_2.exe from:"
        err "  https://www.native-instruments.com/fileadmin/downloads/Native-Access_2.exe"
        err "Then extract it:"
        err "  7z x Native-Access_2.exe -o./ni-installer"
        err "  7z x './ni-installer/\$PLUGINSDIR/app-64.7z' -o./ni-installer/extracted"
        exit 1
    fi
    log "Found Native Access.exe"

    if [[ -f "$VCREDIST" ]]; then
        log "Found VC_redist.x64.exe"
    else
        warn "VC_redist.x64.exe not found — will skip VC++ install (may already be in prefix)"
    fi
}

# ============================================================================
# Step 1: Find Proton
# ============================================================================
find_proton() {
    if [[ -n "$PROTON_VERSION" ]]; then
        echo "$PROTON_VERSION"
        return
    fi

    local search_paths=(
        "$HOME/.steam/steam/steamapps/common"
        "$HOME/.local/share/Steam/steamapps/common"
        "$HOME/.steam/root/steamapps/common"
    )

    # Prefer Proton 9.0 — Proton 10.0 has GDI handle exhaustion with Electron apps
    for preferred in "Proton 9.0 (Beta)" "Proton 9.0" "Proton 10.0" "Proton - Experimental"; do
        for base in "${search_paths[@]}"; do
            local candidate="$base/$preferred"
            if [[ -x "$candidate/files/bin/wine64" ]]; then
                echo "$candidate"
                return
            fi
        done
    done

    err "No Proton installation found. Install Proton via Steam first."
    err "  Recommended: Proton 9.0"
    exit 1
}

step_find_proton() {
    info "Locating Proton..."
    PROTON_DIR="$(find_proton)"
    setup_proton_env

    log "Using Proton at: $PROTON_DIR"
    log "Wine version: $("$WINE64" --version 2>/dev/null || echo 'unknown')"
}

# ============================================================================
# Step 2: Find yabridge
# ============================================================================
step_find_yabridge() {
    info "Locating yabridge..."

    if ! command -v yabridgectl &>/dev/null; then
        err "yabridgectl not found. Install yabridge first."
        err "  GitHub: https://github.com/robbert-vdh/yabridge/releases"
        exit 1
    fi

    # The yabridge tarball extracts to a nested yabridge/yabridge/ folder.
    # Fix this common issue: flatten it so yabridgectl auto-detection works.
    local yabridge_dir="$HOME/.local/share/yabridge"
    local nested_dir="$yabridge_dir/yabridge"
    if [[ -f "$nested_dir/libyabridge-chainloader-vst3.so" ]] && \
       [[ ! -f "$yabridge_dir/libyabridge-chainloader-vst3.so" ]]; then
        info "Fixing nested yabridge directory (yabridge/yabridge/ -> yabridge/)..."
        local tmpdir="${yabridge_dir}-tmp-$$"
        mv "$nested_dir" "$tmpdir"
        find "$yabridge_dir" -maxdepth 1 -type f -name "*.tar.gz" -exec mv {} /tmp/ \; 2>/dev/null || true
        rmdir "$yabridge_dir" 2>/dev/null || rm -rf "$yabridge_dir"
        mv "$tmpdir" "$yabridge_dir"
        log "Fixed yabridge directory layout."
    fi

    # Verify yabridge .so files exist
    local yabridge_lib=""
    for candidate in \
        "$HOME/.local/share/yabridge" \
        "/usr/lib/yabridge" \
        "$HOME/.local/lib/yabridge"; do
        if [[ -f "$candidate/libyabridge-chainloader-vst3.so" ]]; then
            yabridge_lib="$candidate"
            break
        fi
    done

    if [[ -z "$yabridge_lib" ]]; then
        err "yabridge .so files not found. Install yabridge binaries to:"
        err "  $HOME/.local/share/yabridge/"
        err "  GitHub: https://github.com/robbert-vdh/yabridge/releases"
        exit 1
    fi

    log "yabridgectl $(yabridgectl --version 2>/dev/null)"
    log "yabridge libs at: $yabridge_lib"

    # Verify yabridgectl can find the libs
    if ! yabridgectl status 2>&1 | grep -q "libyabridge"; then
        warn "yabridgectl cannot auto-detect yabridge libs."
        warn "Try: yabridgectl set --path=\"$yabridge_lib\""
    fi
}

# ============================================================================
# Step 3: Create Wine prefix
# ============================================================================
step_create_prefix() {
    info "Setting up Wine prefix at: $WINEPREFIX"
    export WINEPREFIX

    if [[ -d "$WINEPREFIX/drive_c" ]]; then
        warn "Wine prefix already exists. Reusing it."
        warn "  To start fresh, delete $WINEPREFIX and re-run."
    else
        info "Creating new 64-bit Wine prefix (this may take a few minutes)..."
        info "  Wine is installing Mono and setting up the Windows environment."
        info "  Please be patient on first run."
        WINEARCH=win64 "$WINE64" wineboot --init
        info "Waiting for Wine prefix initialization to complete..."
        "$WINESERVER" --wait
        log "Wine prefix created."
    fi

    # Set Windows version to Windows 10
    info "Setting Windows version to Windows 10..."
    "$WINE64" reg add "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" \
        /v ProductName /t REG_SZ /d "Windows 10 Pro" /f
    "$WINESERVER" --wait
    "$WINE64" reg add "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" \
        /v CSDVersion /t REG_SZ /d "" /f
    "$WINE64" reg add "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" \
        /v CurrentBuildNumber /t REG_SZ /d "19041" /f
    "$WINE64" reg add "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion" \
        /v CurrentVersion /t REG_SZ /d "10.0" /f
    "$WINESERVER" --wait

    log "Prefix configured as Windows 10."

    # Copy vkd3d DLLs into system32 if they exist in Proton but not in the prefix.
    # Proton keeps them in a separate directory; wined3d.dll expects them in system32.
    local sys32="$WINEPREFIX/drive_c/windows/system32"
    if [[ ! -f "$sys32/libvkd3d-1.dll" ]]; then
        info "Copying vkd3d DLLs to system32..."
        # Proton 10 uses lib/vkd3d/x86_64-windows/, Proton 9 uses lib64/vkd3d/
        local vkd3d_src=""
        for candidate in \
            "$PROTON_DIR/files/lib/vkd3d/x86_64-windows" \
            "$PROTON_DIR/files/lib64/vkd3d"; do
            if [[ -f "$candidate/libvkd3d-1.dll" ]]; then
                vkd3d_src="$candidate"
                break
            fi
        done
        if [[ -n "$vkd3d_src" ]]; then
            cp "$vkd3d_src/libvkd3d-1.dll" "$sys32/"
            cp "$vkd3d_src/libvkd3d-shader-1.dll" "$sys32/"
            log "vkd3d DLLs installed."
        else
            warn "vkd3d DLLs not found in Proton — GPU rendering may not work."
        fi
    fi
}

# ============================================================================
# Step 4: Install VC++ Redistributable
# ============================================================================
step_install_vcredist() {
    if [[ ! -f "$VCREDIST" ]]; then
        warn "Skipping VC++ Redistributable (file not found)"
        return
    fi

    info "Installing VC++ Redistributable..."
    "$WINE64" "$VCREDIST" /quiet /norestart 2>/dev/null || {
        warn "VC++ install returned non-zero (this is often OK)"
    }
    "$WINESERVER" --wait 2>/dev/null || true
    log "VC++ Redistributable installed."
}

# ============================================================================
# Step 5: Install winetricks dependencies (if winetricks available)
# ============================================================================
step_winetricks() {
    if ! command -v winetricks &>/dev/null; then
        warn "winetricks not installed — skipping optional dependencies."
        warn "  For best results: sudo apt install winetricks  (or your distro equivalent)"
        warn "  Then re-run this script, or manually run:"
        warn "    WINEPREFIX=$WINEPREFIX winetricks -q gdiplus corefonts"
        return
    fi

    info "Installing winetricks dependencies (gdiplus, corefonts)..."
    WINE="$WINE64" WINESERVER="$WINESERVER" \
        winetricks -q gdiplus corefonts 2>/dev/null || {
        warn "Some winetricks components may have failed (often OK)"
    }
    "$WINESERVER" --wait 2>/dev/null || true
    log "Winetricks dependencies installed."
}

# ============================================================================
# Step 6: Install Native Access into the prefix
# ============================================================================
step_install_native_access() {
    info "Installing Native Access into Wine prefix..."

    # Create target directories
    mkdir -p "$WINEPREFIX/drive_c/Program Files/Native Instruments/Native Access"

    # Copy the extracted Electron app into the prefix
    cp -r "$EXTRACTED_DIR"/* "$WINEPREFIX/drive_c/Program Files/Native Instruments/Native Access/"

    log "Native Access files copied to prefix."

    # Patch Native Access.exe PE header to increase stack size from 8MB to 32MB.
    # Wine's Windows API stubs use more stack than real Windows, and Chromium/Electron
    # has deep call stacks that overflow the default 8MB limit.
    local na_exe_path="$WINEPREFIX/drive_c/Program Files/Native Instruments/Native Access/Native Access.exe"
    if [[ -f "$na_exe_path" ]]; then
        info "Patching Native Access.exe stack size (8MB -> 32MB)..."
        python3 -c "
import struct
with open('$na_exe_path', 'r+b') as f:
    f.seek(0x3c)
    pe_offset = struct.unpack('<I', f.read(4))[0]
    stack_offset = pe_offset + 4 + 20 + 72
    new_reserve = 32 * 1024 * 1024  # 32MB
    new_commit = 1 * 1024 * 1024    # 1MB
    f.seek(stack_offset)
    f.write(struct.pack('<Q', new_reserve))
    f.write(struct.pack('<Q', new_commit))
" && log "Stack size patched." || warn "Stack patch failed (python3 required)"
    fi

    # Create the standard VST directories
    mkdir -p "$VST3_DIR"
    mkdir -p "$VST2_DIR"
    mkdir -p "$NI_DIR"

    # Register Native Access in the Wine registry (NI plugins look for this)
    "$WINE64" reg add "HKEY_LOCAL_MACHINE\\SOFTWARE\\Native Instruments\\Native Access" \
        /v InstallDir /t REG_SZ \
        /d "C:\\Program Files\\Native Instruments\\Native Access" /f &>/dev/null || true

    log "Registry entries added."

    # Disable hardware acceleration in Native Access config.
    # The Electron renderer crashes under Wine with GPU acceleration enabled.
    local na_appdata="$WINEPREFIX/drive_c/users/steamuser/AppData/Roaming/Native Instruments/Native Access"
    mkdir -p "$na_appdata"

    # Create the missing tracking file (prevents a startup error)
    local ni_docs="$WINEPREFIX/drive_c/users/Public/Documents/Native Instruments"
    mkdir -p "$ni_docs"
    [[ -f "$ni_docs/tracking.json" ]] || echo '{}' > "$ni_docs/tracking.json"

    # Write or patch the settings file that controls hardware acceleration.
    # The filename is a content hash — find any existing one, or create a new one.
    local settings_file
    settings_file=$(find "$na_appdata" -maxdepth 1 -name "*.json" -exec grep -l "disableHardwareAcceleration" {} \; 2>/dev/null | head -1)

    if [[ -n "$settings_file" ]]; then
        # Patch existing file
        sed -i 's/"disableHardwareAcceleration": false/"disableHardwareAcceleration": true/' "$settings_file"
        log "Patched hardware acceleration off in: $(basename "$settings_file")"
    else
        # No settings file yet — create one with sensible defaults
        cat > "$na_appdata/settings.json" << 'SETTINGS_EOF'
{
	"productListViewType": "grid",
	"dataTrackingEnabled": true,
	"isFirstAppStart": true,
	"marketingConsentStatus": false,
	"trackedOptOut": false,
	"showOpenHelperModal": true,
	"showOpenHelperProductActionsMenu": false,
	"theme": "system",
	"hideCategoryHeaders": [],
	"disableHardwareAcceleration": true,
	"onboardingHasSeenRepairAll": false
}
SETTINGS_EOF
        log "Created settings with hardware acceleration disabled."
    fi
}

# ============================================================================
# Step 7: Install NTKDaemon
# ============================================================================
step_install_daemon() {
    local daemon_exe="$EXTRACTED_DIR/resources/daemon/win/NTKDaemon 1.30.0 Setup PC.exe"
    if [[ ! -f "$daemon_exe" ]]; then
        warn "NTKDaemon installer not found — skipping"
        return
    fi

    info "Installing NTKDaemon (NI background service)..."
    "$WINE64" "$daemon_exe" /S 2>/dev/null || {
        warn "NTKDaemon installer returned non-zero (may need manual install)"
    }
    "$WINESERVER" --wait 2>/dev/null || true
    log "NTKDaemon installed."
}

# ============================================================================
# Step 8: Configure yabridge
# ============================================================================
step_configure_yabridge() {
    info "Configuring yabridge for NI plugin directories..."

    # Add VST3 path
    yabridgectl add "$VST3_DIR" 2>/dev/null || true

    # Add NI-specific paths (some NI plugins install VST3 here)
    if [[ -d "$NI_DIR" ]]; then
        local ni_vst3_dirs
        ni_vst3_dirs=$(find "$NI_DIR" -type d -name "*.vst3" -printf '%h\n' 2>/dev/null | sort -u || true)
        for dir in $ni_vst3_dirs; do
            yabridgectl add "$dir" 2>/dev/null || true
        done
    fi

    info "Running yabridge sync..."
    yabridgectl sync 2>/dev/null || {
        warn "yabridge sync had warnings (check output above)"
    }

    log "yabridge configured."
    echo ""
    info "Current yabridge status:"
    yabridgectl status 2>/dev/null || true
}

# ============================================================================
# Step 9: Create launcher scripts
# ============================================================================
step_create_launchers() {
    info "Creating launcher scripts..."

    local launcher="$HOME/.local/bin/native-access"
    mkdir -p "$HOME/.local/bin"

    cat > "$launcher" << 'LAUNCHER_HEADER'
#!/usr/bin/env bash
# Native Access Launcher (Linux/Proton)
# Generated by install-ni-linux.sh
LAUNCHER_HEADER

    cat >> "$launcher" << LAUNCHER_VARS
WINEPREFIX="$WINEPREFIX"
PROTON_DIR="$PROTON_DIR"
LAUNCHER_VARS

    cat >> "$launcher" << 'LAUNCHER_BODY'
export WINEPREFIX

PROTON="$PROTON_DIR/files"

# Proton's wine needs to find itself for sub-processes
export PATH="$PROTON/bin:$PATH"
export WINEDLLPATH="$PROTON/lib64/wine/x86_64-unix:$PROTON/lib/wine/x86_64-unix:$PROTON/lib/wine/i386-unix"
export LD_LIBRARY_PATH="$PROTON/lib64:$PROTON/lib:${LD_LIBRARY_PATH:-}"
export VK_ICD_FILENAMES="${VK_ICD_FILENAMES:-/usr/share/vulkan/icd.d/nvidia_icd.json}"
export DXVK_LOG_LEVEL=none
export windir="C:\\windows"
export SystemRoot="C:\\windows"

WINE="$PROTON/bin/wine64"
NA_EXE="C:\\Program Files\\Native Instruments\\Native Access\\Native Access.exe"

echo "Starting Native Access via Proton..."
echo "  Prefix: $WINEPREFIX"
echo "  Wine:   $($WINE --version 2>/dev/null)"

# --force-device-scale-factor=1 avoids a stack overflow in Wine's TextScaleFactor stub
"$WINE" "$NA_EXE" --force-device-scale-factor=1 "$@" &
echo "Native Access launched (PID: $!)"
LAUNCHER_BODY

    chmod +x "$launcher"
    log "Launcher created: $launcher"

    # Desktop entry
    local desktop_file="$HOME/.local/share/applications/native-access.desktop"
    mkdir -p "$HOME/.local/share/applications"

    cat > "$desktop_file" << DESKTOP_EOF
[Desktop Entry]
Name=Native Access (Wine)
Comment=Native Instruments Plugin Manager
Exec=$launcher
Type=Application
Categories=AudioVideo;Audio;Music;
Icon=native-instruments
StartupNotify=true
DESKTOP_EOF

    log "Desktop entry created: $desktop_file"
}

# ============================================================================
# Step 10: Post-install instructions
# ============================================================================
step_summary() {
    echo ""
    echo "============================================================"
    echo -e "${GREEN} Installation Complete!${NC}"
    echo "============================================================"
    echo ""
    echo "  Wine prefix:   $WINEPREFIX"
    echo "  Proton:        $PROTON_DIR"
    echo "  VST3 dir:      $VST3_DIR"
    echo ""
    echo -e "${CYAN}Next steps:${NC}"
    echo ""
    echo "  1. Launch Native Access:"
    echo "     \$ native-access"
    echo ""
    echo "  2. Log in with your NI account and download your plugins."
    echo "     Plugins will install to the Wine prefix automatically."
    echo ""
    echo "  3. After installing plugins, re-sync yabridge:"
    echo "     \$ yabridgectl sync"
    echo ""
    echo "  4. In Bitwig, go to Settings > Plug-ins > Locations and add:"
    echo "     ~/.vst3/yabridge"
    echo "     Then click 'Rescan'."
    echo ""
    echo "  5. Your NI plugins should appear in Bitwig's plugin browser!"
    echo ""
    echo -e "${YELLOW}Tips:${NC}"
    echo "  - If a plugin GUI looks wrong, try X11 mode (not Wayland)"
    echo "  - Run 'yabridgectl status' to check bridged plugins"
    echo "  - Plugin content (Kontakt libraries etc.) installs to:"
    echo "    $WINEPREFIX/drive_c/Users/\$USER/"
    echo "  - To uninstall: rm -rf $WINEPREFIX"
    echo ""
}

# ============================================================================
# Main
# ============================================================================
main() {
    echo ""
    echo "============================================================"
    echo "  Native Instruments Linux Installer"
    echo "  (Proton + yabridge + Bitwig)"
    echo "============================================================"
    echo ""

    step_verify_files
    step_find_proton
    step_find_yabridge
    step_create_prefix
    step_install_vcredist
    step_winetricks
    step_install_native_access
    step_install_daemon
    step_configure_yabridge
    step_create_launchers
    step_summary
}

main "$@"
