import os
import logging
import asyncio
from datetime import datetime, timedelta, timezone
from typing import Literal

import discord
from discord import app_commands
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


async def _resolve_and_fetch(interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
    """Shared resolution + fetch logic, returns (player, rows) or sends an error and returns None."""
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

        barrels_cmd = app_commands.Command(
            name="barrels",
            description="Quality of contact: exit velo, launch angle, hard-hit rate",
            callback=self._barrels_callback,
        )
        self.tree.add_command(barrels_cmd)

        luck_cmd = app_commands.Command(
            name="luck",
            description="Actual vs expected wOBA -- is this hitter over/underperforming?",
            callback=self._luck_callback,
        )
        self.tree.add_command(luck_cmd)

        velo_cmd = app_commands.Command(
            name="velo",
            description="Pitcher velocity trend by pitch type over a date range",
            callback=self._velo_callback,
        )
        self.tree.add_command(velo_cmd)

        livevelo_cmd = app_commands.Command(
            name="livevelo",
            description="Check if a pitcher's velocity is down live, right now, vs their recent baseline",
            callback=self._livevelo_callback,
        )
        self.tree.add_command(livevelo_cmd)

        pitchmix_cmd = app_commands.Command(
            name="pitchmix",
            description="A pitcher's pitch usage %, velocity, and whiff rate per pitch type",
            callback=self._pitchmix_callback,
        )
        self.tree.add_command(pitchmix_cmd)

        vspitch_cmd = app_commands.Command(
            name="vspitch",
            description="How a batter performs against one specific pitch type (e.g. sliders)",
            callback=self._vspitch_callback,
        )
        self.tree.add_command(vspitch_cmd)

        setchannel_cmd = app_commands.Command(
            name="setchannel",
            description="Set this channel (reserved for future automatic posts)",
            callback=self._setchannel_callback,
        )
        self.tree.add_command(setchannel_cmd)

        checkleaderboard_cmd = app_commands.Command(
            name="checkleaderboard",
            description="Debug: test Savant's percentile-rankings leaderboard CSV export",
            callback=self._checkleaderboard_callback,
        )
        self.tree.add_command(checkleaderboard_cmd)

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

    async def _barrels_callback(self, interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
        await interaction.response.defer()
        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        qoc = analysis.quality_of_contact(rows)
        if qoc["batted_balls"] == 0:
            await interaction.followup.send(f"{player['name']}: no batted-ball events found in that range.")
            return

        embed = discord.Embed(title=f"{player['name']} — Quality of Contact", color=discord.Color.blue())
        embed.add_field(name="Batted Balls", value=str(qoc["batted_balls"]), inline=True)
        embed.add_field(name="Avg Exit Velo", value=f"{qoc['avg_exit_velo']} mph", inline=True)
        embed.add_field(name="Avg Launch Angle", value=f"{qoc['avg_launch_angle']}°", inline=True)
        embed.add_field(name="Hard-Hit Rate (95+ mph)", value=f"{qoc['hard_hit_rate']}%", inline=True)
        embed.set_footer(text=f"{start_date} to {end_date} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _luck_callback(self, interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
        await interaction.response.defer()
        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        gap_result = analysis.expected_vs_actual(rows)
        if gap_result["gap"] is None:
            await interaction.followup.send(f"{player['name']}: not enough data to compute actual vs expected wOBA.")
            return

        gap = gap_result["gap"]
        if gap > 0.02:
            verdict = "🍀 Overperforming (getting a bit lucky)"
        elif gap < -0.02:
            verdict = "😤 Underperforming (due for positive regression)"
        else:
            verdict = "✅ Performing about as expected"

        embed = discord.Embed(title=f"{player['name']} — Actual vs Expected", color=discord.Color.gold())
        embed.add_field(name="Actual wOBA", value=str(gap_result["actual_woba"]), inline=True)
        embed.add_field(name="Expected wOBA (xwOBA)", value=str(gap_result["expected_woba"]), inline=True)
        embed.add_field(name="Gap", value=f"{gap:+.3f}", inline=True)
        embed.add_field(name="Read", value=verdict, inline=False)
        embed.set_footer(text=f"{start_date} to {end_date} • {gap_result['sample_size']} events • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

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
                    f"Movement (unverified units*): V {baseline_metrics['break_vert']}→{live_metrics['break_vert']}, "
                    f"H {baseline_metrics.get('break_horz', '-')}→{live_metrics.get('break_horz', '-')}"
                )

            embed.add_field(name=pt, value="\n".join(lines), inline=False)

        if drops:
            embed.description = f"⚠️ **{len(drops)} metric(s) showing a real drop from baseline**"
        else:
            embed.description = "✅ No significant drop detected"

        embed.set_footer(text="*Movement shown for reference only -- units not cross-verified between live feed and baseline yet. Live: MLB official feed • Baseline: last 30 days, Baseball Savant")
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

    async def _pitchmix_callback(self, interaction: discord.Interaction, player_name: str, start_date: str, end_date: str):
        await interaction.response.defer()
        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        mix = statcast_api.pitch_mix_breakdown(rows)
        if not mix:
            await interaction.followup.send(f"{player['name']}: no pitch data found in that range.")
            return

        embed = discord.Embed(title=f"{player['name']} — Pitch Mix", color=discord.Color.blue())
        for pt, data in mix.items():
            velo = f"{data['avg_velo']} mph" if "avg_velo" in data else "-"
            whiff = f"{data['whiff_pct']}% whiff" if "whiff_pct" in data else "no swings"
            embed.add_field(
                name=f"{pt} — {data['usage_pct']}% ({data['count']} thrown)",
                value=f"{velo} • {whiff}",
                inline=False,
            )
        embed.set_footer(text=f"{start_date} to {end_date} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _vspitch_callback(self, interaction: discord.Interaction, player_name: str, pitch_type: str, start_date: str, end_date: str):
        await interaction.response.defer()
        result = await _resolve_and_fetch(interaction, player_name, start_date, end_date)
        if result is None:
            return
        player, rows = result

        pt = pitch_type.upper()
        vs_result = statcast_api.vs_pitch_type_stats(rows, pt)
        if vs_result is None:
            await interaction.followup.send(f"{player['name']}: no pitches of type '{pt}' found in that range. Try FF, SI, SL, CH, CU, FC, ST, FS.")
            return

        embed = discord.Embed(title=f"{player['name']} vs {pt}", color=discord.Color.green())
        embed.add_field(name="Pitches seen", value=str(vs_result["pitches_seen"]), inline=True)
        if "avg" in vs_result:
            embed.add_field(name="AVG", value=str(vs_result["avg"]), inline=True)
        if "whiff_pct" in vs_result:
            embed.add_field(name="Whiff %", value=f"{vs_result['whiff_pct']}%", inline=True)
        embed.set_footer(text=f"{start_date} to {end_date} • Data: Baseball Savant")
        await interaction.followup.send(embed=embed)

    async def _setchannel_callback(self, interaction: discord.Interaction):
        storage.set_config("announce_channel_id", str(interaction.channel_id))
        await interaction.response.send_message("✅ Channel saved (reserved for future automatic posts).")

    async def _checkleaderboard_callback(self, interaction: discord.Interaction, player_type: str = "batter"):
        await interaction.response.defer()
        import csv
        import io

        try:
            text = await asyncio.to_thread(statcast_api.fetch_percentile_leaderboard, player_type, 2026)
        except Exception as e:
            await interaction.followup.send(f"Request failed: {type(e).__name__}: {e}"[:2000])
            return

        if text.startswith("\ufeff"):
            text = text[1:]

        try:
            reader = csv.DictReader(io.StringIO(text))
            rows = list(reader)
        except Exception as e:
            await interaction.followup.send(f"Couldn't parse as CSV: {e}\nRaw preview:\n```{text[:1000]}```"[:2000])
            return

        if not rows:
            await interaction.followup.send(f"Valid CSV format but 0 rows.\nRaw preview:\n```{text[:1200]}```"[:2000])
            return

        columns = list(rows[0].keys())
        msg = (
            f"**Percentile leaderboard test — SUCCESS**\n\n"
            f"Rows: {len(rows)}\n"
            f"Columns: {len(columns)}\n\n"
            f"ALL columns: {columns}\n\n"
            f"Sample full row (Soto if found, else first row): "
            f"{next((dict(r) for r in rows if 'Soto' in r.get('player_name', '')), dict(rows[0]))}"
        )
        await interaction.followup.send(msg[:2000])

    async def on_ready(self):
        log.info("Logged in as %s", self.user)


client = StatcastBot()

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Set DISCORD_TOKEN in your .env file.")
    client.run(TOKEN)
