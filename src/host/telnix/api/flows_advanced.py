"""高级流量分析 API：延迟统计、交叉分析、异常检测、报表导出。

提供 P1 功能增强的后端支持：
1. 响应时间深度分析（延迟分布直方图 + P50/P75/P90/P95/P99）
2. 多维度交叉分析（Host x Status Code、Content-Type x Size）
3. 智能异常检测（高延迟、错误率、流量突增/突降）
4. 增强报表导出（JSON/CSV/HTML）
"""

import csv
import io
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse

from .. import db
from . import ok

router = APIRouter()


# ---------- 延迟统计 ----------

@router.get("/flows/latency-stats")
async def get_latency_stats(
    host: str | None = Query(None, description="按 host 过滤"),
    process: str | None = Query(None, description="按进程过滤"),
    limit: int = Query(50000, ge=1000, le=200000, description="采样上限"),
) -> Any:
    """响应时间深度分析：延迟分布直方图 + P50/P75/P90/P95/P99 统计。

    返回结构：
    {
      histogram: [{range, label, count, pct}],  # <50ms, 50-200ms, 200-500ms, 500ms-1s, >1s
      percentiles: {p50, p75, p90, p95, p99},
      total: int,
      avg_ms: float,
      slow_count: int,   # >1s 的慢请求数
      slow_pct: float,
    }
    """
    result = db.get_delay_stats(host=host, process=process, limit=limit)
    return ok(result)


# ---------- 交叉分析 ----------

@router.get("/flows/cross-analysis")
async def get_cross_analysis(
    host: str | None = Query(None, description="按 host 过滤"),
    process: str | None = Query(None, description="按进程过滤"),
    limit: int = Query(50000, ge=1000, le=200000, description="采样上限"),
) -> Any:
    """多维度交叉分析：Host x Status Code、Content-Type x Size。

    返回结构：
    {
      host_status: {
        dimensions: [host_labels],
        buckets: [status_labels],
        matrix: [[count]]
      },
      content_size: {
        dimensions: [content_types],
        buckets: [size_ranges],
        matrix: [[count]]
      }
    }
    """
    result = db.get_cross_analysis(host=host, process=process, limit=limit)
    return ok(result)


# ---------- 异常检测 ----------

@router.get("/flows/anomalies")
async def get_anomalies(
    host: str | None = Query(None, description="按 host 过滤"),
    process: str | None = Query(None, description="按进程过滤"),
    limit: int = Query(10000, ge=1000, le=100000, description="采样上限"),
) -> Any:
    """智能异常检测：高延迟、错误率、流量突增/突降。

    返回结构：
    {
      anomalies: [{type, severity, message, details, count}],
      summary: {total, error_count, error_rate, avg_ms, anomaly_count}
    }
    """
    result = db.detect_anomalies(host=host, process=process, limit=limit)
    return ok(result)


# ---------- 报表导出 ----------

@router.get("/flows/export-report")
async def export_report(
    format: str = Query("json", description="导出格式: json | csv | html"),
    host: str | None = Query(None, description="按 host 过滤"),
    process: str | None = Query(None, description="按进程过滤"),
    limit: int = Query(50000, ge=1000, le=100000, description="采样上限"),
) -> StreamingResponse:
    """增强报表导出：JSON/CSV/HTML 格式，包含摘要、图表数据、Top 端点、异常列表。

    采样最近的 limit 条流量进行分析。
    """
    # 收集数据
    latency_stats = db.get_delay_stats(host=host, process=process, limit=limit)
    cross_analysis = db.get_cross_analysis(host=host, process=process, limit=limit)
    anomalies = db.detect_anomalies(host=host, process=process, limit=limit)

    # 获取 Top 端点
    try:
        endpoints = db.get_flows_endpoint_stats(limit=2000)
        top_endpoints = sorted(endpoints, key=lambda x: -x.get("count", 0))[:20]
    except Exception:
        top_endpoints = []

    # 统计摘要
    total = latency_stats.get("total", 0)
    summary = {
        "generated_at": datetime.now().isoformat(),
        "filter": {"host": host, "process": process},
        "sample_size": total,
        "latency_stats": latency_stats,
        "anomalies": anomalies,
        "top_endpoints": top_endpoints,
    }

    if format == "json":
        content = json.dumps(summary, ensure_ascii=False, indent=2)
        return StreamingResponse(
            io.BytesIO(content.encode("utf-8")),
            media_type="application/json",
            headers={"Content-Disposition": "attachment; filename=telnix_report.json"},
        )

    elif format == "csv":
        output = io.StringIO()
        # 延迟分布
        output.write("# 延迟分布\n")
        output.write("Range,Count,Percentage\n")
        for h in latency_stats.get("histogram", []):
            output.write(f"{h['range']},{h['count']},{h['pct']}%\n")
        output.write("\n# 分位数\n")
        output.write("Percentile,Value(ms)\n")
        for p, v in latency_stats.get("percentiles", {}).items():
            output.write(f"{p.upper()},{v}\n")
        output.write("\n# 异常列表\n")
        output.write("Type,Severity,Message,Count\n")
        for a in anomalies.get("anomalies", []):
            output.write(f"{a['type']},{a['severity']},{a['message']},{a.get('count', 0)}\n")
        output.write("\n# Top 端点\n")
        output.write("Method,Host,Path,Count,StatusCodes\n")
        for ep in top_endpoints:
            sc = ",".join(str(s) for s in ep.get("status_codes", []))
            output.write(f"{ep.get('method','')},{ep.get('host','')},{ep.get('path_template','')},{ep.get('count',0)},{sc}\n")

        return StreamingResponse(
            io.BytesIO(output.getvalue().encode("utf-8")),
            media_type="text/csv",
            headers={"Content-Disposition": "attachment; filename=telnix_report.csv"},
        )

    else:  # html
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Telnix 流量分析报表</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; margin: 40px; background: #f5f7fa; }}
  .container {{ max-width: 1200px; margin: 0 auto; }}
  h1 {{ color: #1a1a2e; border-bottom: 2px solid #409eff; padding-bottom: 10px; }}
  h2 {{ color: #2c3e50; margin-top: 30px; }}
  .meta {{ color: #666; font-size: 14px; margin-bottom: 20px; }}
  .card {{ background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 10px; }}
  th, td {{ border: 1px solid #e0e0e0; padding: 10px 12px; text-align: left; }}
  th {{ background: #f8f9fa; font-weight: 600; color: #2c3e50; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  .p50 {{ color: #409eff; font-weight: bold; }}
  .p95 {{ color: #e6a23c; font-weight: bold; }}
  .p99 {{ color: #f56c6c; font-weight: bold; }}
  .alert {{ padding: 12px 16px; border-radius: 4px; margin: 8px 0; }}
  .alert-warning {{ background: #fdf6ec; border-left: 4px solid #e6a23c; }}
  .alert-error {{ background: #fef0f0; border-left: 4px solid #f56c6c; }}
  .alert-info {{ background: #f4f9ff; border-left: 4px solid #409eff; }}
  .badge {{ display: inline-block; padding: 2px 8px; border-radius: 3px; font-size: 12px; }}
  .badge-warning {{ background: #e6a23c; color: white; }}
  .badge-error {{ background: #f56c6c; color: white; }}
  .badge-info {{ background: #409eff; color: white; }}
  .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; margin-bottom: 20px; }}
  .summary-item {{ background: white; padding: 16px; border-radius: 8px; text-align: center; box-shadow: 0 2px 8px rgba(0,0,0,0.08); }}
  .summary-value {{ font-size: 28px; font-weight: bold; color: #409eff; }}
  .summary-label {{ color: #666; font-size: 13px; margin-top: 4px; }}
</style>
</head>
<body>
<div class="container">
  <h1>Telnix 流量分析报表</h1>
  <div class="meta">
    生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |
    采样数量: {total} |
    过滤条件: {f"host={host}" if host else ""}{f"process={process}" if process else "全部"}
  </div>

  <!-- 统计摘要 -->
  <div class="summary-grid">
    <div class="summary-item">
      <div class="summary-value">{total}</div>
      <div class="summary-label">总请求数</div>
    </div>
    <div class="summary-item">
      <div class="summary-value">{latency_stats.get('avg_ms', 0):.1f}ms</div>
      <div class="summary-label">平均延迟</div>
    </div>
    <div class="summary-item">
      <div class="summary-value p95">{latency_stats.get('percentiles', {}).get('p95', 0)}ms</div>
      <div class="summary-label">P95 延迟</div>
    </div>
    <div class="summary-item">
      <div class="summary-value">{anomalies.get('summary', {}).get('error_rate', 0):.1f}%</div>
      <div class="summary-label">错误率</div>
    </div>
  </div>

  <!-- 延迟分布 -->
  <div class="card">
    <h2>延迟分布</h2>
    <table>
      <tr><th>范围</th><th>数量</th><th>占比</th></tr>
      {"".join(f"<tr><td>{h['range']}</td><td>{h['count']}</td><td>{h['pct']}%</td></tr>" for h in latency_stats.get('histogram', []))}
    </table>
  </div>

  <!-- 分位数 -->
  <div class="card">
    <h2>延迟分位数</h2>
    <table>
      <tr><th>百分位</th><th>延迟 (ms)</th></tr>
      <tr><td class="p50">P50</td><td class="p50">{latency_stats.get('percentiles', {}).get('p50', 0)}</td></tr>
      <tr><td>P75</td><td>{latency_stats.get('percentiles', {}).get('p75', 0)}</td></tr>
      <tr><td>P90</td><td>{latency_stats.get('percentiles', {}).get('p90', 0)}</td></tr>
      <tr><td class="p95">P95</td><td class="p95">{latency_stats.get('percentiles', {}).get('p95', 0)}</td></tr>
      <tr><td class="p99">P99</td><td class="p99">{latency_stats.get('percentiles', {}).get('p99', 0)}</td></tr>
    </table>
  </div>

  <!-- 异常检测 -->
  <div class="card">
    <h2>异常检测 ({anomalies.get('summary', {}).get('anomaly_count', 0)} 项)</h2>
    {"".join(f"""
    <div class="alert alert-{a['severity']}">
      <span class="badge badge-{a['severity']}">{a['severity']}</span>
      {a['message']}
    </div>""" for a in anomalies.get('anomalies', []))}
    {"<p style='color:#999'>未检测到明显异常</p>" if not anomalies.get('anomalies') else ""}
  </div>

  <!-- Top 端点 -->
  <div class="card">
    <h2>Top 20 端点</h2>
    <table>
      <tr><th>方法</th><th>Host</th><th>路径</th><th>请求数</th><th>状态码</th></tr>
      {"".join(f"<tr><td>{ep.get('method','')}</td><td>{ep.get('host','')}</td><td>{ep.get('path_template','')}</td><td>{ep.get('count',0)}</td><td>{','.join(str(s) for s in ep.get('status_codes',[]))}</td></tr>" for ep in top_endpoints)}
    </table>
  </div>
</div>
</body>
</html>"""

        return StreamingResponse(
            io.BytesIO(html.encode("utf-8")),
            media_type="text/html",
            headers={"Content-Disposition": "attachment; filename=telnix_report.html"},
        )
