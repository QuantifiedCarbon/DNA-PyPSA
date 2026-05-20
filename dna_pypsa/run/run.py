"""Core solve functions and two-stage orchestration for dna_pypsa."""

import logging
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import pypsa

from dna_pypsa import constants
from dna_pypsa.check_network import check_network
from dna_pypsa.run.capacity_adjustment import adjust_capacity
from dna_pypsa.run.convergence_utils import (
    check_dispatch_settlement,
    check_objective_consecutive,
    check_objective_window,
    warn_slow_capacity_movers,
)
from dna_pypsa.run.investment import investment_pre_solve
from dna_pypsa.run.variability_adjustment import apply_variable_commitment

logger = logging.getLogger(__name__)


# ============================================================================
# Output helpers
# ============================================================================


def _restore_snapshot_datetimes(network: pypsa.Network) -> None:
    """Set the actual DatetimeIndex on all snapshot-indexed dataframes.

    Parameters
    ----------
    network : pypsa.Network
        Network whose ``snapshots`` is a DatetimeIndex.

    Notes
    -----
    PyPSA 1.2.0 stores ``snapshot_weightings`` with an integer positional
    index even when ``set_snapshots`` was called with a DatetimeIndex.  When
    the network is exported to netCDF the snapshot variable is therefore
    written as int64.  Explicitly restoring the DatetimeIndex on
    ``snapshot_weightings`` and every panel-data frame before export causes
    PyPSA to write datetime64 values, which Convexity requires to determine
    the simulation time range.

    """
    snapshots = network.snapshots
    if not isinstance(snapshots, pd.DatetimeIndex):
        logger.debug("Snapshots are not a DatetimeIndex — skipping datetime restore")
        return

    network.snapshot_weightings.index = snapshots

    for comp in network.all_components:
        pnl = network.pnl(comp)
        for attr in list(pnl.keys()):
            df = pnl[attr]
            if len(df) == len(snapshots):
                df.index = snapshots

    logger.debug("Restored DatetimeIndex on all snapshot-indexed dataframes")


def export_results(network: pypsa.Network, output_directory: str | Path) -> None:
    """Export a solved network to CSV folder and netCDF.

    Parameters
    ----------
    network : pypsa.Network
        Solved network to export.
    output_directory : str | Path
        Directory to write results into.  Creates
        ``<output_directory>/results/`` (CSV) and
        ``<output_directory>/results.nc`` (netCDF).

    """
    output_directory = Path(output_directory)

    network.sanitize()
    network.meta = {}

    if "auto_variability" in network.generators.columns:
        network.generators["auto_variability"] = (
            network.generators["auto_variability"]
            .replace("", False)
            .infer_objects(copy=False)
            .astype(bool)
        )

    _restore_snapshot_datetimes(network)

    csv_path = output_directory / constants.RESULTS_CSV_DIR
    csv_path.mkdir(parents=True, exist_ok=True)
    network.export_to_csv_folder(csv_path)
    logger.info("Results exported to CSV: %s", csv_path)

    nc_path = output_directory / constants.RESULTS_NC_FILE
    network.export_to_netcdf(nc_path)
    logger.info("Results exported to netCDF: %s", nc_path)


# ============================================================================
# Solver helpers
# ============================================================================


def _last_stable_barrier_gap(log_path: Path) -> tuple[float, float, float] | None:
    """Parse a Gurobi barrier log and return the last stable (primal, dual, gap_pct).

    Parameters
    ----------
    log_path : Path
        Path to the Gurobi log file.

    Returns
    -------
    tuple[float, float, float] | None
        (primal, dual, gap_pct) of the last stable barrier iteration, or None.

    """
    primal, dual = None, None
    try:
        with log_path.open(encoding="utf-8") as f:
            for line in f:
                parts = line.split()
                if len(parts) < 6 or not parts[0].isdigit():  # noqa: PLR2004
                    continue
                try:
                    p, d = float(parts[1]), float(parts[2])
                except ValueError:
                    continue
                if (
                    abs(p) < constants.DIVERGE_THRESHOLD
                    and abs(d) < constants.DIVERGE_THRESHOLD
                ):
                    primal, dual = p, d
    except OSError:
        return None
    if primal is None or dual is None:
        return None
    gap_pct = abs(primal - dual) / max(abs(primal), 1e-10) * 100
    return primal, dual, gap_pct


def _apply_external_stop_price(network: pypsa.Network, price: float) -> None:
    """Cap slack generator cost for zones without expandable capacity.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.
    price : float
        Replacement marginal_cost for external-zone slack generators.

    """
    if price <= 0:
        return

    expansion_buses = set(
        network.generators.loc[network.generators["p_nom_extendable"], "bus"],
    )
    updated = []
    for bus in network.buses.index:
        if network.buses.loc[bus, "carrier"] != "AC":
            continue
        if bus in expansion_buses:
            continue
        gen_name = f"slack_gen_{bus}"
        if gen_name not in network.generators.index:
            continue
        old = float(network.generators.loc[gen_name, "marginal_cost"])
        if old <= price:
            continue
        network.generators.loc[gen_name, "marginal_cost"] = price
        updated.append(bus)

    if updated:
        logger.info(
            "External stop price %.0f €/MWh applied to %d non-expansion zones: %s",
            price,
            len(updated),
            updated,
        )


# ============================================================================
# Core solver
# ============================================================================


def _post_solve_diagnostics(network: pypsa.Network) -> None:
    """Log storage, slack generator, and curtailment diagnostics after a solve.

    Parameters
    ----------
    network : pypsa.Network
        Solved network.

    """
    if len(network.stores_t.e.columns) > 0:
        logger.debug("Storage state of charge:\n%s", network.stores_t.e.describe())
    else:
        logger.debug("No stores in network")

    slack_output = network.generators_t.p[
        [col for col in network.generators_t.p.columns if "slack" in col]
    ]
    if (slack_output > constants.DISPATCH_NOISE_THRESHOLD_MW).any().any():
        logger.warning(
            "Slack generators are being used — model is infeasible without them"
        )
    else:
        logger.info("Slack generators are not being used — model is feasible")

    sink_cols = [
        col
        for col in network.links_t.p0.columns
        if col.endswith(("_curtailment", "_curtail_link"))
    ]
    if sink_cols:
        noise = constants.DISPATCH_NOISE_THRESHOLD_MW
        sink_output = network.links_t.p0[sink_cols]
        n_active = (sink_output > noise).any().sum()
        total_sink_flow = sink_output[sink_output > noise].sum().sum()
        if n_active > 0:
            logger.warning(
                "Curtailment links active: %d/%d links used, total flow = %.1f MWh",
                n_active,
                len(sink_cols),
                total_sink_flow,
            )
        else:
            logger.info("No curtailment links active")
    else:
        logger.info("No curtailment links in network")


def solve_network(
    network: pypsa.Network,
    solver: str,
    output_directory: str | Path,
) -> tuple[str, str]:
    """Solve a PyPSA network and run post-solve diagnostics.

    Parameters
    ----------
    network : pypsa.Network
        The network to solve.
    solver : str
        Solver name (``"highs"`` or ``"gurobi"``).
    output_directory : str | Path
        Directory for solver log files.

    Returns
    -------
    tuple[str, str]
        Optimisation status tuple (status, termination_condition).

    Raises
    ------
    ValueError
        If optimisation fails or the solver name is not recognised.

    """
    if solver not in constants.VALID_SOLVERS:
        raise ValueError(
            f"Unknown solver {solver!r}. Expected one of {constants.VALID_SOLVERS}."
        )
    start_time = time.time()
    log_path = Path(output_directory) / f"{solver}_log.txt"

    if solver == "gurobi":
        logger.info("Solving with Gurobi (log → %s)", log_path)
        opt_result = network.optimize(
            solver_name="gurobi",
            logfile=str(log_path),
            env={"LogToConsole": 0},
            solver_options={"DualReductions": 0, "ScaleFlag": 2, "BarConvTol": 5e-4},
        )
        if "infeasible" in opt_result[1]:
            logger.warning("**** INFEASIBILITY REPORT ****")
            try:
                network.model.print_infeasibilities()
            except Exception:  # noqa: BLE001
                logger.warning("Could not compute IIS — Gurobi job was aborted.")
            raise ValueError("Optimisation was infeasible — see report above.")
    else:
        logger.info("Solving with %s (log → %s)", solver, log_path)
        opt_result = network.optimize(solver_name=solver, logfile=str(log_path))

    elapsed = time.time() - start_time
    logger.info("Optimisation time: %.2f seconds", elapsed)
    logger.info("**** OPTIMISATION COMPLETE: %s ****", opt_result)

    if "suboptimal" in str(opt_result):
        logger.warning("Solver reported suboptimal termination.")
        gap_info = _last_stable_barrier_gap(log_path)
        if gap_info:
            primal, dual, gap_pct = gap_info
            logger.warning(
                "Last stable barrier — primal: %.6e  dual: %.6e  gap: %.4f%%",
                primal,
                dual,
                gap_pct,
            )

    if "ok" not in opt_result:
        logger.info("Manual infeasibility check by bus:")
        check_network(network)
        raise ValueError("Optimisation failed — see diagnostics above.")

    _post_solve_diagnostics(network)
    return opt_result


# ============================================================================
# Two-stage adjustment steps
# ============================================================================


def _check_convergence(
    history: dict,
    iteration: int,
    min_iterations: int,
    convergence_window: int,
    convergence_threshold: float,
    variability_convergence_threshold: float,
    *,
    capacity_adjustment: bool,
) -> str | None:
    """Run all convergence checks and return the first firing reason, or None.

    Parameters
    ----------
    history : dict
        Two-stage history dict.
    iteration : int
        Current zero-based iteration index.
    min_iterations : int
        Minimum iterations before any check can fire.
    convergence_window : int
        Rolling window length for the objective stability check.
    convergence_threshold : float
        Objective spread threshold (%) for window and consecutive checks.
    variability_convergence_threshold : float
        Maximum allowable p75 of ``|Δv|`` for dispatch settlement.
    capacity_adjustment : bool
        Whether capacity feedback is active.

    Returns
    -------
    str | None

    """
    if iteration == 0 or len(history["objective"]) <= 1 or iteration < min_iterations:
        return None
    return (
        check_objective_window(
            history,
            convergence_window,
            convergence_threshold,
            capacity_adjustment=capacity_adjustment,
        )
        or check_dispatch_settlement(
            history,
            iteration,
            convergence_threshold,
            variability_convergence_threshold,
            capacity_adjustment=capacity_adjustment,
        )
        or check_objective_consecutive(
            history,
            convergence_threshold,
            capacity_adjustment=capacity_adjustment,
        )
    )


def _log_solve_summary(
    history: dict,
    convergence_reason: str | None,
    total_iterations: int,
) -> None:
    """Log the two-stage solve summary.

    Parameters
    ----------
    history : dict
    convergence_reason : str | None
    total_iterations : int

    """
    logger.info("%s", "=" * 80)
    logger.info("TWO-STAGE OPTIMISATION COMPLETE")
    logger.info("%s", "=" * 80)
    if convergence_reason is not None:
        logger.info("STOP REASON  : %s", convergence_reason)
    else:
        logger.warning(
            "STOP REASON  : max iterations reached (%d) without convergence",
            total_iterations,
        )
    if len(history["objective"]) == 0:
        logger.error("No iterations completed successfully.")
    else:
        logger.info("Total iterations: %s", len(history["objective"]))
        logger.info("Final objective: %.0f", history["objective"][-1])
        if len(history["objective"]) > 1:
            obj_change = history["objective"][-1] - history["objective"][0]
            obj_change_pct = 100 * obj_change / abs(history["objective"][0])
            logger.info("Objective change: %+.0f (%+.2f%%)", obj_change, obj_change_pct)


def _log_iteration_prices(
    network: pypsa.Network,
    history: dict,
    iteration: int,
) -> None:
    """Log per-iteration price distribution.

    Parameters
    ----------
    network : pypsa.Network
    history : dict
    iteration : int

    """
    ac_buses = network.buses.index[network.buses.carrier == "AC"]
    ac_prices = network.buses_t.marginal_price[ac_buses]
    ac_prices_flat = ac_prices.to_numpy().ravel()

    logger.info("Objective value: %.0f", network.objective)
    pct = np.percentile(ac_prices_flat, [5, 25, 50, 75, 95])
    n_at_floor = int((ac_prices_flat <= constants.DEFAULT_START_PRICE + 1).sum())
    n_at_ceil = int((ac_prices_flat >= constants.DEFAULT_STOP_PRICE - 1).sum())
    total = len(ac_prices_flat)
    logger.info(
        "Price range (AC): %.2f - %.2f €/MWh | "
        "p5/p25/p50/p75/p95: %.0f/%.0f/%.0f/%.0f/%.0f | "
        "mean: %.1f | at floor: %d/%d (%.1f%%) | at ceil: %d/%d (%.1f%%)",
        ac_prices_flat.min(),
        ac_prices_flat.max(),
        pct[0],
        pct[1],
        pct[2],
        pct[3],
        pct[4],
        ac_prices_flat.mean(),
        n_at_floor,
        total,
        100 * n_at_floor / total,
        n_at_ceil,
        total,
        100 * n_at_ceil / total,
    )

    if iteration > 0 and f"iter_{iteration}" in history["prices"]:
        prev_ac_prices = history["prices"][f"iter_{iteration}"][ac_buses]
        price_changes = (ac_prices - prev_ac_prices).abs()
        logger.info(
            "Price changes (AC): mean=%.4f, min=%.4f, max=%.4f €/MWh",
            price_changes.to_numpy().mean(),
            price_changes.to_numpy().min(),
            price_changes.to_numpy().max(),
        )


def _apply_variable_commitment_step(
    network: pypsa.Network,
    iteration: int,
    total_iterations: int,
    iteration_schedule: list[tuple[int, float]],
    history: dict,
    *,
    is_adaptive: bool,
    gen_chunk_history: dict,
    prev_n_chunks: int | None,
    commitment_kwargs: dict,
    variability_warmup_iterations: int = 0,
    variability_warmup_max_step: float = 0.05,
) -> tuple[dict, int | None]:
    """Apply capture-price variability adjustments for one iteration.

    Parameters
    ----------
    network : pypsa.Network
    iteration : int
    total_iterations : int
    iteration_schedule : list[tuple[int, float]]
    history : dict
    is_adaptive : bool
    gen_chunk_history : dict
    prev_n_chunks : int | None
    commitment_kwargs : dict
    variability_warmup_iterations : int
    variability_warmup_max_step : float

    Returns
    -------
    tuple[dict, int | None]
        Updated (gen_chunk_history, prev_n_chunks).

    """
    logger.info("\nApplying dynamic commitment adjustments…")
    current_n_chunks, base_step_size = iteration_schedule[iteration]
    current_step_size = base_step_size

    if is_adaptive and len(history["objective"]) >= constants.STEP_DECAY_MIN_ITERATIONS:
        obj = history["objective"]
        window = obj[-min(constants.STEP_DECAY_WINDOW, len(obj)) :]
        span_pct = (max(window) - min(window)) / abs(window[-1]) * 100
        scale = float(np.clip(span_pct / 1.0, 0.1, 1.0))
        if scale < 1.0:
            logger.info(
                "Step decay: %.0f%% of base step (%.3f%% obj span over last %d iters)",
                scale * 100,
                span_pct,
                len(window),
            )
            current_step_size *= scale

    min_step_so_far = history.get("min_step_so_far", current_step_size)
    current_step_size = min(current_step_size, min_step_so_far)
    history["min_step_so_far"] = current_step_size

    if variability_warmup_iterations > 0 and iteration < variability_warmup_iterations:
        logger.info(
            "Variability warmup: capping step at %.4f (iter %d/%d warmup)",
            variability_warmup_max_step,
            iteration + 1,
            variability_warmup_iterations,
        )
        base_step_size = min(base_step_size, variability_warmup_max_step)
        current_step_size = min(current_step_size, variability_warmup_max_step)

    logger.info(
        "Stage: n_chunks=%d, step_size=%.4f (extendable), %.4f (fixed) (iteration %d/%d)",
        current_n_chunks,
        current_step_size,
        base_step_size,
        iteration + 1,
        total_iterations,
    )

    if is_adaptive and current_n_chunks != prev_n_chunks:
        gen_chunk_history = {}
    prev_n_chunks = current_n_chunks

    adjusted = apply_variable_commitment(
        network=network,
        n_chunks=current_n_chunks,
        step_size=base_step_size,
        extendable_step_size=current_step_size if is_adaptive else None,
        chunk_history=gen_chunk_history if is_adaptive else None,
        **commitment_kwargs,
    )

    if is_adaptive:
        for gn, gadj in adjusted.items():
            cd = gadj.get("diagnostics", {}).get("chunk_data")
            if cd is not None:
                gen_chunk_history[gn] = cd

    history["p_min_pu"][f"iter_{iteration + 1}"] = adjusted
    logger.info("Adjusted %s generators", len(adjusted))

    return gen_chunk_history, prev_n_chunks


def _apply_capacity_adjustment_step(
    network: pypsa.Network,
    capacity_max_step: float,
    capacity_min_step_mw: float,
    cap_history: dict,
) -> tuple[dict, dict]:
    """Adjust generator capacities based on profitability.

    Parameters
    ----------
    network : pypsa.Network
    capacity_max_step : float
    capacity_min_step_mw : float
    cap_history : dict

    Returns
    -------
    tuple[dict, dict]
        (cap_history, cap_adjusted).

    """
    logger.info("\nApplying capacity feedback…")
    cap_adjusted = adjust_capacity(
        network=network,
        max_step_fraction=capacity_max_step,
        min_step_mw=capacity_min_step_mw,
        cap_history=cap_history,
    )
    n_reduced = sum(1 for d in cap_adjusted.values() if d["p_nom_new"] < d["p_nom_old"])
    n_increased = sum(
        1 for d in cap_adjusted.values() if d["p_nom_new"] > d["p_nom_old"]
    )
    logger.info(
        "Capacity feedback: %d generators (%d reduced, %d increased)",
        len(cap_adjusted),
        n_reduced,
        n_increased,
    )
    new_entry = {
        gn: {"p_nom": d["p_nom_old"], "profit_per_mw": d["profit_per_mw"]}
        for gn, d in cap_adjusted.items()
    }
    for gn, entry in new_entry.items():
        if cap_history is not None and gn in cap_history:
            cap_history[gn] = cap_history[gn] + [entry]
        else:
            cap_history = cap_history or {}
            cap_history[gn] = [entry]
    return cap_history, cap_adjusted


# ============================================================================
# Two-stage orchestrator
# ============================================================================


def two_stage_solve(  # noqa: PLR0913
    network: pypsa.Network,
    solver: str,
    output_directory: str | Path,
    schedule: list[tuple[int, float, int]] | None = None,
    convergence_threshold: float = 0.10,
    convergence_window: int = 3,
    variability_convergence_threshold: float = 0.005,
    *,
    capacity_adjustment: bool = True,
    capacity_max_step: float = 0.05,
    capacity_min_step_mw: float = 200.0,
    capacity_adjustment_start: int = 1,
    capacity_warmup_iterations: int = 6,
    capacity_warmup_max_step: float = 0.02,
    variability_warmup_iterations: int = 6,
    variability_warmup_max_step: float = 0.05,
    min_iterations: int = 10,
    **commitment_kwargs: Any,  # noqa: ANN401
) -> tuple[tuple[str, str], dict[str, dict | list]]:
    """Perform two-stage optimisation with iterative variability and capacity adjustment.

    Parameters
    ----------
    network : pypsa.Network
        Network to optimise (modified in-place).
    solver : str
        Solver name (``"highs"`` or ``"gurobi"``).
    output_directory : str | Path
        Directory for solver log files.
    schedule : list[tuple[int, float, int]] | None
        Coarse-to-fine schedule: list of ``(n_chunks, step_size, num_iterations)``.
        Default selects ``[(104, 0.15, 100)]`` in adaptive mode or
        ``[(104, 0.02, 100)]`` otherwise.
    convergence_threshold : float
        Objective spread threshold (%) for rolling-window and consecutive checks.
    convergence_window : int
        Number of recent iterations for the rolling objective stability check.
    variability_convergence_threshold : float
        Maximum allowable p75 of ``|Δv|`` for variability settlement.
    capacity_adjustment : bool
        Enable profitability-based capacity feedback, by default ``True``.
    capacity_max_step : float
        Maximum p_nom change per iteration as a fraction.
    capacity_min_step_mw : float
        Absolute floor for the capacity step in MW.
    capacity_adjustment_start : int
        First iteration at which capacity feedback activates.
    capacity_warmup_iterations : int
        Number of initial iterations in which the capacity step is capped at
        ``capacity_warmup_max_step``.
    capacity_warmup_max_step : float
        Maximum fractional p_nom change per iteration during capacity warmup.
    variability_warmup_iterations : int
        Number of initial iterations in which variability steps are capped at
        ``variability_warmup_max_step``.
    variability_warmup_max_step : float
        Maximum variability step during the warmup period.
    min_iterations : int
        Minimum iterations before any convergence check can fire.
    **commitment_kwargs : Any
        Forwarded to :func:`apply_variable_commitment`.

    Returns
    -------
    tuple[tuple[str, str], dict[str, dict | list]]
        ``(final_status, history)``.

    """
    logger.info("=" * 80)
    logger.info("STARTING TWO-STAGE OPTIMISATION")
    logger.info("=" * 80)

    is_adaptive = commitment_kwargs.get("adaptive", False)
    commitment_kwargs.pop("chunk_history", None)

    if schedule is None:
        schedule = [(104, 0.15, 100)] if is_adaptive else [(104, 0.02, 100)]

    total_iterations = sum(n for _, _, n in schedule)
    logger.info("Schedule: %d total iterations: %s", total_iterations, schedule)

    iteration_schedule: list[tuple[int, float]] = []
    for n_chunks, step_size, num_iters in schedule:
        iteration_schedule.extend([(n_chunks, step_size)] * num_iters)

    history: dict[str, dict | list] = {
        "prices": {},
        "p_min_pu": {},
        "dispatch": {},
        "objective": [],
        "capacity": [],
        "convergence_reason": None,
    }
    final_status = None
    convergence_reason = None
    gen_chunk_history: dict = {}
    prev_n_chunks: int | None = None
    cap_history: dict = {}

    for iteration in range(total_iterations):
        logger.info("%s", "=" * 80)
        logger.info("ITERATION %s/%s", iteration + 1, total_iterations)
        logger.info("%s", "=" * 80)

        try:
            final_status = solve_network(network, solver, output_directory)
        except ValueError:
            logger.exception("Optimisation failed at iteration %s", iteration + 1)
            break

        history["objective"].append(network.objective)
        history["prices"][
            f"iter_{iteration + 1}"
        ] = network.buses_t.marginal_price.copy()
        history["dispatch"][f"iter_{iteration + 1}"] = network.generators_t.p.copy()

        _log_iteration_prices(network, history, iteration)

        convergence_reason = _check_convergence(
            history,
            iteration,
            min_iterations,
            convergence_window,
            convergence_threshold,
            variability_convergence_threshold,
            capacity_adjustment=capacity_adjustment,
        )
        if convergence_reason:
            break

        if iteration < total_iterations - 1:
            gen_chunk_history, prev_n_chunks = _apply_variable_commitment_step(
                network,
                iteration,
                total_iterations,
                iteration_schedule,
                history,
                is_adaptive=is_adaptive,
                gen_chunk_history=gen_chunk_history,
                prev_n_chunks=prev_n_chunks,
                commitment_kwargs=commitment_kwargs,
                variability_warmup_iterations=variability_warmup_iterations,
                variability_warmup_max_step=variability_warmup_max_step,
            )

            if capacity_adjustment and iteration >= capacity_adjustment_start:
                in_warmup = (
                    capacity_warmup_iterations > 0
                    and iteration < capacity_warmup_iterations
                )
                effective_cap_step = (
                    capacity_warmup_max_step if in_warmup else capacity_max_step
                )
                if in_warmup:
                    logger.info(
                        "Capacity warmup: capping step at %.2f%% (iter %d/%d warmup)",
                        effective_cap_step * 100,
                        iteration + 1,
                        capacity_warmup_iterations,
                    )
                cap_history, cap_adjusted = _apply_capacity_adjustment_step(
                    network,
                    effective_cap_step,
                    capacity_min_step_mw,
                    cap_history,
                )
                history["capacity"].append(cap_adjusted)
                warn_slow_capacity_movers(history["capacity"])

    history["convergence_reason"] = convergence_reason
    _log_solve_summary(history, convergence_reason, total_iterations)

    return final_status, history


# ============================================================================
# Pipeline entry points
# ============================================================================


def run_expand(
    network: pypsa.Network,
    solver: str,
    output_directory: str | Path,
    investment_slack_cost: float = 0.0,
    pre_solve: str = "full",
    external_stop_price: float = 0.0,
) -> None:
    """Capacity expansion pre-solve followed by a single dispatch solve.

    Parameters
    ----------
    network : pypsa.Network
        Built and parameterised network (modified in-place).
    solver : str
        Solver name (``"highs"`` or ``"gurobi"``).
    output_directory : str | Path
        Directory for log files and results output.
    investment_slack_cost : float
        If > 0, temporarily reduce slack generator cost during investment pre-solve.
    pre_solve : str
        Investment mode: ``"full"`` or ``"island"``.
    external_stop_price : float
        If > 0, cap slack costs for non-investment zones.

    """
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    _apply_external_stop_price(network, external_stop_price)
    investment_pre_solve(
        network,
        solver,
        output_directory,
        investment_slack_cost=investment_slack_cost,
        pre_solve=pre_solve,
    )
    solve_network(network, solver, output_directory)
    export_results(network, output_directory)


def run_iterate(
    network: pypsa.Network,
    solver: str,
    output_directory: str | Path,
    investment_slack_cost: float = 0.0,
    pre_solve: str = "full",
    external_stop_price: float = 0.0,
    **two_stage_kwargs: Any,  # noqa: ANN401
) -> dict:
    """Capacity expansion pre-solve followed by iterative adjustment until convergence.

    Parameters
    ----------
    network : pypsa.Network
        Built and parameterised network (modified in-place).
    solver : str
        Solver name (``"highs"`` or ``"gurobi"``).
    output_directory : str | Path
        Directory for log files and results output.
    investment_slack_cost : float
        If > 0, temporarily reduce slack generator cost during investment pre-solve.
    pre_solve : str
        Investment mode: ``"full"`` or ``"island"``.
    external_stop_price : float
        If > 0, cap slack costs for non-investment zones.
    **two_stage_kwargs : Any
        Forwarded to :func:`two_stage_solve` (convergence thresholds, schedule,
        warmup parameters, commitment kwargs, etc.).

    Returns
    -------
    dict
        Two-stage history dict containing per-iteration prices, dispatch,
        objectives, capacity adjustments, and the convergence reason.

    """
    output_directory = Path(output_directory)
    output_directory.mkdir(parents=True, exist_ok=True)

    _apply_external_stop_price(network, external_stop_price)
    investment_pre_solve(
        network,
        solver,
        output_directory,
        investment_slack_cost=investment_slack_cost,
        pre_solve=pre_solve,
    )
    _, history = two_stage_solve(network, solver, output_directory, **two_stage_kwargs)
    export_results(network, output_directory)
    return history
