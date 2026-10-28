"""取数诊断只保留白名单阶段、错误分类和耗时，不保留第三方原文。"""
from __future__ import annotations

import json
import logging
import math
from pathlib import Path
import socket
import threading
import time
import urllib.error

from requests import exceptions as request_errors

STAGES = frozenset(("worker", "pe_hist", "pb_hist", "div", "fin", "statements", "price_hist",
                    "fzb", "lrb", "llb", "score", "export"))
STATES = frozenset(("start", "ok", "empty", "failed", "retry", "fresh_cache", "stale_cache"))
CATEGORIES = frozenset(("http", "timeout", "network", "json", "structure", "normalization",
                        "response_limit", "cache", "unknown"))
MAX_EVENTS = 64
MAX_BYTES = 24 * 1024


def error_category(exc):
    """只按异常类型分类；不解析可能含认证信息的第三方异常文本。"""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, urllib.error.HTTPError):
            return {"category": "http", "http_status": exc.code}
        if isinstance(exc, request_errors.HTTPError):
            status = getattr(getattr(exc, "response", None), "status_code", None)
            return {"category": "http", **({"http_status": status} if isinstance(status, int) else {})}
        if isinstance(exc, (TimeoutError, socket.timeout, request_errors.Timeout)):
            return {"category": "timeout"}
        if isinstance(exc, urllib.error.URLError):
            if isinstance(exc.reason, (TimeoutError, socket.timeout)):
                return {"category": "timeout"}
            return {"category": "network"}
        if isinstance(exc, (ConnectionError, request_errors.ConnectionError)):
            return {"category": "network"}
        if isinstance(exc, json.JSONDecodeError):
            return {"category": "json"}
        if isinstance(exc, (KeyError, TypeError, AttributeError)):
            return {"category": "structure"}
        exc = exc.__cause__ or exc.__context__
    return {"category": "unknown"}


def safe_event(event):
    """生产和读取侧共用同一白名单，拒绝附带的原始错误/URL/任意字符串。"""
    if (not isinstance(event, dict)
            or not isinstance(event.get("stage"), str) or event["stage"] not in STAGES
            or not isinstance(event.get("state"), str) or event["state"] not in STATES):
        return None
    result = {"stage": event["stage"], "state": event["state"]}
    for key in ("elapsed_ms", "offset_ms"):
        value = event.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 <= value <= 86400000 and math.isfinite(value):
            result[key] = round(value, 1)
    if isinstance(event.get("category"), str) and event["category"] in CATEGORIES:
        result["category"] = event["category"]
    status = event.get("http_status")
    if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
        result["http_status"] = status
    return result


def emit_diagnostic(sink, stage, state, started=None, **fields):
    if sink is None:
        return
    event = {"stage": stage, "state": state, **fields}
    if started is not None:
        event["elapsed_ms"] = (time.monotonic() - started) * 1000
    event = safe_event(event)
    if event is not None:
        # 诊断持久化故障不能改变评分或取数的返回值。
        try:
            sink(event)
        except OSError:
            logging.getLogger(__name__).warning("取数诊断侧车写入失败；继续原取数流程")


class DiagnosticRecorder:
    """线程安全、有界、原子侧车；父进程终止worker后仍可读取最后完成事件。"""
    def __init__(self, path):
        self.path = Path(path)
        self.started = time.monotonic()
        self.events = []
        self.lock = threading.Lock()

    def __call__(self, event):
        event = safe_event(event)
        if event is None:
            return
        with self.lock:
            event["offset_ms"] = round((time.monotonic() - self.started) * 1000, 1)
            self.events = (self.events + [event])[-MAX_EVENTS:]
            data = json.dumps({"events": self.events}, ensure_ascii=True).encode("utf-8")
            if len(data) > MAX_BYTES:
                return
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".tmp")
            temporary.write_bytes(data)
            temporary.replace(self.path)


def read_diagnostics(path):
    """批次只复制有界白名单摘要，不把worker文件中的额外字段向下游传播。"""
    try:
        with Path(path).open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return []
        events = json.loads(raw).get("events", [])
        if not isinstance(events, list):
            return []
        return [item for event in events[-MAX_EVENTS:] if (item := safe_event(event)) is not None]
    except (OSError, ValueError, AttributeError):
        return []
