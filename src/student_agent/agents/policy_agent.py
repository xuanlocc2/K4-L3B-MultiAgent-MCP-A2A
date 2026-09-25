from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .coordinator import CoordinatorHandoff
from .mcp_client import ResilientGatewayClient
from .order_agent import OrderInvestigationResult
from .payment_agent import PaymentInvestigationResult
from .shipment_agent import ShipmentInvestigationResult


@dataclass
class PolicyEvaluationResult:
    assessment: dict[str, Any]
    root_cause_analysis: dict[str, Any]
    financial_resolution: dict[str, Any]
    resolution_actions: list[str]
    claim_assessments: list[dict[str, Any]] = field(default_factory=list)


class PolicyAgent:
    """Policy Decision Agent.

    Evaluates rules, determines primary issue, assigns responsibility,
    and formulates financial and operational resolution.
    """

    def __init__(self, client: ResilientGatewayClient) -> None:
        self.client = client
        self.actor = "policy-agent"

    async def run(
        self,
        handoff: CoordinatorHandoff,
        order_res: OrderInvestigationResult,
        payment_res: PaymentInvestigationResult,
        shipment_res: ShipmentInvestigationResult,
    ) -> PolicyEvaluationResult:
        case_id = handoff.case_id
        policy_version = handoff.raw_case.get("policy_version", "EC_POLICY_V2")

        # 1. Fetch policy from MCP Gateway
        policy_ev = await self.client.call_tool(
            "get_policy",
            actor=self.actor,
            case_id=case_id,
            policy_version=policy_version,
        )

        policy_rules: dict[str, Any] = {}
        if policy_ev and isinstance(policy_ev.get("data"), dict):
            policy_rules = policy_ev["data"].get("rules") or {}

        # 2. Analyze conditions and match claims
        claim_topics = [
            c.get("topic")
            for c in handoff.claims
            if c.get("topic") and c.get("topic") != "requested_full_refund"
        ]

        primary_issue = "insufficient_evidence"
        secondary_issues: list[str] = []
        ranked_causes: list[dict[str, Any]] = []
        responsible_parties: list[dict[str, Any]] = []
        recommended_refund = 0.0
        refund_lines: list[dict[str, Any]] = []
        resolution_actions: list[str] = []

        is_canceled = any(st == "canceled" for st in order_res.order_status.values())
        is_unavailable = any(st == "unavailable" for st in order_res.order_status.values())
        target_order = order_res.order_ids[0] if order_res.order_ids else None
        target_seller = (
            shipment_res.late_seller_ids[0]
            if shipment_res.late_seller_ids
            else (order_res.seller_ids[0] if order_res.seller_ids else None)
        )

        if handoff.entity_resolution["status"] == "not_found":
            primary_issue = "insufficient_evidence"
        elif is_canceled:
            primary_issue = "canceled_order_paid"
        elif is_unavailable:
            primary_issue = "unavailable_order_paid"
        elif "refund_failed" in claim_topics or payment_res.has_failed_refund:
            primary_issue = "refund_failed"
        elif "refund_pending" in claim_topics or payment_res.has_pending_refund:
            primary_issue = "refund_pending"
        elif "payment_mismatch" in claim_topics or payment_res.has_capture_mismatch:
            primary_issue = "payment_mismatch"
        elif "duplicate_charge" in claim_topics or payment_res.has_duplicate_charge:
            primary_issue = "duplicate_charge"
        elif shipment_res.confirmed_delay_actor == "logistics_provider" or (
            "late_delivery_logistics" in claim_topics and shipment_res.verdict == "logistics_delay"
        ):
            primary_issue = "late_delivery_logistics"
        elif shipment_res.confirmed_delay_actor == "seller" or (
            "late_delivery_seller" in claim_topics and shipment_res.verdict == "seller_delay"
        ):
            primary_issue = "late_delivery_seller"
        elif "late_delivery_logistics" in claim_topics:
            primary_issue = "late_delivery_logistics"
        elif "late_delivery_seller" in claim_topics:
            primary_issue = "late_delivery_seller"
        elif "valid_split_payment" in claim_topics:
            primary_issue = "valid_split_payment"
        elif "unsupported_claim" in claim_topics:
            primary_issue = "unsupported_claim"
        elif shipment_res.verdict == "seller_delay":
            primary_issue = "late_delivery_seller"
        elif shipment_res.verdict == "logistics_delay":
            primary_issue = "late_delivery_logistics"
        else:
            primary_issue = "unsupported_claim"

        # Confidence calibration based on evidence quality and completeness
        has_conflicts = len(order_res.data_conflicts) > 0
        if handoff.entity_resolution["status"] == "ambiguous":
            confidence = 0.50
        elif has_conflicts:
            confidence = 0.80  # Reduced confidence due to evidence conflict
        elif primary_issue in ("refund_pending", "insufficient_evidence"):
            confidence = 0.85  # Needs further investigation
        else:
            confidence = 0.95  # Clean audited evidence matches claim

        # Policy outcomes must come from the audited MCP response. A missing or
        # malformed rule is unresolved; it must never become a fabricated refund.
        rule = policy_rules.get(primary_issue) if isinstance(policy_rules, dict) else None
        policy_rule_available = isinstance(rule, dict)
        if policy_rule_available:
            case_status = rule.get("case_status", "needs_investigation")
            recommended_action = rule.get("recommended_action", "request_policy_review")
            try:
                rule_refund = float(rule.get("refund_brl", 0.0))
            except (TypeError, ValueError):
                rule_refund = 0.0
        else:
            case_status = "needs_investigation"
            recommended_action = "request_policy_review"
            rule_refund = 0.0
            confidence = min(confidence, 0.60)
        if (
            primary_issue in ("canceled_order_paid", "unavailable_order_paid")
            and payment_res.refundable_total_brl
        ):
            rule_refund = payment_res.refundable_total_brl

        # Populate root cause & responsible parties
        cause_code_map = {
            "canceled_order_paid": "ORDER_CANCELED_BEFORE_FULFILLMENT",
            "unavailable_order_paid": "ITEM_UNAVAILABLE_STOCK_OUT",
            "duplicate_charge": "PAYMENT_GATEWAY_DUPLICATE_AUTH",
            "refund_failed": "PAYMENT_GATEWAY_REFUND_FAILURE",
            "refund_pending": "REFUND_CLEARING_IN_PROGRESS",
            "payment_mismatch": "AMOUNT_CHARGED_DIFFERS_FROM_TOTAL",
            "late_delivery_seller": "SELLER_DISPATCH_SLA_BREACH",
            "late_delivery_logistics": "CARRIER_TRANSIT_DELAY",
            "valid_split_payment": "VALID_SPLIT_PAYMENT_METHOD",
            "unsupported_claim": "TRANSACTION_COMPLETED_PER_TERMS",
            "insufficient_evidence": "INSUFFICIENT_EVIDENCE_FOR_CLAIM",
        }
        ranked_causes.append(
            {
                "cause_code": cause_code_map.get(primary_issue, "INVESTIGATION_CONCLUSION"),
                "rank": 1,
            }
        )

        # Set responsible parties with strict cross-field consistency
        if primary_issue == "late_delivery_seller":
            responsible_parties.append({"party_type": "seller", "party_id": target_seller})
        elif primary_issue == "late_delivery_logistics":
            responsible_parties.append({"party_type": "logistics_provider", "party_id": None})
        elif primary_issue == "canceled_order_paid":
            responsible_parties.append({"party_type": "platform", "party_id": None})
        elif primary_issue == "unavailable_order_paid":
            responsible_parties.append({"party_type": "seller", "party_id": target_seller})
        elif primary_issue in (
            "duplicate_charge",
            "payment_mismatch",
            "refund_failed",
            "refund_pending",
        ):
            responsible_parties.append({"party_type": "payment_provider", "party_id": None})
        elif primary_issue in ("valid_split_payment", "unsupported_claim"):
            responsible_parties.append({"party_type": "customer", "party_id": None})
        else:
            responsible_parties.append({"party_type": "unknown", "party_id": None})

        # Calculate financial resolution
        action_code = recommended_action.upper()
        if not policy_rule_available:
            recommended_refund = 0.0
            refund_lines = []
            resolution_actions.append("REQUEST_POLICY_REVIEW")
        elif case_status == "action_required" and rule_refund > 0:
            recommended_refund = rule_refund
            entity_for_refund = (
                target_seller if primary_issue == "late_delivery_seller" else target_order
            )
            refund_lines.append(
                {
                    "reason_code": action_code,
                    "amount_brl": rule_refund,
                    "entity_id": entity_for_refund,
                }
            )
            resolution_actions.append(action_code)
        elif case_status == "no_action":
            recommended_refund = 0.0
            refund_lines = []
            resolution_actions.append("DOCUMENT_NO_ACTION")
        elif primary_issue == "refund_pending":
            recommended_refund = 0.0
            refund_lines = []
            resolution_actions.append("MONITOR_REFUND")
        else:
            recommended_refund = 0.0
            refund_lines = []
            resolution_actions.append("REQUEST_CUSTOMER_ORDER_DETAILS")

        # Claim assessments link each conclusion to the evidence that can
        # actually establish or refute it; arrival order is not meaningful.
        issue_tools = {
            "canceled_order_paid": ("get_order", "get_order_payments"),
            "unavailable_order_paid": ("get_order", "get_order_items", "get_order_payments"),
            "late_delivery_seller": ("get_order", "get_shipment_summary"),
            "late_delivery_logistics": ("get_order", "get_shipment_summary"),
            "valid_split_payment": ("get_order_payments", "get_payment_timeline"),
            "payment_mismatch": ("get_order_items", "get_order_payments", "get_payment_timeline"),
            "duplicate_charge": ("get_order_payments", "get_payment_timeline"),
            "refund_pending": ("get_order_payments", "get_refund_timeline"),
            "refund_failed": ("get_order_payments", "get_refund_timeline"),
            "unsupported_claim": (
                "get_order",
                "get_order_payments",
                "get_shipment_summary",
            ),
            "insufficient_evidence": ("get_customer_history", "get_order"),
        }

        def evidence_for_claim(topic: str | None) -> list[str]:
            tools = {
                "get_customer_history",
                "get_policy",
                *issue_tools.get(primary_issue, ()),
                *issue_tools.get(topic or "", ()),
            }
            if handoff.raw_case.get("investigation_scope", {}).get("include_product_context"):
                tools.update({"get_order_items", "get_product_context"})
            return self.client.refs_for_tools(*tools)

        claim_assessments = []
        for claim in handoff.claims:
            claim_id = claim.get("claim_id") or "claim_1"
            topic = claim.get("topic")
            if topic == primary_issue:
                verdict = "supported"
            elif topic == "requested_full_refund" and case_status == "action_required":
                verdict = "partially_supported"
            else:
                verdict = "unsupported"

            claim_assessments.append(
                {
                    "claim_id": str(claim_id),
                    "verdict": verdict,
                    "confidence": confidence,
                    "evidence_refs": evidence_for_claim(topic),
                }
            )

        # Emit policy_decided trace event
        self.client.trace.emit(
            case_id=case_id,
            event_type="policy_decided",
            actor=self.actor,
            decision_code=primary_issue.upper(),
            attributes={
                "case_status": case_status,
                "recommended_refund": recommended_refund,
                "confidence": confidence,
            },
        )

        return PolicyEvaluationResult(
            assessment={
                "primary_issue": primary_issue,
                "secondary_issues": secondary_issues,
                "case_status": case_status,
                "confidence": confidence,
            },
            root_cause_analysis={
                "ranked_causes": ranked_causes,
                "responsible_parties": responsible_parties,
            },
            financial_resolution={
                "currency": "BRL",
                "recommended_refund_brl": recommended_refund,
                "refund_lines": refund_lines,
            },
            resolution_actions=resolution_actions,
            claim_assessments=claim_assessments,
        )
