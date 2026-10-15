#!/usr/bin/env python3
"""
schema_unify.py — reversible taxonomy-consolidation tool (dry-run first).

The transferable mechanism:

    Taxonomy consolidation as a DRY-RUN-FIRST, REVERSIBLE migration:
      * fold the long tail of types into a catch-all bucket;
      * stamp EVERY moved item with its legacy type so the move can be undone;
      * soft-delete with a TTL window before any purge.

DESIGN GUARANTEES
  * DEFAULT IS DRY-RUN. The tool prints exactly what it would change and
    writes NOTHING to the target store unless an explicit apply flag
    (`--apply` / `--rollback` / `--purge`) is passed.
  * REVERSIBLE. Every retyped item keeps its legacy type (configurable key);
    `--rollback` restores the original type and clears the marker.
  * SOFT-DELETE + TTL. Folded items can be soft-deleted (opt-in) with a
    `purge_after` window; `--purge` refuses to remove anything still inside
    its window unless `--force`.
  * EVERY MUTATION IS LEDGERED to `<store>.unify-ledger.jsonl` (append-only).
  * Idempotent: re-running apply after apply changes nothing (0 folds left).

ADAPTERS
  jsonl  — a JSON-Lines store of `{"id":..., "type":...}` records (any item list).
  sqlite — any SQLite table; column names are fully configurable via CLI flags
           (defaults: id / type / metadata / soft-delete / superseded columns).
  skills — READ-ONLY view of a skills tree (type == top-level category
           directory), for analysis-only consolidation plans.

Usage (dry-run is the default; nothing below writes without --apply):
  schema_unify.py --adapter jsonl --target store.jsonl \
      --mapping mappings/demo-bloated-types.json
  schema_unify.py --adapter sqlite --target db.sqlite --type-col source \
      --mapping mappings/demo-source.json --apply --soft-delete
  schema_unify.py ... --rollback        # undo a prior apply
  schema_unify.py ... --purge           # delete soft-deleted items past TTL
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

DEFAULT_TTL_HOURS = 72


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def now_dt():
    return datetime.now(timezone.utc).astimezone()


def now_iso():
    return now_dt().isoformat(timespec="seconds")


def parse_iso(s):
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def atomic_write(path, text):
    parent = os.path.dirname(os.path.abspath(path))
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


class Ledger:
    """Append-only JSONL record of every mutation, beside the store."""

    def __init__(self, path):
        self.path = path

    def append(self, entries):
        if not entries or not self.path:
            return
        with open(self.path, "a", encoding="utf-8") as fh:
            for e in entries:
                fh.write(json.dumps(e, sort_keys=True) + "\n")

    def read(self):
        if not self.path or not os.path.isfile(self.path):
            return []
        out = []
        with open(self.path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line:
                    try:
                        out.append(json.loads(line))
                    except ValueError:
                        pass
        return out


# --------------------------------------------------------------------------
# mapping rules
# --------------------------------------------------------------------------
class Mapping:
    def __init__(self, d):
        self.name = d.get("name", "unnamed")
        self.description = d.get("description", "")
        self.catch_all = d.get("catch_all", "note")
        self.legacy_key = d.get("legacy_key", "legacy_type")
        self.min_cluster_size = int(d.get("min_cluster_size", 1))
        self.ttl_hours = int(d.get("ttl_hours", DEFAULT_TTL_HOURS))
        self.canonical = list(d.get("canonical", []))
        self.rules = list(d.get("rules", []))

    def rule_target(self, type_name):
        """Return the rule's target type, or None if no rule matches."""
        for r in self.rules:
            kind = r.get("match", "exact")
            src = r.get("from", "")
            if kind == "exact" and type_name == src:
                return r.get("to")
            if kind == "prefix" and type_name.startswith(src):
                return r.get("to")
            if kind == "regex" and re.search(src, type_name):
                return r.get("to")
        return None

    @classmethod
    def load(cls, path):
        with open(path, "r", encoding="utf-8") as fh:
            return cls(json.load(fh))


# --------------------------------------------------------------------------
# adapters
# --------------------------------------------------------------------------
class ReadOnlyError(RuntimeError):
    pass


class JsonlStore:
    kind = "jsonl"
    read_only = False

    def __init__(self, path, **_):
        self.path = path
        self.ledger = Ledger(path + ".unify-ledger.jsonl")

    def load(self):
        recs = []
        if not os.path.isfile(self.path):
            return recs
        with open(self.path, "r", encoding="utf-8") as fh:
            for i, line in enumerate(fh):
                line = line.strip()
                if not line:
                    continue
                obj = json.loads(line)
                obj.setdefault("id", "line-%d" % i)
                obj.setdefault("type", "")
                recs.append(obj)
        return recs

    def _write(self, recs):
        text = "".join(json.dumps(r, sort_keys=True) + "\n" for r in recs)
        atomic_write(self.path, text)

    def apply_plan(self, folds, mapping, soft_delete, ttl_hours):
        recs = self.load()
        by_type = {}
        for r in recs:
            by_type.setdefault(r.get("type"), []).append(r)
        purge_after = (now_dt() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
        entries = []
        moved = 0
        for from_type, to_type in folds:
            for r in by_type.get(from_type, []):
                r[mapping.legacy_key] = from_type
                r["type"] = to_type
                if soft_delete:
                    r["_deleted_at"] = now_iso()
                    r["_purge_after"] = purge_after
                    r["_superseded_by"] = to_type
                entries.append({
                    "ts": now_iso(), "op": "retype", "id": r["id"],
                    "from": from_type, "to": to_type,
                    "soft_deleted": bool(soft_delete), "purge_after": purge_after if soft_delete else None,
                })
                moved += 1
        self._write(recs)
        self.ledger.append(entries)
        return moved, purge_after

    def rollback(self, mapping):
        recs = self.load()
        entries = []
        for r in recs:
            if mapping.legacy_key in r:
                orig = r.pop(mapping.legacy_key)
                old = r.get("type")
                r["type"] = orig
                for k in ("_deleted_at", "_purge_after", "_superseded_by"):
                    r.pop(k, None)
                entries.append({"ts": now_iso(), "op": "rollback", "id": r["id"],
                                "from": old, "to": orig})
        self._write(recs)
        self.ledger.append(entries)
        return len(entries)

    def restore_all(self, mapping):
        recs = self.load()
        n = 0
        for r in recs:
            if r.pop("_deleted_at", None) is not None or r.pop("_purge_after", None) is not None:
                r.pop("_superseded_by", None)
                n += 1
        self._write(recs)
        return n

    def purge(self, force=False):
        recs = self.load()
        now = now_dt()
        keep, purged, retained = [], 0, 0
        for r in recs:
            pa = parse_iso(r.get("_purge_after"))
            if r.get("_deleted_at") is None:
                keep.append(r)
            elif force or (pa is not None and pa <= now):
                purged += 1
            else:
                retained += 1
                keep.append(r)
        self._write(keep)
        self.ledger.append([{"ts": now_iso(), "op": "purge",
                             "purged": purged, "retained_in_window": retained, "force": force}])
        return purged, retained


class SqliteStore:
    """Any SQLite table; column names are configurable (see CLI flags)."""
    kind = "sqlite"
    read_only = False

    def __init__(self, path, table="working_memory", id_col="id", type_col="memory_type",
                 metadata_col="metadata_json", softdelete_col="valid_until",
                 superseded_col="superseded_by", **_):
        self.path = path
        self.table = table
        self.id_col = id_col
        self.type_col = type_col
        self.metadata_col = metadata_col
        self.softdelete_col = softdelete_col
        self.superseded_col = superseded_col
        self.ledger = Ledger(path + ".unify-ledger.jsonl")

    def _connect(self, write=False):
        if write:
            return sqlite3.connect(self.path)
        try:
            return sqlite3.connect("file:%s?mode=ro" % self.path, uri=True)
        except sqlite3.OperationalError:
            return sqlite3.connect(self.path)

    def load(self):
        conn = self._connect(write=False)
        try:
            cols = [self.id_col, self.type_col, self.metadata_col,
                    self.softdelete_col, self.superseded_col]
            q = "SELECT %s FROM %s" % (", ".join(cols), self.table)
            recs = []
            for row in conn.execute(q):
                recs.append({
                    "id": row[0],
                    "type": row[1] or "",
                    "_meta_raw": row[2],
                    "_softdelete": row[3],
                    "_superseded": row[4],
                })
            return recs
        finally:
            conn.close()

    @staticmethod
    def _meta_has_key(raw, key):
        if not raw:
            return False
        try:
            return key in json.loads(raw)
        except (ValueError, TypeError):
            return False

    def apply_plan(self, folds, mapping, soft_delete, ttl_hours):
        conn = self._connect(write=True)
        try:
            cur = conn.cursor()
            purge_after = (now_dt() + timedelta(hours=ttl_hours)).isoformat(timespec="seconds")
            entries = []
            moved = 0
            for from_type, to_type in folds:
                rows = cur.execute(
                    "SELECT %s, %s FROM %s WHERE %s = ?" %
                    (self.id_col, self.metadata_col, self.table, self.type_col),
                    (from_type,),
                ).fetchall()
                for rid, raw in rows:
                    meta = {}
                    if raw:
                        try:
                            meta = json.loads(raw) or {}
                        except (ValueError, TypeError):
                            meta = {}
                    meta[mapping.legacy_key] = from_type
                    if soft_delete:
                        cur.execute(
                            "UPDATE %s SET %s=?, %s=?, %s=?, %s=? WHERE %s=?" % (
                                self.table, self.type_col, self.metadata_col,
                                self.softdelete_col, self.superseded_col, self.id_col),
                            (to_type, json.dumps(meta, sort_keys=True), purge_after,
                             to_type, rid),
                        )
                    else:
                        cur.execute(
                            "UPDATE %s SET %s=?, %s=? WHERE %s=?" % (
                                self.table, self.type_col, self.metadata_col, self.id_col),
                            (to_type, json.dumps(meta, sort_keys=True), rid),
                        )
                    entries.append({
                        "ts": now_iso(), "op": "retype", "id": rid,
                        "from": from_type, "to": to_type,
                        "soft_deleted": bool(soft_delete),
                        "purge_after": purge_after if soft_delete else None,
                    })
                    moved += 1
            conn.commit()
            self.ledger.append(entries)
            return moved, purge_after
        finally:
            conn.close()

    def rollback(self, mapping):
        conn = self._connect(write=True)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT %s, %s FROM %s" % (self.id_col, self.metadata_col, self.table)
            ).fetchall()
            entries = []
            for rid, raw in rows:
                if not self._meta_has_key(raw, mapping.legacy_key):
                    continue
                meta = json.loads(raw)
                orig = meta.pop(mapping.legacy_key)
                cur.execute(
                    "UPDATE %s SET %s=?, %s=?, %s=NULL, %s=NULL WHERE %s=?" % (
                        self.table, self.type_col, self.metadata_col,
                        self.softdelete_col, self.superseded_col, self.id_col),
                    (orig, json.dumps(meta, sort_keys=True), rid),
                )
                entries.append({"ts": now_iso(), "op": "rollback", "id": rid, "to": orig})
            conn.commit()
            self.ledger.append(entries)
            return len(entries)
        finally:
            conn.close()

    def restore_all(self, mapping):
        conn = self._connect(write=True)
        try:
            cur = conn.cursor()
            cur.execute("UPDATE %s SET %s=NULL, %s=NULL WHERE %s IS NOT NULL" % (
                self.table, self.softdelete_col, self.superseded_col, self.softdelete_col))
            n = cur.rowcount
            conn.commit()
            return n
        finally:
            conn.close()

    def purge(self, force=False):
        conn = self._connect(write=True)
        try:
            cur = conn.cursor()
            rows = cur.execute(
                "SELECT %s, %s FROM %s WHERE %s IS NOT NULL" % (
                    self.id_col, self.softdelete_col, self.table, self.softdelete_col)
            ).fetchall()
            now = now_dt()
            purged = retained = 0
            for rid, until in rows:
                pa = parse_iso(until)
                if force or (pa is not None and pa <= now):
                    cur.execute("DELETE FROM %s WHERE %s=?" % (self.table, self.id_col), (rid,))
                    purged += 1
                else:
                    retained += 1
            conn.commit()
            self.ledger.append([{"ts": now_iso(), "op": "purge", "purged": purged,
                                 "retained_in_window": retained, "force": force}])
            return purged, retained
        finally:
            conn.close()


class SkillsTreeStore:
    """Read-only view: type == top-level category directory under the skills root."""
    kind = "skills"
    read_only = True

    def __init__(self, path, **_):
        self.path = path
        self.ledger = Ledger(None)

    def load(self):
        recs = []
        if not os.path.isdir(self.path):
            return recs
        for name in sorted(os.listdir(self.path)):
            d = os.path.join(self.path, name)
            if not os.path.isdir(d):
                continue
            n = 0
            for _dp, _dn, files in os.walk(d):
                if "SKILL.md" in files:
                    n += 1
            # one record per CATEGORY; its weight is how many skills it holds,
            # so a 60-skill category is "significant" and a 1-skill category is
            # a singleton for the min_cluster_size guard.
            recs.append({"id": name, "type": name, "_count": n, "weight": n})
        return recs

    def apply_plan(self, *a, **k):
        raise ReadOnlyError("skills adapter is analysis-only (read-only); "
                            "no filesystem move is performed")

    def rollback(self, *a, **k):
        raise ReadOnlyError("skills adapter is read-only")

    def restore_all(self, *a, **k):
        raise ReadOnlyError("skills adapter is read-only")

    def purge(self, *a, **k):
        raise ReadOnlyError("skills adapter is read-only")


def make_adapter(args):
    kw = dict(table=args.table, id_col=args.id_col, type_col=args.type_col,
              metadata_col=args.metadata_col, softdelete_col=args.softdelete_col,
              superseded_col=args.superseded_col)
    if args.adapter == "jsonl":
        return JsonlStore(args.target, **kw)
    if args.adapter == "sqlite":
        return SqliteStore(args.target, **kw)
    if args.adapter == "skills":
        return SkillsTreeStore(args.target, **kw)
    raise SystemExit("unknown adapter: %s" % args.adapter)


# --------------------------------------------------------------------------
# planning
# --------------------------------------------------------------------------
def compute_plan(records, mapping):
    counts = {}
    for r in records:
        w = r.get("weight", r.get("_count", 1))
        counts[r.get("type")] = counts.get(r.get("type"), 0) + w

    keep, folds = [], []
    for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        rule_to = mapping.rule_target(t)
        if t == mapping.catch_all:
            # the catch-all bucket is a SINK — never a fold source (else a
            # re-run would re-stamp it forever)
            keep.append((t, c, None))
        elif rule_to is not None:
            if rule_to == t:
                keep.append((t, c, None))
            else:
                folds.append((t, c, rule_to))
        elif t in mapping.canonical:
            keep.append((t, c, None))
        elif c >= mapping.min_cluster_size:
            keep.append((t, c, None))          # significant cluster, left alone
        else:
            folds.append((t, c, mapping.catch_all))

    folded_types = [(t, c, to) for (t, c, to) in folds]
    keep_types = {t for (t, _c, _to) in keep}
    fold_targets = {to for (_t, _c, to) in folds}
    distinct_after = len(keep_types | fold_targets)
    return {
        "counts": counts,
        "keep": [(t, c) for (t, c, _to) in keep],
        "folds": folded_types,
        "items_folded": sum(c for (_t, c, _to) in folds),
        "distinct_before": len(counts),
        "distinct_after": distinct_after,
    }


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
def render_plan(plan, mapping, adapter, mode, ttl_hours, soft_delete_flag, source):
    L = []
    banner = {"dry-run": "DRY-RUN (no changes will be written)",
              "apply": "APPLY (writing changes)",
              "rollback": "ROLLBACK (restoring legacy types)",
              "purge": "PURGE (removing items past their TTL)",
              "restore": "RESTORE (un-soft-deleting)"}[mode]
    L.append("=" * 68)
    L.append("  schema-unify — %s" % banner)
    L.append("  store   : %s (%s)" % (adapter.kind, source))
    L.append("  mapping : %s  (catch_all=%s, legacy_key=%s, min_cluster=%d)"
             % (mapping.name, mapping.catch_all, mapping.legacy_key, mapping.min_cluster_size))
    L.append("=" * 68)
    L.append("  distinct types  before : %d" % plan["distinct_before"])
    L.append("  distinct types  after  : %d   (target canonical: %d)"
             % (plan["distinct_after"], len(mapping.canonical)))
    L.append("  items to retype        : %d" % plan["items_folded"])
    if soft_delete_flag:
        L.append("  soft-delete folded     : YES (TTL %dh, restore before purge)" % ttl_hours)
    else:
        L.append("  soft-delete folded     : no (retype only)")
    L.append("-" * 68)
    L.append("  WOULD RETYPE  (long tail -> bucket):")
    if plan["folds"]:
        for t, c, to in plan["folds"]:
            L.append("    %-38s %5d  ->  %s" % (t, c, to))
    else:
        L.append("    (nothing — taxonomy already within the canonical set)")
    L.append("-" * 68)
    L.append("  WOULD KEEP  (canonical / ruled / significant):")
    for t, c in plan["keep"]:
        L.append("    %-38s %5d" % (t, c))
    L.append("=" * 68)
    return "\n".join(L)


def render_report_md(plan, mapping, adapter, mode, ttl_hours, soft_delete_flag, source, when):
    lines = [
        "# schema-unify report",
        "",
        f"- **Mode:** `{mode}`",
        f"- **Store:** `{source}` (adapter: {adapter.kind})",
        f"- **Mapping:** `{mapping.name}` (catch_all=`{mapping.catch_all}`, "
        f"legacy_key=`{mapping.legacy_key}`, min_cluster_size={mapping.min_cluster_size})",
        f"- **When:** {when}",
        f"- **Distinct types:** {plan['distinct_before']} -> {plan['distinct_after']}",
        f"- **Items retyped:** {plan['items_folded']}",
        f"- **Soft-delete folded:** {'yes (TTL %dh)' % ttl_hours if soft_delete_flag else 'no'}",
        "",
        "## Retype plan (long tail -> bucket)",
        "",
        "| Type | Items | -> Target |",
        "|---|---|---|",
    ]
    if plan["folds"]:
        for t, c, to in plan["folds"]:
            lines.append(f"| `{t}` | {c} | `{to}` |")
    else:
        lines.append("| _(none)_ | 0 | — |")
    lines += ["", "## Kept (canonical / ruled / significant)", "",
              "| Type | Items |", "|---|---|"]
    for t, c in plan["keep"]:
        lines.append(f"| `{t}` | {c} |")
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# cli
# --------------------------------------------------------------------------
def build_parser():
    ap = argparse.ArgumentParser(description="schema-unify — reversible taxonomy consolidation (dry-run first)")
    ap.add_argument("--adapter", choices=["jsonl", "sqlite", "skills"], required=True)
    ap.add_argument("--target", required=True, help="store path / db / skills root")
    ap.add_argument("--mapping", required=True, help="mapping rules JSON")
    ap.add_argument("--ttl-hours", type=int, default=None, help="soft-delete TTL (override mapping)")
    # sqlite shaping
    ap.add_argument("--table", default="working_memory")
    ap.add_argument("--id-col", default="id")
    ap.add_argument("--type-col", default="memory_type")
    ap.add_argument("--metadata-col", default="metadata_json")
    ap.add_argument("--softdelete-col", default="valid_until")
    ap.add_argument("--superseded-col", default="superseded_by")
    # actions (mutually exclusive; absence == dry-run)
    ap.add_argument("--apply", action="store_true", help="WRITE the retype plan")
    ap.add_argument("--soft-delete", action="store_true",
                    help="with --apply: also soft-delete folded items (TTL window)")
    ap.add_argument("--rollback", action="store_true", help="undo a prior apply (restore legacy types)")
    ap.add_argument("--restore-all", action="store_true", help="un-soft-delete everything")
    ap.add_argument("--purge", action="store_true", help="DELETE soft-deleted items past their TTL")
    ap.add_argument("--force", action="store_true", help="with --purge: ignore the TTL window")
    ap.add_argument("--json", action="store_true", help="emit a machine-readable plan")
    ap.add_argument("--report", default=None, help="write a markdown report to this file")
    return ap


def main(argv=None):
    args = build_parser().parse_args(argv)
    mapping = Mapping.load(args.mapping)
    ttl_hours = args.ttl_hours if args.ttl_hours is not None else mapping.ttl_hours
    adapter = make_adapter(args)

    actions = [a for a in ("apply", "rollback", "restore_all", "purge")
               if getattr(args, a)]
    if len(actions) > 1:
        raise SystemExit("choose at most one of --apply/--rollback/--restore-all/--purge")
    if args.soft_delete and not args.apply:
        raise SystemExit("--soft-delete only meaningful with --apply")

    when = now_iso()

    # rollback / restore / purge paths ------------------------------------
    if args.rollback or args.restore_all or args.purge:
        if adapter.read_only:
            raise SystemExit("adapter '%s' is read-only; %s is not possible" % (adapter.kind, actions[0]))
        if args.rollback:
            n = adapter.rollback(mapping)
            print("ROLLBACK complete: restored legacy type on %d item(s); "
                  "ledger: %s" % (n, adapter.ledger.path))
        elif args.restore_all:
            n = adapter.restore_all(mapping)
            print("RESTORE complete: un-soft-deleted %d item(s)" % n)
        else:
            purged, retained = adapter.purge(force=args.force)
            print("PURGE complete: deleted %d item(s); retained %d still inside TTL window%s"
                  % (purged, retained, "" if args.force else " (use --force to override)"))
        return 0

    # plan (dry-run or apply) --------------------------------------------
    records = adapter.load()
    if not records:
        raise SystemExit("no records loaded from %s" % args.target)
    plan = compute_plan(records, mapping)
    mode = "apply" if args.apply else "dry-run"

    if args.json:
        print(json.dumps({
            "mode": mode, "store": args.target, "adapter": adapter.kind,
            "mapping": mapping.name, "distinct_before": plan["distinct_before"],
            "distinct_after": plan["distinct_after"], "items_folded": plan["items_folded"],
            "folds": [{"type": t, "items": c, "to": to} for (t, c, to) in plan["folds"]],
            "keep": [{"type": t, "items": c} for (t, c) in plan["keep"]],
        }, indent=2))
    else:
        print(render_plan(plan, mapping, adapter, mode, ttl_hours, args.soft_delete, args.target))

    if args.report:
        atomic_write(args.report, render_report_md(
            plan, mapping, adapter, mode, ttl_hours, args.soft_delete, args.target, when))

    if not args.apply:
        msg = "  -> dry-run only. Re-run with --apply to write (parent review required)."
        print(msg, file=sys.stderr) if args.json else print(msg)
        return 0

    if adapter.read_only:
        raise SystemExit("adapter '%s' is read-only; --apply refused" % adapter.kind)

    folds = [(t, to) for (t, _c, to) in plan["folds"]]
    moved, purge_after = adapter.apply_plan(folds, mapping, args.soft_delete, ttl_hours)
    # verify
    after = compute_plan(adapter.load(), mapping)
    print("APPLY complete: retyped %d item(s). distinct types %d -> %d."
          % (moved, plan["distinct_before"], after["distinct_before"]))
    print("  legacy marker '%s' stamped for rollback." % mapping.legacy_key)
    if args.soft_delete:
        print("  soft-deleted: purge_after=%s (restore before then)" % purge_after)
    print("  ledger: %s" % adapter.ledger.path)
    if after["items_folded"] != 0:
        print("  NOTE: %d item(s) still match fold rules after apply (check mapping)."
              % after["items_folded"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
