# Database Performance Review: glp1_care_gap_copilot

**Generated:** 2026-08-12 15:15:51
**Reviewer:** Claude Code (CPA)
**Plugin version reviewed:** 1.2.0 (commit `ef03c19`)

> Supersedes `db-performance-review-20260812-141107.md`, which reviewed 0.1.2.
> Re-measured because 1.2.0 changed the lab-ordering query shape.

## Hot path

Unchanged: the handler responds to `NOTE_OPENED` and
`PATIENT_CHART__MEDICATIONS`, so it runs on **every note open and every chart
load, for every patient on the instance**, with a clinician waiting on the
render. There is no batch or cron path, and as of 1.1.0 there is no network I/O
either — the render is pure database work.

### Measured cost per render

Measured with `CaptureQueriesContext` against a real database:

| Scenario | Queries |
|---|---|
| Out of scope (no GLP-1 medication, no obesity diagnosis) | **2** |
| In scope, sparse chart | **9** |
| In scope, loaded chart (39 observations, 39 appointments, 20 lab reports) | **9** |
| In scope, **lab ordering configured**, labs gap open | **11** |

**Query count remains flat with respect to chart size** — a 20×-larger chart
costs the same 9 queries. `tests/test_query_budget.py` asserts this.

### What changed at 1.2.0

Lab-test resolution moved from `order_name__icontains` matching to an explicit
`LAB_TEST_ORDER_CODES` map. The query *count* on that path is unchanged at two
(one `LabPartner` lookup, one `LabPartnerTest` validation), but both are now
cheaper and better-bounded:

- The catalog query changed from a chain of `icontains` `Q` objects over
  `order_name` to `order_code__in=[...]` — an indexed exact-match lookup against
  a list whose length is fixed by configuration.
- The result set is bounded by the number of *configured* codes rather than by
  how many catalog entries happen to contain a substring. On the real XPC Lab
  catalog the old form matched 8 rows for "comprehensive metabolic panel"; the
  new form matches at most one row per configured lab.

These two queries only run when a labs gap is open **and** both
`LAB_PARTNER_NAME` and `LAB_TEST_ORDER_CODES` are configured. Unconfigured
instances — the default — stay at 9.

The 1.1.0 LLM removal did not change query count, but it removed the render's
only network call, so per-render latency is now bounded entirely by these
queries.

## Summary

| Axis | Category | Status | Notes |
|------|----------|--------|-------|
| Read count | N+1 Query Patterns | ✅ Pass | No query issued per row of patient data |
| Read count | select_related / prefetch_related | ✅ Pass | N/A by design — no FK traversal, all reads are projections |
| Memory | Over-hydration | ✅ Pass | No model instances hydrated anywhere |
| Memory | Large queryset materialization | ✅ Pass | No `.all()` scans, no unbounded materialization |
| Memory | Cache/state accumulator | N/A | No cache or resumable state |
| Memory | Serializer contract locked | ✅ Pass | Card payload is fixed-shape, ≤3 recommendations |
| Write | Redundant writes | N/A | **Zero** database writes |
| Write | Bounded reconcile | N/A | No sync, webhook, or reconcile path |
| Exec limit | Custom-data PK (`dbid` vs `id`) | N/A | No custom-data models |
| Exec limit | Effect-batch ceiling | ✅ Pass | Exactly one effect, never queryset-proportional |

## Detailed Findings

### N+1 Query Patterns — ✅ Pass

No query is executed per row of patient data, verified by measurement rather
than inspection: the same 9 queries serve a chart with 4 records and one with 98.

One query-in-a-loop remains at `gaps.py:169` — `latest_lab_datetime` is called
once per configured expected lab. **This is bounded by configuration, not by
patient data** (default: 3), which is the property that makes N+1 dangerous. See
*Recommendations* for the trade-off and its caveat.

### select_related / prefetch_related — ✅ Pass (N/A by design)

Neither is used and neither is needed. The plugin never loads a model instance
and traverses a foreign key. Every read is a projection: `.exists()` for
booleans, `.values_list(...).first()` for a single scalar,
`.values_list(..., flat=True)` for one column. Relations are crossed inside the
query predicate (`report__patient__id`, `report__date_performed`) so the join
happens in SQL and a bare scalar comes back.

### Over-hydration — ✅ Pass

No `Note` access, so `Note._body` is never loaded. No blob column is read
anywhere. `dedupe.py` calls `list()` on a queryset, but on
`.values_list("title", flat=True)` pre-filtered to the plugin's own task prefix —
a short list of strings.

### Write Amplification — N/A

No `.save()`, `.create()`, `.update()`, or `.delete()` in the source.

### Effect-Batch Ceiling — ✅ Pass

`compute()` returns `[]` or a single-element list containing one
`ADD_OR_UPDATE_PROTOCOL_CARD`, carrying at most three recommendations. Nothing
scales with a queryset.

### Test Realism — ✅ Pass

The ORM is not mocked. All 108 tests exercise real queries against a real
database via SDK factories, so a field-name or query error surfaces in CI rather
than passing against a mock.

## Recommendations

| Priority | Issue | Location | Recommendation | Status |
|----------|-------|----------|----------------|--------|
| LOW | One query per configured expected lab | `gaps.py:169` | Keep as-is; document the scaling caveat | ✅ Documented in README |

### On the per-lab query — why it stays

Collapsing the lab lookups into one query would mean fetching the patient's lab
values and matching names in Python — trading 2 saved queries for loading every
lab row the patient has, which is unbounded in the one dimension that actually
varies between patients. Three cheap indexed scalar fetches beat one query that
hydrates an unbounded result set.

**Caveat:** `REQUIRED_LAB_NAMES` is operator-configurable, so the count scales
1:1 with it. At the default of 3 this is negligible; a deployment configuring 30
labs would issue 30 queries per render on a hot path, at which point the
trade-off inverts.

## Verdict

**✅ PASS** — no HIGH or MEDIUM issues.

Read-only, no writes, no model hydration, no network I/O, and a fixed query
budget that is provably flat with respect to chart size. The cheapest path
(2 queries) is the one taken for the majority of patients, which is the right
shape for something that runs on every chart open. A regression test pins these
numbers so a future per-row query fails in CI rather than in production.
