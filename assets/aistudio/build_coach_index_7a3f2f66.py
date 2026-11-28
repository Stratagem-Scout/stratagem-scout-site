#!/usr/bin/env python3
"""
Zero-Hallucination 50-State Coach Index Builder for Stratagem Scout.

HARD DATA INTEGRITY INVARIANTS (ENFORCED BY UNIT TESTS):
  1. STRICT ENTITY RESOLUTION:
     - `coach_id` is deterministically keyed to `normalized_name + primary_school_id + state`
       (e.g., `coach-mi-west-branch-ogemaw-heights-chad-miller`).
     - NEVER auto-merges coaches across schools by name alone! Cross-school career stints
       are merged ONLY when verified by an identical MaxPreps `contact_id` UUID across
       explicit `Head Coach` staff listings OR an explicit verified entry in
       `registry/coach_cross_school_links.json`.
  2. ZERO AUTOFILL BACKFILL:
     - NEVER attaches a school's historical seasons to a coach unless that EXACT season is
       explicitly verified in `coaching_stints` (`position == 'Head Coach'` for 2026, or
       explicit matching `headCoach` on `/history/` that does not belong to an assistant or
       statistician in `initStaffList` for prior years).
     - Seasons with blank/missing coach names (`" - "`) are NEVER interpolated or assumed.
  3. VERIFIED GAME-SCORE COMPUTATION ONLY:
     - Every Win/Loss, PPG Scored, PPG Allowed, Margin, Playoff Win, Giant Killer Win,
       Clutch 1-Score Win, Road Warrior Win, Top-25 Win, Shutout, and Program Architect
       metric is computed strictly from verified game-by-game score ledgers (excluding
       administrative forfeits from scoring averages & margin).
  4. CONFIDENCE GATING:
     - `verification_status == 'verified'` requires BOTH a verified head coach stint AND
       corroborated verified game scores (`>= 1` verified game).
     - `verification_status == 'name_only'` (no corroborated games) emits 0 badges, 0 ranks,
       and renders plain text only + account-gated "Submit / Claim Coach Record" CTA.
  5. NO BOTTOM RANKING (ZERO-SHAME GUARANTEE):
     - All 12 leaderboards rank strictly positive prestige metrics.
     - Minimum game floors are strictly enforced per window & category.
     - Each leaderboard is capped at `min(50, floor(qualifying_coaches * 0.5))` (or positive
       performance threshold) so a "worst coaches" list can NEVER emerge even in small states.

Outputs:
  - `public/data/coaches/coaches_{st}.json` (50 compact per-state files, lazy-loaded)
  - `public/data/coaches/team_coach_cards.json` (lightweight map by `team_id` for Team Pages & SEO prerender)
  - `public/data/coaches/national_summary.json` (global coach search index + national Top 50 leaderboards)
"""

from __future__ import annotations

import argparse
import csv
import datetime
import glob
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

ROOT_DIR = Path(__file__).resolve().parents[2]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from scripts.coaches.audit_architect_coverage import coverage_counts
from scripts.coaches.harvest_multiyear_coaches import atomic_json
from scripts.backfill_team_colors import (
    STATE_WIKIDATA_QIDS,
    match_candidate_to_state_team,
    normalize_city,
    normalize_school_base_name,
)

PUBLIC_DATA_DIR = ROOT_DIR / "public" / "data"
COACHES_OUT_DIR = PUBLIC_DATA_DIR / "coaches"
STAFF_CACHE_PATH = ROOT_DIR / ".cache" / "maxpreps_staff_cache.json"
HISTORY_CACHE_PATH = ROOT_DIR / ".cache" / "coaches" / "history_by_school.json"
HIST_SCORES_CACHE_PATH = ROOT_DIR / ".cache" / "coaches" / "historical_scores_by_state.json"
MI_DW_SCORES_PATH = ROOT_DIR / ".cache" / "coaches" / "mi_dw_scores_2014_2026.json"
CROSS_SCHOOL_LINKS_PATH = ROOT_DIR / "registry" / "coach_cross_school_links.json"
HONORS_MHSFCA_PATH = ROOT_DIR / "registry" / "coach_honors_mhsfca.json"
HONORS_OVERRIDES_PATH = ROOT_DIR / "registry" / "coach_honors_overrides.json"

ALL_STATES = sorted(STATE_WIKIDATA_QIDS.keys())

# Rolling year windows supported in Coach Index UI
ROLLING_WINDOWS: List[Tuple[str, Optional[int]]] = [
    ("2026", 1),
    ("2yr", 2),
    ("4yr", 4),
    ("5yr", 5),
    ("7yr", 7),
    ("career", None),
]

# The 12 strictly positive, 100%-verifiable leaderboard categories
LEADERBOARD_SPECS: List[Dict[str, Any]] = [
    {"id": "win_pct", "label": "Win Percentage", "higher_is_better": True, "min_games_1yr": 8, "min_games_multi": 18, "min_value": 0.50},
    {"id": "turnaround", "label": "Program Architect", "higher_is_better": True, "min_games_1yr": 999, "min_games_multi": 16, "min_value": 0.05},
    {"id": "postseason", "label": "Postseason Wizard", "higher_is_better": True, "min_games_1yr": 3, "min_games_multi": 4, "min_value": 0.50},
    {"id": "giant_killer", "label": "Giant Killer", "higher_is_better": True, "min_games_1yr": 2, "min_games_multi": 2, "min_value": 2},
    {"id": "offense_ppg", "label": "Offensive Juggernaut", "higher_is_better": True, "min_games_1yr": 8, "min_games_multi": 15, "min_value": 24.0},
    {"id": "defense_ppg", "label": "Iron Curtain Defense", "higher_is_better": False, "min_games_1yr": 8, "min_games_multi": 15, "max_value": 22.0},
    {"id": "margin", "label": "Point Differential", "higher_is_better": True, "min_games_1yr": 8, "min_games_multi": 15, "min_value": 5.0},
    {"id": "clutch", "label": "Clutch Closer (1-Score)", "higher_is_better": True, "min_games_1yr": 3, "min_games_multi": 5, "min_value": 0.55},
    {"id": "road_warrior", "label": "Road Warrior", "higher_is_better": True, "min_games_1yr": 4, "min_games_multi": 8, "min_value": 0.55},
    {"id": "ranked_wins", "label": "Quality Top-25 Wins", "higher_is_better": True, "min_games_1yr": 2, "min_games_multi": 2, "min_value": 2},
    {"id": "shutouts", "label": "Shutout Control", "higher_is_better": True, "min_games_1yr": 2, "min_games_multi": 3, "min_value": 2},
    {"id": "total_wins", "label": "Total Verified Wins", "higher_is_better": True, "min_games_1yr": 8, "min_games_multi": 12, "min_value": 8},
]

# State enrollment division tier orderings (index 0 = largest enrollment tier -> higher index = smaller enrollment)
# Used exclusively for verifiable Giant Killer "punch-up" win computation.
DIVISION_TIER_PATTERNS: List[Tuple[re.Pattern[str], int]] = [
    (re.compile(r"\b(?:8A|OPEN|SUPER)\b", re.I), 1),
    (re.compile(r"\b7A\b", re.I), 2),
    (re.compile(r"\b6A\b", re.I), 3),
    (re.compile(r"\b5A\b", re.I), 4),
    (re.compile(r"\b4A\b", re.I), 5),
    (re.compile(r"\b3A\b", re.I), 6),
    (re.compile(r"\b2A\b", re.I), 7),
    (re.compile(r"\b1A\b", re.I), 8),
    (re.compile(r"\b(?:CLASS\s*A|C6|GROUP\s*5|LL|11AAA)\b", re.I), 2),
    (re.compile(r"\b(?:CLASS\s*B|C5|GROUP\s*4|L|11AA)\b", re.I), 4),
    (re.compile(r"\b(?:CLASS\s*C|C4|GROUP\s*3|MM|11A)\b", re.I), 6),
    (re.compile(r"\b(?:CLASS\s*D|C3|GROUP\s*2|M|11B)\b", re.I), 8),
    (re.compile(r"\b(?:C2|GROUP\s*1|SS)\b", re.I), 9),
    (re.compile(r"\b(?:C1|S)\b", re.I), 10),
]


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", str(text or "").strip().lower()).strip("-")


def normalize_coach_name(name: str) -> str:
    s = re.sub(r"\b(?:coach|hc|jr\.?|sr\.?|iii|ii|iv)\b", "", str(name or "").strip(), flags=re.I)
    s = re.sub(r"[^a-zA-Z\s'-]+", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


JUNK_COACH_RE = re.compile(
    r"(https?://|www\.|\.(?:com|net|org|pro|io|tv|live|edu|gov|vg)\b|@|"
    r"\b(?:copy|watch|stream|livestream|gamesport|gamesports|click|free|football|varsity|"
    r"baseball|basketball|soccer|softball|track|wrestling|staff|admin|athletic|director|"
    r"booster|offensive|defensive|coordinator|assistant|statistician|manager|principal|"
    r"superintendent|unknown|vacant|tbd|tba|none|n/a|open|interim|present)\b|"
    r"[0-9><@#$%^&*=+_/\\]|\bhc\b|\boc\b|\bdc\b|\bqbs?\b)",
    re.I,
)
TRAILING_HC_TAG_RE = re.compile(
    r"\s*(?:[-–—]\s*(?:varsity\s+)?head\s+coach|\(\s*(?:varsity\s+)?head\s+coach\s*\))\s*$",
    re.I,
)
LEADING_COACH_PREFIX_RE = re.compile(r"^coach\s+", re.I)
VALID_SHORT_TOKENS = {"ii", "iii", "iv", "jr", "sr", "st", "mc", "de", "la", "le", "el", "al", "ed", "ty", "bo", "cy", "jo", "mo", "rj", "dj", "aj", "cj", "bj", "jd", "jp", "jt", "tj", "jj", "pj", "kc", "kj", "mk", "jb", "jc", "jr"}


def clean_coach_display_name(name: str) -> str:
    s = re.sub(r"\s+", " ", str(name or "").strip())
    if not s:
        return ""
    # Strip benign explicit trailing " - Head Coach" or "(Head Coach)" label
    s = TRAILING_HC_TAG_RE.sub("", s).strip()
    # Strip leading "Coach " ONLY if at least 2 real words remain (e.g., "Coach Bob Barone" -> "Bob Barone", leaving bare "Coach Kaufman" rejected)
    if LEADING_COACH_PREFIX_RE.match(s):
        rest = LEADING_COACH_PREFIX_RE.sub("", s).strip()
        if len(rest.split()) >= 2:
            s = rest
    # Collapse doubled full names ("Jeremi Carter Jeremi Carter" -> "Jeremi Carter")
    toks = s.split(" ")
    if len(toks) == 4 and [t.lower() for t in toks[:2]] == [t.lower() for t in toks[2:]]:
        s = " ".join(toks[:2])
    # Normalize all-lowercase ("rob zimmerman") or ALL-CAPS ("JOHN SMITH") while preserving real mixed-case ("McDonald", "O'Brien")
    if s == s.lower() or s == s.upper():
        parts = []
        for token in s.split(" "):
            if re.match(r"^(?:ii|iii|iv)$", token, re.I):
                parts.append(token.upper())
            elif re.match(r"^(?:mc)([a-z].*)$", token, re.I):
                m = re.match(r"^(mc)(.*)$", token, re.I)
                parts.append("Mc" + m.group(2).capitalize())
            elif "'" in token:
                parts.append("'".join(p.capitalize() for p in token.split("'")))
            elif "-" in token:
                parts.append("-".join(p.capitalize() for p in token.split("-")))
            else:
                parts.append(token.capitalize())
        s = " ".join(parts)
    return s


def is_valid_human_coach_name(raw_name: str) -> bool:
    """
    GATE 1 — Hard Spam / Bot / URL / Email / Placeholder / Non-HC-Role Rejector.
    Rejects livestream spam bots ("W> Copy gamesports.pro"), emails ("agj_01@yahoo.com Jones"),
    placeholders ("Head Coach", "Coach X", "Montana Football"), non-HC roles, and keyboard smash ("Yousef jku").
    """
    if not raw_name or re.search(r"\bnot (?:listed|available|reported)\b", raw_name, re.I):
        return False
    cleaned = clean_coach_display_name(raw_name)
    if not cleaned or len(cleaned) < 4 or len(cleaned) > 40:
        return False
    # Reject anything with parentheses left over after stripping benign "(Head Coach)" (catches "('92)", "(Coach Blue)", "(Offensive Coordinator)")
    if "(" in cleaned or ")" in cleaned or '"' in cleaned:
        return False
    # Reject bare "Coach Lastname" or "Coach X" (missing verified first name!)
    if LEADING_COACH_PREFIX_RE.match(cleaned):
        return False
    if JUNK_COACH_RE.search(cleaned):
        return False
    # Reject bare "Head Coach" placeholder
    if re.search(r"\bhead\s+coach\b", cleaned, re.I):
        return False

    tokens = [t.strip(".,'") for t in cleaned.split() if t.strip(".,'")]
    if len(tokens) < 2 or len(tokens) > 4:
        return False

    # First name & last name (excluding Jr/Sr/II/III/IV suffix) must be plausible real name tokens
    core_tokens = [t for t in tokens if t.lower() not in ("jr", "sr", "ii", "iii", "iv")]
    if len(core_tokens) < 2:
        return False
    first_tok, last_tok = core_tokens[0], core_tokens[-1]
    if len(last_tok) < 2 or len(first_tok) < 2:
        return False
    # Reject spam-bot casing where first name is Capitalized and final surname is all-lowercase ("Yousef jku", "Emeli angel", "Paxto neshua")
    if first_tok[0].isupper() and last_tok == last_tok.lower() and last_tok not in VALID_SHORT_TOKENS:
        return False
    for tok in core_tokens:
        alpha = re.sub(r"[^a-zA-Z]", "", tok)
        if not alpha:
            return False
        low = alpha.lower()
        # Reject keyboard-smash tokens (>= 3 letters with zero vowels a/e/i/o/u/y or impossible consonant starts like "jku")
        if len(low) >= 3 and low not in VALID_SHORT_TOKENS and (
            not re.search(r"[aeiouy]", low) or re.match(r"^(?:jk|qx|zx|vx|kq|bq|fq|xz|qz)", low)
        ):
            return False
        # Reject single-letter last names or weird repeated chars ("xxx")
        if re.search(r"(.)\1\1", low):
            return False
    return True


def is_complete_season_ledger(
    year: int,
    yr_games: Sequence[Dict[str, Any]],
    hist_overall_record: Optional[str] = None,
) -> bool:
    """
    GATE 2 — Complete-Season Game Floor.
    Rejects truncated partial-scrape seasons (e.g. 0-5, 4-2, 1-4) so incomplete game logs never distort career records.
    """
    if len(yr_games) < (6 if year == 2020 else 8):
        return False
    if year >= 2026 or not hist_overall_record:
        return True
    record = re.fullmatch(r"(\d+)-(\d+)(?:-(\d+))?", hist_overall_record.strip())
    if not record or not any(g.get("source") == "maxpreps.com" for g in yr_games):
        return True
    expected = tuple(int(v or 0) for v in record.groups())
    if not sum(expected):
        return True
    if all(g.get("source") == "maxpreps.com" for g in yr_games):
        outcomes = Counter(g["result"] for g in yr_games)
        return tuple(outcomes[r] for r in "WLT") == expected
    # A primary source may correct an individual score. A higher published
    # game count still identifies an incomplete, partially matched schedule.
    return len(yr_games) >= sum(expected)


def matches_archive_record(games, record):
    outcomes = Counter(game["result"] for game in games)
    return set(outcomes) <= set("WLT") and "-".join(str(outcomes[r]) for r in "WLT") == record


def apply_documented_season(history, evidence, games):
    """Require independent game totals when a summary publishes a final record."""
    merged = {**history, **evidence}
    old, new = history.get("head_coach"), evidence.get("head_coach")
    if (history.get("evidence_conflict") or evidence.get("evidence_conflict")
            or (is_valid_human_coach_name(old) and normalize_coach_name(old) != normalize_coach_name(new))):
        merged["head_coach"] = None
        merged["evidence_conflict"] = True
    record = evidence.get("archive_record")
    if record:
        merged["summary_matches_ledger"] = (matches_archive_record(games, record)
                                            and history.get("archive_record", record) == record)
    return merged


def make_canonical_coach_id(state: str, primary_team_id: str, coach_name: str) -> str:
    """Strict entity resolution: coach_id is always scoped to state + primary_school_id + normalized name."""
    st_l = state.strip().lower()
    team_slug = slugify(primary_team_id)
    if team_slug.startswith(f"{st_l}-"):
        team_slug = team_slug[len(st_l) + 1 :]
    name_slug = slugify(normalize_coach_name(coach_name))
    return f"coach-{st_l}-{team_slug}-{name_slug}"


def parse_division_enrollment_rank(div_label: str) -> Optional[int]:
    """
    Maps an official state association enrollment division string to a relative ordinal
    (smaller number = strictly LARGER school enrollment). Returns None if non-enrollment/ambiguous.
    """
    if not div_label:
        return None
    s = str(div_label).strip().upper()
    if any(skip in s for skip in ("IND", "PRIV", "8-MAN", "9-MAN", "6-MAN", "8 MAN", "9 MAN", "6 MAN", "ALL")):
        return None
    # Explicit D1..D8 / Division 1..Division 14
    m_d = re.search(r"(?:^|\b)(?:D|DIV(?:ISION)?\s*)(\d{1,2})\b", s)
    if m_d:
        return int(m_d.group(1))
    # Roman numeral divisions (Division I..VII)
    roman_map = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7, "VIII": 8}
    m_r = re.search(r"\bDIV(?:ISION)?\s+(VIII|VII|VI|IV|V|III|II|I)\b", s)
    if m_r:
        return roman_map.get(m_r.group(1))
    for pat, rank_val in DIVISION_TIER_PATTERNS:
        if pat.search(s):
            return rank_val
    return None


def is_forfeit_score(team_score: int, opp_score: int, explicitly_forfeit: bool = False) -> bool:
    if explicitly_forfeit:
        return True
    hi, lo = max(team_score, opp_score), min(team_score, opp_score)
    return lo == 0 and hi in (1, 2)


def compute_window_metrics(
    games: Sequence[Dict[str, Any]],
    window_years: Optional[int],
    as_of_year: int = 2026,
    prior_school_games: Optional[Sequence[Dict[str, Any]]] = None,
    min_prior_seasons: int = 2,
) -> Dict[str, Any]:
    """
    Computes all verified coaching stats strictly from verified game score rows.
    Excludes administrative forfeits from PPG & scoring margin calculations.
    """
    if window_years is not None:
        min_yr = as_of_year - window_years + 1
        scoped = [g for g in games if int(g.get("season", 2026)) >= min_yr]
    else:
        scoped = list(games)

    wins = losses = ties = 0
    non_ff_games = 0
    pts_for = 0
    pts_against = 0
    playoff_w = playoff_l = 0
    one_score_w = one_score_l = 0
    road_w = road_l = 0
    giant_killer_w = 0
    giant_killer_g = 0
    ranked_w = 0
    ranked_g = 0
    shutouts = 0
    hold_under_8 = 0
    seasons_by_yr: Dict[int, List[int]] = defaultdict(lambda: [0, 0, 0])

    for g in scoped:
        yr = int(g.get("season", 2026))
        ts = int(g.get("team_score", 0))
        os_ = int(g.get("opp_score", 0))
        res = str(g.get("result") or "").upper()
        if res not in ("W", "L", "T"):
            res = "W" if ts > os_ else "L" if ts < os_ else "T"

        if res == "W":
            wins += 1
            seasons_by_yr[yr][0] += 1
        elif res == "L":
            losses += 1
            seasons_by_yr[yr][1] += 1
        else:
            ties += 1
            seasons_by_yr[yr][2] += 1

        ff = is_forfeit_score(ts, os_, bool(g.get("is_forfeit", False)))
        if not ff:
            non_ff_games += 1
            pts_for += ts
            pts_against += os_
            if res == "W" and os_ == 0:
                shutouts += 1
            if res == "W" and os_ <= 7:
                hold_under_8 += 1

            margin = abs(ts - os_)
            if margin <= 8:
                if res == "W":
                    one_score_w += 1
                elif res == "L":
                    one_score_l += 1

        if g.get("is_playoff"):
            if res == "W":
                playoff_w += 1
            elif res == "L":
                playoff_l += 1

        loc = str(g.get("location") or "").upper()
        if loc in ("A", "N"):
            if res == "W":
                road_w += 1
            elif res == "L":
                road_l += 1

        if g.get("is_giant_killer_opp"):
            giant_killer_g += 1
            if res == "W":
                giant_killer_w += 1

        if g.get("is_top25_opp"):
            ranked_g += 1
            if res == "W":
                ranked_w += 1

    gp = wins + losses + ties
    win_pct = round(wins / gp, 4) if gp > 0 else 0.0
    ppg_for = round(pts_for / non_ff_games, 1) if non_ff_games > 0 else 0.0
    ppg_against = round(pts_against / non_ff_games, 1) if non_ff_games > 0 else 0.0
    avg_margin = round((pts_for - pts_against) / non_ff_games, 1) if non_ff_games > 0 else 0.0

    po_gp = playoff_w + playoff_l
    po_win_pct = round(playoff_w / po_gp, 4) if po_gp > 0 else 0.0

    os_gp = one_score_w + one_score_l
    one_score_pct = round(one_score_w / os_gp, 4) if os_gp > 0 else 0.0

    rd_gp = road_w + road_l
    road_pct = round(road_w / rd_gp, 4) if rd_gp > 0 else 0.0

    winning_seasons = sum(1 for w, l, _ in seasons_by_yr.values() if w > l and (w + l) >= 5)
    nine_win_seasons = sum(1 for w, _, _ in seasons_by_yr.values() if w >= 9)

    # Program Architect: coach win % at current school minus same school's verified pre-hire win %
    turnaround_delta: Optional[float] = None
    prior_record_str: Optional[str] = None
    if prior_school_games and len(seasons_by_yr) >= 2 and gp >= 16:
        prior_by_yr = defaultdict(list)
        for pg in prior_school_games:
            if pg.get("result") in ("W", "L", "T"):
                prior_by_yr[int(pg["season"])].append(pg)
        prior_years = sorted(prior_by_yr)
        current_years = sorted({int(g["season"]) for g in games})
        prior_gp = sum(map(len, prior_by_yr.values()))
        if (len(prior_years) >= max(2, min_prior_seasons) and prior_gp >= 18
                and prior_years == list(range(prior_years[0], current_years[0]))
                and current_years == list(range(current_years[0], current_years[-1] + 1))
                and all(is_complete_season_ledger(y, rows) for y, rows in prior_by_yr.items())):
            pw = sum(g["result"] == "W" for g in prior_school_games)
            pl = sum(g["result"] == "L" for g in prior_school_games)
            pt = prior_gp - pw - pl
            turnaround_delta = round(wins / gp - pw / prior_gp, 4)
            prior_record_str = f"{pw}-{pl}" + (f"-{pt}" if pt else "")

    return {
        "games": gp,
        "non_ff_games": non_ff_games,
        "wins": wins,
        "losses": losses,
        "ties": ties,
        "win_pct": win_pct,
        "seasons_count": len(seasons_by_yr),
        "winning_seasons": winning_seasons,
        "nine_win_seasons": nine_win_seasons,
        "ppg_for": ppg_for,
        "ppg_against": ppg_against,
        "avg_margin": avg_margin,
        "playoff_wins": playoff_w,
        "playoff_losses": playoff_l,
        "playoff_games": po_gp,
        "playoff_win_pct": po_win_pct,
        "one_score_wins": one_score_w,
        "one_score_losses": one_score_l,
        "one_score_games": os_gp,
        "one_score_pct": one_score_pct,
        "road_wins": road_w,
        "road_losses": road_l,
        "road_games": rd_gp,
        "road_win_pct": road_pct,
        "giant_killer_wins": giant_killer_w,
        "giant_killer_games": giant_killer_g,
        "ranked_wins": ranked_w,
        "ranked_games": ranked_g,
        "shutouts": shutouts,
        "hold_under_8": hold_under_8,
        "turnaround_delta": turnaround_delta,
        "prior_record": prior_record_str,
    }


def program_architect_badge(metrics):
    delta = metrics.get("turnaround_delta")
    if delta is None or delta < 0.18 or metrics["games"] < 16:
        return None
    return {
        "id": "program_architect",
        "label": "Program Architect",
        "detail": f"+{delta * 100:.1f}% win-rate jump vs prior era ({metrics.get('prior_record')})",
        "tone": "amber",
    }


def compute_ego_trip_badges(
    career_metrics: Dict[str, Any],
    state_p90_offense: float,
    state_p10_defense: float,
    verified_honors: Sequence[Dict[str, Any]],
) -> List[Dict[str, str]]:
    """
    Computes up to 3 positive, verified "ego-trip" badges strictly from verified game metrics.
    Returns priority-ordered list of `{id, label, detail, tone}` (length 0..3).
    """
    badges: List[Tuple[int, Dict[str, str]]] = []
    gp = career_metrics["games"]
    if gp < 8:
        return []

    has_state_title = any("state champion" in str(h.get("title", "")).lower() for h in verified_honors)

    # 1. Program Architect (Turnaround)
    architect = program_architect_badge(career_metrics)
    if architect:
        badges.append((100, architect))

    # 2. Postseason Wizard
    if has_state_title or (career_metrics["playoff_games"] >= 4 and career_metrics["playoff_win_pct"] >= 0.65):
        badges.append((
            95,
            {
                "id": "postseason_wizard",
                "label": "Postseason Wizard",
                "detail": f"{career_metrics['playoff_wins']}-{career_metrics['playoff_losses']} verified postseason record",
                "tone": "purple",
            },
        ))

    # 3. Giant Killer
    if career_metrics["giant_killer_wins"] >= 2:
        badges.append((
            90,
            {
                "id": "giant_killer",
                "label": "Giant Killer",
                "detail": f"{career_metrics['giant_killer_wins']} verified punch-up wins vs larger-enrollment divisions",
                "tone": "rose",
            },
        ))

    # 4. Elite Program Builder
    if gp >= 18 and career_metrics["win_pct"] >= 0.75:
        badges.append((
            88,
            {
                "id": "elite_builder",
                "label": "Elite Program Builder",
                "detail": f"{career_metrics['wins']}-{career_metrics['losses']} ({career_metrics['win_pct'] * 100:.1f}%) across {career_metrics['seasons_count']} verified seasons",
                "tone": "emerald",
            },
        ))
    elif gp >= 9 and career_metrics["win_pct"] >= 0.80:
        badges.append((
            82,
            {
                "id": "elite_campaign",
                "label": "Elite Campaign",
                "detail": f"{career_metrics['wins']}-{career_metrics['losses']} ({career_metrics['win_pct'] * 100:.1f}%) verified record",
                "tone": "emerald",
            },
        ))

    # 5. Clutch Closer (1-score games)
    if career_metrics["one_score_games"] >= 3 and career_metrics["one_score_pct"] >= 0.70:
        badges.append((
            84,
            {
                "id": "clutch_closer",
                "label": "Clutch Closer",
                "detail": f"{career_metrics['one_score_wins']}-{career_metrics['one_score_losses']} in verified 1-score games (<=8 pts)",
                "tone": "cyan",
            },
        ))

    # 6. Offensive Juggernaut
    if career_metrics["non_ff_games"] >= 8 and career_metrics["ppg_for"] >= max(34.0, state_p90_offense):
        badges.append((
            80,
            {
                "id": "offensive_juggernaut",
                "label": "Offensive Juggernaut",
                "detail": f"{career_metrics['ppg_for']:.1f} PPG scored (Top 10% statewide)",
                "tone": "amber",
            },
        ))

    # 7. Iron Curtain Defense
    if career_metrics["non_ff_games"] >= 8 and (
        career_metrics["ppg_against"] <= min(14.0, state_p10_defense)
        or (career_metrics["shutouts"] >= 3)
    ):
        badges.append((
            78,
            {
                "id": "iron_curtain",
                "label": "Iron Curtain",
                "detail": f"{career_metrics['ppg_against']:.1f} PPG allowed · {career_metrics['shutouts']} verified shutouts",
                "tone": "sky",
            },
        ))

    # 8. Ranked Hunter
    if career_metrics["ranked_wins"] >= 2:
        badges.append((
            76,
            {
                "id": "ranked_hunter",
                "label": "Ranked Hunter",
                "detail": f"{career_metrics['ranked_wins']} verified wins vs Top-25 state opponents",
                "tone": "amber",
            },
        ))

    # 9. Road Warrior
    if career_metrics["road_games"] >= 4 and career_metrics["road_win_pct"] >= 0.75:
        badges.append((
            72,
            {
                "id": "road_warrior",
                "label": "Road Warrior",
                "detail": f"{career_metrics['road_wins']}-{career_metrics['road_losses']} verified road/neutral record",
                "tone": "cyan",
            },
        ))

    # 10. Dominance Engine
    if career_metrics["non_ff_games"] >= 8 and career_metrics["avg_margin"] >= 18.0:
        badges.append((
            70,
            {
                "id": "dominance_engine",
                "label": "Dominance Engine",
                "detail": f"+{career_metrics['avg_margin']:.1f} PPG verified average scoring margin",
                "tone": "emerald",
            },
        ))

    badges.sort(key=lambda x: -x[0])
    return [b for _, b in badges[:3]]


def extract_metric_value(metrics: Dict[str, Any], cat_id: str) -> Tuple[Optional[float], int]:
    """Returns `(metric_value, qualifying_sample_count)` for a leaderboard category."""
    if cat_id == "win_pct":
        return metrics["win_pct"], metrics["games"]
    if cat_id == "turnaround":
        return metrics.get("turnaround_delta"), metrics["games"]
    if cat_id == "postseason":
        return metrics["playoff_win_pct"], metrics["playoff_games"]
    if cat_id == "giant_killer":
        return float(metrics["giant_killer_wins"]), metrics["giant_killer_wins"]
    if cat_id == "offense_ppg":
        return metrics["ppg_for"], metrics["non_ff_games"]
    if cat_id == "defense_ppg":
        return metrics["ppg_against"], metrics["non_ff_games"]
    if cat_id == "margin":
        return metrics["avg_margin"], metrics["non_ff_games"]
    if cat_id == "clutch":
        return metrics["one_score_pct"], metrics["one_score_games"]
    if cat_id == "road_warrior":
        return metrics["road_win_pct"], metrics["road_games"]
    if cat_id == "ranked_wins":
        return float(metrics["ranked_wins"]), metrics["ranked_wins"]
    if cat_id == "shutouts":
        return float(metrics["shutouts"]), metrics["shutouts"]
    if cat_id == "total_wins":
        return float(metrics["wins"]), metrics["wins"]
    return None, 0


def build_positive_leaderboard(
    verified_coaches: Sequence[Dict[str, Any]],
    cat_spec: Dict[str, Any],
    window_key: str,
    max_rows: int = 50,
    *, national: bool = False,
) -> List[Dict[str, Any]]:
    """
    Strictly enforces NO BOTTOM RANKING:
      - Only verified coaches meeting the minimum game/sample floor AND positive value gate.
      - Caps rows at `min(max_rows, max(3, floor(len(eligible) * 0.5)))` when eligible >= 6
        (or all strictly positive elite qualifiers when eligible < 6) so a losing/bottom-half
        coach can NEVER appear on a leaderboard.
    """
    cat_id = cat_spec["id"]
    is_single_yr = window_key == "2026"
    min_floor = cat_spec["min_games_1yr"] if is_single_yr else cat_spec["min_games_multi"]
    higher = bool(cat_spec["higher_is_better"])
    min_val = cat_spec.get("min_value")
    max_val = cat_spec.get("max_value")

    eligible: List[Tuple[float, float, int, Dict[str, Any], Dict[str, Any]]] = []
    ordered_windows = [key for key, _ in ROLLING_WINDOWS]
    for c in verified_coaches:
        if c.get("verification_status") != "verified":
            continue
        c_wins = c.get("windows") or {}
        candidates = reversed(ordered_windows[:ordered_windows.index(window_key) + 1])
        w_metrics = next((c_wins[key] for key in candidates if key in c_wins), None)
        if not w_metrics:
            continue
        if window_key not in ("career", "2026") and w_metrics["seasons_count"] < 2:
            continue
        val, sample = extract_metric_value(w_metrics, cat_id)
        if val is None or sample < min_floor:
            continue
        if min_val is not None and val < min_val:
            continue
        if max_val is not None and val > max_val:
            continue
        # Tie-breakers: secondary wins count & games played
        tie_w = float(w_metrics.get("playoff_wins" if cat_id == "postseason" else "wins", 0))
        tie_g = int(w_metrics.get("games", 0))
        eligible.append((float(val), tie_w, tie_g, c, w_metrics))

    if not eligible:
        return []

    if higher:
        eligible.sort(key=lambda x: (-x[0], -x[1], -x[2], x[3]["name"], x[3]["coach_id"]))
    else:
        eligible.sort(key=lambda x: (x[0], -x[1], -x[2], x[3]["name"], x[3]["coach_id"]))

    # Strict No-Bottom-Ranking Cap: never expose bottom 50% of eligible pool on ratio boards
    if cat_id in ("win_pct", "offense_ppg", "defense_ppg", "margin", "clutch", "road_warrior") and len(eligible) >= 8:
        cap = min(max_rows, max(5, len(eligible) // 2))
    else:
        cap = min(max_rows, len(eligible))

    out: List[Dict[str, Any]] = []
    state_totals = Counter(c["state"] for _, _, _, c, _ in eligible) if national else {}
    state_positions = Counter()
    for idx, (val, _, _, c, wm) in enumerate(eligible if national else eligible[:cap], start=1):
        dual_rank = {}
        if national:
            state_positions[c["state"]] += 1
            dual_rank = {"state_rank": state_positions[c["state"]], "state_total": state_totals[c["state"]],
                         "national_rank": idx, "national_total": len(eligible)}
            placement = c.get("ranks", {}).get(window_key, {}).get(cat_id)
            if placement:
                placement.update(dual_rank)
            if idx > cap:
                continue
        out.append({
            **dual_rank,
            "total": len(eligible),
            "rank": idx,
            "coach_id": c["coach_id"],
            "name": c["name"],
            "team_id": c["current_team_id"],
            "team_name": c["current_team_name"],
            "state": c["state"],
            "division": c["current_division"],
            "value": round(val, 4),
            "wins": wm["wins"],
            "losses": wm["losses"],
            "games": wm["games"],
            "seasons": wm["seasons_count"],
            "badges": [b["label"] for b in (c.get("badges") or [])[:2]],
        })
    return out


def load_csv_master_scores(
    state_code: str,
    csv_path: Path,
    state_teams: Sequence[Dict[str, Any]],
) -> Dict[str, Dict[int, List[Dict[str, Any]]]]:
    """
    Loads verified multi-year CSV score ledgers (`ohsaa_master_scores_2023_2026.csv`,
    `ihsaa_master_scores_2022_2026.csv`, `wiaa_master_scores_2024_2026.csv`) strictly keyed by team_id.
    Skips 2026 (which comes from the live bundle with full metadata).
    """
    if not csv_path.exists():
        return {}

    by_exact: Dict[str, Dict[str, Any]] = {}
    by_base: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for t in state_teams:
        by_exact[t["name"].lower()] = t
        by_base[t["base_name"]].append(t)

    def resolve_t(raw_name: str) -> Optional[Dict[str, Any]]:
        clean = str(raw_name or "").strip()
        if not clean:
            return None
        hit = by_exact.get(clean.lower())
        if hit:
            return hit
        b = normalize_school_base_name(clean)
        cands = by_base.get(b) or []
        return cands[0] if len(cands) == 1 else None

    out: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    seen_pairs: Dict[Tuple[str, int, str, str, int, int], bool] = {}

    with open(csv_path, "r", encoding="utf-8", errors="ignore") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                yr = int(str(row.get("season") or "").strip())
            except Exception:
                continue
            if yr >= 2026 or yr < 2015:
                continue
            w_name = str(row.get("winner") or "").strip()
            l_name = str(row.get("loser") or "").strip()
            try:
                ws = int(str(row.get("winner_score") or "").strip())
                ls = int(str(row.get("loser_score") or "").strip())
            except Exception:
                continue
            if ws < 0 or ls < 0 or ws > 140 or ls > 140:
                continue
            date_s = str(row.get("date_iso") or row.get("date") or "").strip()
            rnd = str(row.get("round") or "").lower()
            is_po = any(
                kw in rnd
                for kw in ("playoff", "region", "section", "district", "quarter", "semi", "final", "champ", "round", "level", "state")
            )

            wt = resolve_t(w_name)
            lt = resolve_t(l_name)
            w_div_rk = parse_division_enrollment_rank(wt["division"]) if wt else None
            l_div_rk = parse_division_enrollment_rank(lt["division"]) if lt else None

            if wt:
                k = (wt["team_id"], yr, date_s, l_name.lower(), ws, ls)
                if k not in seen_pairs:
                    seen_pairs[k] = True
                    out[wt["team_id"]][yr].append({
                        "season": yr,
                        "date": date_s,
                        "opp_name": l_name,
                        "opp_id": lt["team_id"] if lt else "",
                        "result": "W",
                        "team_score": ws,
                        "opp_score": ls,
                        "is_playoff": is_po,
                        "is_forfeit": is_forfeit_score(ws, ls),
                        "location": "U",
                        "is_giant_killer_opp": bool(
                            w_div_rk is not None and l_div_rk is not None and l_div_rk < w_div_rk
                        ),
                        "is_top25_opp": bool(lt and lt.get("state_rank", 999) <= 25),
                    })
            if lt:
                k = (lt["team_id"], yr, date_s, w_name.lower(), ls, ws)
                if k not in seen_pairs:
                    seen_pairs[k] = True
                    out[lt["team_id"]][yr].append({
                        "season": yr,
                        "date": date_s,
                        "opp_name": w_name,
                        "opp_id": wt["team_id"] if wt else "",
                        "result": "L",
                        "team_score": ls,
                        "opp_score": ws,
                        "is_playoff": is_po,
                        "is_forfeit": is_forfeit_score(ls, ws),
                        "location": "U",
                        "is_giant_killer_opp": bool(
                            w_div_rk is not None and l_div_rk is not None and w_div_rk < l_div_rk
                        ),
                        "is_top25_opp": bool(wt and wt.get("state_rank", 999) <= 25),
                    })

    return out


def extract_2026_bracket_honors(state_code: str, state_teams: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    """
    Computes 100% verified 2026 postseason honors (State Champion, Runner-Up, Regional/District Champion,
    Undefeated Regular Season) directly from official verified bracket files and schedules.
    """
    st_u = state_code.upper()
    honors_by_tid: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    by_name: Dict[str, str] = {}
    for t in state_teams:
        by_name[t["name"].lower()] = t["team_id"]
        by_name[t["base_name"]] = t["team_id"]

    # 1. Undefeated regular season (9+ regular-season games, 0 regular-season losses)
    for t in state_teams:
        sched = t.get("schedule_2026") or []
        reg_games = [g for g in sched if not g.get("is_playoff")]
        reg_w = sum(1 for g in reg_games if g.get("result") == "W")
        reg_l = sum(1 for g in reg_games if g.get("result") == "L")
        if reg_w >= 8 and reg_l == 0:
            honors_by_tid[t["team_id"]].append({
                "year": 2026,
                "title": f"2026 Undefeated Regular Season ({reg_w}-0)",
                "category": "season_milestone",
            })

        # Deep playoff run from verified playoff wins (strictly factual, no state-round guesswork)
        po_w = sum(1 for g in sched if g.get("is_playoff") and g.get("result") == "W")
        po_l = sum(1 for g in sched if g.get("is_playoff") and g.get("result") == "L")
        if po_w >= 4 and po_l == 0:
            honors_by_tid[t["team_id"]].append({
                "year": 2026,
                "title": f"2026 {t['division']} Unbeaten Postseason ({po_w}-0)",
                "category": "playoff",
            })
        elif po_w >= 3:
            honors_by_tid[t["team_id"]].append({
                "year": 2026,
                "title": f"2026 {t['division']} Deep Postseason Run ({po_w}-{po_l} Playoffs)",
                "category": "playoff",
            })
        elif po_w == 2:
            honors_by_tid[t["team_id"]].append({
                "year": 2026,
                "title": f"2026 {t['division']} Multi-Round Playoff Winner ({po_w}-{po_l})",
                "category": "playoff",
            })

    return honors_by_tid


def reconcile_archive_games(games, archive, complete):
    """Prefer an archive on matching fixtures. Preserve playoffs absent from regular-season-only tables."""
    def same_opponent(a, b):
        if a.get("opp_id") and b.get("opp_id"):
            return a["opp_id"] == b["opp_id"]
        return normalize_school_base_name(a.get("opp_name", "")) == normalize_school_base_name(b.get("opp_name", ""))

    output = [] if complete else list(games)
    used = set()
    for row in archive:
        candidates = [i for i, game in enumerate(games)
                      if i not in used and game.get("date") == row["date"] and same_opponent(row, game)]
        if len(candidates) == 1:
            i, = candidates
            used.add(i)
            # Score metadata belongs to the selected score source; verified
            # site and playoff metadata can be shared by the same fixture.
            selected = {**games[i], **row}
            if row["location"] == "U":
                selected["location"] = games[i].get("location", "U")
            if not complete:
                output[i] = selected
                continue
        elif complete:
            selected = row.copy()
        else:
            continue
        output.append(selected)
    return output


def build_all_states_coach_index(states: Sequence[str] = ALL_STATES) -> Dict[str, Any]:
    source_priority = json.loads((ROOT_DIR / "scripts" / "football_sources.json").read_text(encoding="utf-8"))
    season_sources = json.loads((ROOT_DIR / "registry" / "coach_season_sources.json").read_text(encoding="utf-8"))
    review = json.loads((ROOT_DIR / "scripts/coaches/research/local_baseline_needs_review.json").read_text())
    disputed_years = {row["team_id"]: row.get("disputed_years", []) for row in review["needs_review"]}
    # Load cached MaxPreps 2026 staff + multi-year history + historical MI scores + honors
    staff_cache: Dict[str, Dict[str, Any]] = {}
    if STAFF_CACHE_PATH.exists():
        staff_cache = json.loads(STAFF_CACHE_PATH.read_text(encoding="utf-8"))

    history_cache: Dict[str, Dict[str, Any]] = {}
    if HISTORY_CACHE_PATH.exists():
        history_cache = json.loads(HISTORY_CACHE_PATH.read_text(encoding="utf-8"))

    hist_scores_cache: Dict[str, Dict[str, Dict[str, List[Dict[str, Any]]]]] = {}
    if HIST_SCORES_CACHE_PATH.exists():
        hist_scores_cache = json.loads(HIST_SCORES_CACHE_PATH.read_text(encoding="utf-8"))

    mi_dw_scores: Dict[str, Any] = {}
    if MI_DW_SCORES_PATH.exists():
        mi_dw_scores = json.loads(MI_DW_SCORES_PATH.read_text(encoding="utf-8"))

    mhsfca_honors: List[Dict[str, Any]] = []
    if HONORS_MHSFCA_PATH.exists():
        try:
            mhsfca_honors = json.loads(HONORS_MHSFCA_PATH.read_text(encoding="utf-8")).get("honors") or []
        except Exception:
            pass

    override_honors: List[Dict[str, Any]] = []
    if HONORS_OVERRIDES_PATH.exists():
        try:
            override_honors = json.loads(HONORS_OVERRIDES_PATH.read_text(encoding="utf-8")).get("honors") or []
        except Exception:
            pass

    cross_school_links_by_cid: Dict[str, List[Dict[str, Any]]] = {}
    if CROSS_SCHOOL_LINKS_PATH.exists():
        try:
            for lnk in (json.loads(CROSS_SCHOOL_LINKS_PATH.read_text(encoding="utf-8")).get("links") or []):
                cid_k = str(lnk.get("coach_id") or "").strip()
                if cid_k and isinstance(lnk.get("past_stints"), list):
                    cross_school_links_by_cid[cid_k] = lnk["past_stints"]
        except Exception:
            pass

    # Group MaxPreps staff records by state
    staff_by_state: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    staff_by_sid: Dict[str, Dict[str, Any]] = {}
    for sid, rec in staff_cache.items():
        if not isinstance(rec, dict) or rec.get("missing"):
            continue
        st_c = str(rec.get("state") or "").upper()
        if st_c:
            staff_by_state[st_c].append(rec)
            staff_by_sid[sid.lower()] = rec

    if not staff_by_state or any(not staff_by_state.get(st) for st in states):
        raise ValueError("Missing 2026 staff cache for requested states")
    if "MI" in states and not mi_dw_scores.get("by_slug"):
        raise ValueError("Missing Michigan Football historical scores")
    state_payloads = {}
    COACHES_OUT_DIR.mkdir(parents=True, exist_ok=True)
    team_coach_cards: Dict[str, Dict[str, Any]] = {}
    all_national_coaches: List[Dict[str, Any]] = []
    global_search_entries: List[Dict[str, Any]] = []
    state_summaries: Dict[str, Dict[str, Any]] = {}

    csv_master_map = {
        "OH": PUBLIC_DATA_DIR / "ohsaa_master_scores_2023_2026.csv",
        "IN": PUBLIC_DATA_DIR / "ihsaa_master_scores_2022_2026.csv",
        "WI": PUBLIC_DATA_DIR / "wiaa_master_scores_2024_2026.csv",
    }

    for st in [s.upper() for s in states]:
        st_l = st.lower()
        bundle_path = (
            PUBLIC_DATA_DIR / "stratagem_live_bundle.json"
            if st == "MI"
            else PUBLIC_DATA_DIR / f"stratagem_live_bundle_{st_l}.json"
        )
        if not bundle_path.exists():
            raise FileNotFoundError(bundle_path)

        bundle = json.loads(bundle_path.read_text(encoding="utf-8"))
        rows = bundle.get("rankings") or bundle.get("teams") or []

        state_teams: List[Dict[str, Any]] = []
        team_by_id: Dict[str, Dict[str, Any]] = {}
        team_by_mp_id: Dict[str, Dict[str, Any]] = {}
        by_base: Dict[str, List[Dict[str, Any]]] = defaultdict(list)

        for r in rows:
            if not isinstance(r, dict):
                continue
            tid = str(r.get("team_id") or r.get("id") or "").strip()
            name = str(r.get("team") or r.get("name") or "").strip()
            if not tid or not name:
                continue
            mp_id = str((r.get("source_ids") or {}).get("maxpreps") or "").strip().lower()
            t_obj = {
                "team_id": tid,
                "name": name,
                "base_name": normalize_school_base_name(name),
                "city": str(r.get("city") or "").strip(),
                "city_norm": normalize_city(str(r.get("city") or "")),
                "division": str(r.get("division") or "").strip(),
                "div_enroll_rank": parse_division_enrollment_rank(str(r.get("division") or "")),
                "state_rank": int(r.get("rank") or 999),
                "maxpreps_id": mp_id,
                "schedule_2026": r.get("schedule_2026") or [],
            }
            state_teams.append(t_obj)
            team_by_id[tid] = t_obj
            if mp_id:
                team_by_mp_id[mp_id] = t_obj
            by_base[t_obj["base_name"]].append(t_obj)

        # Match MaxPreps staff records strictly to state_teams
        staff_for_team: Dict[str, Dict[str, Any]] = {}
        for rec in staff_by_state.get(st, []):
            sid = str(rec.get("school_id") or "").lower()
            matched = team_by_mp_id.get(sid) if sid else None
            if not matched and rec.get("school_name"):
                matched, _ = match_candidate_to_state_team(
                    rec["school_name"], rec.get("city", ""), state_teams, by_base
                )
            # Fallback for states whose canonical team names include mascot (e.g., OH: "Pickerington North Panthers")
            if not matched and rec.get("school_name") and rec.get("mascot"):
                full_with_mascot = f"{rec['school_name']} {rec['mascot']}".strip()
                matched, _ = match_candidate_to_state_team(
                    full_with_mascot, rec.get("city", ""), state_teams, by_base
                )
            if not matched and rec.get("school_name") and rec.get("city"):
                city_and_name = f"{rec['city']} {rec['school_name']}".strip()
                matched, _ = match_candidate_to_state_team(
                    city_and_name, rec.get("city", ""), state_teams, by_base
                )
            if matched and matched["maxpreps_id"] and matched["maxpreps_id"] != sid:
                continue
            if matched and matched["team_id"] not in staff_for_team:
                staff_for_team[matched["team_id"]] = rec
                if sid and not matched["maxpreps_id"]:
                    matched["maxpreps_id"] = sid
                    team_by_mp_id[sid] = matched

        # Load multi-year game scores for state:
        #  - MI: Authoritative michigan-football.com (2014-2025, enriched with MaxPreps H/A location when scores match),
        #        falling back to MaxPreps harvested schedules only for schools absent from michigan-football.com.
        #  - OH / IN / WI: Official verified CSV master ledgers.
        hist_games_by_tid: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        if st in csv_master_map:
            csv_scores = load_csv_master_scores(st, csv_master_map[st], state_teams)
            for tid, by_yr in csv_scores.items():
                for yr, gl in by_yr.items():
                    hist_games_by_tid[tid][yr].extend({**g, "source": csv_master_map[st].name} for g in gl)

        st_mp_hist = hist_scores_cache.get(st) or {}
        mp_games_by_tid_yr: Dict[str, Dict[int, List[Dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
        for sid, by_yr in st_mp_hist.items():
            t_obj = team_by_mp_id.get(sid.lower())
            if not t_obj:
                continue
            my_div_rk = t_obj.get("div_enroll_rank")
            for yr_s, gl in by_yr.items():
                yr = int(yr_s)
                if yr >= 2026:
                    continue
                for g in gl:
                    opp_t = team_by_mp_id.get(str(g.get("opp_school_id") or "").lower())
                    opp_div_rk = opp_t.get("div_enroll_rank") if opp_t else None
                    mp_games_by_tid_yr[t_obj["team_id"]][yr].append({
                        **g,
                        "source": "maxpreps.com",
                        "opp_id": opp_t["team_id"] if opp_t else "",
                        "is_giant_killer_opp": bool(
                            my_div_rk is not None and opp_div_rk is not None and opp_div_rk < my_div_rk
                        ),
                        "is_top25_opp": bool(opp_t and opp_t.get("state_rank", 999) <= 25),
                    })

        if st == "MI" and mi_dw_scores:
            tid_to_slug = mi_dw_scores.get("team_id_to_slug") or {}
            by_slug = mi_dw_scores.get("by_slug") or {}
            mi_by_exact = {t["name"].lower(): t for t in state_teams}

            def resolve_mi_opp(opp_name: str) -> Optional[Dict[str, Any]]:
                low = str(opp_name or "").strip().lower()
                if low in mi_by_exact:
                    return mi_by_exact[low]
                b = normalize_school_base_name(opp_name)
                cands = by_base.get(b) or []
                return cands[0] if len(cands) == 1 else None

            dw_loaded_tids: Set[str] = set()
            for t_obj in state_teams:
                tid = t_obj["team_id"]
                slug = tid_to_slug.get(tid)
                dw_by_yr = by_slug.get(slug) if slug else None
                if not dw_by_yr:
                    continue
                dw_loaded_tids.add(tid)
                my_div_rk = t_obj.get("div_enroll_rank")
                for yr_s, gl in dw_by_yr.items():
                    yr = int(yr_s)
                    if yr >= 2026 or yr < 2014:
                        continue
                    # Build lookup of MaxPreps H/A locations strictly by exact (team_score, opp_score, result) match
                    mp_loc_map: Dict[Tuple[str, int, int, str], str] = {}
                    for mg in mp_games_by_tid_yr[tid].get(yr, []):
                        if mg.get("location") in ("H", "A", "N"):
                            mp_loc_map[(mg["date"], int(mg["team_score"]), int(mg["opp_score"]), str(mg["result"]))] = mg["location"]
                    for g in gl:
                        opp_t = resolve_mi_opp(str(g.get("opp_name") or ""))
                        opp_div_rk = opp_t.get("div_enroll_rank") if opp_t else None
                        ts_i, os_i = int(g["team_score"]), int(g["opp_score"])
                        res_c = str(g["result"])
                        loc = mp_loc_map.get((g["date"], ts_i, os_i, res_c), "U")
                        hist_games_by_tid[tid][yr].append({
                            **g,
                            "source": "michigan-football.com",
                            "opp_id": opp_t["team_id"] if opp_t else "",
                            "location": loc,
                            "is_giant_killer_opp": bool(
                                my_div_rk is not None and opp_div_rk is not None and opp_div_rk < my_div_rk
                            ),
                            "is_top25_opp": bool(opp_t and opp_t.get("state_rank", 999) <= 25),
                        })

            # Fallback to MaxPreps historical scores only for MI teams absent from michigan-football.com
            for tid, by_yr in mp_games_by_tid_yr.items():
                if tid in dw_loaded_tids:
                    continue
                for yr, gl in by_yr.items():
                    hist_games_by_tid[tid][yr].extend(gl)
        else:
            for tid, by_yr in mp_games_by_tid_yr.items():
                for yr, gl in by_yr.items():
                    # Prefer a complete association ledger. Never concatenate two
                    # independent copies of the school's season.
                    if not is_complete_season_ledger(yr, hist_games_by_tid[tid][yr]):
                        hist_games_by_tid[tid][yr] = gl

        archive_tenures = defaultdict(dict)
        # Apply the regional historian first, then the association/local archive
        # selected by the team's source catalog. A matching primary score wins.
        history_root = HISTORY_CACHE_PATH.parent
        regional = sorted((history_root / "historian_seasons" / st).glob("*.json"))
        primary = sorted((history_root / "state_archives" / st).glob("*.json"))
        for path in regional + primary:
            year = int(path.stem)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            complete = set(evidence["complete"])
            for tid, meta in evidence["coaches"].items():
                previous = archive_tenures[tid].get(str(year), {})
                if (meta.get("archive_record") and previous.get("head_coach")
                        and normalize_coach_name(meta["head_coach"]) != normalize_coach_name(previous["head_coach"])):
                    meta = {**meta, "head_coach": None, "evidence_conflict": True}
                archive_tenures[tid][str(year)] = meta
            for tid in evidence.get("disputed", []):
                archive_tenures[tid][str(year)] = {"head_coach": None, "evidence_conflict": True}
            for tid, gl in evidence["scores"].items():
                if tid not in team_by_id:
                    continue
                allowed = ("hsfha.org",) if path in regional else source_priority[st][:-1]
                gl = [g for g in gl if g["source"] in allowed]
                if gl:
                    hist_games_by_tid[tid][year] = reconcile_archive_games(
                        hist_games_by_tid[tid][year], gl, tid in complete
                    )

        # Historical opponent tiers must come from the game season. A 2026
        # poll or classification cannot establish a 2019 quality win.
        history_by_tid = {
            t["team_id"]: (history_cache.get(t["maxpreps_id"]) or {}).get("seasons", {})
            for t in state_teams
        }
        final_polls = {
            int(path.stem): json.loads(path.read_text(encoding="utf-8"))["ranks"]
            for path in (history_root / "state_rankings" / st).glob("*.json")
        }
        for tid, by_year in hist_games_by_tid.items():
            for year, ledger in by_year.items():
                team_season = history_by_tid.get(tid, {}).get(str(year), {})
                tier = parse_division_enrollment_rank(team_season.get("division") or "")
                for game in ledger:
                    opponent = history_by_tid.get(game.get("opp_id"), {}).get(str(year), {})
                    opp_tier = parse_division_enrollment_rank(opponent.get("division") or "")
                    game["is_giant_killer_opp"] = tier is not None and opp_tier is not None and opp_tier < tier
                    opp_sid = (team_by_id.get(game.get("opp_id")) or {}).get("maxpreps_id")
                    rank = final_polls.get(year, {}).get(opp_sid) or opponent.get("state_rank")
                    game["is_top25_opp"] = rank is not None and 0 < rank <= 25

        # Add 2026 verified schedule games for ALL teams
        for t_obj in state_teams:
            tid = t_obj["team_id"]
            my_div_rk = t_obj.get("div_enroll_rank")
            for g in t_obj["schedule_2026"]:
                if not isinstance(g, dict):
                    continue
                ts = g.get("team_score")
                os_ = g.get("opp_score")
                if not isinstance(ts, (int, float)) or not isinstance(os_, (int, float)):
                    continue
                ts_i, os_i = int(ts), int(os_)
                res = str(g.get("result") or "").upper()
                if res not in ("W", "L", "T"):
                    continue
                opp_id = str(g.get("opp_id") or "").strip()
                opp_t = team_by_id.get(opp_id) if opp_id else None
                opp_div_rk = opp_t.get("div_enroll_rank") if opp_t else None
                host_id = str(g.get("host_team_id") or "").strip()
                if g.get("is_neutral"):
                    loc = "N"
                elif host_id:
                    loc = "H" if host_id == tid else "A"
                else:
                    loc = "U"
                hist_games_by_tid[tid][2026].append({
                    "season": 2026,
                    "source": bundle_path.name,
                    "date": str(g.get("date_iso") or g.get("date") or ""),
                    "opp_name": str(g.get("opponent") or ""),
                    "opp_id": opp_id,
                    "result": res,
                    "team_score": ts_i,
                    "opp_score": os_i,
                    "is_playoff": bool(g.get("is_playoff", False)),
                    "is_forfeit": is_forfeit_score(ts_i, os_i),
                    "location": loc,
                    "is_giant_killer_opp": bool(
                        my_div_rk is not None and opp_div_rk is not None and opp_div_rk < my_div_rk
                    ),
                    "is_top25_opp": bool(opp_t and opp_t.get("state_rank", 999) <= 25),
                })

        # Compute 2026 verified bracket honors
        auto_honors_by_tid = extract_2026_bracket_honors(st, state_teams)

        # Build verified coaching stints (STRICT ZERO-AUTOFILL RULE)
        # First pass: determine state scoring percentiles (p90 offense, p10 defense) among teams with >= 8 games
        off_vals: List[float] = []
        def_vals: List[float] = []
        for t_obj in state_teams:
            g26 = hist_games_by_tid[t_obj["team_id"]].get(2026) or []
            if len(g26) >= 8:
                m26 = compute_window_metrics(g26, 1)
                if m26["non_ff_games"] >= 8:
                    off_vals.append(m26["ppg_for"])
                    def_vals.append(m26["ppg_against"])
        off_vals.sort()
        def_vals.sort()
        state_p90_off = off_vals[int(len(off_vals) * 0.90)] if len(off_vals) >= 10 else 36.0
        state_p10_def = def_vals[int(len(def_vals) * 0.10)] if len(def_vals) >= 10 else 13.0

        state_coaches: List[Dict[str, Any]] = []
        for t_obj in state_teams:
            tid = t_obj["team_id"]
            st_rec = staff_for_team.get(tid)
            if not st_rec:
                continue

            # Extract verified 2026 Head Coach strictly from `head_coaches` (`position == 'Head Coach'`)
            if st_rec.get("year") and st_rec["year"] != "26-27":
                continue
            hcs = [hc for hc in (st_rec.get("head_coaches") or [])
                   if isinstance(hc, dict) and str(hc.get("position") or "").strip().lower() == "head coach"]
            hc_entry = hcs[0] if isinstance(hcs, list) and len(hcs) > 0 else None
            raw_coach_name = ""
            contact_id = ""
            is_strict_hc = False
            if isinstance(hc_entry, dict) and hc_entry.get("name"):
                raw_coach_name = str(hc_entry["name"])
                contact_id = str(hc_entry.get("contact_id") or "").strip().lower()
                is_strict_hc = True
            elif st_rec.get("context_coach_name"):
                # Single-source context coach name without explicit staff role -> name_only unless corroborated!
                raw_coach_name = str(st_rec["context_coach_name"])
                is_strict_hc = False

            # GATE 1: Hard Spam / Bot / URL / Email / Placeholder / Non-HC-Role Rejector
            if not is_valid_human_coach_name(raw_coach_name):
                continue
            coach_name = clean_coach_display_name(raw_coach_name)
            coach_norm = normalize_coach_name(coach_name)
            coach_id = make_canonical_coach_id(st, tid, coach_name)

            sid = t_obj.get("maxpreps_id") or str(st_rec.get("school_id") or "").lower()
            hist_rec = history_cache.get(sid) if sid else None
            hist_seasons = {}
            if hist_rec and hist_rec.get("school_id") == sid and hist_rec.get("state") == st:
                hist_seasons = dict(hist_rec.get("seasons") or {})

            for year, evidence in archive_tenures.get(tid, {}).items():
                if "archive_record" in evidence:
                    hist_seasons[year] = apply_documented_season(
                        hist_seasons.get(year, {}), evidence, hist_games_by_tid[tid].get(int(year), [])
                    )
                else:
                    hist_seasons[year] = {**hist_seasons.get(year, {}), **evidence}
            for year, evidence in season_sources.get(tid, {}).items():
                if (hist_seasons.get(year) or {}).get("evidence_conflict"):
                    continue
                hist_seasons[year] = apply_documented_season(
                    hist_seasons.get(year, {}), evidence, hist_games_by_tid[tid].get(int(year), [])
                )

            for year in disputed_years.get(tid, ()):
                hist_seasons[str(year)] = {"head_coach": None, "evidence_conflict": True}

            if len({normalize_coach_name(hc.get("name", "")) for hc in hcs}) > 1:
                is_strict_hc = False
            context_name = st_rec.get("context_coach_name")
            if (context_name and is_valid_human_coach_name(context_name)
                    and normalize_coach_name(context_name) != coach_norm
                    and normalize_coach_name((hist_seasons.get("2026") or {}).get("head_coach") or "") != coach_norm):
                is_strict_hc = False

            # GATE 3: 2026 Conflict Check — if /history/["2026"] explicitly names a DIFFERENT valid human HC
            # (e.g., Massillon Washington hist 2026 = "Nate Moore" vs hijacked staff "Emeli angel";
            # Olentangy Liberty hist 2026 = "John Sansbury" vs hijacked staff "LESLEY TALLEY"), demote staff to name_only!
            if (hist_seasons.get("2026", {}).get("evidence_conflict")
                    or not hist_seasons.get("2026", {}).get("summary_matches_ledger", True)):
                is_strict_hc = False
            if is_strict_hc and hist_seasons:
                hc_2026_hist = (hist_seasons.get("2026") or {}).get("head_coach")
                if hc_2026_hist and is_valid_human_coach_name(hc_2026_hist):
                    if normalize_coach_name(hc_2026_hist) != coach_norm:
                        is_strict_hc = False

            # GATE 2 & GATE 4: Complete-Season Game Floor + ZERO SWISS-CHEESE UNBROKEN CONTIGUOUS STINTS
            # 1. Start from 2026 ONLY if is_strict_hc AND 2026 has a complete game ledger (>= 8 games).
            g26 = hist_games_by_tid[tid].get(2026) or []
            rec26_hist = (hist_seasons.get("2026") or {}).get("overall_record") if hist_seasons else None
            verified_years: List[int] = []
            if is_strict_hc and is_complete_season_ledger(2026, g26, rec26_hist):
                verified_years.append(2026)

            # 2. Collect explicit historical years matching coach_norm in /history/
            explicit_hist_years: Set[int] = set()
            if verified_years and hist_seasons:
                for yr_s, s_meta in hist_seasons.items():
                    yr = int(yr_s)
                    if yr >= 2026 or yr < 2014:
                        continue
                    h_hc = s_meta.get("head_coach")
                    if h_hc and is_valid_human_coach_name(h_hc) and normalize_coach_name(h_hc) == coach_norm:
                        explicit_hist_years.add(yr)

                # Walk backwards only through explicitly attributed, complete seasons.
                cur_yr = 2025
                while cur_yr >= 2014:
                    if cur_yr not in explicit_hist_years:
                        break
                    yr_games = hist_games_by_tid[tid].get(cur_yr) or []
                    season = hist_seasons[str(cur_yr)]
                    if (not season.get("summary_matches_ledger", True)
                            or not is_complete_season_ledger(cur_yr, yr_games, season.get("overall_record"))):
                        break
                    verified_years.append(cur_yr)
                    cur_yr -= 1

            verified_years = sorted(set(verified_years))
            earliest_verified_yr = min(verified_years) if verified_years else 2026

            # Every baseline season must immediately precede the hire, have a
            # complete ledger, and explicitly identify a different head coach.
            baseline_stop = "pre_hire_history_missing"
            prior_era_games: List[Dict[str, Any]] = []
            if is_strict_hc and len(verified_years) >= 2 and hist_seasons:
                for pyr in range(earliest_verified_yr - 1, max(2013, earliest_verified_yr - 6), -1):
                    s_meta = hist_seasons.get(str(pyr)) or {}
                    h_hc = s_meta.get("head_coach")
                    yr_games = hist_games_by_tid[tid].get(pyr) or []
                    if not h_hc or not is_valid_human_coach_name(h_hc):
                        baseline_stop = "pre_hire_coach_unconfirmed"
                        break
                    if normalize_coach_name(h_hc) == coach_norm:
                        baseline_stop = "same_coach_in_preceding_season"
                        break
                    if (s_meta.get("archive_record") and
                            normalize_coach_name(h_hc).split()[-1:] == coach_norm.split()[-1:]):
                        baseline_stop = "pre_hire_coach_unconfirmed"
                        break
                    if (not s_meta.get("summary_matches_ledger", True)
                            or not is_complete_season_ledger(pyr, yr_games, s_meta.get("overall_record"))):
                        baseline_stop = "pre_hire_schedule_incomplete"
                        break
                    prior_era_games.extend(yr_games)
                else:
                    baseline_stop = "baseline_before_2014_archive_floor"

            # Gather strictly verified games for this coach's verified seasons ONLY
            current_school_games: List[Dict[str, Any]] = []
            coach_games: List[Dict[str, Any]] = []
            season_logs: List[Dict[str, Any]] = []
            for yr in sorted(verified_years, reverse=True):
                yg = hist_games_by_tid[tid].get(yr) or []
                if not yg:
                    continue
                current_school_games.extend(yg)
                coach_games.extend(yg)
                ym = compute_window_metrics(yg, None)
                season_logs.append({
                    "year": yr,
                    "score_sources": sorted({g["source"] for g in yg if g.get("source")}),
                    "score_urls": sorted({g["source_url"] for g in yg if g.get("source_url")}),
                    "coach_source": ((hist_seasons.get(str(yr)) or {}).get("source_url")
                                     or str(st_rec.get("team_link") or st_rec.get("canonical_url") or "").rstrip("/")
                                     + ("/staff/" if yr == 2026 else "/history/")),
                    "team_id": tid,
                    "team_name": t_obj["name"],
                    "division": t_obj["division"],
                    "wins": ym["wins"],
                    "losses": ym["losses"],
                    "ties": ym["ties"],
                    "ppg_for": ym["ppg_for"],
                    "ppg_against": ym["ppg_against"],
                    "playoff_wins": ym["playoff_wins"],
                    "playoff_losses": ym["playoff_losses"],
                })

            # Apply explicitly verified cross-school past coaching stops (NEVER auto-merged by name alone!)
            past_stints_out: List[Dict[str, Any]] = []
            all_verified_years = list(verified_years)
            if verified_years and coach_id in cross_school_links_by_cid:
                for p_stint in sorted(cross_school_links_by_cid[coach_id], key=lambda p: max(p.get("years") or [0]), reverse=True):
                    p_tid = str(p_stint.get("team_id") or "").strip()
                    p_years = sorted({int(y) for y in (p_stint.get("years") or []) if int(y) < earliest_verified_yr})
                    p_team = team_by_id.get(p_tid)
                    if not p_tid or not p_years or not p_team:
                        continue
                    p_games: List[Dict[str, Any]] = []
                    p_used_years: List[int] = []
                    for pyr in sorted(p_years, reverse=True):
                        pyg = hist_games_by_tid[p_tid].get(pyr) or []
                        record = (history_by_tid.get(p_tid, {}).get(str(pyr)) or {}).get("overall_record")
                        if pyr != min(all_verified_years) - 1 or not is_complete_season_ledger(pyr, pyg, record):
                            break
                        p_games.extend(pyg)
                        p_used_years.append(pyr)
                        all_verified_years.append(pyr)
                        pym = compute_window_metrics(pyg, None)
                        season_logs.append({
                            "year": pyr,
                            "team_id": p_tid,
                            "team_name": p_team["name"],
                            "division": p_team["division"],
                            "wins": pym["wins"],
                            "losses": pym["losses"],
                            "ties": pym["ties"],
                            "ppg_for": pym["ppg_for"],
                            "ppg_against": pym["ppg_against"],
                            "playoff_wins": pym["playoff_wins"],
                            "playoff_losses": pym["playoff_losses"],
                        })
                    if p_games:
                        coach_games.extend(p_games)
                        pm = compute_window_metrics(p_games, None)
                        past_stints_out.append({
                            "team_id": p_tid,
                            "team_name": p_team["name"],
                            "state": st,
                            "division": p_team["division"],
                            "verified_years": sorted(p_used_years),
                            "start_year": min(p_used_years),
                            "end_year": max(p_used_years),
                            "wins": pm["wins"],
                            "losses": pm["losses"],
                            "ties": pm["ties"],
                            "win_pct": pm["win_pct"],
                            "ppg_for": pm["ppg_for"],
                            "ppg_against": pm["ppg_against"],
                            "playoff_wins": pm["playoff_wins"],
                            "playoff_losses": pm["playoff_losses"],
                            "source_url": str(p_stint.get("source_url") or ""),
                        })

            season_logs.sort(key=lambda x: -int(x["year"]))
            all_verified_years = sorted(set(all_verified_years))

            # Confidence Gating
            is_verified = is_strict_hc and len(coach_games) >= 1
            verification_status = "verified" if is_verified else "name_only"

            # Attach verified honors (only when verified! Exact coach + school match required!)
            honors: List[Dict[str, Any]] = []
            if is_verified:
                # Match MHSFCA official State & Regional Coach of the Year for MI
                if st == "MI":
                    t_base = t_obj["base_name"]
                    t_low = t_obj["name"].lower()
                    for mh in mhsfca_honors:
                        mh_coach = normalize_coach_name(str(mh.get("coach_name") or ""))
                        mh_school = str(mh.get("school_name") or "").strip()
                        mh_sbase = normalize_school_base_name(mh_school)
                        if not mh_coach or mh_coach != coach_norm:
                            continue
                        school_ok = (
                            mh_sbase == t_base
                            or (mh_sbase and len(mh_sbase) >= 5 and (mh_sbase in t_low or t_base in mh_sbase))
                        )
                        if not school_ok:
                            continue
                        honors.append({
                            "year": int(mh["year"]),
                            "title": mh["title"],
                            "category": mh.get("category", "coach_of_year"),
                            "source_url": mh.get("source_url", ""),
                        })
                # Match curated override honors
                for oh in override_honors:
                    if oh.get("coach_id") == coach_id or (
                        oh.get("state") == st and normalize_coach_name(oh.get("coach_name", "")) == coach_norm
                        and oh.get("team_id") == tid
                    ):
                        honors.append({
                            "year": int(oh["year"]),
                            "title": oh["title"],
                            "category": oh.get("category", "official_honor"),
                            "source_url": oh.get("source_url", ""),
                        })
                honors.extend(auto_honors_by_tid.get(tid, []))
                # Sort honors: official COY first, then most recent year
                honors.sort(key=lambda h: (0 if h.get("category") == "coach_of_year" else 1, -int(h.get("year", 0))))

            # Compute rolling windows strictly from verified games
            # Compact windows storage: avoid duplicating identical metrics 6x for 1-season coaches!
            windows_full: Dict[str, Dict[str, Any]] = {}
            if is_verified:
                for w_key, w_yrs in ROLLING_WINDOWS:
                    wm = compute_window_metrics(
                        coach_games,
                        w_yrs,
                        as_of_year=2026,
                        prior_school_games=None,
                    )
                    if wm["games"] > 0:
                        if w_key != "2026" and prior_era_games:
                            stint_window = compute_window_metrics(current_school_games, w_yrs,
                                                                 prior_school_games=prior_era_games)
                            wm["turnaround_delta"] = stint_window["turnaround_delta"]
                            wm["prior_record"] = stint_window["prior_record"]
                        windows_full[w_key] = wm

            windows: Dict[str, Dict[str, Any]] = {}
            if windows_full:
                if "2026" in windows_full:
                    windows["2026"] = windows_full["2026"]
                if len(all_verified_years) >= 2:
                    prev_sig = (
                        windows_full["2026"]["games"],
                        windows_full["2026"]["wins"],
                        windows_full["2026"].get("turnaround_delta"),
                    ) if "2026" in windows_full else None
                    for w_key in ("2yr", "4yr", "5yr", "7yr"):
                        wm = windows_full.get(w_key)
                        if not wm:
                            continue
                        sig = (wm["games"], wm["wins"], wm.get("turnaround_delta"))
                        if sig != prev_sig:
                            windows[w_key] = wm
                            prev_sig = sig
                    if "career" in windows_full:
                        windows["career"] = windows_full["career"]
                elif "career" in windows_full and "2026" not in windows:
                    windows["career"] = windows_full["career"]

            career_m = windows_full.get("career") or compute_window_metrics([], None)
            cur_stint_m = compute_window_metrics(current_school_games, None) if is_verified else career_m
            badges = (
                compute_ego_trip_badges(career_m, state_p90_off, state_p10_def, honors)
                if is_verified
                else []
            )

            # Construct verified stints timeline (current stop + any explicitly verified past stops)
            stints = (
                [
                    {
                        "team_id": tid,
                        "team_name": t_obj["name"],
                        "state": st,
                        "division": t_obj["division"],
                        "verified_years": verified_years,
                        "start_year": min(verified_years) if verified_years else 2026,
                        "end_year": max(verified_years) if verified_years else 2026,
                        "wins": cur_stint_m["wins"],
                        "losses": cur_stint_m["losses"],
                        "ties": cur_stint_m["ties"],
                        "win_pct": cur_stint_m["win_pct"],
                        "ppg_for": cur_stint_m["ppg_for"],
                        "ppg_against": cur_stint_m["ppg_against"],
                        "playoff_wins": cur_stint_m["playoff_wins"],
                        "playoff_losses": cur_stint_m["playoff_losses"],
                    },
                    *past_stints_out,
                ]
                if is_verified
                else []
            )

            coach_record = {
                "coach_id": coach_id,
                "name": coach_name,
                "state": st,
                "current_team_id": tid,
                "current_team_name": t_obj["name"],
                "current_division": t_obj["division"],
                "verification_status": verification_status,
                "contact_id_verified": bool(contact_id),
                "verified_years": all_verified_years,
                "start_year": min(all_verified_years) if all_verified_years else None,
                "end_year": max(all_verified_years) if all_verified_years else None,
                "honors": honors[:6],
                "badges": badges,
                "stints": stints,
                "seasons": season_logs,
                "windows": windows,
                "ranks": {},  # Populated after state leaderboards are computed
            }
            if career_m.get("turnaround_delta") is not None:
                baseline_by_year = defaultdict(Counter)
                for game in prior_era_games:
                    baseline_by_year[game["season"]][game["result"]] += 1
                coach_record["turnaround_baseline"] = [
                    {"year": year, "head_coach": hist_seasons[str(year)]["head_coach"],
                     "wins": counts["W"], "losses": counts["L"], "ties": counts["T"]}
                    for year, counts in sorted(baseline_by_year.items())
                ]
            elif is_verified and len(verified_years) >= 2:
                if len({g["season"] for g in prior_era_games}) >= 2 and len(prior_era_games) < 18:
                    baseline_stop = "pre_hire_under_18_games"
                coach_record["turnaround_review_reason"] = baseline_stop
            state_coaches.append(coach_record)

        # Build state-level positive leaderboards for all 12 categories
        # For 1-year states (multi_year == 0), emit only "2026" (avoids 6x JSON bloat; UI maps career -> 2026 cleanly).
        state_has_multi = any(
            c["verification_status"] == "verified" and len(c["verified_years"]) >= 2
            for c in state_coaches
        )
        active_windows = ROLLING_WINDOWS if state_has_multi else [("2026", 1)]

        state_leaderboards: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
        coach_by_cid = {c["coach_id"]: c for c in state_coaches}

        for w_key, _ in active_windows:
            w_boards: Dict[str, List[Dict[str, Any]]] = {}
            for cat in LEADERBOARD_SPECS:
                board = build_positive_leaderboard(state_coaches, cat, w_key, max_rows=50)
                if board:
                    w_boards[cat["id"]] = board
                    # Stamp rank placement back onto each coach record for quick profile / teaser lookup
                    for row in board:
                        c_obj = coach_by_cid.get(row["coach_id"])
                        if c_obj is not None:
                            c_obj["ranks"].setdefault(w_key, {})[cat["id"]] = {
                                "rank": row["rank"],
                                "total": row["total"],
                                "value": row["value"],
                            }
            if w_boards:
                state_leaderboards[w_key] = w_boards

        # Populate lightweight Team Profile cards map (for TeamProfilePage + SEO prerender)
        for c in state_coaches:
            tid = c["current_team_id"]
            c_wins = c.get("windows") or {}
            cm = c_wins.get("career") or c_wins.get("2026") or {}
            # Summarize best verified rank tiers (for paywall teaser banner!)
            best_ranks = []
            career_ranks = (c.get("ranks") or {}).get("career") or (c.get("ranks") or {}).get("2026") or {}
            for cat_id, r_info in sorted(career_ranks.items(), key=lambda x: x[1]["rank"])[:4]:
                best_ranks.append({
                    "cat": cat_id,
                    "rank": r_info["rank"],
                    "tier": "Top 5" if r_info["rank"] <= 5 else "Top 10" if r_info["rank"] <= 10 else "Top 25" if r_info["rank"] <= 25 else "Top 50",
                })

            team_coach_cards[tid] = {
                "coach_id": c["coach_id"],
                "name": c["name"],
                "state": st,
                "status": c["verification_status"],
                "start_year": c["start_year"],
                "end_year": c["end_year"],
                "seasons_count": cm.get("seasons_count", 0),
                "wins": cm.get("wins", 0),
                "losses": cm.get("losses", 0),
                "honors": [h["title"] for h in (c.get("honors") or [])[:3]],
                "badges": c.get("badges") or [],
                "qualifying_boards": len(career_ranks),
                "best_tiers": best_ranks[:2],
            }

            if c["verification_status"] == "verified":
                all_national_coaches.append(c)
                global_search_entries.append({
                    "id": c["coach_id"],
                    "n": c["name"],
                    "t": c["current_team_name"],
                    "tid": tid,
                    "st": st,
                    "d": c["current_division"],
                    "w": cm.get("wins", 0),
                    "l": cm.get("losses", 0),
                    "s": cm.get("seasons_count", 1),
                    "b": [b["label"] for b in (c.get("badges") or [])[:2]],
                })

        verified_cnt = sum(1 for c in state_coaches if c["verification_status"] == "verified")
        multi_yr_cnt = sum(
            1 for c in state_coaches if c["verification_status"] == "verified" and len(c["verified_years"]) >= 2
        )
        state_payload = {
            "version": 1,
            "state": st,
            "updated_at": datetime.date.today().isoformat(),
            "verified_count": verified_cnt,
            "multi_year_count": multi_yr_cnt,
            "available_windows": list(state_leaderboards.keys()),
            "coaches": state_coaches,
            "leaderboards": state_leaderboards,
        }
        state_payloads[st] = state_payload
        coverage = coverage_counts(state_coaches)
        state_summaries[st] = {
            "architect_measured": coverage["measured"],
            "architect_ranked": coverage["ranked"],
            "verified": verified_cnt,
            "multi_year": multi_yr_cnt,
            "total_listed": len(state_coaches),
        }

    # National Top 50 leaderboards across all verified coaches
    national_leaderboards: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    for w_key, _ in ROLLING_WINDOWS:
        nw: Dict[str, List[Dict[str, Any]]] = {}
        for cat in LEADERBOARD_SPECS:
            board = build_positive_leaderboard(all_national_coaches, cat, w_key, max_rows=50, national=True)
            if board:
                nw[cat["id"]] = board
        if nw:
            national_leaderboards[w_key] = nw

    for st, payload in state_payloads.items():
        atomic_json(COACHES_OUT_DIR / f"coaches_{st.lower()}.json", payload)

    atomic_json(COACHES_OUT_DIR / "team_coach_cards.json", {
        "version": 1,
        "updated_at": datetime.date.today().isoformat(),
        "count": len(team_coach_cards),
        "teams": team_coach_cards,
    })
    atomic_json(COACHES_OUT_DIR / "national_summary.json", {
        "version": 1,
        "updated_at": datetime.date.today().isoformat(),
        "total_verified_coaches": len(all_national_coaches),
        "states": state_summaries,
        "leaderboards": national_leaderboards,
        "search_index": global_search_entries,
    })

    tot_verified = len(all_national_coaches)
    tot_multi = sum(v["multi_year"] for v in state_summaries.values())
    print(
        f"[Coach Index] Built Coach Index: {tot_verified} verified coaches "
        f"({tot_multi} multi-year verified) across {len(state_summaries)} states -> {COACHES_OUT_DIR}",
        flush=True,
    )
    return {
        "total_verified": tot_verified,
        "total_multi_year": tot_multi,
        "states": state_summaries,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Build 50-state zero-hallucination Coach Index datasets.")
    ap.add_argument("--states", help="Optional comma-separated states (default: all 50)")
    args = ap.parse_args()
    target = [s.strip().upper() for s in args.states.split(",") if s.strip()] if args.states else ALL_STATES
    build_all_states_coach_index(target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
