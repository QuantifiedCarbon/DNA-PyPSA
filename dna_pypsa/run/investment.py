"""Investment pre-solve: determine optimal capacities before the dispatch solve."""

import logging
from pathlib import Path

import pandas as pd
import pypsa

from dna_pypsa import constants

logger = logging.getLogger(__name__)


# ============================================================================
# Helpers
# ============================================================================


def _apply_island_mode(
    network: pypsa.Network,
    extendable: pd.DataFrame,
    extendable_links: pd.DataFrame,
) -> pd.Series | None:
    """Penalise cross-border links to isolate investment zones.

    Identifies AC buses that contain extendable generators or extendable storage
    links, then finds links that cross between an investment zone and a
    non-investment AC zone.  Their ``marginal_cost`` is raised to
    :data:`~dna_pypsa.constants.DEFAULT_STOP_PRICE` to remove export-arbitrage
    incentives while still allowing emergency imports.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.
    extendable : pd.DataFrame
        Extendable generators (subset of ``network.generators``).
    extendable_links : pd.DataFrame
        Extendable links (subset of ``network.links``).

    Returns
    -------
    pd.Series | None
        Original ``marginal_cost`` values for the penalised links (to restore
        later), or ``None`` if no cross-border links were found.

    """
    links = network.links
    investment_zones = set(extendable["bus"].values) if not extendable.empty else set()

    for link_name in extendable_links.index:
        b0 = links.loc[link_name, "bus0"]
        b1 = links.loc[link_name, "bus1"]
        if b0 in network.buses.index and network.buses.loc[b0, "carrier"] == "AC":
            investment_zones.add(b0)
        if b1 in network.buses.index and network.buses.loc[b1, "carrier"] == "AC":
            investment_zones.add(b1)

    ac_buses = set(network.buses[network.buses.carrier == "AC"].index)
    cross_border = [
        name
        for name in network.links.index
        if (
            (
                network.links.loc[name, "bus0"] in investment_zones
                and network.links.loc[name, "bus1"] in ac_buses
                and network.links.loc[name, "bus1"] not in investment_zones
            )
            or (
                network.links.loc[name, "bus1"] in investment_zones
                and network.links.loc[name, "bus0"] in ac_buses
                and network.links.loc[name, "bus0"] not in investment_zones
            )
        )
    ]

    if not cross_border:
        logger.warning(
            "Island mode: no cross-border links found for zones %s",
            sorted(investment_zones),
        )
        return None

    saved = network.links.loc[cross_border, "marginal_cost"].copy()
    network.links.loc[cross_border, "marginal_cost"] = constants.DEFAULT_STOP_PRICE
    logger.info(
        "Island mode: penalised %d cross-border links for zones %s "
        "(marginal_cost → %.0f)",
        len(cross_border),
        sorted(investment_zones),
        constants.DEFAULT_STOP_PRICE,
    )
    return saved


def _zero_costs_for_lp(
    network: pypsa.Network,
    investment_slack_cost: float,
) -> dict:
    """Zero quadratic costs and prepare the network for a pure LP investment solve.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.
    investment_slack_cost : float
        If > 0, slack generator costs are temporarily reduced to this value.

    Returns
    -------
    dict
        Saved values keyed by ``"gen_mcq"``, ``"gen_mc"``, ``"store_mcq"``,
        ``"link_mcq"``, ``"link_mc"``, ``"p_min_pu"``, ``"slack_mc"``,
        ``"slack_gens"``.

    """
    gens = network.generators
    saved_mcq = gens["marginal_cost_quadratic"].copy()
    saved_mc = gens["marginal_cost"].copy()

    if "supply_curve_width" in gens.columns:
        width_shift = gens["supply_curve_width"].fillna(0.0)
        network.generators.loc[:, "marginal_cost"] = saved_mc + width_shift
        logger.info(
            "Shifted marginal_cost to supply-curve midpoint for %d generators "
            "with supply_curve_width.",
            (width_shift > 0).sum(),
        )

    saved_p_min_pu = network.generators_t.p_min_pu.copy()
    if not saved_p_min_pu.empty:
        network.generators_t.p_min_pu = pd.DataFrame(
            index=network.snapshots,
            dtype=float,
        )
        logger.info(
            "Suspended must-run p_min_pu for %d generators during investment pre-solve.",
            len(saved_p_min_pu.columns),
        )

    demand_mask = network.generators.get("sign", 1) == -1
    network.generators.loc[~demand_mask, "marginal_cost_quadratic"] = 0.0

    stores = network.stores
    if "marginal_cost_quadratic" in stores.columns:
        saved_store_mcq = stores["marginal_cost_quadratic"].copy()
        network.stores.loc[:, "marginal_cost_quadratic"] = 0.0
    else:
        saved_store_mcq = None

    links = network.links
    if "marginal_cost_quadratic" in links.columns:
        saved_link_mcq = links["marginal_cost_quadratic"].copy()
        saved_link_mc = links["marginal_cost"].copy()
        link_width = (saved_link_mcq * links["p_nom"] / 2).fillna(0.0)
        network.links.loc[:, "marginal_cost"] = saved_link_mc + link_width
        network.links.loc[:, "marginal_cost_quadratic"] = 0.0
        logger.info(
            "Shifted link marginal_cost to supply-curve midpoint for %d links "
            "with quadratic costs.",
            (link_width > 0).sum(),
        )
    else:
        saved_link_mcq = None
        saved_link_mc = None

    logger.info("Zeroed all quadratic costs for investment pre-solve (pure LP).")

    slack_gens = [g for g in network.generators.index if "slack" in g]
    if investment_slack_cost > 0 and slack_gens:
        saved_slack_mc = network.generators.loc[slack_gens, "marginal_cost"].copy()
        network.generators.loc[slack_gens, "marginal_cost"] = investment_slack_cost
        logger.info(
            "Reduced slack generator cost to %.1f for investment pre-solve "
            "(%d slack generators).",
            investment_slack_cost,
            len(slack_gens),
        )
    else:
        saved_slack_mc = None

    return {
        "gen_mcq": saved_mcq,
        "gen_mc": saved_mc,
        "store_mcq": saved_store_mcq,
        "link_mcq": saved_link_mcq,
        "link_mc": saved_link_mc,
        "p_min_pu": saved_p_min_pu,
        "slack_mc": saved_slack_mc,
        "slack_gens": slack_gens,
    }


def _lock_investment_capacities(
    network: pypsa.Network,
    extendable: pd.DataFrame,
    extendable_stores: pd.DataFrame,
    extendable_links: pd.DataFrame,
    gen_mcq_saved: pd.Series,
) -> None:
    """Lock optimal capacities and restore final quadratic costs after the LP solve.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.
    extendable : pd.DataFrame
        Extendable generators (subset of ``network.generators``).
    extendable_stores : pd.DataFrame
        Extendable stores (subset of ``network.stores``).
    extendable_links : pd.DataFrame
        Extendable links (subset of ``network.links``).
    gen_mcq_saved : pd.Series
        Original ``marginal_cost_quadratic`` values for all generators.

    """
    gens = network.generators
    for gen_name in extendable.index:
        p_nom_opt = network.generators.loc[gen_name, "p_nom_opt"]
        logger.info(
            "  %s: p_nom_opt = %.1f MW (was p_nom = %.1f)",
            gen_name,
            p_nom_opt,
            extendable.loc[gen_name, "p_nom"],
        )
        network.generators.loc[gen_name, "p_nom"] = p_nom_opt
        network.generators.loc[gen_name, "p_nom_extendable"] = False

        if "supply_curve_width" in gens.columns:
            supply_curve_width = network.generators.loc[gen_name, "supply_curve_width"]
            recalc = (
                pd.notna(supply_curve_width)
                and supply_curve_width > 0
                and p_nom_opt > constants.CAP_MCQ_RECALC_MIN_MW
            )
            if recalc:
                new_mcq = supply_curve_width / p_nom_opt
                network.generators.loc[gen_name, "marginal_cost_quadratic"] = new_mcq
                logger.info(
                    "  %s: recalculated marginal_cost_quadratic = %.6f "
                    "(width=%.1f, p_nom=%.1f)",
                    gen_name,
                    new_mcq,
                    supply_curve_width,
                    p_nom_opt,
                )
            else:
                network.generators.loc[gen_name, "marginal_cost_quadratic"] = (
                    gen_mcq_saved.loc[gen_name]
                )
        else:
            network.generators.loc[gen_name, "marginal_cost_quadratic"] = (
                gen_mcq_saved.loc[gen_name]
            )

    non_extendable = gens.index.difference(extendable.index)
    network.generators.loc[non_extendable, "marginal_cost_quadratic"] = (
        gen_mcq_saved.loc[non_extendable]
    )

    for store_name in extendable_stores.index:
        e_nom_opt = network.stores.loc[store_name, "e_nom_opt"]
        logger.info(
            "  %s: e_nom_opt = %.1f MWh (was e_nom = %.1f)",
            store_name,
            e_nom_opt,
            extendable_stores.loc[store_name, "e_nom"],
        )
        network.stores.loc[store_name, "e_nom"] = e_nom_opt
        network.stores.loc[store_name, "e_nom_extendable"] = False

    for link_name in extendable_links.index:
        p_nom_opt = network.links.loc[link_name, "p_nom_opt"]
        logger.info(
            "  %s: p_nom_opt = %.1f MW (was p_nom = %.1f)",
            link_name,
            p_nom_opt,
            extendable_links.loc[link_name, "p_nom"],
        )
        network.links.loc[link_name, "p_nom"] = p_nom_opt
        network.links.loc[link_name, "p_nom_extendable"] = False


def _restore_lp_costs(
    network: pypsa.Network,
    saved: dict,
    saved_link_mc_island: pd.Series | None,
) -> None:
    """Restore all network costs modified by :func:`_zero_costs_for_lp`.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.
    saved : dict
        Dict returned by :func:`_zero_costs_for_lp`.
    saved_link_mc_island : pd.Series | None
        Saved cross-border link costs from island mode, or ``None``.

    """
    network.generators.loc[:, "marginal_cost"] = saved["gen_mc"]

    if saved["store_mcq"] is not None:
        network.stores.loc[:, "marginal_cost_quadratic"] = saved["store_mcq"]

    if saved["link_mcq"] is not None:
        network.links.loc[:, "marginal_cost_quadratic"] = saved["link_mcq"]
        network.links.loc[:, "marginal_cost"] = saved["link_mc"]

    if saved["slack_mc"] is not None:
        network.generators.loc[saved["slack_gens"], "marginal_cost"] = saved["slack_mc"]
        logger.info("Restored slack generator costs for dispatch solve.")

    if not saved["p_min_pu"].empty:
        network.generators_t.p_min_pu = saved["p_min_pu"]
        logger.info(
            "Restored must-run p_min_pu for %d generators.",
            len(saved["p_min_pu"].columns),
        )

    if saved_link_mc_island is not None:
        network.links.loc[saved_link_mc_island.index, "marginal_cost"] = (
            saved_link_mc_island
        )
        logger.info(
            "Restored marginal_cost on %d cross-border links for dispatch solve.",
            len(saved_link_mc_island),
        )


def _update_curtailment_capacities(network: pypsa.Network) -> None:
    """Ensure curtailment link capacity covers total generation at each bus.

    Parameters
    ----------
    network : pypsa.Network
        Network to modify in-place.

    """
    for bus in network.buses.index[network.buses.carrier == "AC"]:
        link_name = f"{bus}_curtailment"
        if link_name not in network.links.index:
            continue

        bus_gens = network.generators[network.generators.bus == bus]
        total_gen = float(bus_gens["p_nom"].sum())
        if total_gen <= 0:
            continue

        required_cap = max(10_000.0, total_gen)
        current_cap = float(network.links.loc[link_name, "p_nom"])
        if required_cap > current_cap:
            network.links.loc[link_name, "p_nom"] = required_cap
            logger.info(
                "Updated curtailment capacity for %s: %.0f → %.0f MW",
                link_name,
                current_cap,
                required_cap,
            )


# ============================================================================
# Public API
# ============================================================================


def investment_pre_solve(
    network: pypsa.Network,
    solver: str,
    output_directory: str | Path,
    investment_slack_cost: float = 0.0,
    pre_solve: str = "full",
) -> bool:
    """Run an investment pre-solve to determine optimal capacities.

    If any generators have ``p_nom_extendable=True``, this runs a linear solve
    (with ``marginal_cost_quadratic`` temporarily zeroed) to find ``p_nom_opt``.
    It then locks capacities and restores the original quadratic cost.

    Parameters
    ----------
    network : pypsa.Network
        The network to pre-solve (modified in-place).
    solver : str
        Solver name.
    output_directory : str | Path
        Directory for solver log files.
    investment_slack_cost : float
        If > 0, temporarily reduce slack generator marginal_cost to this
        value during the investment pre-solve, then restore it afterwards.
    pre_solve : str
        Investment mode: ``"full"`` (default) or ``"island"`` (isolate
        investment zones from cross-border transmission).

    Returns
    -------
    bool
        ``True`` if an investment solve was performed, ``False`` if no
        extendable components were found.

    Raises
    ------
    ValueError
        If the investment solve fails or is infeasible.

    """
    output_directory = Path(output_directory)
    gens = network.generators
    extendable = gens[gens["p_nom_extendable"]]
    extendable_stores = network.stores[network.stores["e_nom_extendable"]]
    extendable_links = network.links[network.links["p_nom_extendable"]]

    network.generators["is_extendable"] = False
    network.generators.loc[extendable.index, "is_extendable"] = True

    if extendable.empty and extendable_stores.empty and extendable_links.empty:
        logger.info(
            "No extendable generators, stores, or links — skipping investment pre-solve.",
        )
        return False

    if not extendable.empty:
        logger.info(
            "Investment pre-solve (%s): %d extendable generators: %s",
            pre_solve,
            len(extendable),
            list(extendable.index),
        )
    if not extendable_stores.empty:
        logger.info(
            "Investment pre-solve (%s): %d extendable stores: %s",
            pre_solve,
            len(extendable_stores),
            list(extendable_stores.index),
        )

    saved_link_mc_island = None
    if pre_solve == "island":
        saved_link_mc_island = _apply_island_mode(network, extendable, extendable_links)

    saved = _zero_costs_for_lp(network, investment_slack_cost)

    logger.info("Pass 1: solving with linear costs to determine optimal capacities…")
    log_path = output_directory / f"{solver}_investment_log.txt"
    if solver == "gurobi":
        network.optimize.create_model(include_objective_constant=False)
        opt_result = network.optimize.solve_model(
            solver_name="gurobi",
            logfile=str(log_path),
            env={"LogToConsole": 0},
            solver_options={
                "DualReductions": 0,
                "ScaleFlag": 2,
                "Method": 2,
                "Crossover": 0,
                "BarConvTol": 5e-4,
            },
        )
    else:
        opt_result = network.optimize(solver_name=solver, logfile=str(log_path))

    if "ok" not in opt_result:
        if "infeasible" in str(opt_result):
            try:
                network.model.print_infeasibilities()
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Could not compute IIS — Gurobi job may have been aborted."
                )
        msg = (
            f"Investment pre-solve failed: {opt_result}. "
            f"Check {log_path} for solver output."
        )
        raise ValueError(msg)
    logger.info("Investment pre-solve status: %s", opt_result)

    _lock_investment_capacities(
        network,
        extendable,
        extendable_stores,
        extendable_links,
        saved["gen_mcq"],
    )
    _restore_lp_costs(network, saved, saved_link_mc_island)
    _update_curtailment_capacities(network)

    logger.info(
        "Investment pre-solve complete. Capacities locked, proceeding to dispatch solve.",
    )
    return True
