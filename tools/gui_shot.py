"""开发期截图工具：把主窗口抓成 PNG，用于人工核对界面。

只依赖标准库：用 GDI 的 BitBlt 抓像素，再用 zlib 手写 PNG。
不引入任何第三方库，也不进打包产物（tools/ 下的开发脚本）。

用法：
    python tools/gui_shot.py [输出文件] [--width 1060] [--height 760]
"""
from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from ctypes import wintypes
from pathlib import Path

SRCCOPY = 0x00CC0020
BI_RGB = 0


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


def grab(hwnd: int, height: int, width: int) -> bytes:
    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    hdc = user32.GetWindowDC(hwnd)
    memdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
    gdi32.SelectObject(memdc, bmp)
    if not gdi32.BitBlt(memdc, 0, 0, width, height, hdc, 0, 0, SRCCOPY):
        raise SystemExit("BitBlt 失败")

    info = BITMAPINFO()
    info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    info.bmiHeader.biWidth = width
    info.bmiHeader.biHeight = -height  # 负数 = 自上而下
    info.bmiHeader.biPlanes = 1
    info.bmiHeader.biBitCount = 32
    info.bmiHeader.biCompression = BI_RGB
    buf = ctypes.create_string_buffer(width * height * 4)
    gdi32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(info), 0)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(memdc)
    user32.ReleaseDC(hwnd, hdc)
    return buf.raw


def write_png(path: Path, raw: bytes, width: int, height: int) -> None:
    rows = []
    for y in range(height):
        row = raw[y * width * 4 : (y + 1) * width * 4]
        rows.append(b"\x00" + bytes(row[i] for i in range(width * 4) if i % 4 != 3))
    body = zlib.compress(b"".join(rows), 6)

    def chunk(kind: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(kind + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", crc)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    width = 1060
    height = 760
    for flag in ("width", "height"):
        if f"--{flag}" in sys.argv:
            value = int(sys.argv[sys.argv.index(f"--{flag}") + 1])
            if flag == "width":
                width = value
            else:
                height = value

    import tkinter as tk

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from xdao import gui

    root = tk.Tk()
    app = gui.App(root)  # noqa: F841  —— 构建界面本身就是要截的东西
    # 位置压在屏幕左上角、别超出可视区，否则截出来的图下半截是桌面
    root.geometry(f"{width}x{height}+40+20")
    for _ in range(6):
        root.update_idletasks()
        root.update()
    root.after(400, root.quit)
    root.mainloop()

    # winfo_id() 给的就是客户区窗口，直接抓它最稳：
    # （曾经用 GetParent + 窗体坐标抓，结果把标题栏算进去、图整体上移了 31px）
    hwnd = root.winfo_id()
    # 以窗口真实高度为准：Windows 会拒绝比屏幕还高的尺寸，硬按请求值截会截到桌面
    width = root.winfo_width()
    height = root.winfo_height()
    raw = grab(hwnd, height, width)
    out = Path(args[0] if args else ".test-artifacts/gui-shot.png")
    out.parent.mkdir(parents=True, exist_ok=True)
    write_png(out, raw, width, height)
    root.destroy()
    print(f"已保存 {out}（{out.stat().st_size} 字节，{width}x{height}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
