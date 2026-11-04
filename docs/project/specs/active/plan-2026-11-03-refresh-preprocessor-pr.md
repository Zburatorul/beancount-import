# Bring Darren’s deterministic preprocessor PR to merge readiness

Recommended approach: prepare a focused, tested refresh of [PR #243](https://github.com/jbms/beancount-import/pull/243), then coordinate on that existing PR. Preserve Darren’s authorship and keep personal rules and data out of the contribution.

## What we know

Darren’s November 2024 PR implements the exact framework used by our fork: the callback hook, `BasePreprocessor`, `PreprocessingRule`, and `TransactionRuleMixin`. It addresses [issue #241](https://github.com/jbms/beancount-import/issues/241). The PR remains open, with no recorded reviews or checks on its current head.

Our fork adds posting-order independence and typing corrections. Our `det.py` adds personal rules and specialized automation on top; those do not belong in this PR.

Darren [reported a possible pending-state/reference problem](https://github.com/jbms/beancount-import/pull/243#issuecomment-2525199412). We have not reproduced or ruled it out. Its cause should be established with tests rather than inferred from that comment.

Your GitHub account has upstream write access, and the PR permits maintainer edits. Updating the original contributor branch appears feasible; verify access before attempting that update.

## 1. Prepare the patch locally

Start from current upstream in an isolated branch and apply Darren’s original commit, preserving his author attribution. Put our fixes in separate commits. Use the existing PR as the source, rather than extracting a patch from the whole personal fork.

Keep the scope to the preprocessing hook, helper classes, simple rule matcher, documentation, and tests. Include the posting-order fix and any correctness fixes demonstrated by the investigation. Leave classifier changes, smart_importer integration, Amazon features, and personal configuration for separate work.

The hook should remain opt-in, with existing imports behaving identically when no preprocessor is configured. Document when it runs, which state is available, and how callbacks persist changes.

## 2. Prove correctness at the state and persistence boundaries

Use temporary journals and invented transactions. Cover these behaviors:

- Recording removes the pending entry, writes it once, and makes it available for subsequent matching.
- Ignoring removes the pending entry and writes it once to the ignore journal.
- Modifying replaces the pending entry and updates its matching-index membership consistently.
- Multiple directives and output files are handled without repeating previously staged writes.
- Reloading or repeating an import does not offer already recorded or ignored entries again.
- Unmatched entries remain available for normal UI review; posting order does not affect rule matching.

Inspect failure handling too: a failed journal write should not silently discard the pending transaction from the active state. These are investigation targets, not confirmed defects.

Where a test exposes a defect, fix the underlying state transition and retain the regression test. Reuse the reconciler’s existing staging mechanisms where practical.

Add a small documented example showing one rule and how to supply the callback. Run the full Python suite, mypy, and the supported CI matrix.

## 3. Coordinate and update #243

Once the patch and validation are reviewable, summarize the refresh on Darren’s existing PR: preserved design, specific fixes, tests, and the outcome of his state-update concern. Credit his original implementation and invite his input.

Prefer updating #243 so its discussion and attribution stay together. If its branch cannot be updated, offer the prepared commits to Darren. A replacement PR is the fallback; cross-link it and retain his authored commit.

Get review of the hook’s lifecycle and persistence behavior, then merge once the agreed changes and CI pass. No GitHub messages or branch updates have been made as part of this plan.

## What follows

After the framework is accepted, propose `build_classifier=False` as a focused optimization for rules-only callers. At that point it supports an upstream workflow reviewers can actually run. A general headless runner could follow if users need one; publishing our personal `det.py` is unnecessary.

## Completion criteria

The accepted PR provides an opt-in, documented preprocessor and simple rules framework, has regression coverage for state consistency and reloads, passes supported checks, and retains Darren’s credit. Our local deterministic workflow should then depend on that upstream framework with only personal customization remaining locally.

---
comments:
  c1:
    body: looks good, go ahead
    by: user
    at: 2026-11-04T01:33:19.476Z
