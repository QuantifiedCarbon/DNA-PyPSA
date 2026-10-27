# Variability Adjustment

Variability adjustment is the inner loop of the `iterate` run mode. It approximates unit commitment by iteratively updating generator availability profiles (`p_max_pu`, `p_min_pu`) based on whether each generator is profitable in each time period.

---

## Motivation

A standard LP dispatch solve treats generator availability as fixed: if `p_max_pu = 1.0`, the generator can dispatch any amount from 0 to `p_nom` in every hour. This is appropriate for renewables but unrealistic for thermal plant, which would not be committed in hours where the market price is below their variable cost.

Modelling unit commitment properly requires a mixed-integer programme (MIP), which is typically 100–1000× slower than an LP for large networks. The variability adjustment loop avoids this by iterating between LP dispatch solves and availability updates until the profiles stabilise.

The method is adapted from the cgrid unit commitment heuristic, described in:

> Cox, D.M. et al. (2025). Strategic capacity expansion planning in hydro-dominated power systems: Insights from the Nordics. *Energy*. https://doi.org/10.1016/j.energy.2025.054143

---

## Which generators are adjusted

Only generators with `auto_variability = true` are adjusted. This flag is set in the topology CSV or scenario config and indicates that the generator's availability should be determined endogenously by the iterative loop rather than taken from a fixed profile.

Renewables (wind, solar) typically have `auto_variability = false` because their physical availability is dictated by the weather profile. Thermal plant (gas CCGT, coal, nuclear) with flexible commitment should have `auto_variability = true`.

---

## Algorithm

Each iteration of the outer loop:

1. **Solve the dispatch LP** to get hourly dispatch `p` and bus shadow prices (marginal prices).

2. **For each adjusted generator**, divide the year into `n_chunks` time segments (default 104, approximately weekly). For each chunk:

   a. Calculate the **capture price** — the generation-weighted average market price:

   ```
   capture_price = sum(p_t * price_t) / sum(p_t)
   ```

   If the generator dispatches nothing in the chunk, the simple average price is used.

   b. Calculate the **variable cost** — the generator's marginal cost at current output. For generators with a quadratic supply curve:

   ```
   variable_cost_t = marginal_cost + 2 × marginal_cost_quadratic × p_t
   ```

   For generators with only a flat marginal cost, `variable_cost = marginal_cost`.

   c. Compute the **gap**:

   ```
   gap = capture_price - mean(variable_cost)
   ```

   d. **Adjust availability** for the chunk:
   - `gap > 0` (capture price > cost): increase `p_max_pu` by `step_size`
   - `gap < 0` (capture price < cost): decrease `p_max_pu` by `step_size`
   - `|gap| < equilibrium_tolerance` (1 EUR/MWh): no change

3. **Smooth** the updated `p_max_pu` profile with a 24-hour centred moving average to avoid abrupt transitions between chunks.

4. **Clip** to the allowed range `[min_variability, max_variability]` (defaults 0.05–1.0).

5. **Update `p_min_pu`** proportionally: `p_min_pu = p_max_pu × min_fraction × multiplier`.

6. **Re-solve** the dispatch LP with the updated profiles.

---

## Step size and damping

### Fixed step mode (default)

Each chunk's availability moves by exactly `±step_size`. The default `step_size = 0.02` means a maximum shift of 2 percentage points per iteration. This is conservative and stable but can require many iterations to converge.

### Market-share damping

Large generators that dominate a zone's supply are price-setters: their dispatch directly determines the market price they receive. Naively increasing their availability raises supply and depresses prices, potentially over-correcting. To prevent oscillation, the effective step is scaled by a market-share damping factor:

```
market_share = p_nom / zone_total_capacity
damping = 1 / (1 + market_share × VAR_MARKET_SENSITIVITY)
```

A generator that is 50% of zone capacity receives roughly half the nominal step. A generator that is 10% of capacity receives close to the full step.

### Adaptive mode

When `adaptive = true` is passed (not the default in standard runs), the step size is computed proportionally to the price–cost gap rather than being fixed:

```
step = gap / reference_cost × max_step × gain × damping
```

Adaptive mode also uses **secant acceleration** when two or more history points are available: it extrapolates the trajectory of capture price vs variability to predict where the zero-crossing (equilibrium) is, and takes a larger step in that direction. This can halve the number of iterations required but adds numerical sensitivity — the secant step is guarded against excessively large multipliers.

---

## Warmup period

For the first `variability_warmup_iterations` iterations (default 6), the step size is capped at `variability_warmup_max_step` (default 0.05). This prevents large early swings from a cold start from destabilising the capacity adjustment that runs in parallel.

---

## Interaction with capacity adjustment

Variability adjustment and capacity adjustment run on alternate phases within each iteration of the outer loop. Variability adjusts the shape of dispatch (who runs and when) while capacity adjusts the scale (how much total capacity exists). The two are coupled: changing `p_nom` changes the market share and capture price of each generator, which in turn changes what variability the next iteration recommends.

This coupling is why both checks must pass before the loop can converge: the objective window and dispatch settlement checks together ensure neither the variability profiles nor the capacity levels are still drifting when the loop exits.
