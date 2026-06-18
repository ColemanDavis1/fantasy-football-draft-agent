"""Phase 1 CLI — verify data loads and print the auto-detected league config.

Run from the backend/ directory:

    python -m app.cli refresh                 # pull Sleeper players + trending
    python -m app.cli config --league-id 123  # read + confirm ESPN league config
    python -m app.cli players --pos RB -n 20   # peek at the loaded board
    python -m app.cli status                   # cache ages + row counts

The league ID is never hardcoded: pass --league-id (or set ESPN_LEAGUE_ID in
.env) only when you want to read a specific league, e.g. on draft day.
"""

from __future__ import annotations

import argparse
import json
import sys

from . import board, config, db, enrich, ingest
from .data import espn
from .engine import tiers, vorp


def _fmt_age(conn, key: str) -> str:
    from datetime import datetime, timezone
    val = db.get_meta(conn, key)
    if not val:
        return "never"
    try:
        ts = datetime.fromisoformat(val)
        delta = datetime.now(timezone.utc) - ts
        hrs = delta.total_seconds() / 3600
        return f"{hrs:.1f}h ago" if hrs < 48 else f"{hrs/24:.1f}d ago"
    except Exception:
        return val


def _resolve_league(conn, args) -> config.LeagueConfig | None:
    """Find a league to talk to: CLI/.env first, else the last saved league.
    Returns None if no league is known (tool stays league-agnostic)."""
    cfg = config.load_league_config(
        league_id=getattr(args, "league_id", None),
        season=getattr(args, "season", None),
        swid=getattr(args, "swid", None),
        espn_s2=getattr(args, "espn_s2", None),
        my_team_id=getattr(args, "my_team_id", None),
    )
    if cfg.has_league:
        return cfg
    row = conn.execute(
        "SELECT league_id, season FROM league_settings ORDER BY updated_at DESC LIMIT 1"
    ).fetchone()
    if not row:
        return None
    # Reuse env cookies for the saved league (private leagues).
    cfg.league_id = row["league_id"]
    cfg.season = row["season"]
    return cfg


def _refresh_projections(conn, args) -> None:
    """Pull real ESPN projections if we have a league; otherwise note the skip.
    Projection failures never abort a refresh — the engine falls back to the
    rank-based placeholder."""
    cfg = _resolve_league(conn, args)
    if not cfg or not cfg.has_league:
        print("Refreshing projections... skipped (no league configured; run "
              "`config --league-id <id>` first). Engine uses placeholder "
              "projections until then.")
        return
    print(f"Refreshing ESPN projections (league {cfg.league_id}, "
          f"season {cfg.season}, scoring-aware)...")
    try:
        psum = ingest.load_projections(
            conn, cfg.league_id, cfg.season, swid=cfg.swid, espn_s2=cfg.espn_s2)
        print(f"  projections: {psum['matched']} matched to board, "
              f"{psum['unmatched']} ESPN players unmatched "
              f"(of {psum['espn_players']} projected)")
    except PermissionError as e:
        print(f"  projections: SKIPPED — {e}")
    except Exception as e:
        print(f"  projections: SKIPPED — error talking to ESPN: {e}")


def cmd_refresh(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    print("Refreshing Sleeper player DB (cached to disk, refresh <=1x/day)...")
    psum = ingest.load_players(conn, force=args.force)
    src = "network" if psum["fetched_from_network"] else "disk cache"
    print(f"  players: {psum['kept_fantasy']} fantasy-relevant of "
          f"{psum['total']} total (from {src}); "
          f"bye weeks: {psum['bye_weeks_loaded']} teams")
    print("Refreshing Sleeper trending adds/drops...")
    tsum = ingest.load_trending(conn)
    print(f"  trending: {tsum.get('add',0)} adds, {tsum.get('drop',0)} drops")
    if not args.no_projections:
        _refresh_projections(conn, args)
    conn.close()
    print("Done.")
    return 0


def cmd_projections(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    cfg = _resolve_league(conn, args)
    if not cfg or not cfg.has_league:
        print("No league configured. Pass --league-id 123456 (or set "
              "ESPN_LEAGUE_ID / run `config` first).", file=sys.stderr)
        conn.close()
        return 2
    print(f"Pulling ESPN projections for league {cfg.league_id}, "
          f"season {cfg.season}...")
    try:
        psum = ingest.load_projections(
            conn, cfg.league_id, cfg.season, swid=cfg.swid, espn_s2=cfg.espn_s2)
    except PermissionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        conn.close()
        return 1
    except Exception as e:
        print(f"ERROR talking to ESPN: {e}", file=sys.stderr)
        conn.close()
        return 1
    cov = board.projection_coverage(conn)
    conn.close()
    print(f"  ESPN projected {psum['espn_players']} players; "
          f"{psum['matched']} matched, {psum['unmatched']} unmatched.")
    print(f"  Board coverage: {cov['real']}/{cov['total']} real, "
          f"{cov['placeholder']} on placeholder.")
    return 0


def cmd_config(args) -> int:
    cfg_rt = config.load_league_config(
        league_id=args.league_id, season=args.season,
        swid=args.swid, espn_s2=args.espn_s2, my_team_id=args.my_team_id,
    )
    if not cfg_rt.has_league:
        print("No league ID provided. Pass --league-id 123456 or set "
              "ESPN_LEAGUE_ID in .env.\nThe tool is league-agnostic until you "
              "give it one (e.g. on draft day).", file=sys.stderr)
        return 2

    print(f"Reading ESPN config for league {cfg_rt.league_id}, "
          f"season {cfg_rt.season} "
          f"({'private' if cfg_rt.is_private else 'public'})...\n")
    try:
        detected = espn.get_league_config(
            cfg_rt.league_id, cfg_rt.season,
            swid=cfg_rt.swid, espn_s2=cfg_rt.espn_s2,
            my_team_id=cfg_rt.my_team_id,
        )
    except PermissionError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"ERROR talking to ESPN: {e}", file=sys.stderr)
        return 1

    _print_detected_config(detected)

    if not args.no_save:
        conn = db.connect()
        db.init_db(conn)
        ingest.save_league_config(conn, detected)
        conn.close()
        print("\nSaved to league_settings + teams. "
              "Confirm the above looks right before drafting.")
    return 0


def _print_detected_config(cfg: dict) -> None:
    line = "=" * 64
    print(line)
    print(f"LEAGUE: {cfg.get('name') or '(unnamed)'}  "
          f"[id {cfg['league_id']}, season {cfg['season']}]")
    print(line)
    print(f"  Teams:        {cfg['num_teams']}")
    print(f"  Scoring:      {cfg['scoring_type']}  (reception = {cfg['ppr_value']} pts)")
    print(f"  Superflex:    {'YES' if cfg['is_superflex'] else 'no'}")
    print(f"  Draft type:   {cfg.get('draft_type')}")
    starters = cfg["starter_slots"]
    bench = cfg["roster_slots"].get("BE", 0)
    ir = cfg["roster_slots"].get("IR", 0)
    start_str = ", ".join(f"{n}x {s}" for s, n in starters.items())
    print(f"  Starters:     {start_str}")
    print(f"  Bench/IR:     {bench} BE, {ir} IR")
    print()
    print("  DRAFT ORDER / TEAMS:")
    me_found = any(t.get("is_me") for t in cfg["teams"])
    for t in cfg["teams"]:
        slot = t["draft_slot"]
        marker = "  <-- YOU" if t.get("is_me") else ""
        slot_str = f"#{slot:>2}" if slot else "  ?"
        owner = f" ({t['owner']})" if t.get("owner") else ""
        print(f"    {slot_str}  [team {t['team_id']:>2}] {t['name']}{owner}{marker}")
    if not me_found:
        print("\n  NOTE: 'YOU' not set. Re-run with --my-team-id <id> (or set "
              "MY_TEAM_ID) to flag your team and compute your draft slot.")


def cmd_enrich(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    try:
        if args.collect:
            res = enrich.collect(conn, wait=not args.no_wait)
            if res["status"] != "ended":
                print(f"Batch {res['batch_id']} status: {res['status']} "
                      f"(not ready). Re-run `enrich --collect` later.")
                conn.close()
                return 0
            print(f"Collected batch {res['batch_id']}: wrote {res['written']} "
                  f"enrichment notes ({res.get('errored', 0)} errored).")
        else:
            if args.no_wait:
                bid = enrich.submit(conn, limit=args.limit,
                                    use_web_search=not args.no_web_search)
                print(f"Submitted enrichment batch {bid} for top {args.limit} "
                      f"players. Collect later with `enrich --collect`.")
            else:
                print(f"Submitting enrichment batch (top {args.limit}, "
                      f"web search {'off' if args.no_web_search else 'on'}); "
                      f"waiting for results (Batch API, ~minutes)...")
                res = enrich.run(conn, limit=args.limit,
                                 use_web_search=not args.no_web_search, wait=True)
                if res.get("status") == "ended":
                    print(f"Done: wrote {res['written']} notes "
                          f"({res.get('errored', 0)} errored).")
                else:
                    print(f"Batch {res['batch_id']} still {res.get('status')}. "
                          f"Collect later with `enrich --collect`.")
    except RuntimeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        conn.close()
        return 1
    except Exception as e:
        print(f"ERROR running enrichment: {e}", file=sys.stderr)
        conn.close()
        return 1
    cov = enrich.coverage(conn)
    print(f"Enrichment coverage: {cov['enriched']}/{cov['total']} players.")
    conn.close()
    return 0


def cmd_players(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    where, params = "WHERE active=1", []
    if args.pos:
        where += " AND position=?"
        params.append(args.pos.upper())
    rows = conn.execute(
        f"""SELECT full_name, position, team, bye_week, search_rank, adp,
                   injury_status
            FROM players {where}
            ORDER BY (search_rank IS NULL), search_rank ASC
            LIMIT ?""",
        (*params, args.n),
    ).fetchall()
    if not rows:
        print("No players loaded. Run: python -m app.cli refresh")
        conn.close()
        return 1
    print(f"{'RANK':>5}  {'POS':<4} {'NAME':<26} {'TEAM':<4} {'BYE':>3}  {'INJ'}")
    for r in rows:
        rank = r["search_rank"] if r["search_rank"] is not None else "-"
        print(f"{str(rank):>5}  {r['position'] or '?':<4} "
              f"{(r['full_name'] or '?')[:26]:<26} {r['team'] or '-':<4} "
              f"{str(r['bye_week'] or '-'):>3}  {r['injury_status'] or ''}")
    conn.close()
    return 0


def cmd_board(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    players = board.load_engine_players(conn)
    if not players:
        print("No players loaded. Run: python -m app.cli refresh")
        conn.close()
        return 1
    league, src, _ = board.load_league_settings(conn)
    cov = board.projection_coverage(conn)
    conn.close()

    vorp.assign_vorp(players, league)
    tiers.assign_tiers(players)
    pool = [p for p in players if (not args.pos or p.position == args.pos.upper())]
    pool.sort(key=lambda p: (p.vorp if p.vorp is not None else -1e9), reverse=True)

    if cov["real"]:
        proj_note = (f"real ESPN projections ({cov['real']}/{cov['total']}; "
                     f"{cov['placeholder']} on placeholder)")
    else:
        proj_note = "PLACEHOLDER projections (run `projections` with a league)"
    print(f"VORP board using {src}.  Projections: {proj_note}.")
    print(f"{'VORP':>6} {'POS':<4} {'TIER':>4}  {'NAME':<26} {'TEAM':<4} {'BYE':>3} {'ADP':>5}")
    for p in pool[:args.n]:
        print(f"{(p.vorp or 0):>6.0f} {p.position:<4} {str(p.tier):>4}  "
              f"{p.name[:26]:<26} {p.team or '-':<4} {str(p.bye_week or '-'):>3} "
              f"{p.adp:>5.0f}")
    return 0


def cmd_status(args) -> int:
    conn = db.connect()
    db.init_db(conn)
    print(f"DB: {config.DB_PATH}")
    print(f"  schema_version:           {db.get_meta(conn, 'schema_version')}")
    print(f"  players last refresh:     {_fmt_age(conn, 'sleeper_players_last_refresh')}")
    print(f"  trending last refresh:    {_fmt_age(conn, 'sleeper_trending_last_refresh')}")
    proj_season = db.get_meta(conn, "espn_projections_season")
    print(f"  projections last refresh: {_fmt_age(conn, 'espn_projections_last_refresh')}"
          f"{f' (season {proj_season})' if proj_season else ''}")
    cov = board.projection_coverage(conn)
    if cov["total"]:
        print(f"  projection coverage:      {cov['real']}/{cov['total']} real, "
              f"{cov['placeholder']} placeholder")
    print(f"  enrichment last collect:  {_fmt_age(conn, 'enrich_last_collected_at')}")
    ecov = enrich.coverage(conn)
    if ecov["total"]:
        print(f"  enrichment coverage:      {ecov['enriched']}/{ecov['total']} players")
    for table in ("players", "trending", "league_settings", "teams",
                  "picks", "rosters", "opponent_profiles"):
        n = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()["c"]
        print(f"  {table:<22} {n:>6} rows")
    conn.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="app.cli",
                                description="Fantasy Draft Agent — Phase 1 data/config CLI")
    sub = p.add_subparsers(dest="command", required=True)

    def add_league_args(sp):
        sp.add_argument("--league-id")
        sp.add_argument("--season", type=int)
        sp.add_argument("--swid", help="private leagues only")
        sp.add_argument("--espn-s2", help="private leagues only")
        sp.add_argument("--my-team-id", type=int)

    pr = sub.add_parser("refresh", help="Pull Sleeper players + trending + ESPN projections")
    pr.add_argument("--force", action="store_true", help="ignore the 24h cache")
    pr.add_argument("--no-projections", action="store_true",
                    help="skip the ESPN projection pull")
    add_league_args(pr)
    pr.set_defaults(func=cmd_refresh)

    pc = sub.add_parser("config", help="Read + print ESPN league config")
    add_league_args(pc)
    pc.add_argument("--no-save", action="store_true", help="print only, don't write DB")
    pc.set_defaults(func=cmd_config)

    pj = sub.add_parser("projections", help="Pull real ESPN projections (league-scored)")
    add_league_args(pj)
    pj.set_defaults(func=cmd_projections)

    pe = sub.add_parser("enrich", help="Night-before Claude enrichment pass (Batch API)")
    pe.add_argument("--limit", type=int, default=150,
                    help="how many top players to enrich (default 150)")
    pe.add_argument("--no-web-search", action="store_true",
                    help="skip the web_search tool (cheaper, less current)")
    pe.add_argument("--no-wait", action="store_true",
                    help="submit only; collect later with --collect")
    pe.add_argument("--collect", action="store_true",
                    help="fetch results for a previously submitted batch")
    pe.set_defaults(func=cmd_enrich)

    pp = sub.add_parser("players", help="Peek at the loaded board")
    pp.add_argument("--pos", help="filter by position (QB/RB/WR/TE/K/DEF)")
    pp.add_argument("-n", type=int, default=25)
    pp.set_defaults(func=cmd_players)

    pb = sub.add_parser("board", help="VORP + tiers on the live board (placeholder projections)")
    pb.add_argument("--pos", help="filter by position (QB/RB/WR/TE/K/DEF)")
    pb.add_argument("-n", type=int, default=30)
    pb.set_defaults(func=cmd_board)

    ps = sub.add_parser("status", help="Cache ages + row counts")
    ps.set_defaults(func=cmd_status)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
