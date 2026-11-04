# Matching search: independent design assessment

Analysis of upstream `3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4`, prepared after reading the original matching implementation, its grouping/performance history, issue #260, and PR #224. This is a proposal, not an implementation. No private journal was used.

The companion [matching history](2026-11-04-matching-history.md) gives the fuller chronology; this assessment was formed independently before reading that report.

## Recommendation

Treat this as an interactive search with an explicit incomplete state. Preserve the existing matching rules, give ordinary searches their existing exhaustive answers, and pause unusually large searches after a bounded amount of work. Show valid answers found so far, keep the original transaction available, and offer an explicit continuation. Schedule alternatives fairly; never silently call the first 1,000 depth-first results the best 1,000.

That is the best attainable direction if retaining access to unusual legitimate matches matters. It costs more engineering than a cap. A small cap can be a useful first mitigation, but it must disclose truncation and must not claim a general latency bound. In particular, an outer transaction-state cap alone cannot prevent an individual pair of transactions from consuming seconds or exhausting memory.

Do not start by rewriting the matching rules, dropping to the nearest five transactions, or replacing the matcher with a beam. Those approaches exchange recall for speed before establishing which matches are safe to discard. If that product tradeoff is wanted, describe it explicitly and provide an opt-in wider search.

## What the history says

The [2016 initial implementation](https://github.com/jbms/beancount-import/commit/3fa3951236927c2eff2b95b42a64dcad3b12c77a) already describes recursive multi-transaction merging, alternate ways to merge a pair, cleared versus uncleared postings, aggregate posting subsets, and deduplication of alternate search paths. This is deliberately more general than pairing two bank records.

The [fuzzy-amount change](https://github.com/jbms/beancount-import/commit/2facf06bcd1fce6b01d224737da09470991df82f) addresses a real payroll/retirement-account case. The [mixed-sign grouping change](https://github.com/jbms/beancount-import/commit/02cd249a996e04b38c039ce26181a9ac09724c44), [zero-subset exclusion](https://github.com/jbms/beancount-import/commit/d1ce74d1ec8e18021cf9b670350319216783c6d4), and [same-sign maximal groups](https://github.com/jbms/beancount-import/commit/b5576e2f34f4446411d7994abc139b4b40bf04d4) serve invoices with discounts, tax, and larger groups. A small fixed transaction or posting count cannot be justified solely by the subway example.

The [original aggregate cap](https://github.com/jbms/beancount-import/commit/4514b5563cd5bf96fdebdec977b6631562374c21) and [aggregate cache](https://github.com/jbms/beancount-import/commit/73ffb6efefd88110ebca6d87a6fc392110b55b4b) acknowledge an earlier combinatorial problem. The cache author reports a reduction from over 7,000 seconds to 240 seconds in one case: an enormous improvement that still does not make interactive latency acceptable.

[PR #214](https://github.com/jbms/beancount-import/pull/214) halves indexing time through reuse of keys and hoisting work. [PR #210](https://github.com/jbms/beancount-import/pull/210) corrects a fuzzy-search boundary and adds tests that hit the previously untested nonzero lower bound. More recently, [#275](https://github.com/jbms/beancount-import/commit/a6351d68a0424215bd0caeb9fdac6f5ad8d16309) stops constructing an aggregate subset size once that entire size is already destined for rejection, and [#276](https://github.com/jbms/beancount-import/commit/43f5f385221c2c98733b57a919918d81be79ccab) builds aggregate and fuzzy-date index work on demand. These preserve existing answers; they do not bound the recursive transaction graph. [#286](https://github.com/jbms/beancount-import/commit/7796a2ce3eaffed405c0dcb0b0d6c9265fa21cfc) makes date/currency sorting handle incomplete currencies, a reminder that supposedly universal ordering keys need tests for partially specified postings.

The existing algorithm is already heuristic: partial aggregate groups stop at four postings, larger same-sign maximal groups get special treatment, a capacity limit discards whole subset sizes, opposite known-account postings inhibit merges, and dominated match sets are removed. In this proposal, “complete” means complete relative to those current rules and the current deduplication semantics, not every mathematically conceivable reconciliation.

## The current failure is reproducible without aggregate postings

[Issue #260](https://github.com/jbms/beancount-import/issues/260) reports thirteen similar subway entries, roughly eleven seconds of candidate computation, and suggests 1,000 candidates or a small partner shortlist. A synthetic case on the current checkout reproduces its scale:

| Similar transactions, including the selected one | Merged candidates | Candidates including original | Matcher elapsed seconds |
| --- | ---: | ---: | ---: |
| 4 | 7 | 8 | 0.0062 |
| 7 | 63 | 64 | 0.0735 |
| 10 | 511 | 512 | 0.8510 |
| 13 | 4,095 | 4,096 | 10.9331 |

These are one-run measurements using system Python 3.13, not a benchmark guarantee. Only `get_extended_transactions` was timed; account prediction, staging, serialization, and rendering were excluded. Every transaction has two uncleared postings with distinct accounts, so no multi-posting aggregate is needed. Every resulting transaction looks the same, but consumes a different subset of source entries. The first-ranked answer consumes all N entries.

Reproduction:

```python
import datetime
import time
from decimal import Decimal
from beancount.core.amount import Amount
from beancount.core.data import Posting, Transaction
from beancount_import import matching

for n in (4, 7, 10, 13):
    entries = [Transaction(
        {}, datetime.date(2026, 1, 1), '*', None, 'Subway',
        frozenset(), frozenset(), [
            Posting('Assets:Cash', Amount(Decimal('-3'), 'USD'),
                    None, None, None, {}),
            Posting('Expenses:FIXME', Amount(Decimal('3'), 'USD'),
                    None, None, None, {}),
        ]) for _ in range(n)]
    db = matching.PostingDatabase(3, Decimal('.01'), lambda p: False, frozenset())
    for entry in entries:
        db.add_transaction(entry)
    start = time.perf_counter()
    results = matching.get_extended_transactions(entries[0], db)
    print(n, len(results), time.perf_counter() - start,
          len(results[0].used_transactions))
```

The `2^(N-1)` count includes the original; merged results alone number `2^(N-1)-1`. Source provenance is not cosmetic: accepting a candidate removes or replaces the entries it consumes. Deduplicating these candidates solely by rendered transaction text would change the operation the user approves. See [candidate acceptance](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/reconcile.py#L1153).

## Three separate expensive layers

1. **Aggregate generation and lookup.** Eligible subsets of postings within one transaction become effective postings. Current lazy indexing reduces unused work, but the first relevant lookup can still trigger substantial aggregate work. The cap counts accepted aggregates, so many rejected subsets can still require computation; it is not a general instruction/time budget.
2. **Pair merging.** Matching one transaction against another enumerates disjoint posting correspondences, balanced combinations, and a product across currencies. These are largely materialized lists, not an interruptible stream.
3. **Recursive transaction search.** Every new merged state can merge another original transaction. Deduplication avoids alternate paths to an already-seen state but does not erase the exponentially many distinct used-source subsets.

The first layer has received several optimizations; #260 can live entirely in the third. A large Amazon pair can fail in the second. One global candidate-count setting does not address all three. See [aggregate implementation](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/matching.py#L887), [pair search](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/matching.py#L1283), and [recursive search](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/matching.py#L1839).

## Why the previous patch is not a complete answer

[Open PR #224](https://github.com/jbms/beancount-import/pull/224) explicitly accepts missing a valid combination to prevent CPU seizure. Its patch does two things:

- For an account/currency group with at least 25 postings, stop considering partial aggregate subsets and retain the eligible same-sign maximal subsets.
- In the outer generator loop, recurse only while `level <= 6`.

This is not a transaction-partner breadth limit. Starting at level zero, that condition permits seven added transactions, or eight total. It is checked after the generator has already computed and yielded the child, so the boundary state still computes single-step extensions even when none will be visited. It also leaves arbitrarily many alternative partners at every allowed depth. It can help the cited workloads but is not a reliable complexity bound and does not handle the common root-width problem.

## Guarantees that are and are not possible

An algorithm cannot materialize an exponential number of distinct answers in a fixed amount of time and memory. The synthetic example supplies the output-size lower bound directly; no complexity-theory assumption is needed.

It is possible to bound **one interactive request's work** while retaining a continuation that eventually enumerates the same finite result set. That does not bound total time, total answers, or total retained frontier/deduplication storage. Strict bounded RAM plus indefinite exact continuation needs spilling to disk, recomputation, or a different representation. A finite session memory ceiling should therefore pause with an honest resource-limit state, not drop branches and report completion.

Similarly, returning the exact global best K under the existing score without searching everything requires a valid upper bound on every unexplored branch. No such bound exists in the current implementation. A promising partial score is not that bound: further merges improve match counts, and locally weak states can lead to the best completed result. A beam or top-N partner list is an approximation.

## Search contract and scheduling

Introduce a search session that owns its snapshot, work frontier, deduplication state, emitted candidates, and counters. Each advance returns a bounded batch and one explicit status: complete, paused with continuation, cancelled, or stopped by a resource limit. Keep “number found” separate from an unknown total. Reject stale continuations after the underlying snapshot changes.

Use a deterministic work-unit budget for repeatable tests and a cooperative elapsed-time deadline for responsiveness. Count attempted work as well as accepted states: a search that rejects millions of combinations must still stop. Candidate-count and memory ceilings are additional limits, not substitutes for the work budget. Check cancellation inside expensive aggregate, match-set, balancing, and cross-currency loops, not just after a complete child is returned.

For fair initial coverage, visit simple one-partner alternatives before letting the first promising path consume the whole budget. Schedule resumable expansion tasks round-robin, with an explicit preference for shallow states and bounded opportunities for deeper states. Do not allocate one unbounded queue to every child in advance. A pure breadth-first traversal is easy to explain but can defer legitimate deep matches behind an enormous layer and retains a large frontier; a pure best-first traversal can concentrate all work on the repeated-entry family. Exact scheduling policy should be chosen against fixtures, not assumed from one case.

A depth or partner-count restriction may be an explicit optional search mode, but it is a semantic restriction. A paused search should preserve undiscovered alternatives for continuation. Repeated continuation over a small fixed snapshot must converge to the exhaustive oracle's result set. A memory ceiling that prevents that must be reported distinctly.

Keep the current presentation score initially, but describe partial results as **best found so far**. It sorts by cleared matches, uncleared matches, then fewer removals; it does not encode likelihood or a preference for minimal source consumption. The synthetic case exposes that distinction. Changing ranking to penalize source count or use description/date evidence is a separate product decision with different regressions; do not smuggle it into a performance patch.

## Important barriers to a naive lazy rewrite

The [pair matcher](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/matching.py#L1477) filters dominated balanced match sets only after finding them. A match set emitted early may later be dominated and absent from the exhaustive answer. Merely replacing `append` with `yield` can broaden semantics.

The [single-step generator](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/matching.py#L1737) only permits opposite-unknown removal for a posting after establishing that no ordinary merged result matched it across all examined partner transactions. Stopping that scan early and treating “not found yet” as “absent” can invent removal candidates. A continuation must preserve this unfinished negative-information check.

Therefore the first safe resumability refactor can retain completion barriers: resumably compute a pair's internal results, finalize dominance, then publish that pair's final results; delay conditional removals until the ordinary-match scan is complete. This preserves semantics but means an exceptionally large first pair may delay useful merged answers even while the UI remains responsive. Producing earlier provisional results with later retraction is possible, but creates a much more complicated acceptance contract and is not my recommendation.

## Identity and reproducibility

Current state identity contains the used transaction IDs and a frozenset of normalized postings. Retain source provenance. Two visually identical source transactions need distinct stable occurrence IDs; a content hash alone is insufficient. Use snapshot-scoped source ordinals, with a generation token, for external candidate IDs and deterministic tie breakers. Existing in-process `id()` values must not become persistent continuation or UI identities.

Do not casually “improve” the existing state key while changing traversal. Its posting frozenset discards multiplicity, and it omits transaction-level metadata and score counters. Whether every conflated representative is future-equivalent needs its own audit. At minimum test complete transaction content, multiplicities, scores, source consumption, and subsequent extensions when comparing the new search to the old one. Reordering traversal can change which representative wins a deduplication collision, so equal result counts alone are weak evidence.

## UI, cancellation, and cache invalidation

Initial reconciler loading is asynchronous, but interactive candidate generation is currently synchronous: [webserver `handle_skip` calls `get_next_candidates`](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/beancount_import/webserver.py#L566), which calls the matcher, predicts accounts for all candidates, and builds all staged changes before publishing state. An outer generator with `list(...)` at its caller changes no user-visible latency.

The issue describes hovering, but current pending-entry mouse hover only changes local highlighting; selection/skipping triggers this server computation. The current candidate component also renders the full list. See [pending interaction](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/frontend/pending.tsx#L135) and [candidate rendering](https://github.com/jbms/beancount-import/blob/3ee9c4045d29d47be5b1ea08a8f1b23f041db2b4/frontend/candidates.tsx#L483).

Advance a cooperative session in short event-loop work slices, or use a serialized worker with a safely owned snapshot. Do not simply launch unbounded background threads: lazy indexes and caches mutate on reads, accepting a transaction changes the database, and Python thread cancellation does not interrupt a running search. Worker-process hard termination is a possible backstop, but serializing/rebuilding the database has costs and a killed process cannot resume arbitrary in-flight local work without checkpoints.

Start with one active computation, coalesce superseded selections, and cancel obsolete work promptly. Separate journal/source/config revision, search session ID, and request sequence. Check them both when publishing and when accepting a candidate. Reuse the existing generation checks but do not treat them as cancellation. Accepting/importing, reloading, source changes, or matching-rule changes invalidate relevant sessions; retraining invalidates prediction/staging caches even if raw match results remain reusable under a separately defined revision.

Materialize predictions, associated data, stages, and formatted payload only for displayed pages. Use stable candidate IDs for selection and edits: the current index-based acceptance must not accidentally approve a different candidate if later results reorder the list. Freeze already displayed rows or preserve selected IDs across reranking. The original transaction should remain immediately available. An incomplete search should say “More matches may exist” and offer “Search more”; it must never say “No matches” when the search merely exhausted its budget.

## Suggested delivery sequence

1. Capture the synthetic regression and exhaustive small fixtures. Instrument state attempts, unique states, pair work, aggregate work, candidate materialization, peak retained state, and first-page latency. Freeze the semantic baseline.
2. Add explicit partial-result plumbing and an outer work/state guard as a narrow, honest mitigation for #260, with deterministic fair traversal and the original always present. A default such as 1,000 is a provisional operational setting to benchmark, not a proof of quality or responsiveness. Label the remaining unbounded-pair limitation in the change description.
3. Make inner work cancellable/resumable while respecting dominance and negative-information barriers, and connect bounded work slices or owned workers to the UI. Add continuation and lazy display materialization. Only after this layer is covered claim a responsive search budget, and still qualify pathological oversized individual primitive operations.
4. Consider better ordering, compact repeated-entry presentation, or optional heuristic pruning separately, using real anonymized/synthetic cases and declared recall criteria.

If the desired scope is strictly one small upstream patch, stop after the explicitly disclosed mitigation rather than pretending it solves the whole architecture. If the requirement is “no lost valid candidates and no UI freezes,” the session/lifecycle work is necessary scope, not optional polish.

## Validation that would justify shipping

- Compare full bounded-search continuations with the old exhaustive matcher on small cases, including exact provenance, output multiplicities, score order and tie behavior where promised. Keep current matching goldens, fuzzy bounds, mixed signs, zero subsets, cleared/uncleared constraints, costs, multiple currencies, and missing currencies.
- Repeat the N-entry fixture at safe sizes. Assert a budget terminates based on deterministic work counts, the original survives, incomplete is true, and immediate alternatives are not all displaced by one deep branch. Keep wall-time measurements as benchmarks rather than brittle unit assertions.
- Place the correct answer beyond the first K partners and beyond a proposed depth restriction. It must remain discoverable by continuation. Separately test a weak-looking intermediate state whose descendant ranks above early answers.
- Exercise duplicate visible transactions with different source occurrences, repeated equal postings within a transaction, and representatives with different relevant metadata. Verify that accepting a candidate consumes exactly its displayed sources.
- Construct one oversized pair, including a late dominating match and a late regular match that suppresses an unknown-removal edge. Cancellation/pausing must not publish candidates the exhaustive matcher rejects.
- Cancel while building lazy aggregates and while materializing a pair; reload or accept a different candidate before the worker responds. Stale results, selections, and continuations must be rejected, and no obsolete work may mutate the live index.
- Compare batch sizes, cold/warm caches, database insertion orders, and fresh processes for the defined deterministic order. Separate snapshot determinism from intentionally unspecified order changes.
- Verify memory-limit status, expired continuations, rapid navigation, no-match complete versus no-match incomplete, stable selection across appended pages, and prediction/staging work proportional to displayed results.

The central tradeoff is honest: exhaustive discovery can remain available, but it cannot be both instantaneous and globally resource-bounded for these inputs. The UI should make that computational fact manageable without silently redefining which reconciliations are possible.
