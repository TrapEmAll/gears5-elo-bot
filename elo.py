"""Pure Elo and match-validation logic for the Gears 5 Discord bot."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable

K_FACTOR = 32

MODES: dict[str, dict[str, object]] = {
    "control_1v1": {"label": "Control 1v1", "team_size": 1},
    "control_3v3": {"label": "Control 3v3", "team_size": 3},
    "control_4v4": {"label": "Control 4v4", "team_size": 4},
    "gnashers_1v1": {"label": "1v1 Gnashers", "team_size": 1},
    "gnashers_2v2": {"label": "2v2 Gnashers", "team_size": 2},
}
MENTION_RE = re.compile(r"^(?:<@!?(\d+)>|(\d+))$")
CONTROL_STATS = ("captures", "breaks", "kills", "deaths", "assists", "damage", "score")
GNASHERS_STATS = ("kills", "deaths", "damage", "score")
GNASHERS_2V2_STATS = ("kills", "deaths", "assists", "damage", "score")


@dataclass(frozen=True)
class RatingChange:
    user_id: int
    old_rating: int
    new_rating: int
    delta: int


def mode_label(mode: str) -> str:
    return str(MODES[mode]["label"])


def team_size(mode: str) -> int:
    return int(MODES[mode]["team_size"])


def parse_team(raw: str, expected_size: int) -> list[int]:
    players = [piece.strip() for piece in raw.split(",") if piece.strip()]
    if len(players) != expected_size:
        raise ValueError(f"Enter exactly {expected_size} player mention(s) or ID(s), separated by commas")
    ids: list[int] = []
    for player in players:
        match = MENTION_RE.fullmatch(player)
        if not match:
            raise ValueError(f"Could not read `{player}` as a player mention or Discord ID")
        ids.append(int(match.group(1) or match.group(2)))
    if len(set(ids)) != len(ids):
        raise ValueError("Each player must be listed only once")
    return ids


def team_key(player_ids: Iterable[int]) -> str:
    return ",".join(map(str, sorted(player_ids)))


def canonical_matchup(team_one: Iterable[int], team_two: Iterable[int]) -> tuple[str, str, bool]:
    first = team_key(team_one)
    second = team_key(team_two)
    return (first, second, True) if first < second else (second, first, False)


def stat_names(mode: str) -> tuple[str, ...]:
    if mode.startswith("control_"):
        return CONTROL_STATS
    return GNASHERS_2V2_STATS if mode == "gnashers_2v2" else GNASHERS_STATS


def parse_player_stats(raw: str, mode: str) -> dict[str, int]:
    values: dict[str, int] = {}
    required = set(stat_names(mode))
    for piece in raw.replace(",", " ").split():
        if "=" not in piece:
            raise ValueError(f"Expected key=value, such as kills=10")
        key, value = piece.split("=", 1)
        if key not in required:
            raise ValueError(f"`{key}` is not used in {mode_label(mode)}")
        try:
            number = int(value)
        except ValueError as error:
            raise ValueError(f"`{value}` is not a whole number") from error
        if number < 0:
            raise ValueError("Stats cannot be negative")
        values[key] = number
    missing = required - values.keys()
    if missing:
        raise ValueError(f"Missing {', '.join(sorted(missing))}")
    return values


def parse_match_stats(raw: str, mode: str, player_ids: Iterable[int]) -> dict[int, dict[str, int]]:
    """Parse one line per player: <mention-or-id> kills=10 deaths=3 ..."""
    expected_players = set(player_ids)
    if not raw.strip():
        raise ValueError("Enter one stat line for every player")
    results: dict[int, dict[str, int]] = {}
    required = set(stat_names(mode))
    # Discord slash-command text options are single-line fields, so accept
    # semicolons as separators as well as newlines for pasted/test input.
    for line in re.split(r"[;\n]+", raw):
        pieces = line.split()
        if not pieces:
            continue
        player = parse_team(pieces[0], 1)[0]
        if player not in expected_players:
            raise ValueError(f"Stats include player {player}, who is not in this match")
        if player in results:
            raise ValueError(f"Stats were entered more than once for player {player}")
        try:
            results[player] = parse_player_stats(" ".join(pieces[1:]), mode)
        except ValueError as error:
            raise ValueError(f"Player {player}: {error}") from error
    if set(results) != expected_players:
        missing = expected_players - set(results)
        raise ValueError(f"Missing stat lines for player(s): {', '.join(map(str, sorted(missing)))}")
    return results


def expected_score(team_rating: float, opponent_rating: float) -> float:
    return 1 / (1 + 10 ** ((opponent_rating - team_rating) / 400))


def calculate_match_changes(
    mode: str,
    team_one: Iterable[tuple[int, int]],
    team_two: Iterable[tuple[int, int]],
    winner: int,
    k_factor: int = K_FACTOR,
) -> list[RatingChange]:
    """Return each player's rating change. Tuples are (user_id, current_rating)."""
    if mode not in MODES:
        raise ValueError("Unknown mode")
    first = list(team_one)
    second = list(team_two)
    expected_size = team_size(mode)
    if len(first) != expected_size or len(second) != expected_size:
        raise ValueError(f"{mode_label(mode)} requires {expected_size} player(s) per team")
    all_ids = [player_id for player_id, _ in first + second]
    if len(all_ids) != len(set(all_ids)):
        raise ValueError("A player cannot appear on both teams or twice on one team")
    if winner not in (1, 2):
        raise ValueError("Winner must be team 1 or team 2")

    first_average = sum(rating for _, rating in first) / len(first)
    second_average = sum(rating for _, rating in second) / len(second)
    first_expected = expected_score(first_average, second_average)
    second_expected = 1 - first_expected
    first_score = 1 if winner == 1 else 0
    second_score = 1 - first_score
    first_delta = round(k_factor * (first_score - first_expected))
    second_delta = round(k_factor * (second_score - second_expected))

    changes = [
        RatingChange(player_id, rating, rating + first_delta, first_delta)
        for player_id, rating in first
    ]
    changes.extend(
        RatingChange(player_id, rating, rating + second_delta, second_delta)
        for player_id, rating in second
    )
    return changes
