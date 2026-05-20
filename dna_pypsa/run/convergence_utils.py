"""Convergence checks for the two-stage solve loop."""

import logging

import numpy as np

from dna_pypsa import constants

logger = logging.getLogger(__name__)


def warn_slow_capacity_movers(cap_records: list[dict]) -> None:
    """Warn when a generator is moving in one direction with consistently small steps.

    Fires when a generator has moved in one direction for
    ``constants.CAP_SLOW_MOVER_WINDOW`` consecutive iterations AND at least one of those
    steps was below ``constants.CAP_SLOW_MOVER_MIN_PERCENT`` percent.  The size check avoids
    false positives for generators that are simply far from equilibrium and
    already taking maximum steps - those are working correctly and just need
    more iterations.

    A genuine slow mover is one where the secant should be projecting toward
    break-even in 1-2 steps but instead takes many small proportional steps.
    Common causes: ``|Δp_nom|`` or ``|Δprofit|`` below their secant thresholds,
    or the secant slope has the wrong sign (price-taker regime) and falls back
    to proportional.

    Parameters
    ----------
    cap_records : list[dict]
        ``history["capacity"]`` list (most recent entry last).

    """
    if len(cap_records) < constants.CAP_SLOW_MOVER_WINDOW:
        return
    recent = cap_records[-constants.CAP_SLOW_MOVER_WINDOW :]
    for gen_name in recent[-1]:
        signs = []
        pcts = []
        for rec in recent:
            if gen_name not in rec:
                break
            d = rec[gen_name]
            delta = d["p_nom_new"] - d["p_nom_old"]
            if delta == 0:
                break
            signs.append(1 if delta > 0 else -1)
            pcts.append(abs(delta) / max(d["p_nom_old"], 1.0) * 100.0)
        if (
            len(signs) == constants.CAP_SLOW_MOVER_WINDOW
            and len(set(signs)) == 1
            and min(pcts) < constants.CAP_SLOW_MOVER_MIN_PERCENT
        ):
            direction = "expanding" if signs[0] > 0 else "contracting"
            logger.warning(
                "Capacity slow mover: %s has been %s for %d consecutive"
                " iterations with small steps (min %.1f%%) — secant may"
                " not be accelerating (check Δp_nom / Δprofit thresholds"
                " and slope sign)",
                gen_name,
                direction,
                constants.CAP_SLOW_MOVER_WINDOW,
                min(pcts),
            )


def _capacity_stable(history: dict, threshold: float) -> bool:
    """Check whether capacity adjustments have stabilised.

    Parameters
    ----------
    history : dict
        Two-stage solve history dict (must contain ``"capacity"`` key).
    threshold : float
        Maximum allowable percentage change.  Returns ``False`` if any
        generator's last capacity change exceeds this.

    Returns
    -------
    bool
        ``True`` if no capacity history exists or all changes are below
        *threshold*; ``False`` otherwise.

    """
    if not history.get("capacity"):
        return True
    last_cap = history["capacity"][-1]
    cap_changes = {
        gn: abs(d["p_nom_new"] - d["p_nom_old"]) / d["p_nom_old"] * 100.0
        for gn, d in last_cap.items()
        if d["p_nom_old"] > 1.0
    }
    large = {gn: pct for gn, pct in cap_changes.items() if pct >= threshold}
    if large:
        worst = max(large, key=large.__getitem__)
        logger.info(
            "Capacity still changing (max %.1f%% by %s"
            " >= %.1f%% threshold) — continuing",
            large[worst],
            worst,
            threshold,
        )
        return False
    return True


def check_objective_window(
    history: dict,
    convergence_window: int,
    convergence_threshold: float,
    *,
    capacity_adjustment: bool,
) -> str | None:
    """Check whether the objective has stabilised over a rolling window.

    Catches oscillation where consecutive-pair changes look large but the
    envelope is stable.

    Parameters
    ----------
    history : dict
        Two-stage solve history dict.
    convergence_window : int
        Number of recent iterations to consider.
    convergence_threshold : float
        Maximum allowable span as a percentage of the mean.
    capacity_adjustment : bool
        Whether capacity feedback is active (gates convergence).

    Returns
    -------
    str | None
        Convergence reason string, or ``None`` if not converged.

    """
    if len(history["objective"]) < convergence_window:
        return None

    window_objs = history["objective"][-convergence_window:]
    obj_mean = abs(np.mean(window_objs))
    obj_span_pct = (
        100.0 * (max(window_objs) - min(window_objs)) / obj_mean
        if obj_mean > 0
        else 0.0
    )
    logger.info(
        "Objective rolling window (%d iters): span=%.4f%% → threshold %.4f%% (%.0f%%)",
        convergence_window,
        obj_span_pct,
        convergence_threshold,
        (
            100.0 * obj_span_pct / convergence_threshold
            if convergence_threshold > 0
            else float("inf")
        ),
    )
    if obj_span_pct >= convergence_threshold:
        return None

    if capacity_adjustment and not _capacity_stable(history, convergence_threshold):
        return None

    reason = (
        f"objective_window: span={obj_span_pct:.4f}% < {convergence_threshold:.4f}% "
        f"over {convergence_window} iterations (oscillation resolved)"
    )
    logger.info(
        "Converged! Objective stable over last %d iterations "
        "(span=%.4f%% < %.4f%%) - oscillation resolved",
        convergence_window,
        obj_span_pct,
        convergence_threshold,
    )
    return reason


def check_dispatch_settlement(
    history: dict,
    iteration: int,
    convergence_threshold: float,
    variability_convergence_threshold: float,
    *,
    capacity_adjustment: bool,
) -> str | None:
    """Check whether variability adjustments have settled.

    Uses the per-generator mean variability change (``v_change``) recorded by
    the last round of :func:`apply_variable_commitment`.  Converges when the
    p75 of ``|v_change|`` across adjusted generators is below
    ``variability_convergence_threshold``.

    Parameters
    ----------
    history : dict
        Two-stage solve history dict.
    iteration : int
        Current zero-based iteration index.
    convergence_threshold : float
        Objective-based threshold (%) passed to :func:`_capacity_stable`.
    variability_convergence_threshold : float
        Maximum allowable p75 of ``|v_change|`` for convergence.
    capacity_adjustment : bool
        Whether capacity feedback is active (gates convergence).

    Returns
    -------
    str | None
        Convergence reason string, or ``None`` if not converged.

    """
    last_adjusted = history["p_min_pu"].get(f"iter_{iteration}", {})
    if not last_adjusted:
        return None

    v_changes = [
        abs(data.get("diagnostics", {}).get("v_change", float("nan")))
        for data in last_adjusted.values()
        if not np.isnan(data.get("diagnostics", {}).get("v_change", float("nan")))
    ]
    if not v_changes:
        return None

    p75 = float(np.percentile(v_changes, 75))
    converged = p75 < variability_convergence_threshold

    logger.info(
        "Variability settlement (%d generators): p75 |Δv|=%.4f → threshold %.4f %s",
        len(v_changes),
        p75,
        variability_convergence_threshold,
        "✓ settled" if converged else "✗ not yet",
    )

    if not converged:
        return None

    if capacity_adjustment and not _capacity_stable(history, convergence_threshold):
        return None

    reason = f"variability_settled: p75 |Δv|={p75:.4f} < {variability_convergence_threshold:.4f}"
    logger.info(
        "Converged! Variability settled: p75 |Δv|=%.4f < %.4f",
        p75,
        variability_convergence_threshold,
    )
    return reason


def check_objective_consecutive(
    history: dict,
    convergence_threshold: float,
    *,
    capacity_adjustment: bool,
) -> str | None:
    """Check whether consecutive objective values have converged.

    Parameters
    ----------
    history : dict
        Two-stage solve history dict.
    convergence_threshold : float
        Maximum allowable change as a percentage.
    capacity_adjustment : bool
        Whether capacity feedback is active (gates convergence).

    Returns
    -------
    str | None
        Convergence reason string, or ``None`` if not converged.

    """
    if len(history["objective"]) < 2:
        return None

    previous = history["objective"][-2]
    current = history["objective"][-1]
    change = abs(current - previous)
    change_pct = 100 * change / abs(previous)

    logger.info(
        "Objective change: %.2f (%.4f%%) → consecutive threshold %.4f%% (%.0f%%)",
        change,
        change_pct,
        convergence_threshold,
        (
            100.0 * change_pct / convergence_threshold
            if convergence_threshold > 0
            else float("inf")
        ),
    )

    if change_pct == 0:
        logger.warning(
            "Objective unchanged (%.2f) - adjustments had no impact. "
            "Consider reviewing adjustment parameters.",
            current,
        )

    if not (0 < change_pct < convergence_threshold):
        return None

    if capacity_adjustment and not _capacity_stable(history, convergence_threshold):
        return None

    reason = f"objective_consecutive: {change_pct:.4f}% < {convergence_threshold:.4f}%"
    logger.info(
        "Converged! Objective change (%.4f%%) below threshold (%.4f%%)",
        change_pct,
        convergence_threshold,
    )
    return reason
