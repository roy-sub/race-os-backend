"""Turning plan rows into API objects.

Its own module because **two consumers must not diverge**: the JSON the app
renders and the PDF the athlete prints are the same plan, and a formatting
difference between them would look like a solver bug. Both call these
functions.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from raceos.api.schemas.plan import (
    AidActionOut,
    BagItemOut,
    BagOut,
    ConstraintRefOut,
    FuellingOut,
    GateOut,
    PlanDetail,
    PlanSummary,
    SegmentOut,
    SplitOut,
    format_hm,
)
from raceos.db.models import (
    Plan,
    PlanAidAction,
    PlanBag,
    PlanBagItem,
    PlanConstraintRef,
    PlanFuelling,
    PlanGate,
    PlanSegment,
    PlanSplit,
)


def plan_summary(plan: Plan) -> PlanSummary:
    out = PlanSummary.model_validate(plan)
    out.projected_label = format_hm(plan.projected_minutes)
    return out


def plan_detail(session: Session, plan: Plan) -> PlanDetail:
    """Read-your-own-writes: every child row is read back from the database.

    Not assembled from the solver's return value — a bug that dropped a child
    row on write would then be invisible until a user noticed.
    """
    detail = PlanDetail.model_validate(
        {**plan_summary(plan).model_dump(), "forecast_snapshot": plan.forecast_snapshot or {}}
    )

    detail.segments = [
        SegmentOut.model_validate(row)
        for row in session.scalars(
            select(PlanSegment).where(PlanSegment.plan_id == plan.id).order_by(PlanSegment.ordinal)
        )
    ]
    detail.splits = []
    for split_row in session.scalars(select(PlanSplit).where(PlanSplit.plan_id == plan.id)):
        split = SplitOut.model_validate(split_row)
        split.split_label = format_hm(split_row.split_minutes)
        detail.splits.append(split)
    # Legs in the fixed order the solver accumulates them.
    order = {"SWIM": 0, "BIKE": 1, "RUN": 2}
    detail.splits.sort(key=lambda s: order[s.leg.value])

    detail.gates = []
    for gate_row in session.scalars(
        select(PlanGate).where(PlanGate.plan_id == plan.id).order_by(PlanGate.limit_minutes)
    ):
        gate = GateOut.model_validate(gate_row)
        sign = "+" if gate_row.margin_minutes >= 0 else "-"
        gate.margin_label = f"{sign}{format_hm(abs(gate_row.margin_minutes))}"
        detail.gates.append(gate)

    fuelling = session.scalar(select(PlanFuelling).where(PlanFuelling.plan_id == plan.id))
    detail.fuelling = FuellingOut.model_validate(fuelling) if fuelling else None

    detail.aid_actions = [
        AidActionOut.model_validate(row)
        for row in session.scalars(
            select(PlanAidAction)
            .where(PlanAidAction.plan_id == plan.id)
            .order_by(PlanAidAction.ordinal)
        )
    ]

    detail.bags = []
    for bag_row in session.scalars(select(PlanBag).where(PlanBag.plan_id == plan.id)):
        bag = BagOut.model_validate(bag_row)
        bag.items = [
            BagItemOut.model_validate(item)
            for item in session.scalars(
                select(PlanBagItem)
                .where(PlanBagItem.bag_id == bag_row.id)
                .order_by(PlanBagItem.ordinal)
            )
        ]
        detail.bags.append(bag)
    from raceos.solver.tables.bag_rules import BAG_ORDER

    position = {key: index for index, key in enumerate(BAG_ORDER)}
    detail.bags.sort(key=lambda b: position[b.key])

    detail.constraint_refs = [
        ConstraintRefOut.model_validate(row)
        for row in session.scalars(
            select(PlanConstraintRef).where(PlanConstraintRef.plan_id == plan.id)
        )
    ]
    return detail
