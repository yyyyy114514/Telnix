"""Tech Stack Fingerprint.

Identifies the technology stack used by the target site via response header /
cookie / body signatures:
- Servers: Nginx / Apache / IIS / Caddy
- Languages: PHP / Java / Python / Node.js / ASP.NET / Ruby / Go
- Frameworks: Django / Flask / Spring / Express / Laravel / Rails / ASP.NET MVC
- Frontend: React / Vue / Angular / jQuery / Next.js / Nuxt
- CMS: WordPress / Drupal / Discuz
- CDN / WAF: Cloudflare / Akamai / 阿里云 WAF / 腾讯云 WAF
- Analytics: Google Analytics / 百度统计 / 友盟
- Build tools: Webpack / Vite

Identification results are returned grouped by category, with confidence
(high/medium/low).
"""
from __future__ import annotations

import re
from typing import Optional


# 单条指纹规则
# - name: 技术名称（展示用，如 "React"）
# - category: 类别（server / language / framework / frontend / cms / cdn_waf / analytics / build_tool）
# - confidence: 置信度（high=明确特征 / medium=强特征 / low=弱特征）
# - header_re: 匹配响应头的正则（None=不匹配）
# - cookie_re: 匹配 Set-Cookie 的正则（None=不匹配）
# - body_re: 匹配响应 body 的正则（None=不匹配）
# - version_re: 从 header_re 的第一个分组提取版本号（None=不提取）
_FINGERPRINTS: list[dict] = [
    # ---------- 服务器 ----------
    {
        "name": "Nginx", "category": "server", "confidence": "high",
        "header_re": r"^nginx(?:/([\d.]+))?",
        "header_field": "server", "version_group": 1,
    },
    {
        "name": "Apache", "category": "server", "confidence": "high",
        "header_re": r"^Apache(?:/([\d.]+))?",
        "header_field": "server", "version_group": 1,
    },
    {
        "name": "Apache Tomcat", "category": "server", "confidence": "high",
        "header_re": r"^Apache-Coyote",
        "header_field": "server",
    },
    {
        "name": "IIS", "category": "server", "confidence": "high",
        "header_re": r"^Microsoft-IIS(?:/([\d.]+))?",
        "header_field": "server", "version_group": 1,
    },
    {
        "name": "Caddy", "category": "server", "confidence": "high",
        "header_re": r"^Caddy",
        "header_field": "server",
    },
    {
        "name": "openresty", "category": "server", "confidence": "high",
        "header_re": r"^openresty",
        "header_field": "server",
    },
    {
        "name": "gunicorn", "category": "server", "confidence": "high",
        "header_re": r"^gunicorn(?:/([\d.]+))?",
        "header_field": "server", "version_group": 1,
    },
    {
        "name": "uvicorn", "category": "server", "confidence": "high",
        "header_re": r"^uvicorn",
        "header_field": "server",
    },
    # ---------- 语言 / 后端运行时 ----------
    {
        "name": "PHP", "category": "language", "confidence": "high",
        "header_re": r"^PHP(?:/([\d.]+))?",
        "header_field": "x-powered-by", "version_group": 1,
    },
    {
        "name": "ASP.NET", "category": "language", "confidence": "high",
        "header_re": r"^ASP\.NET",
        "header_field": "x-powered-by",
    },
    {
        "name": "JSP", "category": "language", "confidence": "medium",
        "cookie_re": r"JSESSIONID=",
    },
    {
        "name": "ASP.NET", "category": "language", "confidence": "medium",
        "cookie_re": r"ASP\.NET_SessionId=",
    },
    {
        "name": "PHP", "category": "language", "confidence": "medium",
        "cookie_re": r"PHPSESSID=",
    },
    # ---------- 框架 ----------
    {
        "name": "Django", "category": "framework", "confidence": "high",
        "cookie_re": r"csrftoken=|sessionid=",
    },
    {
        "name": "Flask", "category": "framework", "confidence": "medium",
        "cookie_re": r"session=\.\|",
    },
    {
        "name": "Express", "category": "framework", "confidence": "medium",
        "header_re": r"^Express",
        "header_field": "x-powered-by",
    },
    {
        "name": "Next.js", "category": "framework", "confidence": "high",
        "header_re": r"^nextjs",
        "header_field": "x-powered-by",
    },
    {
        "name": "Laravel", "category": "framework", "confidence": "high",
        "header_re": r"^Laravel",
        "header_field": "x-powered-by",
    },
    {
        "name": "Spring", "category": "framework", "confidence": "high",
        "header_re": r"^Spring",
        "header_field": "x-powered-by",
    },
    # ---------- 前端框架 ----------
    {
        "name": "React", "category": "frontend", "confidence": "high",
        "body_re": r"data-reactroot|__REACT_DEVTOOLS_GLOBAL_HOOK__|react-dom",
    },
    {
        "name": "Vue", "category": "frontend", "confidence": "high",
        "body_re": r"data-v-[a-z0-9]{8}|__VUE_DEVTOOLS_GLOBAL_HOOK__|vue\.",
    },
    {
        "name": "Angular", "category": "frontend", "confidence": "high",
        "body_re": r"ng-version|ng-app|angular(?:\.min)?\.js",
    },
    {
        "name": "jQuery", "category": "frontend", "confidence": "high",
        "body_re": r"jquery(?:\.(?:min|slim))?\.js|jQuery v([\d.]+)",
    },
    {
        "name": "Next.js", "category": "frontend", "confidence": "medium",
        "body_re": r"_next/static|__NEXT_DATA__",
    },
    {
        "name": "Nuxt.js", "category": "frontend", "confidence": "medium",
        "body_re": r"_nuxt/|__NUXT__",
    },
    {
        "name": "Bootstrap", "category": "frontend", "confidence": "medium",
        "body_re": r"bootstrap(?:\.min)?\.(?:css|js)",
    },
    {
        "name": "Tailwind CSS", "category": "frontend", "confidence": "medium",
        "body_re": r"tailwind",
    },
    # ---------- CMS ----------
    {
        "name": "WordPress", "category": "cms", "confidence": "high",
        "body_re": r"wp-content|wp-includes|wp-json",
    },
    {
        "name": "Discuz", "category": "cms", "confidence": "high",
        "body_re": r"discuz_uid|forum\.php\?mod=",
    },
    {
        "name": "Drupal", "category": "cms", "confidence": "high",
        "header_re": r"^Drupal",
        "header_field": "x-generator",
    },
    {
        "name": "Joomla", "category": "cms", "confidence": "high",
        "header_re": r"^Joomla!",
        "header_field": "x-generator",
    },
    # ---------- CDN / WAF ----------
    {
        "name": "Cloudflare", "category": "cdn_waf", "confidence": "high",
        "header_re": r"^cloudflare",
        "header_field": "server",
    },
    {
        "name": "Cloudflare", "category": "cdn_waf", "confidence": "medium",
        "header_re": r"^cloudflare",
        "header_field": "cf-ray",
    },
    {
        "name": "Akamai", "category": "cdn_waf", "confidence": "medium",
        "header_re": r"^AkamaiGHost",
        "header_field": "server",
    },
    {
        "name": "阿里云 CDN", "category": "cdn_waf", "confidence": "medium",
        "header_re": r"^AliSwift|kunlun",
        "header_field": "server",
    },
    {
        "name": "腾讯云 CDN", "category": "cdn_waf", "confidence": "medium",
        "header_re": r"^CDN-X",
        "header_field": "server",
    },
    # ---------- 统计 ----------
    {
        "name": "Google Analytics", "category": "analytics", "confidence": "high",
        "body_re": r"google-analytics\.com/(?:analytics|ga)\.js|gtag/js\?id=",
    },
    {
        "name": "百度统计", "category": "analytics", "confidence": "high",
        "body_re": r"hm\.baidu\.com/hm\.js",
    },
    {
        "name": "友盟+", "category": "analytics", "confidence": "high",
        "body_re": r"web-analytics\.cn/um",
    },
    # ---------- 构建工具 ----------
    {
        "name": "Webpack", "category": "build_tool", "confidence": "medium",
        "body_re": r"webpackjsonp|__webpack_require__",
    },
    {
        "name": "Vite", "category": "build_tool", "confidence": "medium",
        "body_re": r"/@vite/client|/@react-refresh",
    },
]


def fingerprint(
    response_headers: dict | str | None,
    response_body: bytes | str | None,
    set_cookie: str | list[str] | None = None,
) -> list[dict]:
    """Identify the tech stack and return the fingerprint list.

    Parameters:
    - response_headers: dict or JSON string (lowercase keys, string values)
    - response_body: response body (bytes or str, only the first 512KB is used to avoid slow large files)
    - set_cookie: Set-Cookie header (single string or list)

    Returns: [{name, category, confidence, version?}] sorted by category, deduplicated
    """
    # 标准化 headers（dict）
    headers: dict[str, str] = {}
    if response_headers:
        if isinstance(response_headers, str):
            try:
                import json
                headers = {k.lower(): str(v) for k, v in json.loads(response_headers).items()}
            except Exception:  # noqa: BLE001
                pass
        elif isinstance(response_headers, dict):
            headers = {k.lower(): str(v) for k, v in response_headers.items()}

    # 标准化 cookie
    cookies_str = ""
    if set_cookie:
        if isinstance(set_cookie, list):
            cookies_str = "; ".join(str(c) for c in set_cookie if c)
        else:
            cookies_str = str(set_cookie)

    # 标准化 body（截断 512KB 避免大文件慢）
    body_str = ""
    if response_body:
        if isinstance(response_body, bytes):
            try:
                body_str = response_body[:512 * 1024].decode("utf-8", "ignore")
            except Exception:  # noqa: BLE001
                body_str = ""
        else:
            body_str = str(response_body)[:512 * 1024]

    results: dict[tuple[str, str], dict] = {}  # (category, name) -> dict

    for fp in _FINGERPRINTS:
        name = fp["name"]
        category = fp["category"]
        confidence = fp["confidence"]
        version: Optional[str] = None

        # 响应头匹配
        header_field = fp.get("header_field")
        header_re = fp.get("header_re")
        if header_field and header_re and header_field.lower() in headers:
            val = headers[header_field.lower()]
            m = re.search(header_re, val, re.IGNORECASE)
            if m:
                # 提取版本
                vg = fp.get("version_group")
                if vg and vg <= len(m.groups()):
                    version = m.group(vg)
                _add_result(results, name, category, confidence, version)
                continue

        # Cookie 匹配
        cookie_re = fp.get("cookie_re")
        if cookie_re and cookies_str:
            if re.search(cookie_re, cookies_str, re.IGNORECASE):
                _add_result(results, name, category, confidence, version)
                continue

        # Body 匹配
        body_re = fp.get("body_re")
        if body_re and body_str:
            m = re.search(body_re, body_str, re.IGNORECASE)
            if m:
                _add_result(results, name, category, confidence, version)

    # 按类别排序输出
    category_order = {
        "server": 0, "language": 1, "framework": 2,
        "frontend": 3, "cms": 4, "cdn_waf": 5,
        "analytics": 6, "build_tool": 7,
    }
    out = list(results.values())
    out.sort(key=lambda x: (category_order.get(x["category"], 99), x["name"]))
    return out


def _add_result(results: dict, name: str, category: str,
                confidence: str, version: Optional[str]):
    """Add an identification result, deduplicating and keeping the higher confidence.

    Merge rules:
    - Existing and new confidence is higher: upgrade confidence; use the new version (higher-confidence source is more reliable)
    - Existing and new confidence is lower or equal: keep the original confidence; only fill in version when the original is empty
    - New entry: insert directly
    """
    key = (category, name)
    existing = results.get(key)
    if existing is None:
        results[key] = {
            "name": name,
            "category": category,
            "confidence": confidence,
            "version": version,
        }
        return
    conf_order = {"high": 3, "medium": 2, "low": 1}
    new_conf = conf_order.get(confidence, 0)
    old_conf = conf_order.get(existing["confidence"], 0)
    if new_conf > old_conf:
        # 升级：置信度和 version 都用新的
        existing["confidence"] = confidence
        existing["version"] = version
    else:
        # 不升级：仅在原无 version 且新有 version 时补充
        if not existing.get("version") and version:
            existing["version"] = version


def fingerprint_flow(flow: dict) -> list[dict]:
    """Extract tech stack fingerprints from a DB flow dict.

    The flow must contain response_headers / response_body / set_cookie fields.
    """
    return fingerprint(
        response_headers=flow.get("response_headers"),
        response_body=flow.get("response_body"),
        set_cookie=None,  # DB 未单独存 Set-Cookie，已包含在 response_headers 中
    )
