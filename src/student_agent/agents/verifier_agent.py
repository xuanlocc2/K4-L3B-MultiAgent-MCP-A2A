from __future__ import annotations

from typing import Any

from ..contracts import Contracts
from .coordinator import CoordinatorHandoff
from .mcp_client import ResilientGatewayClient
from .order_agent import OrderInvestigationResult
from .payment_agent import PaymentInvestigationResult
from .policy_agent import PolicyEvaluationResult
from .shipment_agent import ShipmentInvestigationResult


class VerifierAgent:
    """Verifier and Gatekeeper Agent.

    Enforces all verification invariants and strictly validates the output
    against the official contract schema before output finalization.
    """

    def __init__(self, client: ResilientGatewayClient, contracts: Contracts) -> None:
        self.client = client
        self.contracts = contracts
        self.actor = "verifier-agent"

    def assemble_and_verify(
        self,
        handoff: CoordinatorHandoff,
        order_res: OrderInvestigationResult,
        payment_res: PaymentInvestigationResult,
        shipment_res: ShipmentInvestigationResult,
        policy_res: PolicyEvaluationResult,
    ) -> dict[str, Any]:
        case_id = handoff.case_id

        # 1. Deduplicate and clean entity sets
        order_ids = list(dict.fromkeys(order_res.order_ids))
        item_ids = list(dict.fromkeys(order_res.item_ids))
        seller_ids = list(dict.fromkeys(order_res.seller_ids))
        payment_refs = list(dict.fromkeys(payment_res.payment_references))
        shipment_ids = list(dict.fromkeys(shipment_res.shipment_ids))

        # 2. Evidence references - strictly audited from this run
        evidence_refs = list(dict.fromkeys(self.client.collected_evidence_refs))

        # 3. Data conflicts
        data_conflicts = list(order_res.data_conflicts)

        # 4. Construct payload strictly matching l3b-output-v2.schema.json
        output: dict[str, Any] = {
            "schema_version": "day09-l3b-output-v2",
            "case_id": case_id,
            "assessment": policy_res.assessment,
            "affected_entities": {
                "order_ids": order_ids,
                "item_ids": item_ids,
                "seller_ids": seller_ids,
                "payment_references": payment_refs,
                "shipment_ids": shipment_ids,
            },
            "entity_resolution": handoff.entity_resolution,
            "customer_context": handoff.customer_context,
            "shipment_analysis": {
                "verdict": shipment_res.verdict,
                "late_seller_ids": list(dict.fromkeys(shipment_res.late_seller_ids)),
                "timeline_complete": shipment_res.timeline_complete,
            },
            "payment_analysis": {
                "verdict": payment_res.verdict,
                "captured_total_brl": payment_res.captured_total_brl,
                "refunded_total_brl": payment_res.refunded_total_brl,
                "refundable_total_brl": payment_res.refundable_total_brl,
            },
            "root_cause_analysis": policy_res.root_cause_analysis,
            "evidence_refs": evidence_refs,
            "data_conflicts": data_conflicts,
            "financial_resolution": policy_res.financial_resolution,
            "resolution_actions": list(dict.fromkeys(policy_res.resolution_actions)),
        }

        if policy_res.claim_assessments:
            output["claim_assessments"] = policy_res.claim_assessments

        # 5. Invariant Checks & Cross-field Consistency
        # Invariant: Disjoint resolved and rejected candidates
        resolved_set = set(output["entity_resolution"]["resolved_order_ids"])
        rejected_candidates = output["entity_resolution"]["rejected_candidates"]
        output["entity_resolution"]["rejected_candidates"] = [
            c for c in rejected_candidates if c not in resolved_set
        ]

        # Consistency: Cross-field responsible parties validation
        primary_issue = output["assessment"]["primary_issue"]
        case_status = output["assessment"]["case_status"]

        if primary_issue == "late_delivery_seller":
            seller_id = (
                output["shipment_analysis"]["late_seller_ids"][0]
                if output["shipment_analysis"]["late_seller_ids"]
                else (seller_ids[0] if seller_ids else None)
            )
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "seller", "party_id": seller_id}
            ]
        elif primary_issue == "late_delivery_logistics":
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "logistics_provider", "party_id": None}
            ]
            output["shipment_analysis"]["late_seller_ids"] = []
        elif primary_issue == "canceled_order_paid":
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "platform", "party_id": None}
            ]
        elif primary_issue == "unavailable_order_paid":
            seller_id = seller_ids[0] if seller_ids else None
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "seller", "party_id": seller_id}
            ]
        elif primary_issue in (
            "duplicate_charge",
            "payment_mismatch",
            "refund_failed",
            "refund_pending",
        ):
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "payment_provider", "party_id": None}
            ]
        elif primary_issue in ("valid_split_payment", "unsupported_claim"):
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "customer", "party_id": None}
            ]
        elif primary_issue == "insufficient_evidence":
            output["root_cause_analysis"]["responsible_parties"] = [
                {"party_type": "unknown", "party_id": None}
            ]

        # Consistency: Status vs financial resolution
        if case_status in ("no_action", "needs_investigation"):
            output["financial_resolution"]["recommended_refund_brl"] = 0.0
            output["financial_resolution"]["refund_lines"] = []
            if case_status == "no_action":
                output["resolution_actions"] = ["DOCUMENT_NO_ACTION"]
            elif "REQUEST_POLICY_REVIEW" in output["resolution_actions"]:
                output["resolution_actions"] = ["REQUEST_POLICY_REVIEW"]
            elif primary_issue == "refund_pending":
                output["resolution_actions"] = ["MONITOR_REFUND"]
            else:
                output["resolution_actions"] = ["REQUEST_CUSTOMER_ORDER_DETAILS"]
        else:
            rec_refund = output["financial_resolution"]["recommended_refund_brl"]
            refund_lines = output["financial_resolution"]["refund_lines"]
            lines_sum = round(sum(line["amount_brl"] for line in refund_lines), 2)
            if lines_sum != rec_refund:
                output["financial_resolution"]["recommended_refund_brl"] = lines_sum

        # Consistency: Confidence calibration
        if len(data_conflicts) > 0 and output["assessment"]["confidence"] > 0.80:
            output["assessment"]["confidence"] = 0.80
        elif primary_issue in ("refund_pending", "insufficient_evidence"):
            output["assessment"]["confidence"] = 0.85
        elif output["assessment"]["confidence"] > 0.95:
            output["assessment"]["confidence"] = 0.95

        # Deduplicate resolution actions and cap at 8 items
        output["resolution_actions"] = list(dict.fromkeys(output["resolution_actions"]))[:8]

        # Invariant: Public JSON Schema Validation
        self.contracts.validate_output(output, f"verifier:case_{case_id}")

        # 6. Emit verification_completed trace event
        self.client.trace.emit(
            case_id=case_id,
            event_type="verification_completed",
            actor=self.actor,
            decision_code="VERIFIED",
            evidence_refs=evidence_refs[:10],
            attributes={
                "valid": True,
                "primary_issue": output["assessment"]["primary_issue"],
                "evidence_count": len(evidence_refs),
            },
        )

        return output
