# Diagnosis: Issue #262 — "How to fix invalid entries"

## Error Message

```
bofa: 1 occurrences more than expected
```

## What the Error Means

The message `"bofa: 1 occurrences more than expected"` is rendered by the
frontend in `frontend/invalid_references.tsx:128`. It means: **for a given
transaction key, there are more matching entries already in the journal than
there are corresponding rows in the CSV source file.**

## How the Reconciliation Works

The core logic is in
`beancount_import/source/description_based_source.py:69-112`
(`get_pending_and_invalid_entries`):

1. **Scan the journal** — for every posting in a source-controlled account
   (e.g. `Liabilities:CC:BofA:Rewards`), extract a key of
   `(account, date, units, source_desc)` and count how many journal entries
   match each key.

2. **Consume against CSV rows** — for each raw CSV entry, compute the same
   key. If a journal entry with that key exists, decrement the counter (it's
   "matched"). If not, mark it as a new pending import.

3. **Check for leftovers** — after consuming all CSV rows, any journal entries
   whose counter is still > 0 are flagged as **invalid references** with
   `num_extras` set to the remaining count.

## Root Cause

The error means one of these situations:

1. **Duplicate journal entries**: The same transaction was imported into the
   journal more than once for the same CSV row. For example, the same
   `2023-01-11 * "STMTTRN - COSTCO WHSE"` with
   `source_desc: "STMTTRN - COSTCO WHSE"` and amount `15.21 USD` appears
   twice in the beancount journal, but only once in the CSV. The reconciler
   finds 2 journal postings but only 1 CSV row to match against, leaving
   `num_extras = 1`.

2. **Manually created entries with matching metadata**: A manually written
   journal entry has `source_desc` and `date` metadata matching a CSV row,
   creating an extra match the system doesn't expect.

3. **Key collision across CSV files**: If multiple CSV files in the archive
   directory contain the same transaction row (e.g., overlapping date ranges
   from different statement downloads), the deduplication logic in
   `generic_importer_source.py:72-78` should handle this. However, if the
   deduplication fails (different file order, slightly different formatting),
   the CSV side might have fewer entries than the journal side after prior
   imports.

4. **CSV rows removed or truncated**: If the archived CSV was edited or
   truncated after entries were already imported, the journal has entries
   that no longer have corresponding CSV rows.

Given the user says this affects **thousands of entries**, the most likely
cause is **scenario 1 or 3**: entries were double-imported into the journal,
or CSV deduplication across overlapping statement files isn't working
correctly.

## Key Matching Details

The key used for matching is `(account, date, units, source_desc)` — see
`source/mint.py:261` and `source/generic_importer_source.py:122-125`. Two
CSV rows on the same date, with the same description and amount, are
**indistinguishable**. Identical transactions (e.g., two $15.21 charges at
Costco on the same day) would need the `source_desc0`/`source_desc1`
mechanism to avoid collision.

## Resolution Options

1. **Check for duplicate journal entries**: Search the beancount journal for
   entries with matching `source_desc`, `date`, and amount on the flagged
   account. Remove the duplicates.

2. **Verify CSV archive integrity**: Ensure no CSV files have overlapping
   content that causes deduplication issues.

3. **Use the UI**: The invalid references panel shows the specific
   transactions. Clicking on them navigates to the journal location where
   duplicates can be identified and removed.

## Conclusion

This is not a bug in beancount-import — the system is correctly warning that
the journal contains entries that cannot be matched back to source data. The
fix is a data cleanup on the user's side.
