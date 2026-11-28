#!/usr/bin/env python3
"""
Strict zero-hallucination unit & dataset integrity tests for the 50-State Coach Index.

Enforces:
  1. Strict entity resolution (`coach_id` keyed to state + primary_team_id + normalized name;
     two same-name coaches at different schools NEVER auto-merge).
  2. Zero autofill backfill (coach verified only in 2026 never receives 2022-2025 games).
  3. Confidence gating (`name_only` coaches emit 0 badges, 0 ranks, 0 stints).
  4. No bottom ranking (strict positive floors + top-50% cap prevent worst-coach lists).
  5. Forfeit exclusion (1-0 / 2-0 forfeits excluded from PPG and scoring margin).
  6. Program Turnaround math & Giant Killer enrollment-tier math.
  7. Ego-trip badge hard cap (<= 3) and live dataset sanity across `public/data/coaches/`.
"""

import json
import sys
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.coaches.build_coach_index import (
    COACHES_OUT_DIR,
    LEADERBOARD_SPECS,
    apply_documented_season,
    build_positive_leaderboard,
    clean_coach_display_name,
    compute_ego_trip_badges,
    compute_window_metrics,
    is_complete_season_ledger,
    is_forfeit_score,
    is_valid_human_coach_name,
    make_canonical_coach_id,
    parse_division_enrollment_rank,
    reconcile_archive_games,
)


def _make_game(
    season: int,
    result: str,
    ts: int,
    os_: int,
    *,
    playoff: bool = False,
    location: str = "H",
    giant_killer: bool = False,
    top25: bool = False,
    forfeit: bool = False,
):
    return {
        "season": season,
        "result": result,
        "team_score": ts,
        "opp_score": os_,
        "is_playoff": playoff,
        "location": location,
        "is_giant_killer_opp": giant_killer,
        "is_top25_opp": top25,
        "is_forfeit": forfeit,
    }


class TestCoachIndexIntegrity(unittest.TestCase):
    def test_archive_record_and_existing_coach_conflicts(self):
        evidence = {"head_coach": "Alex Miller", "archive_record": "8-2-0"}
        games = [{"result": r} for r in "WWWWWWWWLL"]
        self.assertTrue(apply_documented_season({}, evidence, games)["summary_matches_ledger"])
        self.assertFalse(apply_documented_season({}, evidence, games[:-1])["summary_matches_ledger"])
        wrong_results = games[:-1] + [{"result": "W"}]
        self.assertFalse(apply_documented_season({}, evidence, wrong_results)["summary_matches_ledger"])
        conflict = apply_documented_season({"head_coach": "Jim Wilson"}, evidence, games)
        self.assertIsNone(conflict["head_coach"])
        self.assertTrue(conflict["evidence_conflict"])
        self.assertFalse(is_valid_human_coach_name("Coach not listed"))

    def test_summary_archive_layout_and_identity(self):
        from scripts.coaches.season_summary_archives import summary_seasons
        from urllib.error import HTTPError

        class Pages:
            def get(self, url, ttl):
                if url.endswith("A.html"):
                    return ('<title>texashighschoolfootballhistory</title><div id="txtBox_7">'
                            'AUSTIN ACME “Owls”<br>YEAR W-L-T PTS-OPP COACH<br>'
                            '2024 R 8-2-0 280-92 Alex Miller<br>2025\n 7-3-0 201-99 John Jones<br>'
                            'AUSTIN NORTH “Bees”<br>YEAR W-L-T PTS-OPP COACH<br>'
                            '2024 8-2-0 280-92 Alex Miller / John Jones</div>')
                if url.endswith("sum-a.htm"):
                    return ('<title>IHSA Football Season Summaries</title><h3>Austin (Acme)</h3><table>'
                            + ''.join(f'<th>{k}</th>' for k in "Season Class Titles Place Won Lost Tied Coach".split())
                            + '<tr><td>2024-25</td><td></td><td></td><td></td>'
                              '<td>8</td><td>2</td><td>&nbsp;</td><td>Alex Miller</td></tr></table>')
                raise HTTPError(url, 404, "missing letter", {}, None)

        teams = [{"team_id": key, "team": name, "city": "Austin"}
                 for key, name in (("a", "Acme"), ("b", "North"))]
        for state, domain in (("TX", "pigskinprep.com"), ("IL", "ihsa.org")):
            years = dict(summary_seasons(state, [2024, 2025], teams, Pages(), [domain, "maxpreps.com"]))
            self.assertEqual(years[2024]["coaches"]["a"]["archive_record"], "8-2-0")
            self.assertEqual(years[2024]["scores"], {})
            if state == "TX":
                self.assertEqual(years[2024]["disputed"], ["b"])
                self.assertEqual(years[2025]["coaches"]["a"]["head_coach"], "John Jones")
            ambiguous = teams + [{**teams[0], "team_id": "duplicate"}]
            result = dict(summary_seasons(state, [2024], ambiguous, Pages(), [domain, "maxpreps.com"]))
            self.assertNotIn("a", result[2024]["coaches"])

    def test_strict_entity_resolution_no_cross_school_auto_merge(self):
        id_a = make_canonical_coach_id("MI", "mi-romeo", "John Smith")
        id_b = make_canonical_coach_id("MI", "mi-detroit-king", "John Smith")
        id_c = make_canonical_coach_id("OH", "oh-romeo", "John Smith")
        self.assertNotEqual(id_a, id_b)
        self.assertNotEqual(id_a, id_c)
        self.assertTrue(id_a.startswith("coach-mi-romeo-"))
        self.assertTrue(id_b.startswith("coach-mi-detroit-king-"))

    def test_forfeit_scores_excluded_from_ppg_and_margin(self):
        self.assertTrue(is_forfeit_score(2, 0))
        self.assertTrue(is_forfeit_score(1, 0))
        self.assertTrue(is_forfeit_score(0, 2))
        self.assertFalse(is_forfeit_score(3, 0))
        self.assertFalse(is_forfeit_score(28, 14))

        games = [
            _make_game(2026, "W", 35, 7),
            _make_game(2026, "W", 28, 14),
            _make_game(2026, "W", 2, 0),  # administrative forfeit win
        ]
        m = compute_window_metrics(games, None)
        self.assertEqual(m["wins"], 3)
        self.assertEqual(m["games"], 3)
        self.assertEqual(m["non_ff_games"], 2)
        self.assertEqual(m["ppg_for"], 31.5)
        self.assertEqual(m["ppg_against"], 10.5)
        self.assertEqual(m["avg_margin"], 21.0)
        self.assertEqual(m["shutouts"], 0)  # 2-0 forfeit must NOT count as a shutout!

    def test_program_turnaround_delta_calculation(self):
        coach_games = [
            *[_make_game(2025, "W", 35, 14) for _ in range(8)],
            *[_make_game(2025, "L", 14, 28) for _ in range(2)],
            *[_make_game(2026, "W", 42, 7) for _ in range(9)],
            _make_game(2026, "L", 21, 24),
        ]  # 17-3 = 0.850
        prior_games = [
            *[_make_game(2023, "W", 21, 14) for _ in range(2)],
            *[_make_game(2023, "L", 7, 35) for _ in range(7)],
            *[_make_game(2024, "W", 14, 10) for _ in range(2)],
            *[_make_game(2024, "L", 10, 31) for _ in range(7)],
        ]  # 4-14 = 0.2222
        m = compute_window_metrics(coach_games, None, prior_school_games=prior_games)
        self.assertIsNotNone(m["turnaround_delta"])
        self.assertAlmostEqual(m["turnaround_delta"], round(0.85 - (4 / 18), 4), places=3)
        self.assertEqual(m["prior_record"], "4-14")

        self.assertIsNone(compute_window_metrics(coach_games, None, prior_school_games=prior_games[:-1])["turnaround_delta"])
        gapped = [{**g, "season": 2021 if g["season"] == 2023 else 2024} for g in prior_games]
        self.assertIsNone(compute_window_metrics(coach_games, None, prior_school_games=gapped)["turnaround_delta"])

        # Require >= 2 prior seasons and >= 18 prior games so single-year noise never triggers turnaround
        short_prior = [_make_game(2024, "L", 7, 28) for _ in range(9)]
        m_short = compute_window_metrics(coach_games, None, prior_school_games=short_prior)
        self.assertIsNone(m_short["turnaround_delta"])

    def test_division_enrollment_tier_ordering_for_giant_killer(self):
        self.assertLess(parse_division_enrollment_rank("D1"), parse_division_enrollment_rank("D5"))
        self.assertLess(parse_division_enrollment_rank("Division I"), parse_division_enrollment_rank("Division IV"))
        self.assertLess(parse_division_enrollment_rank("6A"), parse_division_enrollment_rank("3A"))
        self.assertIsNone(parse_division_enrollment_rank("8-Man D1"))
        self.assertIsNone(parse_division_enrollment_rank("Independent"))

    def test_ego_trip_badges_strict_cap_of_3_and_minimum_games(self):
        # Super-coach qualifying for 6+ badges must still be strictly capped at 3
        games = [
            *[_make_game(2025, "W", 45, 7, playoff=(i >= 8), location="A", giant_killer=(i < 3), top25=(i < 3)) for i in range(11)],
            *[_make_game(2026, "W", 48, 0, playoff=(i >= 8), location="A", giant_killer=(i < 2), top25=(i < 2)) for i in range(11)],
        ]
        prior = [*[_make_game(2023, "L", 7, 35) for _ in range(9)], *[_make_game(2024, "L", 7, 35) for _ in range(9)]]
        m = compute_window_metrics(games, None, prior_school_games=prior)
        badges = compute_ego_trip_badges(m, 34.0, 14.0, [])
        self.assertEqual(len(badges), 3)
        self.assertEqual(badges[0]["id"], "program_architect")

        # Coach with < 8 games earns ZERO badges
        few = [_make_game(2026, "W", 49, 0) for _ in range(5)]
        self.assertEqual(compute_ego_trip_badges(compute_window_metrics(few, None), 34.0, 14.0, []), [])

    def test_no_bottom_ranking_and_confidence_gating(self):
        win_pct_spec = next(s for s in LEADERBOARD_SPECS if s["id"] == "win_pct")
        coaches = []
        # Create 10 verified coaches from 10-0 down to 1-9 + 2 unverified coaches with 10-0
        for i in range(10):
            wins = 10 - i
            losses = i
            coaches.append({
                "coach_id": f"coach-mi-school-{i}-test",
                "name": f"Coach {i}",
                "state": "MI",
                "current_team_id": f"mi-school-{i}",
                "current_team_name": f"School {i}",
                "current_division": "D1",
                "verification_status": "verified",
                "badges": [],
                "windows": {
                    "2026": {
                        "games": 10,
                        "wins": wins,
                        "losses": losses,
                        "win_pct": wins / 10.0,
                        "seasons_count": 1,
                    }
                },
            })
        coaches.append({
            "coach_id": "coach-mi-unverified-test",
            "name": "Unverified Coach",
            "state": "MI",
            "current_team_id": "mi-unverified",
            "current_team_name": "Unverified High",
            "current_division": "D1",
            "verification_status": "name_only",
            "badges": [],
            "windows": {"2026": {"games": 10, "wins": 10, "losses": 0, "win_pct": 1.0, "seasons_count": 1}},
        })

        board = build_positive_leaderboard(coaches, win_pct_spec, "2026", max_rows=50)
        # No unverified coach can ever appear
        self.assertNotIn("coach-mi-unverified-test", [r["coach_id"] for r in board])
        # Zero coaches below .500 win_pct can appear
        self.assertTrue(all(r["value"] >= 0.50 for r in board))
        # Ordered descending
        vals = [r["value"] for r in board]
        self.assertEqual(vals, sorted(vals, reverse=True))

    def test_spam_bot_placeholder_and_role_name_rejector(self):
        # Must reject every real MaxPreps spam/bot/email/placeholder/non-HC failure caught in forensic audit
        bad_names = [
            "W> Copy gamesports.pro",
            "Head Coach",
            "Coach X",
            "Coach Kaufman",
            "Coach Gelato",
            "agj_01@yahoo.com Jones",
            "michael@carrier.vg Carrier",
            "Montana Football",
            "Daniel (Athletic Director) Threadgill",
            "Jaden Scott (Offensive Coordinator)",
            "Coach Eric Elias HC/OC/QBS)",
            "Yousef jku",
            "Vaweiny 3",
            "Danny Chan ('92)",
            "Elliott Turner II (2017 - Present)",
        ]
        for bn in bad_names:
            self.assertFalse(is_valid_human_coach_name(bn), f"Failed to reject junk coach name: {bn!r}")

        # Must preserve real coaches and cleanly normalize benign formatting
        self.assertTrue(is_valid_human_coach_name("Chad Miller"))
        self.assertTrue(is_valid_human_coach_name("Rob Zimmerman"))
        self.assertTrue(is_valid_human_coach_name("Ty Detmer"))
        self.assertTrue(is_valid_human_coach_name("RJ McDonald"))
        self.assertTrue(is_valid_human_coach_name("Coach Bob Barone"))
        self.assertEqual(clean_coach_display_name("Coach Bob Barone"), "Bob Barone")
        self.assertEqual(clean_coach_display_name("Jeremi Carter Jeremi Carter"), "Jeremi Carter")
        self.assertEqual(clean_coach_display_name("Lance Mitchell - Head Coach"), "Lance Mitchell")
        self.assertEqual(clean_coach_display_name("rob zimmerman"), "Rob Zimmerman")

    def test_complete_season_ledger_gate(self):
        partial = [{**_make_game(2025, "W", 28, 7), "source": "maxpreps.com"}] * 8
        self.assertFalse(is_complete_season_ledger(2025, partial, "8-3"))
        self.assertTrue(is_complete_season_ledger(2025, partial, "8-0"))
        self.assertFalse(is_complete_season_ledger(2025, [_make_game(2025, "W", 28, 7)] * 7, "7-0"))
        self.assertFalse(is_complete_season_ledger(2026, [_make_game(2026, "W", 28, 7) for _ in range(5)]))
        self.assertTrue(is_complete_season_ledger(2026, [_make_game(2026, "W", 28, 7) for _ in range(9)]))
        self.assertTrue(is_complete_season_ledger(2020, [_make_game(2020, "W", 28, 7) for _ in range(7)]))
        self.assertFalse(is_complete_season_ledger(2020, [_make_game(2020, "W", 28, 7) for _ in range(4)]))

    def test_real_duplicate_fixture_and_explicit_playoff_metadata(self):
        from scripts.coaches.harvest_multiyear_coaches import parse_schedule_contests_html

        # MaxPreps lists both sides' entries for Ogemaw Heights vs Benzie
        # Central on 2022-10-07. Both entries have a completed-game flag of 4.
        def contest(game_id, opponent, date, team_score, opp_score):
            def team(sid, score, result):
                row = [None] * 17
                row[1], row[5], row[6], row[14] = sid, result, score, sid
                return row
            row = [None] * 30
            result = "W" if team_score > opp_score else "L"
            row[0] = [team("ogemaw", team_score, result), team(opponent, opp_score, "L" if result == "W" else "W")]
            row[1], row[11], row[15] = game_id, date, 4
            return row

        original = contest("a", "benzie", "2022-10-07", 40, 14)
        duplicate = contest("b", "benzie", "2022-10-07", 40, 14)
        playoff = contest("c", "gladwin", "2022-10-28", 20, 35)
        cancelled = contest("d", "other", "2022-10-14", 21, 0)
        cancelled[28] = "Game cancelled"
        props = {"teamContext": {"data": {"year": "22-23", "teamId": "ogemaw"}},
                 "contests": [original, duplicate, playoff, cancelled],
                 "tournaments": [{"contestId": "c", "isTournamentPlayOff": True}]}
        html = '<script id="__NEXT_DATA__">' + json.dumps({"props": {"pageProps": props}}) + "</script>"
        rows = parse_schedule_contests_html(html, "ogemaw", 2022)
        self.assertEqual([(r["game_id"], r["is_playoff"]) for r in rows], [("a", False), ("c", True)])
        for sid, year in [("ogemaw", 2023), ("another-school", 2022)]:
            with self.assertRaises(ValueError):
                parse_schedule_contests_html(html, sid, year)

    def test_state_archive_precedence_and_ambiguous_partial_seasons(self):
        base = {"season": 2022, "date": "2022-10-14", "opp_name": "Mill Creek", "opp_id": "ga-mill-creek",
                "team_score": 38, "opp_score": 27, "result": "W", "location": "H", "is_playoff": False}
        primary = {**base, "source": "ghsa.net", "team_score": 39, "location": "U"}
        postseason = {**base, "date": "2022-11-11", "is_playoff": True}
        merged = reconcile_archive_games([base, postseason], [primary], False)
        self.assertEqual(len(merged), 2)
        self.assertEqual((merged[0]["team_score"], merged[0]["location"], merged[0]["source"]), (39, "H", "ghsa.net"))
        self.assertTrue(merged[1]["is_playoff"])
        unknown = {**primary, "opp_id": "another-school"}
        self.assertEqual(reconcile_archive_games([base], [unknown], False), [base])
        self.assertEqual(reconcile_archive_games([base], [unknown], True), [unknown])

    def test_checkpoint_retries_transient_failure_and_preserves_completed_seasons(self):
        from scripts.coaches import harvest_multiyear_coaches as harvest

        calls = []
        def fetch(task, pacer):
            year = task[-1]
            calls.append(year)
            if year == 2021 and calls.count(year) == 1:
                raise TimeoutError("temporary")
            if year == 2020:
                return "absent", None
            return "ok", [{"season": year}]

        links = [{"school_id": "a", "team_link": "https://example.test/football/"}]
        with TemporaryDirectory() as tmp, patch.object(harvest, "CACHE_DIR", Path(tmp)), \
                patch.object(harvest, "fetch_result", side_effect=fetch):
            def run(cache):
                return harvest.harvest_state("TX", links, ["schedule"], cache, [2019, 2020, 2021], 1, None)

            self.assertEqual(run({"schedule": {}}), 1)
            rebuilt = {"schedule": {"TX": {"a": {"2020": [{"season": 2020}]}}}}
            self.assertEqual(run(rebuilt), 0)
            self.assertEqual(calls, [2019, 2020, 2021, 2021])
            self.assertEqual(sorted(rebuilt["schedule"]["TX"]["a"]), ["2019", "2021"])
            self.assertEqual(run({"schedule": {}}), 0)
            self.assertEqual(len(calls), 4)
            path = Path(tmp) / "scores.json"
            harvest.atomic_json(path, rebuilt)
            self.assertEqual(harvest.load_cache(path), rebuilt)
            with self.assertRaises(TypeError):
                harvest.atomic_json(path, object())
            self.assertEqual(harvest.load_cache(path), rebuilt)
        self.assertGreaterEqual(harvest.retry_delay("20", 1), 20)

    def test_rank_denominators_and_compact_windows_before_display_cap(self):
        spec = next(s for s in LEADERBOARD_SPECS if s["id"] == "win_pct")
        coaches = []
        for i in range(80):
            metrics = {"wins": 30 + i, "losses": 2, "games": 32 + i, "win_pct": 0.9,
                       "seasons_count": 3}
            coaches.append({"name": str(i), "coach_id": str(i), "verification_status": "verified",
                            "state": "GA" if i % 2 else "AL", "current_team_id": str(i),
                            "current_team_name": str(i), "current_division": "A",
                            "ranks": {"5yr": {"win_pct": {"rank": 1}}}, "windows": {"4yr": metrics}})
        rows = build_positive_leaderboard(coaches, spec, "5yr", national=True)
        self.assertEqual(len(rows), 40)
        self.assertEqual((rows[0]["state_rank"], rows[0]["state_total"], rows[0]["national_rank"],
                          rows[0]["national_total"]), (1, 40, 1, 80))
        self.assertEqual(coaches[0]["ranks"]["5yr"]["win_pct"]["national_rank"], 80)

    def test_live_generated_coach_index_integrity(self):
        summary_path = COACHES_OUT_DIR / "national_summary.json"
        cards_path = COACHES_OUT_DIR / "team_coach_cards.json"
        mi_path = COACHES_OUT_DIR / "coaches_mi.json"
        oh_path = COACHES_OUT_DIR / "coaches_oh.json"
        ca_path = COACHES_OUT_DIR / "coaches_ca.json"
        ks_path = COACHES_OUT_DIR / "coaches_ks.json"
        if not summary_path.exists():
            self.skipTest("Run scripts/coaches/build_coach_index.py first")

        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        self.assertGreaterEqual(summary["total_verified_coaches"], 10000)
        self.assertEqual(len(summary["states"]), 50)
        self.assertTrue(all(st["multi_year"] > 0 for st in summary["states"].values()))
        for window, categories in summary["leaderboards"].items():
            for category, board in categories.items():
                for position, row in enumerate(board, 1):
                    self.assertEqual(row["national_rank"], position, (window, category, row["coach_id"]))
                    self.assertLessEqual(position, row["national_total"])
                    self.assertLessEqual(row["state_rank"], row["state_total"])
                    self.assertLessEqual(row["state_total"], row["national_total"])

        cards = json.loads(cards_path.read_text(encoding="utf-8"))["teams"]
        self.assertGreaterEqual(len(cards), 10000)

        # 1. Verify confidence gating across ALL team coach cards
        for tid, card in cards.items():
            self.assertLessEqual(len(card.get("badges") or []), 3, f"Too many badges on {tid}")
            if card.get("status") != "verified":
                self.assertEqual(card.get("badges"), [], f"Unverified coach {tid} leaked badges!")
                self.assertEqual(card.get("honors"), [], f"Unverified coach {tid} leaked honors!")
                self.assertEqual(card.get("qualifying_boards", 0), 0, f"Unverified coach {tid} leaked ranks!")

        # 2. Global 50-state Fail-the-Build Gates:
        #    - ZERO spam/bot/email/placeholder/non-HC names
        #    - ZERO Swiss-cheese internal gap years in any verified coach's verified_years
        #    - ZERO truncated incomplete seasons (<8 non-2020 or <6 in 2020)
        for p in sorted(COACHES_OUT_DIR.glob("coaches_*.json")):
            d = json.loads(p.read_text(encoding="utf-8"))
            state_summary = summary["states"][d["state"]]
            deltas = [coach["windows"].get("career", {}).get("turnaround_delta")
                      for coach in d["coaches"] if coach["verification_status"] == "verified"]
            measured = [delta for delta in deltas if delta is not None]
            self.assertEqual(state_summary["architect_measured"], len(measured))
            qualified = sum(delta >= 0.05 for delta in measured)
            self.assertEqual(state_summary["architect_ranked"], qualified)
            board = d["leaderboards"].get("career", {}).get("turnaround", [])
            self.assertEqual(board[0]["total"] if board else 0, qualified)
            for c in d["coaches"]:
                self.assertTrue(
                    is_valid_human_coach_name(c["name"]),
                    f"Junk/spam coach name leaked into {p.name}: {c['name']!r} ({c['current_team_name']})",
                )
                if c["verification_status"] != "verified":
                    continue
                vy = sorted(c["verified_years"])
                self.assertGreaterEqual(len(vy), 1, f"Verified coach {c['coach_id']} has empty verified_years")
                expected_contiguous = list(range(vy[0], vy[-1] + 1))
                self.assertEqual(
                    vy,
                    expected_contiguous,
                    f"Swiss-cheese gap year in {p.name} for {c['name']} ({c['current_team_name']}): {vy}",
                )
                career = c.get("windows", {}).get("career", {})
                if career.get("turnaround_delta") is not None:
                    baseline = c["turnaround_baseline"]
                    self.assertGreaterEqual(len(baseline), 2)
                    first = c["stints"][0]["start_year"]
                    self.assertEqual([row["year"] for row in baseline], list(range(first - len(baseline), first)))
                    total = 0
                    for row in baseline:
                        self.assertTrue(is_valid_human_coach_name(row["head_coach"]))
                        self.assertNotEqual(clean_coach_display_name(row["head_coach"]).casefold(), c["name"].casefold())
                        gp = row["wins"] + row["losses"] + row["ties"]
                        self.assertGreaterEqual(gp, 6 if row["year"] == 2020 else 8)
                        total += gp
                    self.assertGreaterEqual(total, 18)
                for wk, placements in c.get("ranks", {}).items():
                    for cat, placement in placements.items():
                        self.assertEqual((placement["state_rank"], placement["state_total"]),
                                         (placement["rank"], placement["total"]), (c["coach_id"], wk, cat))
                        self.assertLessEqual(placement["national_rank"], placement["national_total"])
                for s in c.get("seasons") or []:
                    gp = s["wins"] + s["losses"] + s["ties"]
                    min_gp = 6 if s["year"] == 2020 else 8
                    self.assertGreaterEqual(
                        gp,
                        min_gp,
                        f"Truncated season in {p.name}: {c['name']} ({s['year']}: {s['wins']}-{s['losses']})",
                    )

        # 3. Authoritative Michigan multi-source check (michigan-football.com):
        #    West Branch Ogemaw Heights (Chad Miller): exact 60-25 across unbroken 2019-2026 + turnaround vs 6-39!
        mi = json.loads(mi_path.read_text(encoding="utf-8"))
        ogemaw = next((c for c in mi["coaches"] if "ogemaw" in c["current_team_name"].lower()), None)
        self.assertIsNotNone(ogemaw)
        self.assertEqual(ogemaw["name"], "Chad Miller")
        self.assertEqual(ogemaw["verification_status"], "verified")
        self.assertEqual(ogemaw["verified_years"], [2019, 2020, 2021, 2022, 2023, 2024, 2025, 2026])
        og_career = ogemaw["windows"]["career"]
        self.assertEqual((og_career["wins"], og_career["losses"]), (60, 25))
        self.assertEqual(og_career["prior_record"], "6-39")
        self.assertAlmostEqual(og_career["turnaround_delta"], 60 / 85 - 6 / 45, places=4)

        # 4. OH hijacked spam/conflict gates: Glenville, Massillon Washington, Olentangy Liberty must NEVER publish spam/hijacked names
        oh = json.loads(oh_path.read_text(encoding="utf-8"))
        oh_verified_names = {c["current_team_id"]: c["name"] for c in oh["coaches"] if c["verification_status"] == "verified"}
        for bad_sub in ("gamesports", "emeli angel", "lesley talley"):
            for tid, nm in oh_verified_names.items():
                self.assertNotIn(bad_sub, nm.lower(), f"Hijacked/spam OH coach leaked on {tid}: {nm}")

        # 5. CA edge-case gates: Pomona doubled name fixed; short <8G Foothill Bakersfield demoted to name_only
        ca = json.loads(ca_path.read_text(encoding="utf-8"))
        pomona = next((c for c in ca["coaches"] if c["current_team_id"] == "ca-pomona"), None)
        if pomona:
            self.assertEqual(pomona["name"], "Jeremi Carter")
        foothill_bak = next((c for c in ca["coaches"] if c["current_team_id"] == "ca-foothill-bakersfield"), None)
        if foothill_bak:
            self.assertEqual(foothill_bak["verification_status"], "name_only")

        # 6. KS: Bishop Miege must be Head Coach Jon Holmes, NOT Statistician Alan Peel
        ks = json.loads(ks_path.read_text(encoding="utf-8"))
        miege = next((c for c in ks["coaches"] if "bishop miege" in c["current_team_name"].lower()), None)
        if miege:
            self.assertEqual(miege["name"], "Jon Holmes")
            self.assertNotIn("alan peel", miege["name"].lower())


if __name__ == "__main__":
    unittest.main()
