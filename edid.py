"""Inspect a TCL0963 EDID or build a 2160p60 YCbCr 4:2:0 software override."""

import argparse
from pathlib import Path


HDMI = bytes.fromhex("03 0c 00")
HDMI_FORUM = bytes.fromhex("d8 5d c4")
VIC_2160P60 = 97


def vendor(block):
    return block[1:4] if block[0] >> 5 == 3 else b""


def parse(data):
    if len(data) != 256 or data[:8] != bytes.fromhex("00 ff ff ff ff ff ff 00"):
        raise ValueError("Expected a 256-byte EDID with one extension.")
    if data[8:12] != bytes.fromhex("50 6c 63 09"):
        raise ValueError("This tool only handles monitor ID TCL0963.")
    ext = data[128:]
    if data[126] != 1 or ext[:2] != bytes([2, 3]):
        raise ValueError("Expected one CTA-861 revision 3 extension.")
    if sum(data[:128]) % 256 or sum(ext) % 256:
        raise ValueError("Invalid EDID checksum.")
    end = ext[2]
    if not 4 <= end <= 127:
        raise ValueError("Unsupported CTA descriptor offset.")
    blocks = []
    pos = 4
    while pos < end:
        size = 1 + (ext[pos] & 31)
        if pos + size > end:
            raise ValueError("Truncated CTA data block.")
        blocks.append(ext[pos:pos + size])
        pos += size
    hdmi = [b for b in blocks if vendor(b) == HDMI]
    if len(hdmi) != 1 or len(hdmi[0]) < 8:
        raise ValueError("Missing or ambiguous HDMI vendor block.")
    address = int.from_bytes(hdmi[0][4:6], "big")
    if address not in (0x1000, 0x2000):
        raise ValueError("Expected a direct connection to HDMI 1 or 2.")
    return ext, blocks, address >> 12


def y420_block(b):
    return b[0] >> 5 == 7 and b[1:2] == bytes([14])


def inspect(data):
    _, blocks, port = parse(data)
    hdmi = next(b for b in blocks if vendor(b) == HDMI)
    vics = [v for b in blocks if y420_block(b) for v in b[2:]]
    print(f"TCL0963 HDMI {port} ({port}.0.0.0)")
    print(f"HDMI VSDB max TMDS: {hdmi[7] * 5} MHz")
    print("HDMI Forum VSDB:", any(vendor(b) == HDMI_FORUM for b in blocks))
    print("4:2:0-only VICs:", vics)
    print("3840x2160p60 (VIC 97) in 4:2:0-only list:", VIC_2160P60 in vics)


def make_override(data):
    ext, blocks, _ = parse(data)
    hdmi_index = next(i for i, b in enumerate(blocks) if vendor(b) == HDMI)
    if blocks[hdmi_index][7] != 60:
        raise ValueError("Expected the tested 300 MHz HDMI profile.")
    for b in blocks:
        if (b[0] >> 5 == 2 and VIC_2160P60 in b[1:]) or (y420_block(b) and VIC_2160P60 in b[2:]):
            raise ValueError("The input already advertises VIC 97. Use the original hardware backup.")
    if not any(vendor(b) == HDMI_FORUM for b in blocks):
        # Minimal HDMI Forum VSDB, version 1. No extra link rate, SCDC or deep color.
        blocks.insert(hdmi_index + 1, bytes.fromhex("67 d8 5d c4 01 00 00 00"))
    for i, b in enumerate(blocks):
        if y420_block(b):
            if len(b) == 32:
                raise ValueError("Existing 4:2:0 video block is full.")
            blocks[i] = bytes([b[0] + 1]) + b[1:] + bytes([VIC_2160P60])
            break
    else:
        blocks.append(bytes([0xe2, 14, VIC_2160P60]))

    # Preserve complete 18-byte descriptors, including their trailing zero bytes.
    old_end = ext[2]
    pos = old_end
    while any(ext[pos:127]):
        if pos + 18 > 127:
            raise ValueError("Truncated or unsupported CTA descriptor.")
        pos += 18
    descriptors = ext[old_end:pos]
    payload = b"".join(blocks)
    new_end = 4 + len(payload)
    padding = 127 - new_end - len(descriptors)
    if padding < 2:
        raise ValueError("Insufficient space for the additional blocks.")
    edited = bytearray(ext[:4] + payload + descriptors + bytes(padding + 1))
    edited[2] = new_end
    edited[127] = -sum(edited[:127]) % 256
    result = data[:128] + edited
    parse(result)
    return result


def save(path, data):
    try:
        with path.open("xb") as stream:
            stream.write(data)
    except FileExistsError:
        if path.read_bytes() != data:
            raise ValueError(f"{path} exists with different contents. Choose another filename.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    show = commands.add_parser("inspect")
    show.add_argument("input", type=Path)
    build = commands.add_parser("build")
    build.add_argument("input", type=Path)
    build.add_argument("output", type=Path)
    args = parser.parse_args()
    data = args.input.read_bytes()
    if args.command == "build":
        if args.input.resolve() == args.output.resolve():
            raise ValueError("Use separate original and output paths.")
        data = make_override(data)
        save(args.output, data)
    inspect(data)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
