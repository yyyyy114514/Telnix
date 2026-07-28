"""扩展测试：raw capture + transparent proxy 同时运行，发 20 个请求看是否持续 hang。

最小化测试中 C 组第一个请求 502，后续成功。需要确认：
- 是启动瞬时问题（前几个失败后恢复）？
- 还是持续性问题（间歇性或全部 hang）？
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

API = "http://127.0.0.1:18901"


def api_post(path: str, body: dict | None = None, timeout: float = 15):
    data = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(f"{API}{path}", data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def main() -> int:
    print("=" * 60)
    print("扩展测试：raw + tp 同时，20 个请求")
    print("=" * 60)

    # 停止全部
    for p in ["/api/raw/stop", "/api/transparent-proxy/stop", "/api/system/clear-proxy"]:
        try:
            api_post(p, timeout=5)
        except Exception:
            pass
    time.sleep(2)

    # 启动 raw + tp
    r1 = api_post("/api/raw/start", {})
    print(f"raw start: {r1['msg']}")
    time.sleep(1)
    r2 = api_post("/api/transparent-proxy/start")
    print(f"tp start: {r2['msg']}")
    time.sleep(2)

    # 发 20 个请求
    print("\n开始 20 个 HTTP 请求（超时 6s）：")
    success = 0
    fail = 0
    latencies = []
    for i in range(1, 21):
        url = f"http://example.com/?n={i}"
        start = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Telnix-Ext/1.0"})
            with urllib.request.urlopen(req, timeout=6) as r:
                r.read(200)
                elapsed = time.time() - start
                latencies.append(elapsed)
                success += 1
                mark = "✓" if elapsed < 3 else "~"
                print(f"  #{i:02d} {mark} {r.status} {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - start
            fail += 1
            print(f"  #{i:02d} × {elapsed:.2f}s {type(e).__name__}: {e}")
        time.sleep(0.5)  # 间隔 0.5s

    # 停止
    api_post("/api/transparent-proxy/stop")
    api_post("/api/raw/stop")
    time.sleep(1)

    # 汇总
    print(f"\n{'='*60}")
    print(f"成功 {success} / 失败 {fail}")
    if latencies:
        print(f"延迟：avg={sum(latencies)/len(latencies):.2f}s max={max(latencies):.2f}s min={min(latencies):.2f}s")
    if fail > 0:
        print("× 存在失败 → 多句柄交互导致间歇性 hang")
        return 1
    else:
        print("✓ 全部成功")
        return 0


if __name__ == "__main__":
    sys.exit(main())
