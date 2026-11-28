"""Emit baseline, 2026-only and missing-current-coach research leads.

Run from any directory with python3 scripts/coaches/find_local_baseline_leads.py.
Use --scope baseline for the original CSV. Evidence gaps reflect local inputs.
An empty missing_evidence field means both inputs are present for reconciliation.
Baseline years precede the first verified current-school season. Confirm the hire boundary,
predecessor identity and complete schedules before crediting a baseline. A history
link points to the current coach's evidence and is only a starting point.
"""
import argparse
import csv
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parents[2] / "public/data"
ROOT = DATA.parents[1]
sys.path.insert(0, str(ROOT))
from scripts.coaches.build_coach_index import (
    HISTORY_CACHE_PATH, HIST_SCORES_CACHE_PATH, MI_DW_SCORES_PATH, STAFF_CACHE_PATH,
    is_complete_season_ledger, is_valid_human_coach_name, matches_archive_record, normalize_coach_name,
)

LEDGERS = {
    "IN": "ihsaa_master_scores_2022_2026.csv",
    "OH": "ohsaa_master_scores_2023_2026.csv",
    "WI": "wiaa_master_scores_2024_2026.csv",
}


def schedules(path):
    games = defaultdict(set)
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            day = row.get("date_iso") or row["date"]
            winner, loser = row["winner"].strip(), row["loser"].strip()
            if not (day and winner and loser and winner != loser):
                continue
            if not all(row[k].strip().isdigit() for k in ("winner_score", "loser_score")):
                continue
            for school in (winner, loser):
                games[school, int(row["season"])].add(day)
    return games


def leads():
    for state, ledger in LEDGERS.items():
        games = schedules(DATA / ledger)
        coaches = json.loads((DATA / "coaches" / f"coaches_{state.lower()}.json").read_text())["coaches"]
        schools = Counter(c["current_team_name"] for c in coaches)
        for coach in coaches:
            school = coach["current_team_name"]
            if schools[school] != 1 or coach["verification_status"] != "verified":
                continue
            if coach["windows"].get("career", {}).get("turnaround_delta") is not None:
                continue
            stint = sorted((s for s in coach["seasons"] if s["team_id"] == coach["current_team_id"]),
                           key=lambda s: s["year"])
            if len(stint) < 2 or sum(s["wins"] + s["losses"] + s["ties"] for s in stint) < 16:
                continue
            first = stint[0]["year"]
            years = (first - 2, first - 1)
            dates = [len(games[school, year]) for year in years]
            if sum(dates) >= 18 and all(n >= (6 if y == 2020 else 8) for y, n in zip(years, dates)):
                yield (state, coach["current_team_id"], school, coach["name"], first,
                       years[0], dates[0], years[1], dates[1], ledger, stint[0]["coach_source"])


def cached(path):
    return json.loads(path.read_text()) if path.exists() else {}


def local_game_logs(path, year):
    fixtures = defaultdict(dict)
    conflicts = set()
    with path.open(newline="") as stream:
        for row in csv.DictReader(stream):
            if row["season"] != str(year):
                continue
            day = row.get("date_iso") or row["date"]
            a, b = row["winner"].strip(), row["loser"].strip()
            if not day or not a or not b or a == b:
                continue
            if not all(row[k].isdigit() and int(row[k]) <= 140 for k in ("winner_score", "loser_score")):
                continue
            sa, sb = int(row["winner_score"]), int(row["loser_score"])
            for school, opponent, scored, allowed in ((a, b, sa, sb), (b, a, sb, sa)):
                key = (day, opponent)
                if key in fixtures[school] and fixtures[school][key] != (scored, allowed):
                    conflicts.add(school)
                fixtures[school][key] = (scored, allowed)
    return {school: [{"result": "W" if a > b else "L" if a < b else "T"} for a, b in games.values()]
            for school, games in fixtures.items() if school not in conflicts}


def evidence_leads():
    """Describe evidence available in this checkout. A lead never changes the index."""
    history = cached(HISTORY_CACHE_PATH)
    historical_games = cached(HIST_SCORES_CACHE_PATH)
    michigan = cached(MI_DW_SCORES_PATH)
    staff = cached(STAFF_CACHE_PATH)
    registry = cached(ROOT / "registry/coach_season_sources.json")
    reviewed = cached(ROOT / "scripts/coaches/research/local_baseline_needs_review.json")
    disputed = {r["team_id"]: r.get("disputed_years", []) for r in reviewed["needs_review"]}
    for path in sorted((DATA / "coaches").glob("coaches_??.json")):
        state = path.stem[-2:].upper()
        bundle = DATA / ("stratagem_live_bundle.json" if state == "MI"
                         else f"stratagem_live_bundle_{state.lower()}.json")
        payload = cached(bundle)
        teams = payload.get("rankings") or payload["teams"]
        name_counts = Counter(t["team"] for t in teams)
        coaches = defaultdict(list)
        for c in cached(path)["coaches"]:
            coaches[c["current_team_id"]].append(c)
        games = local_game_logs(DATA / LEDGERS[state], 2025) if state in LEDGERS else {}
        archives = [cached(HISTORY_CACHE_PATH.parent / folder / state / "2025.json")
                    for folder in ("historian_seasons", "state_archives")]
        for t in teams:
            tid, school = t["team_id"], t["team"]
            sid = (t.get("source_ids") or {}).get("maxpreps")
            verified = [c for c in coaches[tid] if c["verification_status"] == "verified"]
            candidate = verified[0] if len(verified) == 1 else None
            if verified and (not candidate or candidate["verified_years"] != [2026]):
                continue
            year = 2025 if candidate else 2026
            group = "single_year" if candidate else "name_only" if coaches[tid] else "unmapped"
            coach = candidate or (coaches[tid][0] if len(coaches[tid]) == 1 else {})
            hist = history.get(sid, {})
            meta = (hist.get("seasons", {}).get(str(year), {})
                    if sid and hist.get("school_id") == sid and hist.get("state") == state else {})
            official = registry.get(tid, {}).get(str(year), {})
            evidence = [meta, official]
            if year == 2025:
                evidence.extend(archive.get("coaches", {}).get(tid, {}) for archive in archives)
                logs = [{**g, "source": "maxpreps.com"} for g in
                        historical_games.get(state, {}).get(sid, {}).get("2025", [])]
                local = games.get(school, []) if name_counts[school] == 1 else []
                candidates = [local, logs]
                slug = michigan.get("team_id_to_slug", {}).get(tid)
                if slug:
                    candidates.append(michigan["by_slug"][slug].get("2025", []))
                for archive in archives:
                    if tid in archive.get("complete", []):
                        candidates.append(archive.get("scores", {}).get(tid, []))
                records = {e["archive_record"] for e in evidence if e.get("archive_record")}
                complete = any(is_complete_season_ledger(year, ledger, meta.get("overall_record"))
                               and all(matches_archive_record(ledger, rec) for rec in records)
                               for ledger in candidates)
            else:
                scores = [g for g in t.get("schedule_2026", [])
                          if g.get("result") in ("W", "L", "T")
                          and all(type(g.get(k)) is int for k in ("team_score", "opp_score"))]
                complete = is_complete_season_ledger(year, scores)
                explicit = []
                rec = staff.get(sid, {})
                if sid and rec.get("school_id") == sid and rec.get("state") == state and rec.get("year") == "26-27":
                    explicit = [{"head_coach": h.get("name")} for h in rec.get("head_coaches", [])
                                if str(h.get("position")).strip().lower() == "head coach"]
                    evidence.extend(explicit)
            names = {normalize_coach_name(e["head_coach"]) for e in evidence if e.get("head_coach")}
            has_coach = len(names) == 1 and all(is_valid_human_coach_name(e["head_coach"])
                                               for e in evidence if e.get("head_coach"))
            if year in disputed.get(tid, ()) or (year == 2025 and any(tid in a.get("disputed", ()) for a in archives)):
                has_coach = False
            if year == 2026:
                has_coach = has_coach and bool(explicit)
                if coach and normalize_coach_name(coach["name"]) not in names:
                    has_coach = False
                context = rec.get("context_coach_name")
                if (context and is_valid_human_coach_name(context) and normalize_coach_name(context) not in names
                        and not any(e.get("head_coach") for e in (meta, official))):
                    has_coach = False
            missing = ("games" if has_coach else "both") if not complete else (
                "head_coach" if not has_coach else "")
            hire = registry.get(tid, {}).get("2026", {})
            confirmed = bool(candidate and hire.get("hire_year") == 2026 and hire.get("source_url")
                             and normalize_coach_name(hire.get("head_coach")) == normalize_coach_name(coach["name"]))
            source = next((e["source_url"] for e in reversed(evidence) if e.get("source_url")), "")
            if not source and candidate:
                source = next((s["coach_source"] for s in candidate["seasons"]
                               if s["year"] == 2026 and s["team_id"] == tid), "")
            yield (state, tid, school, coach.get("name", ""), 2026 if candidate else "",
                   "", "", "", "", "", source, group, year, missing, confirmed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scope", choices=("baseline", "evidence", "all"), default="all")
    args = parser.parse_args()
    writer = csv.writer(sys.stdout, lineterminator="\n")
    fields = ("state", "team_id", "school", "coach", "first_verified_year", "older_year",
              "older_game_dates", "newer_year", "newer_game_dates", "ledger", "history_lead")
    extra = ("lead_type", "target_year", "missing_evidence", "confirmed_2026_hire")
    writer.writerow(fields if args.scope == "baseline" else fields + extra)
    if args.scope != "evidence":
        for row in sorted(leads()):
            writer.writerow(row if args.scope == "baseline" else row + ("baseline", "", "", ""))
    if args.scope != "baseline":
        writer.writerows(sorted(evidence_leads()))
