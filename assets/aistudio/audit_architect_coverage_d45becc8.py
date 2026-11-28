"""Report Career Program Architect coverage from the published coach records.

Run from the checkout with python3 scripts/coaches/audit_architect_coverage.py.
Older state files omit pre-hire rejection details. Keep those cases unresolved.
"""
import csv
import json
import sys
from collections import Counter
from pathlib import Path


def coverage_counts(coaches):
    counts = Counter()
    for coach in coaches:
        if coach["verification_status"] != "verified":
            continue
        counts["verified"] += 1
        if len(coach["verified_years"]) < 2:
            continue
        counts["multi_year"] += 1
        career = coach["windows"].get("career", {})
        if career.get("turnaround_delta") is not None:
            counts["measured"] += 1
            if career["turnaround_delta"] >= 0.05:
                counts["ranked"] += 1
            continue
        seasons = [s for s in coach["seasons"] if s["team_id"] == coach["current_team_id"]]
        years = sorted({s["year"] for s in seasons})
        games = sum(s["wins"] + s["losses"] + s["ties"] for s in seasons)
        if len(years) < 2:
            counts["current_stint_under_two_seasons"] += 1
        elif games < 16:
            counts["current_stint_under_16_games"] += 1
        elif years != list(range(years[0], years[-1] + 1)):
            counts["current_stint_gap"] += 1
        elif years[0] < 2016:
            counts["baseline_before_2014_archive_floor"] += 1
        else:
            counts[coach.get("turnaround_review_reason", "pre_hire_source_review_needed")] += 1
    return counts


if __name__ == "__main__":
    directory = Path(__file__).resolve().parents[2] / "public/data/coaches"
    states = {p.stem[-2:].upper(): coverage_counts(json.loads(p.read_text())["coaches"])
              for p in sorted(directory.glob("coaches_??.json"))}
    totals = sum(states.values(), Counter())
    writer = csv.writer(sys.stdout)
    writer.writerow(["state", *totals])
    for state, counts in [*states.items(), ("US", totals)]:
        writer.writerow([state, *(counts[key] for key in totals)])
