"""
Fetches Savant's season leaderboard (real per-player aggregate stats,
confirmed working: 544 batters, 23 columns) and computes both simple
leaders (top N by a stat) and percentile ranks (where does this player
fall among all qualified players -- computed ourselves from the real
distribution, not assumed to come pre-computed from Savant).
"""
import csv
import io
import requests

LEADERBOARD_URL = "https://baseballsavant.mlb.com/leaderboard/percentile-rankings"

# Maps a friendly stat name to the real confirmed column name, plus whether
# higher is better (K% and BB% for pitchers would flip, but this bot's
# leaderboard is batter-focused for now)
STAT_COLUMNS = {
    "xwoba": ("xwoba", True),
    "xba": ("xba", True),
    "xslg": ("xslg", True),
    "xiso": ("xiso", True),
    "xobp": ("xobp", True),
    "barrel_pct": ("brl_percent", True),
    "exit_velo": ("exit_velocity", True),
    "max_ev": ("max_ev", True),
    "hard_hit_pct": ("hard_hit_percent", True),
    "k_pct": ("k_percent", False),  # lower is better for a hitter
    "bb_pct": ("bb_percent", True),
}


def fetch_leaderboard(player_type: str = "batter", year: int = 2026) -> list[dict]:
    """Returns qualified players only (rows with actual data, not the
    empty placeholder rows for unqualified players)."""
    resp = requests.get(
        LEADERBOARD_URL,
        params={"type": player_type, "year": year, "team": "", "csv": "true"},
        timeout=20,
    )
    resp.raise_for_status()
    text = resp.text
    if text.startswith("\ufeff"):
        text = text[1:]

    reader = csv.DictReader(io.StringIO(text))
    all_rows = list(reader)
    # Filter to qualified players: must have a real xwoba value, since
    # that's populated for every qualified player and empty otherwise
    return [r for r in all_rows if r.get("xwoba")]


def get_leaders(rows: list[dict], stat_key: str, limit: int = 10) -> list[dict]:
    """Top N players by a given stat (using the friendly key from STAT_COLUMNS)."""
    if stat_key not in STAT_COLUMNS:
        return []
    column, higher_is_better = STAT_COLUMNS[stat_key]

    parsed = []
    for r in rows:
        raw = r.get(column)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        parsed.append({"name": r.get("player_name", "?"), "value": value})

    parsed.sort(key=lambda p: p["value"], reverse=higher_is_better)
    return parsed[:limit]


def _name_matches(input_name: str, csv_name: str) -> bool:
    """
    CSV names are formatted 'Last, First' (confirmed from real data:
    'Fairchild, Stuart'), but people naturally type 'First Last'. A plain
    substring check fails since 'juan soto' never appears contiguously in
    'soto, juan' -- so instead, check that every word from the input
    appears somewhere in the CSV name, order-independent.
    """
    input_words = set(input_name.lower().replace(",", "").split())
    csv_words = set(csv_name.lower().replace(",", "").split())
    return input_words.issubset(csv_words)


def get_percentile(rows: list[dict], stat_key: str, player_name: str) -> dict | None:
    """
    Computes this player's percentile rank (0-100) among all qualified
    players for a given stat, by ranking within the real distribution --
    standard percentile-rank method, not assumed pre-computed by Savant.
    """
    if stat_key not in STAT_COLUMNS:
        return None
    column, higher_is_better = STAT_COLUMNS[stat_key]

    values = []
    target_value = None
    for r in rows:
        raw = r.get(column)
        if not raw:
            continue
        try:
            v = float(raw)
        except ValueError:
            continue
        values.append(v)
        if _name_matches(player_name, r.get("player_name", "")):
            target_value = v

    if target_value is None or not values:
        return None

    if higher_is_better:
        better_or_equal = sum(1 for v in values if v <= target_value)
    else:
        better_or_equal = sum(1 for v in values if v >= target_value)

    percentile = round((better_or_equal / len(values)) * 100)
    return {"value": target_value, "percentile": percentile, "sample_size": len(values)}
