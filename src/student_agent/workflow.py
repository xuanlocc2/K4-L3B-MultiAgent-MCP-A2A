from __future__ import annotations

from typing import Any

from .agents.coordinator import CoordinatorAgent
from .agents.mcp_client import ResilientGatewayClient
from .agents.order_agent import OrderAgent
from .agents.payment_agent import PaymentAgent, PaymentInvestigationResult
from .agents.policy_agent import PolicyAgent
from .agents.shipment_agent import ShipmentAgent, ShipmentInvestigationResult
from .agents.verifier_agent import VerifierAgent
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Execute the Day09 L3B Multi-Agent A2A workflow.

    Workflow architecture:
    1. Coordinator / Router: Entity resolution, customer context, task assignments, handoff.
    2. Specialist Cluster:
       - Order/Item Agent: Investigates order rows, items, product context, and sellers.
       - Payment Agent: Reconciles payments, detects duplicates/mismatches, refund timelines.
       - Shipment Agent: Analyzes shipping milestones, carrier timelines, seller SLA delays.
    3. Policy Agent: Synthesizes evidence, assigns liability and root cause, determines refunds.
    4. Verifier Agent: Invariant verification, schema validation, and audit trail emission.
    """
    case_id = case["case_id"]
    client = ResilientGatewayClient(gateway, trace)
    contracts = trace.contracts

    # Instantiate agents
    coordinator = CoordinatorAgent(client)
    order_agent = OrderAgent(client)
    payment_agent = PaymentAgent(client)
    shipment_agent = ShipmentAgent(client)
    policy_agent = PolicyAgent(client)
    verifier_agent = VerifierAgent(client, contracts)

    # Step 1: Coordinator / Router (Emits task_assigned and resolves entities)
    handoff = await coordinator.run(case)
    resolved_order_ids = handoff.entity_resolution.get("resolved_order_ids") or []

    claim_topics = {
        str(claim.get("topic"))
        for claim in handoff.claims
        if claim.get("topic") and claim.get("topic") != "requested_full_refund"
    }
    investigation_scope = case.get("investigation_scope") or {}

    def assign(target: str, reason: str) -> None:
        trace.emit(
            case_id=case_id,
            event_type="task_assigned",
            actor="coordinator",
            target=target,
            attributes={"reason": reason},
        )

    # Step 2: Establish the primary order context. Item/product evidence is
    # collected only when the case scope explicitly asks for it.
    assign("order-agent", "establish_order_context")
    order_res = await order_agent.run(
        case_id,
        resolved_order_ids,
        case,
        include_product_context=bool(investigation_scope.get("include_product_context")),
        customer_history_orders=handoff.customer_history_orders,
    )

    order_requires_payment = any(
        status in {"canceled", "unavailable"} for status in order_res.order_status.values()
    )
    payment_topics = {
        "canceled_order_paid",
        "unavailable_order_paid",
        "valid_split_payment",
        "payment_mismatch",
        "duplicate_charge",
        "refund_pending",
        "refund_failed",
    }
    shipment_topics = {"late_delivery_seller", "late_delivery_logistics"}
    broad_verification = "unsupported_claim" in claim_topics

    payment_res = PaymentInvestigationResult()
    if resolved_order_ids and (
        order_requires_payment or broad_verification or bool(claim_topics & payment_topics)
    ):
        assign("payment-agent", "verify_payment_or_refund_claim")
        payment_res = await payment_agent.run(
            case_id,
            resolved_order_ids,
            expected_total_brl=round(order_res.items_total_brl + order_res.freight_total_brl, 2),
            claim_topics=claim_topics,
        )

    shipment_res = ShipmentInvestigationResult()
    if resolved_order_ids and (broad_verification or bool(claim_topics & shipment_topics)):
        assign("shipment-agent", "verify_delivery_claim")
        shipment_res = await shipment_agent.run(case_id, resolved_order_ids)

    # Step 3: Multi-Agent A2A Handoff to Policy Agent
    trace.emit(
        case_id=case_id,
        event_type="handoff",
        actor="specialist-cluster",
        target="policy-agent",
        attributes={
            "status": handoff.entity_resolution.get("status", "unknown"),
            "resolved_orders": ",".join(resolved_order_ids) or "none",
        },
    )

    # Step 4: Policy Agent retrieves the policy exactly once, after the
    # specialists have established which facts are material.
    assign("policy-agent", "apply_policy_to_verified_facts")
    policy_res = await policy_agent.run(handoff, order_res, payment_res, shipment_res)

    # Step 5: Verifier Agent (Cross-Field Invariants, Schema & Emits verification_completed)
    final_output = verifier_agent.assemble_and_verify(
        handoff, order_res, payment_res, shipment_res, policy_res
    )

    return final_output
