# CLI Reference

DNA-PyPSA is driven entirely from the command line via the `dna-pypsa` entry point.

```bash
dna-pypsa <mode> -n <network_dir> -c <config.json> -o <output_dir> [options]
```

Two subcommands are available: `expand` and `iterate`. Both share a common set of arguments; `iterate` adds convergence controls.

---

## Common arguments

These apply to both `expand` and `iterate`.

| Argument | Required | Type | Default | Description |
| --- | --- | --- | --- | --- |
| `-n, --network` | yes | path | — | Directory containing a full PyPSA CSV network export (buses, generators, loads, links, stores, snapshots). |
| `-c, --config` | yes | path | — | Path to the scenario config JSON file. |
| `-o, --output` | yes | path | — | Directory to write results into. Created if it does not exist. |
| `--solver` | no | `highs` \| `gurobi` | from config | Solver to use. Overrides the `solver` key in the config. Defaults to `highs` if neither CLI nor config specifies one. |
| `--pre-solve` | no | `full` \| `island` | from config | Investment pre-solve mode. Overrides `run.pre_solve` in the config. See [Run Modes](run_modes.md) for an explanation of `island`. |
| `--investment-slack-cost` | no | float | `0.0` | If > 0, temporarily reduce slack generator marginal cost to this value during the investment pre-solve. Useful when the full stop-price makes the investment LP numerically stiff. |
| `--dry-run` | no | flag | off | Build the network and export it to CSVs in the output directory, then exit without solving. Useful for inspecting what parameters have been applied before committing to a solve. |
| `-v, --verbose` | no | flag | off | Enable DEBUG-level logging. |

---

## `expand` subcommand

```bash
dna-pypsa expand -n <network_dir> -c <config.json> -o <output_dir> [options]
```

Runs the investment pre-solve followed by a single dispatch solve. No iteration. See [Run Modes — Expand](run_modes.md#expand).

No additional arguments beyond the common set.

---

## `iterate` subcommand

```bash
dna-pypsa iterate -n <network_dir> -c <config.json> -o <output_dir> [options]
```

Runs the investment pre-solve followed by the two-stage iterative loop (variability adjustment + capacity feedback) until convergence. See [Run Modes — Iterate](run_modes.md#iterate).

Additional arguments:

| Argument | Type | Default | Description |
|---|---|---|---|
| `--convergence-threshold` | float | `0.10` | Objective spread threshold (%) for the rolling-window and consecutive convergence checks. Lower values require tighter convergence; `0.10` means the objective must be stable within 0.1% across the window before the loop exits. |
| `--convergence-window` | int | `3` | Number of recent iterations used by the rolling objective stability check. |
| `--min-iterations` | int | `10` | Minimum number of iterations to run before any convergence check can fire. Prevents early exit on the first plateau. |

---

## Output

All results are written to `<output_dir>/`:

```bash
<output_dir>/
├── <timestamp>_<scenario>.log        # Full run log
├── highs_investment_log.txt          # Solver log for investment pre-solve
├── highs_log.txt                     # Solver log for dispatch solve(s)
├── results/                          # PyPSA CSV export (post-solve)
│   ├── generators.csv
│   ├── loads-p.csv
│   └── ...
└── results.nc                        # PyPSA netCDF export (post-solve)
```

The log file is named `<YYYYMMDD_HHMMSS>_<config_stem>.log`, e.g. `20250520_143022_nuclear_scenario.log`.

---

## Examples

```bash
# Expand solve with HiGHS
dna-pypsa expand \
  -n inputs/full_scenarios/networks/nuclear \
  -c inputs/full_scenarios/scenarios/nuclear/nuclear_scenario.json \
  -o outputs/nuclear

# Iterate with Gurobi, verbose logging
dna-pypsa iterate \
  -n inputs/full_scenarios/networks/reference \
  -c inputs/full_scenarios/scenarios/reference/reference_scenario.json \
  -o outputs/reference \
  --solver gurobi \
  --verbose

# Dry run — inspect network before solving
dna-pypsa expand \
  -n inputs/simple/network \
  -c inputs/simple/simple_scenario.json \
  -o outputs/simple_dry \
  --dry-run

# Run all 7 full scenarios in sequence
python scripts/run_full_scenarios.py

# Run specific scenarios
python scripts/run_full_scenarios.py --scenario nuclear --scenario solar
```
