"""JSON scenario config loader, validator, and timeseries resolver."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from dna_pypsa.constants import DEFAULT_STOP_PRICE, VALID_SOLVERS

logger = logging.getLogger(__name__)

#: Top-level keys that hold per-component parameter dicts.
COMPONENT_KEYS = ("generators", "links", "loads", "stores", "buses")

#: Keys whose values may be timeseries (scalar, array, or file reference).
TIMESERIES_PARAMS = {
    "generators": {"p_max_pu", "p_min_pu"},
    "links": {"p_max_pu", "p_min_pu"},
    "loads": {"p_set"},
    "stores": {"e_max_pu", "e_min_pu", "e_set", "p_set"},
    "buses": set(),
}


class ConfigError(ValueError):
    """Raised when the scenario config is invalid."""


def load_config(config_path: str | Path) -> dict:
    """Load and validate a scenario config JSON file.

    Parameters
    ----------
    config_path : str | Path
        Path to the JSON config file.

    Returns
    -------
    dict
        Validated config dict with ``_config_dir`` injected as a Path so
        that relative file references can be resolved later.

    Raises
    ------
    ConfigError
        If required keys are missing or values are out of range.

    """
    config_path = Path(config_path).resolve()
    if not config_path.exists():
        raise ConfigError(f"Config file not found: {config_path}")

    with config_path.open() as fh:
        config = json.load(fh)

    config["_config_dir"] = config_path.parent
    config["_config_name"] = config_path.stem
    _validate_config(config)
    logger.info("Loaded config from %s", config_path)
    return config


def _validate_config(config: dict) -> None:
    """Raise ConfigError if the config contains invalid values.

    Parameters
    ----------
    config : dict
        Config dict (with ``_config_dir`` present).

    """
    run = config.get("run", {})
    mode = run.get("mode")
    if mode is not None and mode not in ("expand", "iterate"):
        raise ConfigError(f"run.mode must be 'expand' or 'iterate', got {mode!r}")

    pre_solve = run.get("pre_solve")
    if pre_solve is not None and pre_solve not in ("full", "island"):
        raise ConfigError(
            f"run.pre_solve must be 'full' or 'island', got {pre_solve!r}"
        )

    solver = config.get("solver")
    if solver is not None and solver not in VALID_SOLVERS:
        raise ConfigError(f"solver must be one of {VALID_SOLVERS}, got {solver!r}")

    network = config.get("network", {})
    stop_price = network.get("stop_price")
    if stop_price is not None and stop_price <= 0:
        raise ConfigError(f"network.stop_price must be > 0, got {stop_price}")

    ext_stop = network.get("external_stop_price")
    if ext_stop is not None and ext_stop < 0:
        raise ConfigError(f"network.external_stop_price must be >= 0, got {ext_stop}")


def get_solver(config: dict) -> str:
    """Return the solver name from config, defaulting to 'highs'.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    str

    """
    return config.get("solver", "highs")


def get_run_mode(config: dict) -> str:
    """Return the run mode from config, defaulting to 'expand'.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    str

    """
    return config.get("run", {}).get("mode", "expand")


def get_network_params(config: dict) -> dict:
    """Return global network parameters from config.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    dict
        Keys: ``stop_price``, ``snapshot_weightings``.

    """
    network = config.get("network", {})
    return {
        "stop_price": float(network.get("stop_price", DEFAULT_STOP_PRICE)),
        "snapshot_weightings": float(network.get("snapshot_weightings", 1.0)),
    }


def get_external_stop_price(config: dict) -> float:
    """Return the external stop price from config, defaulting to 0.0.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    float
        Value of ``network.external_stop_price``, or 0.0 if unset.

    """
    return float(config.get("network", {}).get("external_stop_price", 0.0))


def get_pre_solve(config: dict) -> str:
    """Return the investment pre-solve mode from config, defaulting to 'island'.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    str

    """
    return config.get("run", {}).get("pre_solve", "island")


def get_zone_filter(config: dict) -> list[str] | None:
    """Return the zone filter list, or None if not set.

    Parameters
    ----------
    config : dict
        Loaded config dict.

    Returns
    -------
    list[str] | None

    """
    return config.get("run", {}).get("zones")


def resolve_timeseries(
    value: Any,
    config_dir: Path,
    snapshots: pd.DatetimeIndex,
) -> float | pd.Series:
    """Resolve a config value that may be a scalar, array, or file reference.

    Parameters
    ----------
    value : Any
        One of:
        - ``float | int`` — broadcast to all snapshots.
        - ``list`` — one value per snapshot (must match snapshot length).
        - ``dict`` with ``"file"`` key — path to a CSV file, optionally with
          a ``"column"`` key to select a specific column.
    config_dir : Path
        Directory of the config file; file paths are resolved relative to it.
    snapshots : pd.DatetimeIndex
        Network snapshot index; used for length validation and Series index.

    Returns
    -------
    float | pd.Series
        A scalar float or a Series indexed by snapshots.

    Raises
    ------
    ConfigError
        If the value type is unrecognised, the file is missing, the column
        is not found, or the array length does not match the snapshot count.

    """
    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, list):
        if len(value) != len(snapshots):
            raise ConfigError(
                f"Inline array length {len(value)} does not match "
                f"snapshot count {len(snapshots)}"
            )
        return pd.Series(value, index=snapshots, dtype=float)

    if isinstance(value, dict):
        return _resolve_file_ref(value, config_dir, snapshots)

    raise ConfigError(
        f"Timeseries value must be a number, list, or file-reference dict; "
        f"got {type(value).__name__}"
    )


def _resolve_file_ref(
    ref: dict,
    config_dir: Path,
    snapshots: pd.DatetimeIndex,
) -> pd.Series:
    """Load a timeseries from a CSV file reference dict.

    Parameters
    ----------
    ref : dict
        Must contain ``"file"``. Optionally contains ``"column"``.
    config_dir : Path
        Base directory for resolving relative file paths.
    snapshots : pd.DatetimeIndex
        Expected index; used for length validation.

    Returns
    -------
    pd.Series

    Raises
    ------
    ConfigError
        If the file is missing, the column is not found, or the row count
        does not match the snapshot count.

    """
    if "file" not in ref:
        raise ConfigError(f"File-reference dict missing 'file' key: {ref}")

    file_path = (config_dir / ref["file"]).resolve()
    if not file_path.exists():
        raise ConfigError(f"Timeseries file not found: {file_path}")

    df = pd.read_csv(file_path, index_col=0)

    column = ref.get("column")
    if column is not None:
        if column not in df.columns:
            raise ConfigError(
                f"Column {column!r} not found in {file_path}. "
                f"Available: {list(df.columns)}"
            )
        series = df[column]
    elif df.shape[1] == 1:
        series = df.iloc[:, 0]
    else:
        raise ConfigError(
            f"File {file_path} has {df.shape[1]} columns; specify 'column' "
            f"to select one. Available: {list(df.columns)}"
        )

    if len(series) != len(snapshots):
        raise ConfigError(
            f"File {file_path} has {len(series)} rows but network has "
            f"{len(snapshots)} snapshots"
        )

    return pd.Series(series.values, index=snapshots, dtype=float)


def get_component_overrides(config: dict, component: str) -> dict[str, dict]:
    """Return the per-component override dicts for a given component type.

    Parameters
    ----------
    config : dict
        Loaded config dict.
    component : str
        One of ``'generators'``, ``'links'``, ``'loads'``, ``'stores'``,
        ``'buses'``.

    Returns
    -------
    dict[str, dict]
        Mapping of component name → parameter dict.

    """
    return config.get(component, {})
