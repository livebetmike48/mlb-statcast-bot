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



SWING_DESCRIPTIONS = {"swinging_strike", "swinging_strike_blocked", "foul", "foul_tip", "hit_into_play"}
WHIFF_DESCRIPTIONS = {"swinging_strike", "swinging_strike_blocked"}


def pitch_mix_breakdown(rows: list[dict]) -> dict:
    """
    A pitcher's pitch usage breakdown: % thrown, avg velocity, and whiff
    rate per pitch type. Whiff rate = whiffs / swings (not whiffs / total
    pitches, which would understate it -- a batter can only whiff on a
    pitch they actually swung at).
    """
    total_pitches = len(rows)
    if total_pitches == 0:
        return {}

    by_type: dict[str, dict] = {}
    for r in rows:
        pt = r.get("pitch_type")
        if not pt:
            continue
        bucket = by_type.setdefault(pt, {"count": 0, "speeds": [], "swings": 0, "whiffs": 0})
        bucket["count"] += 1
        speed = _safe_float(r.get("release_speed"))
        if speed is not None:
            bucket["speeds"].append(speed)
        desc = r.get("description")
        if desc in SWING_DESCRIPTIONS:
            bucket["swings"] += 1
        if desc in WHIFF_DESCRIPTIONS:
            bucket["whiffs"] += 1

    result = {}
    for pt, b in by_type.items():
        entry = {
            "usage_pct": round(b["count"] / total_pitches * 100, 1),
            "count": b["count"],
        }
        if b["speeds"]:
            entry["avg_velo"] = round(sum(b["speeds"]) / len(b["speeds"]), 1)
        if b["swings"] > 0:
            entry["whiff_pct"] = round(b["whiffs"] / b["swings"] * 100, 1)
        result[pt] = entry
    return dict(sorted(result.items(), key=lambda x: -x[1]["count"]))


def vs_pitch_type_stats(rows: list[dict], pitch_type: str) -> dict | None:
    """
    A batter's performance against ONE specific pitch type: whiff rate,
    batting average (same correct at-bat denominator as the xBA fix --
    strikeouts count as automatic outs, walks excluded), K%, and xwOBA
    (same averaging convention already tested in analysis.expected_vs_actual,
    scoped to just this pitch type's batted balls).
    """
    pitch_rows = [r for r in rows if r.get("pitch_type") == pitch_type]
    if not pitch_rows:
        return None

    swings = sum(1 for r in pitch_rows if r.get("description") in SWING_DESCRIPTIONS)
    whiffs = sum(1 for r in pitch_rows if r.get("description") in WHIFF_DESCRIPTIONS)

    pa_rows = [r for r in pitch_rows if r.get("events")]
    strikeouts = sum(1 for r in pa_rows if r.get("events") == "strikeout")
    walks = sum(1 for r in pa_rows if r.get("events") in {"walk", "intent_walk"})
    hits = sum(1 for r in pa_rows if r.get("events") in {"single", "double", "triple", "home_run"})
    balls_in_play_outs = sum(
        1 for r in pa_rows
        if r.get("events") not in {"strikeout", "walk", "intent_walk", "single", "double", "triple", "home_run", "hit_by_pitch"}
    )
    at_bats = hits + balls_in_play_outs + strikeouts

    xwoba_values = [_safe_float(r.get("estimated_woba_using_speedangle")) for r in pitch_rows]
    xwoba_values = [v for v in xwoba_values if v is not None]

    result = {
        "pitches_seen": len(pitch_rows),
        "swings": swings,
        "pa_ending_on_this_pitch": len(pa_rows),
    }
    if swings > 0:
        result["whiff_pct"] = round(whiffs / swings * 100, 1)
    if at_bats > 0:
        result["avg"] = round(hits / at_bats, 3)
    if pa_rows:
        result["k_pct"] = round(strikeouts / len(pa_rows) * 100, 1)
    if xwoba_values:
        result["xwoba"] = round(sum(xwoba_values) / len(xwoba_values), 3)
    return result


def pitch_mix_by_handedness(rows: list[dict]) -> dict:
    """
    Same pitch mix breakdown, but split by batter handedness (vs LHH / vs
    RHH), since a pitcher's approach often genuinely differs by who's up --
    confirmed real use case: a pitcher throwing his fastball much more to
    one side than the other. Uses the 'stand' field (batter's batting
    side), confirmed present in the real CSV structure since the very
    first test tonight.
    """
    vs_l_rows = [r for r in rows if r.get("stand") == "L"]
    vs_r_rows = [r for r in rows if r.get("stand") == "R"]

    return {
        "vs_L": pitch_mix_breakdown(vs_l_rows),
        "vs_R": pitch_mix_breakdown(vs_r_rows),
        "overall": pitch_mix_breakdown(rows),
    }


def vs_handedness_stats(rows: list[dict], hand_field: str, hand_value: str) -> dict | None:
    """
    Same proven calculation methods as vs_pitch_type_stats (correct AB
    denominator, PA-based K%/BB%, averaged xwOBA) but grouped by
    handedness instead of pitch type. hand_field is 'p_throws' (to split
    a BATTER's stats by pitcher handedness faced) or 'stand' (to split a
    PITCHER's stats by batter handedness faced).
    """
    filtered = [r for r in rows if r.get(hand_field) == hand_value]
    if not filtered:
        return None

    swings = sum(1 for r in filtered if r.get("description") in SWING_DESCRIPTIONS)
    whiffs = sum(1 for r in filtered if r.get("description") in WHIFF_DESCRIPTIONS)

    pa_rows = [r for r in filtered if r.get("events")]
    strikeouts = sum(1 for r in pa_rows if r.get("events") == "strikeout")
    walks = sum(1 for r in pa_rows if r.get("events") in {"walk", "intent_walk"})
    hits = sum(1 for r in pa_rows if r.get("events") in {"single", "double", "triple", "home_run"})
    balls_in_play_outs = sum(
        1 for r in pa_rows
        if r.get("events") not in {"strikeout", "walk", "intent_walk", "single", "double", "triple", "home_run", "hit_by_pitch"}
    )
    at_bats = hits + balls_in_play_outs + strikeouts

    xba_batted = [r for r in filtered if r.get("description") == "hit_into_play"]
    xba_numerator = sum(_safe_float(r.get("estimated_ba_using_speedangle"), 0.0) for r in xba_batted)

    xwoba_values = [_safe_float(r.get("estimated_woba_using_speedangle")) for r in filtered]
    xwoba_values = [v for v in xwoba_values if v is not None]

    result = {"pa": len(pa_rows)}
    if at_bats > 0:
        result["avg"] = round(hits / at_bats, 3)
        result["xba"] = round(xba_numerator / at_bats, 3)
    if swings > 0:
        result["whiff_pct"] = round(whiffs / swings * 100, 1)
    if pa_rows:
        result["k_pct"] = round(strikeouts / len(pa_rows) * 100, 1)
        result["bb_pct"] = round(walks / len(pa_rows) * 100, 1)
    if xwoba_values:
        result["xwoba"] = round(sum(xwoba_values) / len(xwoba_values), 3)
    return result


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
        "stadium": "", "hfBBL": "", "hfNewZones": "", "hfGT": "R|",
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
