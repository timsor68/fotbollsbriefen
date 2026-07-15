#!/usr/bin/env python3
"""
Hämtar kommande matcher, tabellplaceringar och en enkel, transparent
matchprognos per liga för Fotbollsbriefen.

Datakällor:
  - football-data.org (gratis, v4) — kommande matcher + tabell/form för
    samtliga fem ligor. Kräver miljövariabeln FOOTBALL_DATA_API_KEY.
  - API-Football — valfri, extra lagstatistik men BARA för Premier
    League, för att hålla oss inom den snäva gratiskvoten (100 anrop/
    dygn totalt). Kräver APIFOOTBALL_KEY. Går som standard direkt mot
    api-sports.io (registrera på dashboard.api-football.com, nyckeln
    finns under Account -> My Access). Sätt APIFOOTBALL_PROVIDER=rapidapi
    om du i stället vill gå via RapidAPI/Rapid-marknadsplatsen. Saknas
    nyckeln hoppas berikningen bara över.

Filen skrivs BARA om det faktiska matchinnehållet har ändrats sedan
förra körningen — annars lämnas matches.json orört så vi slipper
tomma commits/timestamp-churn var tredje timme när inget nytt har hänt
(matcher/tabeller ändras sällan mitt på dagen).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
OUTPUT_PATH = ROOT / "matches.json"
USER_AGENT = "Mozilla/5.0 Fotbollsbriefen/Matches-1.0"

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"
FOOTBALL_DATA_TOKEN = os.environ.get("FOOTBALL_DATA_API_KEY", "").strip()

# Standard: direkt mot api-sports.io (enklast — bara APIFOOTBALL_KEY
# behövs). Sätt APIFOOTBALL_PROVIDER=rapidapi för att i stället gå via
# RapidAPI/Rapid, då används APIFOOTBALL_HOST + rapidapi-headers.
APIFOOTBALL_KEY = os.environ.get("APIFOOTBALL_KEY", "").strip()
APIFOOTBALL_PROVIDER = os.environ.get("APIFOOTBALL_PROVIDER", "direct").strip().lower()
APIFOOTBALL_HOST = os.environ.get("APIFOOTBALL_HOST", "api-football-v1.p.rapidapi.com").strip()
APIFOOTBALL_DIRECT_BASE = "https://v3.football.api-sports.io"
APIFOOTBALL_PL_LEAGUE_ID = 39  # Premier League i API-Footballs egen numrering

# Samma fem ligor som redan används för nyheter/Skador-sektionen.
LEAGUES: list[tuple[str, str]] = [
    ("Premier League", "PL"),
    ("La Liga", "PD"),
    ("Serie A", "SA"),
    ("Bundesliga", "BL1"),
    ("Ligue 1", "FL1"),
]

DAYS_AHEAD = 10          # hur långt fram vi hämtar kommande matcher
MAX_MATCHES_PER_LEAGUE = 8
H2H_LOOKUP_LIMIT = 2     # bara de N närmaste matcherna per liga får inbördes-möten (rate limit-hänsyn)
FOOTBALL_DATA_MIN_INTERVAL = 6.2  # sekunder mellan anrop, håller oss under 10/min med marginal

RESULT_POINTS = {"W": 3, "D": 1, "L": 0}


class RateLimiter:
    """Enkel throttling så vi respekterar football-data.orgs 10 anrop/minut."""

    def __init__(self, min_interval: float) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        elapsed = time.monotonic() - self._last
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        self._last = time.monotonic()


football_data_limiter = RateLimiter(FOOTBALL_DATA_MIN_INTERVAL)


def fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20) -> Any:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, **(headers or {})},
    )
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_football_data(path: str) -> Any:
    if not FOOTBALL_DATA_TOKEN:
        raise RuntimeError("FOOTBALL_DATA_API_KEY saknas — hoppar över football-data.org")
    football_data_limiter.wait()
    return fetch_json(
        f"{FOOTBALL_DATA_BASE}{path}",
        headers={"X-Auth-Token": FOOTBALL_DATA_TOKEN},
    )


def fetch_apifootball(path: str, params: dict[str, str]) -> Any:
    if not APIFOOTBALL_KEY:
        raise RuntimeError("APIFOOTBALL_KEY saknas — hoppar över API-Football-berikning")
    query = "&".join(f"{k}={urllib.parse.quote(str(v))}" for k, v in params.items())
    if APIFOOTBALL_PROVIDER == "rapidapi":
        base = f"https://{APIFOOTBALL_HOST}/v3"
        headers = {"x-rapidapi-key": APIFOOTBALL_KEY, "x-rapidapi-host": APIFOOTBALL_HOST}
    else:
        base = APIFOOTBALL_DIRECT_BASE
        headers = {"x-apisports-key": APIFOOTBALL_KEY}
    return fetch_json(f"{base}{path}?{query}", headers=headers)


def current_season_start_year(today: dt.date | None = None) -> int:
    """European-säsonger namnges efter startåret (t.ex. '2025' för 2025/26).
    Säsongen brukar dra igång i augusti, så juni/juli räknas som förra säsongens svans."""
    today = today or dt.date.today()
    return today.year if today.month >= 7 else today.year - 1


def get_standings(competition_code: str) -> dict[int, dict[str, Any]]:
    """Returnerar {team_id: {position, points, played, won, draw, lost, form}}"""
    payload = fetch_football_data(f"/competitions/{competition_code}/standings")
    table: dict[int, dict[str, Any]] = {}
    total_teams = 0
    for group in payload.get("standings", []):
        if group.get("type") != "TOTAL":
            continue
        rows = group.get("table", [])
        total_teams = max(total_teams, len(rows))
        for row in rows:
            team = row.get("team", {})
            team_id = team.get("id")
            if team_id is None:
                continue
            form_raw = (row.get("form") or "")
            form = [r for r in form_raw.split(",") if r in RESULT_POINTS] if form_raw else []
            table[team_id] = {
                "position": row.get("position"),
                "points": row.get("points"),
                "played": row.get("playedGames"),
                "won": row.get("won"),
                "draw": row.get("draw"),
                "lost": row.get("lost"),
                "goal_difference": row.get("goalDifference"),
                "form": form[-5:],
                "total_teams": total_teams,
            }
    return table


def get_scheduled_matches(competition_code: str) -> list[dict[str, Any]]:
    today = dt.date.today()
    date_from = today.isoformat()
    date_to = (today + dt.timedelta(days=DAYS_AHEAD)).isoformat()
    payload = fetch_football_data(
        f"/competitions/{competition_code}/matches"
        f"?status=SCHEDULED&dateFrom={date_from}&dateTo={date_to}"
    )
    matches = payload.get("matches", [])
    matches.sort(key=lambda m: m.get("utcDate", ""))
    return matches[:MAX_MATCHES_PER_LEAGUE]


def get_head_to_head(match_id: int) -> dict[str, Any] | None:
    try:
        payload = fetch_football_data(f"/matches/{match_id}/head2head?limit=10")
    except Exception:
        return None
    aggregates = payload.get("aggregates", {})
    home = aggregates.get("homeTeam", {})
    away = aggregates.get("awayTeam", {})
    total = aggregates.get("numberOfMatches", 0)
    if not total:
        return None
    return {
        "matches": total,
        "home_wins": home.get("wins", 0),
        "away_wins": away.get("wins", 0),
        "draws": max(0, total - home.get("wins", 0) - away.get("wins", 0)),
    }


def team_snapshot(team: dict[str, Any], standings: dict[int, dict[str, Any]]) -> dict[str, Any]:
    row = standings.get(team.get("id"), {})
    return {
        "id": team.get("id"),
        "name": team.get("name") or team.get("shortName") or "Okänt lag",
        "short_name": team.get("shortName") or team.get("tla") or team.get("name"),
        "crest": team.get("crest"),
        "position": row.get("position"),
        "points": row.get("points"),
        "played": row.get("played"),
        "form": row.get("form", []),
        "table_size": row.get("total_teams"),
    }


def _form_score(form: list[str]) -> float:
    """0-100 baserat på poäng i de senaste (max 5) matcherna."""
    if not form:
        return 50.0
    earned = sum(RESULT_POINTS.get(r, 0) for r in form)
    possible = len(form) * 3
    return (earned / possible) * 100 if possible else 50.0


def _table_score(position: int | None, total_teams: int | None) -> float:
    if not position or not total_teams:
        return 50.0
    # Klampad till 0-100: skyddar mot trasig/ofullständig tabelldata
    # (t.ex. en position som råkar ligga utanför antalet hämtade rader).
    raw = ((total_teams - position + 1) / total_teams) * 100
    return max(0.0, min(100.0, raw))


def _h2h_score(h2h: dict[str, Any] | None, home: bool) -> float:
    if not h2h or not h2h.get("matches"):
        return 50.0
    wins = h2h["home_wins"] if home else h2h["away_wins"]
    return max(0.0, min(100.0, (wins / h2h["matches"]) * 100))


def _apportion_to_100(values: dict[str, float]) -> dict[str, int]:
    """Rundar ett antal icke-negativa flyttal som summerar till ~100 till
    heltalsprocent som garanterat summerar till exakt 100 (largest-remainder-
    metoden). Förhindrar att avrundning kan ge en negativ eller >100 andel."""
    floors = {key: int(value // 1) for key, value in values.items()}
    remainder = 100 - sum(floors.values())
    remainders = sorted(values.items(), key=lambda kv: kv[1] - floors[kv[0]], reverse=True)
    result = dict(floors)
    for key, _ in remainders[:max(0, remainder)]:
        result[key] += 1
    return result


def predict_match(
    home: dict[str, Any],
    away: dict[str, Any],
    h2h: dict[str, Any] | None,
) -> dict[str, Any]:
    """Enkel, transparent prognos — INTE ett spelråd.

    Vikter: 45% formkurva (senaste 5 matcherna), 35% tabellplacering,
    20% inbördes möten, plus ett schablonmässigt hemmaplanstillägg.
    Allt bygger på offentligt tillgänglig tabell-/formdata, inga odds."""
    home_score = (
        0.45 * _form_score(home.get("form", []))
        + 0.35 * _table_score(home.get("position"), home.get("table_size"))
        + 0.20 * _h2h_score(h2h, home=True)
        + 8  # hemmaplansfördel
    )
    away_score = (
        0.45 * _form_score(away.get("form", []))
        + 0.35 * _table_score(away.get("position"), away.get("table_size"))
        + 0.20 * _h2h_score(h2h, home=False)
    )

    diff = home_score - away_score
    draw_pct = max(16.0, 30.0 - abs(diff) * 0.35)
    remaining = 100.0 - draw_pct

    total = home_score + away_score
    if total <= 0:
        home_share = away_share = 0.5
    else:
        home_share = home_score / total
        away_share = away_score / total

    home_pct = remaining * home_share
    away_pct = remaining * away_share

    # Largest-remainder-avrundning: garanterat icke-negativa heltal som
    # alltid summerar till exakt 100, oavsett indata.
    rounded = _apportion_to_100({"home": home_pct, "draw": draw_pct, "away": away_pct})

    return {
        "home_win_pct": rounded["home"],
        "draw_pct": rounded["draw"],
        "away_win_pct": rounded["away"],
        "basis": "Formkurva (45%), tabellplacering (35%), inbördes möten (20%) + hemmaplansfördel. "
                 "Statistisk uppskattning — inget spelråd.",
    }


def enrich_with_apifootball_pl(match: dict[str, Any], home_id: int | None, away_id: int | None) -> dict[str, Any] | None:
    """Extra lagstatistik för Premier League, hämtat sparsamt (två anrop
    per match) för att hålla oss långt under API-Footballs 100/dygn."""
    if not APIFOOTBALL_KEY or not home_id or not away_id:
        return None
    season = current_season_start_year()
    try:
        home_stats = fetch_apifootball(
            "/teams/statistics",
            {"league": APIFOOTBALL_PL_LEAGUE_ID, "season": season, "team": home_id},
        ).get("response", {})
        away_stats = fetch_apifootball(
            "/teams/statistics",
            {"league": APIFOOTBALL_PL_LEAGUE_ID, "season": season, "team": away_id},
        ).get("response", {})
    except Exception as exc:
        print(f"WARN API-Football-berikning misslyckades: {exc}")
        return None

    def summarize(stats: dict[str, Any]) -> dict[str, Any]:
        fixtures = stats.get("fixtures", {})
        goals = stats.get("goals", {})
        return {
            "clean_sheets": stats.get("clean_sheet", {}).get("total"),
            "avg_goals_for": goals.get("for", {}).get("average", {}).get("total"),
            "avg_goals_against": goals.get("against", {}).get("average", {}).get("total"),
            "wins_home": fixtures.get("wins", {}).get("home"),
            "wins_away": fixtures.get("wins", {}).get("away"),
        }

    return {"home": summarize(home_stats), "away": summarize(away_stats)}


def build_league(display_name: str, code: str) -> dict[str, Any] | None:
    try:
        standings = get_standings(code)
        matches = get_scheduled_matches(code)
    except Exception as exc:
        print(f"WARN {display_name}: kunde inte hämta data ({exc})")
        return None

    output_matches = []
    for index, raw in enumerate(matches):
        home_team = raw.get("homeTeam", {})
        away_team = raw.get("awayTeam", {})
        home = team_snapshot(home_team, standings)
        away = team_snapshot(away_team, standings)

        h2h = get_head_to_head(raw["id"]) if index < H2H_LOOKUP_LIMIT and raw.get("id") else None
        prediction = predict_match(home, away, h2h)

        extra_stats = None
        if code == "PL":
            extra_stats = enrich_with_apifootball_pl(raw, home_team.get("id"), away_team.get("id"))

        output_matches.append({
            "id": f"fd-{raw.get('id')}",
            "league": display_name,
            "utc_kickoff": raw.get("utcDate"),
            "matchday": raw.get("matchday"),
            "venue": (raw.get("venue") or None),
            "home": home,
            "away": away,
            "h2h": h2h,
            "prediction": prediction,
            "extra_stats": extra_stats,
            "source": "football-data.org" + (" + API-Football" if extra_stats else ""),
        })

    return {"competition_code": code, "matches": output_matches}


def payload_unchanged(new_leagues: dict[str, Any]) -> bool:
    if not OUTPUT_PATH.exists():
        return False
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    existing_leagues = existing.get("leagues", {})
    return json.dumps(existing_leagues, sort_keys=True) == json.dumps(new_leagues, sort_keys=True)


def main() -> int:
    if not FOOTBALL_DATA_TOKEN:
        print("FOOTBALL_DATA_API_KEY saknas i miljön — avbryter utan att röra matches.json.")
        return 0

    leagues: dict[str, Any] = {}
    for display_name, code in LEAGUES:
        league_data = build_league(display_name, code)
        if league_data is not None:
            leagues[display_name] = league_data

    if not leagues:
        print("Ingen ligadata kunde hämtas; lämnar matches.json orört.")
        return 0

    if payload_unchanged(leagues):
        print("Inga förändringar i matcher/tabeller/prognoser sedan förra körningen — skriver inte om filen.")
        return 0

    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "leagues": leagues,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    total_matches = sum(len(l["matches"]) for l in leagues.values())
    print(f"Skrev {total_matches} matcher över {len(leagues)} ligor (innehållet hade ändrats).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
