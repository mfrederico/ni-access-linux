#!/usr/bin/env python3
"""
NTKDaemon binary patcher — patches Windows Service API calls to allow
the daemon to run as a regular process under Wine/Proton.

Reads patch-strategy.json for what to patch, applies patches, runs test.

Usage: python3 patch-daemon.py [--strategy N]
"""
import struct
import shutil
import json
import sys
import os

ORIGINAL = os.path.expanduser(
    "~/Downloads/t/daemon-extracted/data/OFFLINE/DB4CA852/6916149A/NTKDaemon.exe"
)
DEST_PREFIX = os.path.expanduser(
    "~/.steam/steam/steamapps/compatdata/3486537896/pfx"
)
DEST = os.path.join(DEST_PREFIX, "drive_c/Program Files/Native Instruments/NTK Daemon/NTKDaemon.exe")
RESULT_FILE = os.path.join(os.path.dirname(__file__), "patch-result.json")
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "patch-strategy.json")

def load_exe(path):
    with open(path, "rb") as f:
        return bytearray(f.read())

def save_exe(data, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)

def parse_pe(data):
    pe_offset = struct.unpack_from('<I', data, 0x3c)[0]
    image_base = struct.unpack_from('<Q', data, pe_offset + 0x30)[0]
    num_sections = struct.unpack_from('<H', data, pe_offset + 6)[0]
    opt_hdr_size = struct.unpack_from('<H', data, pe_offset + 0x14)[0]
    section_offset = pe_offset + 0x18 + opt_hdr_size
    sections = []
    for i in range(num_sections):
        off = section_offset + i * 40
        name = data[off:off+8].rstrip(b'\x00').decode()
        vsize = struct.unpack_from('<I', data, off+8)[0]
        rva = struct.unpack_from('<I', data, off+12)[0]
        rawsize = struct.unpack_from('<I', data, off+16)[0]
        rawoff = struct.unpack_from('<I', data, off+20)[0]
        sections.append({'name': name, 'rva': rva, 'vsize': vsize, 'raw': rawoff, 'rawsize': rawsize})
    return {'pe_offset': pe_offset, 'image_base': image_base, 'sections': sections}

def rva_to_offset(pe, rva):
    for s in pe['sections']:
        if s['rva'] <= rva < s['rva'] + s['vsize']:
            return rva - s['rva'] + s['raw']
    return None

def offset_to_rva(pe, off):
    for s in pe['sections']:
        if s['raw'] <= off < s['raw'] + s['rawsize']:
            return off - s['raw'] + s['rva']
    return None

def find_iat_calls(data, pe, func_name_bytes):
    """Find all CALL [IAT] instructions that call a specific import."""
    pe_offset = pe['pe_offset']
    import_rva = struct.unpack_from('<I', data, pe_offset + 0x90)[0]
    import_off = rva_to_offset(pe, import_rva)

    results = []
    i = import_off
    while True:
        ilt_rva = struct.unpack_from('<I', data, i)[0]
        name_rva = struct.unpack_from('<I', data, i+12)[0]
        iat_rva = struct.unpack_from('<I', data, i+16)[0]
        if ilt_rva == 0 and name_rva == 0:
            break
        j = rva_to_offset(pe, ilt_rva)
        idx = 0
        while True:
            entry = struct.unpack_from('<Q', data, j)[0]
            if entry == 0:
                break
            if not (entry & (1 << 63)):
                hint_off = rva_to_offset(pe, entry & 0x7FFFFFFF)
                fname = data[hint_off+2:data.index(b'\x00', hint_off+2)]
                if func_name_bytes in fname:
                    iat_entry_rva = iat_rva + idx * 8
                    # Find call sites
                    text = pe['sections'][0]
                    for k in range(text['raw'], text['raw'] + text['rawsize'] - 6):
                        if data[k] == 0xFF and data[k+1] == 0x15:
                            disp = struct.unpack_from('<i', data, k+2)[0]
                            call_rva = offset_to_rva(pe, k) + 6
                            if call_rva + disp == iat_entry_rva:
                                results.append({
                                    'func': fname.decode(),
                                    'file_offset': k,
                                    'rva': offset_to_rva(pe, k),
                                    'iat_rva': iat_entry_rva,
                                })
            j += 8
            idx += 1
        i += 20
    return results


def get_strategies():
    """Define different patching strategies to try."""
    return [
        {
            "id": 1,
            "name": "NOP all service calls, call ServiceMain directly",
            "description": "Replace StartServiceCtrlDispatcher with direct ServiceMain call, NOP RegisterServiceCtrlHandler and SetServiceStatus"
        },
        {
            "id": 2,
            "name": "NOP all service calls, allocate fake handle for RegisterServiceCtrlHandler",
            "description": "Like strategy 1 but allocate heap memory for fake service handle"
        },
        {
            "id": 3,
            "name": "Patch ServiceMain to skip service registration entirely",
            "description": "NOP out the RegisterServiceCtrlHandler and SetServiceStatus calls within ServiceMain, let everything else run"
        },
        {
            "id": 4,
            "name": "Skip ServiceMain, find and call the actual daemon logic directly",
            "description": "Find the NTKDaemonApp constructor call and invoke it directly from main"
        },
    ]


def apply_strategy(data, pe, strategy_id):
    """Apply a patching strategy and return the modified data."""
    data = bytearray(data)  # copy

    # Find all service API call sites
    dispatcher_calls = find_iat_calls(data, pe, b"StartServiceCtrlDispatcherW")
    register_calls = find_iat_calls(data, pe, b"RegisterServiceCtrlHandlerW")
    status_calls = find_iat_calls(data, pe, b"SetServiceStatus")

    print(f"Found: {len(dispatcher_calls)} StartServiceCtrlDispatcher, "
          f"{len(register_calls)} RegisterServiceCtrlHandler, "
          f"{len(status_calls)} SetServiceStatus calls")

    # ServiceMain is referenced in the SERVICE_TABLE_ENTRY before the dispatcher call
    # At 0xf3914: LEA RAX, [RIP+disp] loads ServiceMain address
    disp = struct.unpack_from('<i', data, 0xf3917)[0]
    service_main_rva = offset_to_rva(pe, 0xf3914) + 7 + disp
    service_main_off = rva_to_offset(pe, service_main_rva)
    print(f"ServiceMain at RVA 0x{service_main_rva:x} (offset 0x{service_main_off:x})")

    if strategy_id == 1:
        # Strategy 1: Direct ServiceMain call + NOP service APIs
        # Patch dispatcher call site to call ServiceMain(0, NULL)
        patch_off = 0xf390c
        patch_rva = offset_to_rva(pe, patch_off)
        rel = service_main_rva - (patch_rva + 9)
        patch = b'\x33\xC9\x33\xD2\xE8' + struct.pack('<i', rel) + b'\xEB\xFE'
        patch += b'\x90' * (0xf392d - patch_off - len(patch))
        data[patch_off:patch_off+len(patch)] = patch

        # NOP RegisterServiceCtrlHandler -> return 1
        for c in register_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x01\x00\x00\x00\x90'

        # NOP SetServiceStatus -> return 1
        for c in status_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x01\x00\x00\x00\x90'

    elif strategy_id == 2:
        # Strategy 2: Same as 1, but allocate memory for fake handle
        # For RegisterServiceCtrlHandler, we need to return a valid pointer
        # Use a static address in .data section

        # Find some writable space in .data section
        data_section = [s for s in pe['sections'] if s['name'] == '.data'][0]
        # Use the end of .data section (should be zero-filled)
        fake_handle_rva = data_section['rva'] + data_section['rawsize'] - 256
        fake_handle_off = rva_to_offset(pe, fake_handle_rva)

        # Write a fake SERVICE_STATUS_HANDLE-like structure there
        # Just needs to be non-null and not crash when dereferenced
        data[fake_handle_off:fake_handle_off+64] = b'\x00' * 64

        # Patch dispatcher -> call ServiceMain
        patch_off = 0xf390c
        patch_rva = offset_to_rva(pe, patch_off)
        rel = service_main_rva - (patch_rva + 9)
        patch = b'\x33\xC9\x33\xD2\xE8' + struct.pack('<i', rel) + b'\xEB\xFE'
        patch += b'\x90' * (0xf392d - patch_off - len(patch))
        data[patch_off:patch_off+len(patch)] = patch

        # RegisterServiceCtrlHandler -> LEA RAX, [RIP+fake_handle]
        for c in register_calls:
            call_rva = c['rva']
            rel_handle = fake_handle_rva - (call_rva + 7)  # LEA RAX,[RIP+disp] is 7 bytes
            # 48 8D 05 xx xx xx xx = LEA RAX, [RIP+disp32]
            # But we only have 6 bytes... use MOV EAX, imm32 instead
            # The handle just needs to be non-null
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x00\x10\x00\x00\x90'  # MOV EAX, 0x1000

        # SetServiceStatus -> return 1
        for c in status_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x01\x00\x00\x00\x90'

    elif strategy_id == 3:
        # Strategy 3: Keep StartServiceCtrlDispatcher (it will be called by ServiceMain flow)
        # but NOP out RegisterServiceCtrlHandler and SetServiceStatus inside ServiceMain

        # Don't touch the dispatcher - let the original flow work
        # The original code at 0xf390c sets up SERVICE_TABLE_ENTRY and calls dispatcher
        # The dispatcher invokes ServiceMain as a callback
        # Inside ServiceMain, NOP the register/status calls

        for c in register_calls:
            # Return a non-null fake handle
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x00\x10\x00\x00\x90'

        for c in status_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x01\x00\x00\x00\x90'

    elif strategy_id == 4:
        # Strategy 4: Find the actual daemon app init and call it directly
        # Look for "DAEMON START" string reference which is in NTKDaemonApp constructor
        daemon_start = data.find(b"DAEMON START")
        if daemon_start >= 0:
            print(f"'DAEMON START' string at offset 0x{daemon_start:x}")

        # For now, same as strategy 2 but with an infinite sleep loop at the end
        # instead of JMP $, use a Sleep(INFINITE) call

        patch_off = 0xf390c
        patch_rva = offset_to_rva(pe, patch_off)
        rel = service_main_rva - (patch_rva + 9)
        patch = b'\x33\xC9\x33\xD2\xE8' + struct.pack('<i', rel) + b'\xEB\xFE'
        patch += b'\x90' * (0xf392d - patch_off - len(patch))
        data[patch_off:patch_off+len(patch)] = patch

        for c in register_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x00\x10\x00\x00\x90'

        for c in status_calls:
            data[c['file_offset']:c['file_offset']+6] = b'\xB8\x01\x00\x00\x00\x90'

    return data


def main():
    strategy_id = int(sys.argv[1]) if len(sys.argv) > 1 else 1

    strategies = get_strategies()
    strategy = next((s for s in strategies if s['id'] == strategy_id), None)
    if not strategy:
        print(f"Unknown strategy {strategy_id}. Available: {[s['id'] for s in strategies]}")
        sys.exit(1)

    print(f"\n=== Strategy {strategy_id}: {strategy['name']} ===")
    print(f"    {strategy['description']}\n")

    data = load_exe(ORIGINAL)
    pe = parse_pe(data)

    patched = apply_strategy(data, pe, strategy_id)
    save_exe(patched, DEST)
    print(f"\nPatched daemon saved to: {DEST}")

    result = {
        "strategy_id": strategy_id,
        "strategy_name": strategy['name'],
        "dest": DEST,
    }
    with open(RESULT_FILE, 'w') as f:
        json.dump(result, f, indent=2)


if __name__ == "__main__":
    main()
