#!/usr/bin/env python3
"""Generate samples/demo_store.jsonl — a throwaway item list with a bloated
type taxonomy that reproduces a realistic long-tail taxonomy (dozens of noisy types, a
redirect cluster, one-off singleton types) so the full
reversible lifecycle can be proven on data that is safe to destroy."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "demo_store.jsonl")

# type -> how many items (deterministic, no randomness)
PLAN = [
    ("tweet", 20), ("tweet-thread", 12), ("tweet-bundle", 8), ("tweet-single", 5),
    ("tweet-stub", 2), ("media/x-tweet/bundle", 3),
    ("concept-redirect", 9), ("atom-partner-link", 4),
    ("civic", 3), ("framework", 2), ("insight", 2), ("memo", 1), ("anecdote", 1),
    ("note", 5), ("person", 6), ("company", 4), ("concept", 5), ("project", 3),
    ("writing", 4), ("source", 2), ("deal", 1), ("email", 2), ("slack", 1),
]

with open(OUT, "w", encoding="utf-8") as fh:
    n = 0
    for type_name, count in PLAN:
        for i in range(count):
            n += 1
            fh.write(json.dumps({
                "id": "p-%03d" % n,
                "type": type_name,
                "title": "%s item %d" % (type_name, i + 1),
            }, sort_keys=True) + "\n")

print("wrote %d records to %s across %d distinct types" % (n, OUT, len(PLAN)))
