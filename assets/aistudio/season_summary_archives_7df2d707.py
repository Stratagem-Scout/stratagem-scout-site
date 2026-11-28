"""School-season summaries. These sources report final records, not individual games."""

from collections import defaultdict
from string import ascii_lowercase
import re
from urllib.error import HTTPError

from bs4 import BeautifulSoup
from scripts.score_verification import matches_domain
from scripts.coaches.build_coach_index import is_valid_human_coach_name


def illinois(html):
    page = BeautifulSoup(html, "lxml")
    if not page.title or page.title.get_text(strip=True) != "IHSA Football Season Summaries":
        raise ValueError("Unexpected IHSA page")
    for heading in page.select("h3"):
        table = heading.find_next_sibling()
        if not table or table.name != "table":
            continue
        headers = [th.get_text(strip=True) for th in table.select("th")[:8]]
        if headers != ["Season", "Class", "Titles", "Place", "Won", "Lost", "Tied", "Coach"]:
            continue
        for tr in table.select("tr"):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all("td", recursive=False)]
            if len(cells) != 8 or not re.fullmatch(r"\d{4}-\d{2}", cells[0]):
                continue
            year = int(cells[0][:4])
            if int(cells[0][-2:]) != (year + 1) % 100:
                continue
            record = (cells[4], cells[5], cells[6] or "0")
            yield heading.get_text(" ", strip=True), year, cells[7], record


TX_SEASON = re.compile(
    r"(\d{4})\s+(?:[A-Z.*/ -]+\s+)?(\d+)\s*-\s*(\d+)\s*-\s*(\d+)"
    r"\s+\d+\s*-\s*\d+\s+(.+)"
)


def texas(html):
    page = BeautifulSoup(re.sub(r"[\r\n\t]+", " ", html), "lxml")
    if not (page.title and "texashighschoolfootballhistory" in page.title.get_text(strip=True).lower()
            and re.search(r"W-L-T\s+PTS-OPP\s+COACH", page.get_text(" ", strip=True), re.I)):
        raise ValueError("Unexpected PigskinPrep page")
    for box in page.select('div[id^="txtBox"]'):
        for block in box.select("div, p"):
            block.insert(0, "\n")
            block.append("\n")
        for br in box.select("br"):
            br.replace_with("\n")
        lines = [" ".join(line.replace("\u200b", "").split())
                 for line in box.get_text().splitlines()]
        lines = [line for line in lines if line]
        school = None
        for i, line in enumerate(lines):
            if re.fullmatch(r"YEAR\s+W-L-T\s+PTS-OPP\s+COACH", line, re.I):
                # The mascot is on the line between the school and column headings.
                heading = re.fullmatch(r'([A-Z][A-Z .&/()\'-]+)\s+[“"].+[”"]', lines[i-1]) if i else None
                school = heading[1].strip() if heading else (lines[i-2] if i >= 2 and lines[i-2].isupper()
                          and re.fullmatch(r'[“"].+[”"]', lines[i-1]) else None)
            elif school and re.match(r"^\d{4}\b", line):
                match = TX_SEASON.fullmatch(line)
                if match:
                    year, wins, losses, ties, coach = match.groups()
                    yield school, int(year), coach, (wins, losses, ties)
                else:
                    yield school, int(line[:4]), "", ()
            elif line.upper().startswith("YEAR") or "ALL-TIME" in line.upper() or re.search(r'[“"]', line):
                school = None


def summary_seasons(state, years, teams, cache, sources):
    parse = texas if state == "TX" else illinois
    # Retain locality qualifiers. Punctuation and spacing are significant here.
    directory = defaultdict(set)
    base_ids = defaultdict(set)
    for team in teams:
        name, city = team["team"], team.get("city", "")
        base = re.sub(r" \([^()]+\)$", "", name)
        base_ids[base.casefold()].add(team["team_id"])
        names = [name]
        if city:
            qualified_name = base if name.casefold().endswith(f" ({city})".casefold()) else name
            names.append(f"{city} {qualified_name}" if state == "TX" else f"{city} ({qualified_name})")
        for alias in names:
            directory[alias.casefold()].add(team["team_id"])
    observations = {year: defaultdict(set) for year in years}
    reviews = {year: [] for year in years}
    fetched = 0
    for letter in ascii_lowercase:
        url = (f"https://www.pigskinprep.com/Season%20Records%20{letter.upper()}.html"
               if state == "TX" else f"https://www.ihsa.org/data/fb/records/sum-{letter}.htm")
        if not any(matches_domain(url, domain) for domain in sources[:-1]):
            raise ValueError(f"Unregistered archive: {url}")
        try:
            html = cache.get(url, ttl=31536000)
        except HTTPError as exc:
            if exc.code not in (404, 410):
                raise
            continue
        fetched += 1
        for school, year, coach, record in parse(html):
            if year not in observations:
                continue
            ids = directory.get(school.casefold(), set())
            if len(ids) != 1 or len(base_ids.get(school.casefold(), ())) > 1:
                reviews[year].append({"school": school, "url": url, "reason": "school not uniquely mapped"})
                continue
            observations[year][next(iter(ids))].add((coach, record, url))
    if not fetched:
        raise ValueError(f"No {state} season summary pages available")
    for year, schools in observations.items():
        coaches = {}
        disputed = []
        for tid, rows in schools.items():
            values = {(coach, record) for coach, record, url in rows}
            coach, record, url = min(rows)
            if (len(values) != 1 or not is_valid_human_coach_name(coach)
                    or re.search(r"\b(and|or|until|then|with|interim)\b|[,/&;]|[^a-zA-Z .’'-]", coach, re.I)
                    or len(record) != 3 or not all(n.isdecimal() for n in record)):
                disputed.append(tid)
                reviews[year].append({"team_id": tid, "url": url, "reason": "missing or conflicting season evidence"})
                continue
            coaches[tid] = {"head_coach": coach, "source_url": url,
                            "archive_record": "-".join(record)}
        yield year, {"scores": {}, "complete": [], "coaches": coaches, "conflicts": len(disputed),
                     "disputed": sorted(disputed),
                     "audit": reviews[year]}
