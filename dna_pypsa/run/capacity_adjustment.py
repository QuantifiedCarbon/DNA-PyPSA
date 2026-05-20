"""Profitability-based capacity feedback for extendable generators."""

import logging

import numpy as np
import pandas as pd
import pypsa

from dna_pypsa import constants

logger = logging.getLogger(__name__)


def _calc_generator_profit(
    gen: pd.Series,
    gen_name: str,
    network: pypsa.Network,
) -> dict | None:
    """Compute per-MW profit for a single extendable generator.

    Parameters
    ----------
    gen : pd.Series
        Generator row from ``network.generators``.
    gen_name : str
        Generator name (index label).
    network : pypsa.Network
        Solved network with dispatch and prices.

    Returns
    -------
    dict | None
        Profit result dict, or None if the generator should be skipped
        (not extendable, too small, or missing dispatch/price data).

    """
    if not gen.get("is_extendable", False):
        return None
    p_nom = float(gen["p_nom"])
    if p_nom < 1.0:
        return None
    bus = gen["bus"]
    if gen_name not in network.generators_t.p.columns:
        logger.warning(
            "  %s: extendable generator has no dispatch data — skipping"
            " capacity adjustment (p_nom=%.0f MW, check p_max_pu / variability)",
            gen_name,
            p_nom,
        )
        return None
    if bus not in network.buses_t.marginal_price.columns:
        logger.warning(
            "  %s: bus %s has no marginal price data — skipping capacity adjustment",
            gen_name,
            bus,
        )
        return None

    dispatch = network.generators_t.p[gen_name]
    prices = network.buses_t.marginal_price[bus]
    annual_revenue = float((dispatch * prices).sum())

    mc_base = float(gen.get("marginal_cost", 0.0))
    mc_quad = float(gen.get("marginal_cost_quadratic", 0.0))
    annual_variable_cost = float((mc_base * dispatch + mc_quad * dispatch**2).sum())

    capital_cost = float(gen.get("capital_cost", 0.0))
    fom_cost = float(gen.get("fom_cost", 0.0))
    fixed_cost_per_mw = capital_cost + fom_cost

    profit_per_mw = (annual_revenue - annual_variable_cost) / p_nom - fixed_cost_per_mw

    return {
        "p_nom_old": p_nom,
        "p_nom_new": p_nom,
        "profit_per_mw": profit_per_mw,
        "annual_revenue": annual_revenue,
        "annual_variable_cost": annual_variable_cost,
        "annual_fixed_cost": fixed_cost_per_mw * p_nom,
        "fixed_cost_per_mw": fixed_cost_per_mw,
        "hit_bound": None,
        "bus": bus,
        "p_nom": p_nom,
    }


def _apply_fast_retirement(
    gen: pd.Series,
    gen_name: str,
    network: pypsa.Network,
    profit_info: dict,
) -> dict | None:
    """Retire a generator to p_nom_min if fast-retirement criteria are met.

    Parameters
    ----------
    gen : pd.Series
        Generator row from ``network.generators``.
    gen_name : str
        Generator name.
    network : pypsa.Network
        Network to modify in-place.
    profit_info : dict
        Output of :func:`_calc_generator_profit` for this generator.

    Returns
    -------
    dict | None
        Updated result dict if retired, else None (no action taken).

    """
    profit_per_mw = profit_info["profit_per_mw"]
    if profit_per_mw >= 0:
        return None

    annual_revenue = profit_info["annual_revenue"]
    annual_variable_cost = profit_info["annual_variable_cost"]
    p_nom = profit_info["p_nom"]
    fixed_cost_per_mw = profit_info["fixed_cost_per_mw"]

    revenue_per_mw = (annual_revenue - annual_variable_cost) / p_nom
    retire = revenue_per_mw < 0
    if not retire and fixed_cost_per_mw > 0:
        raw_severity = abs(profit_per_mw) / fixed_cost_per_mw
        retire = raw_severity >= constants.CAP_FAST_RETIRE_SEVERITY

    if not retire:
        return None

    p_nom_min = float(gen.get("p_nom_min", 0.0))
    target = max(p_nom_min, 1.0)
    reason = (
        "revenue < variable cost"
        if revenue_per_mw < 0
        else f"loss severity {abs(profit_per_mw) / fixed_cost_per_mw:.1f}× fixed cost"
    )
    logger.info(
        "  %s: fast retirement (%s, %.0f EUR/MW/yr)",
        gen_name,
        reason,
        profit_per_mw,
    )
    network.generators.loc[gen_name, "p_nom"] = target
    # Zero out the quadratic term rather than recalculating 2*width/target.
    # At p_nom_min (typically 1 MW) the coefficient becomes enormous and ruins
    # QP conditioning for the entire model — a retired generator contributes
    # negligibly to the objective so the supply-curve shape is irrelevant.
    network.generators.loc[gen_name, "marginal_cost_quadratic"] = 0.0

    result = dict(profit_info)
    result["p_nom_new"] = target
    result["hit_bound"] = "min"
    return result


def _oscillation_depth(
    cap_history: dict[str, list[dict]],
    gen_name: str,
) -> int:
    """Return total direction-alternating step count across the full cap_history.

    Counts all adjacent pairs of diffs with opposite sign (direction flips)
    across the entire history, not just the most recent consecutive run.  This
    means depth never resets: a single off-beat step (two contractions in a
    row) does not undo accumulated damping, it simply fails to increment.

    Returns 0 until constants.CAP_OSCILLATION_MIN_FLIPS flips have been observed.
    Returns 1 at exactly MIN_FLIPS, then increments by one per additional flip.
    The caller applies ``constants.CAP_OSCILLATION_DAMPING ** depth`` to
    ``gen_max_step_fraction`` so depth monotonically drives the step size down.
    """
    if not cap_history or gen_name not in cap_history:
        return 0
    entries = cap_history[gen_name]
    if len(entries) < constants.CAP_OSCILLATION_MIN_FLIPS + 1:
        return 0
    pnoms = [e["p_nom"] for e in entries]
    diffs = [pnoms[i + 1] - pnoms[i] for i in range(len(pnoms) - 1)]
    n_flips = sum(
        1
        for i in range(1, len(diffs))
        if diffs[i] != 0 and diffs[i - 1] != 0 and (diffs[i] > 0) != (diffs[i - 1] > 0)
    )
    if n_flips < constants.CAP_OSCILLATION_MIN_FLIPS:
        return 0
    return n_flips - constants.CAP_OSCILLATION_MIN_FLIPS + 1


def adjust_capacity(
    network: pypsa.Network,
    max_step_fraction: float = 0.05,
    min_step_mw: float = 200.0,
    cap_history: dict[str, list[dict]] | None = None,
) -> dict[str, dict]:
    """Adjust p_nom for originally-extendable generators based on profitability.

    For each generator with ``is_extendable == True``, compute per-MW profit
    (revenue minus variable cost minus annualised fixed cost).  If negative,
    reduce ``p_nom``; if positive and below ``p_nom_max``, increase it.

    Uses proportional steps with market-share damping.  No secant acceleration
    is applied — secant was removed because the extrapolated slope from
    small-capacity data is invalidated when the capacity mix itself shifts
    prices, causing the algorithm to converge to the wrong equilibrium (e.g.
    retiring nuclear as wind over-expands).

    Parameters
    ----------
    network : pypsa.Network
        A solved PyPSA network (must have dispatch and prices available).
    max_step_fraction : float
        Maximum p_nom change per iteration as a fraction of current p_nom,
        by default 0.05 (5%).
    min_step_mw : float
        Minimum absolute step size in MW, by default 200.  The effective
        max step is ``max(max_step_fraction * p_nom, min_step_mw)``, so
        generators below ``min_step_mw / max_step_fraction`` (4 GW at
        defaults) use the absolute floor instead of the percentage.
    cap_history : dict[str, list[dict]] | None
        Per-generator history (retained for API compatibility but no longer
        used for secant acceleration).  By default None.

    Returns
    -------
    dict[str, dict]
        Dictionary mapping generator names to adjustment results.

    """
    if "is_extendable" not in network.generators.columns:
        logger.info("No is_extendable column — skipping capacity adjustment.")
        return {}

    slack_cols = [c for c in network.generators_t.p.columns if "slack" in c]
    slack_active = bool(
        slack_cols and (network.generators_t.p[slack_cols] > 1e-2).any().any()
    )
    if slack_active:
        logger.warning(
            "Slack generators are active — solve is infeasible without them."
            " Skipping fast retirement to avoid acting on unreliable prices."
        )

    # Pre-compute zone capacities for market-share damping
    zone_capacities = network.generators.groupby("bus")["p_nom"].sum().to_dict()

    results = {}

    for gen_name in network.generators.index:
        gen = network.generators.loc[gen_name]

        profit_info = _calc_generator_profit(gen, gen_name, network)
        if profit_info is None:
            continue

        p_nom = profit_info["p_nom"]
        bus = profit_info["bus"]
        profit_per_mw = profit_info["profit_per_mw"]
        fixed_cost_per_mw = profit_info["fixed_cost_per_mw"]

        _internal_keys = ("p_nom", "bus", "fixed_cost_per_mw")
        result = {k: v for k, v in profit_info.items() if k not in _internal_keys}

        # Profit tolerance: skip adjustment when near break-even.  Prevents
        # endless tiny nudges when a generator is close to equilibrium.
        tolerance = constants.CAP_PROFIT_TOLERANCE * max(
            fixed_cost_per_mw,
            constants.CAP_MIN_REFERENCE_COST,
        )
        if 0 <= profit_per_mw < tolerance:
            logger.info(
                "  %s: within tolerance at %.0f MW (profit %+.0f,"
                " tolerance +%.0f EUR/MW/yr) — no adjustment",
                gen_name,
                p_nom,
                profit_per_mw,
                tolerance,
            )
            results[gen_name] = result
            continue

        # Fast retirement: delegate to shared helper.
        if not slack_active:
            retired = _apply_fast_retirement(gen, gen_name, network, profit_info)
            if retired is not None:
                results[gen_name] = retired
                continue

        # Insignificance retirement: loss-making generators below the minimum
        # meaningful capacity are parked immediately.  At this scale their
        # contribution to the system is negligible and grinding through small
        # proportional steps (or oscillating) wastes iterations.
        if profit_per_mw < 0 and p_nom < constants.CAP_INSIGNIFICANT_MW:
            p_nom_min = float(gen.get("p_nom_min", 0.0))
            target = max(p_nom_min, 1.0)
            logger.info(
                "  %s: insignificant (%.0f MW < %.0f MW threshold, loss"
                " %.0f EUR/MW/yr) — parking at %.0f MW",
                gen_name,
                p_nom,
                constants.CAP_INSIGNIFICANT_MW,
                profit_per_mw,
                target,
            )
            network.generators.loc[gen_name, "p_nom"] = target
            network.generators.loc[gen_name, "marginal_cost_quadratic"] = 0.0
            result["p_nom_new"] = target
            result["hit_bound"] = "min"
            results[gen_name] = result
            continue

        # Progressive oscillation damping — apply constants.CAP_OSCILLATION_DAMPING once
        # per consecutive alternating step beyond constants.CAP_OSCILLATION_MIN_FLIPS so
        # that persistent oscillation drives the step fraction toward zero and
        # eventually below the convergence threshold.
        gen_max_step_fraction = max_step_fraction
        osc_depth = _oscillation_depth(cap_history, gen_name)
        is_oscillating = osc_depth > 0
        if is_oscillating:
            gen_max_step_fraction = max_step_fraction * (
                constants.CAP_OSCILLATION_DAMPING**osc_depth
            )
            logger.info(
                "  %s: oscillation depth %d — step fraction %.3f%%",
                gen_name,
                osc_depth,
                gen_max_step_fraction * 100,
            )

        # Detect post-contraction state: if a generator has ever been more than
        # one min_step larger than its current p_nom it is searching for
        # equilibrium, not bootstrapping from zero.  Suppress the 200 MW
        # min_step floor and the small-generator fraction for the duration —
        # permanently, not just for a short lookback window — so it cannot
        # overshoot on any subsequent profitable step.
        gen_history = (cap_history or {}).get(gen_name, [])
        max_historical_pnom = max((e["p_nom"] for e in gen_history), default=0.0)
        has_contracted = max_historical_pnom > p_nom + min_step_mw

        # Step calculation: base fraction of p_nom, scaled up when the profit
        # signal is strong.  The scaling threshold differs by direction:
        # - Retirement (profit < 0): severity > 0.5 to avoid premature shrinkage
        # - Expansion  (profit > 0): severity > 0.2 so profitable generators
        #   converge quickly without waiting for the loss-oriented threshold
        # Small-generator fraction boosts expansion for genuine bootstrapping
        # (1 MW → meaningful scale).  Suppress it when has_contracted or
        # oscillating: those generators are finding equilibrium, not
        # bootstrapping, and the boosted fraction causes large overshoots.
        if (
            profit_per_mw >= 0
            and p_nom < constants.CAP_SMALL_GENERATOR_THRESHOLD_MW
            and not (has_contracted or is_oscillating)
        ):
            effective_fraction = constants.CAP_SMALL_GENERATOR_FRACTION
        else:
            effective_fraction = gen_max_step_fraction
        if fixed_cost_per_mw > 0:
            severity = min(abs(profit_per_mw) / fixed_cost_per_mw, 3.0)
            threshold = 0.5 if profit_per_mw < 0 else 0.2
            if severity > threshold:
                effective_fraction = effective_fraction * (1.0 + severity)
        # For retirement, cap the absolute floor at the proportional step so
        # oscillation damping is effective below ~4 GW (without this,
        # min_step_mw dominates and halving gen_max_step_fraction has no
        # effect on max_step).  For expansion, keep the full min_step_mw so
        # tiny profitable generators can reach meaningful scale quickly.
        # Suppress the floor for post-contraction generators for the same reason.
        if profit_per_mw < 0 or is_oscillating or has_contracted:
            effective_min_step_mw = min(min_step_mw, gen_max_step_fraction * p_nom)
        else:
            effective_min_step_mw = min_step_mw
        max_step = max(effective_fraction * p_nom, effective_min_step_mw)

        # If oscillation damping has reduced the maximum possible step below
        # the model's meaningful precision, the generator is effectively at
        # equilibrium — further solves would move it by a sub-MW rounding error.
        # Only applies when oscillating: a steadily-retiring small generator
        # must continue contracting toward the insignificance threshold.
        if is_oscillating and max_step < constants.CAP_MIN_ABSOLUTE_STEP_MW:
            logger.info(
                "  %s: step ceiling %.2f MW < %.1f MW precision floor — converged",
                gen_name,
                max_step,
                constants.CAP_MIN_ABSOLUTE_STEP_MW,
            )
            results[gen_name] = result
            continue

        # Market-share damping — prevents large price-setters from overshooting
        # equilibrium.  Relaxed toward 1.0 in both directions when the signal
        # is strong: for deeply unprofitable generators (retirement) and for
        # highly profitable ones (expansion) where slowing is unnecessary.
        zone_cap = zone_capacities.get(bus, p_nom)
        market_share = p_nom / zone_cap if zone_cap > 0 else 1.0
        base_damping = 1.0 / (1.0 + market_share * constants.CAP_MARKET_SENSITIVITY)
        reference_cost = max(fixed_cost_per_mw, constants.CAP_MIN_REFERENCE_COST)
        if profit_per_mw >= 0:
            # Relax toward 1.0 as profitability grows — mirrors retirement logic.
            t = min(profit_per_mw / reference_cost, 1.0)
            damping = base_damping + t * (1.0 - base_damping)
        elif profit_per_mw <= constants.CAP_DAMPING_THRESHOLD:
            damping = 1.0
        else:
            t = (
                profit_per_mw / constants.CAP_DAMPING_THRESHOLD
            )  # 0→1 as profit goes 0→threshold
            damping = base_damping + t * (1.0 - base_damping)

        # Proportional base step.
        reference = max(fixed_cost_per_mw, constants.CAP_MIN_REFERENCE_COST)
        normalised_gap = profit_per_mw / reference
        gain = (
            constants.CAP_RETIRE_GAIN
            if profit_per_mw < 0
            else constants.CAP_EXPAND_GAIN
        )
        adjustment = normalised_gap * max_step * gain * damping
        adjustment = float(np.clip(adjustment, -max_step, max_step))
        method = "proportional"

        # Secant acceleration for retirement (profit < 0) and expansion (profit > 0).
        #
        # Expansion secant was previously disabled because at small capacity wind
        # earns scarcity rents that don't extrapolate to the large-capacity regime.
        # It is now re-enabled with two guards that contain this risk:
        #   1. p_nom >= constants.CAP_SMALL_GENERATOR_THRESHOLD_MW — blocked below 1 GW so the
        #      early scarcity-rent slope is never used.
        #   2. dp > 0 and dcap > 0 enforced — profit must be declining as capacity
        #      grows (normal market dynamics); perverse slopes are rejected.
        # With domestic fraction scaling reducing cross-border inflation, the
        # profit-capacity relationship in the high-capacity regime is sufficiently
        # linear for the secant to extrapolate reliably.
        if cap_history is not None and gen_name in cap_history:
            history = cap_history[gen_name]
            for prev in reversed(history[-constants.CAP_SECANT_LOOKBACK :]):
                dp = prev["profit_per_mw"] - profit_per_mw
                dcap = prev["p_nom"] - p_nom
                if (
                    abs(dp) >= constants.CAP_SECANT_MIN_PROFIT_CHANGE
                    and abs(dcap) >= constants.CAP_SECANT_MIN_PNOM_CHANGE
                ):
                    secant_target = p_nom - profit_per_mw * dcap / dp
                    secant_adj = secant_target - p_nom
                    if profit_per_mw < 0 and secant_adj < 0:
                        # Retirement: reject perverse slope (secant expanding a
                        # loss-maker — price-taker regime).
                        adjustment = float(
                            np.clip(
                                secant_adj,
                                -constants.CAP_SECANT_MAX_STEP_MULTIPLIER_RETIRE
                                * max_step,
                                0.0,
                            ),
                        )
                        method = "secant"
                    elif (
                        profit_per_mw > 0
                        and p_nom >= constants.CAP_SMALL_GENERATOR_THRESHOLD_MW
                        and dp > 0
                        and dcap < 0
                        and secant_adj > 0
                    ):
                        # Expansion: profit must be declining as capacity grew.
                        # dcap = prev["p_nom"] - p_nom < 0 because prev was smaller
                        # (generator was expanding); dp > 0 because profit was higher
                        # at that lower capacity — normal market dynamics.
                        adjustment = float(
                            np.clip(
                                secant_adj,
                                0.0,
                                constants.CAP_SECANT_MAX_STEP_MULTIPLIER_EXPAND
                                * max_step,
                            ),
                        )
                        method = "secant"
                    break

        # Apply with bounds
        new_p_nom = p_nom + adjustment
        p_nom_min = float(gen.get("p_nom_min", 0.0))
        p_nom_max = float(gen.get("p_nom_max", float("inf")))
        hit_bound = None

        if new_p_nom <= max(p_nom_min, 1.0):
            new_p_nom = max(p_nom_min, 1.0)
            hit_bound = "min"
        elif new_p_nom >= p_nom_max:
            new_p_nom = p_nom_max
            hit_bound = "max"

        # Sub-precision guard: steps below constants.CAP_MIN_ABSOLUTE_STEP_MW are
        # rounding noise at country scale — treat as unchanged so they don't
        # keep the convergence loop alive.
        if abs(new_p_nom - p_nom) < constants.CAP_MIN_ABSOLUTE_STEP_MW:
            logger.info(
                "  %s: step %.2f MW < %.1f MW precision floor — treating as unchanged"
                " (p_nom=%.0f MW, profit %+.0f EUR/MW/yr)",
                gen_name,
                abs(new_p_nom - p_nom),
                constants.CAP_MIN_ABSOLUTE_STEP_MW,
                p_nom,
                profit_per_mw,
            )
            results[gen_name] = result
            continue

        # Update network
        network.generators.loc[gen_name, "p_nom"] = new_p_nom

        # Recalculate marginal_cost_quadratic. Zero it out for near-retired
        # generators (below constants.CAP_MCQ_ZERO_THRESHOLD_MW) — at small p_nom the
        # coefficient width/p_nom becomes large enough to degrade QP
        # conditioning even when the generator is not at its hard minimum bound.
        supply_curve_width = gen.get("supply_curve_width", 0.0)
        if hit_bound == "min" or new_p_nom <= constants.CAP_MCQ_ZERO_THRESHOLD_MW:
            network.generators.loc[gen_name, "marginal_cost_quadratic"] = 0.0
        elif pd.notna(supply_curve_width) and supply_curve_width > 0:
            new_mcq = supply_curve_width / new_p_nom
            network.generators.loc[gen_name, "marginal_cost_quadratic"] = new_mcq

        change_pct = 100.0 * (new_p_nom - p_nom) / p_nom
        bound_note = f" [{hit_bound} bound]" if hit_bound else ""
        logger.info(
            "  %s: p_nom %.0f→%.0f MW (%+.1f%%) | profit %+.0f EUR/MW/yr [%s]%s",
            gen_name,
            p_nom,
            new_p_nom,
            change_pct,
            profit_per_mw,
            method,
            bound_note,
        )

        result["p_nom_new"] = new_p_nom
        result["hit_bound"] = hit_bound
        results[gen_name] = result

    return results
