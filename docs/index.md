# DNA-PyPSA Documentation

## Contents

- [Model Design](model_design.md) — how zones, generators, loads, and other components are formulated in PyPSA
- [CLI Reference](cli.md) — all command-line arguments for `dna-pypsa expand` and `dna-pypsa iterate`
- [Scenario Config Reference](scenario_config.md) — every key accepted in the scenario JSON, including timeseries formats and marginal cost calculation
- [Run Modes](run_modes.md) — how `expand` and `iterate` differ, what the investment pre-solve does, and the difference between `full` and `island` pre-solve
- [Variability Adjustment](algorithms/variability_adjustment.md) — the capture-price unit commitment algorithm used in the iterate loop
- [Capacity Adjustment](algorithms/capacity_adjustment.md) — the profitability-based capacity feedback loop

## Quick start

```bash
# Install
pip install -e ".[test]"

# Run a single scenario
dna-pypsa expand \
  -n inputs/simple/network \
  -c inputs/simple/simple_scenario.json \
  -o outputs/simple

# Run all 7 full scenarios
python scripts/run_full_scenarios.py
```

See the [CLI Reference](cli.md) for the full list of options.
