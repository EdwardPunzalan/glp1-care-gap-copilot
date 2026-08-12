# Database Performance Review: glp1_care_gap_copilot

**Generated:** 2026-08-12 14:11:07
**Reviewer:** Claude Code (CPA)
**Plugin version reviewed:** 0.1.2 (commit `f5d0754`)

## Hot path

The handler responds to `NOTE_OPENED` and `PATIENT_CHART__MEDICATIONS`, so it
runs **on every note open and every chart load, for every patient on the
instance**. This is a high-frequency, latency-sensitive path — a clinician is
waiting on the render — so the cost of a single render is the cost that matters.
There is no batch or cron path.

### Measured cost per render

Measured with `CaptureQueriesContext` against a real database (`tests/test_query_budget.py`):

| Scenario | Queries |
|---|---|
| Out of scope (no GLP-1 medication, no obesity diagnosis) | **2** |
| In scope, sparse chart (1 med, 1 condition, 1 obs, 1 appt) | **9** |
| In scope, loaded chart (39 observations, 39 appointments, 20 lab reports) | **9** |

**Query count is flat with respect to chart size.** A 20×-larger chart costs the
same 9 queries. This is asserted by a test, not just measured once.

The 9 queries on a full render:

1. `Medication` cohort match — `.exists()`
2. `Observation` latest weight/BMI — `.values_list(...).first()`
3. `Condition` diabetes check (gates conditional labs) — `.exists()`
4–6. `LabValue` latest result, one per configured expected lab — `.values_list(...).first()`
7. `Appointment` upcoming — `.exists()`
8. `Task` open outreach titles — `.values_list("title", flat=True)`
9. `Appointment` last visit — `.values_list(...).first()`

The out-of-scope case costs 2 queries because the cohort check runs first and
short-circuits. Since most patients on an instance are *not* on a GLP-1, this is
the case that dominates instance-wide load, and it is the cheapest path.

## Summary

| Axis | Category | Status | Issues |
|------|----------|--------|--------|
| Read count | N+1 Query Patterns | ✅ Pass | No query is issued per row of patient data |
| Read count | select_related / prefetch_related Usage | ✅ Pass | N/A by design — no FK traversal; see below |
| Memory | Over-hydration (blob columns / select_related for id only) | ✅ Pass | No model instances hydrated at all |
| Memory | Large queryset materialization / `.iterator()` / `.only()` | ✅ Pass | No `.all()` scans, no unbounded materialization |
| Memory | Cache/state accumulator | N/A | No cache or resumable state |
| Memory | Serializer contract locked | ✅ Pass | Payload allow-list enforced by test |
| Write | Redundant writes / content-hash guard | N/A | Plugin performs **zero** database writes |
| Write | Bounded reconcile / idempotent sync | N/A | No sync, webhook, or reconcile path |
| Exec limit | Custom-data PK (`dbid` vs `id`) | N/A | No custom-data models |
| Exec limit | Effect-batch ceiling | ✅ Pass | Exactly one effect returned, never queryset-proportional |

## Detailed Findings

### N+1 Query Patterns — ✅ Pass

No query is executed per row of patient data. Verified by measurement: the same
9 queries serve a chart with 4 records and one with 98.

There is one query-in-a-loop, at `gaps.py:169-170`:

```python
for lab_name in expected:
    last_result = latest_lab_datetime(patient_id, lab_name)
```

**This is bounded by configuration, not by patient data** — `expected` is derived
from `REQUIRED_LAB_NAMES` (default: 3 entries). It does not grow with the number
of lab reports the patient has, which is the property that makes N+1 dangerous.
See *Recommendations* for the deliberate trade-off and its one caveat.

### select_related / prefetch_related — ✅ Pass (N/A by design)

Neither is used, and neither is needed. The plugin never loads a model instance
and then traverses a foreign key to read fields off it. Every read is a
projection: `.exists()` for booleans, `.values_list(...).first()` for a single
scalar, `.values_list(..., flat=True)` for one column.

Relations are crossed inside the **query predicate** rather than in Python —
e.g. `LabValue.objects.filter(report__patient__id=...)` and
`.values_list("report__date_performed", flat=True)` join to `LabReport` in SQL
and return a bare datetime. Adding `select_related` here would hydrate rows the
code never touches.

### Over-hydration — ✅ Pass

No `Note` access, so `Note._body` is never loaded. No `*_json`, `*_html`,
`payload`, or document blob column is read anywhere. (`payload` appears in
`rationale.py` only as the name of the in-memory LLM dict, not a DB column.)

`dedupe.py:32` calls `list()` on a queryset, but on
`.values_list("title", flat=True)` pre-filtered to titles beginning with the
plugin's own task prefix — a short list of strings, not model instances.

### Unbounded Queries — ✅ Pass

No `.objects.all()` anywhere. Every query is filtered to a single patient except
the two `LabPartner` / `LabPartnerTest` lookups, which read an instance-level
catalog (typically tens of rows) and only run when a lab partner is configured
*and* a labs gap is open.

### Write Amplification — N/A

The plugin performs **no database writes at all**. No `.save()`, `.create()`,
`.update()`, or `.delete()` appears in the source. Its only mutation of instance
state is the single protocol-card effect, and any chart write happens through a
command a clinician explicitly commits.

### Effect-Batch Ceiling — ✅ Pass

`compute()` returns either `[]` or a single-element list containing one
`ADD_OR_UPDATE_PROTOCOL_CARD`. The card carries at most three recommendations —
one per gap rule — so the payload is small and fixed. Nothing is proportional to
a queryset, and `patient_filter` (the fan-out mechanism) is never used.

### Test Realism — ✅ Pass

The ORM is not mocked. All 111 tests exercise real queries against a real
database via SDK factories, so a field-name or query error surfaces in CI rather
than passing against a mock. The one `patch()` in the handler tests targets
`detect_gaps` to simulate a failure, and `test_rationale.py` mocks only the
LLM HTTP client — neither substitutes a queryset.

## Recommendations

| Priority | Issue | Location | Recommendation | Status |
|----------|-------|----------|----------------|--------|
| LOW | One query per configured expected lab | `gaps.py:169` | Keep as-is; document the scaling caveat | ✅ Documented |

### On the per-lab query — why it stays

Collapsing the three lab lookups into one query would mean fetching the
patient's lab values and matching names in Python. That trades 2 saved queries
for loading every lab row the patient has — the exact over-hydration this review
guards against, and unbounded in the one dimension that actually varies between
patients.

The current shape keeps each lookup a single indexed scalar fetch
(`.values_list(...).first()`) whose cost does not depend on lab history depth.
Three cheap queries beat one query that hydrates an unbounded result set.

**Caveat worth knowing:** `REQUIRED_LAB_NAMES` is operator-configurable, so the
count scales 1:1 with how many labs are configured. At the default of 3 this is
negligible. A deployment configuring 30 labs would issue 30 queries per render
on a hot path — at that point the Python-side matching trade-off inverts. This
is now documented in the README under Performance.

## Verdict

**✅ PASS** — no HIGH or MEDIUM issues.

The plugin is read-only, hydrates no model instances, performs no writes, and
holds a fixed 9-query budget that is provably flat with respect to chart size.
The cheapest path (2 queries) is the one taken for the majority of patients,
which is the right shape for something that runs on every chart open.

A query-budget regression test now pins these numbers, so a future change that
introduces a per-row query fails in CI rather than in production.
