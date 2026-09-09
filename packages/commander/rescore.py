"""Rescore saved audits into a new directory without changing original run records."""

import argparse
import csv
import json
from pathlib import Path
from typing import Any

from commander.evals import score_run


def rescore(source: Path, destination: Path) -> list[dict[str, Any]]:
    if source.resolve() == destination.resolve():
        raise ValueError("Rescoring must use a separate output directory")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Rescoring output directory must be empty")
    audits = sorted(source.glob("*/audit.json"))
    if not audits:
        raise ValueError("No completed run audits found")
    results = []
    for path in audits:
        audit = json.loads(path.read_text(encoding="utf-8"))
        run = json.loads(path.with_name("run.json").read_text(encoding="utf-8"))
        evidence = audit.get("evidence")
        supplement = path.with_name("evidence.json")
        if evidence is None and supplement.exists():
            evidence = json.loads(supplement.read_text(encoding="utf-8"))
        result = score_run(
            run["scenario_id"], audit["incident"], audit["hypotheses"], audit["timeline"], evidence
        )
        result["source_audit"] = str(path.resolve())
        result["runtime_backend"] = run.get("backend", "compose")
        result["evidence_export_available"] = evidence is not None
        results.append(result)
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    with (destination / "results.csv").open("w", newline="", encoding="utf-8") as output:
        writer = csv.DictWriter(output, fieldnames=list(results[0]))
        writer.writeheader()
        writer.writerows(results)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    results = rescore(args.source, args.output)
    print(f"Rescored {len(results)} completed audits; originals preserved.")


if __name__ == "__main__":
    main()
