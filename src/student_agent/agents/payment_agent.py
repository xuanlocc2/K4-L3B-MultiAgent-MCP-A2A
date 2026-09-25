from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .mcp_client import ResilientGatewayClient


@dataclass
class PaymentInvestigationResult:
    payment_references: list[str] = field(default_factory=list)
    verdict: str = "insufficient_evidence"
    captured_total_brl: float | None = None
    refunded_total_brl: float | None = None
    refundable_total_brl: float | None = None
    has_pending_refund: bool = False
    has_failed_refund: bool = False
    has_duplicate_charge: bool = False
    has_capture_mismatch: bool = False
    raw_payments: list[dict[str, Any]] = field(default_factory=list)


class PaymentAgent:
    """Specialist agent for Payment reconciliation and Refund analysis."""

    def __init__(self, client: ResilientGatewayClient) -> None:
        self.client = client
        self.actor = "payment-agent"

    async def run(
        self,
        case_id: str,
        resolved_order_ids: list[str],
        expected_total_brl: float = 0.0,
        *,
        claim_topics: set[str] | None = None,
    ) -> PaymentInvestigationResult:
        result = PaymentInvestigationResult()
        topics = claim_topics or set()
        needs_refund_timeline = bool(topics & {"refund_pending", "refund_failed"})
        needs_payment_timeline = bool(
            topics & {"duplicate_charge", "payment_mismatch", "valid_split_payment"}
        )

        if not resolved_order_ids:
            return result

        total_captured = 0.0
        total_refunded = 0.0
        has_payments_data = False

        for order_id in resolved_order_ids:
            # 1. get_order_payments (handles list or dict)
            pay_ev = await self.client.call_tool(
                "get_order_payments", actor=self.actor, case_id=case_id, order_id=order_id
            )
            if pay_ev:
                pdata = pay_ev.get("data")
                payments = (
                    pdata
                    if isinstance(pdata, list)
                    else (pdata.get("payments") or pdata.get("order_payments") or [])
                    if isinstance(pdata, dict)
                    else []
                )
                if payments:
                    has_payments_data = True
                pay_keys: dict[tuple[str, float], int] = {}
                for p in payments:
                    if isinstance(p, dict):
                        result.raw_payments.append(p)
                        try:
                            pval = float(p.get("payment_value") or 0.0)
                        except (ValueError, TypeError):
                            pval = 0.0
                        total_captured += pval

                        seq_num = p.get("payment_sequential", len(result.payment_references) + 1)
                        pref = (
                            p.get("payment_reference")
                            or p.get("payment_id")
                            or f"pay_{order_id}_{seq_num}"
                        )
                        if pref not in result.payment_references:
                            result.payment_references.append(str(pref))

                        ptype = str(p.get("payment_type") or "").lower()
                        payment_status = str(
                            p.get("status") or p.get("payment_status") or ""
                        ).lower()
                        if "refund" in payment_status and "pending" in payment_status:
                            result.has_pending_refund = True
                        if "refund" in payment_status and any(
                            marker in payment_status for marker in ("failed", "error")
                        ):
                            result.has_failed_refund = True
                        key = (ptype, round(pval, 2))
                        pay_keys[key] = pay_keys.get(key, 0) + 1
                        if pay_keys[key] > 1 and pval > 0:
                            result.has_duplicate_charge = True

            # Refund evidence is relevant only to refund disputes.
            if needs_refund_timeline:
                ref_ev = await self.client.call_tool(
                    "get_refund_timeline", actor=self.actor, case_id=case_id, order_id=order_id
                )
            else:
                ref_ev = None
            if ref_ev:
                rdata = ref_ev.get("data")
                events = (
                    rdata
                    if isinstance(rdata, list)
                    else (rdata.get("refund_events") or rdata.get("events") or [])
                    if isinstance(rdata, dict)
                    else []
                )
                for ev in events:
                    if isinstance(ev, dict):
                        status = str(ev.get("status") or ev.get("event_type") or "").lower()
                        try:
                            amount = float(ev.get("amount") or ev.get("refund_amount") or 0.0)
                        except (ValueError, TypeError):
                            amount = 0.0
                        if "completed" in status or "success" in status or "refunded" in status:
                            total_refunded += amount
                        elif "pending" in status:
                            result.has_pending_refund = True
                        elif "failed" in status or "error" in status:
                            result.has_failed_refund = True

            # Detailed payment events distinguish duplicate/split/mismatch claims.
            if needs_payment_timeline:
                pt_ev = await self.client.call_tool(
                    "get_payment_timeline", actor=self.actor, case_id=case_id, order_id=order_id
                )
            else:
                pt_ev = None
            if pt_ev:
                pt_data = pt_ev.get("data")
                events = (
                    pt_data
                    if isinstance(pt_data, list)
                    else (pt_data.get("timeline") or pt_data.get("events") or [])
                    if isinstance(pt_data, dict)
                    else []
                )
                for ev in events:
                    if isinstance(ev, dict):
                        ev_type = str(ev.get("event_type") or ev.get("status") or "").lower()
                        if "duplicate" in ev_type:
                            result.has_duplicate_charge = True
                        if "mismatch" in ev_type or "reconciliation" in ev_type:
                            result.has_capture_mismatch = True
                        if "refund" in ev_type and "pending" in ev_type:
                            result.has_pending_refund = True
                        if "refund" in ev_type and any(
                            marker in ev_type for marker in ("failed", "error")
                        ):
                            result.has_failed_refund = True

        if not has_payments_data:
            result.verdict = "insufficient_evidence"
            return result

        result.captured_total_brl = round(total_captured, 2)
        result.refunded_total_brl = round(total_refunded, 2)
        result.refundable_total_brl = round(max(0.0, total_captured - total_refunded), 2)

        # Verdict logic
        if result.has_duplicate_charge:
            result.verdict = "duplicate_capture"
        elif result.has_capture_mismatch:
            result.verdict = "capture_mismatch"
        elif result.has_failed_refund:
            result.verdict = "refund_failed"
        elif result.has_pending_refund:
            result.verdict = "refund_pending"
        elif (
            result.refunded_total_brl >= result.captured_total_brl and result.captured_total_brl > 0
        ):
            result.verdict = "refunded"
        elif expected_total_brl > 0 and abs(result.captured_total_brl - expected_total_brl) > 1.0:
            result.verdict = "capture_mismatch"
        else:
            result.verdict = "reconciled"

        return result
