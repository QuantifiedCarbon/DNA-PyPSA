# DNA-PyPSA

Open-source PyPSA runner developed for the Danish Nuclear Alliance (DNA). Accepts a PyPSA network topology and a JSON scenario config, then solves the network in one of two modes.

## Overview

DNA-PyPSA separates network *topology* (which components exist) from network *parameters* (capacities, costs, availability profiles). The topology is defined in a set of CSV files; all quantitative parameters are defined in a JSON config file. This allows a single base topology to be used across many scenarios by swapping the config.

The tool is entirely CLI-driven and outputs results in both CSV and netCDF formats.

---

## Architecture

### Input: topology CSVs

A directory of CSV files defining the network structure — buses, generators, links, loads, stores, and snapshots. These files contain only structural columns (name, bus, carrier, location). No quantities.

| File | Columns |
| ------ | --------- |
| `buses.csv` | `name`, `carrier`, `x`, `y` |
| `generators.csv` | `name`, `bus`, `carrier` |
| `links.csv` | `name`, `bus0`, `bus1`, `carrier` |
| `loads.csv` | `name`, `bus`, `carrier` |
| `stores.csv` | `name`, `bus`, `carrier` |
| `snapshots.csv` | `name` (datetime index) |

### Input: scenario config (JSON)

A JSON file defining all quantitative parameters. Organised by component type, then component name. Any parameter that PyPSA accepts on a component can be set here.

Timeseries values (e.g. `p_max_pu`, `p_set`) accept three forms:

- **Scalar** — broadcast to all snapshots: `0.85`
- **Inline array** — one value per snapshot: `[0.8, 0.9, ...]`
- **File reference** — path to a CSV, relative to the config file:

  ```json
  {"file": "profiles/wind_offshore.csv", "column": "DK"}
  ```

  `column` is optional when the CSV has a single data column.

Example config:

```json
{
  "solver": "highs",
  "run": {
    "mode": "iterate",
    "zones": ["DK", "DE"]
  },
  "network": {
    "snapshot_weightings": 1.0
  },
  "generators": {
    "nuclear_DK": {
      "p_nom": 3000.0,
      "p_nom_extendable": false,
      "marginal_cost": 8.0,
      "p_max_pu": 0.85
    },
    "wind_offshore_DK": {
      "p_nom_max": 5000.0,
      "p_nom_extendable": true,
      "capital_cost": 1200000.0,
      "marginal_cost": 0.0,
      "p_max_pu": {"file": "profiles/wind_offshore.csv", "column": "DK"}
    }
  },
  "loads": {
    "demand_DK": {
      "p_set": {"file": "demand/DK_2050.csv"}
    }
  },
  "links": {
    "interconnect_DK_DE": {
      "p_nom": 2000.0,
      "efficiency": 0.98
    }
  },
  "stores": {
    "battery_DK": {
      "e_nom": 500.0,
      "standing_loss": 0.001
    }
  }
}
```

### Marginal cost

`marginal_cost` values in the scenario config must be pre-calculated and set directly — there is no automatic carbon pricing applied at build time. For thermal generators the full marginal cost is:

```text
marginal_cost = (fuel_cost + co2_price × co2_intensity) / efficiency + VOM
```

| Term | Unit | Description |
| --- | --- | --- |
| `fuel_cost` | €/MWh_th | Fuel commodity price |
| `co2_price` | €/tCO₂ | EU ETS carbon price |
| `co2_intensity` | tCO₂/MWh_th | Fuel CO₂ intensity (gas ≈ 0.202, hard coal ≈ 0.341, lignite ≈ 0.401) |
| `efficiency` | MWh_e/MWh_th | Generator thermal efficiency |
| `VOM` | €/MWh_e | Variable operation and maintenance cost |

For example, a gas CCGT at 55 % efficiency with fuel at 40 €/MWh_th, CO₂ at 132 €/tCO₂, and VOM of 3 €/MWh_e:

```text
marginal_cost = (40 + 132 × 0.202) / 0.55 + 3 = (40 + 26.66) / 0.55 + 3 ≈ 124 €/MWh_e
```

Renewables and nuclear carry no fuel cost or CO₂ term; their `marginal_cost` is typically zero or a small VOM.

### Output

Results are written to the specified output directory:

```bash
<output_dir>/
├── results/          # PyPSA CSV network export (post-solve)
└── results.nc        # PyPSA netCDF export (post-solve)
```

---

## Run modes

Two modes are available, both beginning with a capacity expansion pre-solve.

### `expand`

Capacity expansion pre-solve, followed by a single dispatch solve.

```bash
dna-pypsa expand -n <network_dir> -c <config.json> -o <output_dir>
```

1. Investment LP solves optimal capacity for all `p_nom_extendable` components.
2. Capacities are fixed and a full dispatch solve is run.
3. Results are written.

### `iterate`

Capacity expansion pre-solve, followed by iterative variability and capacity adjustment until convergence.

```bash
dna-pypsa iterate -n <network_dir> -c <config.json> -o <output_dir>
```

1. Investment LP solves optimal capacity.
2. Dispatch is solved; generator commitment and availability (`p_max_pu`) are adjusted iteratively to match market-clearing prices (variability adjustment).
3. Generator capacities are adjusted up or down based on profitability signals (capacity adjustment).
4. Steps 2–3 repeat until convergence criteria are met (objective window, dispatch settlement, consecutive objective tolerance).
5. Results are written.

---

## Installation

Requires Python ≥ 3.10.

### Install

```bash
pip install dna-pypsa
```

### From source

```bash
git clone <repo-url>
cd DNA-PyPSA
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install .
```

### Development install

Includes test dependencies (pytest, pre-commit, black):

```bash
pip install -e ".[test]"
pre-commit install
```

### Solvers

**HiGHS** is bundled as a dependency (`highspy`) and requires no additional setup. It is the default solver.

**Gurobi** requires a separate licence; refer to the [Gurobi documentation](https://www.gurobi.com/documentation/) for licence setup. The `gurobipy` bindings are already included in the main dependencies.

Pass `--solver gurobi` at the CLI or set `"solver": "gurobi"` in the scenario JSON to use it.

---

## Project structure

```bash
dna_pypsa/
├── cli.py              # CLI entry point (argparse subcommands)
├── config.py           # JSON config loader, validator, timeseries resolver
├── network.py          # Builds PyPSA network from topology + config
├── constants.py        # Solver parameters and convergence thresholds
├── logging_config.py   # Logging setup
└── run/
    ├── run.py                     # Core solve and two-stage orchestration
    ├── investment.py              # Capacity expansion LP pre-solve
    ├── variability_adjustment.py  # Iterative dispatch adjustment loop
    ├── capacity_adjustment.py     # Profitability-based capacity feedback loop
    └── convergence_utils.py       # Convergence checks
```

---

## Inputs

```bash
inputs/
├── simple/                    # Synthetic 2-bus DK/DE example (168 h, fully runnable)
│   ├── simple_scenario.json
│   ├── network/
│   ├── profiles/
│   └── demand/
├── reference/                 # DNA reference network (full year, 8760 h)
│   ├── network/               # Full PyPSA CSV export with parameters embedded
│   └── reference_scenario.json
└── full_scenarios/            # 7 DNA full scenarios
    ├── networks/              # One network folder per scenario
    │   ├── reference/
    │   ├── nuclear/
    │   ├── solar/
    │   ├── offshore/
    │   ├── nuclear_balanced/
    │   ├── solar_balanced/
    │   └── offshore_balanced/
    └── scenarios/             # One scenario folder per scenario
        ├── reference/
        │   ├── reference_scenario.json
        │   ├── profiles/      # Generator availability, one CSV per zone
        │   │   ├── DK1.csv
        │   │   ├── DK2.csv
        │   │   └── ...
        │   └── demand/        # Load time-series, one CSV per zone
        │       ├── DK1.csv
        │       ├── DK2.csv
        │       └── ...
        ├── nuclear/
        └── ...
```

### full_scenarios layout

Each of the 7 scenarios is split across two directories:

**`networks/<name>/`** — network topology and static time-series. Contains the full PyPSA CSV export (buses, generators, loads, links, stores, snapshots, carriers) plus time-series that are scenario-invariant and loaded directly by PyPSA (`generators-auto_variability.csv`, `stores-e_*.csv`, `links-p_*.csv`).

**`scenarios/<name>/`** — scenario-specific inputs:

- `<name>_scenario.json` — run parameters (solver, mode, pre-solve) and file references that wire generator `p_max_pu`/`p_min_pu` and load `p_set` time-series to the zone CSVs.
- `profiles/<zone>.csv` — generator availability columns for that zone, named `{generator}_{attr}` (e.g. `wind_offshore_DK1_p_max_pu`). Covers `p_max_pu` and `p_min_pu`.
- `demand/<zone>.csv` — load `p_set` columns for that zone, named by load name.

All time-series use hourly 2050 timestamps (`2050-01-01 00:00:00` through `2050-12-31 23:00:00`).

### Running full_scenarios

```bash
# All 7 scenarios
python scripts/run_full_scenarios.py

# Dry run — builds networks and exports CSVs, no solve
python scripts/run_full_scenarios.py --dry-run

# Specific scenarios
python scripts/run_full_scenarios.py --scenario nuclear --scenario nuclear_balanced

# Override the run mode (default: iterate)
python scripts/run_full_scenarios.py --mode expand

# Overwrite existing output folders
python scripts/run_full_scenarios.py --force
```

Results are written to `outputs/full_scenarios/<mode>/<scenario_name>/`, e.g. `outputs/full_scenarios/iterate/nuclear/`. If an output folder already exists the script will exit with an error unless `--force` is passed.
