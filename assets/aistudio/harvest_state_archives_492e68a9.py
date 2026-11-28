"""Read state score archives into Coach Index caches. All team bundles are inputs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
import json
import re
import sys
import subprocess
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlencode, urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))
from bs4 import BeautifulSoup
from football_feed_http import Snapshots
from football_feed_parsers import score_value
from score_verification import matches_domain, valid_score
from scripts.coaches.harvest_multiyear_coaches import CACHE_DIR, atomic_json

ARCHIVE_DIR = CACHE_DIR / "state_archives"
ADAPTER_STATES = ("GA", "AL", "AZ", "MD", "PA", "WV", "WY", "TX", "IL")


def school_key(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold())


def georgia(cache, year, teams, audit):
    url = f"https://www.ghsa.net/{year}-ghsa-football-results"
    page = BeautifulSoup(cache.get(url, ttl=31536000), "lxml")
    if not page.title or f"{year} GHSA Football Results" not in page.title.get_text():
        raise ValueError(f"Wrong GHSA results page: {url}")
    school = ""
    for tr in page.select("tr.schedule-head, tr.schedule-detail"):
        if "schedule-head" in tr["class"]:
            school = tr.get_text(" ", strip=True)
            continue
        cells = [
            td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)
        ]
        if len(cells) != 5 or cells[3] not in ("W", "L", "T"):
            continue
        a, opponent, b, result, day = cells
        if not re.fullmatch(r"\d{1,2}/\d{1,2}", day):
            continue
        sa, sb = score_value(a), score_value(b)
        if valid_score(sa, sb) and result == game_result(sa, sb):
            yield {
                "names": [school, opponent],
                "scores": [sa, sb],
                "url": url,
                "date": datetime.strptime(f"{year}/{day}", "%Y/%m/%d")
                .date()
                .isoformat(),
            }


def game_result(a, b):
    return "W" if a > b else "L" if a < b else "T"


def alabama(cache, year, teams, audit):
    for team in teams:
        school = team["team"]
        url = "https://www.ahsfhs.org/Teams2/gamesbyyear.asp?" + urlencode(
            {"Team": school, "Year": year}
        )
        try:
            page = BeautifulSoup(cache.get(url, ttl=31536000), "lxml")
        except (OSError, ValueError) as exc:
            audit.append({"url": url, "reason": str(exc)})
            continue
        heading = " ".join(
            node.get_text(" ", strip=True) for node in page.select("h1,h2,h3,h4")
        )
        if school_key(school + " Football Team History") not in school_key(heading):
            continue
        record = coach = None
        games = {}
        for tr in page.select("tr"):
            cols = tr.find_all("td", recursive=False)
            if len(cols) == 11:
                link = cols[0].find("a", href=True)
                person = cols[-1].select_one('a[href*="coachestop1"]')
                if (
                    link
                    and person
                    and school_key(link.get_text(strip=True)) == school_key(school)
                ):
                    query = {
                        k.lower(): v
                        for k, v in parse_qs(urlsplit(link["href"]).query).items()
                    }
                    if query.get("year") == [str(year)]:
                        record = cols[1].get_text(strip=True)
                        coach = person.get_text(" ", strip=True)
            if len(cols) != 7:
                continue
            cells = [td.get_text(" ", strip=True) for td in cols]
            stamp = re.fullmatch(r"\w{3}\.,\s*(\w{3})\.?\s+(\d{1,2})", cells[0])
            if not stamp or cells[4] not in ("W", "L", "T"):
                continue
            s1, s2 = score_value(cells[2]), score_value(cells[3])
            if not valid_score(s1, s2) or game_result(s1, s2) != cells[4]:
                continue
            day = (
                datetime.strptime(f"{year} {stamp[1]} {stamp[2]}", "%Y %b %d")
                .date()
                .isoformat()
            )
            opponent_link = cols[1].find("a")
            opponent = (
                opponent_link.get_text(" ", strip=True)
                if opponent_link
                else re.sub(r"^(@|vs\.)\s*|\s*\*$", "", cells[1])
            )
            games[day, opponent] = {
                "names": [school, opponent],
                "scores": [s1, s2],
                "date": day,
                "url": url,
                "only_first": True,
                "playoff": bool(
                    re.search("playoff|championship game", cells[-1], re.I)
                ),
                "host": 1 if cells[1].startswith("@") else None,
            }
        if not record or not coach:
            continue
        counts = Counter(game_result(*g["scores"]) for g in games.values())
        expected = f"{counts['W']}-{counts['L']}" + (
            f"-{counts['T']}" if counts["T"] else ""
        )
        for game in games.values():
            yield {**game, "coach": coach, "complete": expected == record}


def arizona(cache, year, teams, audit):
    from audit_arizona_history import official_scores

    index = "https://azpreps365.com/archives/football"
    page = BeautifulSoup(cache.get(index, ttl=86400), "lxml")
    for option in page.select("option[value]"):
        title = option.get_text(" ", strip=True)
        if not re.fullmatch(rf"{year} Football .+ State Championships", title):
            continue
        doc_id = option["value"]
        if not doc_id.isdigit():
            continue
        url = f"{index}?id={doc_id}"
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
        pdf = cache.get_pdf(
            f"https://aiaonline.org/files/{doc_id}/{slug}.pdf", ttl=31536000
        )
        content = subprocess.run(
            ["pdftotext", "-raw", "-", "-"], input=pdf, capture_output=True, check=True
        ).stdout.decode()
        try:
            for _, day, teams_in_game in official_scores(content, year):
                yield {
                    "date": day,
                    "names": [r[1] for r in teams_in_game],
                    "scores": [r[2] for r in teams_in_game],
                    "playoff": True,
                    "url": url,
                }
        except ValueError as exc:
            audit.append({"url": url, "reason": str(exc)})
        return


def pennsylvania(cache, year, teams, audit):
    from pennsylvania_football import local_scoreboard, local_url

    # EasternPAFootball publishes statewide weekend score articles. Confirm
    # the dated headline and section before considering any reported fixture.
    first = date(year, 8, 15)
    friday = first + timedelta(days=(4 - first.weekday()) % 7)
    while friday <= date(year, 12, 20):
        for day in (friday, friday + timedelta(days=1)):
            url = local_url(day)
            try:
                page = cache.get(url, ttl=31536000)
                for names, scores in local_scoreboard(page, day):
                    yield {
                        "names": names,
                        "scores": scores,
                        "date": day.isoformat(),
                        "url": url,
                    }
            except HTTPError as exc:
                if exc.code not in (404, 410):
                    raise
            except ValueError as exc:
                audit.append({"url": url, "reason": str(exc)})
        friday += timedelta(days=7)


def local_archive(state, cache, year, audit):
    if state == "MD":
        from maryland_foundation import read_foundation

        for row in read_foundation(cache, year, audit):
            yield {**row, "host": 1 if row["has_host"] else None}
    elif state == "WV":
        from west_virginia_tailgate import read_tailgate

        yield from read_tailgate(cache, year, date(year + 1, 2, 28), audit)
    else:
        from build_football_history import wyoming_regional_archive

        yield from wyoming_regional_archive(cache, year, audit)


def harvest_summary_years(state, years, cache):
    from scripts.coaches.season_summary_archives import summary_seasons

    folder = ARCHIVE_DIR / state
    pending = [year for year in years if not (folder / f"{year}.json").exists()]
    if not pending:
        return
    sources = json.loads((ROOT / "scripts/football_sources.json").read_text())[state]
    bundle = ROOT / "public/data" / f"stratagem_live_bundle_{state.lower()}.json"
    teams = json.loads(bundle.read_text())["rankings"]
    for year, result in summary_seasons(state, pending, teams, cache, sources):
        atomic_json(folder / f"{year}.json", result)
        print(f"{state} {year}: {len(result['coaches'])} sourced season summaries")


def harvest_archive(state, year, cache):
    if state in ("TX", "IL"):
        return harvest_summary_years(state, [year], cache)
    target = ARCHIVE_DIR / state / f"{year}.json"
    if target.exists():
        return
    sources = json.loads(
        (ROOT / "scripts/football_sources.json").read_text(encoding="utf-8")
    )[state]
    teams = json.loads(
        (
            ROOT / "public/data" / f"stratagem_live_bundle_{state.lower()}.json"
        ).read_text(encoding="utf-8")
    )["rankings"]
    by_name = defaultdict(set)
    for t in teams:
        by_name[school_key(t["team"])].add(t["team_id"])
    audit = []
    adapters = {"GA": georgia, "AL": alabama, "AZ": arizona, "PA": pennsylvania}
    observations = (
        adapters[state](cache, year, teams, audit)
        if state in adapters
        else local_archive(state, cache, year, audit)
    )
    buckets = defaultdict(dict)
    conflicts = set()
    coaches = {}
    complete = set()
    for row in observations:
        if not row.get("scores") or not valid_score(*row["scores"]):
            continue
        source = next(
            (domain for domain in sources[:-1] if matches_domain(row["url"], domain)),
            None,
        )
        if not source:
            continue
        for i in (0,) if row.get("only_first") else (0, 1):
            ids = by_name[school_key(row["names"][i])]
            if len(ids) != 1:
                continue
            (tid,) = ids
            opp = row["names"][1 - i]
            opp_ids = by_name[school_key(opp)]
            a, b = row["scores"][i], row["scores"][1 - i]
            key = (row["date"], school_key(opp))
            entry = {
                "date": row["date"],
                "season": year,
                "opp_name": opp,
                "opp_id": next(iter(opp_ids)) if len(opp_ids) == 1 else "",
                "result": game_result(a, b),
                "team_score": a,
                "opp_score": b,
                "source_url": row["url"],
                "source": source,
                "location": (
                    "U" if row.get("host") is None else "H" if row["host"] == i else "A"
                ),
            }
            if "playoff" in row:
                entry["is_playoff"] = row["playoff"]
            prev = buckets[tid].get(key)
            if prev and (prev["team_score"], prev["opp_score"]) != (a, b):
                conflicts.add((tid, key))
            buckets[tid][key] = entry
            if row.get("coach"):
                coaches[tid] = {"head_coach": row["coach"], "source_url": row["url"]}
            if row.get("complete"):
                complete.add(tid)
    for tid, key in conflicts:
        buckets[tid].pop(key, None)
        complete.discard(tid)
    result = {
        "scores": {tid: list(rows.values()) for tid, rows in buckets.items()},
        "coaches": coaches,
        "complete": sorted(complete),
        "conflicts": len(conflicts),
        "audit": audit,
    }
    atomic_json(target, result)
    print(
        f"{state} {year}: {sum(map(len,buckets.values()))} team-game observations, {len(coaches)} coach records, {len(audit)} source events",
        flush=True,
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--states", default=",".join(ADAPTER_STATES))
    parser.add_argument("--years", default=",".join(map(str, range(2016, 2026))))
    args = parser.parse_args()
    cache = Snapshots(CACHE_DIR / "archive_pages")
    for state in args.states.upper().split(","):
        years = list(map(int, args.years.split(",")))
        if state in ("TX", "IL"):
            harvest_summary_years(state, years, cache)
        else:
            for year in years:
                harvest_archive(state, year, cache)


if __name__ == "__main__":
    main()
