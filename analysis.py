"""
Turns raw pitch-level Statcast rows into meaningful summary stats.
"""
from statcast_api import _safe_float

HARD_HIT_THRESHOLD = 95.0  # mph exit velocity -- standard, widely-used industry threshold


def quality_of_contact(rows: list[dict]) -> dict:
    """
    Batted-ball quality metrics: avg exit velo, avg launch angle, hard-hit
    rate (exit velo >= 95mph, the standard industry threshold used even
    without needing Statcast's more complex categorical barrel classification).
    """
    batted_balls = []
    for r in rows:
        ev = _safe_float(r.get("launch_speed"))
        la = _safe_float(r.get("launch_angle"))
        if ev is not None and la is not None:
            batted_balls.append({"ev": ev, "la": la})

    if not batted_balls:
        return {"batted_balls": 0}

    avg_ev = sum(b["ev"] for b in batted_balls) / len(batted_balls)
    avg_la = sum(b["la"] for b in batted_balls) / len(batted_balls)
    hard_hit_count = sum(1 for b in batted_balls if b["ev"] >= HARD_HIT_THRESHOLD)
    hard_hit_rate = hard_hit_count / len(batted_balls)

    return {
        "batted_balls": len(batted_balls),
        "avg_exit_velo": round(avg_ev, 1),
        "avg_launch_angle": round(avg_la, 1),
        "hard_hit_rate": round(hard_hit_rate * 100, 1),
    }


def expected_vs_actual(rows: list[dict]) -> dict:
    """
    Actual wOBA vs Statcast's expected wOBA (xwOBA), the classic
    over/underperformance regression signal. Computed from summed
    woba_value/woba_denom for actual (not averaged per-row, which would be
    wrong for a rate stat), and averaged estimated_woba_using_speedangle
    for expected, since that field is already a per-event probability.
    """
    woba_value_sum = 0.0
    woba_denom_sum = 0.0
    xwoba_values = []

    for r in rows:
        wv = _safe_float(r.get("woba_value"))
        wd = _safe_float(r.get("woba_denom"))
        if wv is not None and wd is not None:
            woba_value_sum += wv
            woba_denom_sum += wd

        xwoba = _safe_float(r.get("estimated_woba_using_speedangle"))
        if xwoba is not None:
            xwoba_values.append(xwoba)

    actual_woba = (woba_value_sum / woba_denom_sum) if woba_denom_sum > 0 else None
    expected_woba = (sum(xwoba_values) / len(xwoba_values)) if xwoba_values else None

    gap = None
    if actual_woba is not None and expected_woba is not None:
        gap = actual_woba - expected_woba

    return {
        "actual_woba": round(actual_woba, 3) if actual_woba is not None else None,
        "expected_woba": round(expected_woba, 3) if expected_woba is not None else None,
        "gap": round(gap, 3) if gap is not None else None,
        "sample_size": len(xwoba_values),
    }


def velocity_trend(rows: list[dict]) -> dict:
    """
    Average release speed, split into first-half vs second-half of the
    given date range, to catch a within-window velocity trend (early
    fatigue/decline signal). Grouped by pitch_type since comparing a
    fastball's velo to a changeup's would be meaningless.
    """
    by_pitch_type: dict[str, list[dict]] = {}
    for r in rows:
        pt = r.get("pitch_type")
        speed = _safe_float(r.get("release_speed"))
        date = r.get("game_date")
        if not pt or speed is None or not date:
            continue
        by_pitch_type.setdefault(pt, []).append({"speed": speed, "date": date})

    result = {}
    for pt, entries in by_pitch_type.items():
        if len(entries) < 10:
            continue  # not enough data for a meaningful trend on this pitch type
        entries.sort(key=lambda e: e["date"])
        midpoint = len(entries) // 2
        first_half = entries[:midpoint]
        second_half = entries[midpoint:]
        first_avg = sum(e["speed"] for e in first_half) / len(first_half)
        second_avg = sum(e["speed"] for e in second_half) / len(second_half)
        result[pt] = {
            "first_half_avg": round(first_avg, 1),
            "second_half_avg": round(second_avg, 1),
            "change": round(second_avg - first_avg, 1),
            "count": len(entries),
        }
    return result


def avg_velocity_by_pitch_type(rows: list[dict], min_count: int = 3) -> dict:
    """Simple average release speed per pitch type -- used for both a
    recent-starts baseline and a live in-game snapshot."""
    by_pitch_type: dict[str, list[float]] = {}
    for r in rows:
        pt = r.get("pitch_type")
        speed = _safe_float(r.get("release_speed"))
        if not pt or speed is None:
            continue
        by_pitch_type.setdefault(pt, []).append(speed)

    return {
        pt: round(sum(speeds) / len(speeds), 1)
        for pt, speeds in by_pitch_type.items()
        if len(speeds) >= min_count
    }


def avg_metrics_by_pitch_type(rows: list[dict], min_count: int = 3) -> dict:
    """
    Baseline averages for speed, spin rate, and movement per pitch type,
    from Savant CSV data. Spin rate units (rpm) are confirmed consistent
    with the live feed's spinRate. Movement (pfx_x/pfx_z) units have NOT
    been cross-verified against the live feed's breakVerticalInduced/
    breakHorizontal -- flagged in the bot's output as unverified rather
    than silently assumed correct.
    """
    by_pitch_type: dict[str, dict[str, list[float]]] = {}
    for r in rows:
        pt = r.get("pitch_type")
        if not pt:
            continue
        speed = _safe_float(r.get("release_speed"))
        spin = _safe_float(r.get("release_spin_rate"))
        pfx_z = _safe_float(r.get("pfx_z"))
        pfx_x = _safe_float(r.get("pfx_x"))

        bucket = by_pitch_type.setdefault(pt, {"speed": [], "spin": [], "pfx_z": [], "pfx_x": []})
        if speed is not None:
            bucket["speed"].append(speed)
        if spin is not None:
            bucket["spin"].append(spin)
        if pfx_z is not None:
            bucket["pfx_z"].append(pfx_z)
        if pfx_x is not None:
            bucket["pfx_x"].append(pfx_x)

    result = {}
    for pt, metrics in by_pitch_type.items():
        if len(metrics["speed"]) < min_count:
            continue
        entry = {"speed": round(sum(metrics["speed"]) / len(metrics["speed"]), 1)}
        if metrics["spin"]:
            entry["spin"] = round(sum(metrics["spin"]) / len(metrics["spin"]))
        if metrics["pfx_z"]:
            entry["break_vert"] = round(sum(metrics["pfx_z"]) / len(metrics["pfx_z"]), 1)
        if metrics["pfx_x"]:
            entry["break_horz"] = round(sum(metrics["pfx_x"]) / len(metrics["pfx_x"]), 1)
        result[pt] = entry
    return result


DROP_THRESHOLD_MPH = 2.0  # flag anything down this much or more from baseline
DROP_THRESHOLD_SPIN_PCT = 0.05  # flag spin rate down 5%+ from baseline


def detect_velocity_drops(baseline: dict, live: dict) -> list[dict]:
    """
    Compares a live in-game snapshot against a recent-starts baseline,
    per pitch type, across speed, spin rate, and movement. Only flags
    metrics present in both baseline and live for a given pitch type.
    Movement comparisons are informational only (not used to flag a
    "drop") since the unit consistency between the live feed and Savant
    CSV hasn't been cross-verified -- shown, but not treated as reliable
    enough to trigger an alert on its own yet.
    """
    drops = []
    for pt, live_metrics in live.items():
        baseline_metrics = baseline.get(pt)
        if baseline_metrics is None:
            continue

        live_speed = live_metrics.get("speed")
        baseline_speed = baseline_metrics.get("speed")
        if live_speed is not None and baseline_speed is not None:
            speed_diff = live_speed - baseline_speed
            if speed_diff <= -DROP_THRESHOLD_MPH:
                drops.append({
                    "pitch_type": pt, "metric": "velocity",
                    "baseline": baseline_speed, "live": live_speed, "diff": round(speed_diff, 1),
                })

        live_spin = live_metrics.get("spin")
        baseline_spin = baseline_metrics.get("spin")
        if live_spin is not None and baseline_spin is not None and baseline_spin > 0:
            spin_pct_change = (live_spin - baseline_spin) / baseline_spin
            if spin_pct_change <= -DROP_THRESHOLD_SPIN_PCT:
                drops.append({
                    "pitch_type": pt, "metric": "spin",
                    "baseline": baseline_spin, "live": live_spin,
                    "diff": round(spin_pct_change * 100, 1),
                })

    return drops

