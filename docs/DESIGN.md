# Design: text-fingerprint demo

The design of the demo and the reasoning behind it: the architecture (roadmap step 3), the
security baseline (step 4) and the tagging and removal parts of teardown (step 5). The main
choices are summarized in [Decisions](#decisions) at the end.

## What it does

A visitor opens the page, picks a `.txt` file of up to 100 KB, and sees its fingerprint:
SHA-256, word count, unique words, top 10 words, average sentence length, and reading time.

```
Browser ──GET /, /app.js, /app.css──▶ HTTP API ──▶ page fn
Browser ──POST /uploads──────────────▶ HTTP API ──▶ presign fn ──▶ {id, url, fields}
Browser ──multipart POST (presigned)─────────────────────────────▶ S3 uploads/<id>.txt
                                     S3 ObjectCreated (uploads/ only) ──▶ processor fn
                                                                   processor ──▶ S3 results/<id>.json
Browser ──GET /results/<id> (polls)──▶ HTTP API ──▶ results fn ──▶ S3 results/<id>.json
```

The bucket is never public. The browser touches S3 only through the presigned POST; results
go through the API.

## Resources

All resources live in one stack, `DemoServerlessStack`. `Tags.of(app).add("project", "demo-serverless")`
tags everything the Tags aspect can reach (see [CDK helper functions](#cdk-helper-functions) for the exceptions).

| Resource | Key settings |
|---|---|
| S3 bucket | CDK-generated name (no global name collisions for third parties); `BlockPublicAccess.BLOCK_ALL`; `BucketEncryption.S3_MANAGED`; `enforce_ssl=True`, `minimum_tls_version=1.2`; `ObjectOwnership.BUCKET_OWNER_ENFORCED` (ACLs off); no versioning; lifecycle: expire all objects after 1 day and abort incomplete multipart uploads after 1 day; CORS rule (below); `RemovalPolicy.DESTROY` + `auto_delete_objects=True` |
| S3 event notification | `OBJECT_CREATED` with `NotificationKeyFilter(prefix="uploads/", suffix=".txt")` → processor. The filter also guarantees the processor's own `results/` writes can never re-trigger it. |
| HTTP API (`apigatewayv2.HttpApi`) | `create_default_stage=False`, then an explicit `HttpStage` named `$default` with `auto_deploy=True`, `throttle=ThrottleSettings(10, 20)`, a `POST /uploads` route override of 1 req/s burst 2 (an L1 `RouteSettings` override, because the typed property emits keys CloudFormation rejects) and access logs pointing at their own log group. No API-level CORS (the page and API share an origin). |
| Routes | `GET /`, `GET /app.js`, `GET /app.css` → page fn; `POST /uploads` → presign fn; `GET /results/{id}` → results fn. No other routes. Unmatched paths get API Gateway's 404. |
| Lambda functions (4) | `Runtime.PYTHON_3_14`, `Architecture.ARM_64` (about 20% cheaper; the code is pure Python with no native wheels, so architecture doesn't matter otherwise), `Code.from_asset("functions")` (one shared asset, different handlers), 128 MB, timeout 5 s for the API functions and 30 s for the processor, `reserved_concurrent_executions` from the `reserved_concurrency` context value (default 2; 0 = none), no X-Ray. |
| Log groups (5, or 7 with the helpers) | One per function plus one for API access logs, each created explicitly with `RetentionDays.ONE_WEEK` and `RemovalPolicy.DESTROY`, and passed to the function with `log_group=`. Explicit is required: with this project's `useCdkManagedLogGroup` feature flag, CDK otherwise creates a log group with **731-day retention and `DeletionPolicy: Retain`** (verified by a probe synth), which would survive `cdk destroy`. Plus log groups for the two CDK helper functions (see below). |
| IAM roles | One per function, created by `lambda.Function`. Permissions are explicit statements, not `grant_*` (below). |

No VPC, NAT gateway, KMS key, WAF, database or load balancer, so the stack has **no hourly
costs**. See [Cost and teardown residue](#cost-and-teardown-residue).

### Lambda code layout

```
functions/              # the Lambda asset (not "lambda", which is a Python keyword)
  fingerprint.py        # pure function: bytes -> fingerprint dict. No boto3, no I/O.
  ids.py                # new_id() and is_valid_id(); shared by presign, processor, results
  responses.py          # JSON/HTML/text responses with the security headers
  page.py               # handler: serves static/ files
  presign.py            # handler: POST /uploads
  results.py            # handler: GET /results/{id}
  processor.py          # handler: S3 event
  static/index.html, app.css, app.js   # app.js: see decisions
```

Each handler is a thin wrapper: parse the event, call a pure function, shape the response.
boto3 clients are created at module level (reused across warm invocations) and passed into
small functions as parameters so tests can substitute a fake or a botocore `Stubber`.

## Identifiers and keys

- `new_id()` returns `secrets.token_hex(16)`: 32 lowercase hex characters, 128 bits of randomness,
  generated only on the server.
- `is_valid_id(s)` checks `len(s) == 32 and all(c in "0123456789abcdef" for c in s)`. No regex.
- Upload key: `uploads/<id>.txt`. Result key: `results/<id>.json`. Both are derived from the id
  by code, never taken from user input.

## Function behaviour

### page — `GET /`, `/app.js`, `/app.css`

Serves three files bundled in the asset from a fixed dict `{route path: (file, content type)}`,
so no user input selects a file path. It sends the CSP and security headers below on every response.

### presign — `POST /uploads`

The handler ignores the request body; there is no user input to validate. It returns
`{"id", "url", "fields"}` from `s3.generate_presigned_post`:

- `Key="uploads/<id>.txt"`, `ExpiresIn=300` (5 minutes).
- `Fields={"Content-Type": "text/plain"}`.
- `Conditions=[{"Content-Type": "text/plain"}, ["content-length-range", 1, 102400]]`. boto3 adds the
  exact `bucket` and `key` conditions itself. S3 rejects any form field that no condition
  covers, so the browser can't add tagging, ACL or metadata fields.
- The boto3 client is pinned to the bucket's region with `Config(signature_version="s3v4",
  s3={"addressing_style": "virtual"})`, so the URL is `https://<bucket>.s3.<region>.amazonaws.com/`.
  That is the bucket's **regional** domain name. The global `s3.amazonaws.com` endpoint can
  answer with a 307 redirect for buckets created in the last few hours, and a redirect breaks a
  CORS POST, typically during the first smoke test. A unit test asserts the URL's origin equals
  the `UPLOAD_ORIGIN` env var that the CSP and CORS rule use.

`Content-Type: text/plain` is only a label the browser sends; S3 can't inspect the bytes. The real
content check is the processor's strict decode. The size limit, by contrast, is enforced by S3.

### processor — S3 event

For each record:

1. `key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])` (event keys are URL-encoded).
2. Parse `uploads/<id>.txt` with `str.removeprefix`/`removesuffix` and `is_valid_id`. If the key
   doesn't match, log a warning and skip the record. The prefix/suffix filter makes this
   unreachable in practice, so it's a boundary check, not error handling.
3. `get_object(Range="bytes=0-102400")` reads at most 100 KB + 1 byte. If 102,401 bytes come back,
   write an error result (defence in depth; S3 already enforced the limit). If the object is
   already gone (`NoSuchKey`, for example a duplicate event after lifecycle expiry), log and skip.
4. Call `fingerprint(data: bytes) -> dict`, the pure function.
5. `put_object(Key="results/<id>.json", ContentType="application/json")`.

**Idempotency:** the output key is a function of the input key, and the output is a pure function
of the bytes, so a duplicate event overwrites the result with identical content. No state, locks or
conditional writes are needed.

**Failures:** expected bad input (not UTF-8, empty after decoding) produces
`{"status": "error", "error": "<message>"}`, so the page's polling ends with a clear message.
Unexpected exceptions are **not** caught. They fail the invocation, Lambda's async retries (2)
apply, and the traceback lands in the log group. There is no dead-letter queue (decided); the
page ends its polling with "processing failed or timed out".

### results — `GET /results/{id}`

1. If `is_valid_id(id)` fails, return 400 `{"error": "invalid id"}`. Nothing else reaches S3.
2. `get_object(Key="results/<id>.json")`. On `NoSuchKey` **or `AccessDenied`**, log the code and
   return **200** `{"status": "pending"}` (not 404: "not ready yet" is a normal answer while
   polling, so the browser console stays free of errors; the response is `no-store` so it is never
   cached). The function has no `s3:ListBucket`, so S3 answers a missing key with `AccessDenied`.
   The cost: a real permission bug would also look like "pending" and end as the page's
   "processing failed or timed out" message, so the log line is the way to tell. Any other S3
   error is not caught. The page keeps polling while the status is `pending` and treats any
   non-200 as a failure.
3. Otherwise return 200 with the stored JSON body and `Cache-Control: no-store`. The body is
   parsed with `json.loads` and re-serialized, so the API only ever emits well-formed JSON,
   never raw object bytes.

## Fingerprint definition

`fingerprint(data: bytes) -> dict` is a pure function. Agreed rules:

| Field | Definition |
|---|---|
| `sha256` | `hashlib.sha256(data).hexdigest()` of the **raw bytes**, so it matches `sha256sum file.txt`. |
| `bytes` | `len(data)` |
| decoding | `data.decode("utf-8-sig")` (strict; strips a BOM, which still counts in `sha256` and `bytes`). Invalid UTF-8 or a NUL character → `InvalidTextError`. |
| words | One pass over `casefold()` of the NFC-normalized text (so a decomposed accent stays attached). A word is a run of characters where `ch.isalnum()`; combining marks stay with their word (casefold can create them: `İ`); an apostrophe (`'` or `’`) is kept only between two alphanumerics and written as `'` (`don't` and `don’t` are the same word; `'quoted'` gives `quoted`). Unicode-aware, plain loops, no regex. |
| `word_count` / `unique_words` | `len(words)` / `len(set(words))` |
| `top_words` | Up to 10 `{"word", "count"}` entries sorted by count descending, then word ascending, so ties are deterministic. Stop words are included (raw counts). |
| `sentence_count` | A sentence ends at a `.`, `!` or `?` that follows at least one word (a run of them ends one sentence). A `.` between two digits (`3.14`) is not an end. Trailing words without a terminator count as one more sentence. `"Hi... there!"` gives 2. Abbreviations like "e.g." still over-count (`See e.g. this one.` gives 3); that's accepted for a demo and pinned by a test. |
| `avg_sentence_length` | `word_count / sentence_count` rounded to 1 decimal. |
| `reading_time_seconds` | `(word_count * 60 + 100) // 200`: 200 words per minute, halves rounded up with integer arithmetic. The page formats it as "1 min 30 s". |

**No words** (an empty file, a BOM only, whitespace or punctuation only) is rejected too, with
"No words were found in the file." The function is pure: no clock, no randomness, no AWS, so a
repeated S3 event writes byte-identical output.

Result documents (keys sorted, so identical input gives identical bytes):

- `{"status": "ok", "fingerprint": {...the fields above...}}`
- `{"status": "error", "error": "<message shown to the visitor>"}` for text that can't be
  fingerprinted (`InvalidTextError`, a `ValueError` subclass the processor catches) and for an
  object over 100 KB. Any other exception is **not** caught: the invocation fails, Lambda retries
  it twice, and the traceback is logged.

## IAM permissions per function

Explicit `PolicyStatement`s (via `add_to_role_policy`), **not** `grant_*`: `grant_read` adds
`s3:List*` and `s3:GetBucket*` on the whole bucket, and `grant_put` adds `PutObjectTagging`,
`PutObjectRetention` and friends. Each function gets exactly one action per prefix and nothing
at bucket level, and **no function has `s3:ListBucket`**.

**Logs:** each of the four functions has its own `iam.Role` with **no managed policy**.
`log_group.grant_write(role)` adds only `logs:CreateLogStream` and `logs:PutLogEvents` on that
function's own log group, and there is no `logs:CreateLogGroup`. The default
`AWSLambdaBasicExecutionRole` would allow `CreateLogGroup` on `*`, which lets Lambda recreate a
group we deleted. The log groups are created by CloudFormation before the functions exist, so the
functions never need that action.

| Function | Statements |
|---|---|
| page | none. No S3 access at all. |
| presign | `s3:PutObject` → `bucket/uploads/*`. A presigned POST is signed with the function's own credentials, so the role must be able to write where the browser will write. |
| processor | `s3:GetObject` → `bucket/uploads/*`; `s3:PutObject` → `bucket/results/*`. |
| results | `s3:GetObject` → `bucket/results/*`. |
| S3 → processor | `add_event_notification` adds a resource policy on the processor allowing `s3.amazonaws.com` with `SourceAccount` and `SourceArn` (this bucket). |

`tests/unit/test_demo_serverless_stack.py` asserts these exact (action, resource) sets per
function, and that no inline policy has `s3:List*`, `s3:GetBucket*`, `s3:*` or `Resource: *`.

## HTTP API vs REST API

| | HTTP API (recommended) | REST API |
|---|---|---|
| Price | ~$1.00 per million requests | ~$3.50 per million requests |
| Throttling | Stage-wide and per-route rate/burst only; no per-client limits | Stage and method limits, plus usage plans and API keys for per-client quotas |
| WAF | Not supported | Supported (a web ACL is ~$5/month plus rules; an hourly-style standing cost) |
| Request validation | In the Lambda | JSON-schema request validators (cdk-nag APIG2) |
| Logging | Access logs to a log group in the stack | Access logs; **execution** logs (cdk-nag APIG6) need the account-wide API Gateway CloudWatch role, a per-region singleton outside the stack. Creating it would change a third party's account settings and survive `cdk destroy`; this project's `cdk.json` already disables CDK's creation of it (`@aws-cdk/aws-apigateway:disableCloudWatchRole`). |
| Simplicity | Fewer constructs; `$default` stage at the root path, so `'self'` covers every route | Stage name in the path (`/prod/`), more resources |
| cdk-nag rules | APIG1 (access logs), APIG4 (auth) | APIG1, APIG2, APIG3 (WAF), APIG4, APIG6 |

The REST API's extra features (WAF, usage plans, schema validation) cost money or account-level
changes, and the demo needs none of them: there's one tiny JSON response per route and input
validation is a 32-character id check. HTTP API's limit is that throttling is global, not per
visitor: one noisy client can use up the stage limit for everyone. The throttle also caps the
cost of abuse, but only as tightly as its numbers; see the worst case under
[Throttling and concurrency](#throttling-and-concurrency).

## Bucket CORS rule

The page is served from `https://<api-id>.execute-api.<region>.amazonaws.com` and uploads to
`https://<bucket>.s3.<region>.amazonaws.com`, which is a different origin, so S3 needs one rule:

```python
s3.CorsRule(
    allowed_methods=[s3.HttpMethods.POST],
    allowed_origins=[http_api.api_endpoint],  # exact origin, no wildcard
    allowed_headers=[],  # FormData POST sends no custom headers
    max_age=300,
)
```

- `fetch` with a `FormData` body is a CORS "simple request" (POST, `multipart/form-data`), so
  there is no preflight. The rule makes S3 return `Access-Control-Allow-Origin`, which lets the
  page read the 204 status.
- CORS is **not** the access control here. It only decides whether a browser lets the page read
  the response. Upload authorization is the presigned policy (5 minutes, exact key, size, type).
- No dependency cycle: the bucket depends on the `AWS::ApiGatewayV2::Api` resource (for its id),
  and the functions depend on the bucket, but the API resource itself doesn't depend on its
  routes or integrations. Routes are added after both exist.

## Page: Content Security Policy and security headers

Without `'unsafe-inline'`, the HTML can't contain `<script>` or `<style>` blocks or `style=`
attributes, so JavaScript and CSS are separate files served by the API (`/app.js`, `/app.css`).
The page lambda builds the CSP from the `UPLOAD_ORIGIN` env var, which is
`"https://" + bucket.bucket_regional_domain_name`, the same value the presigner's URL must match:

```
default-src 'none';
script-src 'self';
style-src 'self';
connect-src 'self' https://<bucket>.s3.<region>.amazonaws.com;
img-src 'self';
base-uri 'none';
form-action 'none';
frame-ancestors 'none'
```

The upload uses `fetch` (governed by `connect-src`), not a `<form>` submit (which `form-action 'none'` blocks).

Headers on **every** API response (HTML, JS, CSS and JSON), set in `responses.py`:

| Header | Value |
|---|---|
| `Content-Security-Policy` | above (HTML only; harmless to omit on JSON) |
| `Strict-Transport-Security` | `max-age=31536000` (no `includeSubDomains`; the domain isn't ours) |
| `X-Content-Type-Options` | `nosniff` |
| `Referrer-Policy` | `no-referrer` |
| `X-Frame-Options` | `DENY` (legacy companion to `frame-ancestors`) |
| `Cross-Origin-Opener-Policy` | `same-origin` |
| `Permissions-Policy` | `camera=(), microphone=(), geolocation=()` |
| `Cache-Control` | `no-store` on JSON; `max-age=300` on static files |
| `Content-Type` | always explicit, with `charset=utf-8` |

**Output escaping:** `app.js` writes every value with `textContent` or `document.createElement`,
never `innerHTML`, so words from a user's file can't inject markup. The client also checks
size (≤ 100 KB) and the `.txt` extension, but only for faster feedback; the server-side limits are
what count. Polling: every 1 s, giving up after 30 attempts with a "processing timed out" message.

## Throttling and concurrency

- **API:** stage throttle 10 req/s, burst 20, plus 1 req/s, burst 2 on `POST /uploads`. One upload costs about
  2 + N requests, where N is the number of polls (usually 1–3).
- **Worst-case abuse cost.** `POST /uploads` is unauthenticated. If a script spends the whole
  10 req/s on it all day, that's ~864k presigns/day, each followed by a 100 KB upload, a processor
  run and a result write. Rough us-east-2 prices: S3 PUTs 2 × 864k × $0.005/1k ≈ $8.60, API
  ≈ $0.90, Lambda ≈ $0.50, logs ≈ $0.50, storage (~86 GB, expiring after 1 day) ≈ $0.10.
  Total **≈ $11/day (~$330/month)** while the attack lasts. A route-level throttle of 1 req/s on
  `POST /uploads` cuts that to ≈ $1/day, at the price of rejecting a second visitor who uploads
  in the same second (the page shows "busy, try again").
- **Presigned POST reuse.** A presigned POST can be replayed until it expires, so one presign
  allows repeated uploads to the same key for 5 minutes without going through the API. Each
  replay overwrites the same object, so storage doesn't grow, but every replay is an S3 PUT and a
  processor invocation. Bounded by the 5-minute expiry, the processor's reserved concurrency (extra
  events queue and retry), and the API throttle on getting new presigns.
- **Lambda reserved concurrency:** the account used during development had a quota of **400** concurrent
  executions, all unreserved (checked read-only with `aws lambda get-account-settings`). Lambda
  requires part of the quota to stay unreserved, and **new accounts often have a quota of only 10**,
  which leaves nothing to reserve. On such an
  account, any reserved concurrency fails the deploy, which conflicts with the roadmap's
  "third party deploys it" goal. Resolved: the `reserved_concurrency` context value defaults to 2 per
  function (8 total) and `0` turns reservation off (`-c reserved_concurrency=0`).
- Async S3 invocations that are throttled are retried by Lambda for up to 6 hours, so a burst
  of uploads gets slower but never loses events.

## cdk-nag and checkov

**cdk-nag 3.x works differently from 2.x** (found while building the stack). It is a CDK
policy-validation plugin, not an aspect: `Validations.of(app).add_plugins(AwsSolutionsChecks(app))`
in `app.py`. A finding that isn't acknowledged fails `cdk synth`. Suppression is
`Validations.of(construct).acknowledge(Acknowledgment(id=..., reason=...))`, one finding at a
time (the id carries the detail, e.g. `AwsSolutions-IAM5[Resource::<BucketLogicalId.Arn>/uploads/*]`),
so there is no blanket suppression. It lives in `requirements.txt`, pinned, because synth needs it.

checkov suppressions are `Metadata.checkov.skip` entries on the CloudFormation resource
(`id` + `comment`). All suppressions are in `demo_serverless/suppressions.py` (helpers) and
`_acknowledge_nag_findings` / `_skip_checkov_findings` in the stack, each with its reason.

Findings from the first run, each with its handling:

| Tool and rule | Where | Handling |
|---|---|---|
| nag `IAM4[AWSLambdaBasicExecutionRole]` | none any more | No acknowledgement. Our four function roles and the notifications helper role use no managed policy. The auto-delete helper's role does use it, but cdk-nag never evaluates it; see [below](#why-cdk-nag-does-not-flag-the-auto-delete-helpers-managed-policy). |
| nag `IAM5[Resource::<bucket>/uploads/*]`, `.../results/*` | presign, processor, results policies | Acknowledged per finding: keys are random ids, so a prefix wildcard is the narrowest possible; one action per statement. |
| nag `S1` / checkov `CKV_AWS_18` | bucket | Acknowledged/skipped: access logs need a second bucket with its own retention and teardown. |
| nag `APIG4` | 5 routes | Acknowledged: a public demo with no accounts; defended by throttling, 5-minute links, size limit, 1-day expiry. |
| checkov `CKV_AWS_21` | bucket | Skipped: versioning would keep deleted and overwritten copies of disposable data. |
| checkov `CKV_AWS_116` | 6 functions | Skipped: API functions are synchronous; processor has no DLQ by decision (retried twice, logged, page shows a failure/timeout); helpers are CDK-managed. |
| checkov `CKV_AWS_117` | 6 functions | Skipped: no private resources to reach; a VPC would need NAT/endpoints with hourly costs. |
| checkov `CKV_AWS_173` | 4 functions | Skipped: env vars are the bucket name and its public URL. |
| checkov `CKV_AWS_158` | 7 log groups | Skipped: CloudWatch encrypts at rest; KMS adds a monthly cost and a deletion waiting period. |
| checkov `CKV_AWS_115` | 2 CDK helpers (always); our 4 functions only if `reserved_concurrency=0` | Skipped with the reason. |

These did **not** fire, contrary to the first estimate: `CKV_AWS_144`/`145`/`338`, nag `L1`, `APIG1`, `S10`.

A test (`test_cdk_nag_passes_with_only_the_documented_acknowledgements`) synthesizes with the
plugin and fails on any unacknowledged finding, and a control test proves nag really does fail
a bucket without protections.

### CDK helper functions

Two features add functions we don't write:

- `add_event_notification` adds **BucketNotificationsHandler** (python3.13). It runs under
  **our own role**, passed to the bucket as `notifications_handler_role`
  (`_create_notifications_role`): no managed policy, write access to its own log group only, and
  CDK adds `s3:PutBucketNotification` on this bucket only. Tagged by the aspect.
- `auto_delete_objects` adds **Custom::S3AutoDeleteObjectsCustomResourceProvider** (Node.js).
  It's built from raw `CfnResource`s, so its function and role are **not tagged** by the aspect.

Left alone, Lambda creates `/aws/lambda/<name>` log groups for them at runtime, **with no
retention and no tags, and they survive `cdk destroy`**. Both handlers run during destroy, so
this would break roadmap step 5's tag query. Implemented fix (`_manage_helper_log_groups`):
a log group per helper (1 week, DESTROY, tagged) with a **CloudFormation-generated name**,
attached through an L1 `LoggingConfig.LogGroup` override. The reference alone gives the right
delete order (custom resource → function → log group); naming the groups `/aws/lambda/<Ref fn>`
*and* pointing `LoggingConfig` at them would be a dependency cycle. The Node provider's function
and role get their tags through an L1 `Tags` override. Their construct ids are constants in the
stack; `find_child` raises if a CDK upgrade renames them.

**A log group still leaked on the first destroy, and why.** After the first deploy and destroy, one
untagged, empty `NotificationsHelperLogs` group remained. CloudTrail showed CloudFormation had
created all 7 groups at deploy time, and that this one was created again at destroy time by the
notifications helper's role (`CreateLogGroup`), after it had been deleted. The synthesized
dependency chain is correct: `Custom::S3BucketNotifications` references the function in its
`ServiceToken`, and the function references the log group in `LoggingConfig`, so CloudFormation
deletes them in that order. (`test_helper_delete_order_...` pins this.) The cause is a race, not a
missing dependency: the helper's code PUTs CloudFormation's response itself, from a `finally`
block, **before the invocation returns**. CloudFormation then deletes the function and the log
group while Lambda is still flushing the invocation's last log lines, and Lambda recreates the
group with the role's `logs:CreateLogGroup`.

**Confirmed in CloudTrail** on the first destroy: CloudFormation's last `DeleteLogGroup` for the
group was at 09:11:54, and the helper's role called `CreateLogGroup` for the same group at
09:11:56, two seconds later. (The explanation was first inferred from the template and the
helper's code; the timestamps settle it.)

Fix, per helper:

- **Notifications helper:** it runs under our own role (above), which has no
  `logs:CreateLogGroup`, so Lambda cannot recreate the group. (An earlier version could not
  replace this role and used a deny; `notifications_handler_role` makes that unnecessary.)
  Passing a role is not enough on its own: **CDK attaches `AWSLambdaBasicExecutionRole` to
  whatever role it is given**, so `_create_notifications_role` clears `managed_policy_arns` on the
  underlying `CfnRole`. That is done on the property, not as a template-only override, because
  cdk-nag inspects the property: a template override left it reporting a managed policy the
  synthesized template no longer contained.
- **Auto-delete helper:** its role is built inside CDK from raw `CfnResource`s, and
  `CustomResourceProviderProps` has no role property (only `policy_statements`, which adds
  permissions), so it can't be replaced. It keeps `AWSLambdaBasicExecutionRole` and gets an inline
  `Policies` entry that **denies `logs:CreateLogGroup`** (an explicit deny beats the managed
  policy's allow). The deny is inline on the role, through an override on the underlying `CfnRole`,
  rather than a separate `AWS::IAM::Policy`: a separate policy resource has nothing depending on
  it and CloudFormation can delete it at the start of a destroy, removing the deny while the allow
  is still attached, exactly when the race happens.

The cost is that the helpers' last log lines (the REPORT line) are lost. A `DependsOn` or delay
would only narrow the window; removing or denying the permission closes it.

#### Why cdk-nag does not flag the auto-delete helper's managed policy

It doesn't evaluate that role at all, which is a gap in what cdk-nag checks, not approval of the
role. Checked by experiment while building this: the same managed-policy ARN, written the same way
(`Fn::Sub`), is reported as `AwsSolutions-IAM4` on a typed `iam.CfnRole` and **not** reported on a
raw `CfnResource` of type `AWS::IAM::Role`. The auto-delete provider's role is the second kind, so
the rule never runs on it (the notifications helper's role was flagged because CDK builds that one
as a typed role). So there is nothing to acknowledge, and nothing in the cdk-nag output vouches
for that role. It is handled by hand instead: it is the one remaining role with
`AWSLambdaBasicExecutionRole` (CloudWatch Logs writes plus `CreateLogGroup` on any group), it can't
be replaced, and it carries the inline `logs:CreateLogGroup` deny above. A test pins all three
facts (managed policy still there, deny present, nag passes without an acknowledgement), so a
cdk-nag or CDK upgrade that changes any of them fails the build.

## Cost and teardown residue

**Ongoing costs:** none. Everything is pay-per-request: HTTP API ($1 per million requests),
Lambda (free tier covers a demo), S3 (cents), and CloudWatch Logs ingestion ($0.50/GB).
There's no NAT gateway, load balancer, database, WAF or KMS key.

**What survives `cdk destroy`:**

| Item | Why | Handling |
|---|---|---|
| Helper-function log groups | The first destroy left one behind (a race, see above) | Fixed: the stack owns them; the notifications helper's role has no `logs:CreateLogGroup` and the auto-delete helper's role denies it. `scripts/teardown_check.py` verifies. **Verified live:** the first destroy after this change left no log groups, roles or tagged resources (only the shared `CDKToolkit` stack remained). |
| Log-delivery resource policy for the API access log group | API Gateway grants log delivery at deploy | No **account-level** policy is added. API Gateway's grant is scoped to the access log group itself (see the API access logs section below). |
| Lambda asset zips in the CDK bootstrap bucket (`cdk-hnb659fds-assets-...`) | `cdk deploy` uploads them; destroy doesn't remove them | Document; tiny. Bootstrap resources belong to the account, not the demo. |
| `CDKToolkit` bootstrap stack | Pre-existing, shared | Out of scope; never touched. |

Our bucket and log groups are DESTROY, and the bucket is emptied by `auto_delete_objects`.

## Testing

All handler tests run with fake credentials (`AWS_ACCESS_KEY_ID=testing`...), `AWS_DEFAULT_REGION=us-east-1`,
`AWS_CONFIG_FILE` and `AWS_SHARED_CREDENTIALS_FILE` pointing at `/dev/null`, and `socket.connect`
patched to fail, so no test can use a real AWS profile or touch the network
(`tests/unit/conftest.py`, applied automatically). S3 calls use botocore's `Stubber`, which
fails on any call that wasn't queued.

- `test_ids.py`, `test_config.py`: id and key helpers; `reserved_concurrency` validation.
- `test_page.py`: routes, CSP directives, security headers, no inline code or external
  references in the HTML, no markup-writing calls in `app.js`.
- `test_presign.py`: conditions, 5-minute expiry, key shape, URL origin equals the CSP/CORS origin.
- `test_processor.py`: key decoding, ignored keys (including our own `results/`), duplicate
  events, oversize, S3 errors not swallowed.
- `test_results.py`: id validation before any S3 call, `200 pending` mapping, headers.
- `test_demo_serverless_stack.py`: template assertions for each security property, per-function
  IAM, throttle, log groups, tags, plus the cdk-nag tests. Stack tests use
  `cdk.Environment(region="us-east-1")` with no account.
- `test_fingerprint.py`: the pure function (hash vector, BOM, NUL, invalid UTF-8, empty and
  no-word input, apostrophes, Unicode and normalization, sentence edge cases, tie-breaking,
  reading-time rounding, large pathological inputs).
- `test_teardown_check.py`, `test_smoke_test.py`, `test_access_log_check.py`, `test_awsutil.py`: the helper
  scripts, with stubbed clients (including the missing-`botocore[crt]` crash seen with `aws login` credentials).
- `test_ci_workflow.py`: the GitHub Actions workflow stays credential-free, read-only and deploy-free.
- `scripts/smoke_test.py` (standard library only) runs against a deployed stack: page and CSP
  (including that its upload origin matches the presigned URL), the full upload flow with CORS,
  the real fingerprint, `200 pending`, the bad-input path, size and type rejection, and that the
  bucket isn't publicly readable.
- `scripts/teardown_check.py` runs after `cdk destroy`: stack gone, no new tagged resources
  against a baseline file (kept outside the repo; it contains account IDs), no log groups or IAM
  roles with the stack's prefixes. The tagging API can lag a few minutes.
- `scripts/access_log_check.py` runs after the smoke test (`make smoke` does): waits for a log stream
  in the API's access log group, checks the events have the configured fields, and on failure prints
  the stage's actual `AccessLogSettings`, the log group's class, KMS key and own resource policy,
  and the account-level log resource policies. See below.
- `.github/workflows/check.yml` runs `make check` on pushes to `main` and pull requests with no AWS
  credentials, no secrets and read-only repository access; it first fails if credentials are present.
  `make check` was verified locally with an empty home directory and no AWS environment.
  `actions/checkout` is pinned to a full commit SHA with a version comment (v7.0.1), not the movable
  `v4` tag it started with, so a changed tag can't alter what runs in CI; a test enforces this for
  every `uses:` line. The v7.0.0 and v7.0.1 release notes were checked against this workflow: the only
  security change (blocking fork pull-request checkout for `pull_request_target` and `workflow_run`)
  doesn't apply to its `push` and `pull_request` triggers. Dependabot (`.github/dependabot.yml`,
  GitHub Actions only, weekly, `ci` commit prefix) proposes updates to those pins, changing the SHA and
  the version comment together; there are no pip or npm updates yet.
- The browser script can't be run offline (no JS tooling by design). Structural tests pin that it
  clears the result first and on failure, keeps polling while pending, and writes only through
  `textContent`; the real check is the browser step at deploy time.

### API access logs: what was verified offline, and how the empty-group question was settled

On the first live run the access log group had no streams while every function's did. Offline:

- The stage's `DestinationArn` is `Fn::GetAtt <log group>.Arn`, **identical to what CDK's own
  `HttpStage` L2 (`LogGroupLogDestination`) synthesizes**, and the L2 adds no extra resources or log
  resource policies either.
- The format is valid for an HTTP API: it includes `$context.requestId`, which HTTP APIs require, and
  only `$context` variables HTTP APIs support.
- HTTP API logging needs the *deploying identity* to hold `logs:CreateLogDelivery`,
  `logs:PutResourcePolicy`, `logs:DescribeResourcePolicies` and `logs:DescribeLogGroups`. That can't be
  checked offline.

Nothing in the template explained the empty group, so the cause was left open and
`scripts/access_log_check.py` was added to settle it. It did:

**Resolved after the live runs.** CloudTrail for both earlier deploys shows logging was configured
correctly:

- `CreateStage` carried `accessLogSettings`; and
- one second earlier, CloudFormation made a **log-group-scoped** `logs:PutResourcePolicy` granting
  `delivery.logs.amazonaws.com` `logs:CreateLogStream` and `logs:PutLogEvents` on that group, with
  `aws:SourceAccount` and `aws:SourceArn` conditions.

So the stage settings, the delivery grant and the deploying identity's permissions were all in
place. On the next live run (a fresh clone, smoke test passed) the access-log check saw logs arrive.
The earlier empty group was therefore **most likely a check made too soon** after the requests, since
delivery lags by a short while, not a delivery failure. That is the likeliest explanation rather
than a proof: nothing captured at the time shows why the group was still empty.

**Why "no resource policies" showed before and after the first deploy.**
`aws logs describe-resource-policies` lists only **account-level** policies by default. API
Gateway's grant is a **log-group-level** policy, which appears only with
`--policy-scope RESOURCE --resource-arn <log group ARN>`, using the ARN without the trailing `:*`
(the `logGroupArn` field that `describe-log-groups` returns). An empty account-level list therefore
says nothing about delivery, and the check's failure output now also reads the group's own policy.

## Decisions

Settled before building:

1. HTTP API.
2. One dependency-free `functions/static/app.js`, no npm or build step (the project's `CLAUDE.md` documents exactly this exception).
3. `reserved_concurrency` context value, default 2 per function, `0` disables it, validated in `demo_serverless/config.py` (used by `app.py`).
4. Stage throttle 10 req/s burst 20; `POST /uploads` 1 req/s burst 2.
5. Log retention one week.
6. SSE-S3 encryption.
7. No S3 access logs (justified suppression).
8. No DLQ; the page shows "processing failed or timed out" after 30 polls.
9. No `grant_read`/`grant_put`: explicit statements and no `s3:ListBucket` anywhere.
10. The stack owns the CDK helper log groups.
11. Fingerprint rules as written above.
12. `boto3` and `boto3-stubs[s3]`, pinned in `requirements-dev.txt` (the Lambda runtime provides boto3; these are for tests and mypy only), `pytest.ini` with `pythonpath = functions`, `mypy_path = functions`. Tests use fake credentials, a fixed region, null config files and no network.
13. Stack tests use a region-only environment.
14. cdk-nag pinned to an exact version in `requirements.txt`.
