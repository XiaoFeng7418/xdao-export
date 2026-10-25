"""``xdao/cdp.py`` 的协议层用例：帧、握手、调试接口、选页与会话。

这里**不碰真浏览器**：帧层用 socketpair 造一对连接；会话层用一个假的
CDP 服务端（同一对 socketpair 的另一端）按命令回话。这样协议细节
（掩码、分片、id 配对、ping/pong、断线）都能逐条钉住，跑起来也快。
"""

from __future__ import annotations

import errno
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import xdao.cdp as cdp


# ---------------------------------------------------------------- 假服务端


class _Server:
    """一对 socketpair 的另一端：扮演浏览器，完成握手后按命令回话。"""

    def __init__(
        self,
        on_call=None,
        *,
        leftover: bytes = b"",
        status: str = "HTTP/1.1 101 Switching Protocols",
        accept: str | None = None,
        close_after_handshake: bool = False,
    ) -> None:
        self.client, self._sock = socket.socketpair()
        self.client.settimeout(10.0)
        self.on_call = on_call
        self.leftover = leftover
        self.status = status
        self.accept = accept
        self.close_after_handshake = close_after_handshake
        self.request = b""
        self.path = ""
        self.sent: list[dict] = []
        self.received: list[dict] = []
        self.frame_count = 0
        self.closed = threading.Event()
        self._thread = threading.Thread(target=self._loop, name="fake-cdp", daemon=True)

    # -- 生命周期 ------------------------------------------------------

    def start(self) -> "_Server":
        self._thread.start()
        return self

    def wait_ready(self, timeout: float = 3.0) -> None:
        """等它把握手响应写完（客户端随后就能发命令）。"""
        assert self._handshake_done.wait(timeout), "假服务端没等到握手"

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            self.client.close()
        except OSError:
            pass
        self._thread.join(timeout=2.0)

    # -- 发送 ----------------------------------------------------------

    def send_raw(self, data: bytes) -> None:
        self._sock.sendall(data)

    def send_frame(self, payload: bytes, opcode: int = cdp.OPCODE_TEXT, fin: bool = True) -> None:
        self.send_raw(cdp.build_frame(payload, opcode=opcode, fin=fin))

    def send_json(self, message: dict) -> None:
        self.sent.append(message)
        self.send_frame(json.dumps(message).encode("utf-8"))

    # -- 内部 ----------------------------------------------------------

    def _loop(self) -> None:
        self._handshake_done = getattr(self, "_handshake_done", threading.Event())
        try:
            self._handshake()
            if self.close_after_handshake:
                self._sock.close()
                return
            reader = cdp._FrameReader(self._sock, self.leftover)
            while True:
                frame = reader.read_frame()
                self.frame_count += 1
                if frame.opcode == cdp.OPCODE_CLOSE:
                    return
                if frame.opcode == cdp.OPCODE_PING:
                    self.send_frame(frame.payload, opcode=cdp.OPCODE_PONG)
                    continue
                if frame.opcode == cdp.OPCODE_PONG:
                    continue
                message = json.loads(frame.payload.decode("utf-8"))
                self.received.append(message)
                if self.on_call is not None:
                    answer = self.on_call(message)
                    if answer is not None:
                        self.send_json(answer)
        except Exception:  # noqa: BLE001 - 假服务端：任何异常都只当「这轮结束了」
            pass
        finally:
            self.closed.set()

    def _handshake(self) -> None:
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self._sock.recv(4096)
            if not chunk:
                return
            head += chunk
        self.request = head
        self.path = head.split(b" ")[1].decode("latin-1")
        key = ""
        for line in head.decode("latin-1").split("\r\n"):
            name, _, value = line.partition(":")
            if name.strip().lower() == "sec-websocket-key":
                key = value.strip()
        if self.status.endswith("101 Switching Protocols"):
            accept = self.accept if self.accept is not None else cdp._accept_key(key)
            head = (
                f"{self.status}\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("latin-1")
        else:
            head = f"{self.status}\r\nContent-Length: 0\r\n\r\n".encode("latin-1")
        self._sock.sendall(head + self.leftover)
        self._handshake_done.set()


@pytest.fixture
def patch_connection(monkeypatch):
    """让 ``CDPSession._attach`` 用假服务端那一端当「连上的 socket」。"""
    state = {"clients": []}

    def install(*servers: _Server) -> None:
        queue = [server.client for server in servers]
        state["clients"].extend(queue)

        def create_connection(address, timeout=None):  # noqa: ARG001 - 签名对齐
            assert queue, "没有更多假连接了"
            return queue.pop(0)

        monkeypatch.setattr(cdp.socket, "create_connection", create_connection)

    return install


def _session(monkeypatch, server: _Server, **kwargs) -> cdp.CDPSession:
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    return cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/AAA", timeout=5.0, **kwargs)


def _echo(message: dict) -> dict:
    return {"id": message["id"], "result": {"ok": message.get("method")}}


# ---------------------------------------------------------------- 帧层


def test_accept_key_matches_the_rfc6455_example() -> None:
    assert cdp._accept_key("dGhlIHNhbXBsZSBub25jZQ==") == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="


def test_build_frame_small_payload_has_the_exact_bytes() -> None:
    frame = cdp.build_frame(b"hi", mask=b"\x00\x00\x00\x00")
    assert frame == b"\x81\x82\x00\x00\x00\x00hi"


def test_build_frame_masks_the_payload() -> None:
    frame = cdp.build_frame(b"abc", mask=b"\x01\x02\x03\x04")
    assert frame[:2] == b"\x81\x83"
    assert frame[2:6] == b"\x01\x02\x03\x04"
    assert frame[6:] == bytes([ord("a") ^ 1, ord("b") ^ 2, ord("c") ^ 3])


def test_build_frame_uses_the_16_bit_length_for_medium_payloads() -> None:
    frame = cdp.build_frame(b"x" * 200, mask=b"\x00\x00\x00\x00")
    assert frame[1] == 0x80 | 126
    assert int.from_bytes(frame[2:4], "big") == 200


def test_build_frame_uses_the_64_bit_length_for_big_payloads() -> None:
    payload = b"x" * 70000
    frame = cdp.build_frame(payload, mask=b"\x00\x00\x00\x00")
    assert frame[1] == 0x80 | 127
    assert int.from_bytes(frame[2:10], "big") == 70000
    assert len(frame) == 2 + 8 + 4 + 70000


def test_build_frame_rejects_a_mask_that_is_not_four_bytes() -> None:
    with pytest.raises(cdp.CdpError):
        cdp.build_frame(b"x", mask=b"\x00\x00\x00")


def test_build_frame_carries_the_fin_flag() -> None:
    head = cdp.build_frame(b"x", mask=b"\x00\x00\x00\x00", fin=False)[0]
    assert head & 0x80 == 0
    assert head & 0x0F == cdp.OPCODE_TEXT


def _reader_of(data: bytes):
    """把一段字节做成 read_exact 回调。"""
    state = {"pos": 0}

    def read_exact(count: int) -> bytes:
        start = state["pos"]
        chunk = data[start : start + count]
        assert len(chunk) == count
        state["pos"] += count
        return chunk

    return read_exact


def test_read_frame_reads_an_unmasked_server_frame() -> None:
    frame = cdp.read_frame(_reader_of(b"\x81\x03abc"))
    assert frame == cdp.Frame(fin=True, opcode=cdp.OPCODE_TEXT, payload=b"abc")


def test_read_frame_unmasks_client_style_frames() -> None:
    raw = cdp.build_frame(b"hello", mask=b"\x0a\x0b\x0c\x0d")
    frame = cdp.read_frame(_reader_of(raw))
    assert frame.payload == b"hello"


@pytest.mark.parametrize("size", [0, 1, 125, 126, 300, 65535, 65536])
def test_read_frame_round_trips_every_length_class(size: int) -> None:
    payload = b"z" * size
    frame = cdp.read_frame(_reader_of(cdp.build_frame(payload, mask=b"\x01\x00\x01\x00")))
    assert frame.payload == payload
    assert frame.fin is True


def test_read_frame_keeps_the_opcode_of_continuation_frames() -> None:
    frame = cdp.read_frame(_reader_of(b"\x00\x01!"))
    assert frame.opcode == cdp.OPCODE_CONTINUATION
    assert frame.fin is False


def test_frame_reader_uses_the_leftover_before_touching_the_socket() -> None:
    server = _Server(leftover=b"")
    server.start()
    try:
        reader = cdp._FrameReader(server.client, cdp.build_frame(b"early", mask=b"\x00" * 4))
        # 余包里已经有一整帧：不需要从 socket 上读任何字节。
        assert reader.read_frame().payload == b"early"
    finally:
        server.close()


class _ClosedSocket:
    """读就返回空字节的假 socket（模拟「对方把连接关了」）。"""

    def recv(self, count: int) -> bytes:  # noqa: ARG002 - 签名对齐
        return b""


def test_frame_reader_reports_a_closed_socket() -> None:
    reader = cdp._FrameReader(_ClosedSocket())  # type: ignore[arg-type]
    with pytest.raises(cdp.CdpError) as info:
        reader.read_exact(4)
    assert "关闭" in str(info.value)


# ---------------------------------------------------------------- 握手与地址


def test_split_ws_url_reads_the_three_pieces() -> None:
    assert cdp._split_ws_url("ws://127.0.0.1:9222/devtools/page/AB") == (
        "127.0.0.1",
        9222,
        "/devtools/page/AB",
    )


def test_split_ws_url_keeps_the_query() -> None:
    assert cdp._split_ws_url("ws://host/devtools/page/AB?x=1") == (
        "host",
        80,
        "/devtools/page/AB?x=1",
    )


def test_split_ws_url_fills_the_default_path() -> None:
    assert cdp._split_ws_url("ws://host:9000")[2] == "/"


@pytest.mark.parametrize("url", ["http://127.0.0.1:9222/json/list", "ws:///nohost", "nonsense"])
def test_split_ws_url_rejects_addresses_that_are_not_ws(url: str) -> None:
    with pytest.raises(cdp.CdpError):
        cdp._split_ws_url(url)


def test_handshake_completes_and_returns_the_leftover_bytes() -> None:
    server = _Server(leftover=b"LEFTOVER")
    server.start()
    try:
        # 直接用假服务端那一端当客户端，省去真连接。
        leftover = cdp._ws_handshake(server.client, "ws://127.0.0.1:9222/devtools/page/AB", 2.0)
    finally:
        server.close()
    assert leftover == b"LEFTOVER"
    assert server.path == "/devtools/page/AB"
    assert b"Sec-WebSocket-Key: " in server.request
    assert b"Host: 127.0.0.1:9222" in server.request


def test_handshake_rejects_a_non_101_answer() -> None:
    server = _Server(status="HTTP/1.1 404 Not Found")
    server.start()
    try:
        with pytest.raises(cdp.CdpError) as info:
            cdp._ws_handshake(server.client, "ws://127.0.0.1:9222/devtools/page/AB", 2.0)
    finally:
        server.close()
    assert "404" in str(info.value)


def test_handshake_rejects_a_wrong_accept_value() -> None:
    server = _Server(accept="not-the-right-value")
    server.start()
    try:
        with pytest.raises(cdp.CdpError) as info:
            cdp._ws_handshake(server.client, "ws://127.0.0.1:9222/devtools/page/AB", 2.0)
    finally:
        server.close()
    assert "校验失败" in str(info.value)


class _HandshakeDeadSocket:
    """发得出请求、但收不到任何应答的假 socket。"""

    def settimeout(self, value: float) -> None:
        return

    def sendall(self, data: bytes) -> None:
        return

    def recv(self, count: int) -> bytes:  # noqa: ARG002 - 签名对齐
        return b""


def test_handshake_reports_the_connection_being_closed() -> None:
    with pytest.raises(cdp.CdpError) as info:
        cdp._ws_handshake(_HandshakeDeadSocket(), "ws://127.0.0.1:9222/devtools/page/AB", 0.5)  # type: ignore[arg-type]
    assert "关闭" in str(info.value)


# ---------------------------------------------------------------- 调试接口


def _serve_http(payload: str, *, status: int = 200, times: int = 1):
    """起一个回固定正文的本地 HTTP 服务，返回 (url, 关掉的函数)。"""
    state = {"calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server 的约定
            state["calls"] += 1
            body = payload.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:  # 别往测试输出里刷日志
            return

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{httpd.server_address[1]}/json/list"
    return url, httpd, state


def test_http_json_once_parses_the_json() -> None:
    url, httpd, _ = _serve_http('[{"type": "page"}]')
    try:
        assert cdp._http_json_once(url, 2.0) == [{"type": "page"}]
    finally:
        httpd.shutdown()
        httpd.server_close()


def test_http_json_once_wraps_failures() -> None:
    with pytest.raises(cdp.CdpError):
        cdp._http_json_once("http://127.0.0.1:1/json/list", 0.2)


def test_http_json_once_wraps_broken_json() -> None:
    url, httpd, _ = _serve_http("not json")
    try:
        with pytest.raises(cdp.CdpError):
            cdp._http_json_once(url, 2.0)
    finally:
        httpd.shutdown()
        httpd.server_close()


class _Refused(OSError):
    def __init__(self, err: int) -> None:
        super().__init__(err, "连接被拒绝")
        self.errno = err
        self.winerror = 10061 if err == errno.ECONNREFUSED else None


class _NoErrno(OSError):
    pass


def test_retryable_read_failure_accepts_the_known_errnos() -> None:
    assert cdp._retryable_read_failure(_Refused(errno.ECONNREFUSED)) is True


def test_retryable_read_failure_accepts_the_windows_code() -> None:
    exc = _NoErrno("失败")
    exc.winerror = 10061
    assert cdp._retryable_read_failure(exc) is True


def test_retryable_read_failure_accepts_timeouts_by_type_and_text() -> None:
    assert cdp._retryable_read_failure(TimeoutError("慢")) is True
    assert cdp._retryable_read_failure(OSError("connection timed out")) is True


def test_retryable_read_failure_follows_the_cause_chain() -> None:
    try:
        try:
            raise _Refused(errno.ECONNREFUSED)
        except OSError as inner:
            raise cdp.CdpError("读取失败") from inner
    except cdp.CdpError as outer:
        assert cdp._retryable_read_failure(outer) is True


def test_retryable_read_failure_is_false_for_other_errors() -> None:
    assert cdp._retryable_read_failure(OSError(errno.EACCES, "没有权限")) is False
    assert cdp._retryable_read_failure(ValueError("坏数据")) is False


def test_http_json_retries_until_the_port_answers(monkeypatch) -> None:
    calls: list[int] = []

    def fake_once(url, timeout=5.0):  # noqa: ARG001 - 签名对齐
        calls.append(1)
        if len(calls) < 3:
            raise cdp.CdpError("读取失败") from _Refused(errno.ECONNREFUSED)
        return [{"type": "page"}]

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    monkeypatch.setattr(cdp.time, "sleep", lambda seconds: None)
    assert cdp._http_json("http://x/json/list", 1.0) == [{"type": "page"}]
    assert len(calls) == 3


def test_http_json_gives_up_after_the_budget(monkeypatch) -> None:
    def fake_once(url, timeout=5.0):  # noqa: ARG001 - 签名对齐
        raise cdp.CdpError("读取失败") from _Refused(errno.ECONNREFUSED)

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    monkeypatch.setattr(cdp.time, "sleep", lambda seconds: None)
    with pytest.raises(cdp.CdpError):
        cdp._http_json("http://x/json/list", 1.0, budget=0.0)


def test_http_json_stops_early_when_the_hint_has_a_reason(monkeypatch) -> None:
    def fake_once(url, timeout=5.0):  # noqa: ARG001 - 签名对齐
        raise cdp.CdpError("读取失败") from _Refused(errno.ECONNREFUSED)

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    with pytest.raises(cdp.CdpError) as info:
        cdp._http_json("http://x/json/list", 1.0, hint=lambda: "浏览器已经退出（测试）")
    assert "浏览器已经退出" in str(info.value)


def test_http_json_does_not_retry_errors_that_will_not_heal(monkeypatch) -> None:
    calls: list[int] = []

    def fake_once(url, timeout=5.0):  # noqa: ARG001 - 签名对齐
        calls.append(1)
        raise cdp.CdpError("坏 JSON") from ValueError("bad")

    monkeypatch.setattr(cdp, "_http_json_once", fake_once)
    with pytest.raises(cdp.CdpError):
        cdp._http_json("http://x/json/list", 1.0)
    assert len(calls) == 1


# ---------------------------------------------------------------- 选页与洗数据


def test_pick_page_skips_internal_pages() -> None:
    pages = [{"url": "edge://sync"}, {"url": "https://a/1"}]
    assert cdp.pick_page(pages)["url"] == "https://a/1"


def test_pick_page_falls_back_to_the_first_page() -> None:
    pages = [{"url": "about:blank"}, {"url": "about:blank"}]
    assert cdp.pick_page(pages) is pages[0]


def test_pick_page_refuses_an_empty_list() -> None:
    with pytest.raises(cdp.CdpError):
        cdp.pick_page([])


def test_pick_site_page_prefers_the_site_prefix() -> None:
    pages = [{"url": "about:blank"}, {"url": "https://site/x"}, {"url": "https://other/y"}]
    assert cdp.pick_site_page(pages, ["https://site"])["url"] == "https://site/x"


def test_pick_site_page_falls_back_to_a_plain_page() -> None:
    pages = [{"url": "edge://sync"}, {"url": "https://other/y"}]
    assert cdp.pick_site_page(pages, ["https://site"])["url"] == "https://other/y"


def test_pick_site_page_ignores_internal_and_blank_pages() -> None:
    pages = [{"url": "devtools://x"}, {"url": ""}]
    assert cdp.pick_site_page(pages, ["https://site"]) is None


def test_pick_site_page_works_without_prefixes() -> None:
    pages = [{"url": "about:blank"}, {"url": "https://other/y"}]
    assert cdp.pick_site_page(pages)["url"] == "https://other/y"


def test_context_ids_are_cleaned_and_filtered() -> None:
    assert cdp._context_ids_of(["A", "", 3, None]) == ["A", "3", "None"]
    assert cdp._context_ids_of("nope") == []


def test_context_ids_come_out_of_the_target_list() -> None:
    raw = [{"browserContextId": "A"}, {"browserContextId": ""}, {"no": 1}, "x"]
    assert cdp._context_ids_of_targets(raw) == ["A"]
    assert cdp._context_ids_of_targets({"a": 1}) == []


def test_describe_exception_prefers_the_description() -> None:
    assert cdp._describe_exception({"exception": {"description": "boom"}}) == "boom"


def test_describe_exception_falls_back_to_the_value_then_text() -> None:
    assert cdp._describe_exception({"exception": {"value": 42}}) == "42"
    assert cdp._describe_exception({"text": "Uncaught"}) == "Uncaught"
    assert cdp._describe_exception("plain") == "plain"


# ---------------------------------------------------------------- 会话层


def test_call_sends_an_id_and_returns_the_result(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.call("Browser.getVersion") == {"ok": "Browser.getVersion"}
    finally:
        session.close()
        server.close()
    assert server.received[0]["id"] == 1
    assert server.received[0]["method"] == "Browser.getVersion"
    assert "params" not in server.received[0]


def test_call_sends_params_when_given(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session.call("Network.getCookies", {"urls": ["https://a"]})
    finally:
        session.close()
        server.close()
    assert server.received[0]["params"] == {"urls": ["https://a"]}


def test_call_pairs_answers_by_id_even_out_of_order(monkeypatch) -> None:
    def on_call(message: dict) -> dict | None:
        return None  # 由下面的 send_json 手动回话

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        result: list[dict] = []

        def worker() -> None:
            result.append(session.call("A"))

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.2)
        first_id = server.received[0]["id"]
        server.send_json({"id": 999, "method": "event"})
        server.send_json({"id": first_id, "result": {"ok": 1}})
        thread.join(timeout=3.0)
        assert result == [{"ok": 1}]
    finally:
        session.close()
        server.close()


def test_call_raises_when_the_browser_reports_an_error(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {"id": message["id"], "error": {"message": "被拒绝"}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        with pytest.raises(cdp.CdpError) as info:
            session.call("X")
    finally:
        session.close()
        server.close()
    assert "被拒绝" in str(info.value)


def test_call_raises_after_the_timeout(monkeypatch) -> None:
    server = _Server(lambda message: None).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        with pytest.raises(cdp.CdpError) as info:
            session.call("Slow", timeout=0.2)
    finally:
        session.close()
        server.close()
    assert "超时" in str(info.value)


def test_call_refuses_after_the_connection_failed(monkeypatch) -> None:
    server = _Server().start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session.close()
        with pytest.raises(cdp.CdpError):
            session.call("X")
    finally:
        server.close()


def test_ping_frames_are_answered_with_pong(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        server.send_frame(b"ping-data", opcode=cdp.OPCODE_PING)
        assert session.call("After") == {"ok": "After"}
    finally:
        session.close()
        server.close()


def test_pong_and_events_are_ignored(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        server.send_frame(b"any", opcode=cdp.OPCODE_PONG)
        server.send_json({"method": "Page.loadEventFired"})
        server.send_frame(b"not json")
        assert session.call("After") == {"ok": "After"}
    finally:
        session.close()
        server.close()


def test_fragmented_messages_are_joined_before_dispatch(monkeypatch) -> None:
    server = _Server(lambda message: {"id": message["id"], "result": {"jo": "ined"}}).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        body = json.dumps({"id": 1, "result": {"jo": "ined"}}).encode()
        server.send_frame(body[:5], opcode=cdp.OPCODE_TEXT, fin=False)
        server.send_frame(body[5:], opcode=cdp.OPCODE_CONTINUATION, fin=True)
        assert session.call("X") == {"jo": "ined"}
    finally:
        session.close()
        server.close()


def test_a_close_frame_ends_the_session(monkeypatch) -> None:
    server = _Server(lambda message: None).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        server.send_frame(b"", opcode=cdp.OPCODE_CLOSE)
        with pytest.raises(cdp.CdpError):
            session.call("X", timeout=2.0)
    finally:
        session.close()
        server.close()


def test_close_sends_a_close_frame_and_is_idempotent(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session.close()
        session.close()
    finally:
        server.close()
    assert server.closed.wait(2.0)


def test_with_block_connects_and_closes(monkeypatch) -> None:
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    with cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A", timeout=5.0) as session:
        session.connect()
        server.wait_ready()
        assert session.call("X") == {"ok": "X"}
    server.close()


def test_connect_raises_a_readable_error_when_the_port_is_dead(monkeypatch) -> None:
    def boom(address, timeout=None):  # noqa: ARG001 - 签名对齐
        raise OSError(errno.ECONNREFUSED, "拒绝")

    monkeypatch.setattr(cdp.socket, "create_connection", boom)
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A")
    with pytest.raises(cdp.CdpError) as info:
        session.connect()
    assert "连不上浏览器的调试端口" in str(info.value)


# ---------------------------------------------------------------- 找页面标签


def _pages(*urls: str) -> list[dict]:
    return [
        {"type": "page", "url": url, "webSocketDebuggerUrl": f"ws://127.0.0.1:9222/devtools/page/{i}"}
        for i, url in enumerate(urls)
    ]


def test_page_ws_url_picks_the_site_page(monkeypatch) -> None:
    session = cdp.CDPSession(
        "http://127.0.0.1:9222/json/list",
        site_urls=["https://site"],
        http_json=lambda url, timeout, **kw: _pages("about:blank", "https://site/x"),
    )
    assert session.page_ws_url().endswith("/1")


def test_page_ws_url_raises_without_pages(monkeypatch) -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A", http_json=lambda *a, **k: [])
    with pytest.raises(cdp.CdpError):
        session.page_ws_url()


def test_page_targets_keep_only_attachable_pages(monkeypatch) -> None:
    raw = [
        {"type": "page", "url": "https://a", "webSocketDebuggerUrl": "ws://a"},
        {"type": "browser", "url": "https://b", "webSocketDebuggerUrl": "ws://b"},
        {"type": "page", "url": "https://c"},
        "junk",
    ]
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A", http_json=lambda *a, **k: raw)
    assert [page["url"] for page in session.list_page_targets()] == ["https://a"]


def test_page_targets_pass_the_failure_hint_when_supported() -> None:
    seen: dict = {}

    def reader(url, timeout, **kwargs):
        seen.update(kwargs)
        return _pages("https://site/x")

    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/A",
        http_json=reader,
        failure_hint=lambda: "浏览器没了",
    )
    session._page_targets()  # noqa: SLF001 - 这里就是要测私有实现
    assert seen["hint"]() == "浏览器没了"


def test_page_targets_fall_back_when_the_reader_ignores_the_hint() -> None:
    calls: list[int] = []

    def reader(url, timeout, **kwargs):  # noqa: ARG001 - 签名对齐
        calls.append(1)
        if kwargs:
            raise TypeError("不吃 hint")
        return _pages("https://site/x")

    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/A",
        http_json=reader,
        failure_hint=lambda: "浏览器没了",
    )
    targets = session._page_targets()  # noqa: SLF001 - 测的就是这条退路
    assert [page["url"] for page in targets] == ["https://site/x"]
    assert len(calls) == 2


def test_list_url_is_built_from_a_ws_address() -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9333/devtools/page/A")
    assert session._list_url() == "http://127.0.0.1:9333/json/list"  # noqa: SLF001


def test_list_url_keeps_an_explicit_http_path() -> None:
    session = cdp.CDPSession("http://127.0.0.1:9333/json")
    assert session._list_url() == "http://127.0.0.1:9333/json"  # noqa: SLF001


def test_list_url_rejects_an_http_address_without_host() -> None:
    session = cdp.CDPSession("http:///json/list")
    with pytest.raises(cdp.CdpError):
        session._list_url()  # noqa: SLF001


def test_connect_waits_for_the_site_page(monkeypatch) -> None:
    seen: list[list[dict]] = [
        _pages("about:blank"),
        _pages("about:blank", "https://site/x"),
    ]

    def reader(url, timeout, **kwargs):  # noqa: ARG001 - 签名对齐
        return seen.pop(0) if len(seen) > 1 else seen[0]

    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    monkeypatch.setattr(cdp, "_POLL_INTERVAL", 0.01)
    session = cdp.CDPSession(
        "http://127.0.0.1:9222/json/list",
        site_urls=["https://site"],
        http_json=reader,
        timeout=2.0,
    )
    try:
        session.connect()
        assert session._ws_url.endswith("/1")  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_connect_falls_back_to_the_first_page_when_none_matches(monkeypatch) -> None:
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    monkeypatch.setattr(cdp, "_POLL_INTERVAL", 0.01)
    session = cdp.CDPSession(
        "http://127.0.0.1:9222/json/list",
        site_urls=["https://site"],
        http_json=lambda url, timeout, **kw: _pages("about:blank"),
        timeout=0.3,
    )
    try:
        session.connect()
        assert session._ws_url.endswith("/0")  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_connect_raises_when_no_page_ever_appears(monkeypatch) -> None:
    session = cdp.CDPSession(
        "http://127.0.0.1:9222/json/list",
        http_json=lambda url, timeout, **kw: [],
        timeout=0.2,
    )
    monkeypatch.setattr(cdp, "_POLL_INTERVAL", 0.01)
    with pytest.raises(cdp.CdpError):
        session.connect()


def test_browser_level_session_skips_page_picking(monkeypatch) -> None:
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/browser/ROOT", browser_level=True, timeout=5.0
    )
    try:
        session.connect()
        assert session._ws_url.endswith("/browser/ROOT")  # noqa: SLF001
        session.connect()
        server.wait_ready()
        assert session.call("Target.getTargets") == {"ok": "Target.getTargets"}
    finally:
        session.close()
        server.close()


# ---------------------------------------------------------------- 换挂


def test_retarget_is_a_no_op_on_a_browser_level_session() -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/browser/ROOT", browser_level=True)
    assert session.retarget() is False


def test_retarget_raises_when_no_pages_exist(monkeypatch) -> None:
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/A",
        site_urls=["https://site"],
        http_json=lambda *a, **k: [],
    )
    with pytest.raises(cdp.CdpError):
        session.retarget()


def test_retarget_stays_put_when_already_on_a_good_site_page(monkeypatch) -> None:
    pages = [{
        "type": "page",
        "url": "https://site/cookies",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1",
    }]
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/1",
        site_urls=["https://site"],
        http_json=lambda *a, **k: pages,
    )
    session._sock = object()  # noqa: SLF001 - 装作「连接还活着」
    assert session.retarget() is False


def test_retarget_moves_off_the_login_page_to_a_logged_in_page(monkeypatch) -> None:
    pages = [
        {"type": "page", "url": "https://site/login.html", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"},
        {"type": "page", "url": "https://site/cookies", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/2"},
    ]
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/1",
        site_urls=["https://site"],
        http_json=lambda *a, **k: pages,
        timeout=5.0,
    )
    try:
        assert session.retarget() is True
        assert session._ws_url.endswith("/2")  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_retarget_attaches_when_the_old_connection_died(monkeypatch) -> None:
    pages = [
        {"type": "page", "url": "https://site/login.html", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/1"},
        {"type": "page", "url": "https://site/cookies", "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/2"},
    ]
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/9",
        site_urls=["https://site"],
        http_json=lambda *a, **k: pages,
        timeout=5.0,
    )
    try:
        assert session.retarget() is True
        assert session._ws_url.endswith("/2")  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_retarget_keeps_the_current_page_when_no_site_page_exists(monkeypatch) -> None:
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/page/A",
        site_urls=["https://site"],
        http_json=lambda *a, **k: _pages("https://other/page"),
    )
    session._sock = object()  # noqa: SLF001 - 连接还活着
    assert session.retarget() is False


# ---------------------------------------------------------------- 读饼干 / 执行脚本


def _cookie_call(message: dict) -> dict:
    method = message["method"]
    if method == "Network.getCookies":
        return {
            "id": message["id"],
            "result": {"cookies": [{"name": "PHPSESSID", "domain": "site"}, "junk"]},
        }
    if method == "Network.getAllCookies":
        return {"id": message["id"], "result": {"cookies": [{"name": "A", "domain": "d", "path": "/", "partitionKey": None}]}}
    if method == "Storage.getCookies":
        return {
            "id": message["id"],
            "result": {
                "cookies": [
                    {"name": "A", "domain": "d", "path": "/", "partitionKey": None},
                    {"name": "B", "domain": "d", "path": "/", "partitionKey": {"topLevelSite": "https://s"}},
                ]
            },
        }
    if method == "Target.getBrowserContexts":
        return {"id": message["id"], "result": {"browserContextIds": ["CTX1", ""]}}
    if method == "Target.getTargets":
        return {
            "id": message["id"],
            "result": {"targetInfos": [{"browserContextId": "CTX2"}, {"browserContextId": "CTX1"}]},
        }
    if method == "Runtime.evaluate":
        return {"id": message["id"], "result": {"result": {"value": "https://site/x"}}}
    return {"id": message["id"], "result": {}}


def test_read_cookies_uses_the_first_site_url_by_default(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server, site_urls=["https://site"])
    try:
        session.connect()
        server.wait_ready()
        cookies = session.read_cookies()
    finally:
        session.close()
        server.close()
    assert cookies == [{"name": "PHPSESSID", "domain": "site"}]
    assert server.received[0]["params"] == {"urls": ["https://site"]}


def test_read_cookies_keeps_only_dicts(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.read_cookies(["https://a"]) == [{"name": "PHPSESSID", "domain": "site"}]
    finally:
        session.close()
        server.close()


def test_browser_context_ids_merge_both_sources_and_dedupe(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.browser_context_ids() == ["CTX1", "CTX2"]
    finally:
        session.close()
        server.close()


def test_browser_context_ids_survive_one_failing_source(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        if message["method"] == "Target.getBrowserContexts":
            return {"id": message["id"], "error": {"message": "只认浏览器端点"}}
        return {"id": message["id"], "result": {"targetInfos": [{"browserContextId": "CTX2"}]}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.browser_context_ids() == ["CTX2"]
    finally:
        session.close()
        server.close()


def test_browser_context_ids_raise_when_both_sources_fail(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {"id": message["id"], "error": {"message": "都不行"}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        with pytest.raises(cdp.CdpError):
            session.browser_context_ids()
    finally:
        session.close()
        server.close()


def test_read_storage_cookies_passes_the_context_id(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        cookies = session.read_storage_cookies("CTX9")
    finally:
        session.close()
        server.close()
    assert [cookie["name"] for cookie in cookies] == ["A", "B"]
    assert server.received[0]["method"] == "Storage.getCookies"
    assert server.received[0]["params"] == {"browserContextId": "CTX9"}


def test_read_storage_cookies_sends_no_params_by_default(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session.read_storage_cookies()
    finally:
        session.close()
        server.close()
    assert "params" not in server.received[0]


def test_read_all_cookies_merge_and_dedupe_by_partition_key(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        cookies = session.read_all_cookies()
    finally:
        session.close()
        server.close()
    assert [cookie["name"] for cookie in cookies] == ["A", "B"]


def test_read_all_cookies_uses_the_working_road_when_one_fails(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        if message["method"] == "Network.getAllCookies":
            return {"id": message["id"], "error": {"message": "不支持"}}
        return {"id": message["id"], "result": {"cookies": [{"name": "S", "domain": "d"}]}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert [cookie["name"] for cookie in session.read_all_cookies()] == ["S"]
    finally:
        session.close()
        server.close()


def test_read_all_cookies_raise_when_both_roads_fail(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {"id": message["id"], "error": {"message": "不行"}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        with pytest.raises(cdp.CdpError):
            session.read_all_cookies()
    finally:
        session.close()
        server.close()


def test_evaluate_returns_the_string_value(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.evaluate("location.href") == "https://site/x"
    finally:
        session.close()
        server.close()


def test_evaluate_serialises_non_string_values(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {"id": message["id"], "result": {"result": {"value": {"a": 1}}}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert json.loads(session.evaluate("({a:1})")) == {"a": 1}
    finally:
        session.close()
        server.close()


def test_evaluate_returns_empty_when_the_page_has_no_value(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {"id": message["id"], "result": {"result": {}}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.evaluate("void 0") == ""
    finally:
        session.close()
        server.close()


def test_evaluate_reports_page_script_errors(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        return {
            "id": message["id"],
            "result": {"exceptionDetails": {"text": "Uncaught ReferenceError"}},
        }

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        with pytest.raises(cdp.CdpError) as info:
            session.evaluate("nope()")
    finally:
        session.close()
        server.close()
    assert "Uncaught ReferenceError" in str(info.value)


def test_current_url_reads_location_href(monkeypatch) -> None:
    server = _Server(_cookie_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.current_url() == "https://site/x"
    finally:
        session.close()
        server.close()


# ---------------------------------------------------------------- 边角与错误路


class _BrokenSocket:
    """连接还在，但发/关都会抛错的假 socket。"""

    def __init__(self, *, fail: str = "send") -> None:
        self.fail = fail

    def settimeout(self, value: float) -> None:
        return

    def sendall(self, data: bytes) -> None:
        if self.fail == "send":
            raise OSError("写不进去")

    def shutdown(self, how: int) -> None:  # noqa: ARG002 - 签名对齐
        raise OSError("关不掉")

    def close(self) -> None:
        raise OSError("关不掉")

    def recv(self, count: int) -> bytes:  # noqa: ARG002 - 签名对齐
        return b""


def test_must_read_frame_says_the_connection_is_not_up_yet() -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A")
    with pytest.raises(cdp.CdpError) as info:
        session._must_read_frame()  # noqa: SLF001
    assert "还没建立" in str(info.value)


def test_send_refuses_without_a_connection() -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A")
    with pytest.raises(cdp.CdpError):
        session._send(b"x")  # noqa: SLF001


def test_send_wraps_socket_errors(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session._sock = _BrokenSocket()  # noqa: SLF001 - 换成发不出去的 sock
        with pytest.raises(cdp.CdpError) as info:
            session._send(b"x")  # noqa: SLF001
        assert "发送命令失败" in str(info.value)
    finally:
        session._frames = None  # noqa: SLF001 - 收尾时别去读那对已经拆掉的 socket
        session._sock = None  # noqa: SLF001
        server.close()


def test_close_swallows_socket_errors(monkeypatch) -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A")
    session._sock = _BrokenSocket(fail="close")  # noqa: SLF001
    session.close()  # 不该抛
    assert session._sock is None  # noqa: SLF001


def test_dispatch_ignores_messages_without_an_integer_id() -> None:
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A")
    session._dispatch(b'{"method": "Page.load"}')  # noqa: SLF001
    session._dispatch(b'"plain string"')  # noqa: SLF001
    session._dispatch(b'{"id": "not-int"}')  # noqa: SLF001


def test_a_continuation_frame_without_a_start_is_dropped(monkeypatch) -> None:
    server = _Server(_echo).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        server.send_frame(b"half", opcode=cdp.OPCODE_CONTINUATION, fin=True)
        server.send_frame(b"ignored", opcode=0x3, fin=True)
        assert session.call("After") == {"ok": "After"}
    finally:
        session.close()
        server.close()


def test_unexpected_read_errors_are_reported(monkeypatch) -> None:
    def boom(self) -> cdp.Frame:  # noqa: ARG001 - 签名对齐
        raise ValueError("坏帧")

    monkeypatch.setattr(cdp._FrameReader, "read_frame", boom)
    server = _Server(lambda message: None).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not session._failure:  # noqa: SLF001
            time.sleep(0.02)
        assert "读取调试连接时出错" in session._failure  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_a_pending_call_is_woken_up_by_close(monkeypatch) -> None:
    server = _Server(lambda message: None).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        result: dict = {}

        def worker() -> None:
            try:
                session.call("Slow", timeout=5.0)
            except cdp.CdpError as exc:
                result["error"] = str(exc)

        thread = threading.Thread(target=worker)
        thread.start()
        time.sleep(0.2)
        session.close()
        thread.join(timeout=3.0)
        assert result.get("error"), "等待中的调用应该被叫醒并报错"
    finally:
        server.close()


def test_read_cookies_returns_nothing_when_the_browser_sends_no_list(monkeypatch) -> None:
    server = _Server(lambda message: {"id": message["id"], "result": {}}).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.read_cookies(["https://a"]) == []
        assert session.read_storage_cookies() == []
    finally:
        session.close()
        server.close()


def test_browser_context_ids_use_the_first_source_when_the_second_fails(monkeypatch) -> None:
    def on_call(message: dict) -> dict:
        if message["method"] == "Target.getTargets":
            return {"id": message["id"], "error": {"message": "不行"}}
        return {"id": message["id"], "result": {"browserContextIds": ["CTX1"]}}

    server = _Server(on_call).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        assert session.browser_context_ids() == ["CTX1"]
    finally:
        session.close()
        server.close()


def test_attach_wraps_handshake_failures(monkeypatch) -> None:
    class _BadHandshake:
        def settimeout(self, value: float) -> None:
            return

        def sendall(self, data: bytes) -> None:
            raise OSError("连一半断了")

        def close(self) -> None:
            return

    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: _BadHandshake()
    )
    session = cdp.CDPSession("ws://127.0.0.1:9222/devtools/page/A", timeout=1.0)
    with pytest.raises(cdp.CdpError) as info:
        session.connect()
    assert "握手失败" in str(info.value)


def test_connect_resolves_a_browser_endpoint_to_a_page(monkeypatch) -> None:
    server = _Server(_echo).start()
    monkeypatch.setattr(
        cdp.socket, "create_connection", lambda address, timeout=None: server.client
    )
    pages = [{
        "type": "page",
        "url": "https://site/x",
        "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/page/7",
    }]
    session = cdp.CDPSession(
        "ws://127.0.0.1:9222/devtools/browser/ROOT",
        site_urls=["https://site"],
        http_json=lambda *a, **k: pages,
        timeout=2.0,
    )
    try:
        session.connect()
        assert session._ws_url.endswith("/7")  # noqa: SLF001
    finally:
        session.close()
        server.close()


def test_call_drops_the_waiter_when_sending_fails(monkeypatch) -> None:
    server = _Server(lambda message: None).start()
    session = _session(monkeypatch, server)
    try:
        session.connect()
        server.wait_ready()
        session._sock = _BrokenSocket()  # noqa: SLF001
        with pytest.raises(cdp.CdpError):
            session.call("X")
        assert session._waiters == {}  # noqa: SLF001 - 不能留下等不到的队列
    finally:
        session._frames = None  # noqa: SLF001
        session._sock = None  # noqa: SLF001
        server.close()
