"""Build a PyPSA network from a network directory and a scenario config."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import pandas as pd
import pypsa

from dna_pypsa.config import (
    COMPONENT_KEYS,
    TIMESERIES_PARAMS,
    ConfigError,
    get_component_overrides,
    get_network_params,
    get_zone_filter,
    resolve_timeseries,
)

logger = logging.getLogger(__name__)

#: PyPSA component class names keyed by our CSV filenames.
_PYPSA_COMPONENT: dict[str, str] = {
    "buses": "Bus",
    "generators": "Generator",
    "links": "Link",
    "loads": "Load",
    "stores": "Store",
}


def build_network(network_dir: str | Path, config: dict) -> pypsa.Network:
    """Build a PyPSA network from a CSV network directory and a scenario config.

    Loads a complete PyPSA network from ``network_dir`` using
    :meth:`pypsa.Network.import_from_csv_folder`, then applies any quantitative
    overrides defined in ``config``.

    Parameters
    ----------
    network_dir : str | Path
        Directory containing a full PyPSA CSV export (``buses.csv``,
        ``generators.csv``, ``links.csv``, ``loads.csv``, ``stores.csv``,
        ``snapshots.csv``, plus any timeseries files such as
        ``generators-p_max_pu.csv``).
    config : dict
        Loaded config dict as returned by :func:`dna_pypsa.config.load_config`.

    Returns
    -------
    pypsa.Network

    Raises
    ------
    FileNotFoundError
        If ``network_dir`` does not exist.
    ConfigError
        If a component named in the config does not exist in the network, or
        a timeseries value cannot be resolved.

    """
    network_dir = Path(network_dir).resolve()
    if not network_dir.exists():
        raise FileNotFoundError(f"Network directory not found: {network_dir}")

    network = pypsa.Network(name=config.get("_config_name", ""))
    network.import_from_csv_folder(network_dir)
    _clamp_extendable_capacities(network)

    zone_filter = get_zone_filter(config)
    if zone_filter:
        _filter_zones(network, zone_filter)

    _apply_network_params(network, config)
    _apply_component_params(network, config, network.snapshots)

    logger.info(
        "Network built: %d buses, %d generators, %d links, %d loads, %d stores",
        len(network.buses),
        len(network.generators),
        len(network.links),
        len(network.loads),
        len(network.stores),
    )
    return network


def _clamp_extendable_capacities(network: pypsa.Network) -> None:
    """Clamp p_nom / e_nom to p_nom_max / e_nom_max for extendable components.

    Parameters
    ----------
    network : pypsa.Network

    Notes
    -----
    When a network is exported after a solve, ``p_nom`` holds the optimised
    capacity.  If that value exceeds the ``p_nom_max`` policy cap (e.g. due to
    a cap change between runs), the investment LP is immediately infeasible.
    This function silently caps ``p_nom`` (and ``e_nom`` for stores) at the
    maximum, logging a warning for each affected component.

    """
    import numpy as np

    checks = [
        (network.generators, "p_nom_extendable", "p_nom", "p_nom_max"),
        (network.stores, "e_nom_extendable", "e_nom", "e_nom_max"),
        (network.links, "p_nom_extendable", "p_nom", "p_nom_max"),
    ]
    for df, ext_col, nom_col, max_col in checks:
        if (
            ext_col not in df.columns
            or nom_col not in df.columns
            or max_col not in df.columns
        ):
            continue
        ext = df[df[ext_col].astype(bool)]
        mask = np.isfinite(ext[max_col]) & (ext[nom_col] > ext[max_col])
        for name in ext.index[mask]:
            old = df.loc[name, nom_col]
            cap = df.loc[name, max_col]
            df.loc[name, nom_col] = cap
            logger.warning(
                "Clamped %s %r: %s %.1f → %.1f (exceeds %s)",
                df.attrs.get("name", nom_col.split("_")[0]),
                name,
                nom_col,
                old,
                cap,
                max_col,
            )


def _filter_zones(network: pypsa.Network, zones: list[str]) -> None:
    """Remove all buses (and their attached components) not in ``zones``.

    Parameters
    ----------
    network : pypsa.Network
    zones : list[str]
        Bus names to retain.

    """
    buses_to_remove = [b for b in network.buses.index if b not in zones]
    if buses_to_remove:
        logger.info("Zone filter: removing buses %s", buses_to_remove)
        network.remove("Bus", buses_to_remove)


def _apply_network_params(network: pypsa.Network, config: dict) -> None:
    """Apply global network parameters from config.

    Parameters
    ----------
    network : pypsa.Network
    config : dict

    """
    params = get_network_params(config)
    stop_price = params["stop_price"]

    slack_gens = [g for g in network.generators.index if "slack" in g]
    if slack_gens:
        network.generators.loc[slack_gens, "marginal_cost"] = stop_price
        logger.info(
            "stop_price %.2f EUR/MWh applied to %d slack generator(s): %s",
            stop_price,
            len(slack_gens),
            slack_gens,
        )
    else:
        logger.debug(
            "stop_price %.2f EUR/MWh set (no slack generators found)", stop_price
        )

    weightings = params["snapshot_weightings"]
    if weightings != 1.0:
        network.snapshot_weightings[:] = weightings

    logger.debug(
        "Network params: stop_price=%.2f, weightings=%.2f",
        stop_price,
        weightings,
    )


def _apply_component_params(
    network: pypsa.Network,
    config: dict,
    snapshots: pd.DatetimeIndex,
) -> None:
    """Apply per-component parameter overrides from config to the network.

    Parameters
    ----------
    network : pypsa.Network
    config : dict
    snapshots : pd.DatetimeIndex

    """
    config_dir = config["_config_dir"]
    ts_params = TIMESERIES_PARAMS

    for component_key in COMPONENT_KEYS:
        overrides = get_component_overrides(config, component_key)
        if not overrides:
            continue

        pypsa_component = _PYPSA_COMPONENT[component_key]
        component_df = getattr(network, component_key)
        timeseries_keys = ts_params.get(component_key, set())

        for name, params in overrides.items():
            if name not in component_df.index:
                raise ConfigError(
                    f"{pypsa_component} {name!r} is in config but not in the network"
                )
            _apply_single_component(
                network,
                pypsa_component,
                name,
                params,
                timeseries_keys,
                config_dir,
                snapshots,
            )


def _apply_single_component(
    network: pypsa.Network,
    component: str,
    name: str,
    params: dict,
    timeseries_keys: set[str],
    config_dir: Path,
    snapshots: pd.DatetimeIndex,
) -> None:
    """Apply a single component's parameter dict to the network.

    Parameters
    ----------
    network : pypsa.Network
    component : str
        PyPSA component class name (e.g. ``'Generator'``).
    name : str
        Component name.
    params : dict
        Parameter key-value pairs from config.
    timeseries_keys : set[str]
        Parameters that support timeseries values for this component type.
    config_dir : Path
    snapshots : pd.DatetimeIndex

    """
    scalar_params: dict[str, Any] = {}
    timeseries_params: dict[str, pd.Series] = {}

    for key, value in params.items():
        if key in timeseries_keys and not isinstance(value, (int, float)):
            resolved = resolve_timeseries(value, config_dir, snapshots)
            if isinstance(resolved, pd.Series):
                timeseries_params[key] = resolved
            else:
                scalar_params[key] = resolved
        elif key in timeseries_keys and isinstance(value, (int, float)):
            scalar_params[key] = float(value)
        else:
            scalar_params[key] = value

    if scalar_params:
        network.df(component).loc[name, list(scalar_params.keys())] = list(
            scalar_params.values()
        )

    for attr, series in timeseries_params.items():
        pnl = network.pnl(component)
        if attr not in pnl:
            pnl[attr] = pd.DataFrame(index=snapshots)
        pnl[attr][name] = series

    logger.debug("Applied overrides to %s %r: %s", component, name, list(params.keys()))
