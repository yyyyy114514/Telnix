"""最小化分步测试：定位是哪个组件导致 HTTP hang。

用户症状：raw capture + transparent proxy 同时运行时，ping 通但网页一直加载。
分步隔离：
  A. 仅 raw capture（SNIFF）→ 测 HTTP
  B. 仅 transparent proxy → 测 HTTP
  C. 两者同时 → 测 HTTP（复现 hang）

每步：发 3 个 HTTP 请求（5s 超时），全成功=绿，任一 hang/超时=红。
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


def stop_all():
    for p in ["/api/raw/stop", "/api/transparent-proxy/stop", "/api/system/clear-proxy"]:
        try:
            api_post(p, timeout=5)
        except Exception:
            pass
    time.sleep(2)


def http_test(label: str, count: int = 3, timeout: int = 5) -> bool:
    """发 count 个 HTTP 请求，全成功返回 True。"""
    print(f"  HTTP 测试（{count} 次请求，超时 {timeout}s）：")
    all_ok = True
    for i in range(1, count + 1):
        url = f"http://example.com/?t={int(time.time())}-{i}"
        start = time.time()
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Telnix-Minimal/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read(200)
                elapsed = time.time() - start
                print(f"    #{i} ✓ HTTP {r.status} {elapsed:.2f}s")
        except Exception as e:
            elapsed = time.time() - start
            print(f"    #{i} × FAIL {elapsed:.2f}s {type(e).__name__}: {e}")
            all_ok = False
    return all_ok


def main() -> int:
    print("=" * 60)
    print("最小化分步测试：定位 HTTP hang 根因")
    print("=" * 60)

    # 确保全部停止
    print("\n[0] 停止所有抓包/代理...")
    stop_all()
    if not http_test("基线（无抓包无代理）"):
        print("\n× 基线就 hang，网络本身有问题，先修复网络")
        return 1
    print("  ✓ 基线正常\n")

    # A. 仅 raw capture
    print("[A] 仅 raw capture（SNIFF 模式）")
    r = api_post("/api/raw/start", {})
    print(f"  raw start: code={r['code']} msg={r['msg']}")
    if r["code"] != 0:
        return 1
    time.sleep(1)
    ok_a = http_test("A")
    api_post("/api/raw/stop")
    time.sleep(2)
    print(f"  → {'✓ 通过' if ok_a else '× HANG'}\n")

    # B. 仅 transparent proxy
    print("[B] 仅 transparent proxy")
    r = api_post("/api/transparent-proxy/start")
    print(f"  tp start: code={r['code']} msg={r['msg']}")
    if r["code"] != 0:
        return 1
    time.sleep(1)
    ok_b = http_test("B")
    api_post("/api/transparent-proxy/stop")
    time.sleep(2)
    print(f"  → {'✓ 通过' if ok_b else '× HANG'}\n")

    # C. 两者同时
    print("[C] raw capture + transparent proxy 同时")
    r1 = api_post("/api/raw/start", {})
    print(f"  raw start: code={r1['code']} msg={r1['msg']}")
    time.sleep(1)
    r2 = api_post("/api/transparent-proxy/start")
    print(f"  tp start: code={r2['code']} msg={r2['msg']}")
    time.sleep(1)
    ok_c = http_test("C", count=5, timeout=8)
    api_post("/api/transparent-proxy/stop")
    api_post("/api/raw/stop")
    time.sleep(2)
    print(f"  → {'✓ 通过' if ok_c else '× HANG'}\n")

    # 汇总
    print("=" * 60)
    print("汇总")
    print("=" * 60)
    print(f"A. 仅 raw capture:        {'✓ 通过' if ok_a else '× HANG'}")
    print(f"B. 仅 transparent proxy:  {'✓ 通过' if ok_b else '× HANG'}")
    print(f"C. 两者同时:              {'✓ 通过' if ok_c else '× HANG'}")

    if ok_a and ok_b and not ok_c:
        print("\n→ 结论：raw capture + transparent proxy 多句柄交互导致 hang")
    elif not ok_a:
        print("\n→ 结论：raw capture 单独就会 hang")
    elif not ok_b:
        print("\n→ 结论：transparent proxy 单独就会 hang")
    else:
        print("\n→ 结论：全部通过，无法复现（可能需要更长时间/更多请求）")

    return 0 if (ok_a and ok_b and ok_c) else 1


if __name__ == "__main__":
    sys.exit(main())
