#!/usr/bin/env python3
"""Build RAGAS reference answers from gold expected_facts (deterministic, zero model calls).

Why:
- RAGAS official ContextRecall / ContextPrecision(with reference) require a `reference` string.
- The project's gold set has `expected_facts`, not free-text reference answers.
- This script converts each fact into exactly one sentence, so the reference sentence count
  (ContextRecall denominator) stays controllable and auditable.

Invariants:
- Only reads packet's `question` + `expected_facts`; never reads product answer/contexts.
- One fact -> one sentence (facts joined with Chinese full stop).
- Output records provenance + source shas; no API calls.

Usage:
  python3 evaluation/gold/build_qa_ref_answers.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

EVAL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_PACKET = EVAL_DIR / "results" / "eval100_scope_fixed_packet.json"
DEFAULT_OUT = EVAL_DIR / "gold" / "qa_ref_answers.json"
DEFAULT_SPOTCHECK = EVAL_DIR / "gold" / "qa_ref_spotcheck_30.csv"

TRAILING = "。．.！!？?；;，,、 "

# A coarse sentence splitter for display/validation only; RAGAS uses its own splitter.
_SENT_SPLIT = re.compile(r"[。！？!?；;\n]+")


def clean_fact(text: str) -> str:
    value = re.sub(r"\s+", " ", str(text or "")).strip()
    return value.rstrip(TRAILING)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_reference(facts: list[str]) -> tuple[str, int]:
    sentences = [f"{clean_fact(fact)}。" for fact in facts if clean_fact(fact)]
    if not sentences:
        return "", 0
    return "".join(sentences), len(sentences)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--packet", type=Path, default=DEFAULT_PACKET)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--spotcheck", type=Path, default=DEFAULT_SPOTCHECK)
    parser.add_argument("--spotcheck-n", type=int, default=30)
    args = parser.parse_args()

    packet = json.loads(args.packet.read_text(encoding="utf-8"))
    cases = packet.get("cases") or []
    if not cases:
        raise SystemExit("packet has no cases")

    built: list[dict] = []
    total_sentences = 0
    for case in cases:
        facts = case.get("expected_facts") or []
        reference, n_sent = build_reference(facts)
        if not reference:
            raise SystemExit(f"case {case.get('case_id')} has no usable expected_facts")
        total_sentences += n_sent
        built.append(
            {
                "case_id": case["case_id"],
                "question": case["question"],
                "reference": reference,
                "facts": facts,
                "sentence_count": n_sent,
                "provenance": {
                    "method": "gold_facts_sentence_template",
                    "source": "packet.expected_facts",
                    "llm_calls": 0,
                    "human_checked": False,
                    "note": "1 fact -> 1 sentence; not a human-written reference answer",
                },
            }
        )

    out = {
        "version": 1,
        "built_at": datetime.now(timezone.utc).isoformat(),
        "instrument_label": "r1-reference",
        "method": "gold_facts_sentence_template",
        "packet_path": str(args.packet.relative_to(EVAL_DIR.parent)),
        "packet_sha256": sha256_file(args.packet),
        "n_cases": len(built),
        "total_facts": sum(len(item["facts"]) for item in built),
        "total_sentences": total_sentences,
        "human_checked": False,
        "notes": [
            "Reference is derived only from gold expected_facts; product answer/contexts are never read.",
            "One gold fact is rendered as one sentence to keep the ContextRecall denominator stable.",
            "human_checked=false: a 30-case spot-check CSV is emitted for human review; do not call it human gold before review.",
        ],
        "cases": built,
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    ref_sha = sha256_file(args.out)
    sha_path = args.out.with_suffix(args.out.suffix + ".sha256")
    sha_path.write_text(f"{ref_sha}  {args.out.name}\n", encoding="utf-8")

    # deterministic-ish spot-check sample: evenly spaced across the case list
    step = max(1, len(built) // args.spotcheck_n)
    sample = built[::step][: args.spotcheck_n]
    lines = ["case_id,question,reference,verdict(ok/fix),reviewer,note"]
    for item in sample:
        q = item["question"].replace('"', "'")
        r = item["reference"].replace('"', "'")
        lines.append(f'{item["case_id"]},"{q}","{r}",,,"')
    args.spotcheck.write_text("\n".join(lines) + "\n", encoding="utf-8")

    print(f"wrote {args.out}  cases={len(built)}  facts={out['total_facts']}  sentences={total_sentences}")
    print(f"sha256 {ref_sha}")
    print(f"spotcheck {args.spotcheck}  rows={len(sample)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
