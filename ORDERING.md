# Scheduled Main/V2 first, then the other screens

## Guarantee and scope

All four weekly workflow triggers run Friday **18:00 Europe/London**, starting
13 November 2026 (five screener outputs including Main and V2).
The Book/Margin, Negative-EV and Revenue/Net-Cash workflows each have a separate `wait-for-main`
job. Their production `screen` job now needs both its offline tests and that
gate to succeed. The gate does **not** hold `production-screener`; Main/V2 can
therefore acquire the existing production queue and finish before any other
scheduled screen enters it. Once Main/V2 succeeds, the other three keep their
existing serialized queue; their relative order is deliberately unspecified.

This is a dependency check, not a cron stagger or an assumption about GitHub's
queue order. No workflow is dispatched by another workflow. Existing scheduled
runs keep their original code snapshot; deploying this change does not restart,
cancel, or modify today's in-progress/pending runs.

## Predecessor evidence

The helper `scripts/wait_for_main_screen.py` performs public GitHub **GETs only**.
This repository was verified public when preparing the change. Like the existing
`completion.london_batch_date`, it needs no API token or new permission grant.

For each scheduled child it:

1. Reads that child's own run metadata and validates repository, workflow path,
   main branch/ref, SHA, event, run ID, and current attempt against the actual
   GitHub environment. It derives the batch date from the **original run creation
   time in Europe/London**, not wall-clock time or a rerun's start time.
   Scheduled launches from Friday 18:00 until Sunday 18:00 belong to Friday.
2. Finds a unique scheduled `screener.yml` run for that Friday batch, same repository,
   non-fork head repository, main branch and commit. It never substitutes an
   earlier-date success, an issue/PR run, another commit, or another workflow.
   An ambiguous same-date predecessor or incomplete API listing fails closed.
3. Pins that run ID while waiting. It requires successful completion and confirms
   both `test-completion` and the actual `run-screener` job succeeded for the
   reported attempt. A skipped production job is not enough.
4. Re-lists before release, checking uniqueness and the current attempt again.
   An old successful attempt cannot authorize a child after a newer attempt has
   been observed. A newly successful attempt must receive its own job verification.
   Explicit manual reruns begun after the final check are not atomically controlled
   by this read-only gate; the shared production queue still prevents overlap.

From the first Friday rollout, date filtering uses Friday 18:00 through Sunday
18:00 London boundaries converted to UTC, including DST. Main and children
created on opposite sides of midnight still share Friday's batch and receipt
date. Old scheduled runs created before 13 November 2026 retain their original
London calendar date when rerun. Explicit dispatch/issues runs also retain
their original London calendar date. Runs outside the scheduled launch window
fail closed rather than silently reusing last week's identity. A concurrent
merge producing different commits still blocks rather than guessing.

Book/Margin and Negative-EV now resolve the same original-run batch once before
resume and retain it through publication, matching Main/V2 and Revenue/Net-Cash.
No receipt fields, hashes, criteria, cooldown rules or data freshness rules change.
Friday 18:00 is a trading snapshot, not a promise of US closing prices.


## Failures and manual recovery

Failed/cancelled/timed-out Main/V2, malformed evidence, ambiguous identity, or an
expired wait budget prevents downstream screening. No FMP data is fetched and
no Sheet is written by the gate. Fix the failed scheduled Main run and use **Re-run all jobs** for its recovery;
then, when appropriate, explicitly rerun the failed dependent jobs. Partial Main
reruns that omit a successful test job from the current attempt cannot satisfy
this deliberately strict proof and remain blocked. Existing verified Main/V2
checkpoints still support resume during a full-job rerun. The code never
automatically retries production workflows or creates duplicate weekly runs.

The three existing `workflow_dispatch` entry points remain **explicit standalone
manual recovery**: the gate validates the main-branch workflow context and exits
successfully without any HTTP request. It neither launches Main nor creates
another run. Thus a deliberate manual dispatch is an exception to scheduled
ordering, not a second weekly trigger. PRs execute offline tests only. Existing
Main's `issues` trigger is unchanged and is not accepted as a substitute for the
scheduled predecessor.

## Ranking and completion compatibility

| Output | Workflow path | Existing eligible event types retained |
| --- | --- | --- |
| Main / V2 | `.github/workflows/screener.yml` | `schedule`, `issues` |
| Book / Margin | `.github/workflows/book-margin-screen.yml` | `schedule`, `workflow_dispatch` |
| Revenue / Net Cash | `.github/workflows/revenue-net-cash-screen.yml` | `schedule`, `workflow_dispatch` |
| Negative EV | `.github/workflows/negative-ev-fcf-screen.yml` | `schedule`, `workflow_dispatch` |

The added gate is just a job in the original workflow run. It does not change
`GITHUB_RUN_ID`, attempt, commit, event, workflow path, output owner, or receipt
hashing. Ranking automations can keep their existing run-eligibility and private
receipt verification rules. Downstream automation schedules must cover Friday as separately configured;
no relaxed origin check is needed.
There is no `workflow_run` event, reusable-workflow origin substitution, controller
dispatch, new secret, or Actions write permission.

All screening criteria, receipt/output protocols, cooldown/ranking modules,
and the recently approved 1,800/minute shared budget are unchanged. The production
queue, separate bulk limits, 61-second handoff spacing and `cancel-in-progress:
false` are retained.

## Waiting cost and bounds

- Poll every **240 seconds**. Three ordinary waiters use roughly 45 polling GETs/hour
  combined, plus initial identity and final success checks—not 120+/hour.
- GitHub's unauthenticated allowance is **60 requests/hour per IP**, potentially
  shared with other activity. Honor `Retry-After` and rate-reset headers; a low
  remaining quota delays even final verification. Unverifiable evidence never
  permits screening.
- Each helper has a **350-minute total budget** and each gate job a **360-minute
  timeout**, respecting GitHub-hosted runners' six-hour job limit. This includes
  API backoff. A historically three-to-four-hour Main run fits unless queueing,
  delays or retries consume the remaining margin. Exceeding the limit requires
  explicit recovery; the ordering guarantee is preserved by not running early.
- Three gate jobs occupy three runner slots while waiting. Standard Ubuntu runners
  are currently free for this public repository, but waiting is not resource-free
  and could contend with unrelated jobs on an account with little concurrency
  capacity. Making the repository private or changing runner types requires
  reevaluating access and billing.

A zero-waiting-runner controller would require dispatch authority/credentials or
changing workflow origins and ranking eligibility. That is intentionally outside
this minimal, no-new-grants change.

## Validation and deployment status

The Friday schedule and batch coordination were prepared against main commit
`b9e8f58e38b6923ad1f9dfb3654f7a7508cbc479` under approval on 8 November 2026.
Tests are offline and mock all HTTP, delays, run identities and attempts. Run
`python scripts/test_offline.py` for the aggregate suite; the historical live
`tests/test_v2.py` exclusion remains explicit. No production run is dispatched
for validation.

Official references checked 2026-10-24:
[job dependencies](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs),
[public workflow-run REST reads](https://docs.github.com/en/rest/actions/workflow-runs),
[rate limits](https://docs.github.com/en/rest/using-the-rest-api/rate-limits-for-the-rest-api),
[runner billing](https://docs.github.com/en/actions/concepts/billing-and-usage), and
[job limits](https://docs.github.com/en/actions/reference/limits).

