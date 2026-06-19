# Upgrade: encode evidence-based draft strategy into the engine

Read the codebase first before changing anything — especially `backend/app/engine/` (`vorp.py`, `recommend.py`, `tiers.py`, `projections.py`, `survival.py`, `profiler.py`, `slots.py`, `draftflow.py`), plus `backend/app/enrich.py`, `priors.py`, and `config.py`. Tell me back, briefly, what each does and confirm my read of the gaps below before implementing.

The engine is already strong (VORP with league-aware replacement baselines, tier/cliff detection, opponent-aware survival probability, positional-run detection, roster-need/depth weighting, archetype profiling). Do NOT rewrite that. Keep the engine pure/deterministic, keep everything league-agnostic and config-driven, and add tests in `backend/tests/` for every new piece. Put all new tunable numbers in a single documented constants block per module — no magic numbers scattered around.

Implement these upgrades. Each maps to an evidence-based finding; treat thresholds as tunable.

## 1. Configurable baseline method (vorp.py)
Today's replacement level counts only league starters (dedicated + greedy flex) — effectively VOLS. Add a `baseline_method` option: `"vols"` (current behavior, default unchanged) and `"beer"` (man-games / games-based: push the baseline deeper to account for byes + injuries, ~1.3–1.5× the starter count per position, tunable). Expose it via config/CLI. Rationale: starter-only baselines are empirically too shallow; the BEER/man-games baseline balances starters vs. bench and is the recommended default for most leagues.

## 2. PPR value-shape adjustment layer (new module: engine/value_adjust.py)
Apply multiplicative adjustments to projections (or a parallel `adj_points`) before VORP, gated by the league's actual points-per-reception so it self-disables in standard scoring:
- Boost pass-catching RBs (high projected target/reception share) — a target is worth ~3× a carry in full PPR; receiving backs carry a higher floor.
- Slightly fade interchangeable mid-tier WRs (deep, flat WR curve) and reward genuine high-target WRs.
Keep adjustments small and bounded; log what was adjusted so the rationale can cite it.

## 3. Age-curve adjustment (enrich.py / priors.py + value_adjust.py)
Pull player age (ESPN/Sleeper player data). Apply: discount RBs aging into 28+ (steep ~age-29 cliff, ~-15 to -25% tunable), prefer ascending 2nd–3rd-year RBs; reward WRs with high NFL draft capital and the year-2/year-3 breakout window. Make it a clearly labeled, tunable adjustment, not a hard override.

## 4. "First-or-last" QB + rushing-QB exception (recommend.py)
QB depth value is already low (0.15). Add: (a) a rushing-QB upside flag (from enrichment/projected rush yards) that lets a top dual-threat QB be recommended earlier; (b) a mild penalty on drafting a *pocket* QB in the mid-rounds (the dead zone between elite and streamer). Gate fully on 1-QB vs. superflex — in superflex, invert and prioritize QB early. Reflect this in the rationale.

## 5. TE elite-or-punt shaping (recommend.py / tiers.py)
TE cap is already 2. Add an explicit penalty on mid-tier TEs (below the top elite tier, above streamer level) so the engine recommends a top-~2–3 TE early OR defers to a streamer, never the middle tier. Surface "elite TE value" vs. "punt-and-stream" in the rationale.

## 6. Ceiling-early / floor-late weighting (recommend.py)
Add a round-phase modifier: in early rounds (configurable, ~1–4) up-weight ceiling/upside (use existing sleeper/upside flags from enrichment); in later rounds up-weight floor/weekly-startable stability. Keep it bounded so it never overrides a clearly higher-VORP player.

## 7. My-team strategy lean + emergent pivot (recommend.py)
The profiler classifies opponents' archetypes; add the same awareness for MY roster. Default lean = Hero/Anchor RB (favor one strong RB early, then hoard WR, soft-target ~5–6 RB total), but let it PIVOT to a Zero-RB-style WR-heavy build when elite WR value is cascading and the format supports it (full PPR, shallower league). This must be an emergent nudge layered on VORP + survival, NOT a hard-coded script. Show the detected lean in the recommendation output.

## 8. Handcuff / leverage value (profiler.py or new helper)
Add modest value for: handcuffing my own elite, injury-exposed workhorse RB with a clear-cut backup; and "leverage" handcuffs (high-upside backups in ambiguous backfields I don't own) as cheap upside in later rounds. Small, late-round-weighted bonus only.

## 9. Surface an explicit VONA signal (recommend.py output)
You already compute opponent-aware survival (superior to naive ADP). Additionally surface a simple VONA number per candidate — projection minus the best same-position player likely available at my next pick — as a complementary "draft now vs. wait" readout in the shortlist/overlay.

## Process
1. Confirm the gap analysis and propose a file-by-file plan; wait for my OK before large changes.
2. Implement incrementally in this order: baseline option → value_adjust layer → age curves → QB/TE shaping → ceiling/floor → strategy lean → handcuffs → VONA surfacing.
3. Add/extend tests (`test_engine.py`, `test_projections.py`, plus new ones) covering the value math and a short simulated snake draft. Keep `python -m app.engine.demo` working.
4. Update `README.md` with the new tunable knobs and the strategy rationale, and note that VORP is a median estimate (optimal drafting is a real but modest edge) and that values should be re-derived from current-season ADP/projections each year.
