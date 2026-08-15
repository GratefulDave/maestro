"""One-way projection from a validated plan into semantic review objects."""

from __future__ import annotations

from typing import List, Tuple

from .finalization import ObjectKind, ReviewObject
from .plan_model import AgentNode, Plan


def review_objects(plan: Plan) -> Tuple[ReviewObject, ...]:
    """Return every reviewable object in canonical plan order.

    Digest computation remains the caller's responsibility. This module does
    not canonicalize or hash, and finalization remains independent of the plan
    representation.
    """
    objects: List[ReviewObject] = [ReviewObject("plan", ObjectKind.PLAN)]
    for node in plan.nodes:
        objects.append(ReviewObject("node:{}".format(node.node_id),
                                    ObjectKind.NODE))
        if isinstance(node, AgentNode):
            objects.append(ReviewObject("node:{}#gate".format(node.node_id),
                                        ObjectKind.GATE))
    objects.append(ReviewObject("plan#integration-gate", ObjectKind.GATE))
    objects.extend(ReviewObject("evidence:{}".format(item.evidence_id),
                                ObjectKind.EVIDENCE)
                   for item in plan.evidence)
    return tuple(objects)
