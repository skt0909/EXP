"""Database helpers for deterministic simulation setup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
import random
import uuid

from sqlalchemy import text

from .event_store import ensure_event_store


SIM_SEASON = "SIM-2026"


POSITIONS = ["GK", "GK"] + ["DEF"] * 5 + ["MID"] * 5 + ["FWD"] * 3
POSITION_ENCODED = {"GK": 0, "DEF": 1, "MID": 2, "FWD": 3}


@dataclass(frozen=True)
class SimUser:
    user_id: int
    player_fpl_ids: list[int]
    captain_id: int
    vice_captain_id: int


def reset_simulation_data(engine, season: str) -> None:
    ensure_event_store(engine)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM simulation.football_events WHERE season = :season"), {"season": season})
        sim_users = [r.id for r in conn.execute(text("SELECT id FROM users WHERE username LIKE :prefix"), {"prefix": f"sim_{season}_%"})]
        for user_id in sim_users:
            conn.execute(text("DELETE FROM gw_scores WHERE user_id = :uid"), {"uid": user_id})
            conn.execute(text("DELETE FROM gw_selections WHERE user_id = :uid"), {"uid": user_id})
            conn.execute(
                text(
                    "DELETE FROM squad_players WHERE user_squad_id IN "
                    "(SELECT id FROM user_squads WHERE user_id = :uid)"
                ),
                {"uid": user_id},
            )
            conn.execute(text("DELETE FROM user_squads WHERE user_id = :uid"), {"uid": user_id})
        conn.execute(text("DELETE FROM ml.players WHERE season = :season"), {"season": season})
        conn.execute(text("DELETE FROM ml.fixtures WHERE season = :season"), {"season": season})
        conn.execute(text("DELETE FROM ml.teams WHERE season = :season"), {"season": season})
        conn.execute(text("DELETE FROM gw_scores WHERE season = :season"), {"season": season})
        conn.execute(text("DELETE FROM gw_selections WHERE season = :season"), {"season": season})


def create_teams(engine, season: str, count: int = 20) -> list[int]:
    ids = []
    with engine.begin() as conn:
        for i in range(count):
            ids.append(
                conn.execute(
                    text(
                        "INSERT INTO ml.teams (fpl_id, season, name, short_name) "
                        "VALUES (:fpl_id, :season, :name, :short_name) RETURNING id"
                    ),
                    {
                        "fpl_id": 50000 + i,
                        "season": season,
                        "name": f"Simulation Club {i + 1}",
                        "short_name": f"SC{i + 1}",
                    },
                ).scalar()
            )
    return ids


def create_player_pool(engine, season: str, team_ids: list[int], pool_size: int = 600) -> dict[int, int]:
    internal_by_fpl = {}
    with engine.begin() as conn:
        for i in range(pool_size):
            position = POSITIONS[i % len(POSITIONS)]
            fpl_id = 60000 + i
            internal_by_fpl[fpl_id] = conn.execute(
                text(
                    "INSERT INTO ml.players (fpl_id, season, fpl_name, web_name, position, position_encoded, "
                    "team_id, status, cost_start, now_cost) "
                    "VALUES (:fpl_id, :season, :name, :name, :position, :position_encoded, :team_id, 'a', 60, 60) "
                    "RETURNING id"
                ),
                {
                    "fpl_id": fpl_id,
                    "season": season,
                    "name": f"SimPlayer{fpl_id}",
                    "position": position,
                    "position_encoded": POSITION_ENCODED[position],
                    "team_id": team_ids[i % len(team_ids)],
                },
            ).scalar()
    return internal_by_fpl


def create_fixture(engine, season: str, gameweek: int, kickoff_time: datetime, home_team_id=None, away_team_id=None) -> int:
    with engine.begin() as conn:
        return conn.execute(
            text(
                "INSERT INTO ml.fixtures (fpl_id, season, gameweek, home_team_id, away_team_id, kickoff_time, finished) "
                "VALUES (:fpl_id, :season, :gameweek, :home_team_id, :away_team_id, :kickoff_time, FALSE) RETURNING id"
            ),
            {
                "fpl_id": 70000 + gameweek,
                "season": season,
                "gameweek": gameweek,
                "home_team_id": home_team_id,
                "away_team_id": away_team_id,
                "kickoff_time": kickoff_time,
            },
        ).scalar()


def simulate_users(engine, count: int, season: str, player_fpl_ids: list[int], seed: int = 12345) -> list[SimUser]:
    rng = random.Random(seed)
    users = []
    run_tag = uuid.uuid4().hex[:10]
    with engine.begin() as conn:
        for i in range(count):
            uid = conn.execute(
                text("INSERT INTO users (email, username, password_hash) VALUES (:email, :username, 'simulation') RETURNING id"),
                {"email": f"sim_{season}_{run_tag}_{i}@example.com", "username": f"sim_{season}_{run_tag}_{i}"},
            ).scalar()
            squad = _deterministic_squad(rng, player_fpl_ids)
            user_squad_id = conn.execute(
                text(
                    "INSERT INTO user_squads (user_id, season, budget_remaining) "
                    "VALUES (:uid, :season, 100) RETURNING id"
                ),
                {"uid": uid, "season": season},
            ).scalar()
            for fpl_id in squad:
                conn.execute(
                    text(
                        "INSERT INTO squad_players (user_squad_id, player_id, purchase_price, sell_price, is_active) "
                        "VALUES (:squad_id, :player_id, 60, 60, TRUE)"
                    ),
                    {"squad_id": user_squad_id, "player_id": fpl_id},
                )
            users.append(SimUser(uid, squad, squad[5], squad[6]))
    return users


def create_classic_league(engine, season: str, users: list[SimUser], name: str = "Simulation League") -> int:
    if not users:
        raise ValueError("cannot create a league without users")
    with engine.begin() as conn:
        league_id = conn.execute(
            text(
                "INSERT INTO mini_leagues (name, code, created_by, season, league_type, scoring_type, max_members) "
                "VALUES (:name, :code, :created_by, :season, 'private', 'classic', :max_members) RETURNING id"
            ),
            {
                "name": name,
                "code": f"S{uuid.uuid4().hex[:9]}".upper(),
                "created_by": users[0].user_id,
                "season": season,
                "max_members": min(max(len(users), 1), 32767),
            },
        ).scalar()
        for user in users:
            conn.execute(
                text(
                    "INSERT INTO league_members (league_id, user_id, season_points, rank, last_gw_points) "
                    "VALUES (:league_id, :user_id, 0, 0, 0) ON CONFLICT DO NOTHING"
                ),
                {"league_id": league_id, "user_id": user.user_id},
            )
    return league_id


def seed_transfer_history(engine, users: list[SimUser], season: str, gameweek: int, seed: int, paid_every: int = 5) -> int:
    """Seed deterministic transfer rows so production scoring can count hits."""
    rng = random.Random(seed)
    written = 0
    with engine.begin() as conn:
        for idx, user in enumerate(users):
            if paid_every <= 0 or idx % paid_every != 0:
                continue
            out_id = rng.choice(user.player_fpl_ids)
            in_id = 900000 + gameweek * 100000 + idx
            conn.execute(
                text(
                    "INSERT INTO transfers (user_id, season, gameweek, player_in_id, player_out_id, price_in, price_out, is_free) "
                    "VALUES (:user_id, :season, :gameweek, :player_in_id, :player_out_id, 60, 60, FALSE)"
                ),
                {
                    "user_id": user.user_id,
                    "season": season,
                    "gameweek": gameweek,
                    "player_in_id": in_id,
                    "player_out_id": out_id,
                },
            )
            written += 1
    return written


def force_deadline_passed(engine, season: str, gameweek: int, hours_ago: int = 2) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE ml.fixtures SET kickoff_time = now() - make_interval(hours => :hours) "
                "WHERE season = :season AND gameweek = :gameweek"
            ),
            {"season": season, "gameweek": gameweek, "hours": hours_ago},
        )


def submit_gameweek_selection(engine, user: SimUser, season: str, gameweek: int, chip_used: str | None = None) -> None:
    with engine.begin() as conn:
        selection_id = conn.execute(
            text(
                "INSERT INTO gw_selections (user_id, season, gameweek, captain_id, vice_captain_id, chip_used, is_locked) "
                "VALUES (:uid, :season, :gameweek, :captain_id, :vice_captain_id, :chip_used, FALSE) "
                "ON CONFLICT (user_id, season, gameweek) DO UPDATE SET chip_used = EXCLUDED.chip_used "
                "RETURNING id"
            ),
            {
                "uid": user.user_id,
                "season": season,
                "gameweek": gameweek,
                "captain_id": user.captain_id,
                "vice_captain_id": user.vice_captain_id,
                "chip_used": chip_used,
            },
        ).scalar()
        conn.execute(text("DELETE FROM starting_xi WHERE gw_selection_id = :id"), {"id": selection_id})
        for slot, fpl_id in enumerate(user.player_fpl_ids, start=1):
            conn.execute(
                text(
                    "INSERT INTO starting_xi (gw_selection_id, player_id, position_slot, is_captain, is_vice_captain) "
                    "VALUES (:sid, :pid, :slot, :captain, :vice)"
                ),
                {
                    "sid": selection_id,
                    "pid": fpl_id,
                    "slot": slot,
                    "captain": fpl_id == user.captain_id,
                    "vice": fpl_id == user.vice_captain_id,
                },
            )


def seed_basic_gameweek(engine, season: str, gameweek: int, users: int, seed: int, kickoff_time: datetime):
    reset_simulation_data(engine, season)
    teams = create_teams(engine, season)
    player_map = create_player_pool(engine, season, teams)
    fixture_id = create_fixture(engine, season, gameweek, kickoff_time, teams[0], teams[1])
    sim_users = simulate_users(engine, users, season, list(player_map.keys()), seed)
    for user in sim_users:
        submit_gameweek_selection(engine, user, season, gameweek)
    league_id = create_classic_league(engine, season, sim_users)
    return {"teams": teams, "players": player_map, "fixture_id": fixture_id, "users": sim_users, "league_id": league_id}


def seed_simulation_world(engine, season: str, users: int, seed: int, kickoff_time: datetime):
    reset_simulation_data(engine, season)
    teams = create_teams(engine, season)
    player_map = create_player_pool(engine, season, teams)
    sim_users = simulate_users(engine, users, season, list(player_map.keys()), seed)
    league_id = create_classic_league(engine, season, sim_users)
    return {"teams": teams, "players": player_map, "users": sim_users, "kickoff_time": kickoff_time, "league_id": league_id}


def seed_gameweek_from_world(engine, world: dict, season: str, gameweek: int, kickoff_time: datetime) -> int:
    fixture_id = create_fixture(engine, season, gameweek, kickoff_time, world["teams"][0], world["teams"][1])
    for user in world["users"]:
        submit_gameweek_selection(engine, user, season, gameweek)
    return fixture_id


def _deterministic_squad(rng: random.Random, player_fpl_ids: list[int]) -> list[int]:
    by_position = {"GK": [], "DEF": [], "MID": [], "FWD": []}
    for fpl_id in player_fpl_ids:
        position = POSITIONS[(fpl_id - 60000) % len(POSITIONS)]
        by_position[position].append(fpl_id)
    gks = rng.sample(by_position["GK"], 2)
    defs = rng.sample(by_position["DEF"], 5)
    mids = rng.sample(by_position["MID"], 5)
    fwds = rng.sample(by_position["FWD"], 3)
    xi = [gks[0], *defs[:4], *mids[:4], *fwds[:2]]
    bench = [defs[4], mids[4], gks[1], fwds[2]]
    return xi + bench
