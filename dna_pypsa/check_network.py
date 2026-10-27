"""PyPSA Network Validation Module.

Provides two main functions:
- check_unoptimized_network(): Validates network structure before optimization
- check_optimized_network(): Validates optimization results after solving

Uses modular helper functions for checking specific components.

WARNING this was heavily vibe-coded

"""

import argparse
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pypsa


def print_section(title: str) -> None:
    """Print a formatted section header.

    Parameters
    ----------
    title : str
        The title of the section to print.

    """
    print(f"\n{'='*70}")
    print(f"  {title}")
    print(f"{'='*70}")


def print_subsection(title: str) -> None:
    """Print a formatted subsection header.

    Parameters
    ----------
    title : str
        The title of the subsection to print.

    """
    print(f"\n  {title}")
    print(f"  {'-'*66}")


def report(status: bool, message: str, details: str | None = "") -> None:
    """Print a formatted status report line.

    Parameters
    ----------
    status : bool
        The status of the check (True for pass, False for fail).
    message : str
        The message to print.
    details : str | None, optional
        Additional details to print (default is "").

    """
    symbol = "✅" if status else "❌"
    print(f"  {symbol} {message}")
    if details:
        print(f"     {details}")


def warning(message: str, details: str | None = "") -> None:
    """Print a warning message.

    Parameters
    ----------
    message : str
        The warning message to print.
    details : str | None, optional
        Additional details to print (default is "").

    """
    print(f"  ⚠️  {message}")
    if details:
        print(f"     {details}")


# ============================================================================
# HELPER FUNCTIONS FOR UNOPTIMIZED NETWORK CHECKS
# ============================================================================


def check_generators_structure(network: pypsa.Network) -> bool:
    """Check generator component structure and parameters.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the generator structure is valid, False otherwise.

    """
    print_subsection("Generators")

    if len(network.generators) == 0:
        report(False, "No generators in network")
        return False

    report(True, f"Generator count: {len(network.generators)}")

    # Check critical columns
    critical_cols = {"p_nom", "bus", "type"}
    missing_cols = critical_cols - set(network.generators.columns)
    if missing_cols:
        report(False, f"Missing critical columns: {missing_cols}")
        return False

    # Check for NaN in p_nom
    nan_p_nom = network.generators["p_nom"].isna().sum()
    if nan_p_nom > 0:
        report(False, f"NaN values in p_nom: {nan_p_nom} generators")
        return False

    report(True, "All generators have valid p_nom")

    # Check for Inf values (problematic for solver)
    inf_p_nom_max = network.generators["p_nom_max"].isin([np.inf, -np.inf]).sum()
    if inf_p_nom_max > 0:
        warning(
            f"Inf values in p_nom_max: {inf_p_nom_max} generators",
            "This can cause numerical scaling issues in solver",
        )
    else:
        report(True, "No Inf values in p_nom_max")

    # Check ramp limits
    nan_ramp = network.generators[["ramp_limit_up", "ramp_limit_down"]].isna().sum()
    if nan_ramp.sum() > 0:
        warning(
            f"Missing ramp limits: {nan_ramp.sum()} entries",
            "These generators have no ramp rate constraints (acceptable)",
        )
    else:
        report(True, "All generators have ramp limits defined")

    # Check p_min_pu and p_max_pu
    zero_max_pu = (network.generators["p_max_pu"] == 0).sum()
    if zero_max_pu > 0:
        warning(
            f"Generators with p_max_pu=0: {zero_max_pu}",
            "These units cannot generate power",
        )

    # Check for must-run units
    must_run = (network.generators["p_min_pu"] > 0).sum()
    if must_run > 0:
        min_gen = (network.generators["p_nom"] * network.generators["p_min_pu"]).sum()
        report(
            True,
            f"Must-run generators: {must_run} with minimum {min_gen:,.0f} MW output",
        )

    return True


def check_loads_structure(network: pypsa.Network) -> bool:
    """Check load component structure and time-series data.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the load structure is valid, False otherwise.

    """
    print_subsection("Loads")

    if len(network.loads) == 0:
        report(False, "No loads in network")
        return False

    report(True, f"Load count: {len(network.loads)}")

    # Check critical columns
    critical_cols = {"bus", "p_set"}
    missing_cols = critical_cols - set(network.loads.columns)
    if missing_cols:
        report(False, f"Missing critical columns: {missing_cols}")
        return False

    # Check for static loads vs time-series
    static_loads = (
        network.loads["p_set"]
        .apply(lambda x: not isinstance(x, (pd.Series, pd.DataFrame)))
        .sum()
    )
    ts_loads = len(network.loads) - static_loads

    report(True, f"Static loads: {static_loads}, Time-series loads: {ts_loads}")

    # Check for NaN in static loads
    for idx, row in network.loads.iterrows():
        if not isinstance(row["p_set"], (pd.Series, pd.DataFrame)) and pd.isna(
            row["p_set"],
        ):
            warning(f"Load {idx} has NaN p_set")
            return False

    # Check time-series loads
    for idx, row in network.loads.iterrows():
        if isinstance(row["p_set"], (pd.Series, pd.DataFrame)):
            if row["p_set"].isna().all():
                warning(f"Load {idx} time-series is all NaN")

    return True


def check_storage_units_structure(network: pypsa.Network) -> bool:
    """Check storage unit (battery, pumped hydro) structure.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the storage unit structure is valid, False otherwise.

    """
    print_subsection("Storage Units")

    if len(network.storage_units) == 0:
        report(True, "No storage units in network (expected if using Stores for hydro)")
        return True

    report(True, f"Storage unit count: {len(network.storage_units)}")

    # Check for zero-capacity units (constraint conflicts)
    zero_capacity = (network.storage_units["p_nom"] == 0).sum()
    if zero_capacity > 0:
        warning(
            f"Zero-capacity storage units: {zero_capacity}",
            "These cannot dispatch and may cause constraint conflicts",
        )
        zero_units = network.storage_units[
            network.storage_units["p_nom"] == 0
        ].index.tolist()
        print(f"     Units: {zero_units[:5]}{'...' if len(zero_units) > 5 else ''}")
    else:
        report(True, "All storage units have valid capacity (p_nom > 0)")

    # Check critical columns
    critical_cols = {"p_nom", "bus", "efficiency_store", "efficiency_dispatch"}
    missing_cols = critical_cols - set(network.storage_units.columns)
    if missing_cols:
        report(False, f"Missing critical columns: {missing_cols}")
        return False

    # Check efficiency values
    invalid_eff = (
        (network.storage_units["efficiency_store"] < 0)
        | (network.storage_units["efficiency_store"] > 1)
    ).sum()
    invalid_eff += (
        (network.storage_units["efficiency_dispatch"] < 0)
        | (network.storage_units["efficiency_dispatch"] > 1)
    ).sum()

    if invalid_eff > 0:
        report(False, f"Invalid efficiency values: {invalid_eff} entries")
        return False

    report(True, "All storage units have valid efficiency (0-1)")

    return True


def check_stores_hydro_structure(network: pypsa.Network) -> bool:
    """Check Stores (hydro reservoirs) structure.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the stores structure is valid, False otherwise.

    """
    print_subsection("Stores (Hydro Reservoirs)")

    if len(network.stores) == 0:
        report(True, "No stores in network")
        return True

    report(True, f"Store count: {len(network.stores)}")

    # Check for hydro stores (should have inflow)
    hydro_stores = network.stores[
        network.stores.index.astype(str).str.contains(
            "hydro|store_hydro",
            case=False,
            na=False,
        )
    ]
    if len(hydro_stores) > 0:
        report(True, f"Hydro/water stores: {len(hydro_stores)}")

        # Check inflow time-series (stored in network.stores_t.inflow, not static attributes)
        # Only check if inflow data exists
        if (
            hasattr(network.stores_t, "inflow")
            and len(network.stores_t.inflow.columns) > 0
        ):
            missing_inflow = 0
            for store_name in hydro_stores.index:
                if store_name not in network.stores_t.inflow.columns:
                    missing_inflow += 1
                else:
                    inflow_total = network.stores_t.inflow[store_name].sum()
                    if inflow_total == 0:
                        missing_inflow += 1

            if missing_inflow > 0:
                warning(
                    f"Hydro stores without inflow data: {missing_inflow}",
                    "Inflow must be provided for energy conservation",
                )
            else:
                report(True, "All hydro stores have inflow data")
        else:
            # Inflow data structure doesn't exist - might be okay for non-hydro networks
            report(
                True,
                "No inflow time-series structure found (OK for non-hydro networks)",
            )

    # Check capacity
    zero_cap = (network.stores["e_nom"] == 0).sum()
    if zero_cap > 0:
        warning(f"Zero-capacity stores: {zero_cap}")

    return True


def check_links_structure(network: pypsa.Network) -> bool:
    """Check transmission links and turbines.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the links structure is valid, False otherwise.

    """
    print_subsection("Links (Transmission & Turbines)")

    if len(network.links) == 0:
        report(True, "No links in network")
        return True

    report(True, f"Link count: {len(network.links)}")

    # Check turbine links (hydro)
    turbines = network.links[
        network.links.index.astype(str).str.contains("turbine", case=False, na=False)
    ]
    if len(turbines) > 0:
        report(True, f"Turbine links: {len(turbines)}")

        # Check connections
        for idx, row in turbines.iterrows():
            bus0, bus1 = row["bus0"], row["bus1"]
            if bus0 not in network.buses.index:
                warning(f"Turbine {idx} connected to unknown bus0: {bus0}")
            if bus1 not in network.buses.index:
                warning(f"Turbine {idx} connected to unknown bus1: {bus1}")

    # Check transmission links
    transmissions = network.links[
        ~network.links.index.astype(str).str.contains("turbine", case=False, na=False)
    ]
    if len(transmissions) > 0:
        report(True, f"Transmission links: {len(transmissions)}")

    # Check for Inf values
    inf_count = network.links["p_nom_max"].isin([np.inf, -np.inf]).sum()
    if inf_count > 0:
        warning(
            f"Inf values in link p_nom_max: {inf_count}",
            "Can cause numerical scaling issues",
        )

    return True


def check_numerical_properties(network: pypsa.Network) -> bool:
    """Check for numerical issues that could cause solver problems.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if no numerical issues are detected, False otherwise.

    """
    print_subsection("Numerical Properties")

    issues = 0

    # Check for extreme values in capacity columns
    for component_name in ["generators", "loads", "storage_units", "links"]:
        if component_name not in dir(network):
            continue

        component = getattr(network, component_name)
        if len(component) == 0:
            continue

        # Check p_nom range
        if "p_nom" in component.columns:
            p_nom = component["p_nom"].replace([np.inf, -np.inf], np.nan)
            if p_nom.max() > 0:
                ratio = p_nom.max() / (p_nom[p_nom > 0].min() + 1e-6)
                if ratio > 1e6:
                    warning(
                        f"{component_name}: Large range in p_nom (ratio {ratio:.0e})",
                        "May cause numerical scaling issues",
                    )
                    issues += 1

    if issues == 0:
        report(True, "No extreme numerical values detected")

    return True


def check_network_structure(network: pypsa.Network) -> bool:
    """Check basic network structure and connectivity.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if the network structure is valid, False otherwise.

    """
    print_subsection("Network Structure")

    report(True, f"Buses: {len(network.buses)}")
    report(True, f"Snapshots: {len(network.snapshots)}")
    report(True, f"Generators: {len(network.generators)}")
    report(True, f"Loads: {len(network.loads)}")
    report(True, f"Storage Units: {len(network.storage_units)}")
    report(True, f"Stores: {len(network.stores)}")
    report(True, f"Links: {len(network.links)}")

    # Check time-series index
    if hasattr(network, "snapshots"):
        report(True, f"Time period: {network.snapshots[0]} to {network.snapshots[-1]}")

    return True


# ============================================================================
# HELPER FUNCTIONS FOR OPTIMIZED NETWORK CHECKS
# ============================================================================


def check_optimization_status(network: pypsa.Network) -> bool:
    """Check if optimization was successful.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if optimization status is OK, False otherwise.

    """
    print_subsection("Optimization Status")

    # Check status attribute
    if hasattr(network, "status") and network.status:
        status_ok = "ok" in str(network.status[0]).lower()
        report(status_ok, f"Status: {network.status}")
    else:
        report(False, "No optimization status found")
        return False

    # Check objective value
    if hasattr(network, "objective") and network.objective is not None:
        report(True, f"Objective value: €{network.objective:,.2f}")
    else:
        report(False, "No objective value found")
        return False

    return status_ok if "status_ok" in locals() else False


def check_generator_results(network: pypsa.Network) -> bool:
    """Check generator optimization results.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if generator results are valid, False otherwise.

    """
    print_subsection("Generator Results")

    # Check if p_set exists and has values
    if "p" not in network.generators.columns:
        report(False, "Generator results not found (missing 'p' column)")
        return False

    # Check for non-NaN results
    nan_count = network.generators["p"].isna().sum().sum()
    total_count = network.generators["p"].shape[0] * network.generators["p"].shape[1]

    if nan_count == total_count:
        report(False, "All generator dispatch values are NaN")
        return False

    report(
        True,
        f"Generator dispatch results present ({total_count - nan_count:,.0f}/{total_count} values)",
    )

    # Check total generation vs demand
    total_gen = network.generators["p"].sum().sum()
    total_load = network.loads["p_set"].sum()

    if isinstance(total_load, pd.Series):
        total_load = total_load.sum()

    gen_load_ratio = total_gen / (total_load + 1e-6)
    report(
        True,
        f"Total generation: {total_gen:,.0f} MWh, Total load: {total_load:,.0f} MWh",
    )

    if abs(gen_load_ratio - 1.0) > 0.05:
        warning(
            f"Generation-to-load ratio: {gen_load_ratio:.3f} (not close to 1.0)",
            "Energy balance may not be satisfied",
        )
        return False

    return True


def check_storage_results(network: pypsa.Network) -> bool:
    """Check storage unit optimization results.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if storage results are valid, False otherwise.

    """
    print_subsection("Storage Results")

    if len(network.storage_units) == 0:
        report(True, "No storage units to check")
        return True

    # Check if results exist
    if "e" not in network.storage_units.columns:
        report(False, "Storage results not found (missing 'e' column)")
        return False

    nan_count = network.storage_units["e"].isna().sum().sum()
    total_count = (
        network.storage_units["e"].shape[0] * network.storage_units["e"].shape[1]
    )

    if nan_count == total_count:
        warning("All storage energy values are NaN", "Results may not be populated")
    else:
        report(
            True,
            f"Storage energy results present ({total_count - nan_count:,.0f}/{total_count} values)",
        )

    # Check energy bounds
    for idx, row in network.storage_units.iterrows():
        if "e" in network.storage_units.columns:
            e_series = network.storage_units.loc[idx, "e"]
            if isinstance(e_series, pd.Series):
                violations = (
                    (e_series < row["e_min_pu"] * row["e_nom"])
                    | (e_series > row["e_max_pu"] * row["e_nom"])
                ).sum()
                if violations > 0:
                    warning(
                        f"Storage {idx}: Energy constraint violations: {violations} snapshots",
                    )

    return True


def check_load_results(network: pypsa.Network) -> bool:
    """Check load time-series satisfaction.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if load satisfaction looks reasonable, False otherwise.

    """
    print_subsection("Load Satisfaction")

    # For static loads, p_set should be satisfied by generators
    # For dynamic loads, p_set time-series should match supply
    report(True, f"Load count: {len(network.loads)}")

    # Check total load vs generation (should be equal)
    total_load = 0
    for _, row in network.loads.iterrows():
        if isinstance(row["p_set"], pd.Series):
            total_load += row["p_set"].sum()
        else:
            total_load += row["p_set"] * len(network.snapshots)

    report(True, f"Total load demand: {total_load:,.0f} MWh")

    return True


def check_hydro_energy_balance(network: pypsa.Network) -> bool:
    """Check hydro resource energy conservation.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if hydro energy balance looks reasonable, False otherwise.

    """
    print_subsection("Hydro Energy Balance")

    hydro_stores = network.stores[
        network.stores.index.astype(str).str.contains(
            "hydro|store_hydro",
            case=False,
            na=False,
        )
    ]

    if len(hydro_stores) == 0:
        report(True, "No hydro stores to check")
        return True

    report(True, f"Checking {len(hydro_stores)} hydro stores")

    all_balanced = True
    for idx, store_row in hydro_stores.iterrows():
        # Find associated turbine
        turbine_name = idx.replace("store_", "turbine_")

        if turbine_name not in network.links.index:
            warning(f"Hydro store {idx}: No associated turbine link found")
            all_balanced = False
            continue

        # Check if inflow exists
        if "inflow" not in store_row.index:
            warning(f"Hydro store {idx}: No inflow data")
            all_balanced = False
            continue

        # Get inflow, discharge, storage change
        if isinstance(store_row["inflow"], pd.Series):
            inflow = store_row["inflow"].sum()
        else:
            inflow = (
                store_row["inflow"] * len(network.snapshots)
                if store_row["inflow"]
                else 0
            )

        if "p" in network.links.loc[turbine_name]:
            discharge = network.links.loc[turbine_name, "p"].sum()
        else:
            discharge = 0

        if "e" in network.stores.loc[idx]:
            storage_change = (
                network.stores.loc[idx, "e"].iloc[-1]
                - network.stores.loc[idx, "e"].iloc[0]
            )
        else:
            storage_change = 0

        balance = inflow - discharge - storage_change
        balance_pct = abs(balance) / (inflow + 1e-6) * 100

        if balance_pct < 1:
            report(True, f"{idx}: Energy balance OK (error: {balance_pct:.2f}%)")
        else:
            warning(
                f"{idx}: Energy balance error: {balance_pct:.2f}%",
                f"Inflow: {inflow:.0f}, Discharge: {discharge:.0f}, Storage Δ: {storage_change:.0f}",
            )
            all_balanced = False

    return all_balanced


def check_marginal_costs(network: pypsa.Network) -> bool:
    """Check marginal costs and objective function components.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if marginal costs look reasonable, False otherwise.

    """
    print_subsection("Cost Analysis")

    # Check generator marginal costs
    if "marginal_cost" in network.generators.columns:
        nan_costs = network.generators["marginal_cost"].isna().sum()
        inf_costs = network.generators["marginal_cost"].isin([np.inf, -np.inf]).sum()

        if nan_costs > 0:
            warning(f"Generators with NaN marginal_cost: {nan_costs}")
        if inf_costs > 0:
            warning(f"Generators with Inf marginal_cost: {inf_costs}")

        if nan_costs == 0 and inf_costs == 0:
            report(True, "All generators have valid marginal costs")

    return True


def check_hydro_structure(network: pypsa.Network) -> bool:
    """Check hydro resource structure and verify proper connectivity.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if hydro structure looks reasonable, False otherwise.

    """
    print_subsection("Hydro Resources Structure")

    # Find all hydro stores
    hydro_stores = network.stores[
        network.stores.index.astype(str).str.contains(
            "hydro|store_",
            case=False,
            na=False,
        )
    ]

    if len(hydro_stores) == 0:
        report(True, "No hydro stores in network")
        return True

    report(True, f"Found {len(hydro_stores)} hydro stores")

    hydro_summary = []

    for store_idx, store_row in hydro_stores.iterrows():
        # Extract zone from store name (e.g., "store_Hydro_NO125" -> "NO125")
        parts = store_idx.split("_")
        hydro_name = "_".join(parts[1:]) if len(parts) > 1 else store_idx
        zone = (
            store_row["bus"].replace("hydro_", "")
            if "hydro_" in store_row["bus"]
            else "unknown"
        )

        # Find corresponding turbine link
        turbine_name = f"turbine_{hydro_name}"
        turbine_found = turbine_name in network.links.index

        if not turbine_found:
            warning(
                f"Hydro store {store_idx}: No corresponding turbine link '{turbine_name}'",
            )
            continue

        turbine_row = network.links.loc[turbine_name]

        # Check connections
        bus0_ok = turbine_row["bus0"] == store_row["bus"]
        bus1_ok = turbine_row["bus1"] == zone

        if not bus0_ok or not bus1_ok:
            warning(
                f"Turbine {turbine_name}: Incorrect bus connections",
                f"bus0: {turbine_row['bus0']} (expected {store_row['bus']}), "
                f"bus1: {turbine_row['bus1']} (expected {zone})",
            )
            continue

        # Get capacity and efficiency
        capacity = store_row.get("e_nom", 0)
        efficiency = turbine_row.get("efficiency", 1.0)

        # Check inflow - stored in network.stores_t.inflow (time-series)
        total_inflow = 0
        # inflow_found_in = "not found"

        # First check: network.stores_t.inflow DataFrame
        if hasattr(network, "stores_t") and "inflow" in network.stores_t:
            if store_idx in network.stores_t.inflow.columns:
                inflow_series = network.stores_t.inflow[store_idx]
                if isinstance(inflow_series, pd.Series):
                    total_inflow = inflow_series.sum()
                    # inflow_found_in = "network.stores_t.inflow"

        # Second check: store_row direct attribute (as Series or array)
        if total_inflow == 0:
            inflow_data = store_row.get("inflow")
            if inflow_data is not None:
                if isinstance(inflow_data, pd.Series):
                    total_inflow = inflow_data.sum()
                    # inflow_found_in = "store_row Series"
                elif isinstance(inflow_data, np.ndarray):
                    total_inflow = np.sum(inflow_data)
                    # inflow_found_in = "store_row ndarray"
                elif isinstance(inflow_data, (list, tuple)):
                    total_inflow = sum(inflow_data)
                    # inflow_found_in = "store_row list/tuple"
                elif isinstance(inflow_data, (int, float)):
                    total_inflow = inflow_data * len(network.snapshots)
                    # inflow_found_in = "store_row scalar"

        hydro_info = {
            "Zone": zone,
            "Store": store_idx,
            "Capacity (MWh)": f"{capacity:.0f}",
            "Efficiency": f"{efficiency:.1%}",
            "Total Inflow (MWh)": f"{total_inflow:.0f}",
            "Status": "✅",
        }

        hydro_summary.append(hydro_info)

    if len(hydro_summary) > 0:
        hydro_df = pd.DataFrame(hydro_summary)
        print("\n  Hydro Resources Summary:")
        print("  " + "-" * 100)
        for _, row in hydro_df.iterrows():
            print(
                f"  {row['Status']} Zone {row['Zone']}: Capacity={row['Capacity (MWh)']} MWh, "
                f"Efficiency={row['Efficiency']}, Inflow={row['Total Inflow (MWh)']} MWh",
            )
        print("  " + "-" * 100)

        return True

    return True


def analyse_hydro_generation(network: pypsa.Network) -> bool:
    """Analyse hydro generation from AC bus perspective and compare to total generation.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if hydro generation analysis looks reasonable, False otherwise.


    """
    print_subsection("Hydro Generation Analysis (AC Bus Perspective)")

    if not hasattr(network, "status") or not network.status:
        report(True, "Network not optimized yet - skipping generation analysis")
        return True

    if len(network.stores) == 0:
        report(True, "No hydro stores - skipping hydro generation analysis")
        return True

    # Find turbine links (these appear as generators from AC bus perspective)
    turbine_links = network.links[
        network.links.index.astype(str).str.contains("turbine", case=False, na=False)
    ]

    if len(turbine_links) == 0:
        report(True, "No turbine links found")
        return True

    report(True, f"Analysing {len(turbine_links)} turbine links")

    hydro_gen_by_zone = {}

    # For each zone, calculate total hydro generation
    for turbine_idx, turbine_row in turbine_links.iterrows():
        zone = turbine_row["bus1"]  # Output zone (AC bus)

        # Get turbine dispatch (power output)
        if "p" in network.links.columns:
            dispatch = network.links.loc[turbine_idx, "p"]
            if isinstance(dispatch, pd.Series):
                total_gen = dispatch.sum()
            else:
                total_gen = dispatch
        else:
            total_gen = 0

        if zone not in hydro_gen_by_zone:
            hydro_gen_by_zone[zone] = 0

        hydro_gen_by_zone[zone] += total_gen

    # For each zone with hydro, calculate percentage of total generation
    print("\n  Hydro Generation by Zone (from AC bus perspective):")
    print("  " + "-" * 100)

    for zone in sorted(hydro_gen_by_zone.keys()):
        hydro_gen = hydro_gen_by_zone[zone]

        # Get total generation in zone (from all generators)
        zone_gens = network.generators[network.generators["bus"] == zone]
        total_gen = 0

        if len(zone_gens) > 0 and "p" in network.generators.columns:
            for gen_idx in zone_gens.index:
                gen_dispatch = network.generators.loc[gen_idx, "p"]
                if isinstance(gen_dispatch, pd.Series):
                    total_gen += gen_dispatch.sum()
                else:
                    total_gen += gen_dispatch

        # Add hydro generation to total
        total_with_hydro = total_gen + hydro_gen

        if total_with_hydro > 0:
            hydro_pct = (hydro_gen / total_with_hydro) * 100
            status = "✅" if hydro_pct > 10 else "⚠️"
            print(
                f"  {status} {zone}: Hydro={hydro_gen:>12,.0f} MWh "
                f"({hydro_pct:>5.1f}% of total), "
                f"Hydro+Generators={total_with_hydro:>12,.0f} MWh",
            )
        else:
            print(f"  ❌ {zone}: Hydro={hydro_gen:>12,.0f} MWh (no generation)")

    print("  " + "-" * 100)

    return True


# ============================================================================
# PER-BUS ANALYSIS FUNCTIONS
# ============================================================================


def debug_hydro_data(network: pypsa.Network) -> None:
    """Debug function to inspect actual hydro data structure for all stores.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to analyse.

    """
    print_subsection("DEBUG: Hydro Data Structure - All Stores")

    if len(network.stores) == 0:
        print("  No stores in network")
        return

    hydro_stores = network.stores[
        network.stores.index.astype(str).str.contains("store_", case=False, na=False)
    ]

    if len(hydro_stores) == 0:
        print("  No hydro stores found")
        return

    print(f"\n  Found {len(hydro_stores)} hydro stores")
    print("  Checking network.stores_t structure:")
    if hasattr(network, "stores_t"):
        if hasattr(network.stores_t, "inflow"):
            print("    ✅ stores_t.inflow EXISTS")
            print(
                f"    Columns in stores_t.inflow: {network.stores_t.inflow.columns.tolist()}",
            )
        else:
            print("    ❌ stores_t.inflow does NOT exist")
    else:
        print("    ❌ stores_t does NOT exist")

    # Check inflow for each store
    print("\n  Inflow data per store:")
    print("  " + "-" * 100)

    for store_idx in hydro_stores.index:
        store_row = network.stores.loc[store_idx]
        zone = (
            store_row["bus"].replace("hydro_", "")
            if "hydro_" in store_row["bus"]
            else "unknown"
        )

        total_inflow = 0
        inflow_source = "not found"
        inflow_details = ""

        # Check stores_t.inflow
        if hasattr(network, "stores_t") and hasattr(network.stores_t, "inflow"):
            if store_idx in network.stores_t.inflow.columns:
                inflow_series = network.stores_t.inflow[store_idx]
                total_inflow = inflow_series.sum()
                inflow_source = "stores_t.inflow"
                inflow_details = f"(shape={inflow_series.shape}, "
                inflow_details += (
                    f"min={inflow_series.min():.2f}, max={inflow_series.max():.2f})"
                )

        # Check store_row.inflow attribute if stores_t didn't have it
        if total_inflow == 0:
            inflow_data = store_row.get("inflow")
            if inflow_data is not None:
                if isinstance(inflow_data, pd.Series):
                    total_inflow = inflow_data.sum()
                    inflow_source = "store_row Series"
                elif isinstance(inflow_data, np.ndarray):
                    total_inflow = np.sum(inflow_data)
                    inflow_source = "store_row ndarray"
                elif isinstance(inflow_data, (list, tuple)):
                    total_inflow = sum(inflow_data)
                    inflow_source = "store_row list/tuple"
                elif isinstance(inflow_data, (int, float)):
                    total_inflow = inflow_data * len(network.snapshots)
                    inflow_source = f"store_row scalar ({inflow_data}) × {len(network.snapshots)} snapshots"
                inflow_details = f"(type={type(inflow_data).__name__})"

        status = "✅" if total_inflow > 0 else "⚠️"
        print(
            f"  {status} {store_idx} (Zone {zone}): {total_inflow:>15,.0f} MWh from {inflow_source} {inflow_details}",
        )


def analyse_bus_issues(network: pypsa.Network) -> pd.DataFrame:
    """Analyse network issues on a per-bus basis and create summary table.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to analyse.

    Returns
    -------
    pd.DataFrame
        DataFrame summarizing issues and warnings for each bus.

    """
    print_subsection("Per-Bus Issue Summary")

    bus_issues = []

    for bus_name in network.buses.index:
        issues = []
        warnings_list = []

        # Check generators on this bus
        bus_gens = network.generators[network.generators["bus"] == bus_name]
        if len(bus_gens) == 0:
            warnings_list.append("No generators")
        else:
            # Check for zero capacity generators
            zero_cap = (bus_gens["p_nom"] == 0).sum()
            if zero_cap > 0:
                issues.append(f"Generators with zero capacity: {zero_cap}")

            # Check for NaN p_nom
            nan_pnom = bus_gens["p_nom"].isna().sum()
            if nan_pnom > 0:
                issues.append(f"Generators with NaN p_nom: {nan_pnom}")

            # Check for invalid efficiency
            if "efficiency" in bus_gens.columns:
                invalid_eff = (
                    (bus_gens["efficiency"] < 0) | (bus_gens["efficiency"] > 1)
                ).sum()
                if invalid_eff > 0:
                    issues.append(f"Generators with invalid efficiency: {invalid_eff}")

            total_capacity = bus_gens["p_nom"].sum()
            if pd.isna(total_capacity) or total_capacity < 0:
                issues.append("Invalid total generator capacity")

        # Check loads on this bus
        bus_loads = network.loads[network.loads["bus"] == bus_name]
        if len(bus_loads) == 0:
            warnings_list.append("No loads")
        else:
            total_demand = 0
            for load_name in bus_loads.index:
                # Check time-series data first
                if load_name in network.loads_t.p_set.columns:
                    total_demand += network.loads_t.p_set[load_name].sum()
                # Then check static attribute
                elif "p_set" in bus_loads.columns:
                    static_val = bus_loads.loc[load_name, "p_set"]
                    if not pd.isna(static_val) and not isinstance(
                        static_val,
                        pd.Series,
                    ):
                        total_demand += static_val * len(network.snapshots)

            if total_demand == 0:
                issues.append("Total load demand is zero")

        # Check storage units on this bus
        bus_storage = network.storage_units[network.storage_units["bus"] == bus_name]
        if len(bus_storage) > 0:
            zero_cap = (bus_storage["p_nom"] == 0).sum()
            if zero_cap > 0:
                issues.append(f"Storage units with zero capacity: {zero_cap}")

            # Check efficiency
            invalid_eff = (
                (bus_storage["efficiency_store"] < 0)
                | (bus_storage["efficiency_store"] > 1)
                | (bus_storage["efficiency_dispatch"] < 0)
                | (bus_storage["efficiency_dispatch"] > 1)
            ).sum()
            if invalid_eff > 0:
                issues.append(f"Storage units with invalid efficiency: {invalid_eff}")

        # Check incoming/outgoing transmission links
        incoming_links = network.links[network.links["bus1"] == bus_name]
        outgoing_links = network.links[network.links["bus0"] == bus_name]

        # Check for stores connected to this bus
        bus_stores = network.stores[network.stores["bus"] == bus_name]

        # For transmission isolation check, exclude turbine/hydro links (which are hydro-specific)
        transmission_in = incoming_links[
            ~incoming_links.index.astype(str).str.contains(
                "turbine",
                case=False,
                na=False,
            )
        ]
        transmission_out = outgoing_links[
            ~outgoing_links.index.astype(str).str.contains(
                "turbine",
                case=False,
                na=False,
            )
        ]

        # Bus is isolated if:
        # - No transmission links AND
        # - No stores AND
        # - (No generators OR no loads)
        if (
            len(transmission_in) == 0
            and len(transmission_out) == 0
            and len(bus_stores) == 0
        ):
            if len(bus_gens) == 0 or len(bus_loads) == 0:
                issues.append("Isolated bus (no transmission links or connections)")

        # Determine status
        status = "✅ OK" if len(issues) == 0 else "❌ ISSUES"

        bus_info = {
            "Bus": bus_name,
            "Status": status,
            "Generators": len(bus_gens),
            "Loads": len(bus_loads),
            "StorageUnits": len(bus_storage),
            "Stores": len(bus_stores),
            "Issues": "; ".join(issues) if issues else "None",
        }

        if len(warnings_list) > 0:
            bus_info["Warnings"] = "; ".join(warnings_list)

        bus_issues.append(bus_info)

    # Create DataFrame
    bus_df = pd.DataFrame(bus_issues)

    # Print summary
    print("\n  Bus Summary Table:")
    print("  " + "-" * 120)

    # Print with better formatting
    pd.set_option("display.max_columns", None)
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", None)
    pd.set_option("display.max_colwidth", 40)

    # Print each row nicely
    for _, row in bus_df.iterrows():
        status_symbol = "✅" if "OK" in row["Status"] else "❌"
        print(f"\n  {status_symbol} {row['Bus']}")
        print(
            f"      Generators: {row['Generators']}, Loads: {row['Loads']}, "
            f"StorageUnits: {row['StorageUnits']}, Stores: {row['Stores']}",
        )
        if row["Issues"] != "None":
            print(f"      Issues: {row['Issues']}")
        if "Warnings" in row and pd.notna(row["Warnings"]):
            print(f"      Warnings: {row['Warnings']}")

    print("\n  " + "-" * 120)

    # Summary counts
    ok_buses = (bus_df["Status"] == "✅ OK").sum()
    issue_buses = (bus_df["Status"] == "❌ ISSUES").sum()

    print(f"\n  Summary: {ok_buses} buses OK, {issue_buses} buses with issues")

    return bus_df


def check_ramp_feasibility(network: pypsa.Network) -> bool:
    """Check for ramp-limit / p_min_pu conflicts that cause solver infeasibility.

    Detects generators where a forced shutdown (p_max_pu=0) at snapshot t is
    immediately followed by a minimum-generation requirement (p_min_pu>0) at
    t+1 that the ramp limit cannot satisfy in one step.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.

    Returns
    -------
    bool
        True if no conflicts found, False otherwise.

    """
    print_subsection("Ramp feasibility (p_min_pu vs ramp_limit_up)")

    gens = network.generators
    ramp_gens = gens[gens["ramp_limit_up"].notna()]
    if ramp_gens.empty:
        report(True, "No generators with ramp limits — skipping")
        return True

    weightings = network.snapshot_weightings["generators"]
    conflicts = []

    for gen_name, gen in ramp_gens.iterrows():
        ramp_up = gen["ramp_limit_up"]

        # p_max_pu timeseries (fall back to scalar)
        if gen_name in network.generators_t.p_max_pu.columns:
            p_max_pu = network.generators_t.p_max_pu[gen_name]
        else:
            scalar_max = gen.get("p_max_pu", 1.0)
            if scalar_max > 0:
                continue  # never forced off, no conflict possible
            p_max_pu = pd.Series(scalar_max, index=network.snapshots)

        # p_min_pu timeseries (fall back to scalar)
        if gen_name in network.generators_t.p_min_pu.columns:
            p_min_pu = network.generators_t.p_min_pu[gen_name]
        else:
            scalar_min = gen.get("p_min_pu", 0.0)
            if scalar_min == 0:
                continue
            p_min_pu = pd.Series(scalar_min, index=network.snapshots)

        if (p_min_pu == 0).all():
            continue

        # Find snapshots where the previous step was forced off and this step
        # requires minimum generation. The first snapshot has no prior step.
        prev_forced_off = (p_max_pu.shift(1) == 0).fillna(False)
        requires_min = p_min_pu > 0
        problem_mask = prev_forced_off & requires_min

        if not problem_mask.any():
            continue

        for snap in network.snapshots[problem_mask]:
            # Compare in per-unit of p_nom so p_nom cancels out.
            # ramp_limit_up * weighting must cover p_min_pu in one step.
            available_ramp_pu = ramp_up * weightings[snap]
            required_pu = p_min_pu[snap]
            if available_ramp_pu < required_pu:
                conflicts.append(
                    {
                        "generator": gen_name,
                        "snapshot": snap,
                        "p_min_pu": required_pu,
                        "available_ramp_pu": available_ramp_pu,
                    },
                )

    if conflicts:
        report(
            False,
            f"{len(conflicts)} ramp-feasibility conflict(s) — "
            "forced shutdown followed by unreachable p_min_pu",
        )
        for c in conflicts[:5]:
            print(
                f"     {c['generator']} @ snapshot {c['snapshot']}: "
                f"p_min_pu={c['p_min_pu']:.3f} pu but max ramp={c['available_ramp_pu']:.3f} pu/step",
            )
        if len(conflicts) > 5:
            print(f"     ... and {len(conflicts) - 5} more")
        return False

    report(
        True,
        f"No ramp-feasibility conflicts in {len(ramp_gens)} ramp-limited generators",
    )
    return True


# ============================================================================
# MAIN CHECK FUNCTIONS
# ============================================================================


def check_unoptimized_network(
    network: pypsa.Network,
    detailed: bool | None = False,
) -> bool:
    """Comprehensive check for unoptimized PyPSA network.

    Validates:
    - Network structure (buses, snapshots, components)
    - Generator parameters and constraints
    - Load definitions and time-series
    - Storage unit structure
    - Hydro store and turbine configuration
    - Transmission links
    - Numerical properties

    Parameters
    ----------
    network : pypsa.Network
        PyPSA network object to validate
    detailed : bool | None, optional
        If True, performs detailed per-bus analysis (default is False)

    Returns
    -------
    bool
        True if all checks passed, False otherwise

    """
    print_section("UNOPTIMIZED NETWORK VALIDATION")

    all_ok = True

    # Run all checks
    all_ok &= check_network_structure(network)
    all_ok &= check_generators_structure(network)
    all_ok &= check_ramp_feasibility(network)
    all_ok &= check_loads_structure(network)
    all_ok &= check_storage_units_structure(network)
    all_ok &= check_stores_hydro_structure(network)
    all_ok &= check_hydro_structure(network)
    all_ok &= check_links_structure(network)
    all_ok &= check_numerical_properties(network)

    # Summary
    print_section("SUMMARY")
    if all_ok:
        print("  ✅ Network structure is valid and ready for optimization")
    else:
        print("  ⚠️  Network has issues that may prevent successful optimization")
        print("     Review warnings and errors above")
        print()

    if detailed:  # Show per-bus analysis when issues found
        analyse_bus_issues(network)

    print()
    return all_ok


def check_optimized_network(network: pypsa.Network) -> bool:
    """Comprehensive check for optimized PyPSA network.

    Validates:
    - Optimization status and convergence
    - Generator dispatch results
    - Storage energy levels
    - Load satisfaction
    - Hydro energy conservation
    - Cost and objective function
    - Network structure consistency

    Parameters
    ----------
    network : pypsa.Network
        PyPSA network object with optimization results

    Returns
    -------
    bool
        True if all checks passed, False otherwise

    """
    print_section("OPTIMIZED NETWORK VALIDATION")

    all_ok = True

    # Check optimization success
    all_ok &= check_optimization_status(network)

    # Check results
    all_ok &= check_generator_results(network)
    all_ok &= check_storage_results(network)
    all_ok &= check_load_results(network)
    all_ok &= check_hydro_energy_balance(network)
    all_ok &= check_hydro_structure(network)
    all_ok &= analyse_hydro_generation(network)
    all_ok &= check_marginal_costs(network)

    # Check structure consistency
    all_ok &= check_network_structure(network)
    all_ok &= check_generators_structure(network)

    # Summary
    print_section("SUMMARY")
    if all_ok:
        print("  ✅ Optimization successful and results are valid")
    else:
        print("  ⚠️  Optimization has issues or results are incomplete")
        print("     Review warnings and errors above")
        print()
        # Show per-bus analysis when issues found
        analyse_bus_issues(network)

    print()
    return all_ok


def check_network(network: pypsa.Network, detailed: bool | None = False) -> None:
    """Run all checks on the given PyPSA network.

    Parameters
    ----------
    network : pypsa.Network
        The PyPSA network to check.
    detailed : bool | None, optional
        If True, performs detailed per-bus analysis for unoptimized networks.
        Default is False.

    """
    print_section("PYPSA NETWORK VALIDATION")

    # If optimization results are present, check them
    if hasattr(network, "status") and network.status:
        # first, use the inbuilt method for consistency check
        print("\nRunning built-in network consistency check...")
        network.consistency_check(check_dtypes=True, strict="all")
        print("\nChecking optimization results...")
        check_optimized_network(network)
    else:
        # Check unoptimized network
        print("\nChecking unoptimized network structure and parameters...")
        check_unoptimized_network(network, detailed=detailed)


if __name__ == "__main__":
    # Run network checks from command line

    parser = argparse.ArgumentParser(
        description="Check PyPSA Network Structure and Optimization Results",
    )
    parser.add_argument(
        "network_folder",
        type=str,
        help="Path to the PyPSA network folder",
    )
    parser.add_argument(
        "--detailed",
        action="store_true",
        help="Enable detailed per-bus analysis",
    )
    args = parser.parse_args()

    if not Path(args.network_folder).exists():
        print(f"Error: Network folder '{args.network_folder}' does not exist.")
        sys.exit(1)
    # Load a network
    pypsa_network = pypsa.Network(args.network_folder)

    # Check unoptimized network
    check_network(pypsa_network, detailed=args.detailed)
