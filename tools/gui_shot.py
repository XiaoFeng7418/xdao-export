"""开发期截图工具：把主窗口抓成 PNG，用于人工核对界面。

只依赖标准库：用 GDI 的 BitBlt 抓像素，再用 zlib 手写 PNG。
不引入任何第三方库，也不进打包产物（tools/ 下的开发脚本）。

用法：
    python tools/gui_shot.py [输出文件] [--width 1060] [--height 760]

两道护栏（2026-10-02 加）—— 这工具以前会「截出一张纯黑图，还报成功」：

* 抓到的画面如果只有一两种颜色（窗口还没画出来、被别的窗口挡住，或者 BitBlt
  什么都没拷到），会重试几秒；一直这样就说「没截成」并且**不落盘**，免得一张
  纯黑图被人拿去当「界面正常」的证据。
* 落盘之后把 PNG 读回来核对宽高与结尾，对不上也算没截成。

退出码：0 = 图存下来了；1 = 没截成（参数不对、窗口没画出来、写不进去、写出来不对）。
"""
from __future__ import annotations

import ctypes
import struct
import sys
import time
import zlib
from ctypes import wintypes
from pathlib import Path

SRCCOPY = 0x00CC0020
BI_RGB = 0

DEFAULT_WIDTH = 1060
DEFAULT_HEIGHT = 760
MAX_SIDE = 10000
PAINT_TIMEOUT = 6.0
PAINT_INTERVAL = 0.2
# 少于这么多种颜色就当成「还没画出来」：真界面有底色、边框、字，颜色远不止几种
BLANK_COLORS = 4
DEFAULT_OUTPUT = Path(".test-artifacts/gui-shot.png")
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

USAGE = """用法：python tools/gui_shot.py [输出文件] [--width N] [--height N]

把主窗口抓成 PNG，用于人工核对界面（纯标准库 GDI 截图）。
默认 1060x760，默认写到 .test-artifacts/gui-shot.png。
窗口实际尺寸由系统决定，屏幕上放不下时会被夹小，工具会照实报出来。
"""


class ShotError(RuntimeError):
    """截图这条路上出的问题：都带一句人话，交给 main() 打印结论。"""


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


def win_libs():
    """真机上要用的两个库。测试里换成假对象，免得碰真实桌面。"""
    return ctypes.windll.user32, ctypes.windll.gdi32


def grab(hwnd: int, height: int, width: int, *, libs=None) -> bytes:
    """抓 hwnd 的客户区像素，返回自上而下的 BGRA 原始字节。

    失败一律抛 ``ShotError``（不是 traceback），而且无论走哪条路都会把
    DC 与位图还回去 —— 旧写法在 BitBlt 失败时直接抛异常，一路漏 GDI 句柄。
    """
    user32, gdi32 = libs if libs is not None else win_libs()
    hdc = user32.GetWindowDC(hwnd)
    if not hdc:
        raise ShotError(f"取不到窗口 DC（hwnd={hwnd}）—— 窗口可能已经关掉了")
    memdc = None
    bmp = None
    try:
        memdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, width, height)
        if not memdc or not bmp:
            raise ShotError("系统建不出离屏位图 —— 可能是 GDI 句柄用完了")
        gdi32.SelectObject(memdc, bmp)
        if not gdi32.BitBlt(memdc, 0, 0, width, height, hdc, 0, 0, SRCCOPY):
            raise ShotError("BitBlt 失败：一个像素都没拷过来")

        info = BITMAPINFO()
        info.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        info.bmiHeader.biWidth = width
        info.bmiHeader.biHeight = -height  # 负数 = 自上而下
        info.bmiHeader.biPlanes = 1
        info.bmiHeader.biBitCount = 32
        info.bmiHeader.biCompression = BI_RGB
        buf = ctypes.create_string_buffer(width * height * 4)
        rows = gdi32.GetDIBits(memdc, bmp, 0, height, buf, ctypes.byref(info), 0)
        if rows != height:
            raise ShotError(f"位图只取到 {rows} 行，应该 {height} 行 —— 窗口可能正在被缩放或已经关了")
        return buf.raw
    finally:
        if bmp:
            gdi32.DeleteObject(bmp)
        if memdc:
            gdi32.DeleteDC(memdc)
        user32.ReleaseDC(hwnd, hdc)


def looks_blank(raw: bytes, *, limit: int = BLANK_COLORS) -> bool:
    """整幅画面是不是只有一两种颜色（= 窗口还没画出来 / 被挡住）。"""
    if len(raw) < 4:
        return True
    seen = {raw[0:4]}
    for index in range(4, len(raw) - 3, 4):
        pixel = raw[index : index + 4]
        if pixel not in seen:
            seen.add(pixel)
            if len(seen) >= limit:
                return False
    return True


def write_png(path: Path, raw: bytes, width: int, height: int) -> None:
    """把 BGRA 原始像素写成 PNG（丢掉 alpha 通道）。"""
    if width <= 0 or height <= 0:
        raise ShotError(f"宽高不对（{width}x{height}）")
    want = width * height * 4
    if len(raw) != want:
        raise ShotError(f"像素数据长度不对（拿到 {len(raw)} 字节，应该 {want} 字节）")
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
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", body)
        + chunk(b"IEND", b"")
    )


def verify_png(path: Path, width: int, height: int) -> int:
    """把写出来的 PNG 读回来核对，返回文件字节数。对不上就抛 ``ShotError``。"""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ShotError(f"写完之后读不回来：{exc}") from exc
    if not data.startswith(PNG_SIGNATURE):
        raise ShotError("写出来的东西不像 PNG（开头不对）")
    if len(data) < 26:
        raise ShotError(f"写出来的 PNG 太短（{len(data)} 字节）")
    length, kind = struct.unpack(">I4s", data[8:16])
    if kind != b"IHDR" or length != 13:
        raise ShotError("PNG 的第一块不是 IHDR")
    got_w, got_h, depth, color = struct.unpack(">IIBB", data[16:26])
    if (got_w, got_h) != (width, height):
        raise ShotError(f"写出来的是 {got_w}x{got_h}，本该是 {width}x{height}")
    if (depth, color) != (8, 2):
        raise ShotError(f"PNG 的颜色格式不对（位深 {depth}、色型 {color}）")
    if b"IEND" not in data[-16:]:
        raise ShotError("PNG 结尾少了 IEND —— 文件被截断了")
    return len(data)


def grab_until_painted(
    *,
    hwnd_of,
    size_of,
    pump,
    libs=None,
    timeout: float = PAINT_TIMEOUT,
    interval: float = PAINT_INTERVAL,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[bytes, int, int]:
    """反复抓，直到画面不止一两种颜色（窗口画出来了）为止。

    每轮都重新读一次窗口尺寸（系统会把窗口夹小、布局也可能还在动），
    返回最后那次的 ``(原始像素, 宽, 高)``。
    """
    deadline = clock() + timeout
    while True:
        pump()
        width, height = size_of()
        raw = grab(hwnd_of(), height, width, libs=libs)
        if not looks_blank(raw):
            return raw, width, height
        remaining = deadline - clock()
        if remaining <= 0:
            raise ShotError(
                f"等了 {timeout:.1f} 秒，抓到的画面还是一整片同色 ——"
                "窗口没画出来、被别的窗口挡住，或者根本没显示出来，别拿这张图当证据"
            )
        sleep(min(interval, remaining))


def build_window(width: int, height: int):
    """按请求尺寸建出主窗口（真 Tk）。返回 root，调用方负责 destroy。"""
    import tkinter as tk

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from xdao import gui

    root = tk.Tk()
    gui.App(root)  # noqa: F841  —— 构建界面本身就是要截的东西
    # 位置压在屏幕左上角、别超出可视区，否则截出来的图下半截是桌面
    root.geometry(f"{width}x{height}+40+20")
    return root


def capture(
    width: int,
    height: int,
    *,
    libs=None,
    timeout: float = PAINT_TIMEOUT,
    interval: float = PAINT_INTERVAL,
    clock=time.monotonic,
    sleep=time.sleep,
) -> tuple[bytes, int, int]:
    """开窗口 → 等它画出来 → 抓像素 → 关窗口。"""
    try:
        root = build_window(width, height)
    except Exception as exc:  # tkinter.TclError：没有显示环境 / 开不出窗口
        raise ShotError(f"开不了窗口：{exc}") from exc
    try:
        return grab_until_painted(
            # winfo_id() 给的就是客户区窗口，直接抓它最稳：
            # （曾经用 GetParent + 窗体坐标抓，结果把标题栏算进去、图整体上移了 31px）
            hwnd_of=root.winfo_id,
            size_of=lambda: (root.winfo_width(), root.winfo_height()),
            pump=lambda: (root.update_idletasks(), root.update()),
            libs=libs,
            timeout=timeout,
            interval=interval,
            clock=clock,
            sleep=sleep,
        )
    finally:
        try:
            root.destroy()
        except Exception:  # 窗口已经没了 / Tk 已经拆了：收尾时不该再抛东西
            pass


def parse_size(name: str, value: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ShotError(f"--{name} 要给整数（收到 {value!r}）") from None
    if not 1 <= number <= MAX_SIDE:
        raise ShotError(f"--{name} 要在 1 到 {MAX_SIDE} 之间（收到 {number}）")
    return number


def parse_args(argv: list[str]) -> tuple[Path, int, int] | None:
    """解析命令行。返回 ``None`` 表示打印了帮助、不该再往下走。"""
    output: Path | None = None
    width = DEFAULT_WIDTH
    height = DEFAULT_HEIGHT
    index = 0
    while index < len(argv):
        item = argv[index]
        if item in ("-h", "--help"):
            return None
        if item.startswith("--"):
            name, _, inline = item[2:].partition("=")
            if name not in ("width", "height"):
                raise ShotError(f"不认识的参数 {item!r}（只有 --width / --height / --help）")
            if inline:
                value = inline
            else:
                index += 1
                if index >= len(argv):
                    raise ShotError(f"--{name} 后面要跟一个整数")
                value = argv[index]
            number = parse_size(name, value)
            if name == "width":
                width = number
            else:
                height = number
        elif output is None:
            output = Path(item)
        else:
            raise ShotError(f"多余的参数 {item!r}（输出文件只能给一个）")
        index += 1
    return (output or DEFAULT_OUTPUT), width, height


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        parsed = parse_args(argv)
    except ShotError as exc:
        print(f"× {exc}")
        print("结论：没截成（参数不对）")
        return 1
    if parsed is None:
        print(USAGE)
        return 0

    output, width, height = parsed
    try:
        raw, real_width, real_height = capture(width, height)
        if output.exists() and output.is_dir():
            raise ShotError(f"输出路径是个目录：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        write_png(output, raw, real_width, real_height)
        size = verify_png(output, real_width, real_height)
    except ShotError as exc:
        print(f"× {exc}")
        print("结论：没截成")
        return 1
    except OSError as exc:
        print(f"× 写不进去：{exc}")
        print("结论：没截成")
        return 1

    if (real_width, real_height) != (width, height):
        print(f"! 窗口实际是 {real_width}x{real_height}（请求的是 {width}x{height}，屏幕上放不下时系统会夹小）")
    print(f"结论：已保存 {output}（{real_width}x{real_height}，{size} 字节）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
