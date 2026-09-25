from __future__ import annotations

import asyncio
from typing import Any

from ..mcp_gateway import EvidenceGateway
from ..trace import TraceWriter


class ResilientGatewayClient:
    """Wrapper around EvidenceGateway providing caching, retries and tracing."""

    def __init__(
        self,
        gateway: EvidenceGateway,
        trace: TraceWriter,
        max_retries: int = 2,
        retry_delay_seconds: float = 0.5,
    ) -> None:
        self.gateway = gateway
        self.trace = trace
        self.max_retries = max_retries
        self.retry_delay_seconds = retry_delay_seconds
        self._cache: dict[tuple[str, str, tuple[tuple[str, Any], ...]], dict[str, Any]] = {}
        self.collected_evidence_refs: list[str] = []
        self.evidence_refs_by_tool: dict[str, list[str]] = {}

    def refs_for_tools(self, *tool_names: str) -> list[str]:
        """Return audited refs for the requested tools in original consumption order."""
        wanted = set(tool_names)
        return [
            ref
            for ref in self.collected_evidence_refs
            if any(ref in self.evidence_refs_by_tool.get(tool, []) for tool in wanted)
        ]

    async def call_tool(
        self,
        tool_name: str,
        *,
        actor: str,
        case_id: str,
        **arguments: Any,
    ) -> dict[str, Any] | None:
        """Call MCP tool with per-case deduplication, bounded retries and tracing."""
        clean_args = {k: str(v) for k, v in arguments.items() if v is not None}
        cache_key = (tool_name, case_id, tuple(sorted(clean_args.items())))

        if cache_key in self._cache:
            return self._cache[cache_key]

        for attempt in range(self.max_retries + 1):
            try:
                response = await self.gateway.call(tool_name, case_id=case_id, **clean_args)
                ref = response.get("evidence_ref")
                if ref and ref not in self.collected_evidence_refs:
                    self.collected_evidence_refs.append(ref)
                if ref:
                    refs = self.evidence_refs_by_tool.setdefault(tool_name, [])
                    if ref not in refs:
                        refs.append(ref)

                # Emit observable trace event
                self.trace.emit(
                    case_id=case_id,
                    event_type="tool_result_consumed",
                    actor=actor,
                    tool_name=tool_name,
                    evidence_refs=[ref] if ref else None,
                )

                self._cache[cache_key] = response
                return response
            except Exception:
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_delay_seconds * (attempt + 1))

        # Tool failed after retry budget: do not hallucinate, return None
        return None
