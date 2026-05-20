"""Shared fixtures for dna_pypsa tests."""

import json
from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture
def network_dir(tmp_path: Path) -> Path:
    """Write a minimal 2-bus PyPSA network to a temporary directory.

    Network: two AC buses (DK, DE) connected by a link, one generator and
    one load per bus, one storage unit on DK.  All parameter columns are
    omitted so that tests can supply them as config overrides.
    """
    snapshots = pd.date_range("2026-01-01", periods=24, freq="h")
    pd.DataFrame(index=snapshots).to_csv(tmp_path / "snapshots.csv", index_label="name")

    pd.DataFrame(
        {"carrier": ["AC", "AC"], "x": [10.0, 13.0], "y": [56.0, 52.0]},
        index=["DK", "DE"],
    ).to_csv(tmp_path / "buses.csv", index_label="name")

    pd.DataFrame(
        {"bus": ["DK", "DE"], "carrier": ["gas", "coal"]},
        index=["gas_DK", "coal_DE"],
    ).to_csv(tmp_path / "generators.csv", index_label="name")

    pd.DataFrame(
        {"bus": ["DK", "DE"], "carrier": ["electricity", "electricity"]},
        index=["demand_DK", "demand_DE"],
    ).to_csv(tmp_path / "loads.csv", index_label="name")

    pd.DataFrame(
        {"bus0": ["DK"], "bus1": ["DE"], "carrier": ["AC"]},
        index=["link_DK_DE"],
    ).to_csv(tmp_path / "links.csv", index_label="name")

    pd.DataFrame(
        {"bus": ["DK"], "carrier": ["battery"]},
        index=["battery_DK"],
    ).to_csv(tmp_path / "stores.csv", index_label="name")

    return tmp_path


@pytest.fixture
def base_config(tmp_path: Path) -> dict:
    """Return a minimal valid config dict (without writing to disk)."""
    return {
        "solver": "highs",
        "run": {"mode": "expand"},
        "network": {"stop_price": 3000.0},
        "generators": {
            "gas_DK": {
                "p_nom": 500.0,
                "p_nom_extendable": False,
                "marginal_cost": 60.0,
            },
            "coal_DE": {
                "p_nom": 800.0,
                "p_nom_extendable": False,
                "marginal_cost": 45.0,
            },
        },
        "loads": {
            "demand_DK": {"p_set": 300.0},
            "demand_DE": {"p_set": 400.0},
        },
        "links": {
            "link_DK_DE": {"p_nom": 500.0, "efficiency": 1.0},
        },
        "stores": {
            "battery_DK": {
                "e_nom": 100.0,
                "e_min_pu": 0.0,
                "e_max_pu": 1.0,
                "standing_loss": 0.0,
            }
        },
        "_config_dir": tmp_path,
    }


@pytest.fixture
def config_file(tmp_path: Path, base_config: dict) -> Path:
    """Write the base config (without internal keys) to a JSON file."""
    config = {k: v for k, v in base_config.items() if not k.startswith("_")}
    config_path = tmp_path / "scenario.json"
    config_path.write_text(json.dumps(config))
    return config_path
