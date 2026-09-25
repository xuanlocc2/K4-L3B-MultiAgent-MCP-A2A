from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ResilientGatewayClient


@dataclass
class OrderInvestigationResult:
    order_ids: list[str] = field(default_factory=list)
    item_ids: list[str] = field(default_factory=list)
    seller_ids: list[str] = field(default_factory=list)
    order_status: dict[str, str] = field(default_factory=dict)
    items_total_brl: float = 0.0
    freight_total_brl: float = 0.0
    data_conflicts: list[dict[str, Any]] = field(default_factory=list)


class OrderAgent:
    """Specialist agent for Orders, Items, Products, and Sellers."""

    def __init__(self, client: ResilientGatewayClient) -> None:
        self.client = client
        self.actor = "order-agent"

    async def run(
        self,
        case_id: str,
        resolved_order_ids: list[str],
        raw_case: dict[str, Any],
        *,
        include_product_context: bool = False,
    ) -> OrderInvestigationResult:
        result = OrderInvestigationResult()

        for order_id in resolved_order_ids:
            if order_id not in result.order_ids:
                result.order_ids.append(order_id)

            # 1. get_order
            order_ev = await self.client.call_tool(
                "get_order", actor=self.actor, case_id=case_id, order_id=order_id
            )
            if order_ev and isinstance(order_ev.get("data"), dict):
                data = order_ev["data"]
                status = data.get("order_status") or data.get("status") or "unknown"
                result.order_status[order_id] = str(status).lower()

                claimed_status = raw_case.get("claimed_order_status") or raw_case.get(
                    "customer_request", {}
                ).get("claimed_order_status")
                if claimed_status and claimed_status.lower() != str(status).lower():
                    result.data_conflicts.append(
                        {
                            "field": "order_status",
                            "sources": ["case_claim", "mcp_get_order"],
                            "selected_source": "mcp_get_order",
                            "resolution_code": "AUTHORITATIVE_ORDER_ROW_PRECEDENCE",
                        }
                    )

            # Check customer history if available for order row discrepancies
            customer_uid = (
                raw_case.get("customer_unique_id")
                or raw_case.get("customer_unique_id_hint")
                or raw_case.get("customer_request", {}).get("customer_unique_id")
            )
            if customer_uid:
                hist_ev = await self.client.call_tool(
                    "get_customer_history",
                    actor=self.actor,
                    case_id=case_id,
                    customer_unique_id=customer_uid,
                )
                if hist_ev and isinstance(hist_ev.get("data"), dict):
                    h_orders = hist_ev["data"].get("orders") or []
                    for h_ord in h_orders:
                        if isinstance(h_ord, dict) and h_ord.get("order_id") == order_id:
                            h_status = str(h_ord.get("order_status") or "").lower()
                            curr_status = result.order_status.get(order_id, "")
                            if h_status in ("canceled", "unavailable") and curr_status != h_status:
                                result.data_conflicts.append(
                                    {
                                        "field": "order_status",
                                        "sources": ["mcp_get_order", "mcp_get_customer_history"],
                                        "selected_source": "mcp_get_customer_history",
                                        "resolution_code": "LATEST_EVENT_TIMESTAMP_PRECEDENCE",
                                    }
                                )
                                result.order_status[order_id] = h_status
                                break

            # 2. get_order_items (handles list or dict)
            items_ev = await self.client.call_tool(
                "get_order_items", actor=self.actor, case_id=case_id, order_id=order_id
            )
            if items_ev:
                raw_data = items_ev.get("data")
                items = (
                    raw_data
                    if isinstance(raw_data, list)
                    else (raw_data.get("items") or raw_data.get("order_items") or [])
                    if isinstance(raw_data, dict)
                    else []
                )
                for item in items:
                    if isinstance(item, dict):
                        iid = (
                            item.get("product_id")
                            or item.get("order_item_id")
                            or item.get("item_id")
                        )
                        if iid and str(iid) not in result.item_ids:
                            result.item_ids.append(str(iid))

                        sid = item.get("seller_id")
                        if sid and str(sid) not in result.seller_ids:
                            result.seller_ids.append(str(sid))

                        try:
                            price = float(item.get("price") or 0.0)
                        except (ValueError, TypeError):
                            price = 0.0
                        try:
                            freight = float(item.get("freight_value") or 0.0)
                        except (ValueError, TypeError):
                            freight = 0.0

                        result.items_total_brl += price
                        result.freight_total_brl += freight

            # Product context is explicitly requested by the case scope. Fetch it only
            # after item IDs are known, and only once per distinct product.
            if include_product_context:
                for product_id in list(result.item_ids):
                    await self.client.call_tool(
                        "get_product_context",
                        actor=self.actor,
                        case_id=case_id,
                        product_id=product_id,
                    )

            # 3. get_sellers if seller_ids empty
            if not result.seller_ids:
                sellers_ev = await self.client.call_tool(
                    "get_sellers", actor=self.actor, case_id=case_id, order_id=order_id
                )
                if sellers_ev:
                    sdata = sellers_ev.get("data")
                    s_list = (
                        sdata
                        if isinstance(sdata, list)
                        else (sdata.get("sellers") or [])
                        if isinstance(sdata, dict)
                        else []
                    )
                    for s in s_list:
                        sid = s if isinstance(s, str) else s.get("seller_id")
                        if sid and str(sid) not in result.seller_ids:
                            result.seller_ids.append(str(sid))

        return result
