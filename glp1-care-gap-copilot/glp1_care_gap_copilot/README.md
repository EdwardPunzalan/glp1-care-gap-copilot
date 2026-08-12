# GLP-1 Care Gap Copilot

Surfaces GLP-1 monitoring care gaps in one protocol card when a clinician opens a
patient's chart or note, with one-click shortcuts to act on them.

## What it is — and is not

**It is** a copilot: it reads the chart, computes care gaps against configurable
thresholds, and shows them in one place.

**It is not** an autonomous agent. It never creates a task, never orders a lab,
and never writes to the chart on its own. Every write happens only when a
clinician clicks a button, and even then the command is *staged into the note
uncommitted* — the clinician reviews and signs it. The narrative sentence
restates detected facts; it makes no clinical decisions.

## When it runs

| Event | Fires when |
|---|---|
| `NOTE_OPENED` | A provider expands a note in the patient chart |
| `PATIENT_CHART__MEDICATIONS` | Medications load on the patient chart |

Both events target the Patient. Neither is on Canvas's
[disallowed list](https://docs.canvasmedical.com/sdk/effects/) for
`ADD_OR_UPDATE_PROTOCOL_CARD`, so the card cannot trigger a render loop. The card
is keyed per patient and upserts, so reopening the chart refreshes it rather than
stacking duplicates.

## Who gets a card

A patient is in scope if **either** holds:

- an **active GLP-1 medication** whose coding display matches any configured
  name fragment, or
- an **active obesity-related condition** whose ICD-10 code starts with any
  configured prefix (dotted and undotted codes both match).

Out-of-scope patients produce **no card at all**, not an empty one.

## Gaps detected

| Gap | Rule | Default |
|---|---|---|
| Stale weight | No weight or BMI observation within N days | 30 days |
| Labs overdue | An expected lab has no result within N days | 90 days |
| No follow-up | No future non-cancelled appointment within N days | 90 days |

**Which labs are expected is decided by explicit rule.** Labs listed in
`DIABETES_ONLY_LAB_NAMES` apply only to patients carrying a matching diagnosis
(`DIABETES_ICD10_PREFIXES`); everything else in `REQUIRED_LAB_NAMES` always
applies. Choosing which labs a patient clinically needs is a clinical decision,
so it is kept explicit, auditable, and testable.

## Actions

| Button | Effect | Safety |
|---|---|---|
| **Outreach task** | Stages a `TaskCommand` titled with the gap key | Uncommitted; clinician reviews and signs |
| **Order labs** | Stages a `LabOrderCommand` with the missing tests | Uncommitted; clinician reviews and signs |

`LabOrderCommand` validates its lab partner and test codes against the instance's
`LabPartner` / `LabPartnerTest` records, so the plugin resolves both at runtime
first. **If resolution fails, the gap still renders as an informational bullet
with no button** — the plugin never emits a command it knows to be invalid. The
same applies when `LAB_PARTNER_NAME` is unset.

If `OUTREACH_TEAM_DBID` is unset, outreach tasks are staged unassigned rather
than losing the button.

## Duplicate suppression

Before offering an outreach button, the plugin looks for an open task whose title
starts with `[{TASK_TITLE_PREFIX}: {gap_key}]`. If one exists the gap **still
renders**, annotated "outreach task already open", with the button removed.
Hiding the gap would mislead; keeping the button would duplicate work.

## The narrative

The one-line sentence at the top of the card is assembled deterministically from
the same derived scalars the gap rules produce. It restates what was detected and
nothing more — no clinical advice, no recommendation, no inference.

> Open GLP-1 monitoring gaps: no weight ever recorded; comprehensive metabolic
> panel, lipid panel not resulted within the monitoring interval; no follow-up
> booked in the next 90 days.

This was originally LLM-generated with a templated fallback. **The model path was
removed in 1.1.0** — it billed per render on a path that runs for every chart
open, and the fallback it degraded to was already what clinicians saw in
practice. Removing it also removed the plugin's only network I/O and its only
transmission of anything to a third party, so no patient-derived data leaves the
instance at all.

## Configuration

All values are parsed defensively — a malformed value logs a warning and falls
back to its default rather than breaking the card.

**Blank means "use the default", not "empty".** A variable declared in the
manifest but never configured reaches the plugin as an empty string, which is
indistinguishable from one an operator cleared on purpose. Blank therefore
always means the default. To genuinely empty a list, set it to the literal
`none` — currently supported only on `DIABETES_ONLY_LAB_NAMES`, the one list
where an empty value is meaningful (it makes every configured lab apply to every
patient). On the other lists `none` is treated as an ordinary entry, because an
empty cohort or lab list would silently disable detection.

| Variable | Default | Purpose |
|---|---|---|
| `WEIGHT_CHECK_INTERVAL_DAYS` | `30` | Stale-weight threshold |
| `LAB_INTERVAL_DAYS` | `90` | Lab recency threshold |
| `FOLLOWUP_HORIZON_DAYS` | `90` | Follow-up window |
| `GLP1_MED_NAME_FRAGMENTS` | `semaglutide,tirzepatide,liraglutide,dulaglutide,exenatide` | Cohort meds |
| `OBESITY_ICD10_PREFIXES` | `E66,Z68.4` | Cohort conditions |
| `REQUIRED_LAB_NAMES` | `hemoglobin a1c,comprehensive metabolic panel,lipid panel` | Expected labs |
| `DIABETES_ONLY_LAB_NAMES` | `hemoglobin a1c` | Labs expected only with a diabetes diagnosis. Set to the literal `none` to make every configured lab unconditional — see below |
| `DIABETES_ICD10_PREFIXES` | `E11` | Diagnosis prefixes gating the above |
| `OUTREACH_TEAM_DBID` | — | Default task assignee (Team `dbid`) |
| `LAB_PARTNER_NAME` | — | Lab partner for order commands |
| `TASK_TITLE_PREFIX` | `GLP-1 Copilot` | Dedupe marker |

## Performance

The handler runs on every note open and every chart load, so per-render cost is
what matters. Measured against a real database:

| Scenario | Queries |
|---|---|
| Out of scope (most patients) | **2** |
| In scope, sparse chart | **9** |
| In scope, 39 observations + 39 appointments + 20 lab reports | **9** |

Query count is **flat with respect to chart size**, and
`tests/test_query_budget.py` asserts it so a per-row query fails CI rather than
production. The cohort check runs first and short-circuits, so the majority of
patients — who are not on a GLP-1 — cost only two `.exists()` calls.

No model instances are hydrated anywhere: reads are `.exists()` for booleans and
`.values_list(...).first()` for single scalars, with relations crossed inside the
query predicate rather than in Python. The plugin performs **no database writes**.

**One scaling caveat.** The labs check issues one query per entry in
`REQUIRED_LAB_NAMES`. This is bounded by configuration rather than by patient
data, and at the default of 3 it is negligible. Configuring a large number of
labs (say 30) would issue that many queries per render on a hot path — at that
point it would be worth matching lab names in Python against a single fetch
instead. See the DB performance review in `.cpa-workflow-artifacts/` for the
reasoning behind the current shape.

The plugin makes no network calls at all.

## Development

```bash
uv run pytest                 # 102 tests
uv run pytest --cov=glp1_care_gap_copilot --cov-report=term-missing
uv run mypy glp1_care_gap_copilot tests
uv run canvas validate glp1_care_gap_copilot   # run before every deploy
```

**Run `canvas validate` before deploying.** Canvas executes plugins under
RestrictedPython, which rejects imports and builtins that pytest and mypy accept
without complaint — `collections.abc` and the name `object` are both unavailable,
for instance. Only `canvas validate` loads the handlers in that sandbox, so it is
the only local check that catches this class of failure.

## Verification status

Verified live on a Canvas instance (`xpc-dev`), against a real patient:

- Cohort matching on a brand-name prescription (Ozempic)
- All three gap rules firing, with A1c correctly excluded for a non-diabetic patient
- Card rendering with the templated narrative
- Labs gap rendering **without** a button when no lab partner is configured
- The outreach button staging an uncommitted `TaskCommand` carrying the dedupe marker
- **Duplicate suppression**: after committing that task, the follow-up gap kept
  rendering but lost its button and gained "— outreach task already open", while
  the other two gaps were unaffected

Two paths are **configuration-gated and have not been exercised against live
third-party services**. Both are covered by unit tests, and both degrade to a
verified fallback when unconfigured — which is how they currently run:

| Path | Requires | Unconfigured behavior (verified) |
|---|---|---|
| Lab-order button | `LAB_PARTNER_NAME` and `LAB_TEST_ORDER_CODES` | Gap renders as an informational bullet, no button |

Before enabling it in production, set the variables on one instance and confirm
the log line reports the expected outcome.

## Out of scope for v1

Writing to the chart without a click; patient-facing outreach; dose-titration or
dosing guidance; panel-wide reporting; backfilling gaps for patients whose chart
is never opened.
