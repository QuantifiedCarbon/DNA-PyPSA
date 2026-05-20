# Capacity Adjustment

Capacity adjustment is the outer loop of the `iterate` run mode. After variability adjustment has settled the dispatch patterns, the capacity loop checks whether the installed capacity of each originally-extendable generator is economically justified and nudges `p_nom` up or down accordingly.

---

## Motivation

The investment pre-solve finds the cheapest capacity mix under the assumption that generators dispatch optimally every hour. In practice, thermal generators do not always run — they are committed based on capture price vs marginal cost. Once variability adjustment has established realistic commitment patterns, the capacity that was optimal under the LP may no longer be economically sound. Capacity adjustment feeds back the profitability signal from the dispatch solve into a revised `p_nom`.

The loop does not re-run the investment LP. Instead, it makes proportional adjustments to `p_nom` iteration by iteration, allowing the dispatch solve and variability adjustment to respond before the next step is taken.

---

## Which generators are adjusted

Only generators marked `is_extendable = true` are adjusted. This flag is set by the investment pre-solve on every generator that originally had `p_nom_extendable = true`. Generators that started fixed (e.g. existing plant represented with a fixed `p_nom`) are not touched.

A generator is also skipped if:
- `p_nom < 1.0 MW` — too small to be meaningful.
- No dispatch data is available — the generator never dispatched in the previous solve.
- The bus has no marginal price data.

If any generators named `slack_*` are actively dispatching (> 0.01 MW), the system is infeasible without them. In this case fast retirement is suspended to avoid acting on unreliable shadow prices.

---

## Profit calculation

For each adjusted generator:

```
annual_revenue      = sum(p_t × bus_price_t)
annual_variable_cost = sum(marginal_cost × p_t + marginal_cost_quadratic × p_t²)
fixed_cost_per_mw   = capital_cost + fom_cost

profit_per_mw = (annual_revenue - annual_variable_cost) / p_nom - fixed_cost_per_mw
```

A positive `profit_per_mw` means the generator earns above its total cost and should expand. A negative value means it is loss-making and should contract.

---

## Fast retirement

Before applying a proportional step, the code checks whether the loss is severe enough to justify immediately retiring the generator to its minimum size.

Fast retirement fires when:
- `profit_per_mw < 0` (loss-making), AND
- either `annual_revenue < annual_variable_cost` (doesn't cover variable costs at all), OR
- `|profit_per_mw| / fixed_cost_per_mw ≥ 0.75` (loss exceeds 75% of fixed cost).

When fast retirement fires, `p_nom` is set to `max(p_nom_min, 1.0 MW)` immediately rather than taking a small proportional step. This avoids many iterations of gradual contraction for generators that are clearly not viable.

---

## Progressive proportional step

When fast retirement does not apply, `p_nom` is adjusted by a proportional step in the direction of the profit signal.

### Step size calculation

The effective step is calculated as:

```
base_fraction   = max_step_fraction              # default 5%
severity        = |profit_per_mw| / fixed_cost_per_mw (capped at 3.0)
threshold       = 0.5 for retirement, 0.2 for expansion

if severity > threshold:
    effective_fraction = base_fraction × (1 + severity)

max_step = max(effective_fraction × p_nom, min_step_mw)
```

The `min_step_mw` floor (default 200 MW) ensures that large generators with small percentage signals still move by a meaningful amount each iteration.

**Small-generator boost:** Generators below 1,000 MW that are profitable receive a boosted step fraction of 30% to accelerate bootstrapping from small starting capacities. This boost is suppressed for generators that have previously been larger (i.e. are contracting back toward equilibrium rather than growing for the first time).

**Retirement damping:** For retirement steps, `min_step_mw` is capped at the proportional step to ensure oscillation damping (see below) actually reduces the step — otherwise the 200 MW floor would dominate and damping would have no effect below ~4 GW.

### Market-share damping

Large generators are price-setters. Reducing their capacity raises market prices, which improves their own profitability, potentially causing overshoot. The step is scaled by the same market-share damping used in variability adjustment:

```
market_share = p_nom / zone_total_capacity
base_damping = 1 / (1 + market_share × CAP_MARKET_SENSITIVITY)
```

For deeply loss-making generators (profit < −50,000 EUR/MW/yr), damping is relaxed toward 1.0 to allow faster retirement.

### Tolerance band

Generators within 5% of break-even (`0 ≤ profit_per_mw < tolerance`) are considered at equilibrium and receive no adjustment. This prevents endless tiny nudges.

---

## Oscillation damping

When a generator alternates direction (expand → contract → expand or vice versa) its capacity is oscillating around an equilibrium. Each direction flip is counted across the full history. Once three or more flips are observed, the step fraction is multiplied by `CAP_OSCILLATION_DAMPING ** depth` (default 0.5 per flip). This monotonically drives the effective step size toward zero, allowing the generator to converge rather than oscillating indefinitely.

If the damped step falls below 5 MW, the generator is considered converged and no further adjustment is made.

---

## Insignificant generators

Very small generators (p_nom < 150 MW) that are loss-making are parked immediately at `max(p_nom_min, 1.0 MW)` rather than grinding through many proportional steps. Below this threshold the generator contributes negligibly to the system and oscillation on the fractional step wastes iterations.

---

## Warmup period

For the first `capacity_warmup_iterations` iterations (default 6), the capacity step is capped at `capacity_warmup_max_step` (default 2% per iteration). This prevents large early swings while variability profiles are still settling from the cold start. Capacity feedback also does not start until iteration 2 (one full variability + solve cycle has completed first).

---

## History and secant acceleration

The profit and `p_nom` at each iteration are stored in `cap_history`. When at least two prior data points are available for a generator, the code uses secant extrapolation to predict where `profit_per_mw = 0` (the equilibrium capacity) and takes a step in that direction.

Secant acceleration is guarded against perverse slopes (e.g. when the profit curve is flat or the extrapolation overshoots wildly). The maximum secant multiplier is 5× for retirement and 4× for expansion.
