# GLP-1 Care Gap Copilot

Surfaces GLP-1 monitoring care gaps in one protocol card when a clinician opens a
patient's chart or note, with one-click shortcuts to act on them.

## What it is — and is not

**It is** a copilot: it reads the chart, computes care gaps against configurable
thresholds, and shows them in one place.

**It is not** an autonomous agent. It never creates a task, never orders a lab,
and never writes to the chart on its own. Every write happens only when a
clinician clicks a button, and even then the command is *staged into the note
uncommitted* — the clinician reviews and signs it. The LLM-generated text is a
non-actionable summary; it makes no clinical decisions.

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

**Which labs are expected is decided by rule, not by the model.** Labs listed in
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

## LLM narrative and PHI minimization

The one-line rationale is generated with `LlmAnthropic`. **No raw chart data is
ever sent.** The payload is built by construction from derived scalars only:

```json
{
  "gaps": [
    {"type": "stale_weight", "days_since_last": 58},
    {"type": "labs_overdue", "missing": ["hemoglobin a1c"], "days_since_last": 214}
  ],
  "med_class": "GLP-1 receptor agonist",
  "weeks_since_last_visit": 12
}
```

Never sent: name, DOB, MRN, patient id, address, contact info, medication names
or doses, raw lab values, note text, provider identity. `tests/test_rationale.py`
asserts this by allow-list, so any new payload key fails the suite until it is
explicitly declared safe.

**The card always renders.** A missing key, a disabled kill switch, an HTTP
error, an exception, or an empty response all fall back to a deterministic
templated sentence built from the same facts. Set `ENABLE_LLM_RATIONALE=false` to
disable the model path entirely.

## Configuration

All values are parsed defensively — a malformed or missing value logs a warning
and falls back to its default rather than breaking the card.

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | LLM key (sensitive) |
| `LLM_MODEL` | `claude-sonnet-5` | Model id |
| `ENABLE_LLM_RATIONALE` | `true` | Kill switch for the LLM path |
| `WEIGHT_CHECK_INTERVAL_DAYS` | `30` | Stale-weight threshold |
| `LAB_INTERVAL_DAYS` | `90` | Lab recency threshold |
| `FOLLOWUP_HORIZON_DAYS` | `90` | Follow-up window |
| `GLP1_MED_NAME_FRAGMENTS` | `semaglutide,tirzepatide,liraglutide,dulaglutide,exenatide` | Cohort meds |
| `OBESITY_ICD10_PREFIXES` | `E66,Z68.4` | Cohort conditions |
| `REQUIRED_LAB_NAMES` | `hemoglobin a1c,comprehensive metabolic panel,lipid panel` | Expected labs |
| `DIABETES_ONLY_LAB_NAMES` | `hemoglobin a1c` | Labs expected only with a diabetes diagnosis (may be set empty) |
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

The LLM call is the only network I/O and never blocks the card from rendering.

## Development

```bash
uv run pytest                 # 104 tests
uv run pytest --cov=glp1_care_gap_copilot --cov-report=term-missing
uv run mypy glp1_care_gap_copilot tests
uv run canvas validate glp1_care_gap_copilot   # run before every deploy
```

**Run `canvas validate` before deploying.** Canvas executes plugins under
RestrictedPython, which rejects imports and builtins that pytest and mypy accept
without complaint — `collections.abc` and the name `object` are both unavailable,
for instance. Only `canvas validate` loads the handlers in that sandbox, so it is
the only local check that catches this class of failure.

## Out of scope for v1

Writing to the chart without a click; patient-facing outreach; dose-titration or
dosing guidance; panel-wide reporting; backfilling gaps for patients whose chart
is never opened.
