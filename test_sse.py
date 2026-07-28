"""SSE 实时推送验证脚本：订阅 /api/flows/stream，并发起 HTTP 请求，检查是否收到推送。"""
import threading
import time
import urllib.request
import socket
import sys

BASE = "http://127.0.0.1:18901"
received_events = []
stop_flag = threading.Event()


def sse_subscriber():
    """订阅 SSE 流，记录收到的事件。"""
    try:
        req = urllib.request.Request(f"{BASE}/api/flows/stream")
        resp = urllib.request.urlopen(req, timeout=30)
        buf = b""
        while not stop_flag.is_set():
            chunk = resp.read(1024)
            if not chunk:
                break
            buf += chunk
            while b"\n\n" in buf:
                event_data, buf = buf.split(b"\n\n", 1)
                text = event_data.decode("utf-8", errors="replace")
                for line in text.splitlines():
                    if line.startswith("data:"):
                        received_events.append(line[5:].strip())
                        break
    except Exception as e:
        print(f"[SSE] 异常: {e}")


def main():
    # 启动 SSE 订阅
    t = threading.Thread(target=sse_subscriber, daemon=True)
    t.start()
    time.sleep(1.0)  # 等待订阅建立

    # 记录当前 max_id
    try:
        r = urllib.request.urlopen(f"{BASE}/api/sessions/1/flows?page=1&page_size=1", timeout=5)
        import json
        d = json.loads(r.read())
        max_id_before = d.get("data", {}).get("max_id", 0)
        print(f"[INFO] 订阅前 max_id={max_id_before}")
    except Exception as e:
        print(f"[INFO] 查询 max_id 失败: {e}")
        max_id_before = 0

    # 发起 3 个 HTTP 请求
    print("[INFO] 发起 3 个 HTTP 请求...")
    for i in range(3):
        try:
            urllib.request.urlopen("http://example.com", timeout=10).read()
            print(f"  请求 {i+1} 完成")
        except Exception as e:
            print(f"  请求 {i+1} 失败: {e}")
        time.sleep(0.3)

    # 等待 SSE 推送
    time.sleep(2.0)
    stop_flag.set()

    print(f"\n[结果] 收到 {len(received_events)} 条 SSE 事件")
    for i, ev in enumerate(received_events[-10:]):
        print(f"  事件 {i+1}: {ev[:100]}")

    if len(received_events) > 0:
        print("\n[PASS] SSE 实时推送正常")
        return 0
    else:
        print("\n[FAIL] 未收到 SSE 推送（需要刷新页面才能看到包）")
        return 1


if __name__ == "__main__":
    sys.exit(main())
