#!/usr/bin/env python3
"""Generate and optionally submit weekly NFL pick'em selections.

The script pulls ESPN scoreboard/odds data for a given regular-season week,
skips Thursday games, ranks favorites by spread magnitude, and assigns
descending confidence points (default max 16). A combined Monday tie-breaker
is derived from the listed totals (rounded half-up), and an interactive CLI
allows quick tweaks before optionally submitting picks through headless
Selenium to FantasyTeamsNetwork.
"""
from __future__ import annotations

import argparse
import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from getpass import getpass
import logging
import os
import random
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from pathlib import Path
from zoneinfo import ZoneInfo

ET_TZ = ZoneInfo("America/New_York")
UTC_TZ = ZoneInfo("UTC")
SCOREBOARD_URL = "https://site.web.api.espn.com/apis/site/v2/sports/football/nfl/scoreboard"
ODDS_URL_TEMPLATE = (
    "https://sports.core.api.espn.com/v2/sports/football/leagues/nfl/"
    "events/{event_id}/competitions/{competition_id}/odds"
)
DEFAULT_MAX_POINTS = 16
LOGIN_URL = "https://fantasyteamsnetwork.com/play/login"
MAKE_WEEK_URL = "https://fantasyteamsnetwork.com/play/make_week"


def fallback_home_odds(home_team: Dict[str, Any], note: str) -> GameOdds:
    display_name = home_team.get("displayName") or home_team.get("name") or home_team.get("shortDisplayName")
    provider_label = f"{display_name or 'Home'} {note}"
    return GameOdds(
        spread=0.0,
        over_under=None,
        provider=provider_label,
        favorite_side="home",
    )


@dataclass
class GameOdds:
    spread: float
    over_under: Optional[float]
    provider: str
    favorite_side: str  # "home" or "away"


@dataclass
class Game:
    event_id: str
    competition_id: str
    start_utc: dt.datetime
    start_et: dt.datetime
    home: Dict[str, Any]
    away: Dict[str, Any]
    odds: GameOdds
    status: Dict[str, Any]

    @property
    def favorite(self) -> Dict[str, Any]:
        return self.home if self.odds.favorite_side == "home" else self.away

    @property
    def underdog(self) -> Dict[str, Any]:
        return self.away if self.odds.favorite_side == "home" else self.home

    @property
    def spread_value(self) -> float:
        """Return the favorite's spread as a negative number."""
        magnitude = abs(self.odds.spread)
        return -magnitude

    @property
    def spread_magnitude(self) -> float:
        return abs(self.odds.spread)

    @property
    def over_under(self) -> Optional[float]:
        return self.odds.over_under


@dataclass
class Pick:
    game: Game
    points: int
    selection: str = "favorite"  # "favorite" or "underdog"

    @property
    def selected_competitor(self) -> Dict[str, Any]:
        return self.game.favorite if self.selection == "favorite" else self.game.underdog

    @property
    def opponent_competitor(self) -> Dict[str, Any]:
        return self.game.underdog if self.selection == "favorite" else self.game.favorite

    @property
    def selected_team(self) -> Dict[str, Any]:
        return self.selected_competitor["team"]

    @property
    def opponent_team(self) -> Dict[str, Any]:
        return self.opponent_competitor["team"]

    @property
    def is_selected_home(self) -> bool:
        return self.selected_competitor is self.game.home

    def spread_label(self) -> str:
        spread = self.game.spread_magnitude
        if self.selection == "favorite":
            return f"-{spread:g}"
        return f"+{spread:g}"


@dataclass
class ExistingGamePick:
    visitor: str
    home: str
    selected: str
    points: Optional[int]

    def matchup_label(self) -> str:
        return f"{self.visitor} @ {self.home}"


@dataclass
class ExistingSubmission:
    picks: List[ExistingGamePick]
    tie_breaker: Optional[int]


def canonicalize_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def generate_team_aliases(team: Dict[str, Any]) -> List[str]:
    aliases: List[str] = []
    location = team.get("location", "")
    display = team.get("displayName", "")
    name = team.get("name", "")
    short = team.get("shortDisplayName", "")
    abbreviation = team.get("abbreviation", "")

    components = [
        location,
        display,
        name,
        short,
        f"{location} {name}".strip(),
        f"{display} {name}".strip(),
    ]

    for comp in components:
        if comp:
            aliases.append(canonicalize_label(comp))

    if abbreviation:
        aliases.append(canonicalize_label(abbreviation))
        if name:
            aliases.append(canonicalize_label(f"{abbreviation}{name}"))
            aliases.append(canonicalize_label(f"{abbreviation[:2]}{name}"))

    # Deduplicate while preserving order
    seen = set()
    result: List[str] = []
    for alias in aliases:
        if alias and alias not in seen:
            result.append(alias)
            seen.add(alias)
    return result


def aliases_from_label(label: str) -> set[str]:
    cleaned = label.replace("/", " ")
    parts = cleaned.split()
    combos = {canonicalize_label(cleaned)}
    for part in parts:
        combos.add(canonicalize_label(part))
    if len(parts) >= 2:
        combos.add(canonicalize_label(" ".join(parts[-2:])))
    return {alias for alias in combos if alias}


def normalize_team_name(name: str) -> str:
    return canonicalize_label(name)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate NFL pick'em selections")
    parser.add_argument("week", type=int, help="Regular-season week number (1-18)")
    parser.add_argument(
        "--season",
        type=int,
        default=2025,
        help="Season year (defaults to 2025 regular season)",
    )
    parser.add_argument(
        "--max-points",
        type=int,
        default=DEFAULT_MAX_POINTS,
        help="Highest confidence point value to assign (default: 16)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=None,
        help="Optional seed for tie-breaking randomization",
    )
    parser.add_argument(
        "--provider",
        default="ESPN BET",
        help="Preferred odds provider (fallbacks to first available)",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument(
        "--non-interactive",
        action="store_true",
        help="Skip interactive confirmation and editing prompts",
    )
    parser.add_argument(
        "--submit",
        action="store_true",
        help="Automatically submit picks via Selenium after confirmation",
    )
    parser.add_argument(
        "--compare-existing",
        action="store_true",
        help="Fetch current site picks and highlight differences",
    )
    parser.add_argument(
        "--login-id",
        help="FantasyTeamsNetwork user ID (falls back to FTN_USER_ID env var)",
    )
    parser.add_argument(
        "--login-password",
        help="FantasyTeamsNetwork password (falls back to FTN_PASSWORD env var)",
    )
    parser.add_argument(
        "--login-key",
        help="Optional FTN key (falls back to FTN_KEY env var)",
    )
    parser.add_argument(
        "--odds-provider",
        choices=["espn", "the-odds-api"],
        default="espn",
        help="Source for odds data (default: espn).",
    )
    parser.add_argument(
        "--odds-api-key",
        help="API key for The Odds API (falls back to ODDS_API_KEY env var)",
    )
    parser.add_argument(
        "--odds-bookmakers",
        help="Comma-separated bookmaker preference for The Odds API (default: fanduel,draftkings,betmgm)",
    )
    parser.add_argument(
        "--sbr-fallback-dir",
        default=None,
        help="Directory containing SBR fallback HTML files named sbr_week<week>.html (optional)",
    )
    parser.add_argument(
        "--selenium-browser",
        default="chrome",
        choices=["chrome", "firefox"],
        help="Browser to drive with Selenium (default: chrome)",
    )
    parser.add_argument(
        "--selenium-driver-path",
        help="Path to the WebDriver binary (chromedriver/geckodriver)",
    )
    parser.add_argument(
        "--selenium-no-headless",
        action="store_true",
        help="Run Selenium with a visible browser window",
    )
    parser.add_argument(
        "--selenium-pause-after",
        action="store_true",
        help="Keep the Selenium browser open after submission until you press Enter",
    )
    return parser.parse_args()


def infer_season_year(today: Optional[dt.date] = None) -> int:
    today = today or dt.datetime.now(tz=UTC_TZ).astimezone(ET_TZ).date()
    # Regular season runs Sep-Jan. For Jan/Feb prior to the new league year,
    # attribute the games to the previous calendar year season.
    if today.month < 3:
        return today.year - 1
    return today.year


def fetch_json(url: str, *, params: Optional[Dict[str, Any]] = None, session: requests.Session) -> Dict[str, Any]:
    resp = session.get(url, params=params, timeout=15)
    resp.raise_for_status()
    return resp.json()


def extract_odds(
    session: requests.Session,
    event_id: str,
    competition_id: str,
    preferred_provider: str,
) -> Optional[GameOdds]:
    odds_url = ODDS_URL_TEMPLATE.format(event_id=event_id, competition_id=competition_id)
    data = fetch_json(
        odds_url,
        params={"lang": "en", "region": "us"},
        session=session,
    )
    items: List[Dict[str, Any]] = data.get("items", [])
    if not items:
        return None

    def resolve_item(item: Dict[str, Any]) -> Dict[str, Any]:
        ref = item.get("$ref")
        if ref:
            return fetch_json(ref, session=session)
        return item

    resolved_items = [resolve_item(item) for item in items]

    def pick_item() -> Optional[Dict[str, Any]]:
        for item in resolved_items:
            provider_name = item.get("provider", {}).get("name")
            if (
                provider_name
                and provider_name.lower() == preferred_provider.lower()
                and item.get("spread") is not None
            ):
                return item
        for item in resolved_items:
            if item.get("spread") is not None:
                return item
        return None

    selected = pick_item()
    if not selected:
        return None

    spread = selected.get("spread")
    over_under = selected.get("overUnder")
    provider_name = selected.get("provider", {}).get("name", "Unknown")
    favorite_side = "home"

    if isinstance(spread, (int, float)):
        if spread > 0:
            favorite_side = "away"
        elif spread < 0:
            favorite_side = "home"
        else:
            # fall back to explicit flags if spread == 0
            home_flag = selected.get("homeTeamOdds", {}).get("favorite")
            away_flag = selected.get("awayTeamOdds", {}).get("favorite")
            if home_flag and not away_flag:
                favorite_side = "home"
            elif away_flag and not home_flag:
                favorite_side = "away"
            else:
                favorite_side = "home"
    else:
        return None

    return GameOdds(
        spread=float(spread),
        over_under=float(over_under) if isinstance(over_under, (int, float)) else None,
        provider=provider_name,
        favorite_side=favorite_side,
    )


def parse_games(
    scoreboard: Dict[str, Any],
    *,
    session: requests.Session,
    preferred_provider: str,
    odds_source: str,
    week: int,
    odds_lookup: Optional[Dict[Tuple[str, str], GameOdds]] = None,
) -> List[Game]:
    games: List[Game] = []
    for event in scoreboard.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        comp = competitions[0]
        status = comp.get("status", {})
        date_str = comp.get("date") or event.get("date")
        if not date_str:
            continue

        start_utc = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(UTC_TZ)
        start_et = start_utc.astimezone(ET_TZ)

        if start_et.weekday() == 3:  # Thursday
            continue

        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue

        if odds_source == "the-odds-api":
            display_home = home["team"].get("displayName", "")
            display_away = away["team"].get("displayName", "")
            home_norm = normalize_team_name(display_home)
            away_norm = normalize_team_name(display_away)
            odds = odds_lookup.get((home_norm, away_norm)) if odds_lookup else None
            if not odds and odds_lookup:
                odds = odds_lookup.get((away_norm, home_norm))
                if odds:
                    odds = GameOdds(
                        spread=float(-odds.spread),
                        over_under=odds.over_under,
                        provider=odds.provider,
                        favorite_side="away" if odds.favorite_side == "home" else "home",
                    )
            if not odds:
                logging.warning(
                    "Missing odds for %s; defaulting to home team. Provide data/sbr_week%d.html to override.",
                    event.get("shortName"),
                    week,
                )
                odds = fallback_home_odds(
                    home["team"],
                    note="(assumed favourite due to missing odds)",
                )
        else:
            odds = extract_odds(
                session,
                event_id=event.get("id"),
                competition_id=comp.get("id"),
                preferred_provider=preferred_provider,
            )
            if not odds:
                logging.warning("Skipping %s due to missing odds", event.get("shortName"))
                continue

        if odds.favorite_side not in {"home", "away"}:
            logging.warning("Skipping %s due to unrecognized favorite side", event.get("shortName"))
            continue

        games.append(
            Game(
                event_id=event.get("id"),
                competition_id=comp.get("id"),
                start_utc=start_utc,
                start_et=start_et,
                home=home,
                away=away,
                odds=odds,
                status=status,
            )
        )

    return games


def assign_points(games: List[Game], max_points: int, seed: Optional[int]) -> List[Pick]:
    if seed is not None:
        random.seed(seed)
    else:
        random.seed()

    shuffled = games[:]
    random.shuffle(shuffled)
    sorted_games = sorted(
        shuffled,
        key=lambda g: (
            g.spread_magnitude,
            1 if g.favorite is g.home else 0,
        ),
        reverse=True,
    )

    picks: List[Pick] = []
    for idx, game in enumerate(sorted_games):
        points = max_points - idx
        if points <= 0:
            break
        picks.append(Pick(game=game, points=points, selection="favorite"))
    return picks


def interactive_adjustments(picks: List[Pick], monday_summary: MondaySummary) -> Tuple[List[Pick], Optional[int]]:
    if not picks:
        return picks, None

    tie_breaker_override: Optional[int] = None

    while True:
        response = input("Would you like to edit any picks? [y/N]: ").strip().lower()
        if response in {"", "n", "no"}:
            break
        if response not in {"y", "yes"}:
            print("Please answer 'y' or 'n'.")
            continue

        while True:
            print("\nCurrent picks:")
            picks.sort(key=lambda p: p.points, reverse=True)
            print(render_pick_table(picks))
            choice = input("Enter the game index to edit (or press Enter to finish editing): ").strip()
            if choice == "":
                break
            if not choice.isdigit():
                print("Please enter a valid number.")
                continue
            idx = int(choice)
            if not (1 <= idx <= len(picks)):
                print(f"Please choose a number between 1 and {len(picks)}.")
                continue

            pick = picks[idx - 1]
            game = pick.game
            favorite_team = game.favorite["team"]
            underdog_team = game.underdog["team"]

            print(
                f"Selected: {favorite_team.get('displayName')} vs {underdog_team.get('displayName')}"
            )
            team_prompt = (
                "Choose team [1] Favorite "
                f"({favorite_team.get('displayName')}) or [2] Underdog "
                f"({underdog_team.get('displayName')}) (Enter to keep {pick.selection}): "
            )
            team_choice = input(team_prompt).strip()
            if team_choice == "1":
                pick.selection = "favorite"
            elif team_choice == "2":
                pick.selection = "underdog"

            while True:
                points_input = input(
                    f"Assign confidence points (current {pick.points}). Press Enter to keep: "
                ).strip()
                if points_input == "":
                    break
                try:
                    new_points = int(points_input)
                except ValueError:
                    print("Please enter a whole number.")
                    continue
                if new_points <= 0:
                    print("Points must be a positive integer.")
                    continue
                pick.points = new_points
                break

            picks.sort(key=lambda p: p.points, reverse=True)
            print("Updated pick saved.\n")

    # Allow manual adjustment of Monday tie-breaker pick
    if monday_summary.games and not monday_summary.missing_totals:
        default_pick = monday_summary.computed_pick
        prompt = (
            "Enter a custom Monday tie-breaker total (press Enter to keep "
            f"{default_pick}): "
        )
    elif monday_summary.games:
        prompt = (
            "Enter a Monday tie-breaker total (totals unavailable from ESPN; press Enter to skip): "
        )
    else:
        prompt = ""

    if prompt:
        while True:
            tb_input = input(prompt).strip()
            if tb_input == "" or tb_input.lower() in {"n", "no"}:
                break
            try:
                tie_breaker_override = int(tb_input)
                break
            except ValueError:
                print("Please enter a whole number.")

    picks.sort(key=lambda p: p.points, reverse=True)
    return picks, tie_breaker_override


def round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("0"), rounding=ROUND_HALF_UP))


def format_matchup(game: Game, favorite_first: bool = True) -> str:
    home_team = game.home["team"]
    away_team = game.away["team"]
    home_abbr = home_team.get("abbreviation")
    away_abbr = away_team.get("abbreviation")

    if favorite_first:
        favorite = game.favorite
        underdog = game.underdog
        favorite_team = favorite["team"]
        underdog_team = underdog["team"]
        is_favorite_home = favorite is game.home
        verb = "vs" if is_favorite_home else "@"
        return (
            f"{favorite_team.get('displayName')} ({favorite_team.get('abbreviation')}) "
            f"{verb} {underdog_team.get('displayName')} ({underdog_team.get('abbreviation')})"
        )
    return f"{away_team.get('displayName')} ({away_abbr}) @ {home_team.get('displayName')} ({home_abbr})"

@dataclass
class MondaySummary:
    games: List[Game]
    combined_total: Optional[float]
    computed_pick: Optional[int]
    missing_totals: bool


def render_pick_table(picks: List[Pick]) -> str:
    if not picks:
        return "No games available after filtering."

    header = f"{'Idx':>3}  {'Pts':>3}  {'Pick (spread)':<40}  {'Opponent':<30}  {'Kickoff (ET)':<18}  {'O/U':>5}  Provider"
    lines = [header, "-" * len(header)]

    for idx, pick in enumerate(picks, start=1):
        game = pick.game
        chosen = pick.selected_team
        opponent = pick.opponent_team
        kickoff_str = game.start_et.strftime("%a %m/%d %I:%M %p")
        ou = game.over_under
        ou_str = f"{ou:g}" if ou is not None else "--"
        verb = "vs" if pick.is_selected_home else "@"
        selection_label = (
            f"{chosen.get('displayName')} ({chosen.get('abbreviation')}) {pick.spread_label()}"
        )
        opponent_label = f"{verb} {opponent.get('displayName')} ({opponent.get('abbreviation')})"
        lines.append(
            f"{idx:>3}  {pick.points:>3}  {selection_label:<40}  {opponent_label:<30}  {kickoff_str:<18}  {ou_str:>5}  {game.odds.provider}"
        )

    return "\n".join(lines)


def build_monday_summary(picks: List[Pick]) -> MondaySummary:
    monday_games = [pick.game for pick in picks if pick.game.start_et.weekday() == 0]
    if not monday_games:
        return MondaySummary(games=[], combined_total=None, computed_pick=None, missing_totals=True)

    totals = [g.over_under for g in monday_games]
    if any(total is None for total in totals):
        return MondaySummary(games=monday_games, combined_total=None, computed_pick=None, missing_totals=True)

    combined_total = sum(total for total in totals if total is not None)
    computed_pick = round_half_up(combined_total)
    return MondaySummary(
        games=monday_games,
        combined_total=combined_total,
        computed_pick=computed_pick,
        missing_totals=False,
    )


def collect_scoreboard_events(scoreboard: Dict[str, Any]) -> List[Dict[str, Any]]:
    events_info: List[Dict[str, Any]] = []
    for event in scoreboard.get("events", []):
        competitions = event.get("competitions") or []
        if not competitions:
            continue
        comp = competitions[0]
        date_str = comp.get("date") or event.get("date")
        if not date_str:
            continue
        start_utc = dt.datetime.fromisoformat(date_str.replace("Z", "+00:00")).astimezone(UTC_TZ)
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away:
            continue
        events_info.append(
            {
                "event": event,
                "competition": comp,
                "start_utc": start_utc,
                "home": home,
                "away": away,
            }
        )
    return events_info


def load_sbr_fallback(
    events_info: List[Dict[str, Any]],
    *,
    week: int,
    fallback_dir: Optional[str],
) -> Dict[Tuple[str, str], GameOdds]:
    if not fallback_dir:
        return {}

    path = Path(fallback_dir) / f"sbr_week{week}.html"
    if not path.exists():
        logging.debug("SBR fallback file not found: %s", path)
        return {}

    try:
        html = path.read_text(encoding="utf-8")
    except Exception as exc:  # pylint: disable=broad-except
        logging.warning("Unable to read SBR fallback file %s: %s", path, exc)
        return {}

    soup = BeautifulSoup(html, "html.parser")
    table = None
    for candidate in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in candidate.find_all("th")]
        if headers and "game" in headers[0] and any("spread" in h for h in headers):
            table = candidate
            break

    if table is None:
        logging.warning("No odds table found in SBR fallback file %s", path)
        return {}

    tbody = table.find("tbody")
    if tbody is None:
        logging.warning("SBR fallback table missing tbody in %s", path)
        return {}

    scoreboard_entries: List[Dict[str, Any]] = []
    for info in events_info:
        home_team = info["home"]["team"]
        away_team = info["away"]["team"]
        home_aliases = set(generate_team_aliases(home_team)) | aliases_from_label(home_team.get("displayName", ""))
        away_aliases = set(generate_team_aliases(away_team)) | aliases_from_label(away_team.get("displayName", ""))
        scoreboard_entries.append(
            {
                "key": (
                    normalize_team_name(home_team.get("displayName", "")),
                    normalize_team_name(away_team.get("displayName", "")),
                ),
                "home_aliases": home_aliases,
                "away_aliases": away_aliases,
            }
        )

    fallback_lookup: Dict[Tuple[str, str], GameOdds] = {}

    rows = tbody.find_all("tr")
    for row in rows:
        cols = row.find_all("td")
        if len(cols) < 4:
            continue

        game_text = cols[0].get_text(" ", strip=True)
        spread_text = cols[1].get_text(" ", strip=True)
        total_text = cols[3].get_text(" ", strip=True)

        game_core = re.sub(r"\(.*?\)", "", game_text).strip()
        parts = re.split(r"\s+vs\.?\s+", game_core, maxsplit=1)
        if len(parts) != 2:
            continue
        away_name_raw, home_name_raw = parts[0].strip(), parts[1].strip()

        away_aliases_sbr = aliases_from_label(away_name_raw)
        home_aliases_sbr = aliases_from_label(home_name_raw)

        matched_entry = None
        swapped = False
        for entry in scoreboard_entries:
            if away_aliases_sbr & entry["away_aliases"] and home_aliases_sbr & entry["home_aliases"]:
                matched_entry = entry
                break
            if away_aliases_sbr & entry["home_aliases"] and home_aliases_sbr & entry["away_aliases"]:
                matched_entry = entry
                swapped = True
                break

        if not matched_entry:
            continue

        home_aliases = matched_entry["home_aliases"]
        away_aliases = matched_entry["away_aliases"]
        home_norm, away_norm = matched_entry["key"]

        if swapped:
            home_aliases, away_aliases = away_aliases, home_aliases
            home_norm, away_norm = away_norm, home_norm

        spread_match = re.match(r"([A-Za-z .]+)\s+([+-]?\d+(?:\.\d+)?|PK)", spread_text.replace("½", ".5"))
        if not spread_match:
            continue

        spread_team_text = spread_match.group(1).strip()
        spread_value_raw = spread_match.group(2).strip()
        if spread_value_raw.upper() == "PK":
            spread_value = 0.0
        else:
            spread_value = float(spread_value_raw)

        spread_aliases = aliases_from_label(spread_team_text)
        if spread_aliases & home_aliases:
            team_is_home = True
        elif spread_aliases & away_aliases:
            team_is_home = False
        else:
            continue

        if spread_value < 0:
            favorite_side = "home" if team_is_home else "away"
            spread = float(spread_value)
        elif spread_value > 0:
            favorite_side = "away" if team_is_home else "home"
            spread = -float(spread_value)
        else:
            favorite_side = "home" if team_is_home else "away"
            spread = 0.0

        total_search = re.search(r"\d+(?:\.\d+)?", total_text.replace("½", ".5"))
        total_value = float(total_search.group(0)) if total_search else None

        fallback_lookup[(home_norm, away_norm)] = GameOdds(
            spread=spread,
            over_under=total_value,
            provider="bet365 via SportsbookReview",
            favorite_side=favorite_side,
        )

    if fallback_lookup:
        logging.info(
            "Loaded fallback odds for %d games from %s",
            len(fallback_lookup),
            path,
        )

    return fallback_lookup


def build_the_odds_api_lookup(
    events_info: List[Dict[str, Any]],
    *,
    session: requests.Session,
    api_key: str,
    bookmakers: List[str],
    week: int,
    fallback_dir: Optional[str],
    region: str = "us",
    markets: str = "spreads,totals",
) -> Dict[Tuple[str, str], GameOdds]:
    if not events_info:
        return {}

    start_times = [entry["start_utc"] for entry in events_info]
    min_time = min(start_times) - dt.timedelta(days=2)
    max_time = max(start_times) + dt.timedelta(days=2)

    def fmt(ts: dt.datetime) -> str:
        return ts.astimezone(UTC_TZ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    params = {
        "apiKey": api_key,
        "regions": region,
        "markets": markets,
        "oddsFormat": "american",
        "dateFormat": "iso",
        "commenceTimeFrom": fmt(min_time),
        "commenceTimeTo": fmt(max_time),
    }
    if bookmakers:
        params["bookmakers"] = ",".join(bookmakers)

    url = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl/odds/"
    response = session.get(url, params=params, timeout=20)
    response.raise_for_status()
    data = response.json()

    api_events: List[Dict[str, Any]] = []
    for raw_event in data:
        home_name = raw_event.get("home_team")
        away_name = raw_event.get("away_team")
        if not home_name or not away_name:
            continue
        api_events.append(
            {
                "raw": raw_event,
                "home_aliases": aliases_from_label(home_name),
                "away_aliases": aliases_from_label(away_name),
                "home_name": home_name,
                "away_name": away_name,
            }
        )

    lookup: Dict[Tuple[str, str], GameOdds] = {}

    for info in events_info:
        home_team = info["home"]["team"]
        away_team = info["away"]["team"]
        home_aliases = set(generate_team_aliases(home_team)) | aliases_from_label(home_team.get("displayName", ""))
        away_aliases = set(generate_team_aliases(away_team)) | aliases_from_label(away_team.get("displayName", ""))

        matched_entry: Optional[Dict[str, Any]] = None
        swapped = False
        for event_entry in api_events:
            home_set = event_entry["home_aliases"]
            away_set = event_entry["away_aliases"]
            if home_aliases & home_set and away_aliases & away_set:
                matched_entry = event_entry
                break
            if home_aliases & away_set and away_aliases & home_set:
                matched_entry = event_entry
                swapped = True
                break

        if not matched_entry:
            for event_entry in api_events:
                home_set = event_entry["home_aliases"]
                if home_aliases & home_set:
                    matched_entry = event_entry
                    logging.warning(
                        "Using home-team fallback odds for %s (away team mismatch)",
                        info["event"].get("shortName", "unknown"),
                    )
                    break

        if not matched_entry:
            continue

        api_events.remove(matched_entry)
        raw_event = matched_entry["raw"]
        if swapped:
            home_aliases, away_aliases = away_aliases, home_aliases
        bookmakers_data = raw_event.get("bookmakers") or []

        selected_bookmaker = None
        if bookmakers:
            pref_lower = [b.lower() for b in bookmakers]
            for pref in pref_lower:
                match = next((b for b in bookmakers_data if b.get("key", "").lower() == pref), None)
                if match:
                    selected_bookmaker = match
                    break
        if not selected_bookmaker and bookmakers_data:
            selected_bookmaker = bookmakers_data[0]
        if not selected_bookmaker:
            continue

        markets_data = selected_bookmaker.get("markets") or []
        spreads_market = next((m for m in markets_data if m.get("key") == "spreads"), None)
        totals_market = next((m for m in markets_data if m.get("key") == "totals"), None)

        favorite_side = None
        spread_value = None
        if spreads_market:
            for outcome in spreads_market.get("outcomes", []):
                point = outcome.get("point")
                name = outcome.get("name")
                if point is None or not name:
                    continue
                name_aliases = aliases_from_label(name)
                if name_aliases & home_aliases:
                    if point < 0:
                        favorite_side = "home"
                        spread_value = float(point)
                    elif point > 0 and favorite_side is None:
                        favorite_side = "away"
                        spread_value = -float(point)
                elif name_aliases & away_aliases:
                    if point < 0:
                        favorite_side = "away"
                        spread_value = float(point)
                    elif point > 0 and favorite_side is None:
                        favorite_side = "home"
                        spread_value = -float(point)
        if favorite_side is None or spread_value is None:
            continue

        over_under_value: Optional[float] = None
        if totals_market:
            over_outcome = next(
                (
                    outcome
                    for outcome in totals_market.get("outcomes", [])
                    if outcome.get("name", "").lower() == "over"
                ),
                None,
            )
            if over_outcome and over_outcome.get("point") is not None:
                over_under_value = float(over_outcome["point"])

        home_norm = normalize_team_name(home_team.get("displayName", ""))
        away_norm = normalize_team_name(away_team.get("displayName", ""))

        bookmaker_name = selected_bookmaker.get("title") or selected_bookmaker.get("key", "The Odds API")
        provider_label = f"{bookmaker_name} via The Odds API"

        lookup[(home_norm, away_norm)] = GameOdds(
            spread=float(spread_value),
            over_under=over_under_value,
            provider=provider_label,
            favorite_side=favorite_side,
        )

    sbr_lookup = load_sbr_fallback(
        events_info,
        week=week,
        fallback_dir=fallback_dir,
    )
    for key, odds in sbr_lookup.items():
        if key not in lookup or lookup[key].spread == 0.0:
            lookup[key] = odds
        else:
            existing = lookup[key]
            if existing.over_under is None and odds.over_under is not None:
                lookup[key] = GameOdds(
                    spread=existing.spread,
                    over_under=odds.over_under,
                    provider=existing.provider,
                    favorite_side=existing.favorite_side,
                )

    return lookup
def format_monday_summary(summary: MondaySummary, override_pick: Optional[int] = None) -> str:
    if not summary.games:
        return "No Monday game found for tie-breaker."
    if summary.missing_totals:
        return "Tie-breaker: At least one Monday game is missing a listed total; please check manually."

    games_sorted = sorted(summary.games, key=lambda g: g.start_et)
    details = []
    for game in games_sorted:
        matchup = format_matchup(game, favorite_first=False)
        kickoff = game.start_et.strftime("%a %m/%d %I:%M %p")
        details.append(f"{matchup} (O/U {game.over_under:g}, {kickoff})")

    combined_display = f"{summary.combined_total:g}" if summary.combined_total is not None else "--"
    final_pick = override_pick if override_pick is not None else summary.computed_pick
    pick_display = str(final_pick) if final_pick is not None else "--"
    joined = " | ".join(details)
    return f"Tie-breaker (Monday): {joined} | Combined O/U {combined_display} | Total pick {pick_display}"


def parse_existing_picks_html(html: str) -> ExistingSubmission:
    soup = BeautifulSoup(html, "html.parser")
    results: Dict[Tuple[str, str], ExistingGamePick] = {}
    tie_breaker_value: Optional[int] = None

    for row in soup.find_all("tr"):
        radios = row.find_all("input", attrs={"type": "radio"})
        if len(radios) < 2:
            continue

        def extract_team(radio_tag) -> str:
            cell = radio_tag.find_parent("td")
            if not cell:
                return ""
            row = cell.find_parent("tr")
            if not row:
                return ""

            cells = row.find_all("td")
            try:
                idx = cells.index(cell)
            except ValueError:
                return ""

            def scan(indices: range, require_lineitem: bool) -> Optional[str]:
                for pos in indices:
                    if pos < 0 or pos >= len(cells):
                        continue
                    candidate = cells[pos]
                    text = candidate.get_text(strip=True)
                    if not text:
                        continue
                    classes = candidate.get("class") or []
                    if require_lineitem and "lineitem" not in classes:
                        continue
                    return text
                return None

            # Prefer cells explicitly marked as line items (team names).
            name = scan(range(idx + 1, len(cells)), require_lineitem=True)
            if not name:
                name = scan(range(idx - 1, -1, -1), require_lineitem=True)
            if not name:
                name = scan(range(idx + 1, len(cells)), require_lineitem=False)
            if not name:
                name = scan(range(idx - 1, -1, -1), require_lineitem=False)
            return name or ""

        visitor_radio = radios[0]
        home_radio = radios[1]
        visitor_name = extract_team(visitor_radio)
        home_name = extract_team(home_radio)
        if not visitor_name or not home_name:
            continue

        selected = visitor_name if visitor_radio.has_attr("checked") else home_name if home_radio.has_attr("checked") else ""
        if not selected:
            continue

        points_value: Optional[int] = None
        points_input = row.find(
            "input",
            attrs={
                "name": re.compile(r"(pt|point)", re.IGNORECASE),
            },
        )
        if points_input:
            value = points_input.get("value")
            if value and value.strip().isdigit():
                points_value = int(value.strip())

        key = (canonicalize_label(visitor_name), canonicalize_label(home_name))
        results[key] = ExistingGamePick(
            visitor=visitor_name,
            home=home_name,
            selected=selected,
            points=points_value,
        )

    # Attempt to find tie-breaker input values
    tie_inputs = soup.find_all(
        "input",
        attrs={
            "name": re.compile(r"(tie|tb|mnf)", re.IGNORECASE),
        },
    )
    for tie_input in tie_inputs:
        value = tie_input.get("value")
        if value and value.strip().isdigit():
            tie_breaker_value = int(value.strip())
            break

    if tie_breaker_value is None:
        tie_cell = soup.find(
            lambda tag: tag.name == "td"
            and tag.get_text(strip=True).lower().startswith("monday")
        )
        if tie_cell:
            # Look for next cell with digits
            next_td = tie_cell.find_next("td")
            if next_td:
                digits = re.findall(r"\d+", next_td.get_text())
                if digits:
                    tie_breaker_value = int(digits[0])

    return ExistingSubmission(picks=list(results.values()), tie_breaker=tie_breaker_value)


def fetch_existing_submission(
    *,
    session: requests.Session,
    week: int,
    login_id: Optional[str],
    login_key: Optional[str],
) -> ExistingSubmission:
    params = {"week": week}
    if login_id:
        params["i"] = login_id
    if login_key:
        params["k"] = login_key
    response = session.get(MAKE_WEEK_URL, params=params, timeout=15)
    response.raise_for_status()
    return parse_existing_picks_html(response.text)


def summarize_existing_comparison(
    picks: List[Pick],
    existing_submission: ExistingSubmission,
    *,
    monday_summary: MondaySummary,
) -> str:
    existing = existing_submission.picks
    if not existing:
        tie_note = ""
        if existing_submission.tie_breaker is not None:
            tie_note = f" (tie-breaker total {existing_submission.tie_breaker})"
        return f"Existing comparison: no current picks found on the site{tie_note}."

    diffs: List[str] = []

    # Precompute alias sets for each script game
    script_entries: List[Tuple[Pick, set, set]] = []
    for pick in picks:
        home_team = pick.game.home["team"]
        away_team = pick.game.away["team"]
        home_aliases = set(generate_team_aliases(home_team))
        away_aliases = set(generate_team_aliases(away_team))
        script_entries.append((pick, home_aliases, away_aliases))

    matched_ids: set[int] = set()

    def normalize(value: str) -> str:
        return canonicalize_label(value)

    for existing_pick in existing:
        visitor_norm = normalize(existing_pick.visitor)
        home_norm = normalize(existing_pick.home)

        matched_entry: Optional[Tuple[Pick, set, set]] = None
        for entry in script_entries:
            pick, home_aliases, away_aliases = entry
            if visitor_norm in away_aliases and home_norm in home_aliases:
                matched_entry = entry
                break
            if visitor_norm in home_aliases and home_norm in away_aliases:
                matched_entry = entry
                break

        if not matched_entry:
            diffs.append(
                f"- {existing_pick.matchup_label()}: site has {existing_pick.selected} (pts {existing_pick.points or '--'}), not found in computed slate."
            )
            continue

        pick, home_aliases, away_aliases = matched_entry
        matched_ids.add(id(pick))

        script_team = pick.selected_team["displayName"]
        existing_team = existing_pick.selected
        script_points = pick.points
        existing_points = existing_pick.points

        existing_aliases = aliases_from_label(existing_team)

        site_selects_home = bool(existing_aliases & home_aliases)
        site_selects_away = bool(existing_aliases & away_aliases)

        if site_selects_home and site_selects_away:
            diffs.append(
                f"- {existing_pick.matchup_label()}: site pick '{existing_team}' could match either team; unable to compare."
            )
            continue
        if not site_selects_home and not site_selects_away:
            diffs.append(
                f"- {existing_pick.matchup_label()}: site pick '{existing_team}' did not match the home or away team."
            )
            continue

        script_is_home = pick.is_selected_home
        if (site_selects_home and not script_is_home) or (site_selects_away and script_is_home):
            diffs.append(
                f"- {existing_pick.matchup_label()}: site has {existing_team} (pts {existing_points or '--'}), script prefers {script_team} (pts {script_points})."
            )
        else:
            points_mismatch = False
            if existing_points is None and script_points is not None:
                points_mismatch = True
            elif existing_points is not None and script_points is None:
                points_mismatch = True
            elif existing_points is not None and script_points is not None and existing_points != script_points:
                points_mismatch = True

            if points_mismatch:
                diffs.append(
                    f"- {existing_pick.matchup_label()}: same winner {existing_team}, but site points {existing_points or '--'} vs script {script_points or '--'}."
                )

    # Any script picks unmatched on the site
    for pick, _, _ in script_entries:
        if id(pick) not in matched_ids:
            game = pick.game
            matchup = f"{game.away['team']['displayName']} @ {game.home['team']['displayName']}"
            diffs.append(
                f"- {matchup}: script selects {pick.selected_team['displayName']} (pts {pick.points}) but no site pick detected."
            )

    script_tie = monday_summary.computed_pick
    existing_tie = existing_submission.tie_breaker
    if script_tie is not None and existing_tie is not None and script_tie != existing_tie:
        diffs.append("Tie-breaker:")
        diffs.append(
            f"- Site total {existing_tie}, computed total {script_tie}."
        )
    
    if not diffs:
        return "Existing comparison: site picks already match the computed selections."

    return "Existing comparison:\n" + "\n".join(diffs)


def submit_picks_via_selenium(
    picks: List[Pick],
    tie_breaker_value: Optional[int],
    *,
    week: int,
    login_id: str,
    password: str,
    login_key: Optional[str],
    browser: str,
    driver_path: Optional[str],
    headless: bool,
    pause_after: bool,
) -> None:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import NoSuchElementException, TimeoutException
        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import Select, WebDriverWait
    except ImportError as exc:
        raise RuntimeError(
            "Selenium is required to submit picks. Install it via `pip install selenium`."
        ) from exc

    browser = browser.lower()
    driver = None

    def build_driver() -> webdriver.Remote:
        nonlocal driver
        if browser == "chrome":
            try:
                from selenium.webdriver.chrome.options import Options as ChromeOptions
                from selenium.webdriver.chrome.service import Service as ChromeService
            except ImportError as exc:
                raise RuntimeError("Selenium Chrome bindings are unavailable.") from exc

            options = ChromeOptions()
            if headless:
                options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--window-size=1920,1080")

            if driver_path:
                service = ChromeService(executable_path=driver_path)
            else:
                try:
                    from webdriver_manager.chrome import ChromeDriverManager
                except ImportError as exc:
                    raise RuntimeError(
                        "webdriver_manager is required when no ChromeDriver path is provided. "
                        "Install it via `pip install webdriver-manager` or pass --selenium-driver-path."
                    ) from exc
                service = ChromeService(ChromeDriverManager().install())

            driver = webdriver.Chrome(service=service, options=options)
            return driver

        if browser == "firefox":
            try:
                from selenium.webdriver.firefox.options import Options as FirefoxOptions
                from selenium.webdriver.firefox.service import Service as FirefoxService
            except ImportError as exc:
                raise RuntimeError("Selenium Firefox bindings are unavailable.") from exc

            options = FirefoxOptions()
            options.headless = headless

            if driver_path:
                service = FirefoxService(executable_path=driver_path)
            else:
                try:
                    from webdriver_manager.firefox import GeckoDriverManager
                except ImportError as exc:
                    raise RuntimeError(
                        "webdriver_manager is required when no GeckoDriver path is provided. "
                        "Install it via `pip install webdriver-manager` or pass --selenium-driver-path."
                    ) from exc
                service = FirefoxService(GeckoDriverManager().install())

            driver = webdriver.Firefox(service=service, options=options)
            return driver

        raise RuntimeError(f"Unsupported Selenium browser '{browser}'. Choose 'chrome' or 'firefox'.")

    driver = build_driver()
    wait = WebDriverWait(driver, 20)

    try:
        driver.get(LOGIN_URL)
        wait.until(EC.presence_of_element_located((By.NAME, "user_id")))

        user_input = driver.find_element(By.NAME, "user_id")
        user_input.clear()
        user_input.send_keys(login_id)

        password_input = driver.find_element(By.NAME, "p")
        password_input.clear()
        password_input.send_keys(password)
        password_input.send_keys(Keys.RETURN)

        # Wait for navigation or detect login error.
        wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        page_source = driver.page_source.lower()
        if "try again" in page_source and "user id" in page_source:
            raise RuntimeError("Login failed; please verify the provided credentials.")

        params = {"week": week}
        if login_id:
            params["i"] = login_id
        if login_key:
            params["k"] = login_key
        target_url = f"{MAKE_WEEK_URL}?{urlencode(params)}"
        driver.get(target_url)

        wait.until(EC.presence_of_element_located((By.TAG_NAME, "table")))

        rows = driver.find_elements(By.XPATH, "//tr[td/input[@type='radio']]")
        games = []
        for row in rows:
            radios = row.find_elements(By.XPATH, ".//input[@type='radio']")
            if len(radios) < 2:
                continue

            tds = row.find_elements(By.TAG_NAME, "td")
            if len(tds) < 6:
                continue

            visitor_name = tds[2].text.strip()
            home_name = tds[5].text.strip()
            teams = []
            if visitor_name:
                teams.append({"name": visitor_name, "radio": radios[0]})
            if home_name:
                teams.append({"name": home_name, "radio": radios[1]})

            if teams:
                games.append({"row": row, "teams": teams})

        if not games:
            raise RuntimeError("Unable to locate pick rows on the make_week page.")

        radio_lookup: Dict[str, List[Tuple[Dict[str, Any], Dict[str, Any]]]] = {}
        for game in games:
            for team in game["teams"]:
                aliases = aliases_from_label(team["name"]) | {canonicalize_label(team["name"]) }
                for key in aliases:
                    if key:
                        radio_lookup.setdefault(key, []).append((game, team))

        def select_team(pick: Pick) -> None:
            aliases = generate_team_aliases(pick.selected_team)
            match: Optional[Tuple[Dict[str, Any], Dict[str, Any]]] = None
            for alias in aliases:
                options = radio_lookup.get(alias)
                if options:
                    match = options.pop(0)
                    if not options:
                        radio_lookup.pop(alias, None)
                    break
            if not match:
                raise RuntimeError(
                    f"Could not find a radio button for team '{pick.selected_team.get('displayName')}'."
                )

            game_entry, team_entry = match
            radio_element = team_entry["radio"]
            driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", radio_element)
            if not radio_element.is_selected():
                radio_element.click()

            row = game_entry["row"]
            points_str = str(pick.points)
            # Attempt to set confidence points if there is an editable field.
            try:
                points_input = row.find_element(By.XPATH, ".//input[contains(@name, 'pt') or contains(@name, 'point')]")
                points_input.clear()
                points_input.send_keys(points_str)
            except NoSuchElementException:
                try:
                    points_select = row.find_element(By.XPATH, ".//select[contains(@name, 'pt') or contains(@name, 'point')]")
                    Select(points_select).select_by_value(points_str)
                except NoSuchElementException:
                    # Fall back to attempting to edit a contentEditable cell if present.
                    try:
                        points_cell = row.find_element(By.XPATH, ".//td[contains(@class, 'linepts')]")
                        if points_cell.get_attribute("contenteditable") == "true":
                            points_cell.clear()
                            points_cell.send_keys(points_str)
                    except NoSuchElementException:
                        logging.debug(
                            "Skipped updating points for %s; no editable element detected.",
                            pick.selected_team.get("displayName"),
                        )

        for pick in picks:
            select_team(pick)

        if tie_breaker_value is not None:
            tb_set = False
            tb_candidates = [
                "tb",
                "mnf",
                "tie",
                "tie_breaker",
                "tiebreaker",
                "monday",
                "mnf_total",
            ]
            for name in tb_candidates:
                try:
                    tb_input = driver.find_element(By.NAME, name)
                    tb_input.clear()
                    tb_input.send_keys(str(tie_breaker_value))
                    tb_set = True
                    break
                except NoSuchElementException:
                    continue
            if not tb_set:
                try:
                    tb_input = driver.find_element(
                        By.XPATH,
                        "//tr[.//*[contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'total points') or "
                        "contains(translate(text(),'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'),'tie')]]//input",
                    )
                    tb_input.clear()
                    tb_input.send_keys(str(tie_breaker_value))
                    tb_set = True
                except NoSuchElementException:
                    logging.debug("Could not locate an input for the Monday tie-breaker.")

        submit_clicked = False
        submit_button = None
        submit_xpaths = [
            "//input[@type='submit']",
            "//input[@type='image']",
            "//button[contains(translate(.,'ABCDEFGHIJKLMNOPQRSTUVWXYZ','abcdefghijklmnopqrstuvwxyz'), 'submit')]",
            "//button[contains(.,'Save')]",
            "/html/body/table/tbody/tr[2]/td[1]/input[1]",
        ]
        for xpath in submit_xpaths:
            buttons = driver.find_elements(By.XPATH, xpath)
            if buttons:
                submit_button = buttons[0]
                driver.execute_script("arguments[0].scrollIntoView({block: 'center'});", submit_button)
                driver.execute_script("arguments[0].focus();", submit_button)
                submit_clicked = True
                break

        if not submit_clicked or submit_button is None:
            raise RuntimeError("Could not locate a submit button on the make_week page.")

        try:
            driver.execute_script("crap_shoot('no');")
        except Exception:  # pylint: disable=broad-except
            logging.debug("crap_shoot JS invocation failed; falling back to direct click.")
            try:
                hidden_craps = driver.find_element(By.NAME, "craps")
                driver.execute_script("arguments[0].value='no';", hidden_craps)
            except NoSuchElementException:
                logging.debug("Hidden craps field not found; proceeding without overriding.")
            driver.execute_script("arguments[0].click();", submit_button)

        try:
            wait.until(EC.presence_of_element_located((By.TAG_NAME, "body")))
        except TimeoutException:
            logging.warning("Timed out waiting for submission confirmation; please verify manually.")

    finally:
        if driver is not None:
            if pause_after:
                try:
                    input("Selenium session paused. Press Enter to close the browser...")
                except EOFError:
                    logging.debug("Pause requested but stdin not available; closing browser.")
            driver.quit()
def main() -> None:
    args = parse_args()
    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)

    season = args.season or infer_season_year()
    session = requests.Session()
    scoreboard = fetch_json(
        SCOREBOARD_URL,
        params={"dates": season, "seasontype": 2, "week": args.week},
        session=session,
    )

    events_info = collect_scoreboard_events(scoreboard)

    fallback_dir = args.sbr_fallback_dir
    if not fallback_dir:
        default_path = Path("data") / f"sbr_week{args.week}.html"
        if default_path.exists():
            fallback_dir = str(default_path.parent)
            logging.info("Detected fallback odds file: %s", default_path)

    odds_lookup: Optional[Dict[Tuple[str, str], GameOdds]] = None
    if args.odds_provider == "the-odds-api":
        api_key = args.odds_api_key or os.environ.get("ODDS_API_KEY")
        if not api_key:
            raise SystemExit(
                "The Odds API key is required. Provide --odds-api-key or set ODDS_API_KEY."
            )
        bookmakers = (
            [b.strip() for b in args.odds_bookmakers.split(",") if b.strip()]
            if args.odds_bookmakers
            else ["fanduel", "draftkings", "betmgm"]
        )
        try:
            odds_lookup = build_the_odds_api_lookup(
                events_info,
                session=session,
                api_key=api_key,
                bookmakers=bookmakers,
                week=args.week,
                fallback_dir=fallback_dir,
            )
        except requests.HTTPError as exc:
            logging.error("The Odds API request failed: %s", exc)
            raise SystemExit("Unable to fetch odds from The Odds API; see logs for details.")
        except requests.RequestException as exc:
            logging.warning(
                "The Odds API request timed out or failed (%s); attempting to use local fallback.",
                exc,
            )
            odds_lookup = load_sbr_fallback(
                events_info,
                week=args.week,
                fallback_dir=fallback_dir,
            )
            if not odds_lookup:
                logging.warning(
                    "No local fallback odds available; games without markets will default to home team."
                )

    # Reconstruct scoreboard dict for parse_games (it needs the full structure but we also supply odds lookup)
    games = parse_games(
        scoreboard,
        session=session,
        preferred_provider=args.provider,
        odds_source=args.odds_provider,
        week=args.week,
        odds_lookup=odds_lookup,
    )

    if not games:
        print("No eligible games found. It may be too early for odds or there were filtering issues.")
        return

    # Deterministic tie-break for equal spreads by default: seed via season/week.
    seed = args.seed if args.seed is not None else (season * 100 + args.week)
    picks = assign_points(games, args.max_points, seed)

    if not picks:
        print("No picks generated after applying filters.")
        return

    picks.sort(key=lambda p: p.points, reverse=True)
    monday_summary = build_monday_summary(picks)

    print(render_pick_table(picks))
    print()
    print(format_monday_summary(monday_summary))

    if args.compare_existing:
        compare_login_id = args.login_id or os.environ.get("FTN_USER_ID")
        compare_key = args.login_key or os.environ.get("FTN_KEY")
        if not compare_login_id and not compare_key:
            logging.warning(
                "Cannot compare existing picks without --login-id/FTN_USER_ID or --login-key/FTN_KEY."
            )
        else:
            try:
                existing_submission = fetch_existing_submission(
                    session=session,
                    week=args.week,
                    login_id=compare_login_id,
                    login_key=compare_key,
                )
                comparison = summarize_existing_comparison(
                    picks,
                    existing_submission,
                    monday_summary=monday_summary,
                )
                print()
                print(comparison)
                if not existing_submission.picks:
                    print("(No existing site picks detected for this week.)")
            except Exception as exc:  # pylint: disable=broad-except
                logging.error("Failed to fetch existing site picks: %s", exc)

    tie_breaker_override: Optional[int] = None
    if not args.non_interactive:
        print()
        picks, tie_breaker_override = interactive_adjustments(picks, monday_summary)
        monday_summary = build_monday_summary(picks)
        print("\nFinal picks:")
        print(render_pick_table(picks))
        print()
        print(format_monday_summary(monday_summary, tie_breaker_override))
    else:
        tie_breaker_override = None

    if tie_breaker_override is not None:
        logging.info("Using custom Monday tie-breaker total: %s", tie_breaker_override)

    final_tie_breaker = (
        tie_breaker_override if tie_breaker_override is not None else monday_summary.computed_pick
    )

    should_submit = args.submit
    if not should_submit and not args.non_interactive:
        submit_response = input("Submit picks via headless Selenium now? [y/N]: ").strip().lower()
        should_submit = submit_response in {"y", "yes"}

    if should_submit:
        login_id = args.login_id or os.environ.get("FTN_USER_ID")
        password = args.login_password or os.environ.get("FTN_PASSWORD")
        login_key = args.login_key or os.environ.get("FTN_KEY")

        if not login_id:
            if args.non_interactive:
                raise SystemExit("Login ID is required for submission. Provide --login-id or FTN_USER_ID.")
            login_id = input("Enter FantasyTeamsNetwork user ID: ").strip()

        if not password:
            if args.non_interactive:
                raise SystemExit(
                    "Password is required for submission. Provide --login-password or FTN_PASSWORD."
                )
            password = getpass("Enter FantasyTeamsNetwork password: ")

        try:
            submit_picks_via_selenium(
                picks,
                final_tie_breaker,
                week=args.week,
                login_id=login_id,
                password=password,
                login_key=login_key,
                browser=args.selenium_browser,
                driver_path=args.selenium_driver_path,
                headless=not args.selenium_no_headless,
                pause_after=args.selenium_pause_after,
            )
            print("Submission attempted. Please verify the picks on the site to confirm.")

            compare_login_id = args.login_id or os.environ.get("FTN_USER_ID")
            compare_key = args.login_key or os.environ.get("FTN_KEY")
            if compare_login_id or compare_key:
                try:
                    refreshed = fetch_existing_submission(
                        session=session,
                        week=args.week,
                        login_id=compare_login_id,
                        login_key=compare_key,
                    )
                    post_summary = summarize_existing_comparison(
                        picks,
                        refreshed,
                        monday_summary=build_monday_summary(picks),
                    )
                    print()
                    print("Post-submission check:")
                    print(post_summary)
                except Exception as fetch_exc:  # pylint: disable=broad-except
                    logging.error("Could not verify picks after submission: %s", fetch_exc)
        except Exception as exc:  # pylint: disable=broad-except
            logging.error("Selenium submission failed: %s", exc)
            raise


if __name__ == "__main__":
    main()
