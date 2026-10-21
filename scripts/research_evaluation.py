"""Score supplied ranked source IDs; never contact a database or provider.

Input rankings must come from a separately identified retrieval run. Deduplicate
sources before scoring: repeated chunks from one document do not increase recall.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path


def score(corpus: dict, rankings: dict[str, list[str]], *, k: int = 3) -> dict:
    """Return macro source recall/MRR and unsupported-query empty-result rate."""
    if type(k) is not int or not 1 <= k <= 100:
        raise ValueError("k must be an integer in 1..100")
    cases = corpus["cases"]
    case_ids = [case["id"] for case in cases]
    source_ids = [source["id"] for source in corpus["sources"]]
    if len(set(case_ids)) != len(case_ids) or len(set(source_ids)) != len(source_ids):
        raise ValueError("corpus IDs must be unique")
    if set(rankings) != set(case_ids):
        raise ValueError("rankings must include exactly the declared cases")
    rows, recalls, reciprocals, unsupported = [], [], [], []
    for case in cases:
        ranked = rankings[case["id"]]
        expected = set(case["expected_sources"])
        if not expected <= set(source_ids):
            raise ValueError("expected sources must exist in the corpus")
        if not isinstance(ranked, list) or any(
            not isinstance(sid, str) or sid not in source_ids for sid in ranked
        ):
            raise ValueError("rankings must contain known source IDs")
        if case.get("source_id") and any(sid != case["source_id"] for sid in ranked):
            raise ValueError("source-filtered retrieval leaked another source")
        selected = list(dict.fromkeys(ranked))[:k]
        row = {"id": case["id"], "retrieved_sources": selected}
        if expected:
            recall = len(expected.intersection(selected)) / len(expected)
            reciprocal = next((1 / rank for rank, sid in enumerate(selected, 1)
                               if sid in expected), 0.0)
            recalls.append(recall)
            reciprocals.append(reciprocal)
            row.update(recall=recall, reciprocal_rank=reciprocal)
        else:
            unsupported.append(not selected)
            row["empty_result"] = not selected
        rows.append(row)
    return {
        "scope": corpus["scope"], "k": k, "cases": rows,
        "positive_cases": len(recalls), "unsupported_cases": len(unsupported),
        "macro_source_recall": sum(recalls) / len(recalls) if recalls else None,
        "mean_reciprocal_rank": sum(reciprocals) / len(reciprocals) if reciprocals else None,
        "unsupported_empty_rate": sum(unsupported) / len(unsupported) if unsupported else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("rankings", type=Path, help="JSON object: case ID -> ranked source IDs")
    parser.add_argument("--k", type=int, default=3)
    args = parser.parse_args()
    try:
        result = score(json.loads(args.corpus.read_text()),
                       json.loads(args.rankings.read_text()), k=args.k)
    except (ValueError, KeyError, TypeError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
