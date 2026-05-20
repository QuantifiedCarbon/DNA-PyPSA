"""Dynamic unit commitment approximation using cgrid's capture-price approach.

This module implements a two-stage optimization approach to approximate unit commitment
decisions for aggregated generators (like country-level generation portfolios) without
the computational cost of explicit unit commitment constraints.

The cgrid approach:
1. Solve the model to get shadow prices and dispatch
2. Split time series into chunks (default 104 ≈ weekly periods)
3. For each chunk, calculate capture price (generation-weighted average)
4. Compare capture price to variable cost
5. If capture price > variable cost: increase variability by step_size
6. If capture price < variable cost: decrease variability by step_size
7. Smooth with 24-hour moving average
8. Clip to min/max bounds
9. Apply to both p_max_pu (variability) and p_min_pu
10. Re-solve with adjusted parameters
"""

import logging
from collections.abc import Callable

import numpy as np
import pandas as pd
import pypsa

from dna_pypsa import constants

# Get logger - will inherit handlers (including file handler) from root logger
logger = logging.getLogger(__name__)


def calculate_capture_price(
    generation: np.ndarray,
    prices: np.ndarray,
) -> float:
    """Calculate capture price (generation-weighted average price).

    Parameters
    ----------
    generation : np.ndarray
        Generation values for a time period.
    prices : np.ndarray
        Marginal prices for the same time period.

    Returns
    -------
    float
        Capture price = sum(generation * price) / sum(generation)

    """
    total_generation = np.sum(generation)
    if total_generation == 0:
        return 0.0
    return np.sum(generation * prices) / total_generation


def _adaptive_step(
    capture_price: float,
    avg_variable_cost: float,
    current_mean_v: float,
    max_step: float,
    gen_capacity: float,
    zone_capacity: float,
    chunk_history: list[dict] | None,
    chunk_idx: int,
) -> float:
    """Compute variability adjustment using proportional steps with secant acceleration.

    Uses market-share damping so large aggregated generators (which are
    price-setters) take smaller steps to avoid oscillation.  When history
    from a previous iteration is available, a secant extrapolation predicts
    the variability at which capture price equals variable cost.

    Parameters
    ----------
    capture_price : float
        Generation-weighted average price for this chunk.
    avg_variable_cost : float
        Mean variable cost for this chunk.
    current_mean_v : float
        Current mean variability (p_max_pu) for this chunk.
    max_step : float
        Maximum allowed adjustment magnitude.
    gen_capacity : float
        Generator rated capacity (p_nom) in MW.
    zone_capacity : float
        Total generation capacity on the generator's bus.
    chunk_history : list[dict] | None
        Previous iteration per-chunk data for secant method.
    chunk_idx : int
        Index of the current chunk.

    Returns
    -------
    float
        Signed variability adjustment for this chunk.

    """
    gap = capture_price - avg_variable_cost

    # Market-share damping: large generators get smaller steps
    if gen_capacity > 0 and zone_capacity > 0:
        market_share = gen_capacity / zone_capacity
        damping = 1.0 / (1.0 + market_share * constants.VAR_MARKET_SENSITIVITY)
    else:
        damping = 1.0

    # Try secant if history available
    if (
        chunk_history is not None
        and chunk_idx < len(chunk_history)
        and chunk_history[chunk_idx] is not None
    ):
        prev = chunk_history[chunk_idx]
        prev_cp = prev["capture_price"]
        prev_v = prev["mean_variability"]
        dcp = capture_price - prev_cp
        dv = current_mean_v - prev_v

        if (
            abs(dcp) > constants.VAR_SECANT_MIN_CAPTURE_PRICE_CHANGE
            and abs(dv) > constants.VAR_SECANT_MIN_VARIABILITY_CHANGE
        ):
            target_v = current_mean_v + (avg_variable_cost - capture_price) * dv / dcp
            adjustment = (target_v - current_mean_v) * damping
            return float(np.clip(adjustment, -max_step, max_step))

    # Proportional fallback
    reference = max(abs(avg_variable_cost), constants.VAR_MIN_REFERENCE_PRICE)
    normalised_gap = gap / reference
    adjustment = normalised_gap * max_step * constants.VAR_PROPORTIONAL_GAIN * damping
    return float(np.clip(adjustment, -max_step, max_step))


def adjust_variability(
    generation: pd.Series,
    prices: pd.Series,
    current_variability: pd.Series,
    variable_cost: pd.Series,
    n_chunks: int = 104,
    step_size: float = 0.02,
    smoothing_window: int = 24,
    min_variability: float | pd.Series = 0.0,
    max_variability: float | pd.Series = 1.0,
    gen_name: str = "",
    *,
    adaptive: bool = False,
    gen_capacity: float = 0.0,
    zone_capacity: float = 0.0,
    chunk_history: list[dict] | None = None,
) -> tuple[pd.Series, dict]:
    """Adjust variability (p_max_pu) using cgrid's capture price method.

    This follows the cgrid approach:
    1. Split time into n_chunks periods
    2. For each period, calculate capture price
    3. Compare to variable cost and adjust by ±step_size (scaled by capacity)
    4. Smooth with moving average
    5. Clip to bounds

    Note: For StepResources with quadratic costs, variable_cost should be the
    EFFECTIVE marginal cost at current generation (not base cost), to properly
    reflect the S-curve dispatch behavior and encourage ramping down when
    capture prices are below effective costs.

    Parameters
    ----------
    generation : pd.Series
        Generator dispatch from previous solve.
    prices : pd.Series
        Bus marginal prices.
    current_variability : pd.Series
        Current p_max_pu values.
    variable_cost : pd.Series
        Variable cost (marginal_cost + fuel_cost + CO2_cost).
    n_chunks : int
        Number of time chunks to split into, by default 104
    step_size : float
        Direct p_max_pu adjustment per chunk, by default 0.02 (±0.02 per chunk)
    smoothing_window : int
        Hours for moving average smoothing, by default 24
    min_variability : float | pd.Series
        Minimum allowed variability (scalar or per-hour array), by default 0.0
    max_variability : float | pd.Series
        Maximum allowed variability (scalar or per-hour array), by default 1.0
    gen_name : str
        Generator name for logging, by default ""
    adaptive : bool
        Use proportional + secant steps instead of fixed steps, by default False.
    gen_capacity : float
        Generator rated capacity in MW (for market-share damping), by default 0.0.
    zone_capacity : float
        Total generation capacity on the generator's bus, by default 0.0.
    chunk_history : list[dict] | None
        Previous iteration per-chunk data for secant acceleration, by default None.

    Returns
    -------
    tuple[pd.Series, dict]
        Tuple of:
        - Adjusted variability (p_max_pu) values
        - Dictionary with diagnostic info (capture_prices, increases, decreases)

    """
    # Convert to numpy for easier manipulation
    g = np.array(generation)
    p = np.array(prices)
    v = np.array(current_variability).copy()
    vc = np.array(variable_cost)

    # Split indices into chunks
    nt = len(generation)
    indices = np.arange(nt)
    chunks = np.array_split(indices, n_chunks)

    # Track statistics
    n_increase = 0
    n_decrease = 0
    n_unchanged = 0
    increase_chunk_indices: set[int] = set()
    decrease_chunk_indices: set[int] = set()
    capture_prices = []
    avg_costs = []

    # Adjust variability for each chunk based on capture price
    chunk_data = []
    for chunk_idx, chunk in enumerate(chunks):
        capture_price = calculate_capture_price(g[chunk], p[chunk])
        avg_variable_cost = vc[chunk].mean()
        chunk_mean_v = v[chunk].mean()

        capture_prices.append(capture_price)
        avg_costs.append(avg_variable_cost)

        gap = capture_price - avg_variable_cost

        if abs(gap) < constants.VAR_CHUNK_EQUILIBRIUM_TOLERANCE:
            adjustment = 0.0
            if adaptive:
                chunk_data.append(
                    {
                        "capture_price": capture_price,
                        "mean_variability": chunk_mean_v,
                    },
                )
        elif adaptive:
            adjustment = _adaptive_step(
                capture_price,
                avg_variable_cost,
                chunk_mean_v,
                step_size,
                gen_capacity,
                zone_capacity,
                chunk_history,
                chunk_idx,
            )
            chunk_data.append(
                {
                    "capture_price": capture_price,
                    "mean_variability": chunk_mean_v,
                },
            )
        else:
            if gap > 0:
                adjustment = step_size
            else:
                adjustment = -step_size

        v[chunk] += adjustment

        if adjustment > 1e-8:
            increase_chunk_indices.add(chunk_idx)
            n_increase += 1
        elif adjustment < -1e-8:
            decrease_chunk_indices.add(chunk_idx)
            n_decrease += 1
        else:
            n_unchanged += 1

    # Smooth with moving average
    v = np.convolve(v, np.ones(smoothing_window) / smoothing_window, mode="same")

    # Clip to bounds using fancy indexing (supports both scalar and array bounds,
    # like cgrid)
    v_max = np.asarray(max_variability)
    v_min = np.asarray(min_variability)

    if v_max.ndim == 0:  # scalar
        v[v > v_max] = v_max
    else:  # array
        mask = v > v_max
        v[mask] = v_max[mask]

    if v_min.ndim == 0:  # scalar
        v[v < v_min] = v_min
    else:  # array
        mask = v < v_min
        v[mask] = v_min[mask]

    # Count how many chunks hit bounds and compute per-chunk means (like cgrid)
    n_chunks_at_min = 0
    n_chunks_at_max = 0
    n_increase_chunks_at_max = 0  # How many "increase" chunks got clamped at max
    n_decrease_chunks_at_min = 0  # How many "decrease" chunks got clamped at min

    initial_chunk_means = []  # Per-chunk means before adjustment
    final_chunk_means = []  # Per-chunk means after smoothing

    for chunk_idx, chunk in enumerate(chunks):
        initial_mean = np.array(current_variability)[chunk].mean()
        final_mean = v[chunk].mean()

        initial_chunk_means.append(initial_mean)
        final_chunk_means.append(final_mean)

        if final_mean <= min_variability + 1e-6:
            n_chunks_at_min += 1
            if chunk_idx in decrease_chunk_indices:
                n_decrease_chunks_at_min += 1
        if final_mean >= max_variability - 1e-6:
            n_chunks_at_max += 1
            if chunk_idx in increase_chunk_indices:
                n_increase_chunks_at_max += 1

    # Compute diagnostics using per-chunk means (matching cgrid)
    v_initial_mean = np.mean(initial_chunk_means) if initial_chunk_means else 0.0
    v_final_mean = np.mean(final_chunk_means) if final_chunk_means else 0.0

    diagnostics = {
        "n_chunks": n_chunks,
        "n_increase": n_increase,
        "n_decrease": n_decrease,
        "n_unchanged": n_unchanged,
        "n_chunks_at_min_bound": n_chunks_at_min,
        "n_chunks_at_max_bound": n_chunks_at_max,
        "n_increase_chunks_at_max_bound": n_increase_chunks_at_max,
        "n_decrease_chunks_at_min_bound": n_decrease_chunks_at_min,
        "avg_capture_price": np.mean(capture_prices) if capture_prices else 0,
        "avg_variable_cost": np.mean(avg_costs) if avg_costs else 0,
        "v_initial_mean": v_initial_mean,
        "v_final_mean": v_final_mean,
        "v_change": v_final_mean - v_initial_mean,
    }
    if adaptive:
        diagnostics["chunk_data"] = chunk_data

    if gen_name:
        logger.debug("\\n  %s - Capture Price Analysis:", gen_name)
        logger.debug(
            "    Chunks: increase=%s, decrease=%s, unchanged=%s",
            n_increase,
            n_decrease,
            n_unchanged,
        )
        logger.debug(
            "    Avg capture price: %.2f €/MWh",
            diagnostics["avg_capture_price"],
        )
        logger.debug(
            "    Avg variable cost: %.2f €/MWh",
            diagnostics["avg_variable_cost"],
        )
        if diagnostics["avg_capture_price"] > diagnostics["avg_variable_cost"]:
            diff = diagnostics["avg_capture_price"] - diagnostics["avg_variable_cost"]
            logger.debug(
                "    → Net effect: INCREASE (capture > cost by %.2f €/MWh)",
                diff,
            )
        else:
            diff = diagnostics["avg_variable_cost"] - diagnostics["avg_capture_price"]
            logger.debug(
                "    → Net effect: DECREASE (capture < cost by %.2f €/MWh)",
                diff,
            )
        logger.debug(
            "    Variability change: %.3f → %.3f (Δ=%+.3f)",
            diagnostics["v_initial_mean"],
            diagnostics["v_final_mean"],
            diagnostics["v_change"],
        )
        if n_chunks_at_min > 0 or n_chunks_at_max > 0:
            logger.debug(
                "    Boundary hits: %d chunks at min, %d chunks at max",
                n_chunks_at_min,
                n_chunks_at_max,
            )
            if n_increase_chunks_at_max > 0:
                logger.debug(
                    "    ⚠ %d/%d increase chunks clamped at max bound",
                    n_increase_chunks_at_max,
                    n_increase,
                )
            if n_decrease_chunks_at_min > 0:
                logger.debug(
                    "    ⚠ %d/%d decrease chunks clamped at min bound",
                    n_decrease_chunks_at_min,
                    n_decrease,
                )

    return pd.Series(v, index=generation.index), diagnostics


def apply_variable_commitment(
    network: pypsa.Network,
    n_chunks: int = 104,
    step_size: float = 0.02,
    smoothing_window: int = 24,
    min_variability: float = 0.0,
    max_variability: float = 1.0,
    min_fraction_multiplier: float = 1.0,
    *,
    adjust_p_max: bool = True,
    generator_filter: Callable | None = None,
    variability_damping: float = 1.0,
    adaptive: bool = False,
    chunk_history: dict[str, list[dict]] | None = None,
    extendable_step_size: float | None = None,
) -> dict[str, dict[str, pd.Series]]:
    """Apply variable commitment using cgrid's capture-price approach.

    This follows cgrid's method:
    1. Calculate capture price (generation-weighted average) for time chunks
    2. Compare to variable cost and adjust by ±step_size
    3. Smooth with 24-hour moving average
    4. Apply to both p_max_pu (variability) and p_min_pu

    Note on variable costs: For generators with quadratic costs (StepResources),
    this uses the EFFECTIVE marginal cost at current generation level:
        MC(g) = marginal_cost + 2 * marginal_cost_quadratic * g
    This reflects the S-curve dispatch behavior from cgrid and makes generators
    dispatching at high levels more likely to ramp down when capture price is low.

    For networks from cgrid_to_pypsa, marginal_cost already includes all variable
    costs (O&M + fuel + emissions).

    Parameters
    ----------
    network : pypsa.Network
        A solved PyPSA network (must have dispatch and prices available).
    n_chunks : int
        Number of time chunks to split into, by default 104 (≈weekly for 8760 hours)
    step_size : float
        Amount to adjust variability per iteration, by default 0.02
    smoothing_window : int
        Hours for moving average smoothing, by default 24
    min_variability : float
        Minimum allowed p_max_pu, by default 0.0
    max_variability : float
        Maximum allowed p_max_pu, by default 1.0
    min_fraction_multiplier : float
        Multiply p_min_pu by (variability * this), by default 1.0
    adjust_p_max : bool
        If True, adjust p_max_pu (variability), by default True
    generator_filter : Callable | None, optional
        Function that takes generator row and returns True if it should have
        dynamic commitment. If None, applies to all generators with
        marginal_cost_quadratic > 0 (StepResources), by default None
    variability_damping : float
        Damping factor for variability adjustments (0-1), by default 1.0 (no damping)
    adaptive : bool
        Use proportional + secant steps instead of fixed steps, by default False.
        When True, step_size acts as the maximum adjustment and actual steps
        are scaled by the price-cost gap and damped by market share.
    chunk_history : dict[str, list[dict]] | None
        Per-generator chunk data from the previous iteration (for secant
        acceleration).  Managed by :func:`two_stage_solve`, by default None.
    extendable_step_size : float | None
        Step size for generators with ``p_nom_extendable=True``.  When set,
        extendable generators use this (typically the objective-decayed step)
        while non-extendable generators use the base ``step_size``.  If None,
        all generators use ``step_size``.

    Returns
    -------
    dict[str, dict[str, pd.Series]]
        Dictionary mapping generator names to dict with 'p_min_pu' and optionally
        'p_max_pu' series

    Raises
    ------
    ValueError
        If network has not been solved yet

    Examples
    --------
    >>> # After first solve
    >>> network.optimize(solver_name='highs')
    >>> # Apply variable commitment using cgrid method
    >>> adjusted = apply_variable_commitment(network, n_chunks=104, step_size=0.02)
    >>> # Re-solve
    >>> network.optimize(solver_name='highs')

    """
    if network.buses_t.marginal_price.empty:
        msg = (
            "Network must be solved before applying dynamic commitment "
            "(no marginal prices found)"
        )
        raise ValueError(
            msg,
        )

    if network.generators_t.p.empty:
        msg = (
            "Network must be solved before applying dynamic commitment "
            "(no generator dispatch found)"
        )
        raise ValueError(
            msg,
        )

    slack_cols = [c for c in network.generators_t.p.columns if "slack" in c]
    if slack_cols and (network.generators_t.p[slack_cols] > 1e-2).any().any():
        logger.warning(
            "Slack generators are active — skipping variability update to avoid"
            " locking in profiles from an infeasible solve."
        )
        return {}

    # Default filter: apply to StepResources (generators with quadratic costs)
    if generator_filter is None:

        def generator_filter(gen_row: pd.Series) -> bool:
            return gen_row.get("marginal_cost_quadratic", 0) > 0

    adjusted_generators = {}

    # Pre-compute zone capacities for market-share damping
    zone_capacities = {}
    if adaptive:
        zone_capacities = network.generators.groupby("bus")["p_nom"].sum().to_dict()

    for gen_name in network.generators.index:
        gen = network.generators.loc[gen_name]

        # Check if this generator should have dynamic commitment
        if not generator_filter(gen):
            continue

        # Non-extendable generators are not coupled to the capacity expansion
        # loop, so they can use the full base step regardless of objective decay.
        is_extendable = bool(gen.get("p_nom_extendable", False))
        effective_step = (
            extendable_step_size
            if (is_extendable and extendable_step_size is not None)
            else step_size
        )

        bus = gen["bus"]

        # Get generation and prices from previous solve
        generation = network.generators_t.p[gen_name]
        bus_prices = network.buses_t.marginal_price[bus]

        # Variable cost: In PyPSA, marginal_cost should include all variable costs:
        # - Direct variable O&M cost
        # - Fuel cost (fuel_price / efficiency)
        # - CO2 cost (CO2_intensity * CO2_price / efficiency)
        # This matches cgrid's: variable_cost + fuel_cost + CO2_cost
        # When coming from cgrid_to_pypsa, these are already combined in marginal_cost
        marginal_cost_base = gen.get("marginal_cost", 0)
        marginal_cost_quadratic = gen.get("marginal_cost_quadratic", 0)

        # For generators with quadratic costs, use EFFECTIVE marginal cost at
        # current generation. This reflects the S-curve behavior:
        # MC(g) = base + 2 * quad * (g/p_nom) * p_nom
        # This makes the comparison more realistic - generators dispatching at
        # high levels
        # have higher marginal costs and are more likely to ramp down
        if marginal_cost_quadratic > 0:
            # Effective marginal cost at current generation level
            # MC(g) = marginal_cost + 2 * marginal_cost_quadratic * g
            effective_mc = marginal_cost_base + 2 * marginal_cost_quadratic * generation
            variable_cost = effective_mc

            logger.debug(
                "  %s: Using effective MC (base=%.2f, quad=%.4f)",
                gen_name,
                marginal_cost_base,
                marginal_cost_quadratic,
            )
            logger.debug(
                "    Effective MC range: [%.2f, %.2f] €/MWh",
                effective_mc.min(),
                effective_mc.max(),
            )
        else:
            # No quadratic term - use base marginal cost
            variable_cost = pd.Series(marginal_cost_base, index=generation.index)

        gen_adjustments = {}

        # Adjust p_max_pu (variability) using cgrid method.
        # Only applies to generators with variability="auto" in the cgrid JSON —
        # generators with a CSV variability profile have a fixed physical ceiling
        # that must not be overridden by the iterative adjustment.
        if adjust_p_max and gen.get("auto_variability", False):
            # Get current p_max_pu
            if gen_name in network.generators_t.p_max_pu.columns:
                current_p_max = network.generators_t.p_max_pu[gen_name]
            else:
                # Use scalar value
                current_p_max = pd.Series(
                    gen.get("p_max_pu", 1.0),
                    index=generation.index,
                )

            # Per-generator floor/ceiling: use min_variability / max_variability
            # columns if present (set from the cgrid JSON), otherwise fall back
            # to the global parameters.  Values may be scalars or arrays.
            gen_min_var = gen.get("min_variability", np.nan)
            if np.ndim(gen_min_var) > 0:
                effective_min_variability = gen_min_var
            else:
                effective_min_variability = (
                    float(gen_min_var) if not pd.isna(gen_min_var) else min_variability
                )
            gen_max_var = gen.get("max_variability", np.nan)
            if np.ndim(gen_max_var) > 0:
                effective_max_variability = gen_max_var
            else:
                effective_max_variability = (
                    float(gen_max_var) if not pd.isna(gen_max_var) else max_variability
                )

            # Get chunk history for this generator (secant acceleration)
            gen_chunk_hist = None
            if adaptive and chunk_history is not None:
                gen_chunk_hist = chunk_history.get(gen_name)

            # Apply cgrid adjustment
            new_p_max, diagnostics = adjust_variability(
                generation=generation,
                prices=bus_prices,
                current_variability=current_p_max,
                variable_cost=variable_cost,
                n_chunks=n_chunks,
                step_size=effective_step,
                smoothing_window=smoothing_window,
                min_variability=effective_min_variability,
                max_variability=effective_max_variability,
                gen_name=gen_name,
                adaptive=adaptive,
                gen_capacity=float(gen.get("p_nom", 0)),
                zone_capacity=zone_capacities.get(bus, 0.0),
                chunk_history=gen_chunk_hist,
            )

            # Adaptive damping: generators with strong directional consensus
            # (near 100% chunks voting same way) are in transit to equilibrium
            # and should run at full speed. Generators near 50/50 are oscillating
            # and need damping. Linearly interpolate between the two.
            #
            #   fraction_majority = max(↑, ↓) / n_chunks  ∈ [0.5, 1.0]
            #   effective_a = min_a  # noqa: ERA001
            #     + (1 - min_a) * (fraction_majority - 0.5) * 2
            #     → fraction=1.0 (fully monotone)  ⟹ effective_a = 1.0  (no damping)
            #     → fraction=0.5 (perfectly split)
            #       ⟹ effective_a = min_a  (full damping)
            d = diagnostics
            n_chunks_val = max(d["n_chunks"], 1)
            fraction_majority = max(d["n_increase"], d["n_decrease"]) / n_chunks_val
            effective_alpha = (
                variability_damping
                + (1.0 - variability_damping) * (fraction_majority - 0.5) * 2.0
            )
            effective_alpha = min(effective_alpha, 1.0)  # clamp; shouldn't exceed 1.0

            if effective_alpha < 1.0:
                new_p_max = (
                    effective_alpha * new_p_max
                    + (1.0 - effective_alpha) * current_p_max
                )

            # Summarise variability update at INFO so oscillation is visible in logs.
            # Use actual post-damping values so the log reflects what's applied.
            v_initial_mean = d["v_initial_mean"]
            v_final_mean_applied = float(new_p_max.mean())
            v_change_applied = v_final_mean_applied - v_initial_mean
            if v_change_applied > 1e-6:
                net_dir = "INCREASE"
            elif v_change_applied < -1e-6:
                net_dir = "DECREASE"
            else:
                net_dir = "neutral"
            boundary_note = ""
            if d["n_chunks_at_min_bound"] > 0 or d["n_chunks_at_max_bound"] > 0:
                boundary_note = (
                    f" [{d['n_chunks_at_min_bound']} at min, "
                    f"{d['n_chunks_at_max_bound']} at max]"
                )
            damping_note = (
                f" [a={effective_alpha:.2f}]" if effective_alpha < 0.995 else ""
            )
            logger.info(
                "  %s: chunks ↑%d ↓%d ↔%d | variability %.3f→%.3f (Δ%+.4f) → %s%s%s",
                gen_name,
                d["n_increase"],
                d["n_decrease"],
                d["n_unchanged"],
                v_initial_mean,
                v_final_mean_applied,
                v_change_applied,
                net_dir,
                boundary_note,
                damping_note,
            )

            # Apply to network
            # Clamp p_max_pu from below so it never goes below the existing p_min_pu.
            # This covers both time-varying profiles in generators_t and static scalars.
            if gen_name in network.generators_t.p_min_pu.columns:
                existing_p_min = network.generators_t.p_min_pu[gen_name]
            else:
                existing_p_min = gen.get("p_min_pu", 0.0)
            new_p_max = np.maximum(new_p_max, existing_p_min)
            network.generators_t.p_max_pu[gen_name] = new_p_max
            gen_adjustments["p_max_pu"] = new_p_max
            gen_adjustments["diagnostics"] = diagnostics

            # Also adjust p_min_pu if min_fraction is set
            # Following cgrid: min_generation = min_fraction * capacity * variability
            base_min_fraction = gen.get("min_fraction", 0)
            if base_min_fraction > 0 and min_fraction_multiplier > 0:
                # Scale p_min_pu by the variability
                new_p_min = base_min_fraction * new_p_max * min_fraction_multiplier
                new_p_min = new_p_min.clip(lower=min_variability, upper=max_variability)
                # Ensure p_min_pu never exceeds p_max_pu (would cause infeasibility)
                new_p_min = np.minimum(new_p_min, new_p_max)

                network.generators_t.p_min_pu[gen_name] = new_p_min
                gen_adjustments["p_min_pu"] = new_p_min

        adjusted_generators[gen_name] = gen_adjustments

        log_msg = (
            f"Applied dynamic commitment (cgrid method) to {gen_name} "
            f"(bus={bus}, chunks={n_chunks}, step={step_size}):"
        )
        if "p_max_pu" in gen_adjustments:
            p_max = gen_adjustments["p_max_pu"]
            log_msg += (
                f"\n  p_max_pu: range=[{p_max.min():.3f}, {p_max.max():.3f}], "
                f"mean={p_max.mean():.3f}"
            )
        if "p_min_pu" in gen_adjustments:
            p_min = gen_adjustments["p_min_pu"]
            log_msg += (
                f"\n  p_min_pu: range=[{p_min.min():.3f}, {p_min.max():.3f}], "
                f"mean={p_min.mean():.3f}"
            )
        logger.debug(log_msg)

    return adjusted_generators
