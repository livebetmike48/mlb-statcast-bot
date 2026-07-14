"""
Client for Baseball Savant's Statcast CSV export -- confirmed working
tonight (real 119-column pitch-level data, verified against real players).
Not an official API, but a stable CSV export mechanism, distinct from the
JS-rendered search page and Film Room approaches that failed earlier.
"""
import csv
import io
import requests

SAVANT_BASE = "https://baseballsavant.mlb.com/statcast_search/csv"
PEOPLE_SEARCH = "https://statsapi.mlb.com/api/v1/people/search"
MLB_BASE = "https://statsapi.mlb.com/api/v1"
MLB_BASE_V1_1 = "https://statsapi.mlb.com/api/v1.1"


def find_todays_game_for_pitcher(pitcher_id: int, date_str: str) -> int | None:
    """Finds today's game_pk that this pitcher is playing in, if any."""
    resp = requests.get(
        f"{MLB_BASE}/schedule",
        params={"sportId": 1, "date": date_str, "hydrate": "probablePitcher"},
        timeout=15,
    )
    resp.raise_for_status()
    data = resp.json()
    for date_entry in data.get("dates", []):
        for g in date_entry.get("games", []):
            for side in ("home", "away"):
                probable = (g["teams"][side].get("probablePitcher") or {})
                if probable.get("id") == pitcher_id:
                    return g["gamePk"]
    return None


def get_live_pitch_metrics(game_pk: int, pitcher_id: int) -> dict:
    """
    Pulls this pitcher's pitches SO FAR in a live (or completed) game
    directly from MLB's own official live feed -- confirmed to include
    real-time pitchData.startSpeed, plus spin rate and movement, per pitch,
    since this is the same feed that powers Gameday itself. Grouped by
    pitch type.
    """
    resp = requests.get(f"{MLB_BASE_V1_1}/game/{game_pk}/feed/live", timeout=15)
    resp.raise_for_status()
    data = resp.json()

    by_pitch_type: dict[str, dict[str, list[float]]] = {}
    plays = (data.get("liveData") or {}).get("plays", {}).get("allPlays", [])
    for play in plays:
        if (play.get("matchup") or {}).get("pitcher", {}).get("id") != pitcher_id:
            continue
        for event in play.get("playEvents", []):
            if not event.get("isPitch"):
                continue
            pitch_data = event.get("pitchData") or {}
            breaks = pitch_data.get("breaks") or {}
            speed = pitch_data.get("startSpeed")
            spin = breaks.get("spinRate")
            break_vert = breaks.get("breakVerticalInduced")
            break_horz = breaks.get("breakHorizontal")
            pitch_type = (event.get("details") or {}).get("type", {}).get("code")
            if speed is None or not pitch_type:
                continue
            bucket = by_pitch_type.setdefault(pitch_type, {"speed": [], "spin": [], "break_vert": [], "break_horz": []})
            bucket["speed"].append(speed)
            if spin is not None:
                bucket["spin"].append(spin)
            if break_vert is not None:
                bucket["break_vert"].append(break_vert)
            if break_horz is not None:
                bucket["break_horz"].append(break_horz)

    result = {}
    for pt, metrics in by_pitch_type.items():
        entry = {"speed": round(sum(metrics["speed"]) / len(metrics["speed"]), 1)}
        if metrics["spin"]:
            entry["spin"] = round(sum(metrics["spin"]) / len(metrics["spin"]))
        if metrics["break_vert"]:
            entry["break_vert"] = round(sum(metrics["break_vert"]) / len(metrics["break_vert"]), 1)
        if metrics["break_horz"]:
            entry["break_horz"] = round(sum(metrics["break_horz"]) / len(metrics["break_horz"]), 1)
        result[pt] = entry
    return result


def get_live_pitch_velocity(game_pk: int, pitcher_id: int) -> dict:
    """Kept for backward compatibility -- velocity only, no spin."""
    full = get_live_pitch_metrics(game_pk, pitcher_id)
    return {pt: metrics["speed"] for pt, metrics in full.items()}


def fetch_percentile_leaderboard(player_type: str, year: int, team: str = "") -> str:
    """
    Raw text response from Savant's percentile-rankings leaderboard,
    confirmed as a real live page. The team parameter was already visibly
    part of the confirmed working URL (as an empty value) -- passing a
    real team abbreviation to filter by team, using the same confirmed
    &csv=true convention.
    """
    url = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"
    params = {"type": player_type, "year": year, "team": team, "csv": "true"}
    resp = requests.get(url, params=params, timeout=20)
    resp.raise_for_status()
    return resp.text



def resolve_player(name: str) -> dict | None:
    """Returns {'id':, 'name':, 'is_pitcher': bool} or None if not found."""
    resp = requests.get(PEOPLE_SEARCH, params={"names": name}, timeout=15)
    resp.raise_for_status()
    people = resp.json().get("people", [])
    if not people:
        return None
    p = people[0]
    position_code = (p.get("primaryPosition") or {}).get("code")
    return {"id": p["id"], "name": p.get("fullName", name), "is_pitcher": position_code == "1"}


def fetch_statcast(player_id: int, is_pitcher: bool, start_date: str, end_date: str) -> list[dict]:
    """Returns a list of pitch-level dicts (using DictReader for clean field access)."""
    params = {
        "all": "true", "hfPT": "", "hfAB": "", "hfBBT": "", "hfPR": "", "hfZ": "",
        "stadium": "", "hfBBL": "", "hfNewZones": "", "hfGT": "R|PO|S|=",
        "hfSea": "", "hfSit": "", "hfOuts": "",
        "opponent": "", "pitcher_throws": "", "batter_stands": "", "hfSA": "",
        "game_date_gt": start_date, "game_date_lt": end_date,
        "team": "", "position": "", "hfRO": "",
        "home_road": "", "hfFlag": "", "metric_1": "", "hfInn": "",
        "min_pitches": 0, "min_results": 0, "group_by": "name",
        "sort_col": "pitches", "player_event_sort": "h_launch_speed",
        "sort_order": "desc", "min_abs": 0, "type": "details",
    }
    if is_pitcher:
        params["player_type"] = "pitcher"
        params["pitchers_lookup[]"] = player_id
    else:
        params["player_type"] = "batter"
        params["batters_lookup[]"] = player_id

    resp = requests.get(SAVANT_BASE, params=params, timeout=30)
    resp.raise_for_status()

    # The header has a BOM on the first column name (confirmed from real
    # data tonight: '\ufeff"pitch_type"') -- csv.DictReader with utf-8-sig
    # handling avoids that becoming part of the first key.
    text = resp.text
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


def _safe_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
