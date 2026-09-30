"""真机集成测试（默认跳过）：监控发现有新楼 → 自动导出 → 弹桌面通知。

单元测试全部离线，但"通知到底弹没弹出来"只能在真机上验证，所以这里留一个
需要显式打开的真机用例：

    $env:XDAO_LIVE_NOTIFY = "1"
    python -m pytest tests/test_live_notify.py -q -s

它会连真实接口抓一次 No.50000001（X岛官方的测试串），然后在内存里伪造 3 个新楼层，
让监控以为有更新，走完「导出 + 通知」的完整链路，并断言通知发送成功。
默认不跑：CI 上没有桌面、也不该依赖真实网络。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from xdao.client import XdaoClient
from xdao.notifications import Notifier
from xdao.watcher import WatchTarget, check_once, notify_result

LIVE = os.environ.get("XDAO_LIVE_NOTIFY") == "1"
THREAD_ID = 50000001  # X岛官方的测试串

pytestmark = pytest.mark.skipif(
    not LIVE, reason="真机测试：设 XDAO_LIVE_NOTIFY=1 才跑（需要网络和桌面通知）"
)


def test_watch_detects_new_posts_and_notifies(artifacts_dir: Path) -> None:
    root = artifacts_dir / "live-notify"
    cache_dir = root / "cache"
    out_dir = root / "out"
    out_dir.mkdir(parents=True, exist_ok=True)

    real = XdaoClient()
    first = real.fetch_thread_page(THREAD_ID, 1)
    assert isinstance(first, dict), f"接口没有返回预期内容：{type(first).__name__}"

    posts = list(first.get("Replies") or [])
    assert posts, "测试串没抓到楼层，先确认网络是否通"
    base = max((int(p.get("id") or 0) for p in posts), default=0)
    for offset in range(1, 4):
        fake = json.loads(json.dumps(posts[-1]))
        fake["id"] = base + offset
        fake["content"] = f"集成测试用的假楼层 {base + offset}"
        posts.append(fake)

    class FakeClient(XdaoClient):
        """只把第 1 页换成"多了 3 楼"的版本，其余行为全部保持真实。"""

        def fetch_thread_page(self, thread_id, page=1):  # noqa: ANN001
            payload = super().fetch_thread_page(thread_id, page)
            if page == 1 and isinstance(payload, dict):
                payload = dict(payload)
                payload["Replies"] = posts
                payload["ReplyCount"] = len(posts)
            return payload

    target = WatchTarget(str(THREAD_ID), format_key="txt")
    result = check_once(FakeClient(), target, out_dir, cache_dir=cache_dir)

    assert result.ok, f"监控报错：{result.error}"
    assert result.changed, "伪造了新楼层却没判定为有更新"
    assert result.exported is not None and Path(result.exported).exists()

    notifier = Notifier(min_interval=0.0)
    assert notifier.available(), "当前环境发不出桌面通知"
    assert notify_result(notifier, result) is True
