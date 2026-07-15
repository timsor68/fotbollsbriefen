#!/usr/bin/env python3
"""
Hämtar väderprognos för kommande matcher (matches.json) via Open-Meteo,
som är gratis, kräver ingen API-nyckel och tillåter 10 000 anrop/dygn
för icke-kommersiellt bruk.

Två steg:
  1. Geokoda varenda unik arena/hemmalag till en lat/lon-koordinat.
     Resultatet cachas i venues_cache.json så vi slipper fråga om
     samma arena varje körning — och du kan själv rätta en felaktig
     träff genom att redigera den filen för hand.
  2. Hämta väderprognosen för avsparkstidpunkten på den koordinaten.

Om en arena inte går att slå upp, eller matchen ligger utanför
Open-Meteos prognoshorisont (16 dagar), hoppas den matchen bara över
i stället för att gissa — hellre ingen väderdata än fel väderdata.

Precis som matches.py skrivs weather.json BARA om innehållet faktiskt
har ändrats sedan förra körningen.
"""
from __future__ import annotations

import datetime as dt
import json
import ssl
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
MATCHES_PATH = ROOT / "matches.json"
OUTPUT_PATH = ROOT / "weather.json"
VENUES_CACHE_PATH = ROOT / "venues_cache.json"
USER_AGENT = "Mozilla/5.0 Fotbollsbriefen/Weather-1.0"

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
FORECAST_HORIZON_DAYS = 16  # Open-Meteos gräns för hur långt fram prognosen räcker

# Förenklad tolkning av Open-Meteos WMO-väderkoder.
WEATHER_CODES: dict[int, dict[str, str]] = {
    0: {"description": "Klart", "icon": "☀️"},
    1: {"description": "Mestadels klart", "icon": "🌤️"},
    2: {"description": "Växlande molnighet", "icon": "⛅"},
    3: {"description": "Mulet", "icon": "☁️"},
    45: {"description": "Dimma", "icon": "🌫️"},
    48: {"description": "Rimfrost/dimma", "icon": "🌫️"},
    51: {"description": "Lätt duggregn", "icon": "🌦️"},
    53: {"description": "Duggregn", "icon": "🌦️"},
    55: {"description": "Kraftigt duggregn", "icon": "🌦️"},
    61: {"description": "Lätt regn", "icon": "🌧️"},
    63: {"description": "Regn", "icon": "🌧️"},
    65: {"description": "Kraftigt regn", "icon": "🌧️"},
    66: {"description": "Underkylt regn", "icon": "🌧️"},
    67: {"description": "Kraftigt underkylt regn", "icon": "🌧️"},
    71: {"description": "Lätt snöfall", "icon": "🌨️"},
    73: {"description": "Snöfall", "icon": "🌨️"},
    75: {"description": "Kraftigt snöfall", "icon": "🌨️"},
    77: {"description": "Snökorn", "icon": "🌨️"},
    80: {"description": "Lätta regnskurar", "icon": "🌦️"},
    81: {"description": "Regnskurar", "icon": "🌦️"},
    82: {"description": "Kraftiga regnskurar", "icon": "🌧️"},
    85: {"description": "Lätta snöbyar", "icon": "🌨️"},
    86: {"description": "Kraftiga snöbyar", "icon": "🌨️"},
    95: {"description": "Åska", "icon": "⛈️"},
    96: {"description": "Åska med lätt hagel", "icon": "⛈️"},
    99: {"description": "Åska med kraftigt hagel", "icon": "⛈️"},
}
DEFAULT_WEATHER_CODE = {"description": "Okänt väder", "icon": "🌡️"}


def fetch_json(url: str, timeout: int = 20) -> Any:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout, context=ssl.create_default_context()) as response:
        return json.loads(response.read().decode("utf-8"))


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def geocode(query: str) -> dict[str, Any] | None:
    params = urllib.parse.urlencode({"name": query, "count": 1, "language": "en", "format": "json"})
    try:
        payload = fetch_json(f"{GEOCODE_URL}?{params}")
    except Exception as exc:
        print(f"WARN geokodning misslyckades för '{query}': {exc}")
        return None
    results = payload.get("results") or []
    if not results:
        return None
    hit = results[0]
    return {
        "lat": hit.get("latitude"),
        "lon": hit.get("longitude"),
        "resolved_name": ", ".join(
            part for part in [hit.get("name"), hit.get("admin1"), hit.get("country")] if part
        ),
    }


def resolve_venue(query: str, cache: dict[str, Any]) -> dict[str, Any] | None:
    if query in cache:
        return cache[query]
    result = geocode(query)
    cache[query] = result  # cachar även None så vi inte spammar samma dåliga fråga varje körning
    return result


def nearest_forecast_hour(hourly: dict[str, list], target_iso: str) -> int | None:
    if not hourly.get("time"):
        return None
    target = dt.datetime.fromisoformat(target_iso.replace("Z", "+00:00"))
    best_index, best_diff = None, None
    for index, time_str in enumerate(hourly["time"]):
        candidate = dt.datetime.fromisoformat(time_str).replace(tzinfo=dt.timezone.utc)
        diff = abs((candidate - target).total_seconds())
        if best_diff is None or diff < best_diff:
            best_index, best_diff = index, diff
    return best_index


def get_forecast(lat: float, lon: float, kickoff_iso: str) -> dict[str, Any] | None:
    params = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,weathercode,windspeed_10m",
        "timezone": "UTC",
        "forecast_days": FORECAST_HORIZON_DAYS,
    })
    try:
        payload = fetch_json(f"{FORECAST_URL}?{params}")
    except Exception as exc:
        print(f"WARN väderprognos misslyckades för ({lat},{lon}): {exc}")
        return None

    hourly = payload.get("hourly", {})
    index = nearest_forecast_hour(hourly, kickoff_iso)
    if index is None:
        return None

    code = int(hourly.get("weathercode", [0] * (index + 1))[index] or 0)
    meta = WEATHER_CODES.get(code, DEFAULT_WEATHER_CODE)

    return {
        "temp_c": hourly.get("temperature_2m", [None])[index],
        "precip_mm": hourly.get("precipitation", [None])[index],
        "wind_kph": hourly.get("windspeed_10m", [None])[index],
        "weather_code": code,
        "description": meta["description"],
        "icon": meta["icon"],
        "forecast_for": hourly["time"][index],
    }


def within_forecast_horizon(kickoff_iso: str) -> bool:
    try:
        kickoff = dt.datetime.fromisoformat(kickoff_iso.replace("Z", "+00:00"))
    except Exception:
        return False
    now = dt.datetime.now(dt.timezone.utc)
    return now <= kickoff <= now + dt.timedelta(days=FORECAST_HORIZON_DAYS)


def payload_unchanged(new_matches: dict[str, Any]) -> bool:
    if not OUTPUT_PATH.exists():
        return False
    try:
        existing = json.loads(OUTPUT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return False
    existing_matches = existing.get("matches", {})
    return json.dumps(existing_matches, sort_keys=True) == json.dumps(new_matches, sort_keys=True)


def main() -> int:
    matches_payload = load_json(MATCHES_PATH, None)
    if not matches_payload:
        print("Ingen matches.json hittades — kör matches.py först. Lämnar weather.json orört.")
        return 0

    cache: dict[str, Any] = load_json(VENUES_CACHE_PATH, {})
    weather_by_match: dict[str, Any] = {}

    for league in matches_payload.get("leagues", {}).values():
        for match in league.get("matches", []):
            kickoff = match.get("utc_kickoff")
            if not kickoff or not within_forecast_horizon(kickoff):
                continue

            venue_query = match.get("venue") or f"{match.get('home', {}).get('name', '')} stadium"
            venue_query = venue_query.strip()
            if not venue_query:
                continue

            location = resolve_venue(venue_query, cache)
            if not location:
                continue

            forecast = get_forecast(location["lat"], location["lon"], kickoff)
            if not forecast:
                continue

            weather_by_match[match["id"]] = {
                "venue_query": venue_query,
                "resolved_location": location.get("resolved_name"),
                **forecast,
            }

    # Cachen sparas alltid (även negativa träffar), så nästa körning slipper
    # fråga om arenor som redan är kända — oavsett om weather.json ändras.
    VENUES_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")

    if payload_unchanged(weather_by_match):
        print("Ingen förändring i väderprognoserna sedan förra körningen — skriver inte om filen.")
        return 0

    payload = {
        "updated_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "matches": weather_by_match,
    }
    OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Skrev väderprognoser för {len(weather_by_match)} matcher (innehållet hade ändrats).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
