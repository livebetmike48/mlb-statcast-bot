import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

import statcast_api
import analysis
import leaderboard
import storage

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("statcast_bot")

intents = discord.Intents.default()

_leaderboard_cache = {}


def ordinal(n: int) -> str:
    """82 -> '82nd', 61 -> '61st', 94 -> '94th', 100 -> '100th'."""
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def et_date_str(offset_days: int = 0) -> str:
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    et += timedelta(days=offset_days)
    return et.strftime("%Y-%m-%d")


import re


def _validate_date(date_str: str) -> bool:
    """Strict YYYY-MM-DD check -- Savant's API expects this exact format.
    A malformed date (e.g. MM-DD-YYYY) can silently fail to parse on
    Savant's end and default to a much wider range instead of erroring
    clearly, which is exactly what caused a 3-month request to return
    roughly 9x the expected pitch count."""
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", date_str))


async def _resolve_and_fetch(interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
    """Shared resolution + fetch logic, returns (player, rows) or sends an error and returns None."""
    for label, date_str in [("start_date", start_date), ("end_date", end_date)]:
        if not _validate_date(date_str):
            await interaction.followup.send(
                f"'{date_str}' isn't in the right format for {label}. Use YYYY-MM-DD exactly "
                f"(e.g. 2026-04-10), not MM-DD-YYYY or any other format -- a malformed date can "
                f"silently return wildly wrong, oversized results instead of an error."
            )
            return None

    try:
        player = await asyncio.to_thread(statcast_api.resolve_player, player_name)
    except Exception as e:
        await interaction.followup.send(f"Player lookup failed: {e}")
        return None

    if player is None:
        await interaction.followup.send(f"No player found matching '{player_name}'.")
        return None

    try:
        rows = await asyncio.to_thread(
            statcast_api.fetch_statcast, player["id"], player["is_pitcher"], start_date, end_date
        )
    except Exception as e:
        await interaction.followup.send(f"Statcast fetch failed: {e}")
        return None

    if not rows:
        await interaction.followup.send(
            f"No Statcast data found for {player['name']} between {start_date} and {end_date}."
        )
        return None

    return player, rows


class StatcastBot(discord.Client):
    def __init__(self):
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        storage.init_db()

        velo_cmd = app_commands.Command(
            name="velo",
            description="Pitcher velocity trend by pitch type over a date range",
            callback=self._velo_callback,
        )
        self.tree.add_command(velo_cmd)
        velo_cmd.autocomplete("player_name")(self._pitcher_name_autocomplete)

        livevelo_cmd = app_commands.Command(
            name="livevelo",
            description="Check if a pitcher's velocity is down live, right now, vs their recent baseline",
            callback=self._livevelo_callback,
        )
        self.tree.add_command(livevelo_cmd)
        livevelo_cmd.autocomplete("player_name")(self._pitcher_name_autocomplete)

        pitchmix_cmd = app_commands.Command(
            name="pitchmix",
            description="A pitcher's pitch usage %, velocity, and whiff rate per pitch type",
            callback=self._pitchmix_callback,
        )
        self.tree.add_command(pitchmix_cmd)
        pitchmix_cmd.autocomplete("player_name")(self._pitcher_name_autocomplete)

        vseachpitch_cmd = app_commands.Command(
            name="vseachpitch",
            description="Full table: a player's stats vs EVERY pitch type, filterable by opponent hand",
            callback=self._vseachpitch_callback,
        )
        self.tree.add_command(vseachpitch_cmd)
        vseachpitch_cmd.autocomplete("player_name")(self._any_player_autocomplete)

        matchup_cmd = app_commands.Command(
            name="matchup",
            description="Batter vs pitcher: his mix vs their side, their numbers vs each of his pitches",
            callback=self._matchup_callback,
        )
        self.tree.add_command(matchup_cmd)
        matchup_cmd.autocomplete("batter_name")(self._batter_name_autocomplete)
        matchup_cmd.autocomplete("pitcher_name")(self._pitcher_name_autocomplete)

        vshand_cmd = app_commands.Command(
            name="vshand",
            description="Batter's split vs LHP/RHP, or pitcher's split vs LHB/RHB",
            callback=self._vshand_callback,
        )
        self.tree.add_command(vshand_cmd)
        vshand_cmd.autocomplete("player_name")(self._any_player_autocomplete)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel for automatic velocity/spin drop alerts",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        leaders_cmd = app_commands.Command(
            name="leaders",
            description="Top 10 in a Statcast stat (player_type: batter or pitcher, defaults to batter)",
            callback=self._leaders_callback,
        )
        self.tree.add_command(leaders_cmd)
        leaders_cmd.autocomplete("stat")(self._stat_autocomplete)

        bottomfeeders_cmd = app_commands.Command(
            name="bottomfeeders",
            description="Bottom 10 (worst) in a Statcast stat -- the flip side of /leaders",
            callback=self._bottomfeeders_callback,
        )
        self.tree.add_command(bottomfeeders_cmd)
        bottomfeeders_cmd.autocomplete("stat")(self._stat_autocomplete)

        percentile_cmd = app_commands.Command(
            name="percentile",
            description="A player's percentile ranks (player_type: batter or pitcher, defaults to batter)",
            callback=self._percentile_callback,
        )
        self.tree.add_command(percentile_cmd)
        percentile_cmd.autocomplete("player_name")(self._player_name_autocomplete)

        try:
            guild_id = os.getenv("GUILD_ID")
            if guild_id:
                guild = discord.Object(id=int(guild_id))
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
                log.info("Synced %d slash commands to guild %s", len(synced), guild_id)
            else:
                synced = await self.tree.sync()
                log.info("Synced %d slash commands globally", len(synced))
        except Exception as e:
            log.error("Slash command sync failed: %s", e)

    async def _velo_callback(self, interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
        await interaction.response.defer()
        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        trend = analysis.velocity_trend(rows)
        if not trend:
            await interaction.followup.send(f"{player['name']}: not enough pitches in that range for a trend (need 10+ per pitch type).")
            return

        embed = discord.Embed(title=f"{player['name']} — Velocity Trend", color=discord.Color.red())
        for pitch_type, data in sorted(trend.items(), key=lambda x: -x[1]["count"]):
            arrow = "📉" if data["change"] < -0.5 else ("📈" if data["change"] > 0.5 else "➡️")
            embed.add_field(
                name=f"{pitch_type} ({data['count']} pitches)",
                value=f"{data['first_half_avg']} → {data['second_half_avg']} mph {arrow} ({data['change']:+.1f})",
                inline=False,
            )
        embed.set_footer(text=f"{start_date} to {end_date} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _livevelo_callback(self, interaction: discord.Interaction, player_name: str):
        await interaction.response.defer()
        try:
            player = await asyncio.to_thread(statcast_api.resolve_player, player_name)
        except Exception as e:
            await interaction.followup.send(f"Player lookup failed: {e}")
            return
        if player is None:
            await interaction.followup.send(f"No player found matching '{player_name}'.")
            return
        if not player["is_pitcher"]:
            await interaction.followup.send(f"{player['name']} isn't a pitcher -- live velocity tracking is pitcher-only.")
            return

        today = et_date_str(0)

        try:
            game_pk = await asyncio.to_thread(statcast_api.find_todays_game_for_pitcher, player["id"], today)
        except Exception as e:
            await interaction.followup.send(f"Couldn't check today's schedule: {e}")
            return

        if game_pk is None:
            await interaction.followup.send(f"{player['name']} doesn't appear to be starting today.")
            return

        try:
            live = await asyncio.to_thread(statcast_api.get_live_pitch_metrics, game_pk, player["id"])
        except Exception as e:
            await interaction.followup.send(f"Couldn't pull live pitch data: {e}")
            return

        if not live:
            await interaction.followup.send(
                f"No pitches recorded yet for {player['name']} in today's game -- game may not have started, "
                f"or he hasn't taken the mound yet."
            )
            return

        baseline_start = et_date_str(-30)
        baseline_end = et_date_str(-1)
        try:
            baseline_rows = await asyncio.to_thread(
                statcast_api.fetch_statcast, player["id"], True, baseline_start, baseline_end
            )
        except Exception as e:
            await interaction.followup.send(f"Couldn't fetch baseline: {e}")
            return

        if not baseline_rows:
            await interaction.followup.send(f"No recent baseline data found for {player['name']} in the last 30 days.")
            return

        baseline = analysis.avg_metrics_by_pitch_type(baseline_rows)
        drops = analysis.detect_velocity_drops(baseline, live)

        embed = discord.Embed(
            title=f"{player['name']} — Live Pitch Check",
            color=discord.Color.orange() if drops else discord.Color.green(),
        )
        for pt, live_metrics in live.items():
            baseline_metrics = baseline.get(pt)
            lines = []
            if baseline_metrics and "speed" in baseline_metrics:
                diff = live_metrics["speed"] - baseline_metrics["speed"]
                flag = " ⚠️" if diff <= -analysis.DROP_THRESHOLD_MPH else ""
                lines.append(f"Velo: {baseline_metrics['speed']} → {live_metrics['speed']} mph ({diff:+.1f}){flag}")
            else:
                lines.append(f"Velo: {live_metrics['speed']} mph (no baseline)")

            if "spin" in live_metrics and baseline_metrics and "spin" in baseline_metrics:
                spin_diff_pct = (live_metrics["spin"] - baseline_metrics["spin"]) / baseline_metrics["spin"] * 100
                flag = " ⚠️" if spin_diff_pct <= -analysis.DROP_THRESHOLD_SPIN_PCT * 100 else ""
                lines.append(f"Spin: {baseline_metrics['spin']} → {live_metrics['spin']} rpm ({spin_diff_pct:+.1f}%){flag}")

            if "break_vert" in live_metrics and baseline_metrics and "break_vert" in baseline_metrics:
                lines.append(
                    f"Movement (inches): IVB {baseline_metrics['break_vert']}→{live_metrics['break_vert']}, "
                    f"H {baseline_metrics.get('break_horz', '-')}→{live_metrics.get('break_horz', '-')}"
                )

            embed.add_field(name=pt, value="\n".join(lines), inline=False)

        if drops:
            embed.description = f"⚠️ **{len(drops)} metric(s) showing a real drop from baseline**"
        else:
            embed.description = "✅ No significant drop detected"

        embed.set_footer(text="Live: MLB official feed (verified real-time vs Savant, 2026 ASG) • Baseline: last 30 days, Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _player_name_autocomplete(self, interaction: discord.Interaction, current: str):
        player_type = getattr(interaction.namespace, "player_type", None) or "batter"
        try:
            rows = await self._get_cached_leaderboard(player_type)
        except Exception:
            return []  # autocomplete failures should just show no suggestions, not error out

        current_lower = current.lower()
        matches = []
        for r in rows:
            csv_name = r.get("player_name", "")  # "Last, First" format
            if current_lower in csv_name.lower():
                # Convert to natural "First Last" for display and for the value actually submitted
                parts = [p.strip() for p in csv_name.split(",")]
                display_name = f"{parts[1]} {parts[0]}" if len(parts) == 2 else csv_name
                matches.append(display_name)
            if len(matches) >= 25:
                break

        return [app_commands.Choice(name=name, value=name) for name in matches]

    async def _players_matching(self, player_type: str, current: str) -> list[str]:
        """Shared core: qualified player names from the cached leaderboard,
        converted from the CSV's 'Last, First' to natural 'First Last'."""
        try:
            rows = await self._get_cached_leaderboard(player_type)
        except Exception:
            return []
        current_lower = current.lower()
        matches = []
        for r in rows:
            csv_name = r.get("player_name", "")
            if current_lower in csv_name.lower():
                parts = [p.strip() for p in csv_name.split(",")]
                matches.append(f"{parts[1]} {parts[0]}" if len(parts) == 2 else csv_name)
            if len(matches) >= 25:
                break
        return matches

    async def _batter_name_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await self._players_matching("batter", current)
        return [app_commands.Choice(name=n, value=n) for n in names]

    async def _pitcher_name_autocomplete(self, interaction: discord.Interaction, current: str):
        names = await self._players_matching("pitcher", current)
        return [app_commands.Choice(name=n, value=n) for n in names]

    async def _any_player_autocomplete(self, interaction: discord.Interaction, current: str):
        batters = await self._players_matching("batter", current)
        pitchers = await self._players_matching("pitcher", current)
        merged = list(dict.fromkeys(batters + pitchers))[:25]
        return [app_commands.Choice(name=n, value=n) for n in merged]

    async def _stat_autocomplete(self, interaction: discord.Interaction, current: str):
        current_lower = current.lower()
        # Show both batter and pitcher stat keys, since we don't know which
        # type they'll pick until the command actually runs
        all_keys = list(leaderboard.BATTER_STAT_COLUMNS) + list(leaderboard.PITCHER_STAT_COLUMNS)
        matches = [s for s in dict.fromkeys(all_keys) if current_lower in s.lower()][:25]
        return [app_commands.Choice(name=s, value=s) for s in matches]

    async def _get_cached_leaderboard(self, player_type: str, team: str = ""):
        global _leaderboard_cache
        now = datetime.now(timezone.utc)
        cache_key = (player_type, team)
        cached = _leaderboard_cache.get(cache_key)
        if cached and (now - cached["fetched_at"]).total_seconds() < 3600:
            return cached["rows"]
        rows = await asyncio.to_thread(leaderboard.fetch_leaderboard, player_type, 2026, team)
        _leaderboard_cache[cache_key] = {"rows": rows, "fetched_at": now}
        return rows

    async def _leaders_callback(self, interaction: discord.Interaction, stat: str,
                                 player_type: Literal["batter", "pitcher"] = "batter", team: str = ""):
        await interaction.response.defer()
        stat_columns = leaderboard.PITCHER_STAT_COLUMNS if player_type == "pitcher" else leaderboard.BATTER_STAT_COLUMNS
        if stat not in stat_columns:
            await interaction.followup.send(f"'{stat}' isn't a {player_type} stat. Try: {', '.join(stat_columns)}")
            return

        try:
            rows = await self._get_cached_leaderboard(player_type, team.upper())
        except Exception as e:
            await interaction.followup.send(f"Couldn't fetch leaderboard: {e}")
            return

        leaders = leaderboard.get_leaders(rows, stat, limit=10, stat_columns=stat_columns)
        if not leaders:
            team_note = f" for {team.upper()}" if team else ""
            await interaction.followup.send(f"No qualified {player_type}s found for '{stat}'{team_note}.")
            return

        lines = [f"{i+1}. {p['name']} — {ordinal(p['percentile'])} percentile" for i, p in enumerate(leaders)]
        title_team = f" ({team.upper()})" if team else ""
        embed = discord.Embed(title=f"MLB {player_type.title()} Leaders{title_team} — {stat}", description="\n".join(lines), color=discord.Color.purple())
        embed.set_footer(text=f"2026 season, qualified {player_type}s • Savant's own percentile scores • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _bottomfeeders_callback(self, interaction: discord.Interaction, stat: str,
                                       player_type: Literal["batter", "pitcher"] = "batter", team: str = ""):
        await interaction.response.defer()
        stat_columns = leaderboard.PITCHER_STAT_COLUMNS if player_type == "pitcher" else leaderboard.BATTER_STAT_COLUMNS
        if stat not in stat_columns:
            await interaction.followup.send(f"'{stat}' isn't a {player_type} stat. Try: {', '.join(stat_columns)}")
            return

        try:
            rows = await self._get_cached_leaderboard(player_type, team.upper())
        except Exception as e:
            await interaction.followup.send(f"Couldn't fetch leaderboard: {e}")
            return

        worst = leaderboard.get_leaders(rows, stat, limit=10, stat_columns=stat_columns, worst=True)
        if not worst:
            team_note = f" for {team.upper()}" if team else ""
            await interaction.followup.send(f"No qualified {player_type}s found for '{stat}'{team_note}.")
            return

        lines = [f"{i+1}. {p['name']} — {ordinal(p['percentile'])} percentile" for i, p in enumerate(worst)]
        title_team = f" ({team.upper()})" if team else ""
        embed = discord.Embed(title=f"🪳 Bottom Feeders{title_team} — {player_type} {stat}", description="\n".join(lines), color=discord.Color.dark_grey())
        embed.set_footer(text=f"2026 season, qualified {player_type}s • Savant's own percentile scores • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _percentile_callback(self, interaction: discord.Interaction, player_name: str, player_type: Literal["batter", "pitcher"] = "batter"):
        await interaction.response.defer()
        stat_columns = leaderboard.PITCHER_STAT_COLUMNS if player_type == "pitcher" else leaderboard.BATTER_STAT_COLUMNS

        try:
            rows = await self._get_cached_leaderboard(player_type)
        except Exception as e:
            await interaction.followup.send(f"Couldn't fetch leaderboard: {e}")
            return

        results = {}
        for stat_key in stat_columns:
            result = leaderboard.get_percentile(rows, stat_key, player_name, stat_columns=stat_columns)
            if result:
                results[stat_key] = result

        if not results:
            await interaction.followup.send(
                f"No qualified {player_type} data found for '{player_name}' -- check spelling, wrong player_type "
                f"(try the other one), or they may not meet the qualification threshold."
            )
            return

        embed = discord.Embed(title=f"{player_name} — Percentile Rankings ({player_type})", color=discord.Color.teal())
        for stat_key, result in results.items():
            pct = result["percentile"]
            bar_filled = "🟩" * (pct // 10)
            bar_empty = "⬜" * (10 - pct // 10)
            embed.add_field(name=stat_key, value=f"{bar_filled}{bar_empty} {ordinal(pct)} percentile", inline=False)
        embed.set_footer(text=f"2026 season, among {list(results.values())[0]['sample_size']} qualified {player_type}s • Savant's own percentile scores")
        await interaction.followup.send(embed=embed)

    async def _pitchmix_callback(self, interaction: discord.Interaction, player_name: str,
                                  start_date: str = None, end_date: str = None):
        await interaction.response.defer()
        if not start_date:
            start_date = f"{et_date_str(0)[:4]}-01-01"
        if not end_date:
            end_date = et_date_str(0)

        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        split = statcast_api.pitch_mix_by_handedness(rows)
        if not split["overall"]:
            await interaction.followup.send(f"{player['name']}: no pitch data found in that range.")
            return

        embed = discord.Embed(title=f"{player['name']} — Pitch Mix (vs LHH / vs RHH)", color=discord.Color.blue())
        for pt in split["overall"]:
            vs_l = split["vs_L"].get(pt)
            vs_r = split["vs_R"].get(pt)
            l_str = f"{vs_l['usage_pct']}%" if vs_l else "0%"
            r_str = f"{vs_r['usage_pct']}%" if vs_r else "0%"
            overall = split["overall"][pt]
            velo = f"{overall['avg_velo']} mph" if "avg_velo" in overall else "-"
            embed.add_field(
                name=f"{pt} — {overall['usage_pct']}% overall ({velo})",
                value=f"vs LHH: {l_str} • vs RHH: {r_str}",
                inline=False,
            )
        date_label = "season-to-date" if start_date.endswith("-01-01") else f"{start_date} to {end_date}"
        embed.set_footer(text=f"{date_label} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _vseachpitch_callback(self, interaction: discord.Interaction, player_name: str,
                                     hand: Literal["L", "R", "all"] = "all",
                                     start_date: str = None, end_date: str = None):
        await interaction.response.defer()
        if not start_date:
            start_date = f"{et_date_str(0)[:4]}-01-01"
        if not end_date:
            end_date = et_date_str(0)

        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        hand_field = "stand" if player["is_pitcher"] else "p_throws"
        if hand in ("L", "R"):
            rows = [r for r in rows if r.get(hand_field) == hand]

        table = statcast_api.vs_each_pitch(rows)
        if not table:
            await interaction.followup.send(f"{player['name']}: no pitch data found in that range (hand={hand}).")
            return

        if player["is_pitcher"]:
            hand_label = f" (vs {hand}HB)" if hand in ("L", "R") else " (vs all batters)"
        else:
            hand_label = f" (vs {hand}HP)" if hand in ("L", "R") else " (vs all pitchers)"

        embed = discord.Embed(title=f"{player['name']} — vs Each Pitch{hand_label}", color=discord.Color.dark_green())
        for pt, s in table.items():
            parts = [f"PA: {s['pa_ending_on_this_pitch']}"]
            if "xba" in s:
                parts.append(f"xBA: {s['xba']}")
            if "xwoba" in s:
                parts.append(f"xwOBA: {s['xwoba']}")
            if "whiff_pct" in s:
                parts.append(f"Whiff: {s['whiff_pct']}%")
            if "k_pct" in s:
                parts.append(f"K: {s['k_pct']}%")
            embed.add_field(
                name=f"{pt} ({s['pitches_seen']} pitches)",
                value=" • ".join(parts),
                inline=False,
            )
        embed.set_footer(text=f"{start_date} to {end_date} • min 10 pitches per type • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _matchup_callback(self, interaction: discord.Interaction, batter_name: str, pitcher_name: str,
                                 start_date: str = None, end_date: str = None):
        await interaction.response.defer()
        if not start_date:
            start_date = f"{et_date_str(0)[:4]}-01-01"
        if not end_date:
            end_date = et_date_str(0)
        for label, d in [("start_date", start_date), ("end_date", end_date)]:
            if not _validate_date(d):
                await interaction.followup.send(f"'{d}' isn't valid for {label} -- use YYYY-MM-DD.")
                return

        try:
            batter = await asyncio.to_thread(statcast_api.resolve_player, batter_name)
            pitcher = await asyncio.to_thread(statcast_api.resolve_player, pitcher_name)
        except Exception as e:
            await interaction.followup.send(f"Player lookup failed: {e}")
            return
        if batter is None or pitcher is None:
            missing = batter_name if batter is None else pitcher_name
            await interaction.followup.send(f"No player found matching '{missing}'.")
            return
        if not pitcher.get("pitch_hand"):
            await interaction.followup.send(f"Couldn't determine {pitcher['name']}'s throwing hand.")
            return

        pitcher_hand = pitcher["pitch_hand"]  # 'L' or 'R'
        batter_side = statcast_api.effective_bat_side(batter.get("bat_side") or "R", pitcher_hand)

        try:
            batter_rows = await asyncio.to_thread(
                statcast_api.fetch_statcast, batter["id"], False, start_date, end_date)
            pitcher_rows = await asyncio.to_thread(
                statcast_api.fetch_statcast, pitcher["id"], True, start_date, end_date)
        except Exception as e:
            await interaction.followup.send(f"Statcast fetch failed: {e}")
            return

        # Pitcher's mix vs this batter's side; batter's numbers vs this pitcher's hand
        pitcher_vs_side = [r for r in pitcher_rows if r.get("stand") == batter_side]
        batter_vs_hand = [r for r in batter_rows if r.get("p_throws") == pitcher_hand]

        mix = statcast_api.pitch_mix_breakdown(pitcher_vs_side)
        batter_table = statcast_api.vs_each_pitch(batter_vs_hand, min_pitches=1)
        batter_overall = statcast_api.vs_handedness_stats(batter_rows, "p_throws", pitcher_hand)
        pitcher_overall = statcast_api.vs_handedness_stats(pitcher_rows, "stand", batter_side)

        if not mix or not batter_table:
            await interaction.followup.send(
                f"Not enough data: {'no pitcher data vs ' + batter_side + 'HB' if not mix else ''} "
                f"{'no batter data vs ' + pitcher_hand + 'HP' if not batter_table else ''}".strip()
            )
            return

        switch_note = " (switch, bats " + batter_side + " here)" if batter.get("bat_side") == "S" else ""
        embed = discord.Embed(
            title=f"{batter['name']} ({batter.get('bat_side', '?')}){switch_note} vs {pitcher['name']} ({pitcher_hand})",
            color=discord.Color.gold(),
        )

        if batter_overall:
            embed.add_field(
                name=f"{batter['name']} overall vs {pitcher_hand}HP",
                value=f"PA: {batter_overall['pa']} • xBA: {batter_overall.get('xba', '-')} • xwOBA: {batter_overall.get('xwoba', '-')} • Whiff: {batter_overall.get('whiff_pct', '-')}% • K: {batter_overall.get('k_pct', '-')}% • BB: {batter_overall.get('bb_pct', '-')}%",
                inline=False,
            )
        if pitcher_overall:
            embed.add_field(
                name=f"{pitcher['name']} overall vs {batter_side}HB",
                value=f"PA: {pitcher_overall['pa']} • xBA: {pitcher_overall.get('xba', '-')} • xwOBA: {pitcher_overall.get('xwoba', '-')} • Whiff: {pitcher_overall.get('whiff_pct', '-')}% • K: {pitcher_overall.get('k_pct', '-')}%",
                inline=False,
            )

        # The merged view: each pitch he throws to this side, with the batter's numbers against it
        for pt, m in mix.items():
            b = batter_table.get(pt)
            if b:
                batter_line = f"{batter['name'].split()[-1]}: {b['pa_ending_on_this_pitch']} PA • xBA {b.get('xba', '-')} • xwOBA {b.get('xwoba', '-')} • Whiff {b.get('whiff_pct', '-')}% • K {b.get('k_pct', '-')}%"
            else:
                batter_line = f"{batter['name'].split()[-1]}: hasn't faced this pitch from {pitcher_hand}HP this season"
            embed.add_field(
                name=f"{pt} — {m['usage_pct']}% usage ({m['count']} thrown{', ' + str(m['avg_velo']) + ' mph' if 'avg_velo' in m else ''})",
                value=batter_line,
                inline=False,
            )

        embed.set_footer(text=f"{start_date} to {end_date} • Pitcher mix vs {batter_side}HB • Batter stats vs {pitcher_hand}HP • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _vshand_callback(self, interaction: discord.Interaction, player_name: str,
                                start_date: str = None, end_date: str = None):
        await interaction.response.defer()
        if not start_date:
            start_date = f"{et_date_str(0)[:4]}-01-01"
        if not end_date:
            end_date = et_date_str(0)

        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        # A batter's stats split by the PITCHER's hand (p_throws); a
        # pitcher's stats split by the BATTER's hand (stand) -- confirmed
        # both fields present since the very first test tonight.
        hand_field = "stand" if player["is_pitcher"] else "p_throws"
        vs_l = statcast_api.vs_handedness_stats(rows, hand_field, "L")
        vs_r = statcast_api.vs_handedness_stats(rows, hand_field, "R")

        if not vs_l and not vs_r:
            await interaction.followup.send(f"{player['name']}: no data found in that range.")
            return

        opp_label = "LHB / RHB" if player["is_pitcher"] else "LHP / RHP"
        embed = discord.Embed(title=f"{player['name']} — Split vs {opp_label}", color=discord.Color.dark_teal())

        def _fmt(label, key, pct=False):
            l_val = vs_l.get(key) if vs_l else None
            r_val = vs_r.get(key) if vs_r else None
            l_str = f"{l_val}%" if pct and l_val is not None else (str(l_val) if l_val is not None else "-")
            r_str = f"{r_val}%" if pct and r_val is not None else (str(r_val) if r_val is not None else "-")
            return f"{label}: {l_str} vs L | {r_str} vs R"

        lines = [
            _fmt("PA", "pa"),
            _fmt("AVG", "avg"),
            _fmt("xBA", "xba"),
            _fmt("xwOBA", "xwoba"),
            _fmt("K%", "k_pct", pct=True),
            _fmt("BB%", "bb_pct", pct=True),
            _fmt("Whiff%", "whiff_pct", pct=True),
        ]
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{start_date} to {end_date} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message(
            "✅ Channel saved -- automatic velocity/spin drop alerts will post here."
        )

    async def on_ready(self):
        log.info("Logged in as %s", self.user)
        if not poll_velocity_drops.is_running():
            poll_velocity_drops.start(self)


client = StatcastBot()

VELOCITY_POLL_SECONDS = int(os.getenv("VELOCITY_POLL_SECONDS", "120"))
# Cache each pitcher's 30-day baseline for this long so every poll cycle
# doesn't refetch it -- baseline barely moves start-to-start, and this is
# the same Savant CSV endpoint the manual /livevelo command hits.
BASELINE_CACHE_HOURS = float(os.getenv("BASELINE_CACHE_HOURS", "6"))
_baseline_cache: dict[int, dict] = {}  # pitcher_id -> {"data": ..., "fetched_at": ...}


async def _get_cached_baseline(pitcher_id: int):
    now = datetime.now(timezone.utc)
    cached = _baseline_cache.get(pitcher_id)
    if cached and (now - cached["fetched_at"]).total_seconds() < BASELINE_CACHE_HOURS * 3600:
        return cached["data"]

    baseline_start = et_date_str(-30)
    baseline_end = et_date_str(-1)
    rows = await asyncio.to_thread(statcast_api.fetch_statcast, pitcher_id, True, baseline_start, baseline_end)
    baseline = analysis.avg_metrics_by_pitch_type(rows) if rows else {}
    _baseline_cache[pitcher_id] = {"data": baseline, "fetched_at": now}
    return baseline


def build_autopost_embed(pitcher_name: str, team: str, drops: list[dict]) -> discord.Embed:
    embed = discord.Embed(
        title=f"⚠️ {pitcher_name} ({team}) — Velocity/Spin Drop Detected",
        color=discord.Color.orange(),
    )
    for d in drops:
        if d["metric"] == "velocity":
            embed.add_field(
                name=d["pitch_type"],
                value=f"Velo: {d['baseline']} → {d['live']} mph ({d['diff']:+.1f})",
                inline=False,
            )
        else:
            embed.add_field(
                name=d["pitch_type"],
                value=f"Spin: {d['baseline']} → {d['live']} rpm ({d['diff']:+.1f}%)",
                inline=False,
            )
    embed.set_footer(text="Live: MLB official feed • Baseline: last 30 days, Baseball Savant")
    return embed


@tasks.loop(seconds=VELOCITY_POLL_SECONDS)
async def poll_velocity_drops(bot: StatcastBot):
    try:
        await _poll_velocity_drops_body(bot)
    except Exception as e:
        log.error("poll_velocity_drops cycle failed unexpectedly, will retry next cycle: %s", e)


async def _poll_velocity_drops_body(bot: StatcastBot):
    channel_id = storage.get_config("announce_channel_id")
    if not channel_id:
        return
    channel = bot.get_channel(int(channel_id))
    if channel is None:
        return

    today = et_date_str(0)
    try:
        pitchers = await asyncio.to_thread(statcast_api.get_todays_probable_pitchers, today)
    except Exception as e:
        log.error("Failed to fetch today's probable pitchers: %s", e)
        return

    for p in pitchers:
        try:
            live = await asyncio.to_thread(statcast_api.get_live_pitch_metrics, p["game_pk"], p["id"])
        except Exception as e:
            log.error("Live pitch fetch failed for %s (game %s): %s", p["name"], p["game_pk"], e)
            continue
        if not live:
            continue  # hasn't thrown yet, or between innings with nothing new

        try:
            baseline = await _get_cached_baseline(p["id"])
        except Exception as e:
            log.error("Baseline fetch failed for %s: %s", p["name"], e)
            continue
        if not baseline:
            continue  # no recent-starts data to compare against (e.g. rookie call-up)

        drops = analysis.detect_velocity_drops(baseline, live)
        if not drops:
            continue

        # Post only NEW drops -- dedupe per (game, pitcher, pitch_type, metric)
        # so this alerts once per specific drop, not every poll cycle.
        new_drops = [
            d for d in drops
            if not storage.drop_already_alerted(p["game_pk"], p["id"], d["pitch_type"], d["metric"])
        ]
        if not new_drops:
            continue

        try:
            await channel.send(embed=build_autopost_embed(p["name"], p["team"], new_drops))
            for d in new_drops:
                storage.mark_drop_alerted(p["game_pk"], p["id"], d["pitch_type"], d["metric"])
            log.info("Auto-posted %d drop(s) for %s (game %s)", len(new_drops), p["name"], p["game_pk"])
        except Exception as e:
            log.error("Failed to send auto-post alert for %s: %s", p["name"], e)


@poll_velocity_drops.before_loop
async def before_poll_velocity_drops():
    await client.wait_until_ready()


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
