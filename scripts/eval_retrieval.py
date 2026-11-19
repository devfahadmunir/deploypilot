"""
Measure retrieval quality on a fixed question set (evals/retrieval_set.json).

For each question we check which document the search ranks first and where
the first correct document appears.

Metrics:
  Hit@1  share of questions where the top result is from a correct document
  Hit@3  share where a correct document appears in the top 3
  MRR    mean reciprocal rank: 1.0 if correct at rank 1, 0.5 at rank 2, ...

Usage:
    python scripts/eval_retrieval.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.acme_server import search_knowledge_base  # noqa: E402

EVAL_SET = Path(__file__).resolve().parents[1] / "evals" / "retrieval_set.json"
K = 5


def main() -> None:
    cases = json.loads(EVAL_SET.read_text())
    hit1 = hit3 = rr_sum = 0.0
    print(f"{'':2} {'rank':>4}  {'top result':<20} question")
    for case in cases:
        results = search_knowledge_base(case["question"], top_k=K)
        docs = [r["doc"] for r in results]
        rank = next((i + 1 for i, d in enumerate(docs) if d in case["expected"]), None)
        hit1 += rank == 1
        hit3 += rank is not None and rank <= 3
        rr_sum += 1 / rank if rank else 0
        mark = "✅" if rank == 1 else ("🟡" if rank else "❌")
        print(f"{mark} {rank or '-':>4}  {docs[0]:<20} {case['question']}")

    n = len(cases)
    print(f"\nHit@1: {hit1 / n:.0%}   Hit@3: {hit3 / n:.0%}   MRR: {rr_sum / n:.2f}   ({n} questions)")


if __name__ == "__main__":
    main()
