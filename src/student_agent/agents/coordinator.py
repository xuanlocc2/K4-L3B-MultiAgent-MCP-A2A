from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ResilientGatewayClient

HEX32_PATTERN = re.compile(r"^[a-f0-9]{32}$")


@dataclass
class CoordinatorHandoff:
    """A2A Handoff envelope passed from Coordinator to Specialists."""

    case_id: str
    entity_resolution: dict[str, Any]
    customer_context: dict[str, Any]
    customer_history_orders: list[dict[str, Any]] = field(default_factory=list)
    claims: list[dict[str, Any]] = field(default_factory=list)
    raw_case: dict[str, Any] = field(default_factory=dict)


class CoordinatorAgent:
    """Coordinator and Router Agent.

    Responsibilities:
    - Receives case context and resolves target entity / order ID.
    - Extracts or retrieves customer history.
    - Dispatches tasks to Order, Payment, and Shipment agents.
    - Emits A2A handoff events.
    """

    def __init__(self, client: ResilientGatewayClient) -> None:
        self.client = client
        self.actor = "coordinator"

    async def run(self, case: dict[str, Any]) -> CoordinatorHandoff:
        case_id = case["case_id"]

        # Resolve the target entity before the workflow assigns specialists.
        resolved_order_ids: list[str] = []
        rejected_candidates: list[str] = []
        resolution_status = "not_found"
        resolution_confidence = 0.0

        customer_req = case.get("customer_request", {})
        customer_unique_id = (
            case.get("customer_unique_id")
            or case.get("customer_unique_id_hint")
            or customer_req.get("customer_unique_id")
        )
        related_order_ids: list[str] = []
        customer_history_orders: list[dict[str, Any]] = []

        direct_order_id = case.get("order_id") or customer_req.get("claimed_order_id")
        candidate_order_ids = list(case.get("candidate_order_ids") or [])
        if direct_order_id and direct_order_id not in candidate_order_ids:
            candidate_order_ids.insert(0, direct_order_id)

        # If customer_unique_id exists, fetch customer history
        if customer_unique_id:
            history_evidence = await self.client.call_tool(
                "get_customer_history",
                actor=self.actor,
                case_id=case_id,
                customer_unique_id=customer_unique_id,
            )
            if history_evidence and isinstance(history_evidence.get("data"), dict):
                history_data = history_evidence["data"]
                orders = history_data.get("orders") or history_data.get("order_ids") or []
                if isinstance(orders, list):
                    for ord_item in orders:
                        if isinstance(ord_item, str):
                            oid = ord_item
                            customer_history_orders.append({"order_id": oid})
                        elif isinstance(ord_item, dict):
                            oid = ord_item.get("order_id")
                            customer_history_orders.append(ord_item)
                        else:
                            oid = None
                        if oid and oid not in related_order_ids:
                            related_order_ids.append(oid)

        # Check candidate resolution
        hex_candidates = [c for c in candidate_order_ids if HEX32_PATTERN.fullmatch(c)]
        decoy_candidates = [c for c in candidate_order_ids if not HEX32_PATTERN.fullmatch(c)]

        matched_in_history = [c for c in candidate_order_ids if c in related_order_ids]
        if direct_order_id and direct_order_id in matched_in_history:
            chosen = direct_order_id
            resolved_order_ids = [chosen]
            rejected_candidates = [c for c in candidate_order_ids if c != chosen]
            resolution_status = "resolved"
            resolution_confidence = 1.0
        elif len(matched_in_history) == 1:
            chosen = matched_in_history[0]
            resolved_order_ids = [chosen]
            rejected_candidates = [c for c in candidate_order_ids if c != chosen]
            resolution_status = "resolved"
            resolution_confidence = 0.95
        elif len(matched_in_history) > 1:
            resolved_order_ids = matched_in_history
            rejected_candidates = [c for c in candidate_order_ids if c not in matched_in_history]
            resolution_status = "ambiguous"
            resolution_confidence = 0.50
        elif hex_candidates:
            chosen = hex_candidates[0]
            order_ev = await self.client.call_tool(
                "get_order",
                actor=self.actor,
                case_id=case_id,
                order_id=chosen,
            )
            if order_ev and order_ev.get("data"):
                resolved_order_ids = [chosen]
                rejected_candidates = [c for c in candidate_order_ids if c != chosen]
                resolution_status = "resolved"
                resolution_confidence = 0.85
            else:
                rejected_candidates = candidate_order_ids
        elif candidate_order_ids:
            # Probe first candidate
            first_cand = candidate_order_ids[0]
            order_ev = await self.client.call_tool(
                "get_order",
                actor=self.actor,
                case_id=case_id,
                order_id=first_cand,
            )
            if order_ev and order_ev.get("data"):
                resolved_order_ids = [first_cand]
                rejected_candidates = candidate_order_ids[1:]
                resolution_status = "resolved"
                resolution_confidence = 0.85
            else:
                resolved_order_ids = []
                rejected_candidates = candidate_order_ids
                resolution_status = "not_found"
                resolution_confidence = 0.0
        else:
            resolved_order_ids = []
            rejected_candidates = []
            resolution_status = "not_found"
            resolution_confidence = 0.0

        for oid in resolved_order_ids:
            if oid not in related_order_ids:
                related_order_ids.append(oid)

        for decoy in decoy_candidates:
            if decoy not in rejected_candidates and decoy not in resolved_order_ids:
                rejected_candidates.append(decoy)

        entity_resolution = {
            "status": resolution_status,
            "resolved_order_ids": resolved_order_ids,
            "rejected_candidates": rejected_candidates,
            "confidence": resolution_confidence,
        }

        customer_context = {
            "customer_unique_id": customer_unique_id,
            "related_order_ids": related_order_ids,
        }

        claims = list(case.get("claims") or customer_req.get("claims") or [])

        return CoordinatorHandoff(
            case_id=case_id,
            entity_resolution=entity_resolution,
            customer_context=customer_context,
            customer_history_orders=customer_history_orders,
            claims=claims,
            raw_case=case,
        )
