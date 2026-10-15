# schema-unify — reversible taxonomy consolidation (dry-run first)

A transferable mechanism: **consolidation as a dry-run-first, reversible
migration**. Fold a long tail of types into a catch-all bucket, stamp every
moved item with its legacy type so the move can be undone, soft-delete with a
TTL window before any purge. Stdlib-only, platform-agnostic.

## The three laws this tool enforces

1. **DEFAULT IS DRY-RUN.** It prints exactly what it *would* change and writes
   **nothing** to the target store unless an explicit `--apply` / `--rollback` /
   `--purge` flag is passed. Dry-run touches no store bytes and creates no
   ledger.
2. **REVERSIBLE.** Every retyped item keeps its legacy type (key configurable);
   `--rollback` restores the original type and clears the marker. Verified
   byte-identical round-trip.
3. **SOFT-DELETE + TTL.** `--apply --soft-delete` stamps a `purge_after` window;
   `--purge` refuses to delete anything still inside its window unless
   `--force`. `--restore-all` un-soft-deletes.

Every mutation is appended to `<store>.unify-ledger.jsonl`.

## Requirements

Python 3.8+. Standard library only. Runs on macOS, Linux, and Windows.

## Usage

```sh
# DRY-RUN (default) — nothing is written:
python3 schema_unify.py --adapter jsonl --target samples/demo_store.jsonl \
    --mapping mappings/demo-bloated-types.json

python3 schema_unify.py --adapter sqlite --target db.sqlite \
    --table items --type-col source --mapping mappings/demo-source.json

# WRITE (explicit):
python3 schema_unify.py --adapter jsonl --target store.jsonl \
    --mapping mappings/demo-bloated-types.json --apply --soft-delete

# UNDO / TTL lifecycle:
... --rollback        # restore legacy types
... --restore-all     # un-soft-delete
... --purge           # delete only items past their TTL (--force to override)
```

| Flag | Meaning |
|---|---|
| `--adapter {jsonl,sqlite,skills}` | storage shape |
| `--target PATH` | store file / db / skills root |
| `--mapping FILE` | rules JSON (canonical set, rules, catch_all, min_cluster_size, ttl_hours) |
| `--table`, `--id-col`, `--type-col`, `--metadata-col`, `--softdelete-col`, `--superseded-col` | sqlite shaping (all overridable) |
| `--apply` | **write** the retype plan (default is dry-run) |
| `--soft-delete` | with `--apply`: also set the TTL purge window |
| `--rollback` / `--restore-all` / `--purge [--force]` | reversal + TTL lifecycle |
| `--json` | machine-readable plan on stdout |
| `--report FILE` | write a markdown report |

## Mapping rules

```jsonc
{
  "name": "demo-source",
  "catch_all": "other",            // sink bucket for the long tail
  "legacy_key": "legacy_source",   // marker written on each moved item
  "min_cluster_size": 100,         // clusters >= this are KEPT (never silently folded)
  "ttl_hours": 72,                 // soft-delete window before purge can run
  "canonical": ["api", "cron", "task", "manual"],
  "rules": [
    {"match": "exact",  "from": "concept-redirect", "to": "concept"},
    {"match": "prefix", "from": "tweet-",           "to": "tweet"},
    {"match": "regex",  "from": "^media/.*",        "to": "media"}
  ]
}
```

Decision order per type: **catch-all (sink) → explicit rule → canonical →
`min_cluster_size` significant-cluster guard → fold to `catch_all`.** The
catch-all bucket is a *sink*, never a fold source, so re-runs are idempotent.

## Adapters

| Adapter | Reads/writes | Notes |
|---|---|---|
| `jsonl` | a `{"id","type",...}` JSON-Lines store | fully reversible; atomic rewrite |
| `sqlite` | any table; column names configurable via CLI | live-db dry-run is **read-only** (`mode=ro`) |
| `skills` | a skills tree (type == top-level category dir) | **read-only**; `--apply` is refused |

For the skills adapter, a category's `weight` is how many `SKILL.md` it holds,
so a 60-skill category is "significant" while a 1-skill category is a singleton
for the `min_cluster_size` guard.

## What lives here

| Path | What |
|---|---|
| `schema_unify.py` | engine + CLI (adapters, planner, ledgers, TTL) |
| `mappings/*.json` | generic demo rule sets |
| `samples/` | throwaway fixtures + generators (demo jsonl, demo sqlite) |
| `tests/test_lifecycle.py` | 34-check proof of the full reversible lifecycle |

## Validation

`tests/test_lifecycle.py` (34/34 checks) proves, on throwaway copies, the full
reversible lifecycle across jsonl *and* sqlite:

- dry-run: bytes unchanged, no ledger created, plan substantive
- apply: markers stamped == folded count, ledger written, idempotent (0 folds
  remain), distinct types collapse to the planned count
- rollback: markers gone, distinct types restored to original
- soft-delete: purge window stamped; restore-all clears it
- purge: retains everything inside the TTL window; `--force` removes exactly
  the soft-deleted set

Run it with:

```bash
python3 tests/test_lifecycle.py
```

## Regeneration law

Fixtures are regenerated with `samples/make_demo_store.py` and
`samples/make_demo_db.py` — never hand-edited.

## License

MIT — see [LICENSE](LICENSE).
