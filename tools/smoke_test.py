#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""Zotero 检索索引插件 —— 冒烟回归脚本（T2）

用途
  一条命令验证「插件 HTTP 接口 + 索引健康 + 6 条固定查询 + 排序漂移」，
  用固定退出码给出能否发版/交付的明确信号。

一条命令
  python tools/smoke_test.py

三组检查
  1. ping  : HTTP 200 / ok==true / startupError==null / 打印 version
  2. stats : loaded==true / hasVectors==true / dim==1024 / build.running==false
             / chunks >= --min-chunks（默认 40000）
  3. search: 6 条固定查询（questions.json 原文，k=5，mode=hybrid）
             每个 HTTP 200 + 结构断言（evidence 字段齐全、results 非空、
             每条有 itemKey/title/text、pageSource 属于已知集合）+ 耗时断言
             6 条合计：page != null 的结果数 >= --min-paged（默认 15）

排序漂移（回归价值核心）
  - 先冻结基准：python tools/smoke_test.py --freeze
    写 tools/smoke_expected.json，记录每条查询 top-5 的 parentKey/itemKey
    集合与 top1（另含标题、页码等仅作展示的元数据）。
  - 默认运行对比基准：
      top1 变化             -> WARN（索引每日增量会自然漂移，不判失败）
      top-5 命中 < 阈值      -> WARN（默认 3/5，并打印缺失/新增差异）
      接口挂起/空结果/结构变化 -> FAIL
    注意：同一文献的不同片段可能同时进入 top-5（parentKey/itemKey 相同），
    因此命中数按「条目多重集」计数，而不是集合求交（否则冻结后立刻复跑
    也会出现 4/5 的假漂移）。
  阈值依据（2026-10-23 实测插件 v0.1.21：52,298 块 / 739 篇 / 1024 维 bge-m3）
      ping ~2-7ms、stats ~6-12ms、6 条检索各约 120~230ms、合计 23/30 条有真实页码
      => --max-search-ms 默认 10000（实测约 200ms，留 40 倍以上余量）
      => --min-paged 默认 15（实测 23，留出索引增量波动余量）

超时与预算（历史坑）
  Zotero 服务端按 init.length 分派端点，写错会让请求永久挂起。
  因此每个请求都带独立超时（ping/stats 10s、search 45s），并且整脚本有
  60s 总预算：剩余预算不足时单请求超时会被自动收紧，脚本绝不挂死。

代理
  本机系统代理 127.0.0.1:10801 会拦截对 127.0.0.1 的访问；脚本用 urllib 的
  ProxyHandler({}) 显式禁用代理（等价 curl --noproxy '*'）。

参数
  --base-url URL       默认 http://127.0.0.1:23119
  --k N                top-k，默认 5
  --min-chunks N       块数下限，默认 40000
  --min-paged N        6 条查询合计真实页码下限，默认 15
  --freeze             生成/更新黄金基准 smoke_expected.json
  --json               机器可读 JSON 输出
  --skip-queries       只查 ping/stats（跳过检索与漂移对比）
  --min-golden-hits N  漂移 WARN 阈值，默认 3/5
  --max-search-ms N    单条检索耗时 FAIL 阈值，默认 10000
  --timeout-ping N     ping/stats 单请求超时秒，默认 10
  --timeout-search N   search 单请求超时秒，默认 45
  --total-budget N     全脚本总预算秒，默认 60
  --golden-file PATH   自定义黄金文件路径

退出码
  0 = 全 PASS 或仅 WARN（索引正常演进）
  1 = 存在 FAIL（接口/结构/空结果/阈值/耗时等真实故障）
  2 = 参数错误

仅依赖 Python 标准库；Windows 控制台强制 UTF-8 输出。
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from dataclasses import dataclass, field

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass
try:
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# ---------------- 常量 ----------------

DEFAULT_BASE_URL = "http://127.0.0.1:23119"
DEFAULT_K = 5
DEFAULT_MIN_CHUNKS = 40000
DEFAULT_MIN_PAGED = 15
DEFAULT_MIN_GOLDEN_HITS = 3
DEFAULT_MAX_SEARCH_MS = 10000.0
DEFAULT_TIMEOUT_PING = 10.0
DEFAULT_TIMEOUT_SEARCH = 45.0
DEFAULT_TOTAL_BUDGET = 60.0

SEARCH_MODE = "hybrid"
PLUGIN_NAME = "retrieval-index"
EXPECTED_DIM = 1024
GOLDEN_SCHEMA = 1
GOLDEN_NAME = "smoke_expected.json"

# 6 条固定研究问题（与 D:\zotero\eval_report\questions.json 逐字一致；
# 硬编码避免脚本依赖仓库外路径，黄金文件里同时记录 q 文本以检测问题变更）
QUERIES = (
    ("q1", "儿童色彩偏好随年龄变化的规律是什么？"),
    ("q2", "不同性别儿童在色彩偏好上的差异表现在哪里？"),
    ("q3", "色彩的饱和度与明度如何影响儿童的情绪和唤醒水平？"),
    ("q4", "小学教室或走廊的墙面色彩如何影响学生的行为与注意力？"),
    ("q5", "幼儿园活动室的色彩设计有哪些具体的做法或建议？"),
    ("q6", "暖色与冷色环境对儿童认知任务表现有什么不同影响？"),
)

# 搜索结果 pageSource 的已知取值（见 content/retrieval.js：chunk.pageSource 为
# pdf/ocr/cache；结果字段兜底逻辑会产生 unknown，属旧索引/元数据缺失信号）
KNOWN_PAGE_SOURCES = ("pdf", "ocr", "cache", "unknown")
LEGACY_PAGE_SOURCE = "unknown"

EVIDENCE_KEYS = (
    "returned",
    "papers",
    "abstractOnlySegments",
    "cacheSourcedSegments",
    "segmentsWithPage",
    "topSegmentRole",
    "retractedFiltered",
    "orphansIncluded",
    "orphansFiltered",
    "hints",
)
EVIDENCE_INT_KEYS = (
    "returned",
    "papers",
    "abstractOnlySegments",
    "cacheSourcedSegments",
    "segmentsWithPage",
    "retractedFiltered",
    "orphansFiltered",
)

SECTION_TITLES = {
    "health": "一、接口健康",
    "query": "二、固定查询",
    "aggregate": "三、聚合断言",
    "golden": "四、排序漂移对比（黄金基准）",
    "budget": "五、总预算",
}
GROUP_ORDER = ("health", "query", "aggregate", "golden", "budget")


# ---------------- 基础工具 ----------------


def _iso_now() -> str:
    return _dt.datetime.now().astimezone().isoformat(timespec="seconds")


def _short(text, limit: int) -> str:
    if not isinstance(text, str):
        return "—"
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


def _fmt_json(value) -> str:
    """把 JSON 标量格式化成人类可读文本（true/false/null）。"""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    return str(value)


def _snippet(res: "HttpResult", limit: int = 240) -> str:
    body = res.body or b""
    if not body:
        return ""
    text = body.decode("utf-8", errors="replace")
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[:limit] + "…"
    return text


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _ident(entry) -> tuple:
    """结果/基准条目的排序身份：parentKey（文献）+ itemKey（片段）。"""
    if not isinstance(entry, dict):
        return ("", "")
    parent = entry.get("parentKey")
    item = entry.get("itemKey")
    return (
        parent if isinstance(parent, str) else "",
        item if isinstance(item, str) else "",
    )


def _ident_text(entry) -> str:
    parent, item = _ident(entry)
    if parent and item:
        return f"{parent}/{item}"
    return item or parent or "—"


def _multiset_hits(current_entries, golden_entries) -> int:
    """top-k 命中数（多重集口径）。

    注意：同一文献的不同片段可能同时进入 top-5（parentKey/itemKey 相同），
    用集合求交会把重复条目少算（实测 q1/q3/q5/q6 冻结后立刻复跑只得 4/5），
    因此这里按出现次数计数。
    """
    current_ids = [_ident(item) for item in current_entries]
    golden_ids = [_ident(item) for item in golden_entries]
    return sum((Counter(current_ids) & Counter(golden_ids)).values())


def _multiset_missing(golden_entries, current_entries) -> list:
    """基准中有、当前结果中没有（按出现次数一一抵消）。"""
    available = Counter(_ident(item) for item in current_entries)
    missing = []
    for item in golden_entries:
        ident = _ident(item)
        if available[ident] > 0:
            available[ident] -= 1
        else:
            missing.append(item)
    return missing


def _multiset_added(current_entries, golden_entries) -> list:
    """当前结果中有、基准中没有（按出现次数一一抵消）。"""
    available = Counter(_ident(item) for item in golden_entries)
    added = []
    for item in current_entries:
        ident = _ident(item)
        if available[ident] > 0:
            available[ident] -= 1
        else:
            added.append(item)
    return added


# ---------------- HTTP（绕过代理 + 独立超时 + 总预算） ----------------


@dataclass
class HttpResult:
    url: str
    status: int | None = None
    elapsed_ms: float = 0.0
    obj: object = None
    body: bytes = b""
    error_kind: str | None = None  # timeout/connect/protocol/http/json/budget/other
    error_message: str = ""
    timeout_effective: float = 0.0

    def ok(self) -> bool:
        return self.error_kind is None and self.status == 200 and self.obj is not None


class HttpClient:
    """带总预算的同步 HTTP 客户端：任何请求都有超时，绝不无限等待。"""

    def __init__(self, base_url: str, total_budget: float):
        self.base_url = base_url.rstrip("/")
        self.total_budget = total_budget
        self._clock = time.perf_counter
        self.deadline = self._clock() + total_budget
        # 关键：显式禁用代理。本机系统代理 127.0.0.1:10801 会拦截 127.0.0.1 请求。
        # ProxyHandler({}) 等价 curl --noproxy '*'。
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def remaining(self) -> float:
        return self.deadline - self._clock()

    def get_json(self, path: str, params: dict | None = None,
                 timeout: float = 10.0, label: str = "") -> HttpResult:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        res = HttpResult(url=url)

        remaining = self.remaining()
        if remaining <= 0.5:
            res.error_kind = "budget"
            res.error_message = (
                f"总预算 {self.total_budget:g}s 已耗尽（剩余 {max(0.0, remaining):.1f}s），"
                f"请求 {label or path} 未执行"
            )
            return res

        # 单请求超时既要服从自身上限，也不能突破总预算
        res.timeout_effective = min(timeout, max(0.5, remaining - 0.1))
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json",
                "User-Agent": "zotero-retrieval-smoke/1.0",
            },
        )

        t0 = self._clock()
        try:
            with self.opener.open(request, timeout=res.timeout_effective) as resp:
                res.status = resp.status
                res.body = resp.read()
        except urllib.error.HTTPError as exc:
            res.status = exc.code
            res.error_kind = "http"
            try:
                res.body = exc.read() or b""
            except Exception:
                res.body = b""
        except (socket.timeout, TimeoutError):
            res.error_kind = "timeout"
            res.error_message = "socket 读写超时"
        except urllib.error.URLError as exc:
            reason = getattr(exc, "reason", exc)
            if isinstance(reason, (socket.timeout, TimeoutError)):
                res.error_kind = "timeout"
                res.error_message = "socket 读写超时（URLError 包装）"
            elif isinstance(reason, ConnectionRefusedError):
                res.error_kind = "connect"
                res.error_message = "ConnectionRefusedError: 目标端口无监听（连接被拒绝）"
            elif isinstance(reason, socket.gaierror):
                res.error_kind = "connect"
                res.error_message = f"域名解析失败：{reason}"
            elif isinstance(reason, ConnectionResetError):
                res.error_kind = "protocol"
                res.error_message = f"连接被重置：{reason}"
            else:
                res.error_kind = "connect"
                res.error_message = f"URLError：{reason}"
        except Exception as exc:  # 兜底：任何异常都转成明确错误，不挂死
            res.error_kind = "other"
            res.error_message = f"{type(exc).__name__}: {exc}"
        res.elapsed_ms = (self._clock() - t0) * 1000.0

        if res.error_kind in (None, "http"):
            self._parse_json(res)
        return res

    @staticmethod
    def _parse_json(res: HttpResult) -> None:
        if not res.body:
            if res.error_kind is None:
                res.error_kind = "json"
                res.error_message = "响应体为空"
            return
        try:
            text = res.body.decode("utf-8")
        except UnicodeDecodeError:
            text = res.body.decode("utf-8", errors="replace")
        try:
            res.obj = json.loads(text)
        except Exception as exc:
            if res.error_kind is None:
                res.error_kind = "json"
                res.error_message = f"JSON 解析失败：{exc}"


def _transport_message(res: HttpResult, timeout: float) -> str:
    kind = res.error_kind
    if kind == "http":
        return f"HTTP {res.status}（期望 200）"
    if kind == "timeout":
        return f"请求超时：{res.timeout_effective:.1f}s 内未响应（配置上限 {timeout:g}s）"
    if kind == "connect":
        return "连接失败（服务未启动/端口错误）"
    if kind == "protocol":
        return "HTTP 响应异常（连接被重置）"
    if kind == "json":
        return "响应不是合法 JSON"
    if kind == "budget":
        return "总预算耗尽，请求未执行"
    return "请求异常"


def _transport_details(res: HttpResult, timeout: float) -> list:
    details = [f"请求地址：{res.url}"]
    if res.error_kind == "timeout":
        details.append(
            f"未在 {res.timeout_effective:.1f}s 内响应（配置上限 {timeout:g}s；"
            "脚本有总预算兜底，不会挂死）"
        )
        details.append(
            "历史坑：Zotero 服务端按 init.length 分派端点，写错会导致请求永久挂起；"
            "本次超时即视为回归。"
        )
    elif res.error_kind == "connect":
        details.append(f"底层错误：{res.error_message}")
        details.append(
            "排查：Zotero 是否运行；插件 retrieval-index 是否启用；"
            "--base-url 端口是否正确（默认 23119）。"
        )
    elif res.error_kind == "protocol":
        details.append(f"底层错误：{res.error_message}")
        details.append("服务端异常关闭连接（分派错误/进程退出），属回归信号。")
    elif res.error_kind == "json":
        details.append(f"底层错误：{res.error_message}")
        snippet = _snippet(res)
        if snippet:
            details.append(f"响应片段：{snippet}")
    elif res.error_kind == "http":
        snippet = _snippet(res)
        if snippet:
            details.append(f"响应片段：{snippet}")
    elif res.error_kind == "budget":
        details.append(res.error_message)
        details.append("可用 --total-budget 调大预算后重试。")
    else:
        if res.error_message:
            details.append(f"底层错误：{res.error_message}")
    return details


# ---------------- 结果模型 ----------------


@dataclass
class Check:
    group: str
    cid: str
    name: str
    status: str = "PASS"
    elapsed_ms: float = 0.0
    message: str = ""
    details: list = field(default_factory=list)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "group": self.group,
            "id": self.cid,
            "name": self.name,
            "status": self.status,
            "elapsed_ms": round(self.elapsed_ms, 1),
            "message": self.message,
            "details": list(self.details),
            "data": self.data,
        }


@dataclass
class Report:
    args: argparse.Namespace
    checks: list
    golden: dict
    plugin_info: dict
    stats_snapshot: dict
    started_at: str
    finished_at: str
    duration_ms: float
    exit_code: int

    def summary(self) -> dict:
        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        return counts

    def to_dict(self) -> dict:
        args = self.args
        counts = self.summary()
        return {
            "schema": 1,
            "tool": "zotero-retrieval/tools/smoke_test.py",
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "duration_ms": round(self.duration_ms, 1),
            "base_url": args.base_url,
            "k": args.k,
            "mode": SEARCH_MODE,
            "freeze": bool(args.freeze),
            "skip_queries": bool(args.skip_queries),
            "budget_seconds": args.total_budget,
            "gates": {
                "min_chunks": args.min_chunks,
                "min_paged_results": args.min_paged,
                "max_search_ms": args.max_search_ms,
                "min_golden_hits": args.min_golden_hits,
                "expected_dim": EXPECTED_DIM,
            },
            "plugin": self.plugin_info or None,
            "stats": self.stats_snapshot or None,
            "summary": counts,
            "exit_code": self.exit_code,
            "golden": self.golden,
            "checks": [check.to_dict() for check in self.checks],
        }


# ---------------- 单条查询结构校验 ----------------


def validate_search_payload(payload: dict, k: int):
    """校验 /retrieval/search 响应结构，返回 (problems, warns, results, evidence)。"""
    problems: list = []
    warns: list = []

    results = payload.get("results")
    evidence = payload.get("evidence")
    if not isinstance(results, list):
        problems.append("results 缺失或不是数组")
    if not isinstance(evidence, dict):
        problems.append("evidence 缺失或不是对象")
    if problems:
        return problems, warns, [], {}

    missing = [key for key in EVIDENCE_KEYS if key not in evidence]
    if missing:
        problems.append("evidence 缺少字段：" + "、".join(missing))
    for key in EVIDENCE_INT_KEYS:
        if key in evidence and not _is_int(evidence[key]):
            problems.append(
                f"evidence.{key} 不是整数（实际 {type(evidence[key]).__name__}）"
            )
    if "orphansIncluded" in evidence and not isinstance(evidence["orphansIncluded"], bool):
        problems.append("evidence.orphansIncluded 不是布尔值")
    if "hints" in evidence and not isinstance(evidence["hints"], list):
        problems.append("evidence.hints 不是数组")
    if "topSegmentRole" in evidence and not (
        evidence["topSegmentRole"] is None
        or isinstance(evidence["topSegmentRole"], str)
    ):
        problems.append("evidence.topSegmentRole 不是字符串或 null")

    if not results:
        problems.append("results 为空（检索无结果）")
    if len(results) > k:
        warns.append(f"结果数 {len(results)} 超过 k={k}")

    if (
        "returned" in evidence
        and _is_int(evidence["returned"])
        and evidence["returned"] != len(results)
    ):
        problems.append(
            f"evidence.returned={evidence['returned']} 与 results 长度 {len(results)} 不一致"
        )

    unknown_sources = 0
    for idx, item in enumerate(results):
        prefix = f"results[{idx}]"
        if not isinstance(item, dict):
            problems.append(f"{prefix} 不是对象")
            continue
        for key in ("itemKey", "title", "text"):
            if not _nonempty_str(item.get(key)):
                problems.append(f"{prefix}.{key} 缺失/为空")
        if not isinstance(item.get("parentKey"), str):
            warns.append(f"{prefix}.parentKey 不是字符串（漂移对比将退回 itemKey）")
        source = item.get("pageSource")
        if not isinstance(source, str) or not source:
            problems.append(f"{prefix}.pageSource 缺失/为空")
        elif source not in KNOWN_PAGE_SOURCES:
            problems.append(
                f"{prefix}.pageSource={source!r} 不在已知集合 {sorted(KNOWN_PAGE_SOURCES)}"
            )
        elif source == LEGACY_PAGE_SOURCE:
            unknown_sources += 1
        page = item.get("page")
        if page is not None and (not _is_int(page) or page <= 0):
            problems.append(f"{prefix}.page 非法：{page!r}（应为 null 或正整数）")

    if unknown_sources:
        warns.append(
            f"{unknown_sources} 条结果 pageSource=unknown（索引元数据缺失/旧索引），"
            "页码统计可能被低估"
        )
    return problems, warns, results, evidence


# ---------------- 主流程 ----------------


class SmokeRunner:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self._clock = time.perf_counter
        self.start = self._clock()
        self.started_at = _iso_now()
        self.client = HttpClient(args.base_url, args.total_budget)
        self.checks: list = []
        self.golden_path = args.golden_file or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), GOLDEN_NAME
        )
        self.plugin_info: dict = {}
        self.stats_snapshot: dict = {}
        self.query_payloads: dict = {}
        self.golden = {
            "file": self.golden_path,
            "exists": os.path.exists(self.golden_path),
            "mode": "skipped",
            "frozen_at": None,
            "schema": None,
            "plugin_version": None,
            "k": None,
            "queries": 0,
            "comparisons": [],
            "written": None,
        }
        self.budget_exhausted = False
        self.api_available = False
        self.abort_reason = ""

    # ---- 通用 ----

    def _add(self, group: str, cid: str, name: str, status: str, message: str,
             elapsed_ms: float = 0.0, details: list | None = None,
             data: dict | None = None) -> Check:
        check = Check(
            group=group,
            cid=cid,
            name=name,
            status=status,
            elapsed_ms=elapsed_ms,
            message=message,
            details=list(details or []),
            data=dict(data or {}),
        )
        self.checks.append(check)
        return check

    def _skip(self, group: str, cid: str, name: str, reason: str) -> Check:
        return self._add(group, cid, name, "SKIP", reason)

    # ---- 检查 1：ping ----

    def check_ping(self) -> None:
        res = self.client.get_json(
            "/retrieval/ping", timeout=self.args.timeout_ping, label="ping"
        )
        if res.error_kind:
            if res.error_kind == "budget":
                self.budget_exhausted = True
            self.abort_reason = _transport_message(res, self.args.timeout_ping)
            self._add(
                "health", "ping", "ping 接口", "FAIL",
                self.abort_reason,
                elapsed_ms=res.elapsed_ms,
                details=_transport_details(res, self.args.timeout_ping),
                data={
                    "url": res.url,
                    "http_status": res.status,
                    "error_kind": res.error_kind,
                    "error": res.error_message,
                },
            )
            return

        if res.status != 200:
            self.abort_reason = f"ping 返回 HTTP {res.status}"
            self._add(
                "health", "ping", "ping 接口", "FAIL",
                f"HTTP {res.status}（期望 200）",
                elapsed_ms=res.elapsed_ms,
                details=_transport_details(res, self.args.timeout_ping),
                data={"url": res.url, "http_status": res.status},
            )
            return

        if not isinstance(res.obj, dict):
            self.abort_reason = "ping 响应不是 JSON 对象"
            self._add(
                "health", "ping", "ping 接口", "FAIL",
                "响应结构异常（不是 JSON 对象）",
                elapsed_ms=res.elapsed_ms,
                details=[f"响应片段：{_snippet(res)}"] if _snippet(res) else [],
                data={"url": res.url},
            )
            return

        # 只要拿到有效 JSON 对象，就认为服务端接口栈可用，继续后续检查
        self.api_available = True
        data = res.obj
        version = data.get("version")
        plugin = data.get("plugin")
        startup_error_present = "startupError" in data
        startup_error = data.get("startupError")
        self.plugin_info = {
            "plugin": plugin,
            "version": version,
            "dataDir": data.get("dataDir"),
            "hasDataDir": data.get("hasDataDir"),
            "startupError": startup_error,
            "ts": data.get("ts"),
        }

        problems: list = []
        warns: list = []
        if data.get("ok") is not True:
            problems.append(f"ok={_fmt_json(data.get('ok'))}（期望 true）")
        if not startup_error_present:
            problems.append("startupError 字段缺失（期望显式 null）")
        elif startup_error is not None:
            problems.append(f"startupError={startup_error!r}（期望 null）")
        if not _nonempty_str(version):
            problems.append(f"version 缺失或非字符串：{version!r}")
        if plugin != PLUGIN_NAME:
            warns.append(f"plugin={plugin!r}（期望 {PLUGIN_NAME}）")

        message = (
            f"HTTP 200 | ok={_fmt_json(data.get('ok'))} | "
            f"startupError={_fmt_json(startup_error if startup_error_present else '<缺失>')} | "
            f"plugin={_fmt_json(plugin)} | version={_fmt_json(version)}"
        )
        if problems:
            self._add(
                "health", "ping", "ping 接口", "FAIL", message,
                elapsed_ms=res.elapsed_ms,
                details=[f"✗ {item}" for item in problems]
                + [f"! {item}" for item in warns],
                data={"url": res.url, "http_status": res.status,
                      "response": self.plugin_info},
            )
        else:
            self._add(
                "health", "ping", "ping 接口", "WARN" if warns else "PASS",
                message,
                elapsed_ms=res.elapsed_ms,
                details=[f"! {item}" for item in warns],
                data={"url": res.url, "http_status": res.status,
                      "response": self.plugin_info},
            )

    # ---- 检查 2：stats ----

    def check_stats(self) -> None:
        res = self.client.get_json(
            "/retrieval/stats", timeout=self.args.timeout_ping, label="stats"
        )
        if res.error_kind:
            if res.error_kind == "budget":
                self.budget_exhausted = True
            self._add(
                "health", "stats", "索引统计", "FAIL",
                _transport_message(res, self.args.timeout_ping),
                elapsed_ms=res.elapsed_ms,
                details=_transport_details(res, self.args.timeout_ping),
                data={
                    "url": res.url,
                    "http_status": res.status,
                    "error_kind": res.error_kind,
                    "error": res.error_message,
                },
            )
            return

        if res.status != 200 or not isinstance(res.obj, dict):
            details = _transport_details(res, self.args.timeout_ping)
            if res.status == 200:
                details = [f"响应片段：{_snippet(res)}"] if _snippet(res) else []
            self._add(
                "health", "stats", "索引统计", "FAIL",
                f"HTTP {res.status} | 响应不是有效 JSON 对象",
                elapsed_ms=res.elapsed_ms,
                details=details,
                data={"url": res.url, "http_status": res.status},
            )
            return

        data = res.obj
        build = data.get("build") if isinstance(data.get("build"), dict) else {}
        meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
        startup_error = data.get("startupError")

        problems: list = []
        warns: list = []

        if data.get("loaded") is not True:
            problems.append(f"loaded={_fmt_json(data.get('loaded'))}（期望 true，索引未加载）")
        if data.get("hasVectors") is not True:
            problems.append(f"hasVectors={_fmt_json(data.get('hasVectors'))}（期望 true）")
        dim = data.get("dim")
        if not _is_int(dim) or dim != EXPECTED_DIM:
            problems.append(f"dim={_fmt_json(dim)}（期望 {EXPECTED_DIM}）")
        if not _is_int(data.get("chunks")):
            problems.append(f"chunks 不是整数：{data.get('chunks')!r}")
        elif data["chunks"] < self.args.min_chunks:
            problems.append(
                f"chunks={data['chunks']} 低于阈值 {self.args.min_chunks}"
            )
        if not isinstance(data.get("build"), dict):
            problems.append("build 字段缺失或不是对象")
        elif build.get("running") is not False:
            problems.append(
                f"build.running={_fmt_json(build.get('running'))}（期望 false，索引正在重建）"
            )
        if startup_error is not None:
            problems.append(f"startupError={startup_error!r}（期望 null）")
        if build.get("error") not in (None, False, ""):
            warns.append(f"build.error={build.get('error')!r}（上次建索引有错误）")
        if meta.get("ollamaOnline") is False:
            warns.append("meta.ollamaOnline=false（Ollama 不在线，hybrid 检索可能退化/失败）")

        self.stats_snapshot = {
            "loaded": data.get("loaded"),
            "chunks": data.get("chunks"),
            "papers": data.get("papers"),
            "retractedPapers": data.get("retractedPapers"),
            "hasVectors": data.get("hasVectors"),
            "dim": data.get("dim"),
            "startupError": startup_error,
            "model": meta.get("model"),
            "builtAt": meta.get("builtAt"),
            "ollamaOnline": meta.get("ollamaOnline"),
            "build": {
                "running": build.get("running"),
                "phase": build.get("phase"),
                "processed": build.get("processed"),
                "total": build.get("total"),
                "failed": build.get("failed"),
                "error": build.get("error"),
            },
        }

        message = (
            f"HTTP 200 | loaded={_fmt_json(data.get('loaded'))} | "
            f"hasVectors={_fmt_json(data.get('hasVectors'))} | "
            f"dim={_fmt_json(dim)} | "
            f"build.running={_fmt_json(build.get('running'))} | "
            f"chunks={_fmt_json(data.get('chunks'))} ≥ {self.args.min_chunks} | "
            f"papers={_fmt_json(data.get('papers'))}"
        )
        status = "FAIL" if problems else ("WARN" if warns else "PASS")
        self._add(
            "health", "stats", "索引统计", status, message,
            elapsed_ms=res.elapsed_ms,
            details=[f"✗ {item}" for item in problems]
            + [f"! {item}" for item in warns],
            data={"url": res.url, "http_status": res.status,
                  "snapshot": self.stats_snapshot},
        )

    # ---- 检查 3：6 条固定查询 ----

    def check_queries(self) -> None:
        params_base = {"k": self.args.k, "mode": SEARCH_MODE}
        for qid, question in QUERIES:
            if self.budget_exhausted:
                self._skip(
                    "query", qid, f"{qid} 检索",
                    f"总预算 {self.args.total_budget:g}s 已耗尽，未执行",
                )
                self.query_payloads[qid] = {"ok": False, "skipped": True}
                continue

            res = self.client.get_json(
                "/retrieval/search",
                params={"q": question, **params_base},
                timeout=self.args.timeout_search,
                label=f"search {qid}",
            )
            if res.error_kind:
                if res.error_kind == "budget":
                    self.budget_exhausted = True
                message = _transport_message(res, self.args.timeout_search)
                self._add(
                    "query", qid, f"{qid} 检索", "FAIL", message,
                    elapsed_ms=res.elapsed_ms,
                    details=_transport_details(res, self.args.timeout_search),
                    data={
                        "qid": qid,
                        "q": question,
                        "url": res.url,
                        "http_status": res.status,
                        "error_kind": res.error_kind,
                        "error": res.error_message,
                    },
                )
                self.query_payloads[qid] = {"ok": False, "error": message}
                continue

            if not isinstance(res.obj, dict):
                self._add(
                    "query", qid, f"{qid} 检索", "FAIL",
                    f"HTTP {res.status} | 响应不是 JSON 对象",
                    elapsed_ms=res.elapsed_ms,
                    details=[f"响应片段：{_snippet(res)}"] if _snippet(res) else [],
                    data={"qid": qid, "q": question, "url": res.url,
                          "http_status": res.status},
                )
                self.query_payloads[qid] = {"ok": False, "error": "响应不是 JSON 对象"}
                continue

            payload = res.obj
            problems, warns, results, evidence = validate_search_payload(
                payload, self.args.k
            )
            structure_ok = not problems
            paged = sum(1 for item in results if item.get("page") is not None)
            if res.elapsed_ms > self.args.max_search_ms:
                problems.append(
                    f"耗时 {res.elapsed_ms:.0f}ms 超过阈值 "
                    f"{self.args.max_search_ms:.0f}ms"
                )

            summary = f"HTTP 200 | 返回 {len(results)} 条 | 有页码 {paged}"
            if results and isinstance(results[0], dict):
                summary += (
                    f" | top1: {_short(results[0].get('title'), 34)} "
                    f"[{results[0].get('itemKey')}]"
                )
            status = "FAIL" if problems else ("WARN" if warns else "PASS")
            data = {
                "qid": qid,
                "q": question,
                "http_status": res.status,
                "paged": paged,
                "elapsed_ms": round(res.elapsed_ms, 1),
                "results": [
                    {
                        "itemKey": item.get("itemKey"),
                        "parentKey": item.get("parentKey"),
                        "title": item.get("title"),
                        "page": item.get("page"),
                        "pageSource": item.get("pageSource"),
                    }
                    for item in results
                    if isinstance(item, dict)
                ],
                "evidence": evidence,
            }
            self._add(
                "query", qid, f"{qid} 检索", status, summary,
                elapsed_ms=res.elapsed_ms,
                details=[f"✗ {item}" for item in problems]
                + [f"! {item}" for item in warns],
                data=data,
            )
            self.query_payloads[qid] = {
                "ok": structure_ok,
                "results": results,
                "evidence": evidence,
                "paged": paged,
                "elapsed_ms": res.elapsed_ms,
                "problems": problems,
                "structure_ok": structure_ok,
                "warns": warns,
            }

    # ---- 检查 4：6 条合计页码 ----

    def check_aggregate(self) -> None:
        valid = [
            payload for payload in self.query_payloads.values() if payload.get("ok")
        ]
        ok_count = len(valid)
        if ok_count != len(QUERIES) or not valid:
            self._skip(
                "aggregate", "aggregate", "页码聚合",
                f"查询未全部成功（成功 {ok_count}/{len(QUERIES)}），聚合断言不可信",
            )
            return

        all_results = [item for payload in valid for item in payload["results"]]
        total = len(all_results)
        paged = sum(1 for item in all_results if item.get("page") is not None)
        unknown_sources = sum(
            1 for item in all_results
            if item.get("pageSource") == LEGACY_PAGE_SOURCE
        )
        evidence_page_sum = sum(
            int(payload["evidence"].get("segmentsWithPage") or 0)
            for payload in valid
            if isinstance(payload.get("evidence"), dict)
        )

        problems: list = []
        warns: list = []
        if paged < self.args.min_paged:
            problems.append(
                f"有真实页码的结果仅 {paged}/{total} 条，低于阈值 {self.args.min_paged}"
            )
        if unknown_sources:
            warns.append(
                f"{unknown_sources} 条结果 pageSource=unknown（页码元数据缺失）"
            )
        if evidence_page_sum != paged:
            warns.append(
                f"evidence.segmentsWithPage 合计 {evidence_page_sum} 与 "
                f"实际 page!=null 的 {paged} 条不一致"
            )

        message = (
            f"{ok_count} 条查询共 {total} 条结果，{paged} 条有真实页码"
            f"（阈值 ≥{self.args.min_paged}）"
        )
        status = "FAIL" if problems else ("WARN" if warns else "PASS")
        self._add(
            "aggregate", "aggregate", "页码聚合", status, message,
            details=[f"✗ {item}" for item in problems]
            + [f"! {item}" for item in warns],
            data={
                "queries": ok_count,
                "total_results": total,
                "paged_results": paged,
                "min_paged": self.args.min_paged,
                "unknown_page_sources": unknown_sources,
            },
        )

    # ---- 检查 5a：冻结黄金基准 ----

    def freeze_golden(self) -> None:
        failed = [check for check in self.checks if check.status == "FAIL"]
        missing = [
            qid for qid, _ in QUERIES
            if not (self.query_payloads.get(qid) or {}).get("ok")
        ]
        if failed or missing:
            details = [f"失败项：{check.cid}（{check.message}）" for check in failed]
            details += [f"缺少有效查询结果：{qid}" for qid in missing]
            self._add(
                "golden", "freeze", "冻结黄金基准", "FAIL",
                "存在 FAIL/无效查询，拒绝写入黄金基准（避免把坏状态冻成基准）",
                details=details,
                data={"failed_checks": [check.cid for check in failed],
                      "missing_queries": missing},
            )
            return

        content = self._build_golden_content()
        try:
            os.makedirs(os.path.dirname(self.golden_path), exist_ok=True)
            tmp_path = self.golden_path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(content, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(tmp_path, self.golden_path)
        except Exception as exc:
            self._add(
                "golden", "freeze", "冻结黄金基准", "FAIL",
                f"写入失败：{type(exc).__name__}: {exc}",
                data={"file": self.golden_path},
            )
            return

        self.golden.update({
            "exists": True,
            "mode": "freeze",
            "frozen_at": content["frozen_at"],
            "schema": GOLDEN_SCHEMA,
            "plugin_version": content.get("plugin_version"),
            "k": content.get("k"),
            "queries": len(content["queries"]),
            "written": self.golden_path,
        })
        self._add(
            "golden", "freeze", "冻结黄金基准", "PASS",
            f"已写入 {self.golden_path}（{len(content['queries'])} 条查询 × top-{self.args.k}，"
            f"冻结于 {content['frozen_at']}）",
            data={"file": self.golden_path, "queries": len(content["queries"]),
                  "k": self.args.k, "plugin_version": content.get("plugin_version")},
        )

    def _build_golden_content(self) -> dict:
        query_entries = []
        for qid, question in QUERIES:
            payload = self.query_payloads[qid]
            entries = [
                {
                    "itemKey": item.get("itemKey"),
                    "parentKey": item.get("parentKey"),
                    "title": item.get("title"),
                    "page": item.get("page"),
                    "pageSource": item.get("pageSource"),
                }
                for item in payload["results"]
                if isinstance(item, dict)
            ]
            query_entries.append({
                "qid": qid,
                "q": question,
                "top1": entries[0] if entries else None,
                "top5": entries,
            })
        stats = self.stats_snapshot or {}
        return {
            "schema": GOLDEN_SCHEMA,
            "generated_by": "tools/smoke_test.py --freeze",
            "frozen_at": _iso_now(),
            "base_url": self.args.base_url,
            "plugin": self.plugin_info.get("plugin"),
            "plugin_version": self.plugin_info.get("version"),
            "k": self.args.k,
            "mode": SEARCH_MODE,
            "stats": {
                "chunks": stats.get("chunks"),
                "papers": stats.get("papers"),
                "dim": stats.get("dim"),
                "model": stats.get("model"),
            },
            "queries": query_entries,
        }

    # ---- 检查 5b：排序漂移对比 ----

    def compare_golden(self) -> None:
        t0 = self._clock()
        path = self.golden_path
        if not os.path.exists(path):
            self.golden.update({"exists": False, "mode": "compare"})
            self._add(
                "golden", "golden-file", "黄金基准文件", "WARN",
                f"未找到黄金基准：{path}",
                elapsed_ms=(self._clock() - t0) * 1000.0,
                details=[
                    "先运行：python tools/smoke_test.py --freeze "
                    "生成基准（记录 6 条查询的 top-5 与 top1）"
                ],
                data={"file": path, "exists": False},
            )
            return

        try:
            with open(path, "r", encoding="utf-8") as handle:
                golden = json.load(handle)
            if not isinstance(golden, dict):
                raise ValueError("顶层不是 JSON 对象")
            queries = golden.get("queries")
            if not isinstance(queries, list):
                raise ValueError("queries 不是数组")
        except Exception as exc:
            self.golden.update({"exists": True, "mode": "compare"})
            self._add(
                "golden", "golden-file", "黄金基准文件", "WARN",
                f"黄金基准无法解析：{type(exc).__name__}: {exc}",
                elapsed_ms=(self._clock() - t0) * 1000.0,
                details=["重新生成：python tools/smoke_test.py --freeze"],
                data={"file": path},
            )
            return

        schema = golden.get("schema")
        self.golden.update({
            "exists": True,
            "mode": "compare",
            "frozen_at": golden.get("frozen_at"),
            "schema": schema,
            "plugin_version": golden.get("plugin_version"),
            "k": golden.get("k"),
            "queries": len(queries),
        })
        if schema != GOLDEN_SCHEMA:
            self._add(
                "golden", "golden-file", "黄金基准文件", "WARN",
                f"黄金基准 schema={schema!r} 与脚本 schema={GOLDEN_SCHEMA} 不匹配，跳过对比",
                elapsed_ms=(self._clock() - t0) * 1000.0,
                details=["重新生成：python tools/smoke_test.py --freeze"],
                data={"file": path, "schema": schema},
            )
            return

        golden_by_qid = {}
        for entry in queries:
            if isinstance(entry, dict) and isinstance(entry.get("qid"), str):
                golden_by_qid[entry["qid"]] = entry

        current_version = self.plugin_info.get("version")
        golden_version = golden.get("plugin_version")
        golden_k = golden.get("k")

        for qid, question in QUERIES:
            check = Check("golden", qid, f"{qid} 排序漂移")
            t1 = self._clock()
            payload = self.query_payloads.get(qid) or {}
            if not payload.get("ok"):
                check.status = "SKIP"
                check.message = "查询失败/未执行，无法做漂移对比"
                check.elapsed_ms = (self._clock() - t1) * 1000.0
                self.checks.append(check)
                self.golden["comparisons"].append({
                    "qid": qid, "status": "SKIP", "reason": check.message,
                })
                continue

            entry = golden_by_qid.get(qid)
            if entry is None:
                check.status = "WARN"
                check.message = "黄金基准中缺少该查询（基准过期？）"
                check.details = ["重新生成：python tools/smoke_test.py --freeze"]
                check.elapsed_ms = (self._clock() - t1) * 1000.0
                self.checks.append(check)
                self.golden["comparisons"].append({
                    "qid": qid, "status": "WARN", "reason": "missing in golden",
                })
                continue

            golden_top = entry.get("top5")
            if not isinstance(golden_top, list):
                golden_top = []
            golden_top = [item for item in golden_top if isinstance(item, dict)]
            current_results = payload["results"]

            hits = _multiset_hits(current_results, golden_top)
            total = len(golden_top)
            threshold = min(self.args.min_golden_hits, total) if total else 0

            golden_top1 = entry.get("top1")
            if not isinstance(golden_top1, dict) and golden_top:
                golden_top1 = golden_top[0]
            if not isinstance(golden_top1, dict):
                golden_top1 = None
            current_top1 = current_results[0] if current_results else None

            top1_changed = (
                golden_top1 is not None
                and current_top1 is not None
                and _ident(golden_top1) != _ident(current_top1)
            )
            golden_question = entry.get("q")
            text_changed = isinstance(golden_question, str) and golden_question != question

            warns: list = []
            if text_changed:
                warns.append(
                    f"查询文本与基准不同：基准「{_short(golden_question, 32)}」→ "
                    f"当前「{_short(question, 32)}」，基准可能过期"
                )
            if top1_changed:
                warns.append(
                    f"top1 变化：旧「{_short(golden_top1.get('title'), 38)}」"
                    f"[{_ident_text(golden_top1)}] → "
                    f"新「{_short(current_top1.get('title'), 38)}」"
                    f"[{_ident_text(current_top1)}]"
                )
            if hits < threshold:
                warns.append(f"top-5 命中 {hits}/{total}，低于阈值 {threshold}")
                missing = _multiset_missing(golden_top, current_results)
                added = _multiset_added(current_results, golden_top)
                if missing:
                    warns.append(
                        "基准缺失：" + "；".join(
                            f"{_ident_text(item)}「{_short(item.get('title'), 26)}」"
                            for item in missing[:5]
                        )
                    )
                if added:
                    warns.append(
                        "当前新增：" + "；".join(
                            f"{_ident_text(item)}「{_short(item.get('title'), 26)}」"
                            for item in added[:5]
                        )
                    )
            if total == 0:
                warns.append("基准中该查询的 top-5 为空")

            check.status = "WARN" if warns else "PASS"
            check.message = (
                f"top-5 命中 {hits}/{total}（阈值 ≥{threshold}）｜"
                f"top1 {'已变化' if top1_changed else '未变'}"
            )
            check.details = [f"! {item}" for item in warns]
            if golden_k is not None and golden_k != self.args.k:
                check.details.append(
                    f"! 基准 k={golden_k}，当前 k={self.args.k}（命中统计仅供参考）"
                )
            if golden_version and current_version and golden_version != current_version:
                check.details.append(
                    f"! 基准插件版本 v{golden_version}，当前 v{current_version}"
                    "（漂移可能来自版本升级）"
                )
            check.elapsed_ms = (self._clock() - t1) * 1000.0
            check.data = {
                "qid": qid,
                "hits": hits,
                "total": total,
                "threshold": threshold,
                "top1_changed": top1_changed,
                "top1_old": golden_top1,
                "top1_new": current_top1,
                "missing": [
                    _ident_text(item)
                    for item in _multiset_missing(golden_top, current_results)
                ],
                "added": [
                    _ident_text(item)
                    for item in _multiset_added(current_results, golden_top)
                ],
            }
            self.checks.append(check)
            self.golden["comparisons"].append({
                "qid": qid,
                "status": check.status,
                "hits": hits,
                "total": total,
                "threshold": threshold,
                "top1_changed": top1_changed,
                "top1_old": _ident_text(golden_top1) if golden_top1 else None,
                "top1_new": _ident_text(current_top1) if current_top1 else None,
                "missing": check.data["missing"],
                "added": check.data["added"],
            })

    # ---- 检查 6：总预算 ----

    def check_budget(self) -> None:
        duration_s = self._clock() - self.start
        limit = self.args.total_budget
        if duration_s <= limit + 0.5:
            self._add(
                "budget", "budget", "总预算", "PASS",
                f"总耗时 {duration_s:.2f}s ≤ 预算 {limit:g}s",
                elapsed_ms=duration_s * 1000.0,
                data={"duration_s": round(duration_s, 2), "budget_s": limit},
            )
        else:
            self._add(
                "budget", "budget", "总预算", "FAIL",
                f"总耗时 {duration_s:.2f}s 超过预算 {limit:g}s",
                elapsed_ms=duration_s * 1000.0,
                data={"duration_s": round(duration_s, 2), "budget_s": limit},
            )

    # ---- 汇总 ----

    def run(self) -> Report:
        self.check_ping()

        if self.api_available and not self.budget_exhausted:
            self.check_stats()
        else:
            reason = self.abort_reason or (
                "总预算耗尽" if self.budget_exhausted else "服务不可用"
            )
            self._skip("health", "stats", "索引统计", f"{reason}，跳过")

        if self.args.skip_queries:
            self._skip(
                "query", "queries", "固定查询",
                "已指定 --skip-queries，跳过 6 条固定查询与排序漂移对比",
            )
            self._skip("aggregate", "aggregate", "页码聚合",
                       "--skip-queries，跳过")
            self._skip("golden", "golden", "排序漂移对比",
                       "--skip-queries，跳过")
        elif self.api_available and not self.budget_exhausted:
            self.check_queries()
            self.check_aggregate()
            if self.args.freeze:
                self.freeze_golden()
            else:
                self.compare_golden()
        else:
            reason = self.abort_reason or (
                "总预算耗尽" if self.budget_exhausted else "服务不可用"
            )
            self._skip("query", "queries", "固定查询",
                       f"{reason}，跳过 6 条固定查询")
            self._skip("aggregate", "aggregate", "页码聚合",
                       f"{reason}，跳过")
            self._skip("golden", "golden", "排序漂移对比",
                       f"{reason}，跳过")

        self.check_budget()

        counts = {"PASS": 0, "WARN": 0, "FAIL": 0, "SKIP": 0}
        for check in self.checks:
            counts[check.status] = counts.get(check.status, 0) + 1
        exit_code = 1 if counts["FAIL"] else 0
        duration_ms = (self._clock() - self.start) * 1000.0
        return Report(
            args=self.args,
            checks=self.checks,
            golden=self.golden,
            plugin_info=self.plugin_info,
            stats_snapshot=self.stats_snapshot,
            started_at=self.started_at,
            finished_at=_iso_now(),
            duration_ms=duration_ms,
            exit_code=exit_code,
        )


# ---------------- 输出 ----------------


def print_human(report: Report) -> None:
    args = report.args
    lines: list = []
    lines.append("Zotero 检索索引 冒烟回归测试（smoke_test）")
    lines.append(
        f"接口: {args.base_url}    k={args.k}    mode={SEARCH_MODE}    "
        f"预算={args.total_budget:g}s"
    )
    plugin_info = report.plugin_info or {}
    if plugin_info.get("version") is not None or plugin_info.get("dataDir"):
        lines.append(
            f"插件: {_fmt_json(plugin_info.get('plugin'))} "
            f"v{_fmt_json(plugin_info.get('version'))}    "
            f"数据目录: {_fmt_json(plugin_info.get('dataDir'))}"
        )
    lines.append("=" * 78)

    for group in GROUP_ORDER:
        checks = [check for check in report.checks if check.group == group]
        if not checks:
            continue
        header = SECTION_TITLES[group]
        if group == "query":
            header += f"（{len(QUERIES)} 条 × k={args.k}）"
        if group == "golden":
            golden = report.golden or {}
            if golden.get("mode") == "compare" and golden.get("exists"):
                header += (
                    f"：{os.path.basename(golden.get('file') or '')}"
                    f"（冻结于 {golden.get('frozen_at')}，"
                    f"插件 v{golden.get('plugin_version')}）"
                )
            elif golden.get("mode") == "freeze":
                header += "：生成/更新基准"
        lines.append(header)
        for check in checks:
            lines.append(
                f"  [{check.status:<4}] {check.cid:<12} "
                f"{check.elapsed_ms:>8.1f}ms  {check.message}"
            )
            if check.status in ("WARN", "FAIL", "SKIP"):
                for detail in check.details:
                    lines.append(f"           ↳ {detail}")
        lines.append("")

    counts = report.summary()
    lines.append("-" * 78)
    lines.append(
        f"汇总: PASS {counts['PASS']} / WARN {counts['WARN']} / "
        f"FAIL {counts['FAIL']} / SKIP {counts['SKIP']}    "
        f"总耗时 {report.duration_ms / 1000.0:.2f}s"
        f"（预算 {args.total_budget:g}s）"
    )
    if counts["FAIL"]:
        verdict = f"失败（{counts['FAIL']} 个 FAIL）"
    elif counts["WARN"]:
        verdict = (
            f"通过（{counts['WARN']} 个 WARN：索引正常演进/基准缺失，请人工确认上方 WARN）"
        )
    else:
        verdict = "通过（全部 PASS）"
    lines.append(f"结论: {verdict}")
    lines.append(f"退出码: {report.exit_code}")
    print("\n".join(lines))


def print_json(report: Report) -> None:
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


# ---------------- 参数 ----------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="smoke_test.py",
        description="Zotero 检索索引插件冒烟回归：ping / stats / 6 条固定查询 / 排序漂移",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python tools/smoke_test.py                 # 默认冒烟（对比黄金基准）\n"
            "  python tools/smoke_test.py --freeze        # 生成/更新黄金基准\n"
            "  python tools/smoke_test.py --json          # 机器可读输出\n"
            "  python tools/smoke_test.py --skip-queries  # 只查 ping/stats\n"
            "退出码：0=全 PASS/仅 WARN，1=存在 FAIL，2=参数错误"
        ),
    )
    parser.add_argument(
        "--base-url", default=DEFAULT_BASE_URL,
        help=f"Zotero 本地接口基址（默认 {DEFAULT_BASE_URL}）",
    )
    parser.add_argument(
        "--k", type=int, default=DEFAULT_K,
        help=f"每条查询返回条数（默认 {DEFAULT_K}）",
    )
    parser.add_argument(
        "--min-chunks", type=int, default=DEFAULT_MIN_CHUNKS,
        help=f"索引块数下限（默认 {DEFAULT_MIN_CHUNKS}）",
    )
    parser.add_argument(
        "--min-paged", type=int, default=DEFAULT_MIN_PAGED,
        help=f"6 条查询合计 page!=null 的结果数下限（默认 {DEFAULT_MIN_PAGED}）",
    )
    parser.add_argument(
        "--freeze", action="store_true",
        help="生成/更新黄金基准 smoke_expected.json（要求全部检查通过）",
    )
    parser.add_argument(
        "--json", action="store_true",
        help="输出机器可读 JSON（stdout 只有 JSON）",
    )
    parser.add_argument(
        "--skip-queries", action="store_true",
        help="只检查 ping/stats，跳过 6 条固定查询与漂移对比",
    )
    parser.add_argument(
        "--min-golden-hits", type=int, default=DEFAULT_MIN_GOLDEN_HITS,
        help=f"单条查询 top-5 命中数低于该值时 WARN（默认 {DEFAULT_MIN_GOLDEN_HITS}）",
    )
    parser.add_argument(
        "--max-search-ms", type=float, default=DEFAULT_MAX_SEARCH_MS,
        help=f"单条检索耗时 FAIL 阈值毫秒（默认 {DEFAULT_MAX_SEARCH_MS:.0f}）",
    )
    parser.add_argument(
        "--timeout-ping", type=float, default=DEFAULT_TIMEOUT_PING,
        help=f"ping/stats 单请求超时秒（默认 {DEFAULT_TIMEOUT_PING:g}）",
    )
    parser.add_argument(
        "--timeout-search", type=float, default=DEFAULT_TIMEOUT_SEARCH,
        help=f"search 单请求超时秒（默认 {DEFAULT_TIMEOUT_SEARCH:g}）",
    )
    parser.add_argument(
        "--total-budget", type=float, default=DEFAULT_TOTAL_BUDGET,
        help=f"全脚本总预算秒（默认 {DEFAULT_TOTAL_BUDGET:g}）",
    )
    parser.add_argument(
        "--golden-file", default=None,
        help=f"黄金基准路径（默认脚本同目录 {GOLDEN_NAME}）",
    )
    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.base_url = (args.base_url or "").strip().rstrip("/")
    if not (args.base_url.startswith("http://") or args.base_url.startswith("https://")):
        parser.error("--base-url 需以 http:// 或 https:// 开头")
    if args.k < 1:
        parser.error("--k 必须 ≥ 1")
    if args.min_chunks < 0:
        parser.error("--min-chunks 必须 ≥ 0")
    if args.min_paged < 0:
        parser.error("--min-paged 必须 ≥ 0")
    if args.min_golden_hits < 0:
        parser.error("--min-golden-hits 必须 ≥ 0")
    if args.timeout_ping <= 0 or args.timeout_search <= 0:
        parser.error("--timeout-ping / --timeout-search 必须 > 0")
    if args.total_budget <= 0:
        parser.error("--total-budget 必须 > 0")
    if args.freeze and args.skip_queries:
        parser.error("--freeze 与 --skip-queries 不能同时使用")
    if args.min_paged > len(QUERIES) * args.k:
        parser.error(
            f"--min-paged={args.min_paged} 超过最大可能结果数 "
            f"{len(QUERIES) * args.k}（{len(QUERIES)} 条查询 × k={args.k}）"
        )

    runner = SmokeRunner(args)
    report = runner.run()
    if args.json:
        print_json(report)
    else:
        print_human(report)
    return report.exit_code


if __name__ == "__main__":
    sys.exit(main())
