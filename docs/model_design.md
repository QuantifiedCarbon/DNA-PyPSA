# Model Design

DNA-PyPSA represents the power system as a network of interconnected nodes in PyPSA. Each node corresponds to a bidding zone, and components attached to nodes — generators, loads, stores, links — are formulated as linear or quadratic programmes solved jointly by the optimiser.

The reference model covers 17 bidding zones across northern and western Europe:

| Zone | Country |
| --- | --- |
| DK1 | Denmark West |
| DK2 | Denmark East |
| SE1 | Sweden North |
| SE2 | Sweden North-Central |
| SE3 | Sweden South-Central |
| SE4 | Sweden South |
| NO125 | Norway South/West |
| NO3 | Norway Central |
| NO4 | Norway North |
| FI | Finland |
| DE | Germany |
| PL | Poland |
| BT | Baltics |
| NL | Netherlands |
| GB | Great Britain |
| FR | France |
| ES | Spain |

DK1 and DK2 are the **investment zones** — the only zones where generator capacities are optimised. Capacities in all surrounding zones are **fixed exogenous inputs**, pre-determined by a separate optimisation of the European power system using the Balanced scenario as a reference point. Jointly optimising all countries would risk overestimating coordination gains between national energy policies, which in practice are made independently. The surrounding zones are therefore held fixed and provide realistic import/export conditions and border price signals without being co-optimised.

---

## Zones

PyPSA models can be built at multiple levels of spatial granularity, from individual power plants up to entire bidding zones represented by a single node. Finer granularity increases the realism of locational prices and congestion but also the size of the optimisation problem and the data requirements.

For long-term capacity expansion studies — where precise generator locations in 2050 are unknowable — zonal granularity is appropriate. DK1 and DK2 are each represented as a single bus. All connected zones (neighbouring countries) are also represented as buses and linked to DK1/DK2 via the interconnector Links described below.

Only extendable generators associated with DK1 or DK2 buses are optimised. Generators in surrounding zones carry fixed `p_nom` values and `p_nom_extendable = false`. The zone filter (`run.zones` in config) can be used to reduce the model to a subset of buses for faster test runs or sensitivity studies.

---

## Generator Types

### Dispatchable Generators

The simplest generator produces power at a fixed marginal cost and is paid the market clearing price for its output. If the market price falls below its marginal cost, the optimiser will not dispatch it.

Dispatchable generators include thermal plant (gas CCGT, coal, oil, nuclear) and any other controllable source whose output is not weather-constrained.

**Flat marginal cost** — a single `marginal_cost` value (EUR/MWh) applies uniformly across all output levels:

```
cost_t = marginal_cost × p_t
```

**Quadratic marginal cost** — the marginal cost increases linearly with output, representing the merit-order spread across a fleet of units of varying efficiency. This is controlled by `marginal_cost_quadratic` (EUR/MWh²):

```
cost_t = marginal_cost × p_t + marginal_cost_quadratic × p_t²
```

The quadratic term gives a realistic fleet-level supply curve: the cheapest units in the fleet dispatch first, with incrementally more expensive units committed as demand rises. This approach mirrors the treatment of dispatchable generation in cGrid (Cox et al., 2025).

Because `marginal_cost_quadratic` depends on installed capacity (`p_nom`), it cannot be set before the investment pre-solve. The pre-solve therefore uses a flat cost at the midpoint of the supply curve, and the quadratic coefficient is calculated from `supply_curve_width` and the resulting `p_nom_opt` before the dispatch solve.

Dispatchable generators with `auto_variability = true` have their `p_max_pu` availability profile adjusted iteratively to approximate unit commitment — see [Variability Adjustment](algorithms/variability_adjustment.md).

### Variable Renewable Generators

Wind and solar generation is determined by the weather. In PyPSA this is reproduced by pinning both `p_max_pu` and `p_min_pu` to the same hourly availability profile, so the generator is **forced to dispatch at exactly its available output** in every hour.

Setting `p_min_pu = p_max_pu` is the key constraint. Without it, the optimiser would be free to curtail a wind farm in hours of low or negative prices by simply not dispatching it — which does not reflect realistic grid behaviour where wind farms are typically dispatched to their available output and curtailed only through explicit market mechanisms. Pinning the floor equal to the ceiling removes that degree of freedom.

Renewables therefore have `auto_variability = false`. Their profiles are fixed inputs derived from the weather year (see below) and are never modified by the iterative loop. Where explicit curtailment is required it is handled via the curtailment sink Links rather than by relaxing the VRE floor.

### Hydro Generators

Hydro generation is not part of the Danish system but constitutes a large fraction of the wider Nordic grid and has a significant stabilising effect on regional prices. It is therefore important to model accurately.

Hydro is formulated as free dispatch — the generator can produce at any level from zero to its installed capacity, responding to market prices in the same way as a dispatchable thermal plant. In addition, reservoir minimum and maximum levels are strictly enforced over the year via a Store component that tracks water inventory.

Enforcing reservoir bounds mitigates the perfect-foresight problem inherent to annual optimisation: without them, PyPSA would have complete knowledge of when reservoirs would be replenished over the year and would optimally time all discharge accordingly, which overstates real operational flexibility.

---

## Weather Year and Demand Profiles

All time-varying inputs — wind availability, solar availability, hydro inflow, and electricity demand — are derived from a single weather year. The model runs 8,760 hourly snapshots representing one full year.

### Weather year selection

The underlying climate dataset spans 33 historical weather years (1983–2015), generated using the Weather2Energy tool, which converts reanalysis climate data into consistent hourly profiles for wind capacity factors, solar irradiance, and temperature-dependent demand. The full 33-year range is used in the broader study for reliability analysis and price variability assessment across extreme meteorological conditions — this is a cGrid capability and is not run through DNA-PyPSA.

**DNA-PyPSA uses year 1991 as the single representative weather year for all capacity expansion solves.** It was selected because its weather conditions produce price volatility and renewable capture rates close to the long-run multi-annual mean, making it a neutral and representative basis for capacity expansion. The snapshots in the network are timestamped as the year 2050 but the underlying hourly patterns come from 1991.

### VRE profiles

Wind and solar `p_max_pu` (and `p_min_pu`, which equals `p_max_pu` for VRE) profiles are pre-computed for each zone from the 1991 weather year. These are stored in the network's `generators-p_max_pu.csv` and `generators-p_min_pu.csv` files. A single column covers one generator; all VRE generators in the zone are derived from the same underlying reanalysis dataset.

### Demand profiles

Electricity demand (`p_set`) for each zone is also derived from the 1991 weather year via Weather2Energy. Temperature-dependent loads — space heating, hot water — follow the actual 1991 temperature profile, which means demand peaks and troughs are correlated with the renewable generation profile in a physically consistent way. Demand is specified as a `p_set` timeseries on each `Load` component in `loads-p_set.csv`.

---

## Load Types

### Fixed Loads (Inelastic Demand)

The standard PyPSA `Load` component requires its demand to be satisfied in full at every snapshot. It pays the market clearing price but has no flexibility to reduce consumption. This formulation is appropriate for residential and commercial electricity demand, which is largely price-insensitive in the short run.

Fixed loads are specified via a `p_set` timeseries (MW). The optimiser must satisfy this demand in every hour — failure to do so would require dispatch from a slack generator at the stop price.

### Linear Loads (Price-Sensitive Demand)

Standard PyPSA Loads have no ability to reduce demand in response to high prices. Many real-world consumers — industrial loads, heat pumps, electrolysers — will curtail or shift consumption when prices are sufficiently high. This price sensitivity is modelled using a **negative generator** pattern.

A negative generator has a negative `marginal_cost` (representing the value the consumer places on the electricity it consumes) and `p_max_pu = 1`. The optimiser will dispatch it (i.e. allow consumption) as long as the market price is below the consumer's willingness to pay. Above that threshold, the negative generator stops dispatching and demand effectively drops.

From the model's perspective this appears as a load that reduces its size based on price — a price-sensitive or curtailable load.

### Linear Loads with an Inflexible Component

A realistic price-sensitive consumer often has a base load that must always be met alongside a flexible portion that can respond to prices. This is represented by combining a standard fixed `Load` (covering the inelastic fraction) with a negative generator (covering the flexible fraction). The fixed component is always satisfied; the flexible component only dispatches when prices are acceptable.

### Flexible Loads (Temporally Shiftable Demand)

Some loads — residential heating, EV charging — cannot reduce their total energy consumption over time but can shift it between hours. A heat pump must deliver a certain amount of thermal energy over the week regardless of electricity prices, but the precise hour of delivery is flexible.

This is modelled by attaching a `Store` component to the load. The store is dimensioned to reflect the flexibility window (e.g. hours of thermal storage or battery buffer). The model can charge the store in cheap hours and draw it down in expensive hours, making the overall load appear temporally flexible while preserving the energy balance constraint.

---

## Other Components

### Store (Battery / Pumped Hydro)

`Store` components model energy reservoirs: batteries, pumped-hydro reservoirs, thermal storage. The optimiser controls charge and discharge in each hour subject to round-trip efficiency, state-of-charge bounds (`e_min_pu`, `e_max_pu`), and power limits (`p_nom`).

Stores can be extendable (`e_nom_extendable = true`), in which case the investment pre-solve optimises the energy capacity.

### Link (Transmission Interconnector)

`Link` components model directional power flows between buses — both cross-border interconnectors and internal transmission constraints within Denmark. Each Link has a capacity (`p_nom`) and an efficiency. Cross-border Links connect DK1 and DK2 to neighbouring country buses.

During the island pre-solve, Links between investment zones and non-investment zones are temporarily penalised to prevent the investment LP from routing capacity through cheap exporting zones. See [Run Modes — Island Pre-Solve](run_modes.md#island) for details.

### Slack Generators

Every bus has a slack generator that can supply any quantity of unmet demand at the stop price (default 3000 EUR/MWh). The stop price acts as the model's value of lost load: it is high enough that the optimiser will always prefer to build real generation rather than rely on the slack, but finite so the model remains feasible under extreme scarcity conditions.

The stop price is applied to slack generator `marginal_cost` at build time via `network.build_network`. In peripheral importing zones without extendable capacity, an `external_stop_price` (typically 600 EUR/MWh) can be applied at solve time to set a lower price ceiling that does not distort investment incentives in the main zones.

### Curtailment Sinks

Curtailment sinks are Links attached to each zone's curtailment bus. They absorb excess generation that cannot be stored or exported — for example, wind output that exceeds local demand and export capacity. Their marginal cost is 500 EUR/MWh, which provides a realistic negative price signal: the optimiser will accept curtailment only when the alternative (paying the stop price to balance the system) is worse.

---

## Solve Pipeline

A direct single-stage solve combining capacity expansion and hourly dispatch is not feasible when generators use quadratic marginal costs, because the quadratic coefficient depends on the installed capacity that the solve is simultaneously trying to determine.

DNA-PyPSA resolves this with a two-stage pipeline:

1. **Investment pre-solve** — all generators are simplified to flat marginal costs and minimum generation floors are suspended. The LP determines `p_nom_opt` for every extendable component. Quadratic costs are then calculated from the resulting capacities and restored for the dispatch stage. See [Run Modes — Investment Pre-Solve](run_modes.md#investment-pre-solve).

2. **Dispatch solve** — with capacities fixed, the full model (including quadratic costs, `p_min_pu` floors, storage, and flexible loads) is solved to minimise total system cost over all snapshots.

In `iterate` mode a third stage is added:

3. **Iterative capacity and availability adjustment** — after each dispatch solve, generator `p_max_pu` profiles are adjusted based on profitability per time chunk (variability adjustment) and `p_nom` is nudged up or down based on per-MW profit (capacity adjustment). The loop repeats until both profiles and capacities stabilise. See [Variability Adjustment](algorithms/variability_adjustment.md) and [Capacity Adjustment](algorithms/capacity_adjustment.md).

The combined effect is a system that is cost-optimised at the dispatch level while enforcing long-run profitability for all generators built in Denmark.

---

## Model Scope and Limitations

The following aspects are explicitly outside the scope of the model:

**Market structure**
- Energy-only market with a day-ahead focus. No capacity market, no frequency reserves, no ancillary services. This may underestimate the value of fast-response technologies such as batteries, which earn significant revenue from balancing markets.

**Transmission**
- Only zone-to-zone transmission is modelled (bidding zone Links). There is no sub-zonal grid, no distribution network, and no representation of congestion within a zone.
- Transmission capacities between zones are fixed at values based on ENTSO-E plans. Transmission expansion is not part of the optimisation.
- The DK1–DK2 internal interconnector is not a candidate for investment.

**Temporal scope**
- The model is solved for a single target year (2050). It does not model the transition pathway from today and cannot capture path dependency or stranded asset effects.

**Other exclusions**
- Hydrogen infrastructure (production, storage, transport) costs are excluded from the system cost, though hydrogen demand is included as a flexible load.
- Sub-annual capacity build rates, permitting constraints, and supply chain limits are not explicitly modelled.
- The model assumes a perfect market: all actors have perfect foresight and investment decisions are fully coordinated.
