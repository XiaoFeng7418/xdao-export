"""程序入口。

普通启动：python main.py
连接自检：python main.py --selftest
"""

import sys


def selftest() -> int:
    from xdao.client import XdaoClient, XdaoError

    client = XdaoClient()
    print("1) 测试接口连接…")
    try:
        cdn = client.update_cdn_path()
        print("   OK, CDN =", cdn)
    except XdaoError as exc:
        print("   失败:", exc)
        return 1

    print("2) 测试取串接口（No.50000001 第 1 页）…")
    try:
        data = client.fetch_thread_page(50000001, 1)
        if isinstance(data, dict) and data.get("success") is False:
            print("   接口返回错误:", data.get("error"))
            return 1
        title = data.get("title") if isinstance(data, dict) else None
        print("   OK, 标题 =", title)
    except XdaoError as exc:
        print("   失败:", exc)
        return 1

    print("自检完成。")
    return 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        raise SystemExit(selftest())
    from xdao.gui import run

    run()
