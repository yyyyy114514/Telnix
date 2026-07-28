"""复现用户场景：raw + tp + 系统代理 + HTTPS。

用户症状：ping 通，网页一直加载，不弹超时，看门狗不触发。
之前测试只测 HTTP，第一个 502 后续恢复。用户可能遇到的是 HTTPS 的 hang。
"""
from __future__ import annotations

import json
import subprocess
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


def check_proxy() -> bool:
    try:
        r = subprocess.run(
            ["powershell", "-Command",
             "(Get-ItemProperty 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Internet Settings').ProxyEnable"],
            capture_output=True, text=True, timeout=5)
        return r.stdout.strip() == "1"
    except Exception:
        return False


def req_once(url: str, timeout: int = 8) -> tuple[bool, str, float]:
    start = time.time()
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Telnix-HTTPS-Test/1.0"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read(200)
            elapsed = time.time() - start
            return True, f"{r.status} {elapsed:.2f}s", elapsed
    except Exception as e:
        elapsed = time.time() - start
        return False, f"{elapsed:.2f}s {type(e).__name__}: {e}", elapsed


def main() -> int:
    print("=" * 60)
    print("用户场景复现：raw + tp + 系统代理 + HTTP/HTTPS")
    print("=" * 60)

    # 停止全部
    for p in ["/api/raw/stop", "/api/transparent-proxy/stop", "/api/system/clear-proxy"]:
        try:
            api_post(p, timeout=5)
        except Exception:
            pass
    time.sleep(2)

    # 启动
    print("\n[1] 启动 raw capture")
    print(f"    {api_post('/api/raw/start', {})['msg']}")
    time.sleep(1)
    print("[2] 启动 transparent proxy")
    print(f"    {api_post('/api/transparent-proxy/start')['msg']}")
    time.sleep(1)
    print("[3] 启动系统代理")
    print(f"    {api_post('/api/system/enable-proxy')['msg']}")
    time.sleep(1)
    print(f"    系统代理 ProxyEnable = {check_proxy()}")

    # 测试
    print("\n[4] HTTP 测试（5 次）")
    for i in range(1, 6):
        ok, s, _ = req_once(f"http://example.com/?h={i}", timeout=8)
        print(f"    #{i} {'✓' if ok else '×'} {s}")
        time.sleep(0.5)

    print("\n[5] HTTPS 测试（5 次）")
    for i in range(1, 6):
        ok, s, _ = req_once(f"https://example.com/?s={i}", timeout=8)
        print(f"    #{i} {'✓' if ok else '×'} {s}")
        time.sleep(0.5)

    print("\n[6] HTTPS 其他站点（3 次）")
    for i, url in enumerate(["https://httpbin.org/get", "https://www.baidu.com", "https://www.bing.com"], 1):
        ok, s, _ = req_once(url, timeout=10)
        print(f"    #{i} {'✓' if ok else '×'} {url} → {s}")
        time.sleep(0.5)

    # 系统代理仍开着？
    print(f"\n[7] 系统代理仍开着？ProxyEnable = {check_proxy()}")

    # 停止
    api_post("/api/transparent-proxy/stop")
    api_post("/api/raw/stop")
    api_post("/api/system/clear-proxy")
    return 0


if __name__ == "__main__":
    sys.exit(main())
