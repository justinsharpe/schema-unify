#!/usr/bin/env python3
"""test_lifecycle.py — proves the schema-unify reversible lifecycle end-to-end
on THROWAWAY copies (never the pristine samples, never live stores).

Exercised:
  * dry-run writes nothing (target bytes unchanged, no ledger created)
  * apply retypes + stamps legacy marker; idempotent on re-run
  * rollback restores original types and clears markers
  * soft-delete stamps a purge window; restore-all clears it
  * purge retains items inside the TTL window; --force removes them

Exit 0 on success, 1 on any failed assertion. Stdlib only; run:
    python3 tests/test_lifecycle.py
"""
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CLI = os.path.join(ROOT, "schema_unify.py")
SAMPLES = os.path.join(ROOT, "samples")

FAILS = []


def check(name, cond, extra=""):
    print(("  [ ok ] " if cond else "  [FAIL] ") + name + (("  " + extra) if extra else ""))
    if not cond:
        FAILS.append(name)


def run(args):
    return subprocess.run([sys.executable, CLI] + args,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def dry_json(args):
    r = run(args + ["--json"])
    if r.returncode != 0:
        raise AssertionError("cli failed: %s\n%s" % (r.returncode, r.stderr.decode()))
    return json.loads(r.stdout.decode())


def sha(path):
    import hashlib
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        h.update(fh.read())
    return h.hexdigest()


def read_jsonl(path):
    out = []
    with open(path, "r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                out.append(json.loads(line))
    return out


def test_jsonl(tmp):
    print("\n== jsonl adapter lifecycle ==")
    store = os.path.join(tmp, "store.jsonl")
    shutil.copy(os.path.join(SAMPLES, "demo_store.jsonl"), store)
    mapping = os.path.join(ROOT, "mappings", "demo-bloated-types.json")
    ledger = store + ".unify-ledger.jsonl"

    before_sha = sha(store)
    plan = dry_json(["--adapter", "jsonl", "--target", store, "--mapping", mapping])
    check("dry-run: bytes unchanged", sha(store) == before_sha)
    check("dry-run: no ledger created", not os.path.exists(ledger))
    check("dry-run: plan is substantive", plan["items_folded"] > 0,
          "folds=%d types %d->%d" % (plan["items_folded"], plan["distinct_before"], plan["distinct_after"]))
    n_folded = plan["items_folded"]

    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--apply"])
    check("apply: exit 0", r.returncode == 0)
    recs = read_jsonl(store)
    marked = [x for x in recs if "legacy_type" in x]
    check("apply: legacy markers stamped == folded count", len(marked) == n_folded,
          "%d vs %d" % (len(marked), n_folded))
    check("apply: ledger written", os.path.exists(ledger) and len(read_jsonl(ledger)) > 0)
    after = dry_json(["--adapter", "jsonl", "--target", store, "--mapping", mapping])
    check("apply: idempotent (0 folds remain)", after["items_folded"] == 0)
    check("apply: distinct types collapsed", after["distinct_before"] == plan["distinct_after"],
          "%d" % after["distinct_before"])

    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--rollback"])
    check("rollback: exit 0", r.returncode == 0)
    recs2 = read_jsonl(store)
    restored = dry_json(["--adapter", "jsonl", "--target", store, "--mapping", mapping])
    check("rollback: no legacy markers left", all("legacy_type" not in x for x in recs2))
    check("rollback: distinct types restored", restored["distinct_before"] == plan["distinct_before"],
          "%d vs %d" % (restored["distinct_before"], plan["distinct_before"]))

    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping,
             "--apply", "--soft-delete"])
    check("soft-delete: exit 0", r.returncode == 0)
    recs3 = read_jsonl(store)
    sd = [x for x in recs3 if x.get("_deleted_at")]
    check("soft-delete: items stamped with purge window", len(sd) == n_folded and all(x.get("_purge_after") for x in sd))
    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--restore-all"])
    check("restore-all: exit 0", r.returncode == 0)
    check("restore-all: cleared", all("_deleted_at" not in x for x in read_jsonl(store)))
    run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--rollback"])
    run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--apply", "--soft-delete"])
    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--purge"])
    check("purge (in window): retains everything", r.returncode == 0 and
          len(read_jsonl(store)) == len(recs3), r.stdout.decode().strip()[:80])
    r = run(["--adapter", "jsonl", "--target", store, "--mapping", mapping, "--purge", "--force"])
    left = read_jsonl(store)
    check("purge --force: soft-deleted removed", len(left) == len(recs3) - n_folded,
          "%d left (expected %d)" % (len(left), len(recs3) - n_folded))


def test_sqlite(tmp):
    print("\n== sqlite adapter lifecycle ==")
    db = os.path.join(tmp, "uni.db")
    shutil.copy(os.path.join(SAMPLES, "demo_store.db"), db)
    mapping = os.path.join(ROOT, "mappings", "demo-source.json")
    ledger = db + ".unify-ledger.jsonl"

    def rows(sql):
        conn = sqlite3.connect(db)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    total = rows("SELECT COUNT(*) FROM items")[0][0]
    before_sha = sha(db)
    plan = dry_json(["--adapter", "sqlite", "--target", db, "--table", "items",
                     "--type-col", "source", "--mapping", mapping])
    check("sqlite dry-run: bytes unchanged", sha(db) == before_sha)
    check("sqlite dry-run: no ledger created", not os.path.exists(ledger))
    check("sqlite dry-run: plan substantive", plan["items_folded"] > 0,
          "folds=%d types %d->%d" % (plan["items_folded"], plan["distinct_before"], plan["distinct_after"]))
    n_folded = plan["items_folded"]

    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--apply"])
    check("sqlite apply: exit 0", r.returncode == 0)
    got = rows("SELECT COUNT(*) FROM items WHERE metadata_json LIKE '%legacy_source%'")[0][0]
    check("sqlite apply: legacy_source stamped == folded", got == n_folded, "%d vs %d" % (got, n_folded))
    distinct = rows("SELECT COUNT(DISTINCT source) FROM items")[0][0]
    check("sqlite apply: distinct sources collapsed", distinct == plan["distinct_after"], "%d" % distinct)

    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--rollback"])
    check("sqlite rollback: exit 0", r.returncode == 0)
    left_marked = rows("SELECT COUNT(*) FROM items WHERE metadata_json LIKE '%legacy_source%'")[0][0]
    distinct2 = rows("SELECT COUNT(DISTINCT source) FROM items")[0][0]
    check("sqlite rollback: markers cleared", left_marked == 0)
    check("sqlite rollback: distinct sources restored", distinct2 == plan["distinct_before"], "%d" % distinct2)

    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--apply", "--soft-delete"])
    check("sqlite soft-delete: exit 0", r.returncode == 0)
    sdc = rows("SELECT COUNT(*) FROM items WHERE valid_until IS NOT NULL")[0][0]
    check("sqlite soft-delete: valid_until stamped", sdc == n_folded, "%d vs %d" % (sdc, n_folded))
    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--restore-all"])
    check("sqlite restore-all: cleared", rows("SELECT COUNT(*) FROM items WHERE valid_until IS NOT NULL")[0][0] == 0)
    run(["--adapter", "sqlite", "--target", db, "--table", "items",
         "--type-col", "source", "--mapping", mapping, "--rollback"])
    run(["--adapter", "sqlite", "--target", db, "--table", "items",
         "--type-col", "source", "--mapping", mapping, "--apply", "--soft-delete"])
    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--purge"])
    check("sqlite purge (in window): retains", rows("SELECT COUNT(*) FROM items")[0][0] == total)
    r = run(["--adapter", "sqlite", "--target", db, "--table", "items",
             "--type-col", "source", "--mapping", mapping, "--purge", "--force"])
    check("sqlite purge --force: soft-deleted removed",
          rows("SELECT COUNT(*) FROM items")[0][0] == total - n_folded,
          "%d rows" % rows("SELECT COUNT(*) FROM items")[0][0])


def test_guards(tmp):
    print("\n== safety guards ==")
    # throwaway skills tree (generic categories)
    skills = os.path.join(tmp, "skills")
    for cat in ("core", "devops", "research", "eng-a", "eng-b", "singleton-x"):
        d = os.path.join(skills, cat)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("# %s\n" % cat)
    mapping = os.path.join(ROOT, "mappings", "demo-skills.json")
    r = run(["--adapter", "skills", "--target", skills, "--mapping", mapping, "--apply"])
    check("guards: skills adapter refuses --apply", r.returncode != 0 and b"read-only" in r.stderr)
    r = run(["--adapter", "jsonl", "--target", "nope.jsonl", "--mapping", mapping, "--apply"])
    check("guards: missing store fails cleanly", r.returncode != 0)
    r = run(["--adapter", "jsonl", "--target", os.path.join(SAMPLES, "demo_store.jsonl"),
             "--mapping", mapping, "--soft-delete"])
    check("guards: --soft-delete without --apply rejected", r.returncode != 0)


def main():
    print("schema-unify lifecycle test")
    with tempfile.TemporaryDirectory(prefix="su-test-") as tmp:
        test_jsonl(tmp)
        test_sqlite(tmp)
        test_guards(tmp)
    print("\n== result ==")
    if FAILS:
        print("FAILED: %d check(s): %s" % (len(FAILS), ", ".join(FAILS)))
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
