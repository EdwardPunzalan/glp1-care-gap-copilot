# GLP-1 Care Gap Copilot

Surfaces GLP-1 monitoring care gaps in one protocol card when a clinician opens a
patient's chart or note, with one-click shortcuts to act on them — plus a chart
summary section graphing recent weigh-ins and flagging rapid weight loss.

## What it is — and is not

**It is** a copilot: it reads the chart, computes care gaps against configurable
thresholds, and shows them in one place.

**It is not** an autonomous agent. It never creates a task, never orders a lab,
and never writes to the chart on its own. Every write happens only when a
clinician clicks a button, and even then the command is *staged into the note
uncommitted* — the clinician reviews and signs it. The narrative sentence
restates detected facts; it makes no clinical decisions.

## When it runs

| Event | Handler | Fires when |
|---|---|---|
| `NOTE_OPENED` | Care gap card | A provider expands a note in the patient chart |
| `PATIENT_CHART__MEDICATIONS` | Care gap card | Medications load on the patient chart |
| `PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION` | Summary layout | The chart summary decides which sections to show |
| `PATIENT_CHART_SUMMARY__GET_CUSTOM_SECTION` | Weight trend | The chart summary requests our section's content |
| `SHOW_CHART_PATIENT_HEADER_BUTTON` | Weight trend button | The patient header decides which buttons to show |
| `ACTION_BUTTON_CLICKED` | Weight trend button | The "Weight trend" button is clicked |

The first two events target the Patient. Neither is on Canvas's
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
| **Safety review** | Rapid weight loss **and** a clinical warning sign | see below |
| **Safety check due** | Rapid weight loss and **nobody has screened** the patient | see below |
| Stale weight | No weight or BMI observation within N days | 30 days |
| Monitoring labs due | A required lab has no result within the patient's interval | see below |
| No follow-up | No future non-cancelled appointment within N days | 90 days |

The safety review is not an overdue-monitoring gap like the other three — it is
a signal that something may be wrong right now. It always sorts to the front of
the card, gets a **Contact patient** button instead of the usual outreach one,
and additionally raises a red chart banner. See
[Rapid loss with warning signs](#rapid-loss-with-warning-signs).

**Which labs are expected is decided by explicit rule**, not inferred. See
[Monitoring labs](#monitoring-labs).

## Monitoring labs

Three gates decide what a patient owes. All must pass.

### 1. Time on therapy

Nothing is expected until the patient has been on a GLP-1 for
`GLP1_MIN_DAYS_FOR_LABS` (default **90 days**). Drawing a metabolic panel three
weeks in measures the diet the patient was on *before* the drug.

Time on therapy is read from the **earliest** start date among the patient's
active GLP-1 medications. A patient switched from semaglutide to tirzepatide has
been on GLP-1 therapy continuously — restarting the clock at the switch would
excuse them from labs they are already overdue for.

**A patient who is in scope but not on a GLP-1 is asked for no labs.** There is
no therapy to monitor. This is a deliberate narrowing from earlier versions,
which asked for labs from anyone in the cohort.

### 2. Which labs

| Lab | Who | Satisfied by |
|---|---|---|
| Metabolic panel | everyone past the gate | a **CMP or a BMP** — either counts |
| Lipid panel | everyone past the gate | lipid panel |
| Hemoglobin A1c | everyone past the gate | hemoglobin A1c, HbA1c |
| **TSH with reflex to T4** | **only patients with a thyroid diagnosis** | TSH, thyroid stimulating hormone, thyrotropin |

Each requirement is evaluated independently, so a patient with a current lipid
panel and a stale A1c is asked only for the A1c.

TSH is thyroid-only by explicit clinical decision — adding it for everyone would
order a test most of these patients have no indication for. Clearing
`HYPOTHYROID_ICD10_PREFIXES` with the `none` sentinel switches it off entirely.

### 3. How often — the two tiers

| Patient | Interval | Variable |
|---|---|---|
| Carries a metabolic comorbidity | **90 days** | `LAB_INTERVAL_DAYS` |
| Obesity, and none of those | **365 days** | `LAB_INTERVAL_OBESITY_ONLY_DAYS` |

A comorbidity is any of:

| Comorbidity | Default ICD-10 | Variable |
|---|---|---|
| High cholesterol | `E78` | `HIGH_CHOLESTEROL_ICD10_PREFIXES` |
| Diabetes | `E11`, `E10` | `DIABETES_ICD10_PREFIXES` |
| Pre-diabetes | `R73` | `PREDIABETES_ICD10_PREFIXES` |
| Hypothyroidism | `E03`, `E02`, `E89.0` | `HYPOTHYROID_ICD10_PREFIXES` |

**The two tiers are the point of the design.** Obesity alone is a slower
clinical picture than obesity plus dysglycemia. Putting both on a 90-day cycle
would bury the patients who need watching under the ones who don't, and the card
would be ignored.

All four groups are clearable with the `none` sentinel, so a practice that does
not want (say) pre-diabetes pulling patients onto the short interval can say so.

### Ordering

`LAB_TEST_ORDER_CODES` is keyed by **requirement key**: `metabolic panel`,
`lipid panel`, `hemoglobin a1c`, `tsh`. One code per requirement. Unmapped
requirements render as an informational row with no button rather than a lab
order the partner would reject.

**Older per-lab-name keys still resolve.** The requirement key is tried first,
then the result names it is satisfied by — so an instance configured before
"CMP or BMP" existed, mapping `comprehensive metabolic panel:10231`, keeps its
order button on upgrade instead of silently losing it.

## Actions

| Button | Effect | Safety |
|---|---|---|
| **Outreach task** | Stages a `TaskCommand` titled with the gap key | Uncommitted; clinician reviews and signs |
| **Order labs** | Stages a `LabOrderCommand` with the missing tests | Uncommitted; clinician reviews and signs |

### Which lab test gets ordered

**You state it; the plugin never guesses.** `LAB_TEST_ORDER_CODES` maps each
requirement key to one exact order code:

```
metabolic panel:10231, lipid panel:7600, hemoglobin a1c:496, tsh:8998
```

Matching by name was tried first and does not work against a real catalog. On
`xpc-dev`, XPC Lab lists **8** tests containing "comprehensive metabolic panel"
and **no** test named plain "lipid panel" — so a name match either ordered every
variant at once or found nothing at all. Choosing between `LIPID PANEL, STANDARD`
and `LIPID PANEL, CARDIO IQ(R)` is a clinical and contractual decision for the
practice, so it has to be stated.

Find the codes in Admin › Health Gorilla › Lab tests, filtered by your lab.

Each configured code is re-checked against the partner's catalog at render time,
so a stale mapping degrades to "no button" rather than a command Canvas rejects.
**If anything fails to resolve — no partner, no mapping, unknown code — the gap
still renders as an informational bullet with no button.** The plugin never emits
a command it knows to be invalid.

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

## Weight trend section

A custom chart summary section plots the patient's most recent weigh-ins as a
line graph and shades intervals of **rapid** weight loss in bright red.

### The flagging rule

An interval is flagged only when **both** conditions hold:

| Condition | Default | Variable |
|---|---|---|
| More than N pounds lost | 5 lb | `WEIGHT_DROP_ALERT_LB` |
| ...within N days | 7 days | `WEIGHT_DROP_MAX_INTERVAL_DAYS` |

**The interval test is what makes the flag mean "rapid."** These patients dose
weekly, so losing 14 lb between consecutive weekly weigh-ins is a safety signal,
while losing the same 14 lb across ten weeks is the medication working as
intended. Without the second condition the graph would light up red on every
successful course of treatment and clinicians would learn to ignore it.

Verified live on two patients with identical 14 lb drops:

| Patient | Drop | Interval | Flagged |
|---|---|---|---|
| `Zzdemo Glp1Care` | 14 lb | 14 days | No |
| `Zzdemo Rapidloss` | 14 lb | 7 days | **Yes** |

Only **losses** are flagged — a gain of the same size is left unmarked.

**Intervals are measured in whole days.** Two weigh-ins a clinician would call
"a week apart" are never exactly 168.000 hours apart, so comparing the raw
elapsed time against a 7-day window would flag or spare them depending on what
time of day the patient stepped on the scale.

Three more details worth knowing:

- **The storage unit varies, so the units column is always consulted.** The SDK
  documents weight as ounces (`weight_oz` on the vitals command), and the SDK's
  own growth-chart example converts `obs.value` from ounces without reading the
  units column. That is not universally true: weights posted through the FHIR
  API on `xpc-dev` are normalized and come back in **pounds**. Trusting the
  documented unit would have plotted a 229 lb patient at 3,664 lb. Recognized
  units (`oz`, `lb`, `kg`, `g` and their variants) are converted; an
  unrecognized unit **drops that reading with a warning** rather than plotting a
  guess, because a confidently wrong weight is worse than a gap in the line. A
  blank unit falls back to ounces.
- **The graph reads `weight` only, never `bmi`.** The stale-weight *gap*
  deliberately accepts a BMI observation as evidence that someone weighed the
  patient; the graph must not, or a BMI of 32 lands on a pound-scaled axis.
- **The x axis is scaled by elapsed time, not by reading index.** Evenly spacing
  the points would draw an identical slope for 14 lb lost over three weeks and
  14 lb lost over eight months, which is the exact distinction the graph exists
  to make visible.

Out-of-scope patients get a one-line explanation instead of a graph, so an empty
section never looks like a broken one.

### Seeing it full size

A **Weight trend** button in the patient header opens the same graph in a modal,
rendered from the same template at a larger size. The button hides itself for
patients with no graph to enlarge.

**Why the button is in the header and not in the section's corner.** It was
built there first and it cannot work. Section content is sandboxed page markup:
it cannot return an effect when something inside it is clicked, and Canvas
defines no `ButtonLocation` for custom summary sections, so a native button
cannot be placed inside one either. The documented escape hatch — have the
iframe `fetch()` a SimpleAPI endpoint that returns the effect — was implemented
and **verified not to work for modals**: the request authenticated, reached the
endpoint, built the right graph and returned `200`, but Canvas's frontend never
opened the modal. Every `LaunchModalEffect` in the SDK documentation is returned
from an `ActionButton.handle()` or an `Application.on_open()`, and that is the
path this uses.

Dropping the endpoint also removed the plugin's only HTTP surface, so it still
has no API, no authentication code, and no patient identifier in any URL.

### Chart summary layout — read before deploying

Registering a custom section requires a second handler
(`GLP1ChartSummaryConfiguration`) that answers
`PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION`. That effect **replaces the entire
chart summary layout**, so `BUILT_IN_SECTIONS` restates every section Canvas
ships and a test asserts the list stays complete against
`PatientChartSummaryConfiguration.Section`. Listing a subset would silently
delete the rest of the chart summary for every patient on the instance.

**Only one plugin can win this event.** If another installed plugin also returns
a layout, the result is last-writer-wins and one of the two configurations is
discarded — which can make another plugin's custom section disappear, or ours.
Before enabling on a shared instance, check the logs for a second responder:

```bash
uv run canvas logs --host <instance> | grep SECTION_CONFIGURATION
```

## Rapid loss with warning signs

Weight loss is the *point* of GLP-1 therapy. Weight loss alongside vomiting,
dehydration, or an empty plate is a reason to pick up the phone. This rule
separates the two.

It fires when **both** hold:

1. The trend graph has flagged a **rapid drop** (the same rule the red band
   uses — the banner can never disagree with the chart), and
2. at least `SAFETY_MIN_FINDINGS` of seven warning signs appears within
   `SAFETY_WINDOW_DAYS` either side of that drop's later weigh-in.

### The seven findings

| Finding | Question code | ICD-10 proxy |
|---|---|---|
| Very poor oral intake | `GLP1SC_INTAKE` | `R63.0`, `R63.3` |
| Persistent nausea, vomiting, or diarrhea | `GLP1SC_GI` | `R11`, `R19.7`, `K52.9` |
| Dehydration | `GLP1SC_DEHYDRATION` | `E86` |
| Weakness or significant fatigue | `GLP1SC_FATIGUE` | `R53` |
| Evidence of muscle loss | `GLP1SC_MUSCLE` | `M62.84`, `M62.5` |
| Inadequate protein intake | `GLP1SC_PROTEIN` | `E43`, `E44`, `E46` |
| Abdominal/RUQ pain suggesting gallbladder disease | `GLP1SC_RUQ` | `R10.1`, `K80`, `K81` |

### Two sources, deliberately

**The shipped questionnaire.** This plugin ships a `GLP-1 Safety Check`
structured assessment (`templates/glp1_safety_check.yml`, registered under
`components.questionnaires`). All seven findings are on it, so one completed
form answers the rule by itself. Only **committed** interviews count — a
half-filled draft is not a clinical assertion.

**Coded conditions.** Every finding also has an ICD-10 proxy, so a chart nobody
screened can still trip the rule. In practice only the GI and constitutional
codes get used during a routine visit: nobody reaches for `M62.84` to say a
patient looks sarcopenic. **The condition path is a safety net, not a
replacement for the form** — relying on it alone would leave the plugin
permanently blind to the three nutritional findings, which are exactly the ones
that define malnutrition risk.

A finding confirmed on the form *and* inferred from a code counts once.

### Why there is a time window

The two halves of the rule are observed on different clocks: the drop is
computed between two weigh-ins, while a symptom is recorded whenever the patient
reports it. Without a window, a June weight drop would pair with a November
diagnosis. `SAFETY_WINDOW_DAYS` is measured **either side** of the drop, because
a patient may report the symptom at the visit that discovers the loss or a few
weeks later when they finally call in.

Conditions are matched on `onset_date` — the Canvas `Condition` model exposes no
`created` timestamp, and onset is the clinically correct field anyway.

### The banner clears itself

A banner Canvas has drawn stays on the chart until something removes it, so
every render emits either `AddBannerAlert` or `RemoveBannerAlert` — never
nothing. Emitting nothing on the resolved path would leave the alert outliving
the problem, and clinicians would learn to ignore it. Narratives are capped at
Canvas's 90-character limit: one finding is named, several are counted.

### What this rule cannot do

**It only sees what got written into a box.** A note that says "patient reports
she's barely eating" in prose is invisible to it. If nobody completes the form
and nobody codes a diagnosis, a patient in trouble produces no alert. The rule
narrows the blind spot; it does not close it.

### Surfacing the blind spot

Because the gap cannot be closed in code, the plugin says when it is standing in
one. Whenever a rapid drop has **no committed safety check inside the window**,
the card carries a second row — *"Rapid weight loss (N lb in Nd) with no safety
check on file"* — with a **Task MA to screen** button that stages a task naming
the questionnaire.

Two details that matter:

- **A coded diagnosis does not settle it.** Even when conditions already tripped
  the alert, the screening row still appears. The condition path sees four of
  the seven findings with any reliability, so a diagnosis is never a substitute
  for the form that covers oral intake, protein, and muscle loss.
- **The row states whether there is a visit to screen at.** With a follow-up
  booked it reads "screen at next visit"; with none it reads "no visit booked —
  schedule and screen", and the follow-up gap sits alongside it so both tasks
  can be sent together.

An all-negative form counts as screened. "We looked and they are fine" is a
clinical assertion; silence is not.

## Configuration

All values are parsed defensively — a malformed value logs a warning and falls
back to its default rather than breaking the card.

**Blank means "use the default", not "empty".** A variable declared in the
manifest but never configured reaches the plugin as an empty string, which is
indistinguishable from one an operator cleared on purpose. Blank therefore
always means the default. To genuinely empty a list, set it to the literal
`none` — supported on the four comorbidity prefix lists, where an empty value is
meaningful (it stops that diagnosis pulling patients onto the short interval, and
on the thyroid list it switches TSH off). On the cohort lists `none` is treated
as an ordinary entry, because an empty cohort would silently disable the plugin.

| Variable | Default | Purpose |
|---|---|---|
| `WEIGHT_CHECK_INTERVAL_DAYS` | `30` | Stale-weight threshold |
| `LAB_INTERVAL_DAYS` | `90` | Lab interval for a patient with a metabolic comorbidity |
| `LAB_INTERVAL_OBESITY_ONLY_DAYS` | `365` | Lab interval for a patient whose only qualifying diagnosis is obesity |
| `GLP1_MIN_DAYS_FOR_LABS` | `90` | Time on GLP-1 therapy before any lab is expected |
| `FOLLOWUP_HORIZON_DAYS` | `90` | Follow-up window |
| `GLP1_MED_NAME_FRAGMENTS` | `semaglutide,tirzepatide,liraglutide,dulaglutide,exenatide` | Cohort meds |
| `OBESITY_ICD10_PREFIXES` | `E66,Z68.4` | Cohort conditions |
| `DIABETES_ICD10_PREFIXES` | `E11,E10` | Diabetes; clearable with `none` |
| `HIGH_CHOLESTEROL_ICD10_PREFIXES` | `E78` | High cholesterol; clearable with `none` |
| `PREDIABETES_ICD10_PREFIXES` | `R73` | Pre-diabetes; clearable with `none` |
| `HYPOTHYROID_ICD10_PREFIXES` | `E03,E02,E89.0` | Hypothyroidism; also gates TSH. Clearable with `none` |
| `OUTREACH_TEAM_DBID` | — | Default task assignee (Team `dbid`) |
| `LAB_PARTNER_NAME` | — | Lab partner name for order commands (must be active) |
| `LAB_TEST_ORDER_CODES` | — | `requirement key:order code` pairs (`metabolic panel`, `lipid panel`, `hemoglobin a1c`, `tsh`); no button for unmapped labs |
| `TASK_TITLE_PREFIX` | `GLP-1 Copilot` | Dedupe marker |
| `WEIGHT_TREND_POINTS` | `6` | Weigh-ins plotted on the trend graph |
| `WEIGHT_DROP_ALERT_LB` | `5` | Pounds lost between consecutive weigh-ins before the interval is flagged red |
| `WEIGHT_DROP_MAX_INTERVAL_DAYS` | `7` | How close together those weigh-ins must be for the loss to count as rapid. At `7`, a pair 8 days apart is ignored — raise it if the practice weighs on a looser schedule |
| `SAFETY_WINDOW_DAYS` | `30` | How close a warning sign must sit to the rapid drop, measured either side of it, before the two are treated as one clinical picture |
| `SAFETY_MIN_FINDINGS` | `1` | How many of the seven warning signs must accompany the drop. At `1` this errs toward calling the patient; raise it to demand corroboration |

## Performance

The handler runs on every note open and every chart load, so per-render cost is
what matters. Measured against a real database:

| Scenario | Queries |
|---|---|
| Out of scope (most patients) | **2** |
| In scope, sparse chart | **9** |
| In scope, 39 observations + 39 appointments + 20 lab reports | **9** |
| Weight trend section, out of scope | **2** |
| Weight trend section, in scope | **3** |

The trend section adds a cohort check plus one windowed read of the weigh-ins.
Its cost is bounded by `WEIGHT_TREND_POINTS`, not by how often the patient has
been weighed — a patient with 40 recorded weights costs the same as one with 1.

Query count is **flat with respect to chart size**, and
`tests/test_query_budget.py` asserts it so a per-row query fails CI rather than
production. The cohort check runs first and short-circuits, so the majority of
patients — who are not on a GLP-1 — cost only two `.exists()` calls.

No model instances are hydrated anywhere: reads are `.exists()` for booleans and
`.values_list(...).first()` for single scalars, with relations crossed inside the
query predicate rather than in Python. The plugin performs **no database writes**.

**One scaling note.** The labs check issues one query per required lab — three,
or four for a thyroid patient — plus a single OR-ed comorbidity lookup covering
all four diagnosis groups. The count is fixed by the rule rather than by chart
size or configuration, and patients below the time-on-therapy gate skip all of
it. See the DB performance review in `.cpa-workflow-artifacts/` for the reasoning
behind the current shape.

The plugin makes no network calls at all.

## Development

```bash
uv run pytest                 # 183 tests
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
