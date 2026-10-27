# Run Modes

DNA-PyPSA has two run modes — `expand` and `iterate` — both of which begin with an investment pre-solve. The pre-solve itself has two variants: `full` and `island`.

---

## Investment pre-solve

Both modes start by solving a linear programme (LP) to determine how much capacity to build for each extendable generator (`p_nom_extendable = true`). This step is skipped entirely if no generators, stores, or links are extendable.

### What it does

1. **Zeros quadratic costs.** `marginal_cost_quadratic` is temporarily set to zero on all generators so the problem is a pure LP. Quadratic costs would make the investment problem a QP, which is slower and numerically less stable when starting from zero capacity.

2. **Suspends must-run floors.** `p_min_pu` is zeroed during investment so the optimiser can freely choose capacity without being forced to dispatch must-run plant.

3. **Shifts marginal costs.** For generators with a supply curve (`supply_curve_width`), the marginal cost is shifted to the midpoint of the curve so the investment LP uses a representative single-point cost.

4. **Solves the LP.** PyPSA's optimiser minimises the sum of annualised capital costs plus dispatch variable costs over all snapshots. The result is `p_nom_opt` for each extendable component.

5. **Locks capacities.** Sets `p_nom = p_nom_opt` and `p_nom_extendable = false` for every extendable generator. Also marks each with `is_extendable = true` so the capacity adjustment loop knows which generators to track.

6. **Restores quadratic costs.** Recalculates `marginal_cost_quadratic` from the `supply_curve_width` and the now-known `p_nom_opt`, then restores original costs and `p_min_pu` for the dispatch solve.

Solver output for the investment pre-solve is written to `<solver>_investment_log.txt` in the output directory.

### `full` vs `island` pre-solve

The `pre_solve` setting controls how cross-border transmission is treated during the investment LP.

#### Full

```json
"run": {"pre_solve": "full"}
```

All zones can freely import and export during the investment solve. Generators in one zone can supply demand in another via existing links, which affects how much capacity each zone chooses to build. This is the simpler of the two modes.

**Risk:** in a multi-zone model, cheap capacity in one zone can serve demand in all connected zones. This tends to concentrate investment in the cheapest-cost zone and may produce unrealistic results where peripheral zones build no local capacity and rely entirely on imports.

#### Island

```json
"run": {"pre_solve": "island"}
```

The default mode. Cross-border links between investment zones and non-investment zones are temporarily penalised to the stop price (3000 EUR/MWh) during the investment LP. This makes it economically unattractive to export across zone boundaries, so each zone must be self-sufficient in investment. Links between two investment zones are left untouched.

After the investment solve completes the original link costs are restored for the dispatch solve, which runs with full transmission.

**Why island?** In the DNA reference model, surrounding zones (Norway, Sweden, Germany, etc.) have fixed exogenous capacities — they are not investment candidates. Their generation was pre-determined by a separate European system optimisation and is held constant across all scenarios. Without island mode the investment LP can route capacity into these zones to serve their demand, artificially inflating buildable capacity in Denmark by treating it as a net exporter to constrained neighbours. Island mode prevents this by making cross-border export economically unattractive during the investment step, so each investment zone is sized for its own demand. Full transmission is restored for the dispatch solve.

---

## `expand`

```bash
dna-pypsa expand -n <network_dir> -c <config.json> -o <output_dir>
```

1. Investment pre-solve determines optimal capacities for all extendable components.
2. Capacities are locked (`p_nom_extendable = false`).
3. A single dispatch solve is run.
4. Results are exported.

Use `expand` when:
- You want a fast, deterministic result.
- The network has only renewable generators with fixed availability profiles (no thermal unit commitment dynamics).
- You are debugging or validating a network before committing to a longer iterate run.

---

## `iterate`

```bash
dna-pypsa iterate -n <network_dir> -c <config.json> -o <output_dir>
```

1. Investment pre-solve as above.
2. A two-stage iterative loop runs until convergence:
   - **Dispatch solve** — LP/QP with current availability profiles.
   - **Variability adjustment** — adjusts `p_max_pu` and `p_min_pu` for each generator with `auto_variability = true` based on capture price vs marginal cost.
   - **Capacity adjustment** — adjusts `p_nom` for each originally-extendable generator based on profitability.
   - Repeat.
3. Results are exported after convergence.

See [Variability Adjustment](algorithms/variability_adjustment.md) and [Capacity Adjustment](algorithms/capacity_adjustment.md) for detailed explanations of each step.

### Why iterate?

A single dispatch solve assumes generator availability is fixed. In reality, a thermal plant whose capture price falls below its marginal cost would not run — it would be committed less. `iterate` approximates this unit commitment behaviour without requiring a mixed-integer programme (which is orders of magnitude slower).

Similarly, after market prices have been established, the capacity that was determined by the investment LP may no longer be economically viable. The capacity adjustment loop nudges `p_nom` up or down until each generator is close to its long-run equilibrium (profit per MW ≈ 0).

### Convergence

The loop exits when any one of three criteria is satisfied (and the minimum iteration count has been reached):

| Check | Description |
|---|---|
| **Objective window** | The spread of objectives over the last `convergence_window` iterations is < `convergence_threshold` %. |
| **Dispatch settlement** | The 75th percentile of per-generator variability changes (`\|Δp_max_pu\|`) is < `variability_convergence_threshold`. |
| **Consecutive** | The change in objective between the last two iterations is < `convergence_threshold` %. |

When capacity adjustment is enabled, all three checks additionally require that recent capacity changes are small (< `convergence_threshold` % of the generator's current `p_nom`).

Default values:

| Parameter | Default | CLI flag |
|---|---|---|
| `convergence_threshold` | `0.10` % | `--convergence-threshold` |
| `convergence_window` | `3` iterations | `--convergence-window` |
| `variability_convergence_threshold` | `0.005` | (not exposed on CLI) |
| `min_iterations` | `10` | `--min-iterations` |

The maximum number of iterations is controlled by the internal schedule (default 100 iterations). If the loop exits because the budget is exhausted rather than because a convergence criterion fired, a warning is written to the log.
