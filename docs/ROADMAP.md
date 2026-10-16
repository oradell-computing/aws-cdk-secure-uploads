# Roadmap: text-fingerprint demo

Goal: a small, secure serverless demo that a third party deploys into their own AWS account using only the README, and removes completely with one command.

## Steps

- [x] **0. Prerequisites:** commit emails use the GitHub noreply address; CDK is bootstrapped in the development sandbox account.
- [x] **1. Definition:** "run by a third party" means deployed into their own AWS account, using concise README instructions.
- [x] **2. Portability:** account and region come from CDK context or the deployer's credentials (`CDK_DEFAULT_ACCOUNT` / `CDK_DEFAULT_REGION`); any personal pin lives only in the user-level `~/.cdk.json`, outside the repo; no context lookups. *Done when:* `git grep -nE '\b[0-9]{12}\b'` finds nothing, and `cdk synth` works both with the personal pin and with a placeholder account.
- [x] **3. Architecture:** page served by the API → short-lived presigned POST → private S3 bucket (`uploads/`) → S3 event → processing Lambda → results (`results/`) → fetched by the page through the API. Decide what the processing does before building.
- [x] **4. Security baseline:** cdk-nag (AwsSolutions) and checkov pass, with every suppression justified in a comment; least privilege through explicit per-function statements (the app's functions get no `s3:ListBucket` and write logs only to their own log group; the one exception, CDK's auto-delete helper, is justified in the suppressions); S3 blocks public access, is encrypted, and requires HTTPS; uploads are size- and type-limited, links expire in minutes, and objects auto-delete after 1 day; API throttling and Lambda reserved concurrency; input validation and output escaping; no secrets; log retention.
- [x] **5. Teardown:** destroy-safe removal policies; every resource tagged `project=demo-serverless`. *Done when:* a tag query after `cdk destroy` returns nothing.
- [x] **6. Tests and CI:** unit tests assert the security properties; a post-deploy smoke test; GitHub Actions runs every offline check without AWS credentials.
- [x] **7. Runnable by someone else:** README (prerequisites; Makefile targets for install, deploy with an account confirmation, smoke, destroy; expected cost; troubleshooting), an architecture diagram, and a LICENSE.
- [ ] **8. Publish:** create a new, unrelated public repo from one clean commit of the current files (never a fork, branch or visibility change of the development repo); scan it before and after pushing (manual review, gitleaks, `git grep` for account IDs, emails, ARNs), the second time from a fresh clone; then delete the private development repo.

## Rules throughout

- The demo never references resources in the maintainer's own AWS account.
- Work happens only in a sandbox account; deploys, destroys and bootstraps need the maintainer's approval.
