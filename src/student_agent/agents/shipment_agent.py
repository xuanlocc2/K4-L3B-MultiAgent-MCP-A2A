from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .mcp_client import ResilientGatewayClient


@dataclass
class ShipmentInvestigationResult:
    shipment_ids: list[str] = field(default_factory=list)
    verdict: str = "insufficient_evidence"
    late_seller_ids: list[str] = field(default_factory=list)
    timeline_complete: bool = False
    delivery_status: str = "unknown"
    confirmed_delay_actor: str | None = None


def _parse_iso(val: Any) -> datetime | None:
    if not val or not isinstance(val, str):
        return None
    try:
        return datetime.fromisoformat(val.replace("Z", "+00:00"))
    except Exception:
        return None


class ShipmentAgent:
    """Specialist agent for Logistics, Delivery timestamps, and Shipment analysis."""

    def __init__(self, client: ResilientGatewayClient) -> None:
        self.client = client
        self.actor = "shipment-agent"

    async def run(
        self,
        case_id: str,
        resolved_order_ids: list[str],
    ) -> ShipmentInvestigationResult:
        result = ShipmentInvestigationResult()

        if not resolved_order_ids:
            return result

        has_shipment_data = False
        is_lost = False
        is_returned = False
        has_seller_delay = False
        has_logistics_delay = False
        all_timelines_complete = True

        for order_id in resolved_order_ids:
            ship_ev = await self.client.call_tool(
                "get_shipment_summary", actor=self.actor, case_id=case_id, order_id=order_id
            )
            if not ship_ev:
                all_timelines_complete = False
                continue

            sdata = ship_ev.get("data")
            if not isinstance(sdata, dict):
                all_timelines_complete = False
                continue

            has_shipment_data = True
            shipments = sdata.get("shipments") or [sdata]

            # 1. Check audited events in shipment summary
            events = sdata.get("events") or []
            for ev in events:
                if isinstance(ev, dict):
                    ev_type = str(ev.get("event_type") or "").lower()
                    ev_actor = str(ev.get("actor") or "").lower()
                    ev_status = str(ev.get("status") or "").lower()
                    if "delivered_late" in ev_type and ev_status == "confirmed":
                        if "seller" in ev_actor:
                            has_seller_delay = True
                            result.confirmed_delay_actor = "seller"
                        elif "logistics" in ev_actor:
                            has_logistics_delay = True
                            result.confirmed_delay_actor = "logistics_provider"

            # 2. Check shipping limits
            limits = sdata.get("shipping_limits") or []
            carrier_at = _parse_iso(
                sdata.get("delivered_carrier_at") or sdata.get("order_delivered_carrier_date")
            )
            for lim in limits:
                if isinstance(lim, dict):
                    limit_at = _parse_iso(lim.get("shipping_limit_at"))
                    seller_id = lim.get("seller_id")
                    if carrier_at and limit_at and carrier_at > limit_at:
                        if seller_id and str(seller_id) not in result.late_seller_ids:
                            result.late_seller_ids.append(str(seller_id))
                        has_seller_delay = True

            # 3. Check individual shipments
            for s in shipments:
                if not isinstance(s, dict):
                    continue

                sid_seq = len(result.shipment_ids) + 1
                sid = s.get("shipment_id") or s.get("tracking_id") or f"shp_{order_id}_{sid_seq}"
                if sid not in result.shipment_ids:
                    result.shipment_ids.append(str(sid))

                status = str(s.get("shipment_status") or s.get("order_status") or "").lower()
                if "lost" in status:
                    is_lost = True
                if "return" in status:
                    is_returned = True

                delivered_at = _parse_iso(
                    s.get("delivered_customer_at")
                    or s.get("delivered_customer_date")
                    or s.get("order_delivered_customer_date")
                )
                estimated_delivery = _parse_iso(
                    s.get("estimated_delivery_at")
                    or s.get("estimated_delivery_date")
                    or s.get("order_estimated_delivery_date")
                )
                shipping_limit = _parse_iso(
                    s.get("shipping_limit_at") or s.get("shipping_limit_date")
                )
                carrier_handoff = _parse_iso(
                    s.get("delivered_carrier_at")
                    or s.get("carrier_handoff_date")
                    or s.get("order_delivered_carrier_date")
                )

                seller_id = s.get("seller_id")

                # Check seller handoff delay
                if carrier_handoff and shipping_limit and carrier_handoff > shipping_limit:
                    if seller_id and str(seller_id) not in result.late_seller_ids:
                        result.late_seller_ids.append(str(seller_id))
                    has_seller_delay = True

                # Check delivery delay
                if (
                    delivered_at
                    and estimated_delivery
                    and delivered_at > estimated_delivery
                    and not has_seller_delay
                ):
                    has_logistics_delay = True
                elif not delivered_at and not is_lost and not is_returned:
                    all_timelines_complete = False

        if not has_shipment_data:
            result.verdict = "insufficient_evidence"
            result.timeline_complete = False
            return result

        result.timeline_complete = all_timelines_complete

        if is_lost:
            result.verdict = "lost"
        elif is_returned:
            result.verdict = "returned"
        elif result.confirmed_delay_actor == "logistics_provider":
            result.verdict = "logistics_delay"
            result.late_seller_ids = []  # Carrier at fault, seller not liable
        elif result.confirmed_delay_actor == "seller" or result.late_seller_ids or has_seller_delay:
            result.verdict = "seller_delay"
        elif has_logistics_delay:
            result.verdict = "logistics_delay"
        else:
            result.verdict = "on_time"

        return result
