# Everything you need to check, deploy, try and remove the demo. Run `make` for this list.
#
# Optional variables (none has a default that points at anyone's setup):
#   PROFILE=name                AWS CLI profile to use      (default: your usual credentials)
#   REGION=us-east-1            region to deploy into       (default: AWS_REGION, then your CLI config)
#   RESERVED_CONCURRENCY=0      Lambda reserved concurrency (default: 2 per function; 0 = none, for
#                               accounts with a very small Lambda quota)
#   BASELINE=path               teardown baseline file      (default: a file in your home directory,
#                               outside the repository: it contains account IDs)

SHELL := bash
.DEFAULT_GOAL := help

PROFILE ?=
REGION ?=
RESERVED_CONCURRENCY ?=
BASELINE ?= $(HOME)/.demo-serverless-baseline.txt

STACK := DemoServerlessStack
PY := .venv/bin/python

# Dev tools that aren't project dependencies. Always the pinned version, run through uv
# without installing it, whatever is installed on this machine: CI runs this same Makefile, so
# the two can't disagree because of a version. Override only to experiment, e.g.
# `make check RUFF=ruff`.
RUFF_VERSION := 0.16.10
CHECKOV_VERSION := 3.3.22
RUFF := uvx ruff@$(RUFF_VERSION)
CHECKOV := uvx checkov@$(CHECKOV_VERSION)

PROFILE_ARG := $(if $(PROFILE),--profile $(PROFILE))
CONCURRENCY_ARG := $(if $(RESERVED_CONCURRENCY),-c reserved_concurrency=$(RESERVED_CONCURRENCY))

# Sets $$account and $$region (shell variables) from the credentials you are using, the
# same way a deploy will, so what is printed is what is used. Stops if either is unknown.
define RESOLVE
account=$$(aws $(PROFILE_ARG) sts get-caller-identity --query Account --output text) \
  || { echo "Could not read your AWS credentials. See 'Choosing an account' in the README."; exit 1; }; \
region="$(REGION)"; \
[ -n "$$region" ] || region="$$AWS_REGION"; \
[ -n "$$region" ] || region="$$AWS_DEFAULT_REGION"; \
[ -n "$$region" ] || region=$$(aws $(PROFILE_ARG) configure get region); \
[ -n "$$region" ] || { echo "No region found. Pass REGION=... or set a default region."; exit 1; }
endef

# Prints the target and requires the account ID to be typed back, so a deploy can't
# land in the wrong account because of a stale login.
define CONFIRM
echo "About to $(1) stack $(STACK):"; \
echo "  account: $$account"; \
echo "  region:  $$region"; \
printf "Type the account ID to confirm: "; read answer; \
[ "$$answer" = "$$account" ] || { echo "Not confirmed. Nothing was changed."; exit 1; }
endef

CDK_TARGET = -c account=$$account -c region=$$region $(CONCURRENCY_ARG)

.PHONY: help install check bootstrap baseline deploy smoke access-log-check destroy teardown-check

help: ## Show this list
	@grep -E '^[a-z-]+:.*##' $(MAKEFILE_LIST) | sed -E 's/:.*## /\t/' | sort

$(PY):
	uv venv --python 3.14 .venv

install: $(PY) ## Create the Python 3.14 virtualenv and install the dependencies
	uv pip install --python .venv -r requirements.txt -r requirements-dev.txt

check: ## Lint, type-check, test, synthesize, scan with checkov, and look for account IDs (offline)
	$(RUFF) check .
	$(RUFF) format --check .
	$(PY) -m mypy .
	$(PY) -m pytest -q
	cdk synth -q
	$(CHECKOV) -d cdk.out --compact --quiet
	@if git grep --untracked -nE '\b[0-9]{12}\b' -- . ':!cdk.out'; then \
	  echo "Found what looks like an AWS account ID (above). Remove it."; exit 1; \
	else echo "No 12-digit account IDs found."; fi

bootstrap: ## One-time CDK setup of the account and region (asks you to confirm)
	@set -e; $(RESOLVE); $(call CONFIRM,bootstrap); \
	cdk bootstrap $(PROFILE_ARG) aws://$$account/$$region

baseline: ## Record the resources already tagged project=demo-serverless (run before the first deploy)
	@set -e; $(RESOLVE); \
	echo "Baseline for account $$account, region $$region (read-only)"; \
	$(PY) scripts/teardown_check.py --write-baseline "$(BASELINE)" $(PROFILE_ARG) --region $$region

deploy: ## Show the changes, confirm the account and region, then deploy
	@set -e; $(RESOLVE); \
	cdk diff $(PROFILE_ARG) $(CDK_TARGET); \
	$(call CONFIRM,deploy); \
	cdk deploy $(PROFILE_ARG) $(CDK_TARGET)

smoke: ## Test the deployed stack end to end, then check its access logs arrive
	@set -e; $(RESOLVE); \
	site=$$(aws $(PROFILE_ARG) --region $$region cloudformation describe-stacks --stack-name $(STACK) \
	  --query "Stacks[0].Outputs[?OutputKey=='SiteUrl'].OutputValue" --output text); \
	{ [ -n "$$site" ] && [ "$$site" != None ]; } || { echo "$(STACK) isn't deployed in $$region."; exit 1; }; \
	echo "Testing $$site"; \
	failed=0; \
	$(PY) scripts/smoke_test.py "$$site" || failed=1; \
	echo; echo "Waiting for the API's access logs to appear (up to 2 minutes)..."; \
	$(PY) scripts/access_log_check.py --stack-name $(STACK) $(PROFILE_ARG) --region $$region || failed=1; \
	exit $$failed

access-log-check: ## Check that the API's access logs are being delivered (make smoke does this too)
	@set -e; $(RESOLVE); \
	$(PY) scripts/access_log_check.py --stack-name $(STACK) $(PROFILE_ARG) --region $$region

destroy: ## Confirm the account and region, then delete the stack and everything in it
	@set -e; $(RESOLVE); $(call CONFIRM,destroy); \
	cdk destroy $(PROFILE_ARG) $(CDK_TARGET)

teardown-check: ## After destroy: fail if anything is left behind (compares with the baseline)
	@set -e; $(RESOLVE); \
	echo "Checking account $$account, region $$region (read-only)"; \
	$(PY) scripts/teardown_check.py --baseline "$(BASELINE)" $(PROFILE_ARG) --region $$region
