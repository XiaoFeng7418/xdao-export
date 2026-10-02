"""``tools/gui_shot.py`` 的用例。

它是「改完界面怎么自查」那条流程里唯一的一步：抓一张图，人是照着这张图看界面的。
所以这里钉死三件以前会骗人的事：

1. **纯黑图也算「截图成功」**。旧写法只要 BitBlt 不报错就落盘、就报字节数，
   窗口还没画出来（或被别的窗口挡住）时截出来的是一整片同色 —— 拿去当「界面正常」
   的证据，等于什么都没验。现在只有抓到两种以上颜色才算数，一直同色就报「没截成」
   并且**不落盘**。
2. **像素数据不完整也照样写成 PNG**。``GetDIBits`` 少取几行、缓冲区长度不对，
   旧写法会写出一张错位的图。现在这两个地方都会拦下来。
3. **写出去就不管了**。现在落盘之后把 PNG 读回来核对宽高、颜色格式与结尾，
   对不上也算没截成 —— 发过写请求不等于文件写对了。

用例不碰真实桌面与真窗口：``win_libs`` 换成假的 GDI 对象，``build_window`` 换成
假 root，时间与 sleep 都是假的（一秒都不用真等）。
"""

from __future__ import annotations

import ctypes
import struct
import sys
import zlib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))

import gui_shot  # noqa: E402

BASE = b"\x20\x20\x20\xff"
COLORS = (b"\xff\xff\xff\xff", b"\x10\x20\x30\xff", b"\x00\xff\x00\xff", b"\xff\x00\x00\xff")


def raw_of(width: int, height: int, pixel: bytes = BASE) -> bytes:
    """一整片同色的假像素。"""
    return pixel * (width * height)


def picture(width: int, height: int) -> bytes:
    """一张「画出来了」的假图：底色 + 四个不同像素，够 looks_blank 认出不是空的。"""
    raw = bytearray(raw_of(width, height))
    for index, color in enumerate(COLORS):
        offset = index * 4
        raw[offset : offset + 4] = color
    return bytes(raw)


class _FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class _FakeSleep:
    def __init__(self, clock: _FakeClock) -> None:
        self.calls: list[float] = []
        self.clock = clock

    def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)
        self.clock.advance(seconds)


def _header(info):
    """``ctypes.byref()`` 传给 Python 函数时是 CArgObject，不是指针 —— 两种都要认。"""
    obj = getattr(info, "_obj", None)
    if obj is not None:
        return obj.bmiHeader
    return info.contents.bmiHeader


class _FakeLibs:
    """假的 user32 + gdi32：记录调用，可让 BitBlt / GetDIBits / GetWindowDC 故意失败。"""

    def __init__(
        self,
        *,
        bitblt: int = 1,
        rows: int | None = None,
        window_dc: int = 1234,
        bitmap: int = 9012,
        paint=picture,
        paints: list | None = None,
    ) -> None:
        self.bitblt = bitblt
        self.rows = rows
        self.window_dc = window_dc
        self.bitmap = bitmap
        self.paint = paint
        # 想模拟「前几轮还没画出来、后来画好了」就排一张 paints 表，一轮用一个
        self.paints = list(paints) if paints is not None else None
        self.paint_calls = 0
        self.calls: list[tuple] = []

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    # ---- user32
    def GetWindowDC(self, hwnd):  # noqa: N802 —— 就是 Win32 的名字
        self.calls.append(("GetWindowDC", hwnd))
        return self.window_dc

    def ReleaseDC(self, hwnd, hdc):  # noqa: N802
        self.calls.append(("ReleaseDC", hwnd, hdc))
        return 1

    # ---- gdi32
    def CreateCompatibleDC(self, hdc):  # noqa: N802
        self.calls.append(("CreateCompatibleDC", hdc))
        return 5678

    def CreateCompatibleBitmap(self, hdc, width, height):  # noqa: N802
        self.calls.append(("CreateCompatibleBitmap", width, height))
        return self.bitmap

    def SelectObject(self, memdc, bmp):  # noqa: N802
        self.calls.append(("SelectObject", memdc, bmp))
        return 1

    def BitBlt(self, memdc, x, y, width, height, hdc, sx, sy, rop):  # noqa: N802
        self.calls.append(("BitBlt", width, height))
        return self.bitblt

    def GetDIBits(self, memdc, bmp, start, rows, buf, info, kind):  # noqa: N802
        header = _header(info)
        width = header.biWidth
        height = abs(header.biHeight)
        self.calls.append(("GetDIBits", rows, width, height))
        self.paint_calls += 1
        if self.paints is None:
            pick = self.paint
        else:
            pick = self.paints[min(self.paint_calls - 1, len(self.paints) - 1)]
        if self.rows is None or self.rows == rows:
            ctypes.memmove(buf, pick(width, height), width * height * 4)
        return self.rows if self.rows is not None else rows

    def DeleteObject(self, bmp):  # noqa: N802
        self.calls.append(("DeleteObject", bmp))
        return 1

    def DeleteDC(self, memdc):  # noqa: N802
        self.calls.append(("DeleteDC", memdc))
        return 1


class _FakeRoot:
    def __init__(self, *, width: int = 8, height: int = 6, boom_on_destroy: bool = False) -> None:
        self.size = (width, height)
        self.destroyed = 0
        self.pumps = 0
        self.boom_on_destroy = boom_on_destroy

    def winfo_id(self) -> int:
        return 4242

    def winfo_width(self) -> int:
        return self.size[0]

    def winfo_height(self) -> int:
        return self.size[1]

    def update_idletasks(self) -> None:
        pass

    def update(self) -> None:
        self.pumps += 1

    def destroy(self) -> None:
        self.destroyed += 1
        if self.boom_on_destroy:
            raise RuntimeError("窗口已经没了")


def use_root(monkeypatch, root: _FakeRoot) -> _FakeRoot:
    monkeypatch.setattr(gui_shot, "build_window", lambda width, height: root)
    return root


def run_main(monkeypatch, capsys, argv: list[str]) -> tuple[int, str]:
    code = gui_shot.main(argv)
    return code, capsys.readouterr().out


# ---------------------------------------------------------------- 参数


def test_defaults_are_the_documented_ones() -> None:
    assert gui_shot.parse_args([]) == (gui_shot.DEFAULT_OUTPUT, 1060, 760)


def test_output_and_sizes_can_be_given() -> None:
    assert gui_shot.parse_args(["out.png", "--width", "800", "--height", "600"]) == (
        Path("out.png"),
        800,
        600,
    )


def test_inline_equals_form_works() -> None:
    assert gui_shot.parse_args(["--width=800", "--height=600"]) == (
        gui_shot.DEFAULT_OUTPUT,
        800,
        600,
    )


@pytest.mark.parametrize("flag", ["-h", "--help"])
def test_help_prints_usage_and_does_not_open_a_window(monkeypatch, capsys, flag) -> None:
    def boom(width, height, **kwargs):
        raise AssertionError("打印帮助时不该去开窗口")

    monkeypatch.setattr(gui_shot, "capture", boom)
    code, out = run_main(monkeypatch, capsys, [flag])
    assert code == 0
    assert "用法：" in out


def test_unknown_flag_is_refused_before_opening_a_window(monkeypatch, capsys) -> None:
    def boom(width, height, **kwargs):
        raise AssertionError("参数不对时不该去开窗口")

    monkeypatch.setattr(gui_shot, "capture", boom)
    code, out = run_main(monkeypatch, capsys, ["--widht", "800"])
    assert code == 1
    assert "不认识的参数" in out
    assert "结论：没截成（参数不对）" in out


@pytest.mark.parametrize("value", ["abc", "8.5", ""])
def test_non_integer_size_is_refused(monkeypatch, capsys, value) -> None:
    code, out = run_main(monkeypatch, capsys, ["--width", value])
    assert code == 1
    assert "要给整数" in out


@pytest.mark.parametrize("value", ["0", "-1", str(gui_shot.MAX_SIDE + 1)])
def test_out_of_range_size_is_refused(monkeypatch, capsys, value) -> None:
    code, out = run_main(monkeypatch, capsys, ["--height", value])
    assert code == 1
    assert f"1 到 {gui_shot.MAX_SIDE} 之间" in out


def test_missing_size_value_is_refused(monkeypatch, capsys) -> None:
    code, out = run_main(monkeypatch, capsys, ["--width"])
    assert code == 1
    assert "后面要跟一个整数" in out


def test_extra_positional_is_refused(monkeypatch, capsys) -> None:
    code, out = run_main(monkeypatch, capsys, ["a.png", "b.png"])
    assert code == 1
    assert "输出文件只能给一个" in out


# ---------------------------------------------------------------- 空白判定


@pytest.mark.parametrize("pixel", [b"\x00\x00\x00\x00", b"\xff\xff\xff\xff", b"\x20\x20\x20\xff"])
def test_one_color_means_not_painted_yet(pixel) -> None:
    assert gui_shot.looks_blank(raw_of(6, 4, pixel)) is True


def test_two_and_three_colors_are_still_blank() -> None:
    raw = bytearray(raw_of(6, 4))
    raw[0:4] = COLORS[0]
    assert gui_shot.looks_blank(bytes(raw)) is True
    raw[4:8] = COLORS[1]
    assert gui_shot.looks_blank(bytes(raw)) is True


def test_a_real_picture_is_not_blank() -> None:
    assert gui_shot.looks_blank(picture(6, 4)) is False


def test_empty_buffer_counts_as_blank() -> None:
    assert gui_shot.looks_blank(b"") is True


# ---------------------------------------------------------------- PNG 写入与读回


def _read_chunks(data: bytes) -> list[tuple[bytes, bytes]]:
    chunks = []
    index = len(gui_shot.PNG_SIGNATURE)
    while index < len(data):
        (length,) = struct.unpack(">I", data[index : index + 4])
        kind = data[index + 4 : index + 8]
        body = data[index + 8 : index + 8 + length]
        chunks.append((kind, body))
        index += 12 + length
    return chunks


def test_written_png_has_the_right_header_and_ending(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    width, height = 4, 3
    gui_shot.write_png(path, raw_of(width, height), width, height)
    data = path.read_bytes()
    assert data.startswith(gui_shot.PNG_SIGNATURE)
    chunks = _read_chunks(data)
    assert [kind for kind, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    assert struct.unpack(">IIBB", chunks[0][1][:10]) == (width, height, 8, 2)
    # 每行前面一个 filter 字节（这里恒 0），每像素 3 字节（BGRA 丢掉 alpha）
    assert len(zlib.decompress(chunks[1][1])) == height * (1 + width * 3)
    assert chunks[2][1] == b""


def test_alpha_channel_is_dropped(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    gui_shot.write_png(path, raw_of(2, 1, b"\x11\x22\x33\xff"), 2, 1)
    _, body = [chunk for chunk in _read_chunks(path.read_bytes()) if chunk[0] == b"IDAT"][0]
    assert zlib.decompress(body) == b"\x00\x11\x22\x33\x11\x22\x33"


def test_write_png_refuses_a_short_buffer(artifacts_dir: Path) -> None:
    with pytest.raises(gui_shot.ShotError, match="像素数据长度不对"):
        gui_shot.write_png(artifacts_dir / "shot.png", b"\x00" * 12, 4, 3)


def test_write_png_refuses_a_bad_size(artifacts_dir: Path) -> None:
    with pytest.raises(gui_shot.ShotError, match="宽高不对"):
        gui_shot.write_png(artifacts_dir / "shot.png", b"", 0, 3)


def test_verify_png_accepts_what_we_wrote(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    gui_shot.write_png(path, raw_of(4, 3), 4, 3)
    assert gui_shot.verify_png(path, 4, 3) == len(path.read_bytes())


def test_verify_png_catches_a_size_mismatch(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    gui_shot.write_png(path, raw_of(4, 3), 4, 3)
    with pytest.raises(gui_shot.ShotError, match="写出来的是 4x3，本该是 4x4"):
        gui_shot.verify_png(path, 4, 4)


def test_verify_png_catches_a_truncated_file(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    gui_shot.write_png(path, raw_of(4, 3), 4, 3)
    data = path.read_bytes()
    path.write_bytes(data[:-8])
    with pytest.raises(gui_shot.ShotError, match="IEND"):
        gui_shot.verify_png(path, 4, 3)


def test_verify_png_catches_a_foreign_file(artifacts_dir: Path) -> None:
    path = artifacts_dir / "shot.png"
    path.write_bytes(b"not a png at all, just some text")
    with pytest.raises(gui_shot.ShotError, match="不像 PNG"):
        gui_shot.verify_png(path, 4, 3)


def test_verify_png_reports_a_missing_file(artifacts_dir: Path) -> None:
    with pytest.raises(gui_shot.ShotError, match="读不回来"):
        gui_shot.verify_png(artifacts_dir / "nope.png", 4, 3)


# ---------------------------------------------------------------- 抓像素


def test_grab_returns_the_whole_buffer() -> None:
    fake = _FakeLibs()
    raw = gui_shot.grab(77, 6, 8, libs=(fake, fake))
    assert len(raw) == 8 * 6 * 4
    assert gui_shot.looks_blank(raw) is False
    assert fake.names()[0] == "GetWindowDC"
    assert "ReleaseDC" in fake.names()


def test_grab_releases_gdi_objects_when_bitblt_fails() -> None:
    fake = _FakeLibs(bitblt=0)
    with pytest.raises(gui_shot.ShotError, match="BitBlt 失败"):
        gui_shot.grab(77, 6, 8, libs=(fake, fake))
    assert "DeleteObject" in fake.names()
    assert "DeleteDC" in fake.names()
    assert "ReleaseDC" in fake.names()


def test_grab_releases_gdi_objects_when_rows_are_short() -> None:
    fake = _FakeLibs(rows=2)
    with pytest.raises(gui_shot.ShotError, match="只取到 2 行"):
        gui_shot.grab(77, 6, 8, libs=(fake, fake))
    assert "DeleteObject" in fake.names()
    assert "DeleteDC" in fake.names()
    assert "ReleaseDC" in fake.names()


def test_grab_reports_a_missing_window_dc() -> None:
    fake = _FakeLibs(window_dc=0)
    with pytest.raises(gui_shot.ShotError, match="取不到窗口 DC"):
        gui_shot.grab(77, 6, 8, libs=(fake, fake))
    assert "CreateCompatibleDC" not in fake.names()


def test_grab_reports_when_gdi_cannot_build_a_bitmap() -> None:
    fake = _FakeLibs(bitmap=0)
    with pytest.raises(gui_shot.ShotError, match="建不出离屏位图"):
        gui_shot.grab(77, 6, 8, libs=(fake, fake))
    assert "BitBlt" not in fake.names()
    assert "ReleaseDC" in fake.names()


# ---------------------------------------------------------------- 等窗口画出来


def _wire(sizes):
    """假窗口尺寸：每轮取下一个，取完就一直是最后一个（模拟系统兜住尺寸）。"""
    size_iter = iter(sizes)
    last = {"size": sizes[-1]}
    pumps = {"count": 0}

    def size_of():
        try:
            last["size"] = next(size_iter)
        except StopIteration:
            pass
        return last["size"]

    def pump():
        pumps["count"] += 1

    return size_of, pump, pumps


def test_the_first_picture_is_used_right_away() -> None:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    fake = _FakeLibs()
    size_of, pump, pumps = _wire([(8, 6)])
    raw, width, height = gui_shot.grab_until_painted(
        hwnd_of=lambda: 4242,
        size_of=size_of,
        pump=pump,
        libs=(fake, fake),
        clock=clock,
        sleep=sleep,
    )
    assert (width, height) == (8, 6)
    assert len(raw) == 8 * 6 * 4
    assert pumps["count"] == 1
    assert sleep.calls == []


def test_it_waits_until_the_window_is_painted() -> None:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    fake = _FakeLibs(paints=[lambda w, h: raw_of(w, h), lambda w, h: raw_of(w, h), picture])
    size_of, pump, pumps = _wire([(8, 6)] * 4)
    raw, width, height = gui_shot.grab_until_painted(
        hwnd_of=lambda: 4242,
        size_of=size_of,
        pump=pump,
        libs=(fake, fake),
        clock=clock,
        sleep=sleep,
        timeout=5.0,
        interval=1.0,
    )
    assert fake.paint_calls == 3
    assert (width, height) == (8, 6)
    assert gui_shot.looks_blank(raw) is False
    assert pumps["count"] == 3
    assert sleep.calls == [1.0, 1.0]


def test_it_gives_up_with_a_clear_message() -> None:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    fake = _FakeLibs(paint=lambda width, height: raw_of(width, height))
    size_of, pump, _ = _wire([(8, 6)])
    with pytest.raises(gui_shot.ShotError) as info:
        gui_shot.grab_until_painted(
            hwnd_of=lambda: 4242,
            size_of=size_of,
            pump=pump,
            libs=(fake, fake),
            clock=clock,
            sleep=sleep,
            timeout=2.0,
            interval=0.5,
        )
    message = str(info.value)
    assert "一整片同色" in message
    assert "别拿这张图当证据" in message


def test_the_size_is_read_again_every_round() -> None:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    fake = _FakeLibs(paints=[lambda w, h: raw_of(w, h), picture])
    size_of, pump, _ = _wire([(8, 6), (10, 4)])
    raw, width, height = gui_shot.grab_until_painted(
        hwnd_of=lambda: 4242,
        size_of=size_of,
        pump=pump,
        libs=(fake, fake),
        clock=clock,
        sleep=sleep,
        timeout=5.0,
        interval=1.0,
    )
    assert (width, height) == (10, 4)
    assert len(raw) == 10 * 4 * 4


def test_sleep_never_runs_past_the_deadline() -> None:
    clock = _FakeClock()
    sleep = _FakeSleep(clock)
    fake = _FakeLibs(paint=lambda width, height: raw_of(width, height))
    size_of, pump, _ = _wire([(8, 6)])
    with pytest.raises(gui_shot.ShotError):
        gui_shot.grab_until_painted(
            hwnd_of=lambda: 4242,
            size_of=size_of,
            pump=pump,
            libs=(fake, fake),
            clock=clock,
            sleep=sleep,
            timeout=1.0,
            interval=5.0,
        )
    assert sleep.calls == [1.0]
    assert clock.now == 1.0


# ---------------------------------------------------------------- 开窗口 / 收尾


def test_capture_closes_the_window_when_grabbing_fails(monkeypatch) -> None:
    root = use_root(monkeypatch, _FakeRoot())
    fake = _FakeLibs(bitblt=0)
    with pytest.raises(gui_shot.ShotError, match="BitBlt 失败"):
        gui_shot.capture(8, 6, libs=(fake, fake), timeout=0.0, clock=_FakeClock(), sleep=_FakeSleep(_FakeClock()))
    assert root.destroyed == 1


def test_capture_reports_windows_that_cannot_open(monkeypatch) -> None:
    def boom(width, height):
        raise RuntimeError("no display name and no $DISPLAY environment variable")

    monkeypatch.setattr(gui_shot, "build_window", boom)
    with pytest.raises(gui_shot.ShotError, match="开不了窗口"):
        gui_shot.capture(8, 6)


def test_capture_survives_a_window_that_dies_on_close(monkeypatch) -> None:
    use_root(monkeypatch, _FakeRoot(boom_on_destroy=True))
    fake = _FakeLibs()
    raw, width, height = gui_shot.capture(8, 6, libs=(fake, fake), clock=_FakeClock(), sleep=_FakeSleep(_FakeClock()))
    assert (width, height) == (8, 6)
    assert gui_shot.looks_blank(raw) is False


# ---------------------------------------------------------------- main


def test_main_saves_a_checked_png(monkeypatch, capsys, artifacts_dir: Path) -> None:
    out = artifacts_dir / "shot.png"
    monkeypatch.setattr(gui_shot, "capture", lambda width, height, **kwargs: (picture(8, 6), 8, 6))
    code, text = run_main(monkeypatch, capsys, [str(out)])
    assert code == 0
    assert "结论：已保存" in text
    assert out.is_file()
    assert gui_shot.verify_png(out, 8, 6) == out.stat().st_size


def test_main_says_when_the_window_came_out_smaller(monkeypatch, capsys, artifacts_dir: Path) -> None:
    out = artifacts_dir / "shot.png"
    monkeypatch.setattr(gui_shot, "capture", lambda width, height, **kwargs: (picture(8, 6), 8, 6))
    code, text = run_main(monkeypatch, capsys, [str(out), "--width", "1060", "--height", "760"])
    assert code == 0
    assert "! 窗口实际是 8x6" in text


def test_main_reports_a_failed_capture(monkeypatch, capsys, artifacts_dir: Path) -> None:
    out = artifacts_dir / "shot.png"

    def boom(width, height, **kwargs):
        raise gui_shot.ShotError("等了 6.0 秒，抓到的画面还是一整片同色")

    monkeypatch.setattr(gui_shot, "capture", boom)
    code, text = run_main(monkeypatch, capsys, [str(out)])
    assert code == 1
    assert "一整片同色" in text
    assert "结论：没截成" in text
    assert not out.exists()


def test_main_refuses_a_directory_as_output(monkeypatch, capsys, artifacts_dir: Path) -> None:
    target = artifacts_dir / "shot.png"
    target.mkdir()
    monkeypatch.setattr(gui_shot, "capture", lambda width, height, **kwargs: (picture(8, 6), 8, 6))
    code, text = run_main(monkeypatch, capsys, [str(target)])
    assert code == 1
    assert "是个目录" in text


def test_main_catches_a_write_that_came_out_wrong(monkeypatch, capsys, artifacts_dir: Path) -> None:
    out = artifacts_dir / "shot.png"
    real_write = gui_shot.write_png

    def wrong_write(path, raw, width, height):
        # 写出一张「看着像 PNG、尺寸却不对」的图：main 必须自己读回来才发现
        real_write(path, raw_of(8, 4), 8, 4)

    monkeypatch.setattr(gui_shot, "capture", lambda width, height, **kwargs: (picture(8, 6), 8, 6))
    monkeypatch.setattr(gui_shot, "write_png", wrong_write)
    code, text = run_main(monkeypatch, capsys, [str(out)])
    assert code == 1
    assert "写出来的是 8x4" in text
    assert "结论：没截成" in text


def test_main_reports_an_unwritable_path(monkeypatch, capsys, artifacts_dir: Path) -> None:
    blocker = artifacts_dir / "blocker"
    blocker.write_text("here", encoding="utf-8")
    monkeypatch.setattr(gui_shot, "capture", lambda width, height, **kwargs: (picture(8, 6), 8, 6))
    code, text = run_main(monkeypatch, capsys, [str(blocker / "shot.png")])
    assert code == 1
    assert "写不进去" in text
