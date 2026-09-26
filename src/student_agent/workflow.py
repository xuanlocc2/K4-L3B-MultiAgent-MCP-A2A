"""Deterministic L3B multi-agent workflow.

Each "agent" is an async function with a fixed tool allowlist. The coordinator assigns tasks,
specialists call MCP once per tool, the policy agent maps the detected issue to EC policy,
and the verifier enforces cross-field invariants before the output is returned.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from . import OUTPUT_SCHEMA_VERSION
from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter

PERMISSIONS = {
    "entity-agent": {"get_customer_history", "get_order"},
    "order-agent": {"get_order_items", "get_product_context"},
    "shipment-agent": {"get_shipment_summary"},
    "payment-agent": {"get_payment_timeline", "get_refund_timeline"},
    "policy-agent": {"get_policy"},
}
REFUNDED_STATUSES = {"completed", "succeeded", "refunded", "confirmed"}
# Domains cited in evidence_refs per issue, on top of customer/order/policy/product.
ISSUE_DOMAINS = {
    "late_delivery_seller": {"shipment", "item"},
    "late_delivery_logistics": {"shipment", "item"},
    "canceled_order_paid": {"payment"},
    "unavailable_order_paid": {"payment"},
    "valid_split_payment": {"payment", "item"},
    "payment_mismatch": {"payment", "item"},
    "duplicate_charge": {"payment", "item"},
    "refund_pending": {"refund", "payment"},
    "refund_failed": {"refund", "payment"},
    "unsupported_claim": {"shipment", "payment"},
    "insufficient_evidence": set(),
}
REFUND_ISSUES = {"refund_pending", "refund_failed"}


def _ts(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _money(value: Any) -> float:
    return round(float(value or 0), 2)


class CaseContext:
    """Per-case state: call cache, consumed evidence, trace helper. Never shared across cases."""

    def __init__(self, case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter) -> None:
        self.case = case
        self.case_id = case["case_id"]
        self.gateway = gateway
        self.trace = trace
        self.cache: dict[tuple, dict[str, Any] | None] = {}
        self.refs: dict[str, list[str]] = {}

    def emit(self, event_type: str, actor: str, **kwargs: Any) -> None:
        self.trace.emit(case_id=self.case_id, event_type=event_type, actor=actor, **kwargs)

    async def call(self, actor: str, tool: str, **args: str) -> dict[str, Any] | None:
        if tool not in PERMISSIONS[actor]:
            raise PermissionError(f"{actor} may not call {tool}")
        key = (tool, tuple(sorted(args.items())))
        if key not in self.cache:
            # ponytail: no retry; a tool error means "no rows" (e.g. no refund timeline).
            try:
                evidence = await self.gateway.call(tool, case_id=self.case_id, **args)
            except RuntimeError:
                evidence = None
            self.cache[key] = evidence
            if evidence is not None:
                self.refs.setdefault(evidence["domain"], []).append(evidence["evidence_ref"])
                self.emit(
                    "tool_result_consumed", actor, tool_name=tool,
                    evidence_refs=[evidence["evidence_ref"]],
                    attributes={"domain": evidence["domain"]},
                )
            else:
                self.emit(
                    "tool_result_consumed", actor, tool_name=tool,
                    decision_code="TOOL_ERROR_NO_DATA",
                )
        evidence = self.cache[key]
        return None if evidence is None else evidence["data"]


def _owner(rows: list[dict[str, Any]], moment: datetime | None) -> dict[str, Any] | None:
    """Row (timeline version) whose purchase timestamp is the latest one <= moment."""
    if moment is None:
        return None
    owned = [row for row in rows if _ts(row["order_purchase_timestamp"]) <= moment]
    return max(owned, key=lambda row: _ts(row["order_purchase_timestamp"]), default=None)


def _version(rows: list[dict[str, Any]], opened: datetime) -> dict[str, Any] | None:
    """Authoritative version: latest purchase <= opened among versions whose estimated delivery
    had already passed when the case opened (a version still in flight is the stale copy)."""
    placed = [r for r in rows if _ts(r["order_purchase_timestamp"]) <= opened]
    due = [r for r in placed if (_ts(r["order_estimated_delivery_date"]) or opened) <= opened]
    return max(due or placed, key=lambda r: _ts(r["order_purchase_timestamp"]), default=None)


# --- specialists -------------------------------------------------------------------------


async def entity_agent(ctx: CaseContext) -> dict[str, Any]:
    case = ctx.case
    hint = case["customer_unique_id_hint"]
    history = await ctx.call("entity-agent", "get_customer_history", customer_unique_id=hint)
    if history is None:
        # customer history always exists; failing here means the competition run is gone
        raise RuntimeError("MCP run unavailable (get_customer_history failed)")
    history_ids = {row["order_id"] for row in history.get("orders", [])}
    candidates = case["candidate_order_ids"]
    claimed = case["customer_request"].get("claimed_order_id")
    matches = [oid for oid in candidates if oid in history_ids]
    if claimed in matches:
        matches = [claimed]
    order_id = matches[0] if len(matches) == 1 else None
    order = await ctx.call("entity-agent", "get_order", order_id=order_id) if order_id else None
    rows = [row for row in history.get("orders", []) if row["order_id"] == order_id]
    true_row = _version(rows, _ts(case["opened_at"])) if rows else None
    # identical purchase timestamps: versions can't be told apart, events can't be scoped
    tied = true_row is not None and sum(
        r["order_purchase_timestamp"] == true_row["order_purchase_timestamp"] for r in rows) > 1
    status = "resolved" if order_id else ("ambiguous" if len(matches) > 1 else "not_found")
    result = {
        "status": status,
        "order_id": order_id,
        "rejected": [oid for oid in candidates if oid != order_id],
        "customer_unique_id": history.get("customer_unique_id", hint) if history else None,
        "related_order_ids": sorted(history_ids),
        "rows": rows,
        "true_row": true_row,
        "tied": tied,
        "order_row": order,
    }
    ctx.emit(
        "handoff", "entity-agent", target="coordinator", decision_code=f"ENTITY_{status.upper()}",
        attributes={"timeline_versions": len(rows)},
    )
    return result


async def order_agent(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    oid = entity["order_id"]
    items = await ctx.call("order-agent", "get_order_items", order_id=oid) or []
    await ctx.call("order-agent", "get_product_context", order_id=oid)
    rows, true_row = entity["rows"], entity["true_row"]
    mine = [it for it in items if _owner(rows, _ts(it["shipping_limit_date"])) is true_row]
    result = {
        "items": mine,
        "total": round(sum(_money(it["price"]) + _money(it["freight_value"]) for it in mine), 2),
    }
    ctx.emit("handoff", "order-agent", target="coordinator", decision_code="ITEMS_SCOPED",
             attributes={"items": len(mine)})
    return result


def shipment_verdict(entity: dict[str, Any], order: dict[str, Any]) -> dict:
    """From the authoritative history version; the summary tool only adds citable evidence."""
    row = entity["true_row"]
    carrier = _ts(row["order_delivered_carrier_date"])
    delivered = _ts(row["order_delivered_customer_date"])
    estimated = _ts(row["order_estimated_delivery_date"])
    late_sellers = sorted({
        it["seller_id"] for it in order["items"]
        if carrier and carrier > _ts(it["shipping_limit_date"])
    })
    if delivered is None or estimated is None:
        verdict = "insufficient_evidence"
    elif delivered <= estimated:
        verdict = "on_time"
    elif late_sellers:
        verdict = "seller_delay"
    else:
        verdict = "logistics_delay"
    result = {
        "verdict": verdict,
        "late_seller_ids": late_sellers if verdict == "seller_delay" else [],
        "timeline_complete": all(
            row.get(k) for k in ("order_purchase_timestamp", "order_delivered_carrier_date",
                                 "order_delivered_customer_date", "order_estimated_delivery_date")
        ),
    }
    return result


async def payment_agent(ctx: CaseContext, entity: dict[str, Any], refunds: bool) -> dict:
    oid, rows, true_row = entity["order_id"], entity["rows"], entity["true_row"]
    timeline = await ctx.call("payment-agent", "get_payment_timeline", order_id=oid) or {}
    refund_data = (
        await ctx.call("payment-agent", "get_refund_timeline", order_id=oid) or {}
    ) if refunds else {}

    def mine(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return [e for e in events if _owner(rows, _ts(e["event_at"])) is true_row]

    events = mine(timeline.get("events", []))
    # a refund belongs to the version that captured that amount (it can be dated after the
    # next version was placed); fall back to time ownership when the amount is ambiguous
    capture_owners: dict[float, set[int]] = {}
    by_id = {id(r): r for r in rows}
    for e in timeline.get("events", []):
        if e["event_type"] == "captured":
            owner = _owner(rows, _ts(e["event_at"]))
            capture_owners.setdefault(_money(e["amount_brl"]), set()).add(id(owner))
    refund_events = []
    for e in refund_data.get("events", []):
        owners = capture_owners.get(_money(e["amount_brl"]), set())
        owner = by_id.get(next(iter(owners))) if len(owners) == 1 else _owner(rows, _ts(e["event_at"]))
        if owner is true_row:
            refund_events.append(e)
    captures = [_money(e["amount_brl"]) for e in events
                if e["event_type"] == "captured" and e.get("status") == "confirmed"]
    refunded = sum(_money(e["amount_brl"]) for e in refund_events
                   if e.get("status") in REFUNDED_STATUSES)
    result = {
        "captures": captures,
        "captured": round(sum(captures), 2),
        "refunded": round(refunded, 2),
        "mismatch": any(e["event_type"] == "reconciliation_mismatch" for e in events),
        "refund_statuses": {e.get("status") for e in refund_events},
    }
    ctx.emit("handoff", "payment-agent", target="coordinator", decision_code="PAYMENTS_SCOPED",
             attributes={"captures": len(captures), "refund_events": len(refund_events)})
    return result


# --- decision ----------------------------------------------------------------------------


def detect_issue(entity: dict, order: dict, shipment: dict, payment: dict) -> str:
    status = entity["true_row"]["order_status"]
    if status == "canceled" and payment["captured"] > 0:
        return "canceled_order_paid"
    if status == "unavailable" and payment["captured"] > 0:
        return "unavailable_order_paid"
    if "failed" in payment["refund_statuses"]:
        return "refund_failed"
    if "pending" in payment["refund_statuses"]:
        return "refund_pending"
    if payment["mismatch"]:
        return "payment_mismatch"
    if len(payment["captures"]) > 1 and payment["captured"] > order["total"] + 0.01:
        return "duplicate_charge"
    if shipment["verdict"] == "seller_delay":
        return "late_delivery_seller"
    if shipment["verdict"] == "logistics_delay":
        return "late_delivery_logistics"
    if len(payment["captures"]) > 1 and abs(payment["captured"] - order["total"]) <= 0.01:
        return "valid_split_payment"
    return "unsupported_claim"


PAYMENT_VERDICT = {
    "payment_mismatch": "capture_mismatch",
    "duplicate_charge": "duplicate_capture",
    "refund_pending": "refund_pending",
    "refund_failed": "refund_failed",
}


async def policy_agent(ctx: CaseContext, issue: str) -> dict[str, Any]:
    policy = await ctx.call(
        "policy-agent", "get_policy", policy_version=ctx.case["policy_version"]
    ) or {}
    rule = policy.get("rules", {}).get(issue)
    ctx.emit("policy_decided", "policy-agent", target="coordinator",
             decision_code=f"POLICY_{issue.upper()}",
             attributes={"policy_version": ctx.case["policy_version"], "rule_found": bool(rule)})
    return rule or {}


def build_output(ctx, entity, order, shipment, payment, issue, rule) -> dict[str, Any]:
    case, oid = ctx.case, entity["order_id"]
    topic = case["customer_request"]["claims"][0]["topic"]
    refund = _money(rule.get("refund_brl")) if rule else 0.0
    status = rule.get("case_status", "needs_investigation")
    action = rule.get("recommended_action")
    sellers = sorted({it["seller_id"] for it in order["items"]})
    parties = []
    for party in rule.get("responsible_parties", []) or [{"party_type": "unknown", "party_id": None}]:
        if party["party_type"] == "seller":
            # Policy seller ids are templates; bind to the seller actually responsible.
            for sid in shipment["late_seller_ids"] or sellers or [None]:
                parties.append({"party_type": "seller", "party_id": sid})
        else:
            parties.append({"party_type": party["party_type"], "party_id": party.get("party_id")})

    domains = {"customer", "order", "policy", "product"} | ISSUE_DOMAINS[issue]
    refs = [ref for d in sorted(domains) for ref in ctx.refs.get(d, [])]

    conflicts = []
    order_row = entity["order_row"]
    if order_row and order_row.get("order_purchase_timestamp") != entity["true_row"].get(
        "order_purchase_timestamp"
    ):
        conflicts.append({
            "field": "order_timeline",
            "sources": ["get_order", "get_customer_history"],
            "selected_source": "get_customer_history",
            "resolution_code": "LATEST_VERSION_BEFORE_CASE_OPENED",
        })
    elif len(entity["rows"]) > 1:
        conflicts.append({
            "field": "order_timeline",
            "sources": ["get_customer_history", "get_order"],
            "selected_source": "get_order",
            "resolution_code": "LATEST_VERSION_BEFORE_CASE_OPENED",
        })

    claims = []
    for claim in case["customer_request"]["claims"]:
        if claim["topic"] == "requested_full_refund":
            verdict = "partially_supported" if refund > 0 else "unsupported"
        elif claim["topic"] == "unsupported_claim":
            verdict = "unsupported" if issue == "unsupported_claim" else "supported"
        else:
            verdict = "supported" if claim["topic"] == issue else "unsupported"
        claims.append({"claim_id": claim["claim_id"], "verdict": verdict,
                       "confidence": 0.9 if topic == issue else 0.6, "evidence_refs": refs[:30]})

    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {
            "primary_issue": issue,
            "secondary_issues": [],
            "case_status": status,
            "confidence": 0.9 if topic == issue else 0.6,
        },
        "affected_entities": {
            "order_ids": [oid],
            "item_ids": sorted({it["order_item_id"] for it in order["items"]}),
            "seller_ids": sellers,
            "payment_references": [],
            "shipment_ids": [],
        },
        "claim_assessments": claims,
        "entity_resolution": {
            "status": entity["status"],
            "resolved_order_ids": [oid],
            "rejected_candidates": entity["rejected"],
            "confidence": 0.95,
        },
        "customer_context": {
            "customer_unique_id": entity["customer_unique_id"],
            "related_order_ids": entity["related_order_ids"],
        },
        "shipment_analysis": shipment,
        "payment_analysis": {
            # ponytail: canceled/unavailable-paid map to "reconciled" (capture itself is valid).
            "verdict": PAYMENT_VERDICT.get(issue, "reconciled"),
            "captured_total_brl": payment["captured"],
            "refunded_total_brl": payment["refunded"],
            "refundable_total_brl": round(max(payment["captured"] - payment["refunded"], 0), 2),
        },
        "root_cause_analysis": {
            "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
            "responsible_parties": parties[:5],
        },
        "evidence_refs": refs[:30],
        "data_conflicts": conflicts,
        "financial_resolution": {
            "currency": "BRL",
            "recommended_refund_brl": refund,
            "refund_lines": [{"reason_code": action, "amount_brl": refund, "entity_id": oid}]
            if refund > 0 else [],
        },
        "resolution_actions": [action] if action else [],
    }


def insufficient_output(ctx: CaseContext, entity: dict[str, Any]) -> dict[str, Any]:
    refs = [ref for refs in ctx.refs.values() for ref in refs][:30]
    return {
        "schema_version": OUTPUT_SCHEMA_VERSION,
        "case_id": ctx.case_id,
        "assessment": {"primary_issue": "insufficient_evidence", "secondary_issues": [],
                       "case_status": "needs_investigation", "confidence": 0.5},
        "affected_entities": {"order_ids": [], "item_ids": [], "seller_ids": [],
                              "payment_references": [], "shipment_ids": []},
        "entity_resolution": {"status": entity["status"], "resolved_order_ids": [],
                              "rejected_candidates": entity["rejected"], "confidence": 0.5},
        "customer_context": {"customer_unique_id": entity["customer_unique_id"],
                             "related_order_ids": entity["related_order_ids"]},
        "shipment_analysis": {"verdict": "insufficient_evidence", "late_seller_ids": [],
                              "timeline_complete": False},
        "payment_analysis": {"verdict": "insufficient_evidence", "captured_total_brl": None,
                             "refunded_total_brl": None, "refundable_total_brl": None},
        "root_cause_analysis": {"ranked_causes": [], "responsible_parties": []},
        "evidence_refs": refs,
        "data_conflicts": [],
        "financial_resolution": {"currency": "BRL", "recommended_refund_brl": 0.0,
                                 "refund_lines": []},
        "resolution_actions": ["escalate_for_investigation"],
    }


def verify(ctx: CaseContext, output: dict[str, Any]) -> list[str]:
    """Cross-field invariants. Returns failed check codes (empty = pass)."""
    failed = []
    fin = output["financial_resolution"]
    status = output["assessment"]["case_status"]
    if abs(sum(line["amount_brl"] for line in fin["refund_lines"])
           - fin["recommended_refund_brl"]) > 0.01:
        failed.append("REFUND_LINES_SUM")
    if status == "no_action" and fin["recommended_refund_brl"] > 0:
        failed.append("NO_ACTION_WITH_REFUND")
    if status == "action_required" and not output["resolution_actions"]:
        failed.append("ACTION_REQUIRED_WITHOUT_ACTION")
    issue = output["assessment"]["primary_issue"]
    parties = output["root_cause_analysis"]["responsible_parties"]
    if issue == "late_delivery_seller":
        late = set(output["shipment_analysis"]["late_seller_ids"])
        if not late or not all(p["party_type"] == "seller" and p["party_id"] in late
                               for p in parties):
            failed.append("SELLER_RESPONSIBILITY")
    if issue == "late_delivery_logistics" and any(p["party_type"] == "seller" for p in parties):
        failed.append("LOGISTICS_RESPONSIBILITY")
    consumed = {ref for refs in ctx.refs.values() for ref in refs}
    if not set(output["evidence_refs"]) <= consumed:
        failed.append("UNKNOWN_EVIDENCE_REF")
    er = output["entity_resolution"]
    if set(er["resolved_order_ids"]) & set(er["rejected_candidates"]):
        failed.append("ENTITY_OVERLAP")
    return failed


# --- coordinator -------------------------------------------------------------------------


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    ctx = CaseContext(case, gateway, trace)
    ctx.emit("task_assigned", "coordinator", target="entity-agent", decision_code="RESOLVE_ENTITY")
    entity = await entity_agent(ctx)

    if entity["status"] != "resolved" or entity["true_row"] is None:
        output = insufficient_output(ctx, entity)
    else:
        for agent in ("order-agent", "payment-agent"):
            ctx.emit("task_assigned", "coordinator", target=agent, decision_code="INVESTIGATE")
        order, payment = await asyncio.gather(
            order_agent(ctx, entity), payment_agent(ctx, entity, refunds=False))
        shipment = shipment_verdict(entity, order)

        topic = case["customer_request"]["claims"][0]["topic"]
        # tied versions mix both timelines' events, so the claim topic is the only tie-breaker
        tied = entity["tied"] and topic in ISSUE_DOMAINS
        issue = topic if tied else detect_issue(entity, order, shipment, payment)
        # Refund events only change the verdict when the cheaper evidence did not already
        # confirm the claimed topic; skipping the call saves budget on most cases.
        if issue != topic or topic in REFUND_ISSUES:
            ctx.emit("task_assigned", "coordinator", target="payment-agent",
                     decision_code="CHECK_REFUNDS")
            payment = await payment_agent(ctx, entity, refunds=True)
            if not tied:
                issue = detect_issue(entity, order, shipment, payment)
        if "shipment" in ISSUE_DOMAINS[issue]:
            ctx.emit("task_assigned", "coordinator", target="shipment-agent",
                     decision_code="COLLECT_SHIPMENT_EVIDENCE")
            await ctx.call("shipment-agent", "get_shipment_summary", order_id=entity["order_id"])
            ctx.emit("handoff", "shipment-agent", target="coordinator",
                     decision_code=f"SHIPMENT_{shipment['verdict'].upper()}")
        ctx.emit("task_assigned", "coordinator", target="policy-agent", decision_code="APPLY_POLICY")
        rule = await policy_agent(ctx, issue)
        output = build_output(ctx, entity, order, shipment, payment, issue, rule)

    ctx.emit("handoff", "coordinator", target="verifier", decision_code="VERIFY_OUTPUT")
    failed = verify(ctx, output)
    if failed:
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.5)
    ctx.emit(
        "verification_completed", "verifier", target="coordinator",
        decision_code="PASS" if not failed else "FAIL_" + "_".join(failed)[:70],
        evidence_refs=output["evidence_refs"][:20],
    )
    return output
