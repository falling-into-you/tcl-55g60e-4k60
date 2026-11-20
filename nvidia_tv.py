"""Read the hardware EDID or inspect the active TCL0963 HDMI output on Windows."""

import argparse
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import sys

from edid import inspect, parse, save


U8, U16, U32 = C.c_uint8, C.c_uint16, C.c_uint32
P = C.POINTER


class DisplayDevice(C.Structure):
    _fields_ = [("size", W.DWORD), ("name", W.WCHAR * 32), ("label", W.WCHAR * 128),
                ("flags", W.DWORD), ("device_id", W.WCHAR * 128), ("key", W.WCHAR * 128)]


class EdidData(C.Structure):
    _fields_ = [("version", U32), ("data", C.c_void_p), ("size", U32), ("reserved", U32 * 8)]


class ColorValues(C.Structure):
    _fields_ = [("format", U8), ("colorimetry", U8), ("range", U8), ("bpc", U32),
                ("policy", U32), ("depth", U32)]


class Color(C.Structure):
    _fields_ = [("version", U32), ("size", U16), ("cmd", U8), ("values", ColorValues)]


class TimingInput(C.Structure):
    _fields_ = [("version", U32), ("width", U32), ("height", U32), ("refresh", C.c_float),
                ("interlace", U32), ("cea", U32), ("scaling", U32), ("kind", U32)]


class TimingExtra(C.Structure):
    _fields_ = [("flag", U32), ("refresh", U16), ("millihertz", U32), ("aspect", U32),
                ("rep", U16), ("status", U32), ("name", U8 * 40)]


class Timing(C.Structure):
    _fields_ = [("hvisible", U16), ("hborder", U16), ("hfront", U16), ("hsync", U16),
                ("htotal", U16), ("hpol", U8), ("vvisible", U16), ("vborder", U16),
                ("vfront", U16), ("vsync", U16), ("vtotal", U16), ("vpol", U8),
                ("interlace", U16), ("clock", U32), ("extra", TimingExtra)]


def find_tv():
    enum = C.WinDLL("user32").EnumDisplayDevicesW
    enum.argtypes = [W.LPCWSTR, W.DWORD, P(DisplayDevice), W.DWORD]

    def devices(parent):
        i = 0
        while True:
            item = DisplayDevice()
            item.size = C.sizeof(item)
            if not enum(parent, i, C.byref(item), 0):
                break
            if item.flags & 1:
                yield item
            i += 1

    matches = [adapter.name for adapter in devices(None) for screen in devices(adapter.name)
               if "TCL0963" in screen.device_id.upper().split(chr(92))]
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one active TCL0963 display. Found {len(matches)}.")
    return matches[0]


class Nvidia:
    def __init__(self, display_name):
        self.lib = C.CDLL("nvapi64.dll")
        self.query = self.lib.nvapi_QueryInterface
        self.query.argtypes, self.query.restype = [U32], C.c_void_p
        self.api(0x0150e828)()
        self.display = U32()
        self.api(0xae457190, C.c_char_p, P(U32))(display_name.encode("ascii"), C.byref(self.display))

    def api(self, entry, *args):
        # Interface IDs and structures are from NVIDIA's public NVAPI headers.
        ptr = self.query(entry)
        if not ptr:
            raise ValueError(f"NVIDIA driver lacks interface {entry:#x}.")
        proc = C.CFUNCTYPE(C.c_int, *args)(ptr)
        def checked(*values):
            status = proc(*values)
            if status != 0:
                raise ValueError(f"NVAPI {entry:#x} failed with status {status}.")
        return checked

    def read_edid(self, flag):
        read = self.api(0x436ced76, U32, P(EdidData), P(U32))
        request = EdidData(C.sizeof(EdidData) | (2 << 16))
        source = U32(flag)
        read(self.display, C.byref(request), C.byref(source))
        if not 128 <= request.size <= 32768:
            raise ValueError("Invalid EDID size returned by the driver.")
        buf = C.create_string_buffer(request.size)
        request.data = C.addressof(buf)
        source.value = flag
        read(self.display, C.byref(request), C.byref(source))
        if request.size > len(buf) or (flag == 5 and source.value != 5):
            raise ValueError("Unexpected EDID response. Recheck the connection.")
        return buf.raw[:request.size]

    def status(self):
        request = TimingInput(C.sizeof(TimingInput) | (1 << 16))
        timing = Timing()
        self.api(0x175167e9, U32, P(TimingInput), P(Timing))(
            self.display, C.byref(request), C.byref(timing))
        color = Color(C.sizeof(Color) | (5 << 16), C.sizeof(Color), 1)
        self.api(0x92f9d80d, U32, P(Color))(self.display, C.byref(color))
        formats = {0: "RGB", 1: "YCbCr422", 2: "YCbCr444", 3: "YCbCr420"}
        depths = {1: 6, 2: 8, 3: 10, 4: 12, 5: 16}
        print(f"GPU output: {timing.hvisible}x{timing.vvisible}, "
              f"{timing.extra.millihertz / 1000:g} Hz, "
              f"{formats.get(color.values.format, 'unknown')}, "
              f"{depths.get(color.values.bpc, 'unknown')} bpc")
        print("Hardware EDID (physical input):")
        inspect(self.read_edid(5))
        print("EDID in use by the driver (can include a Windows override):")
        inspect(self.read_edid(0))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    read = commands.add_parser("backup")
    read.add_argument("file", type=Path)
    read.add_argument("--port", type=int, choices=[1, 2])
    commands.add_parser("status")
    args = parser.parse_args()
    if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
        raise ValueError("The NVIDIA reader requires 64-bit Python on Windows.")
    nv = Nvidia(find_tv())
    if args.command == "status":
        nv.status()
        return
    raw = nv.read_edid(5)
    _, _, port = parse(raw)
    if args.port and port != args.port:
        raise ValueError(f"Hardware reports HDMI {port}, expected HDMI {args.port}.")
    save(args.file, raw)
    inspect(raw)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError) as error:
        raise SystemExit(str(error))
