# Plugin Specification — GLP-1 Care Gap Copilot

**Proposed plugin name:** `glp1-care-gap-copilot`
**Date:** 2026-08-11
**Status:** Approved and implemented (see "Resolutions" at the end)

---

## 1. Problem

Patients on GLP-1 receptor agonist therapy require ongoing monitoring between visits: regular
weight/BMI capture to assess response, baseline and interval labs before dose escalation, and
consistently scheduled follow-up. In practice these fall through the cracks — nobody notices a
patient hasn't had a weight recorded in two months until they're already sitting in the room, and
reconstructing the picture (current meds, last weight, last labs, next appointment) means clicking
through several chart sections while the patient waits.

## 2. What this plugin is — and is not

**It is** a copilot for the clinician: it reads the chart, computes care gaps against configurable
thresholds, and surfaces them in one card with one-click shortcuts to act.

**It is not** an autonomous agent. It never creates a task, never orders a lab, and never writes to
the chart on its own. Every write happens only when a clinician clicks a button, and even then the
command is *inserted into the note uncommitted* — the clinician still reviews and signs it. The
LLM-generated text is a non-actionable summary; it makes no clinical decisions.

## 3. Users

| Who | How they encounter it |
|---|---|
| Prescribing clinician | Opens the patient's note → card is already computed, reads the one-line rationale to get up to speed |
| Care coordinator / MA | Opens the chart → sees which gaps are open and whether outreach is already in flight |

## 4. Trigger

Two events, both handled by one `BaseHandler`:

| Event | Fires when | Target |
|---|---|---|
| `NOTE_OPENED` | A provider expands a note in the patient chart | Patient |
| `PATIENT_CHART__MEDICATIONS` | Medications load on the patient chart (i.e. chart open) | Patient |

**Design constraint (verified in SDK docs):** Canvas explicitly disallows returning
`ADD_OR_UPDATE_PROTOCOL_CARD` from `PATIENT_CHART__CONDITIONS` and
`PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION` to prevent infinite loops
([effects docs](https://docs.canvasmedical.com/sdk/effects/)). Neither of the two events above is on
that list, so the design is safe. `PATIENT_CHART__MEDICATIONS` is also a natural fit — this plugin
is fundamentally about medications.

The card is keyed per patient and *upserted*, so re-opening the chart refreshes it rather than
stacking duplicates.

## 5. Cohort

A patient is in scope if **either** condition holds:

- **Active GLP-1 medication** — an active `Medication` whose name matches any fragment in a
  configurable list. Default: `semaglutide, tirzepatide, liraglutide, dulaglutide, exenatide`.
- **Obesity-related condition** — an active `Condition` whose ICD-10 code starts with any prefix in
  a configurable list. Default: `E66` (overweight/obesity), `Z68.4` (BMI ≥ 30 class).

Out-of-scope patients produce **no card at all** (not an empty card).

## 6. Data pulled (Canvas data module — no FHIR API, no external calls)

| Need | Model / query | Doc |
|---|---|---|
| Current meds | `Medication.objects.for_patient(id)`, active only | [data-medication](https://docs.canvasmedical.com/sdk/data-medication/) |
| Weight / BMI | `Observation.objects.for_patient(id).filter(name="weight" \| "bmi")`, latest by effective date | [data-observation](https://docs.canvasmedical.com/sdk/data-observation/) |
| Last visit | Most recent past `Appointment` / `Note` for the patient | [data-appointment](https://docs.canvasmedical.com/sdk/data-appointment/) |
| Labs | `LabReport` / `LabValue` for the patient, most recent per test | [data-labs](https://docs.canvasmedical.com/sdk/data-labs/) |
| Future appointments | `patient.appointments` filtered to future, non-cancelled | [data-appointment](https://docs.canvasmedical.com/sdk/data-appointment/) |
| Open tasks (dedupe) | `patient.tasks.filter(status=TaskStatus.OPEN)` | [data-task](https://docs.canvasmedical.com/sdk/data-task/) |
| Conditions | `Condition.objects.for_patient(id)`, active | [data-condition](https://docs.canvasmedical.com/sdk/data-condition/) |

All queries are patient-scoped and read-only. Query count is bounded and fixed per card render (no
per-item loops — see §11).

## 7. Gaps detected

| # | Gap | Rule | Default threshold |
|---|---|---|---|
| 1 | **Stale weight** | No weight observation within N days | 30 days |
| 2 | **Labs overdue before dose increase** | Any expected lab has no result within N days | 90 days |
| 3 | **No follow-up scheduled** | No future non-cancelled appointment within N days | 90 days |

**Expected labs** are selected deterministically from configurable rules, *not* by the LLM:

- **A1c** — when the patient has a diabetes condition (`E11.*`) or is on a GLP-1 with a diabetes
  indication
- **CMP** — renal/hepatic monitoring during titration
- **Lipid panel** — cardiometabolic risk

> ⚠️ **Assumption flagged for your confirmation.** You wrote that the plugin would "use reasoning to
> output relevant labs." I've implemented lab *relevance* as deterministic, configurable rules rather
> than an LLM judgment, because choosing which labs a patient clinically needs is a clinical
> decision — which conflicts with your own stated principle that the plugin "will not take over the
> care itself" and that the LLM output is "non-actionable." The LLM phrases the summary; it does not
> decide what to order. If you'd rather the LLM propose the lab set, say so and I'll change it —
> but I'd recommend keeping the rule set explicit and auditable.

## 8. The card

Rendered as a `ProtocolCard` ([docs](https://docs.canvasmedical.com/sdk/effect-protocol-cards/)):

```
┌─ PROTOCOLS ─────────────────────────────┐
│ GLP-1 Care Gaps            [DUE]        │
│                                         │
│ 8 weeks overdue for weight check per    │  ← narrative: LLM rationale
│ titration protocol; A1c not drawn       │
│ before next dose increase.              │
│                                         │
│ • No weight recorded in 58 days         │  ← recommendations
│                      [ Outreach task ]  │
│ • A1c overdue before dose increase      │
│                      [ Order labs    ]  │
│ • No follow-up appointment scheduled    │
│                      [ Outreach task ]  │
└─────────────────────────────────────────┘
```

- `key`: stable per patient, so the card upserts instead of duplicating
- `status`: `DUE` when any gap is open, `SATISFIED` when all clear
- `narrative`: the LLM rationale (§9)
- `recommendations`: one per open gap

### Action buttons

Both target commands are on Canvas's supported-insertion list for protocol cards (verified: `Task`
and `LabOrder` both appear in the supported commands table).

| Button | Effect | Safety |
|---|---|---|
| **Outreach task** | Inserts a `TaskCommand` — title carries the gap key, assignee from configured team, comment states the gap | Inserted uncommitted; clinician reviews and signs |
| **Order labs** | Inserts a `LabOrderCommand` with the missing tests pre-selected | Inserted uncommitted; clinician reviews and signs |

`LabOrderCommand` validates `lab_partner` and `tests_order_codes` against the instance's
`LabPartner` / `LabPartnerTest` records. The plugin resolves these at runtime from a configured lab
partner name. **If resolution fails, the gap still renders as an informational bullet with no
button** — the plugin never emits a command it knows to be invalid.

## 9. LLM rationale — PHI minimization

Generated with `LlmAnthropic` + `LlmSettingsAnthropic` (`canvas_sdk.clients.llms`), API key from
plugin secrets. No raw chart data is ever sent.

**What is sent** — only derived scalars:

```json
{
  "gaps": [
    {"type": "stale_weight", "days_overdue": 58},
    {"type": "labs_overdue", "missing": ["A1c"], "days_since_last": 214}
  ],
  "med_class": "GLP-1 receptor agonist",
  "weeks_since_last_visit": 12
}
```

**What is never sent:** name, DOB, MRN, patient id, address, contact info, medication names or
doses, raw lab values, free-text notes, provider identity.

Additional controls:
- System prompt constrains output to one factual sentence, no recommendations, no clinical advice
- Output length capped; response sanitized before rendering
- **Graceful degradation:** on missing key, API error, or timeout, the card falls back to a
  deterministic templated sentence built from the same facts. The card always renders.
- `ENABLE_LLM_RATIONALE=false` disables the LLM path entirely — useful for environments that
  disallow third-party model calls.

## 10. Duplicate suppression

Before offering an outreach-task button, the plugin queries the patient's **open** tasks and looks
for one already covering the same gap, matched on a stable title prefix + gap key
(e.g. `[GLP-1 Copilot: stale_weight]`).

If a matching open task exists, the gap **still renders** — but as an informational bullet annotated
"outreach task already open," with the button removed. This keeps the clinician informed while making
a duplicate impossible. (Hiding the gap entirely would be misleading; leaving the button would
duplicate work.)

## 11. Performance

- Fixed, bounded query count per render — no N+1 over medications, labs, or appointments
- Latest-value lookups use `.order_by(...).first()` rather than hydrating full history
- Cohort check (medication/condition match) runs first and short-circuits: non-GLP-1 patients cost
  two cheap queries and exit
- LLM call is the only network I/O, is timeout-bounded, and never blocks card rendering

## 12. Configuration (manifest-declared secrets)

Every threshold is configurable — nothing clinical is hardcoded.

| Secret | Default | Purpose |
|---|---|---|
| `ANTHROPIC_API_KEY` | — | LLM key |
| `LLM_MODEL` | `claude-sonnet-5` | Model id |
| `ENABLE_LLM_RATIONALE` | `true` | Kill switch for the LLM path |
| `WEIGHT_CHECK_INTERVAL_DAYS` | `30` | Stale-weight threshold |
| `LAB_INTERVAL_DAYS` | `90` | Lab recency threshold |
| `FOLLOWUP_HORIZON_DAYS` | `90` | Follow-up window |
| `GLP1_MED_NAME_FRAGMENTS` | `semaglutide,tirzepatide,liraglutide,dulaglutide,exenatide` | Cohort meds |
| `OBESITY_ICD10_PREFIXES` | `E66,Z68.4` | Cohort conditions |
| `REQUIRED_LAB_NAMES` | `hemoglobin a1c,comprehensive metabolic panel,lipid panel` | Expected labs |
| `OUTREACH_TEAM_DBID` | — | Default task assignee (team) |
| `LAB_PARTNER_NAME` | — | Lab partner for order commands |
| `TASK_TITLE_PREFIX` | `GLP-1 Copilot` | Dedupe marker |

All values are parsed defensively: a malformed or missing secret falls back to its default and logs
a warning rather than breaking the card.

## 13. Out of scope for v1

- Writing anything to the chart without a click
- Patient-facing messaging or outreach automation
- Dose-titration recommendations or any dosing guidance
- Panel-wide / population reporting (this is per-patient at point of care)
- Backfilling gaps for patients whose chart is never opened

## 14. Deliverables

```
glp1-care-gap-copilot/
├── pyproject.toml
├── mypy.ini
├── tests/
│   ├── test_cohort.py           # GLP-1 / obesity matching, exclusions
│   ├── test_gaps.py             # each gap rule + threshold boundaries
│   ├── test_dedupe.py           # open-task suppression
│   ├── test_rationale.py        # PHI minimization + LLM fallback
│   └── test_handler.py          # end-to-end effect shape
└── glp1_care_gap_copilot/
    ├── CANVAS_MANIFEST.json
    ├── README.md
    ├── config.py                # secret parsing + defaults
    ├── cohort.py                # eligibility
    ├── gaps.py                  # gap detection rules
    ├── rationale.py             # LLM call + PHI-minimal payload + fallback
    ├── card.py                  # ProtocolCard assembly
    └── handlers/
        └── care_gap_handler.py  # BaseHandler for both events
```

Target: ≥90% test coverage. Tests assert PHI-minimization explicitly (the LLM payload must contain
no identifiers) and cover threshold boundaries on both sides.

---

## Resolutions (2026-08-11)

**§7 — lab relevance: confirmed deterministic.** Lab selection stays a configurable rule set; the
LLM only phrases the summary sentence. Implemented as two additional secrets beyond §12, so the
conditional rule is as configurable and auditable as everything else:

| Secret | Default | Purpose |
|---|---|---|
| `DIABETES_ONLY_LAB_NAMES` | `hemoglobin a1c` | Labs expected only with a matching diagnosis. May be set empty to make every configured lab unconditional. |
| `DIABETES_ICD10_PREFIXES` | `E11` | Diagnosis prefixes that gate the above. |

**§8 / §12 — action buttons degrade gracefully.** No runtime values were available for
`LAB_PARTNER_NAME` or `OUTREACH_TEAM_DBID`, so both are unset by default. An unresolvable lab
partner or test renders the labs gap as an informational bullet with no button; an unset outreach
team stages the task unassigned rather than dropping the button.

### Corrections found during implementation

- **§5 cohort — medication matching goes through codings, not a name field.** The SDK `Medication`
  model has no `name` attribute ([data-medication](https://docs.canvasmedical.com/sdk/data-medication/));
  the drug name lives on `MedicationCoding.display`. Cohort matching filters
  `codings__display__icontains` accordingly.
- **§5 cohort — ICD-10 codes are matched dotted and undotted.** Instances store both `Z68.41` and
  `Z6841`, so each configured prefix is matched in both forms rather than assuming one convention.
- **§4 trigger — disallowed-effect claim verified.** Canvas disallows `ADD_OR_UPDATE_PROTOCOL_CARD`
  only from `PATIENT_CHART__CONDITIONS` and `PATIENT_CHART_SUMMARY__SECTION_CONFIGURATION`
  ([effects](https://docs.canvasmedical.com/sdk/effects/)). Both chosen events are safe.

### Delivered

104 tests, 100% statement coverage, clean under `mypy` with the project's strict settings, and
`canvas validate-manifest` passes.

---

## Post-1.0.0 changes (2026-08-12)

Two parts of this specification were superseded after the first release. **Where
this document and the README disagree, the README describes what ships.**

### §9 — the LLM narrative was removed entirely (1.1.0)

The spec's §9 describes an `LlmAnthropic` call with a PHI-minimal payload and a
templated fallback. **The model path no longer exists.** It billed per render on
a path that fires for every chart open, and the fallback it degraded to was
already the sentence clinicians saw in practice, since no API key was ever set.

The narrative is now always that deterministic sentence. This also removed the
plugin's only network I/O and its only transmission to a third party, so nothing
patient-derived leaves the instance. `ANTHROPIC_API_KEY`, `LLM_MODEL`, and
`ENABLE_LLM_RATIONALE` are gone from the manifest — 12 variables remain, none
sensitive.

### §8 — lab ordering now requires explicit order codes (1.2.0)

The spec assumed the plugin could resolve which lab test to order by matching
`REQUIRED_LAB_NAMES` against the partner's catalog. **Against a real catalog that
does not work, in both directions.** On `xpc-dev`, XPC Lab lists **8** tests
containing "comprehensive metabolic panel" and **no** test named plain "lipid
panel" — so name matching either bundled every variant into one order or found
nothing at all.

`LAB_TEST_ORDER_CODES` now maps each expected lab to exactly one order code,
chosen by the operator:

```
hemoglobin a1c:496, comprehensive metabolic panel:10231, lipid panel:7600
```

This follows the same principle as §7's lab *relevance* decision: choosing
between `LIPID PANEL, STANDARD` and `LIPID PANEL, CARDIO IQ(R)` is a clinical
and contractual call for the practice, not something the plugin should infer.
Each code is re-validated against the partner catalog at render time, so a stale
mapping degrades to no button rather than an invalid command.

### Final delivered state (1.2.0)

108 tests, 100% statement **and branch** coverage, clean under strict `mypy`,
handler loads in the RestrictedPython sandbox, and every behavior above verified
against real chart data on `xpc-dev` — including duplicate suppression and lab
ordering staging exactly the two mapped tests.
