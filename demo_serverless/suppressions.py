"""Every cdk-nag acknowledgement and checkov skip for the stack, each with its reason.

Nothing is suppressed in bulk: each entry names one rule on the specific constructs
that trigger it. (cdk-nag acknowledgements and checkov skips are two separate mechanisms.)
"""

from collections.abc import Iterable

import aws_cdk as cdk
from aws_cdk import Validations
from constructs import IConstruct


def acknowledge(constructs: Iterable[IConstruct], rule_id: str, reason: str) -> None:
    """cdk-nag v3 suppression: acknowledge one finding on each given construct."""
    for construct in constructs:
        Validations.of(construct).acknowledge(
            cdk.Acknowledgment(id=rule_id, reason=reason)
        )


def cfn_of(construct: IConstruct) -> cdk.CfnResource:
    """The CloudFormation resource behind a construct (or the construct itself if it is one)."""
    cfn = (
        construct
        if isinstance(construct, cdk.CfnResource)
        else construct.node.default_child
    )
    assert isinstance(cfn, cdk.CfnResource)
    return cfn


def skip_checkov(resource: IConstruct, skips: dict[str, str]) -> None:
    """checkov suppression: a ``Metadata.checkov.skip`` list on the CloudFormation resource."""
    cfn_of(resource).add_metadata(
        "checkov",
        {"skip": [{"id": rule, "comment": comment} for rule, comment in skips.items()]},
    )
