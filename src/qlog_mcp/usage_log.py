"""Privacy-conscious structured logging for MCP tool usage."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from contextvars import ContextVar
from itertools import count
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp.types import CallToolRequestParams

_SQL_TRACES: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "qlog_mcp_sql_traces", default=None
)
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_STATION_SCOPES = {"all", "callsign", "profile"}


def record_sql(
    statement: str,
    parameters: list[Any],
    duration_ms: float,
    returned_rows: int | None,
    error: Exception | None = None,
) -> None:
    """Attach one generated SQL statement to the current MCP tool call."""
    traces = _SQL_TRACES.get()
    if traces is None:
        return

    trace: dict[str, Any] = {
        "statement": statement,
        "parameter_count": len(parameters),
        "parameter_types": [type(value).__name__ for value in parameters],
        "duration_ms": round(duration_ms, 2),
    }
    if returned_rows is not None:
        trace["returned_rows"] = returned_rows
    if error is not None:
        trace["outcome"] = "error"
        trace["error_type"] = type(error).__name__
    else:
        trace["outcome"] = "ok"
    traces.append(trace)


class UsageLoggingMiddleware(Middleware):
    """Write redacted tool-call metrics as one JSON object per line."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch(mode=0o600, exist_ok=True)

        handler = RotatingFileHandler(
            path,
            maxBytes=10 * 1024 * 1024,
            backupCount=5,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter("%(message)s"))

        self.session_id = uuid.uuid4().hex
        self.logger = logging.getLogger(f"qlog_mcp.usage.{self.session_id}")
        self.logger.setLevel(logging.INFO)
        self.logger.propagate = False
        self.logger.addHandler(handler)
        self.call_ids = count(1)

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        call_id = f"{next(self.call_ids):06d}"
        started_at = context.timestamp.isoformat().replace("+00:00", "Z")
        started = time.perf_counter()
        token = _SQL_TRACES.set([])
        result: ToolResult | None = None
        exception: Exception | None = None

        try:
            result = await call_next(context)
            return result
        except Exception as error:
            exception = error
            raise
        finally:
            sql_traces = list(_SQL_TRACES.get() or [])
            _SQL_TRACES.reset(token)
            duration_ms = round((time.perf_counter() - started) * 1000, 2)
            event = {
                "schema_version": 1,
                "event": "tool_call",
                "timestamp": started_at,
                "session_id": self.session_id,
                "call_id": call_id,
                "tool": context.message.name,
                "tool_duration_ms": duration_ms,
                "outcome": self._outcome(result, exception),
                "arguments": self._summarize_arguments(
                    context.message.name, context.message.arguments or {}
                ),
                "sql": sql_traces,
            }
            if exception is not None:
                event["error_type"] = self._error_type(exception)
            elif result is not None and result.is_error:
                event["error_type"] = "tool_error"

            if result is not None:
                event["result"] = self._summarize_result(result)
            event["flags"] = self._flags(event)
            self.logger.info(json.dumps(event, ensure_ascii=False, separators=(",", ":")))

    @staticmethod
    def _outcome(result: ToolResult | None, exception: Exception | None) -> str:
        if exception is not None or (result is not None and result.is_error):
            return "error"
        return "ok"

    @staticmethod
    def _error_type(error: Exception) -> str:
        while error.__cause__ is not None:
            error = error.__cause__
        return type(error).__name__

    @classmethod
    def _summarize_arguments(cls, tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if tool == "qso.query":
            return {
                "scope": cls._summarize_scope(arguments.get("scope")),
                "filters": cls._summarize_filters(arguments.get("filters")),
                "fields": cls._identifiers(arguments.get("fields")),
                "one_per_group": cls._summarize_one_per_group(
                    arguments.get("one_per_group")
                ),
                "sort": cls._summarize_sort(arguments.get("sort")),
                "limit": cls._integer(arguments.get("limit"), 100),
                "offset": cls._integer(arguments.get("offset"), 0),
            }
        if tool == "qso.aggregate":
            return {
                "scope": cls._summarize_scope(arguments.get("scope")),
                "filters": cls._summarize_filters(arguments.get("filters")),
                "group_by": cls._summarize_group_by(arguments.get("group_by")),
                "metrics": cls._summarize_metrics(arguments.get("metrics")),
                "one_per_group": cls._summarize_one_per_group(
                    arguments.get("one_per_group")
                ),
                "order_by": cls._summarize_sort(arguments.get("order_by")),
                "limit": cls._integer(arguments.get("limit"), 100),
            }
        if tool == "catalog.query":
            return {
                "catalog": cls._identifier(arguments.get("catalog")),
                "filters": cls._summarize_filters(arguments.get("filters")),
                "fields": cls._identifiers(arguments.get("fields")),
                "sort": cls._summarize_sort(arguments.get("sort")),
                "limit": cls._integer(arguments.get("limit"), 100),
                "offset": cls._integer(arguments.get("offset"), 0),
            }
        if tool == "catalog.match_qso":
            return {
                "catalog": cls._identifier(arguments.get("catalog")),
                "qso_field": cls._identifier(arguments.get("qso_field")),
                "scope": cls._summarize_scope(arguments.get("scope")),
                "qso_filters": cls._summarize_filters(arguments.get("qso_filters")),
                "catalog_filters": cls._summarize_filters(
                    arguments.get("catalog_filters")
                ),
                "relation": cls._identifier(arguments.get("relation", "matched")),
                "fields": cls._identifiers(arguments.get("fields")),
                "sort": cls._summarize_sort(arguments.get("sort")),
                "limit": cls._integer(arguments.get("limit"), 100),
                "offset": cls._integer(arguments.get("offset"), 0),
            }
        if tool == "qlog.get_schema":
            domain = arguments.get("domain", "qso")
            return {"domain": domain if domain in {"qso", "catalog"} else "<invalid>"}
        return {"argument_names": sorted(cls._identifier(name) for name in arguments)}

    @classmethod
    def _summarize_scope(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        station_scope = value.get("station_scope")
        return {
            "station_scope": station_scope if station_scope in _STATION_SCOPES else "<invalid>",
            "station_callsign_count": cls._length(value.get("station_callsigns")),
            "operator_callsign_count": cls._length(value.get("operator_callsigns")),
            "station_profile_count": cls._length(value.get("station_profile_names")),
            "has_date_from": value.get("date_from") is not None,
            "has_date_to": value.get("date_to") is not None,
        }

    @classmethod
    def _summarize_filters(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        conditions: list[dict[str, str]] = []
        group_count = 0

        def visit(group: Any) -> None:
            nonlocal group_count
            if not isinstance(group, dict):
                return
            group_count += 1
            for condition in group.get("conditions", []):
                if isinstance(condition, dict):
                    conditions.append(
                        {
                            "field": cls._identifier(condition.get("field")),
                            "op": cls._identifier(condition.get("op", "eq")),
                        }
                    )
            for child in group.get("groups", []):
                visit(child)

        visit(value)
        return {"group_count": group_count, "conditions": conditions}

    @classmethod
    def _summarize_metrics(cls, value: Any) -> list[dict[str, Any]] | None:
        if not isinstance(value, list):
            return None
        metrics: list[dict[str, Any]] = []
        for metric in value:
            if not isinstance(metric, dict):
                metrics.append({"invalid": True})
                continue
            summary: dict[str, Any] = {
                "function": cls._identifier(metric.get("function")),
                "as": cls._identifier(metric.get("as")),
            }
            if metric.get("field") is not None:
                summary["field"] = cls._identifier(metric.get("field"))
            if metric.get("fields") is not None:
                summary["fields"] = cls._identifiers(metric.get("fields"))
            if metric.get("filters") is not None:
                summary["filters"] = cls._summarize_filters(metric.get("filters"))
            if metric.get("explode") is True:
                summary["explode"] = True
            metrics.append(summary)
        return metrics

    @classmethod
    def _summarize_group_by(cls, value: Any) -> list[Any] | None:
        if not isinstance(value, list):
            return None
        result: list[Any] = []
        for item in value:
            if isinstance(item, str):
                result.append(cls._identifier(item))
            elif isinstance(item, dict):
                summary = {
                    "field": cls._identifier(item.get("field")),
                    "as": cls._identifier(item.get("as")),
                }
                if item.get("explode") is True:
                    summary["explode"] = True
                elif item.get("interval") is not None:
                    summary["interval"] = cls._identifier(item.get("interval"))
                    if item.get("size") is not None:
                        summary["has_size"] = True
                elif item.get("bucket_size") is not None:
                    summary["has_bucket_size"] = True
                result.append(summary)
            else:
                result.append("<invalid>")
        return result

    @classmethod
    def _summarize_one_per_group(cls, value: Any) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {
            "fields": cls._identifiers(value.get("fields")),
            "keep": cls._identifier(value.get("keep")),
        }

    @classmethod
    def _summarize_sort(cls, value: Any) -> list[dict[str, str]] | None:
        if not isinstance(value, list):
            return None
        result: list[dict[str, str]] = []
        for item in value:
            if isinstance(item, dict):
                result.append(
                    {
                        "field": cls._identifier(item.get("field")),
                        "direction": cls._identifier(item.get("direction", "asc")),
                    }
                )
        return result

    @classmethod
    def _identifiers(cls, value: Any) -> list[str] | None:
        if not isinstance(value, list):
            return None
        return [cls._identifier(item) for item in value]

    @staticmethod
    def _identifier(value: Any) -> str:
        return (
            value if isinstance(value, str) and _SAFE_IDENTIFIER.fullmatch(value) else "<invalid>"
        )

    @staticmethod
    def _integer(value: Any, default: int) -> int | str:
        if value is None:
            return default
        return value if isinstance(value, int) and not isinstance(value, bool) else "<invalid>"

    @staticmethod
    def _length(value: Any) -> int:
        return len(value) if isinstance(value, list) else 0

    @staticmethod
    def _summarize_result(result: ToolResult) -> dict[str, Any]:
        data: Any = result.structured_content
        if data is None:
            data = result.model_dump(mode="json")
        wire_data = result.model_dump(mode="json")
        encoded = json.dumps(
            wire_data, ensure_ascii=False, separators=(",", ":"), default=str
        ).encode()

        summary: dict[str, Any] = {
            "bytes": len(encoded),
            "estimated_tokens": (len(encoded) + 3) // 4,
        }
        if isinstance(data, dict):
            if isinstance(data.get("items"), list):
                summary["rows"] = len(data["items"])
            elif isinstance(data.get("rows"), list):
                summary["rows"] = len(data["rows"])
            if isinstance(data.get("page"), dict):
                summary["has_more"] = bool(data["page"].get("has_more"))
            if "truncated" in data:
                summary["truncated"] = bool(data["truncated"])
            if isinstance(data.get("summary"), dict):
                summary["set_counts"] = {
                    name: data["summary"].get(name)
                    for name in (
                        "catalog_values",
                        "matched_values",
                        "not_matched_values",
                        "qso_only_values",
                    )
                    if isinstance(data["summary"].get(name), int)
                }
        return summary

    @staticmethod
    def _flags(event: dict[str, Any]) -> list[str]:
        flags: list[str] = []
        result = event.get("result", {})
        sql = event.get("sql", [])
        if event["tool_duration_ms"] >= 1000:
            flags.append("slow")
        if any(statement.get("duration_ms", 0) >= 1000 for statement in sql):
            flags.append("slow_sql")
        if result.get("rows", 0) > 100 or result.get("bytes", 0) > 50_000:
            flags.append("large_result")
        if result.get("has_more"):
            flags.append("pagination_required")
        if result.get("truncated"):
            flags.append("truncated")
        return flags
