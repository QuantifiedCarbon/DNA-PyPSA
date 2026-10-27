# Scenario Config Reference

Each scenario is controlled by a JSON file passed to `dna-pypsa` via `-c`. The config separates run settings (solver, mode, convergence) from network parameters (costs, capacities, time-series).

---

## Top-level structure

```json
{
  "solver": "highs",
  "run": { ... },
  "network": { ... },
  "generators": { ... },
  "loads": { ... },
  "links": { ... },
  "stores": { ... }
}
```

All keys are optional. Any key not present falls back to its default.

---

## `solver`

```json
"solver": "highs"
```

Which LP/QP solver to use. Accepted values: `"highs"` (default), `"gurobi"`.

Can be overridden at the CLI with `--solver`.

---

## `run`

```json
"run": {
  "mode": "expand",
  "pre_solve": "island",
  "zones": ["DK1", "DK2"]
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `mode` | `"expand"` \| `"iterate"` | — | Run mode. Overridden by the CLI subcommand. |
| `pre_solve` | `"full"` \| `"island"` | `"island"` | Investment pre-solve mode. See [Run Modes](run_modes.md#island-pre-solve). |
| `zones` | list of strings | all zones | If set, only buses whose names are in this list are kept; all others (and their attached components) are removed before solving. |

---

## `network`

```json
"network": {
  "stop_price": 3000.0,
  "snapshot_weightings": 1.0,
  "external_stop_price": 600.0
}
```

| Key | Type | Default | Description |
|---|---|---|---|
| `stop_price` | float | `3000.0` | Price ceiling (EUR/MWh). Applied at build time as the `marginal_cost` of every slack generator in the network. Hours where no real generator can supply cheaply enough are served by the slack at this price. Must be > 0. |
| `snapshot_weightings` | float | `1.0` | Weight applied to every snapshot (hour). Set to `8760 / n_snapshots` when running a representative sub-year to scale investment costs correctly. |
| `external_stop_price` | float | `0.0` | If > 0, caps the marginal cost of slack generators in non-investment zones at this value at solve time. Useful in multi-zone models to prevent peripheral zones (which import rather than invest) from driving prices above a lower ceiling. A typical value is `600.0`. |

### `stop_price` vs `external_stop_price`

`stop_price` is applied once at build time: `build_network` sets the `marginal_cost` of every slack generator in the network to this value. It defines the scarcity price in zones where investment is occurring — high enough that scarcity always justifies new plant.

`external_stop_price` is applied at solve time by `run_expand` / `run_iterate` and only affects slack generators in zones that have no extendable capacity (peripheral importing zones). Setting it lower than `stop_price` separates the expansion price signal from the dispatch price signal: importing zones still have a price ceiling but it does not need to be as high as in investment zones as this can skew expansion decisions.

---

## Component overrides

The `"generators"`, `"loads"`, `"links"`, and `"stores"` keys each contain a dict mapping **component name → parameter dict**. Any PyPSA attribute accepted on that component type can be set here and will override the value in the topology CSV.

```json
"generators": {
  "nuclear_DK1": {
    "p_nom": 3000.0,
    "p_nom_extendable": false,
    "marginal_cost": 8.0,
    "p_max_pu": 0.85
  },
  "wind_offshore_DK1": {
    "p_nom_max": 5000.0,
    "p_nom_extendable": true,
    "capital_cost": 1200000.0,
    "marginal_cost": 0.0,
    "p_max_pu": {"file": "profiles/DK1.csv", "column": "wind_offshore_DK1_p_max_pu"}
  }
}
```

### Timeseries parameters

The following parameters accept a time-series value instead of a scalar:

| Component | Timeseries parameters |
|---|---|
| `generators` | `p_max_pu`, `p_min_pu` |
| `links` | `p_max_pu`, `p_min_pu` |
| `loads` | `p_set` |
| `stores` | `e_max_pu`, `e_min_pu`, `e_set`, `p_set` |

A timeseries value can be provided in three ways:

**Scalar** — broadcast to all snapshots:
```json
"p_max_pu": 0.85
```

**Inline array** — one value per snapshot (must match the network's snapshot count):
```json
"p_max_pu": [0.8, 0.9, 0.7, ...]
```

**File reference** — path to a CSV relative to the config file:
```json
"p_max_pu": {"file": "profiles/DK1.csv", "column": "wind_offshore_DK1_p_max_pu"}
```

The `"column"` key is optional when the CSV has only a single data column. The CSV must have a datetime index matching the network's snapshots.

### Commonly used generator parameters

| Parameter | Description |
|---|---|
| `p_nom` | Installed capacity (MW). Starting point for extendable generators. |
| `p_nom_extendable` | `true` to allow the investment pre-solve to optimise capacity. |
| `p_nom_max` | Upper bound on buildable capacity (MW). |
| `p_nom_min` | Lower bound (MW). Retired generators are floored here. |
| `capital_cost` | Annualised fixed cost (EUR/MW/yr). Used by the investment LP. |
| `marginal_cost` | Variable cost (EUR/MWh). Should include fuel, CO₂, and VOM. See below. |
| `p_max_pu` | Availability profile — fraction of `p_nom` that can dispatch each hour. |
| `p_min_pu` | Must-run floor — fraction of `p_nom` that must dispatch each hour. |
| `auto_variability` | `true` to enable iterative variability adjustment for this generator. |

### Marginal cost calculation

`marginal_cost` must be pre-calculated and set directly — DNA-PyPSA does not apply carbon pricing automatically. For a thermal generator:

```
marginal_cost = (fuel_cost + co2_price × co2_intensity) / efficiency + VOM
```

| Term | Unit | Description |
|---|---|---|
| `fuel_cost` | EUR/MWh_th | Fuel commodity price |
| `co2_price` | EUR/tCO₂ | EU ETS carbon price |
| `co2_intensity` | tCO₂/MWh_th | Gas ≈ 0.202, hard coal ≈ 0.341, lignite ≈ 0.401 |
| `efficiency` | MWh_e/MWh_th | Thermal efficiency of the plant |
| `VOM` | EUR/MWh_e | Variable operation and maintenance cost |

---

## Full example

```json
{
  "solver": "gurobi",
  "run": {
    "mode": "expand",
    "pre_solve": "island"
  },
  "network": {
    "stop_price": 3000.0
  },
  "generators": {
    "nuclear_DK1": {
      "p_nom": 3000.0,
      "p_nom_extendable": false,
      "marginal_cost": 8.0,
      "p_max_pu": 0.85
    },
    "wind_offshore_DK1": {
      "p_nom_max": 5000.0,
      "p_nom_extendable": true,
      "capital_cost": 1200000.0,
      "marginal_cost": 0.0,
      "p_max_pu": {"file": "profiles/DK1.csv", "column": "wind_offshore_DK1_p_max_pu"}
    },
    "gas_CCGT_DK1": {
      "p_nom": 800.0,
      "p_nom_extendable": true,
      "p_nom_max": 2000.0,
      "capital_cost": 600000.0,
      "marginal_cost": 124.0,
      "auto_variability": true
    }
  },
  "loads": {
    "demand_DK1": {
      "p_set": {"file": "demand/DK1.csv", "column": "demand_DK1"}
    }
  }
}
```
