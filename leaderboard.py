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

BATTER_STAT_COLUMNS = {
    "xwoba": ("xwoba", True),
    "xba": ("xba", True),
    "xslg": ("xslg", True),
    "xiso": ("xiso", True),
    "xobp": ("xobp", True),
    "barrel_pct": ("brl_percent", True),
    "exit_velo": ("exit_velocity", True),
    "max_ev": ("max_ev", True),
    "hard_hit_pct": ("hard_hit_percent", True),
    "k_pct": ("k_percent", True),   # this column is Savant's own percentile,
    "bb_pct": ("bb_percent", True), # already correctly oriented -- don't re-invert
}

# Best-informed guess based on Savant's typical pitcher percentile columns
# (same naming convention as batters, where applicable) -- NOT yet verified
# against live data the way the batter columns were. Test with
# /checkleaderboard type:pitcher before trusting these blindly.
PITCHER_STAT_COLUMNS = {
    "xera": ("xera", True),
    "xba_against": ("xba", True),
    "xslg_against": ("xslg", True),
    "xwoba_against": ("xwoba", True),
    "exit_velo_against": ("exit_velocity", True),
    "hard_hit_pct_against": ("hard_hit_percent", True),
    "k_pct": ("k_percent", True),
    "bb_pct": ("bb_percent", True),
    "whiff_pct": ("whiff_percent", True),
    "chase_pct": ("chase_percent", True),
    "fastball_velo": ("fastball_velocity", True),
}

# Kept for backward compatibility
STAT_COLUMNS = BATTER_STAT_COLUMNS


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


def get_leaders(rows: list[dict], stat_key: str, limit: int = 10, stat_columns: dict = None) -> list[dict]:
    """Top N players by a given stat's percentile (using the friendly key).
    These are Savant's own percentile scores (0-100), confirmed -- not raw
    stat values, so ties are common at the extremes."""
    stat_columns = stat_columns or BATTER_STAT_COLUMNS
    if stat_key not in stat_columns:
        return []
    column, _ = stat_columns[stat_key]

    parsed = []
    for r in rows:
        raw = r.get(column)
        if not raw:
            continue
        try:
            value = float(raw)
        except ValueError:
            continue
        parsed.append({"name": r.get("player_name", "?"), "percentile": round(value)})

    parsed.sort(key=lambda p: p["percentile"], reverse=True)
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


def get_percentile(rows: list[dict], stat_key: str, player_name: str, stat_columns: dict = None) -> dict | None:
    """
    Returns this player's percentile directly from Savant's own
    percentile-rankings column -- confirmed these columns are ALREADY
    0-100 percentile scores (not raw stats), verified against a real
    discrepancy: our old self-computed ranking showed K% at the 10th
    percentile when Savant's real page shows 91st for the same player,
    because we were re-inverting a column Savant had already correctly
    oriented (lower K% = better = higher percentile, already applied).
    """
    stat_columns = stat_columns or BATTER_STAT_COLUMNS
    if stat_key not in stat_columns:
        return None
    column, _ = stat_columns[stat_key]

    for r in rows:
        if _name_matches(player_name, r.get("player_name", "")):
            raw = r.get(column)
            if not raw:
                return None
            try:
                percentile = round(float(raw))
            except ValueError:
                return None
            return {"percentile": percentile, "sample_size": len(rows)}
    return None
