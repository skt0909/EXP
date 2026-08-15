--
-- PostgreSQL database dump
--


-- Dumped from database version 18.4
-- Dumped by pg_dump version 18.4

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: ml; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA ml;


--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--



--
-- Name: enforce_fixture_identity_fn(); Type: FUNCTION; Schema: ml; Owner: -
--

CREATE FUNCTION ml.enforce_fixture_identity_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    IF OLD.home_team_id IS DISTINCT FROM NEW.home_team_id OR

       OLD.away_team_id IS DISTINCT FROM NEW.away_team_id OR

       OLD.fpl_id       IS DISTINCT FROM NEW.fpl_id THEN

        RAISE EXCEPTION 'Fixture identity is immutable: fixture fpl_id=%, season=%',

            OLD.fpl_id, OLD.season;

    END IF;

    RETURN NEW;

END;

$$;


--
-- Name: enforce_gw_stats_immutability_fn(); Type: FUNCTION; Schema: ml; Owner: -
--

CREATE FUNCTION ml.enforce_gw_stats_immutability_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    IF OLD.is_live = FALSE THEN

        RAISE EXCEPTION 'Cannot modify settled GW stats row: player_id=%, gameweek=%',

            OLD.player_id, OLD.gameweek;

    END IF;

    RETURN NEW;

END;

$$;


--
-- Name: enforce_ml_prediction_immutability_fn(); Type: FUNCTION; Schema: ml; Owner: -
--

CREATE FUNCTION ml.enforce_ml_prediction_immutability_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    RAISE EXCEPTION 'ML predictions are immutable. INSERT a new row with a new model_version instead.';

    RETURN NULL;

END;

$$;


--
-- Name: enforce_player_identity_fn(); Type: FUNCTION; Schema: ml; Owner: -
--

CREATE FUNCTION ml.enforce_player_identity_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    IF OLD.fpl_id  IS DISTINCT FROM NEW.fpl_id OR

       OLD.season  IS DISTINCT FROM NEW.season THEN

        RAISE EXCEPTION 'Player identity is immutable: fpl_id=%, season=%',

            OLD.fpl_id, OLD.season;

    END IF;

    RETURN NEW;

END;

$$;


--
-- Name: enforce_chip_limit_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_chip_limit_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
        BEGIN
            IF NEW.chip_type IN ('bench_boost', 'triple_captain', 'free_hit') THEN
                IF EXISTS (
                    SELECT 1 FROM chips
                    WHERE user_id = NEW.user_id
                      AND season = NEW.season
                      AND chip_type = NEW.chip_type
                ) THEN
                    RAISE EXCEPTION 'Chip already used this season: user_id=%, chip=%',
                        NEW.user_id, NEW.chip_type;
                END IF;
            ELSIF NEW.chip_type = 'wildcard' THEN
                IF (
                    SELECT COUNT(*) FROM chips
                    WHERE user_id = NEW.user_id
                      AND season = NEW.season
                      AND chip_type = 'wildcard'
                ) >= 2 THEN
                    RAISE EXCEPTION 'Wildcard already used twice this season: user_id=%',
                        NEW.user_id;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$;


--
-- Name: enforce_selection_lock_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_selection_lock_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    IF OLD.is_locked = TRUE THEN

        RAISE EXCEPTION 'Selection is locked and cannot be modified: user_id=%, gameweek=%',

            OLD.user_id, OLD.gameweek;

    END IF;

    RETURN NEW;

END;

$$;


--
-- Name: enforce_snapshot_immutability_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_snapshot_immutability_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    RAISE EXCEPTION 'Leaderboard snapshots are immutable. UPDATE and DELETE are not permitted.';

    RETURN NULL;

END;

$$;


--
-- Name: enforce_transfers_immutability_fn(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE FUNCTION public.enforce_transfers_immutability_fn() RETURNS trigger
    LANGUAGE plpgsql
    AS $$

BEGIN

    RAISE EXCEPTION 'Transfers are append-only. UPDATE and DELETE are not permitted.';

    RETURN NULL;

END;

$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: fixtures; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.fixtures (
    id integer NOT NULL,
    fpl_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    home_team_id integer,
    away_team_id integer,
    kickoff_time timestamp with time zone,
    home_score smallint,
    away_score smallint,
    finished boolean DEFAULT false,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: fixtures_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.fixtures_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: fixtures_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.fixtures_id_seq OWNED BY ml.fixtures.id;


--
-- Name: ml_predictions; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.ml_predictions (
    id integer NOT NULL,
    player_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    predicted_points numeric(6,2) NOT NULL,
    model_version character varying(50) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    tier_or_label character varying(40)
);


--
-- Name: ml_predictions_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.ml_predictions_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: ml_predictions_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.ml_predictions_id_seq OWNED BY ml.ml_predictions.id;


--
-- Name: player_gw_features; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.player_gw_features (
    id integer NOT NULL,
    player_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    pts_rolling_3gw numeric(6,2),
    pts_rolling_5gw numeric(6,2),
    minutes_rolling_3gw numeric(6,2),
    goals_rolling_5gw numeric(6,2),
    assists_rolling_5gw numeric(6,2),
    clean_sheets_rolling_5gw numeric(6,2),
    saves_rolling_3gw numeric(6,2),
    bonus_rolling_3gw numeric(6,2),
    bps_rolling_3gw numeric(6,2),
    ict_rolling_3gw numeric(6,2),
    xg_rolling_5gw numeric(6,3),
    xa_rolling_5gw numeric(6,3),
    xgi_rolling_5gw numeric(6,3),
    position_encoded smallint NOT NULL,
    was_home boolean,
    price_current numeric(4,1) NOT NULL,
    ownership_count integer,
    next_gw_points smallint,
    features_computed_at timestamp with time zone DEFAULT now(),
    label_filled_at timestamp with time zone
);


--
-- Name: player_gw_features_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.player_gw_features_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_gw_features_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.player_gw_features_id_seq OWNED BY ml.player_gw_features.id;


--
-- Name: player_gw_stats; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.player_gw_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    fixture_id integer,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    is_live boolean DEFAULT false,
    minutes smallint DEFAULT 0,
    goals_scored smallint DEFAULT 0,
    assists smallint DEFAULT 0,
    clean_sheets smallint DEFAULT 0,
    goals_conceded smallint DEFAULT 0,
    saves smallint DEFAULT 0,
    bonus smallint DEFAULT 0,
    bps smallint DEFAULT 0,
    yellow_cards smallint DEFAULT 0,
    red_cards smallint DEFAULT 0,
    own_goals smallint DEFAULT 0,
    penalties_saved smallint DEFAULT 0,
    penalties_missed smallint DEFAULT 0,
    ict_index numeric(6,1),
    influence numeric(6,1),
    creativity numeric(6,1),
    threat numeric(6,1),
    expected_goals numeric(6,3),
    expected_assists numeric(6,3),
    expected_goal_involvements numeric(6,3),
    value smallint NOT NULL,
    selected integer,
    transfers_in integer DEFAULT 0,
    transfers_out integer DEFAULT 0,
    transfers_balance integer DEFAULT 0,
    was_home boolean,
    team_h_score smallint,
    team_a_score smallint,
    total_points smallint NOT NULL,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: player_gw_stats_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.player_gw_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: player_gw_stats_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.player_gw_stats_id_seq OWNED BY ml.player_gw_stats.id;


--
-- Name: players; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.players (
    id integer NOT NULL,
    fpl_id integer NOT NULL,
    season character varying(9) NOT NULL,
    fpl_name character varying(150) NOT NULL,
    web_name character varying(100),
    fbref_name character varying(150),
    "position" character varying(3) NOT NULL,
    position_encoded smallint NOT NULL,
    team_id integer,
    cost_start smallint,
    status character varying(1) DEFAULT 'a'::character varying,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    CONSTRAINT players_position_check CHECK ((("position")::text = ANY ((ARRAY['GK'::character varying, 'DEF'::character varying, 'MID'::character varying, 'FWD'::character varying])::text[]))),
    CONSTRAINT players_position_encoded_check CHECK (((position_encoded >= 0) AND (position_encoded <= 3)))
);


--
-- Name: players_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.players_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: players_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.players_id_seq OWNED BY ml.players.id;


--
-- Name: season_stats; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.season_stats (
    id integer NOT NULL,
    player_id integer NOT NULL,
    season character varying(9) NOT NULL,
    data_source character varying(10) NOT NULL,
    total_minutes integer,
    matches_played smallint,
    fpl_total_points smallint,
    goals smallint,
    assists smallint,
    shots smallint,
    shots_on_target smallint,
    xg_season numeric(6,2),
    xa_season numeric(6,2),
    saves smallint,
    clean_sheets smallint,
    gk_save_pct numeric(5,2),
    starts smallint,
    mins_per_match smallint,
    on_off_diff numeric(5,2),
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT season_stats_data_source_check CHECK (((data_source)::text = ANY ((ARRAY['fpl'::character varying, 'fbref'::character varying])::text[])))
);


--
-- Name: season_stats_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.season_stats_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: season_stats_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.season_stats_id_seq OWNED BY ml.season_stats.id;


--
-- Name: teams; Type: TABLE; Schema: ml; Owner: -
--

CREATE TABLE ml.teams (
    id integer NOT NULL,
    fpl_id integer NOT NULL,
    season character varying(9) NOT NULL,
    name character varying(100) NOT NULL,
    short_name character varying(10) NOT NULL,
    strength_overall_home smallint,
    strength_overall_away smallint,
    strength_attack_home smallint,
    strength_attack_away smallint,
    strength_defence_home smallint,
    strength_defence_away smallint,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: teams_id_seq; Type: SEQUENCE; Schema: ml; Owner: -
--

CREATE SEQUENCE ml.teams_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: teams_id_seq; Type: SEQUENCE OWNED BY; Schema: ml; Owner: -
--

ALTER SEQUENCE ml.teams_id_seq OWNED BY ml.teams.id;


--
-- Name: chips; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.chips (
    id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    chip_type character varying(20) NOT NULL,
    gameweek_used smallint NOT NULL,
    used_at timestamp with time zone DEFAULT now(),
    CONSTRAINT chips_chip_type_check CHECK (((chip_type)::text = ANY ((ARRAY['wildcard'::character varying, 'bench_boost'::character varying, 'triple_captain'::character varying, 'free_hit'::character varying])::text[])))
);


--
-- Name: chips_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.chips_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: chips_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.chips_id_seq OWNED BY public.chips.id;


--
-- Name: gw_scores; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gw_scores (
    id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    raw_points smallint DEFAULT 0 NOT NULL,
    final_points smallint DEFAULT 0 NOT NULL,
    transfer_hits smallint DEFAULT 0 NOT NULL,
    hit_deductions smallint DEFAULT 0 NOT NULL,
    total_points smallint DEFAULT 0 NOT NULL,
    season_total integer DEFAULT 0 NOT NULL
);


--
-- Name: gw_scores_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gw_scores_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gw_scores_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gw_scores_id_seq OWNED BY public.gw_scores.id;


--
-- Name: gw_selections; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.gw_selections (
    id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    captain_id integer NOT NULL,
    vice_captain_id integer NOT NULL,
    chip_used character varying(20),
    is_locked boolean DEFAULT false NOT NULL,
    submitted_at timestamp with time zone DEFAULT now(),
    CONSTRAINT gw_selections_chip_used_check CHECK (((chip_used)::text = ANY ((ARRAY['wildcard'::character varying, 'bench_boost'::character varying, 'triple_captain'::character varying, 'free_hit'::character varying])::text[])))
);


--
-- Name: gw_selections_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.gw_selections_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: gw_selections_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.gw_selections_id_seq OWNED BY public.gw_selections.id;


--
-- Name: leaderboard_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.leaderboard_snapshots (
    id integer NOT NULL,
    league_id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    points_this_gw smallint DEFAULT 0 NOT NULL,
    total_points integer DEFAULT 0 NOT NULL,
    rank smallint NOT NULL,
    rank_movement smallint DEFAULT 0 NOT NULL
);


--
-- Name: leaderboard_snapshots_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.leaderboard_snapshots_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: leaderboard_snapshots_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.leaderboard_snapshots_id_seq OWNED BY public.leaderboard_snapshots.id;


--
-- Name: league_h2h_fixtures; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.league_h2h_fixtures (
    id integer NOT NULL,
    league_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    user_id_1 integer NOT NULL,
    user_id_2 integer,
    points_1 smallint,
    points_2 smallint,
    result character varying(10),
    CONSTRAINT league_h2h_fixtures_result_check CHECK (((result)::text = ANY ((ARRAY['win_1'::character varying, 'win_2'::character varying, 'draw'::character varying, 'bye'::character varying])::text[])))
);


--
-- Name: league_h2h_fixtures_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.league_h2h_fixtures_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: league_h2h_fixtures_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.league_h2h_fixtures_id_seq OWNED BY public.league_h2h_fixtures.id;


--
-- Name: league_members; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.league_members (
    id integer NOT NULL,
    league_id integer NOT NULL,
    user_id integer NOT NULL,
    joined_at timestamp with time zone DEFAULT now(),
    season_points integer DEFAULT 0 NOT NULL,
    rank smallint DEFAULT 0 NOT NULL,
    last_gw_points smallint DEFAULT 0 NOT NULL
);


--
-- Name: league_members_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.league_members_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: league_members_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.league_members_id_seq OWNED BY public.league_members.id;


--
-- Name: mini_leagues; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.mini_leagues (
    id integer NOT NULL,
    name character varying(100) NOT NULL,
    code character varying(10) NOT NULL,
    created_by integer NOT NULL,
    season character varying(9) NOT NULL,
    league_type character varying(20) NOT NULL,
    scoring_type character varying(20) NOT NULL,
    max_members smallint DEFAULT 50 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT mini_leagues_league_type_check CHECK (((league_type)::text = ANY ((ARRAY['public'::character varying, 'private'::character varying])::text[]))),
    CONSTRAINT mini_leagues_scoring_type_check CHECK (((scoring_type)::text = ANY ((ARRAY['classic'::character varying, 'head_to_head'::character varying])::text[])))
);


--
-- Name: mini_leagues_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.mini_leagues_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: mini_leagues_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.mini_leagues_id_seq OWNED BY public.mini_leagues.id;


--
-- Name: squad_players; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.squad_players (
    id integer NOT NULL,
    user_squad_id integer NOT NULL,
    player_id integer NOT NULL,
    purchase_price smallint NOT NULL,
    sell_price smallint,
    is_active boolean DEFAULT true NOT NULL
);


--
-- Name: squad_players_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.squad_players_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: squad_players_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.squad_players_id_seq OWNED BY public.squad_players.id;


--
-- Name: starting_xi; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.starting_xi (
    id integer NOT NULL,
    gw_selection_id integer NOT NULL,
    player_id integer NOT NULL,
    position_slot smallint NOT NULL,
    is_captain boolean DEFAULT false NOT NULL,
    is_vice_captain boolean DEFAULT false NOT NULL,
    CONSTRAINT starting_xi_position_slot_check CHECK (((position_slot >= 1) AND (position_slot <= 15)))
);


--
-- Name: starting_xi_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.starting_xi_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: starting_xi_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.starting_xi_id_seq OWNED BY public.starting_xi.id;


--
-- Name: transfers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.transfers (
    id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    gameweek smallint NOT NULL,
    player_in_id integer NOT NULL,
    player_out_id integer NOT NULL,
    price_in smallint NOT NULL,
    price_out smallint NOT NULL,
    is_free boolean NOT NULL,
    transferred_at timestamp with time zone DEFAULT now()
);


--
-- Name: transfers_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.transfers_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: transfers_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.transfers_id_seq OWNED BY public.transfers.id;


--
-- Name: user_squads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_squads (
    id integer NOT NULL,
    user_id integer NOT NULL,
    season character varying(9) NOT NULL,
    budget_remaining smallint DEFAULT 1000 NOT NULL,
    total_transfers smallint DEFAULT 0 NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: user_squads_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.user_squads_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: user_squads_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.user_squads_id_seq OWNED BY public.user_squads.id;


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    id integer NOT NULL,
    email character varying(255) NOT NULL,
    username character varying(50) NOT NULL,
    password_hash character varying(255) NOT NULL,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: users_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.users_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: users_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.users_id_seq OWNED BY public.users.id;


--
-- Name: fixtures id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.fixtures ALTER COLUMN id SET DEFAULT nextval('ml.fixtures_id_seq'::regclass);


--
-- Name: ml_predictions id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.ml_predictions ALTER COLUMN id SET DEFAULT nextval('ml.ml_predictions_id_seq'::regclass);


--
-- Name: player_gw_features id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_features ALTER COLUMN id SET DEFAULT nextval('ml.player_gw_features_id_seq'::regclass);


--
-- Name: player_gw_stats id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_stats ALTER COLUMN id SET DEFAULT nextval('ml.player_gw_stats_id_seq'::regclass);


--
-- Name: players id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.players ALTER COLUMN id SET DEFAULT nextval('ml.players_id_seq'::regclass);


--
-- Name: season_stats id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.season_stats ALTER COLUMN id SET DEFAULT nextval('ml.season_stats_id_seq'::regclass);


--
-- Name: teams id; Type: DEFAULT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.teams ALTER COLUMN id SET DEFAULT nextval('ml.teams_id_seq'::regclass);


--
-- Name: chips id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chips ALTER COLUMN id SET DEFAULT nextval('public.chips_id_seq'::regclass);


--
-- Name: gw_scores id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_scores ALTER COLUMN id SET DEFAULT nextval('public.gw_scores_id_seq'::regclass);


--
-- Name: gw_selections id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_selections ALTER COLUMN id SET DEFAULT nextval('public.gw_selections_id_seq'::regclass);


--
-- Name: leaderboard_snapshots id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leaderboard_snapshots ALTER COLUMN id SET DEFAULT nextval('public.leaderboard_snapshots_id_seq'::regclass);


--
-- Name: league_h2h_fixtures id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures ALTER COLUMN id SET DEFAULT nextval('public.league_h2h_fixtures_id_seq'::regclass);


--
-- Name: league_members id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_members ALTER COLUMN id SET DEFAULT nextval('public.league_members_id_seq'::regclass);


--
-- Name: mini_leagues id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mini_leagues ALTER COLUMN id SET DEFAULT nextval('public.mini_leagues_id_seq'::regclass);


--
-- Name: squad_players id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.squad_players ALTER COLUMN id SET DEFAULT nextval('public.squad_players_id_seq'::regclass);


--
-- Name: starting_xi id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.starting_xi ALTER COLUMN id SET DEFAULT nextval('public.starting_xi_id_seq'::regclass);


--
-- Name: transfers id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transfers ALTER COLUMN id SET DEFAULT nextval('public.transfers_id_seq'::regclass);


--
-- Name: user_squads id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_squads ALTER COLUMN id SET DEFAULT nextval('public.user_squads_id_seq'::regclass);


--
-- Name: users id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users ALTER COLUMN id SET DEFAULT nextval('public.users_id_seq'::regclass);


--
-- Name: fixtures fixtures_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.fixtures
    ADD CONSTRAINT fixtures_pkey PRIMARY KEY (id);


--
-- Name: ml_predictions ml_predictions_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.ml_predictions
    ADD CONSTRAINT ml_predictions_pkey PRIMARY KEY (id);


--
-- Name: player_gw_features player_gw_features_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_features
    ADD CONSTRAINT player_gw_features_pkey PRIMARY KEY (id);


--
-- Name: player_gw_stats player_gw_stats_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_stats
    ADD CONSTRAINT player_gw_stats_pkey PRIMARY KEY (id);


--
-- Name: players players_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.players
    ADD CONSTRAINT players_pkey PRIMARY KEY (id);


--
-- Name: season_stats season_stats_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.season_stats
    ADD CONSTRAINT season_stats_pkey PRIMARY KEY (id);


--
-- Name: teams teams_pkey; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.teams
    ADD CONSTRAINT teams_pkey PRIMARY KEY (id);


--
-- Name: fixtures uq_fixtures_fpl_season; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.fixtures
    ADD CONSTRAINT uq_fixtures_fpl_season UNIQUE (fpl_id, season);


--
-- Name: ml_predictions uq_mlp_player_season_gw_version; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.ml_predictions
    ADD CONSTRAINT uq_mlp_player_season_gw_version UNIQUE (player_id, season, gameweek, model_version);


--
-- Name: player_gw_features uq_pgwf_player_season_gw; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_features
    ADD CONSTRAINT uq_pgwf_player_season_gw UNIQUE (player_id, season, gameweek);


--
-- Name: player_gw_stats uq_pgws_player_season_gw; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_stats
    ADD CONSTRAINT uq_pgws_player_season_gw UNIQUE (player_id, season, gameweek);


--
-- Name: players uq_players_fpl_season; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.players
    ADD CONSTRAINT uq_players_fpl_season UNIQUE (fpl_id, season);


--
-- Name: season_stats uq_season_stats_player_season_source; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.season_stats
    ADD CONSTRAINT uq_season_stats_player_season_source UNIQUE (player_id, season, data_source);


--
-- Name: teams uq_teams_fpl_season; Type: CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.teams
    ADD CONSTRAINT uq_teams_fpl_season UNIQUE (fpl_id, season);


--
-- Name: chips chips_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chips
    ADD CONSTRAINT chips_pkey PRIMARY KEY (id);


--
-- Name: gw_scores gw_scores_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_scores
    ADD CONSTRAINT gw_scores_pkey PRIMARY KEY (id);


--
-- Name: gw_selections gw_selections_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_selections
    ADD CONSTRAINT gw_selections_pkey PRIMARY KEY (id);


--
-- Name: leaderboard_snapshots leaderboard_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leaderboard_snapshots
    ADD CONSTRAINT leaderboard_snapshots_pkey PRIMARY KEY (id);


--
-- Name: league_h2h_fixtures league_h2h_fixtures_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures
    ADD CONSTRAINT league_h2h_fixtures_pkey PRIMARY KEY (id);


--
-- Name: league_members league_members_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_members
    ADD CONSTRAINT league_members_pkey PRIMARY KEY (id);


--
-- Name: mini_leagues mini_leagues_code_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mini_leagues
    ADD CONSTRAINT mini_leagues_code_key UNIQUE (code);


--
-- Name: mini_leagues mini_leagues_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mini_leagues
    ADD CONSTRAINT mini_leagues_pkey PRIMARY KEY (id);


--
-- Name: squad_players squad_players_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.squad_players
    ADD CONSTRAINT squad_players_pkey PRIMARY KEY (id);


--
-- Name: starting_xi starting_xi_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.starting_xi
    ADD CONSTRAINT starting_xi_pkey PRIMARY KEY (id);


--
-- Name: transfers transfers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transfers
    ADD CONSTRAINT transfers_pkey PRIMARY KEY (id);


--
-- Name: gw_scores uq_gw_scores_user_season_gw; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_scores
    ADD CONSTRAINT uq_gw_scores_user_season_gw UNIQUE (user_id, season, gameweek);


--
-- Name: gw_selections uq_gw_selections_user_season_gw; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_selections
    ADD CONSTRAINT uq_gw_selections_user_season_gw UNIQUE (user_id, season, gameweek);


--
-- Name: league_h2h_fixtures uq_h2h_league_season_gw_user1; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures
    ADD CONSTRAINT uq_h2h_league_season_gw_user1 UNIQUE (league_id, season, gameweek, user_id_1);


--
-- Name: league_members uq_league_members_league_user; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_members
    ADD CONSTRAINT uq_league_members_league_user UNIQUE (league_id, user_id);


--
-- Name: leaderboard_snapshots uq_lsnapshots_league_user_gw; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leaderboard_snapshots
    ADD CONSTRAINT uq_lsnapshots_league_user_gw UNIQUE (league_id, user_id, season, gameweek);


--
-- Name: squad_players uq_squad_players_active; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.squad_players
    ADD CONSTRAINT uq_squad_players_active UNIQUE (user_squad_id, player_id);


--
-- Name: starting_xi uq_starting_xi_sel_player; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.starting_xi
    ADD CONSTRAINT uq_starting_xi_sel_player UNIQUE (gw_selection_id, player_id);


--
-- Name: user_squads uq_user_squads_user_season; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_squads
    ADD CONSTRAINT uq_user_squads_user_season UNIQUE (user_id, season);


--
-- Name: user_squads user_squads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_squads
    ADD CONSTRAINT user_squads_pkey PRIMARY KEY (id);


--
-- Name: users users_email_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_email_key UNIQUE (email);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: idx_fixtures_away_team; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_fixtures_away_team ON ml.fixtures USING btree (away_team_id);


--
-- Name: idx_fixtures_home_team; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_fixtures_home_team ON ml.fixtures USING btree (home_team_id);


--
-- Name: idx_fixtures_season_gw; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_fixtures_season_gw ON ml.fixtures USING btree (season, gameweek);


--
-- Name: idx_fixtures_unfinished; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_fixtures_unfinished ON ml.fixtures USING btree (season, gameweek) WHERE (finished = false);


--
-- Name: idx_mlp_model_version; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_mlp_model_version ON ml.ml_predictions USING btree (model_version);


--
-- Name: idx_mlp_player_season_gw; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_mlp_player_season_gw ON ml.ml_predictions USING btree (player_id, season, gameweek);


--
-- Name: idx_pgwf_player_season_gw; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_pgwf_player_season_gw ON ml.player_gw_features USING btree (player_id, season, gameweek);


--
-- Name: idx_pgwf_unlabelled; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_pgwf_unlabelled ON ml.player_gw_features USING btree (season, gameweek) WHERE (next_gw_points IS NULL);


--
-- Name: idx_pgws_live; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_pgws_live ON ml.player_gw_stats USING btree (player_id, gameweek) WHERE (is_live = true);


--
-- Name: idx_pgws_player_season_gw; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_pgws_player_season_gw ON ml.player_gw_stats USING btree (player_id, season, gameweek);


--
-- Name: idx_players_position; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_players_position ON ml.players USING btree ("position");


--
-- Name: idx_players_season; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_players_season ON ml.players USING btree (season);


--
-- Name: idx_players_team_id; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_players_team_id ON ml.players USING btree (team_id);


--
-- Name: idx_ss_player_season; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_ss_player_season ON ml.season_stats USING btree (player_id, season);


--
-- Name: idx_teams_season; Type: INDEX; Schema: ml; Owner: -
--

CREATE INDEX idx_teams_season ON ml.teams USING btree (season);


--
-- Name: idx_chips_user_season; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_chips_user_season ON public.chips USING btree (user_id, season);


--
-- Name: idx_gwscores_season_gw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gwscores_season_gw ON public.gw_scores USING btree (season, gameweek);


--
-- Name: idx_gwscores_user_season; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gwscores_user_season ON public.gw_scores USING btree (user_id, season);


--
-- Name: idx_gwsel_locked; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gwsel_locked ON public.gw_selections USING btree (season, gameweek) WHERE (is_locked = false);


--
-- Name: idx_gwsel_user_season_gw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_gwsel_user_season_gw ON public.gw_selections USING btree (user_id, season, gameweek);


--
-- Name: idx_leagues_season; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_leagues_season ON public.mini_leagues USING btree (season);


--
-- Name: idx_lmembers_league; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lmembers_league ON public.league_members USING btree (league_id);


--
-- Name: idx_lmembers_user; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lmembers_user ON public.league_members USING btree (user_id);


--
-- Name: idx_lsnapshots_league_gw; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_lsnapshots_league_gw ON public.leaderboard_snapshots USING btree (league_id, season, gameweek);


--
-- Name: idx_sqplayers_squad; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_sqplayers_squad ON public.squad_players USING btree (user_squad_id) WHERE (is_active = true);


--
-- Name: idx_startxi_selection; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_startxi_selection ON public.starting_xi USING btree (gw_selection_id);


--
-- Name: idx_transfers_user_season; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_transfers_user_season ON public.transfers USING btree (user_id, season);


--
-- Name: idx_usquads_user_season; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX idx_usquads_user_season ON public.user_squads USING btree (user_id, season);


--
-- Name: uq_chips_user_season_type_restricted; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_chips_user_season_type_restricted ON public.chips USING btree (user_id, season, chip_type) WHERE ((chip_type)::text <> 'wildcard'::text);


--
-- Name: fixtures enforce_fixture_identity; Type: TRIGGER; Schema: ml; Owner: -
--

CREATE TRIGGER enforce_fixture_identity BEFORE UPDATE ON ml.fixtures FOR EACH ROW EXECUTE FUNCTION ml.enforce_fixture_identity_fn();


--
-- Name: player_gw_stats enforce_gw_stats_immutability; Type: TRIGGER; Schema: ml; Owner: -
--

CREATE TRIGGER enforce_gw_stats_immutability BEFORE UPDATE ON ml.player_gw_stats FOR EACH ROW EXECUTE FUNCTION ml.enforce_gw_stats_immutability_fn();


--
-- Name: ml_predictions enforce_ml_prediction_immutability; Type: TRIGGER; Schema: ml; Owner: -
--

CREATE TRIGGER enforce_ml_prediction_immutability BEFORE UPDATE ON ml.ml_predictions FOR EACH ROW EXECUTE FUNCTION ml.enforce_ml_prediction_immutability_fn();


--
-- Name: players enforce_player_identity; Type: TRIGGER; Schema: ml; Owner: -
--

CREATE TRIGGER enforce_player_identity BEFORE UPDATE ON ml.players FOR EACH ROW EXECUTE FUNCTION ml.enforce_player_identity_fn();


--
-- Name: chips enforce_chip_limit; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER enforce_chip_limit BEFORE INSERT ON public.chips FOR EACH ROW EXECUTE FUNCTION public.enforce_chip_limit_fn();


--
-- Name: gw_selections enforce_selection_lock; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER enforce_selection_lock BEFORE UPDATE ON public.gw_selections FOR EACH ROW EXECUTE FUNCTION public.enforce_selection_lock_fn();


--
-- Name: leaderboard_snapshots enforce_snapshot_immutability; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER enforce_snapshot_immutability BEFORE DELETE OR UPDATE ON public.leaderboard_snapshots FOR EACH ROW EXECUTE FUNCTION public.enforce_snapshot_immutability_fn();


--
-- Name: transfers enforce_transfers_immutability; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER enforce_transfers_immutability BEFORE DELETE OR UPDATE ON public.transfers FOR EACH ROW EXECUTE FUNCTION public.enforce_transfers_immutability_fn();


--
-- Name: fixtures fixtures_away_team_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.fixtures
    ADD CONSTRAINT fixtures_away_team_id_fkey FOREIGN KEY (away_team_id) REFERENCES ml.teams(id) ON DELETE SET NULL;


--
-- Name: fixtures fixtures_home_team_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.fixtures
    ADD CONSTRAINT fixtures_home_team_id_fkey FOREIGN KEY (home_team_id) REFERENCES ml.teams(id) ON DELETE SET NULL;


--
-- Name: ml_predictions ml_predictions_player_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.ml_predictions
    ADD CONSTRAINT ml_predictions_player_id_fkey FOREIGN KEY (player_id) REFERENCES ml.players(id) ON DELETE CASCADE;


--
-- Name: player_gw_features player_gw_features_player_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_features
    ADD CONSTRAINT player_gw_features_player_id_fkey FOREIGN KEY (player_id) REFERENCES ml.players(id) ON DELETE CASCADE;


--
-- Name: player_gw_stats player_gw_stats_fixture_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_stats
    ADD CONSTRAINT player_gw_stats_fixture_id_fkey FOREIGN KEY (fixture_id) REFERENCES ml.fixtures(id) ON DELETE SET NULL;


--
-- Name: player_gw_stats player_gw_stats_player_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.player_gw_stats
    ADD CONSTRAINT player_gw_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES ml.players(id) ON DELETE CASCADE;


--
-- Name: players players_team_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.players
    ADD CONSTRAINT players_team_id_fkey FOREIGN KEY (team_id) REFERENCES ml.teams(id) ON DELETE SET NULL;


--
-- Name: season_stats season_stats_player_id_fkey; Type: FK CONSTRAINT; Schema: ml; Owner: -
--

ALTER TABLE ONLY ml.season_stats
    ADD CONSTRAINT season_stats_player_id_fkey FOREIGN KEY (player_id) REFERENCES ml.players(id) ON DELETE CASCADE;


--
-- Name: chips chips_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.chips
    ADD CONSTRAINT chips_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: gw_scores gw_scores_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_scores
    ADD CONSTRAINT gw_scores_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: gw_selections gw_selections_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.gw_selections
    ADD CONSTRAINT gw_selections_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: leaderboard_snapshots leaderboard_snapshots_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leaderboard_snapshots
    ADD CONSTRAINT leaderboard_snapshots_league_id_fkey FOREIGN KEY (league_id) REFERENCES public.mini_leagues(id) ON DELETE CASCADE;


--
-- Name: leaderboard_snapshots leaderboard_snapshots_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.leaderboard_snapshots
    ADD CONSTRAINT leaderboard_snapshots_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: league_h2h_fixtures league_h2h_fixtures_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures
    ADD CONSTRAINT league_h2h_fixtures_league_id_fkey FOREIGN KEY (league_id) REFERENCES public.mini_leagues(id) ON DELETE CASCADE;


--
-- Name: league_h2h_fixtures league_h2h_fixtures_user_id_1_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures
    ADD CONSTRAINT league_h2h_fixtures_user_id_1_fkey FOREIGN KEY (user_id_1) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: league_h2h_fixtures league_h2h_fixtures_user_id_2_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_h2h_fixtures
    ADD CONSTRAINT league_h2h_fixtures_user_id_2_fkey FOREIGN KEY (user_id_2) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: league_members league_members_league_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_members
    ADD CONSTRAINT league_members_league_id_fkey FOREIGN KEY (league_id) REFERENCES public.mini_leagues(id) ON DELETE CASCADE;


--
-- Name: league_members league_members_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.league_members
    ADD CONSTRAINT league_members_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: mini_leagues mini_leagues_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.mini_leagues
    ADD CONSTRAINT mini_leagues_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: squad_players squad_players_user_squad_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.squad_players
    ADD CONSTRAINT squad_players_user_squad_id_fkey FOREIGN KEY (user_squad_id) REFERENCES public.user_squads(id) ON DELETE CASCADE;


--
-- Name: starting_xi starting_xi_gw_selection_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.starting_xi
    ADD CONSTRAINT starting_xi_gw_selection_id_fkey FOREIGN KEY (gw_selection_id) REFERENCES public.gw_selections(id) ON DELETE CASCADE;


--
-- Name: transfers transfers_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transfers
    ADD CONSTRAINT transfers_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- Name: user_squads user_squads_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_squads
    ADD CONSTRAINT user_squads_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--


