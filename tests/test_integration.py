"""End-to-end integration tests."""

import json
from pathlib import Path

import pandas as pd
import pytest

from dna_pypsa.config import load_config
from dna_pypsa.network import build_network
from dna_pypsa.run.run import run_iterate

SIMPLE = Path(__file__).parent.parent / "inputs" / "simple"


@pytest.mark.slow
def test_iterate_simple(tmp_path):
    """Run iterate end-to-end on the simple network with HiGHS and check outputs."""
    config_path = tmp_path / "scenario.json"
    config_path.write_text(
        json.dumps(
            {
                "solver": "highs",
                "run": {"mode": "iterate"},
                "network": {"stop_price": 3000.0},
            }
        )
    )

    config = load_config(config_path)
    network = build_network(str(SIMPLE / "network"), config)

    output_dir = tmp_path / "output"
    run_iterate(network, solver="highs", output_directory=output_dir)

    assert (output_dir / "results.nc").exists()
    assert (output_dir / "results" / "generators.csv").exists()
    assert (output_dir / "results" / "loads-p.csv").exists()


@pytest.mark.slow
def test_iterate_progressive_capacity_adjustment(tmp_path):
    """Iterate loop applies progressive capacity adjustment to loss-making gas.

    Network: single DK bus, 48 hours.  A fixed coal unit (MC=200 EUR/MWh)
    always runs at full capacity and sets the market price.  Gas (MC=80,
    extendable, auto_variability=True) is inframarginal: it earns positive
    net revenue above variable cost but cannot recover its capital cost over
    the 48-hour horizon.

    Loss severity = |profit_per_mw| / capital_cost ≈ 0.71 < CAP_FAST_RETIRE_SEVERITY
    (0.75), so fast retirement is bypassed and the progressive proportional-step
    path executes (lines 292–535 of capacity_adjustment.py).
    """
    net_dir = tmp_path / "network"
    net_dir.mkdir()

    n_hours = 48
    snapshots = pd.date_range("2050-01-01", periods=n_hours, freq="h")

    pd.DataFrame(index=snapshots).to_csv(net_dir / "snapshots.csv", index_label="name")
    pd.DataFrame(
        {"carrier": ["AC"], "x": [12.0], "y": [56.0]},
        index=["DK"],
    ).to_csv(net_dir / "buses.csv", index_label="name")
    pd.DataFrame(
        {
            # coal is fixed (not extendable) — always runs, sets price at 200 EUR/MWh
            # gas is extendable and inframarginal (MC=80 < market price=200)
            "bus": ["DK", "DK"],
            "carrier": ["coal", "gas"],
            "p_nom": [300.0, 600.0],
            "p_nom_extendable": [False, True],
            "p_nom_max": [300.0, 600.0],
            "capital_cost": [0.0, 20000.0],
            "marginal_cost": [200.0, 80.0],
            "p_max_pu": [1.0, 1.0],
            "auto_variability": [False, True],
        },
        index=["coal_DK", "gas_DK"],
    ).to_csv(net_dir / "generators.csv", index_label="name")
    pd.DataFrame(
        {"bus": ["DK"], "carrier": ["electricity"]},
        index=["demand_DK"],
    ).to_csv(net_dir / "loads.csv", index_label="name")
    pd.DataFrame(columns=["name", "bus0", "bus1", "carrier"]).to_csv(
        net_dir / "links.csv", index=False
    )
    pd.DataFrame(columns=["name", "bus", "carrier"]).to_csv(
        net_dir / "stores.csv", index=False
    )

    # Demand requires both units: coal (300 MW fixed) + gas (300 MW from investment).
    # Coal is marginal in all hours, so price = 200 EUR/MWh throughout.
    demand = pd.Series(600.0, index=snapshots, name="demand_DK")
    demand.to_frame().to_csv(net_dir / "loads-p_set.csv", index_label="snapshot")

    config_path = tmp_path / "scenario.json"
    config_path.write_text(
        json.dumps(
            {
                "solver": "highs",
                "network": {"stop_price": 3000.0},
            }
        )
    )

    config = load_config(config_path)
    network = build_network(str(net_dir), config)

    gas_p_nom_before = network.generators.at["gas_DK", "p_nom"]

    output_dir = tmp_path / "output"
    run_iterate(
        network,
        solver="highs",
        output_directory=output_dir,
        min_iterations=2,
        convergence_window=2,
        convergence_threshold=50.0,
        generator_filter=lambda g: bool(g.get("auto_variability", False)),
    )

    assert (output_dir / "results.nc").exists()

    result_gens = pd.read_csv(output_dir / "results" / "generators.csv", index_col=0)
    gas_p_nom_after = result_gens.at["gas_DK", "p_nom"]

    assert (
        gas_p_nom_after < gas_p_nom_before
    ), f"Expected gas p_nom to fall below {gas_p_nom_before}, got {gas_p_nom_after}"
