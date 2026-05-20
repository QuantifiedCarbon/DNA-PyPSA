"""Tests for dna_pypsa.config."""

import json
from pathlib import Path

import pandas as pd
import pytest

from dna_pypsa.config import (
    ConfigError,
    get_network_params,
    get_run_mode,
    get_solver,
    get_zone_filter,
    load_config,
    resolve_timeseries,
)


class TestLoadConfig:
    def test_loads_valid_file(self, config_file: Path) -> None:
        config = load_config(config_file)
        assert config["solver"] == "highs"
        assert "_config_dir" in config

    def test_injects_config_dir(self, config_file: Path) -> None:
        config = load_config(config_file)
        assert config["_config_dir"] == config_file.parent

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            load_config(tmp_path / "nonexistent.json")

    def test_invalid_mode_raises(self, tmp_path: Path) -> None:
        bad = {"run": {"mode": "unknown"}}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ConfigError, match="mode"):
            load_config(path)

    def test_invalid_solver_raises(self, tmp_path: Path) -> None:
        bad = {"solver": "cplex"}
        path = tmp_path / "bad.json"
        path.write_text(json.dumps(bad))
        with pytest.raises(ConfigError, match="solver"):
            load_config(path)


class TestGetters:
    def test_get_solver_default(self) -> None:
        assert get_solver({}) == "highs"

    def test_get_solver_from_config(self) -> None:
        assert get_solver({"solver": "gurobi"}) == "gurobi"

    def test_get_run_mode_default(self) -> None:
        assert get_run_mode({}) == "expand"

    def test_get_run_mode_from_config(self) -> None:
        assert get_run_mode({"run": {"mode": "iterate"}}) == "iterate"

    def test_get_network_params_defaults(self) -> None:
        params = get_network_params({})
        assert params["snapshot_weightings"] == 1.0

    def test_get_zone_filter_none(self) -> None:
        assert get_zone_filter({}) is None

    def test_get_zone_filter_list(self) -> None:
        assert get_zone_filter({"run": {"zones": ["DK", "DE"]}}) == ["DK", "DE"]


class TestResolveTimeseries:
    def _snapshots(self, n: int = 4) -> pd.DatetimeIndex:
        return pd.date_range("2026-01-01", periods=n, freq="h")

    def test_scalar_returns_float(self, tmp_path: Path) -> None:
        result = resolve_timeseries(0.85, tmp_path, self._snapshots())
        assert result == 0.85

    def test_inline_array_returns_series(self, tmp_path: Path) -> None:
        values = [0.8, 0.9, 0.7, 1.0]
        snaps = self._snapshots(4)
        result = resolve_timeseries(values, tmp_path, snaps)
        assert list(result.values) == values

    def test_inline_array_wrong_length_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="length"):
            resolve_timeseries([0.5, 0.6], tmp_path, self._snapshots(4))

    def test_file_reference_single_column(self, tmp_path: Path) -> None:
        snaps = self._snapshots(4)
        csv_path = tmp_path / "profile.csv"
        pd.DataFrame({"val": [0.1, 0.2, 0.3, 0.4]}).to_csv(csv_path)
        result = resolve_timeseries({"file": "profile.csv"}, tmp_path, snaps)
        assert list(result.values) == [0.1, 0.2, 0.3, 0.4]

    def test_file_reference_named_column(self, tmp_path: Path) -> None:
        snaps = self._snapshots(4)
        csv_path = tmp_path / "profile.csv"
        pd.DataFrame({"DK": [0.1, 0.2, 0.3, 0.4], "DE": [0.5, 0.6, 0.7, 0.8]}).to_csv(
            csv_path
        )
        result = resolve_timeseries(
            {"file": "profile.csv", "column": "DE"}, tmp_path, snaps
        )
        assert list(result.values) == [0.5, 0.6, 0.7, 0.8]

    def test_file_reference_ambiguous_column_raises(self, tmp_path: Path) -> None:
        snaps = self._snapshots(4)
        csv_path = tmp_path / "profile.csv"
        pd.DataFrame({"DK": [0.1] * 4, "DE": [0.2] * 4}).to_csv(csv_path)
        with pytest.raises(ConfigError, match="column"):
            resolve_timeseries({"file": "profile.csv"}, tmp_path, snaps)

    def test_file_not_found_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="not found"):
            resolve_timeseries({"file": "missing.csv"}, tmp_path, self._snapshots())

    def test_unrecognised_type_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigError, match="number, list"):
            resolve_timeseries("bad_value", tmp_path, self._snapshots())
