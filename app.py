import os

import aws_cdk as cdk
from aws_cdk import Validations
from cdk_nag import AwsSolutionsChecks

from demo_serverless.config import (
    DEFAULT_RESERVED_CONCURRENCY,
    reserved_concurrency_from,
)
from demo_serverless.demo_serverless_stack import DemoServerlessStack

app = cdk.App()

# Account and region come from CDK context (for example ~/.cdk.json or -c flags),
# falling back to the deployer's own AWS credentials and CLI configuration.
account = app.node.try_get_context("account") or os.getenv("CDK_DEFAULT_ACCOUNT")
region = app.node.try_get_context("region") or os.getenv("CDK_DEFAULT_REGION")

concurrency_context = app.node.try_get_context("reserved_concurrency")

DemoServerlessStack(
    app,
    "DemoServerlessStack",
    env=cdk.Environment(account=account, region=region),
    reserved_concurrency=(
        DEFAULT_RESERVED_CONCURRENCY
        if concurrency_context is None
        else reserved_concurrency_from(concurrency_context)
    ),
)

# cdk-nag (AWS Solutions rules): a finding that isn't acknowledged fails `cdk synth`.
Validations.of(app).add_plugins(AwsSolutionsChecks(app, verbose=True))

app.synth()
