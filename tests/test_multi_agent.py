from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from student_agent.contracts import Contracts
from student_agent.trace import TraceWriter
from student_agent.workflow import solve_case


class MockEvidenceGateway:
    """Mock gateway returning compliant MCP evidence envelopes."""

    def __init__(self, contracts: Contracts, scenario: str = "normal") -> None:
        self.contracts = contracts
        self.scenario = scenario
        self.call_history: list[dict[str, Any]] = []

    async def list_tools(self) -> list[str]:
        return [
            "get_customer_history",
            "get_order",
            "get_order_items",
            "get_order_payments",
            "get_payment_timeline",
            "get_policy",
            "get_product_context",
            "get_refund_timeline",
            "get_sellers",
            "get_shipment_summary",
        ]

    async def call(self, tool_name: str, *, case_id: str, **arguments: str) -> dict[str, Any]:
        self.call_history.append({"tool": tool_name, "case_id": case_id, "args": arguments})

        domain_map = {
            "get_order": "order",
            "get_order_items": "item",
            "get_order_payments": "payment",
            "get_payment_timeline": "payment",
            "get_shipment_summary": "shipment",
            "get_sellers": "seller",
            "get_policy": "policy",
            "get_customer_history": "customer",
            "get_product_context": "product",
            "get_refund_timeline": "refund",
        }

        if tool_name == "get_order":
            status = (
                "canceled"
                if self.scenario in {"canceled", "missing_policy_canceled"}
                else "delivered"
            )
            data = {"order_id": arguments.get("order_id", "ORD_001"), "order_status": status}
        elif tool_name == "get_order_items":
            data = {
                "items": [
                    {
                        "order_item_id": 1,
                        "product_id": "prod_12345",
                        "seller_id": "seller_abcde",
                        "price": 99.9,
                        "freight_value": 15.0,
                    }
                ]
            }
        elif tool_name == "get_order_payments":
            data = {
                "payments": [
                    {
                        "payment_sequential": 1,
                        "payment_type": "credit_card",
                        "payment_installments": 1,
                        "payment_value": 114.9,
                        "payment_reference": "pay_ref_001",
                    }
                ]
            }
        elif tool_name == "get_shipment_summary":
            is_late = self.scenario == "late_seller"
            carrier_date = "2026-03-01T12:00:00Z" if is_late else "2026-02-24T12:00:00Z"
            delivered_date = "2026-03-10T12:00:00Z" if is_late else "2026-03-01T12:00:00Z"
            data = {
                "shipments": [
                    {
                        "shipment_id": "shp_track_001",
                        "shipment_status": "delivered",
                        "delivered_customer_date": delivered_date,
                        "estimated_delivery_date": "2026-03-05T12:00:00Z",
                        "shipping_limit_date": "2026-02-25T12:00:00Z",
                        "carrier_handoff_date": carrier_date,
                        "seller_id": "seller_abcde",
                    }
                ]
            }
        elif tool_name == "get_policy":
            data = {
                "policy_version": "v2",
                "rules": {
                    "canceled_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "refund_brl": 79.0,
                    },
                    "unavailable_order_paid": {
                        "case_status": "action_required",
                        "recommended_action": "issue_refund",
                        "refund_brl": 89.0,
                    },
                    "duplicate_charge": {
                        "case_status": "action_required",
                        "recommended_action": "refund_duplicate_charge",
                        "refund_brl": 64.0,
                    },
                    "late_delivery_seller": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": 18.0,
                    },
                    "late_delivery_logistics": {
                        "case_status": "action_required",
                        "recommended_action": "refund_freight",
                        "refund_brl": 16.0,
                    },
                    "refund_failed": {
                        "case_status": "action_required",
                        "recommended_action": "retry_refund",
                        "refund_brl": 52.0,
                    },
                    "refund_pending": {
                        "case_status": "needs_investigation",
                        "recommended_action": "monitor_refund",
                        "refund_brl": 0.0,
                    },
                    "payment_mismatch": {
                        "case_status": "action_required",
                        "recommended_action": "reconcile_payment",
                        "refund_brl": 35.0,
                    },
                    "unsupported_claim": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": 0.0,
                    },
                    "valid_split_payment": {
                        "case_status": "no_action",
                        "recommended_action": "document_no_action",
                        "refund_brl": 0.0,
                    },
                    "insufficient_evidence": {
                        "case_status": "needs_investigation",
                        "recommended_action": "request_customer_order_details",
                        "refund_brl": 0.0,
                    },
                },
            }
            if self.scenario == "missing_policy_canceled":
                data["rules"] = {}
        elif tool_name == "get_customer_history":
            data = {
                "customer_unique_id": arguments.get("customer_unique_id", "cust_123"),
                "orders": ["ORD_MATCHED"],
            }
        else:
            data = {}

        envelope = {
            "schema_version": "day09-mcp-evidence-v1",
            "evidence_ref": f"ev_{tool_name}_{case_id}_1234567890abcdef",
            "result_hash": "sha256:" + "a" * 64,
            "domain": domain_map.get(tool_name, "order"),
            "data": data,
        }
        self.contracts.validate_evidence(envelope)
        return envelope


def test_solve_case_normal(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace_path = tmp_path / "traces" / "trace.jsonl"
        trace = TraceWriter(trace_path, contracts)
        gateway = MockEvidenceGateway(contracts, scenario="normal")

        case = {
            "case_id": "CASE_NORMAL_01",
            "order_id": "ORD_001",
            "customer_unique_id": "cust_123",
            "claims": [{"claim_id": "claim_01", "description": "Order delivered on time"}],
        }

        trace.emit(case_id="CASE_NORMAL_01", event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        trace.emit(case_id="CASE_NORMAL_01", event_type="case_finalized", actor="coordinator")

        contracts.validate_output(output, "normal_output")
        assert output["assessment"]["case_status"] == "no_action"
        assert output["financial_resolution"]["recommended_refund_brl"] == 0.0

        # Validate trace event sequence
        lines = [
            json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line
        ]
        event_types = [e["event_type"] for e in lines]
        assert event_types[0] == "case_received"
        assert "task_assigned" in event_types
        assert "handoff" in event_types
        assert "tool_result_consumed" in event_types
        assert "policy_decided" in event_types
        assert "verification_completed" in event_types
        assert event_types[-1] == "case_finalized"

    asyncio.run(_test())


def test_solve_case_canceled_order_refund(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace_path = tmp_path / "traces" / "trace.jsonl"
        trace = TraceWriter(trace_path, contracts)
        gateway = MockEvidenceGateway(contracts, scenario="canceled")

        case = {
            "case_id": "CASE_CANCELED_01",
            "order_id": "ORD_002",
            "customer_unique_id": "cust_123",
        }

        trace.emit(case_id="CASE_CANCELED_01", event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        trace.emit(case_id="CASE_CANCELED_01", event_type="case_finalized", actor="coordinator")

        contracts.validate_output(output, "canceled_output")
        assert output["assessment"]["primary_issue"] == "canceled_order_paid"
        assert output["assessment"]["case_status"] == "action_required"
        assert output["financial_resolution"]["recommended_refund_brl"] == 114.9
        assert len(output["financial_resolution"]["refund_lines"]) == 1
        assert output["financial_resolution"]["refund_lines"][0]["amount_brl"] == 114.9

    asyncio.run(_test())


def test_solve_case_candidate_resolution(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace_path = tmp_path / "traces" / "trace.jsonl"
        trace = TraceWriter(trace_path, contracts)
        gateway = MockEvidenceGateway(contracts, scenario="normal")

        case = {
            "case_id": "CASE_CAND_01",
            "candidate_order_ids": ["ORD_REJECTED", "ORD_MATCHED"],
            "customer_unique_id": "cust_123",
        }

        trace.emit(case_id="CASE_CAND_01", event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        trace.emit(case_id="CASE_CAND_01", event_type="case_finalized", actor="coordinator")

        contracts.validate_output(output, "candidate_output")
        assert output["entity_resolution"]["status"] == "resolved"
        assert output["entity_resolution"]["resolved_order_ids"] == ["ORD_MATCHED"]
        assert output["entity_resolution"]["rejected_candidates"] == ["ORD_REJECTED"]

    asyncio.run(_test())


def test_missing_policy_does_not_fabricate_refund(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace = TraceWriter(tmp_path / "trace.jsonl", contracts)
        gateway = MockEvidenceGateway(contracts, scenario="missing_policy_canceled")
        case = {
            "case_id": "CASE_POLICY_MISSING",
            "order_id": "ORD_004",
            "claims": [{"claim_id": "claim-policy", "topic": "canceled_order_paid"}],
        }

        output = await solve_case(case, gateway, trace)

        assert output["assessment"]["case_status"] == "needs_investigation"
        assert output["assessment"]["confidence"] == 0.60
        assert output["financial_resolution"]["recommended_refund_brl"] == 0.0
        assert output["financial_resolution"]["refund_lines"] == []
        assert output["resolution_actions"] == ["REQUEST_POLICY_REVIEW"]

    asyncio.run(_test())


def test_solve_case_late_seller(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        trace_path = tmp_path / "traces" / "trace.jsonl"
        trace = TraceWriter(trace_path, contracts)
        gateway = MockEvidenceGateway(contracts, scenario="late_seller")

        case = {
            "case_id": "CASE_LATE_01",
            "order_id": "ORD_003",
            "claims": [{"claim_id": "claim_late", "topic": "late_delivery_seller"}],
            "investigation_scope": {"include_product_context": True},
        }

        trace.emit(case_id="CASE_LATE_01", event_type="case_received", actor="coordinator")
        output = await solve_case(case, gateway, trace)
        trace.emit(case_id="CASE_LATE_01", event_type="case_finalized", actor="coordinator")

        contracts.validate_output(output, "late_output")
        assert output["shipment_analysis"]["verdict"] == "seller_delay"
        assert "seller_abcde" in output["shipment_analysis"]["late_seller_ids"]
        assert output["assessment"]["primary_issue"] == "late_delivery_seller"
        assert output["assessment"]["case_status"] == "action_required"
        called_tools = [call["tool"] for call in gateway.call_history]
        assert "get_product_context" in called_tools
        assert "get_shipment_summary" in called_tools
        assert "get_order_payments" not in called_tools
        assert "get_payment_timeline" not in called_tools
        assert "get_refund_timeline" not in called_tools
        claim_refs = output["claim_assessments"][0]["evidence_refs"]
        assert "ev_get_product_context_CASE_LATE_01_1234567890abcdef" in claim_refs
        assert "ev_get_shipment_summary_CASE_LATE_01_1234567890abcdef" in claim_refs

    asyncio.run(_test())


def test_claim_routing_uses_minimum_sufficient_tools(tmp_path: Path) -> None:
    async def _test():
        root = Path(__file__).resolve().parents[1]
        contracts = Contracts(root / "contracts" / "schemas")
        expectations = {
            "late_delivery_logistics": ({"get_shipment_summary"}, {"get_order_payments"}),
            "late_delivery_seller": ({"get_shipment_summary"}, {"get_order_payments"}),
            "canceled_order_paid": ({"get_order_payments"}, {"get_shipment_summary"}),
            "unavailable_order_paid": ({"get_order_payments"}, {"get_shipment_summary"}),
            "duplicate_charge": (
                {"get_order_payments", "get_payment_timeline"},
                {"get_refund_timeline", "get_shipment_summary"},
            ),
            "payment_mismatch": (
                {"get_order_payments", "get_payment_timeline"},
                {"get_refund_timeline", "get_shipment_summary"},
            ),
            "valid_split_payment": (
                {"get_order_payments", "get_payment_timeline"},
                {"get_refund_timeline", "get_shipment_summary"},
            ),
            "refund_pending": (
                {"get_order_payments", "get_refund_timeline"},
                {"get_payment_timeline", "get_shipment_summary"},
            ),
            "refund_failed": (
                {"get_order_payments", "get_refund_timeline"},
                {"get_payment_timeline", "get_shipment_summary"},
            ),
            "unsupported_claim": (
                {"get_order_payments", "get_shipment_summary"},
                {"get_payment_timeline", "get_refund_timeline"},
            ),
        }

        for index, (topic, (required, forbidden)) in enumerate(expectations.items(), 1):
            case_id = f"CASE_ROUTE_{index:02d}"
            trace = TraceWriter(tmp_path / f"trace-{index}.jsonl", contracts)
            gateway = MockEvidenceGateway(contracts)
            case = {
                "case_id": case_id,
                "order_id": "ORD_001",
                "claims": [{"claim_id": f"claim-{index}", "topic": topic}],
                "investigation_scope": {"include_product_context": True},
            }

            await solve_case(case, gateway, trace)
            called = {call["tool"] for call in gateway.call_history}
            assert {"get_order", "get_order_items", "get_product_context", "get_policy"} <= called
            assert required <= called
            assert forbidden.isdisjoint(called)
            assert len(gateway.call_history) <= 7

    asyncio.run(_test())
