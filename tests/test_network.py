"""Tests for dna_pypsa.network."""

from pathlib import Path

import pandas as pd
import pytest

from dna_pypsa.config import ConfigError
from dna_pypsa.network import build_network


class TestBuildNetwork:
    def test_builds_from_network_dir(
        self, network_dir: Path, base_config: dict
    ) -> None:
        network = build_network(network_dir, base_config)
        assert set(network.buses.index) == {"DK", "DE"}
        assert set(network.generators.index) == {"gas_DK", "coal_DE"}
        assert len(network.snapshots) == 24

    def test_applies_scalar_param(self, network_dir: Path, base_config: dict) -> None:
        network = build_network(network_dir, base_config)
        assert network.generators.loc["gas_DK", "p_nom"] == 500.0
        assert network.generators.loc["gas_DK", "marginal_cost"] == 60.0

    def test_applies_scalar_timeseries_as_static(
        self, network_dir: Path, base_config: dict
    ) -> None:
        base_config["generators"]["gas_DK"]["p_max_pu"] = 0.85
        network = build_network(network_dir, base_config)
        assert network.generators.loc["gas_DK", "p_max_pu"] == 0.85

    def test_applies_array_timeseries(
        self, network_dir: Path, base_config: dict
    ) -> None:
        values = [0.9] * 24
        base_config["generators"]["gas_DK"]["p_max_pu"] = values
        network = build_network(network_dir, base_config)
        assert "gas_DK" in network.generators_t.p_max_pu.columns
        assert list(network.generators_t.p_max_pu["gas_DK"]) == values

    def test_applies_file_timeseries(
        self, network_dir: Path, base_config: dict
    ) -> None:
        profile = pd.DataFrame({"val": [0.7] * 24})
        profile.to_csv(network_dir / "profile.csv")
        base_config["generators"]["gas_DK"]["p_max_pu"] = {
            "file": "profile.csv",
            "column": "val",
        }
        base_config["_config_dir"] = network_dir
        network = build_network(network_dir, base_config)
        assert "gas_DK" in network.generators_t.p_max_pu.columns

    def test_zone_filter_removes_buses(
        self, network_dir: Path, base_config: dict
    ) -> None:
        base_config["run"] = {"mode": "expand", "zones": ["DK"]}
        network = build_network(network_dir, base_config)
        assert "DE" not in network.buses.index

    def test_missing_network_dir_raises(
        self, base_config: dict, tmp_path: Path
    ) -> None:
        with pytest.raises(FileNotFoundError):
            build_network(tmp_path / "nonexistent", base_config)

    def test_config_component_not_in_network_raises(
        self, network_dir: Path, base_config: dict
    ) -> None:
        base_config["generators"]["phantom_gen"] = {"p_nom": 100.0}
        with pytest.raises(ConfigError, match="phantom_gen"):
            build_network(network_dir, base_config)
