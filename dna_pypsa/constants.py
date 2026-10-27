"""Shared constants used across dna_pypsa modules."""

# ---------------------------------------------------------------------------
# Solver
# ---------------------------------------------------------------------------

VALID_SOLVERS: list[str] = ["highs", "gurobi"]

#: Gurobi barrier-log values above this are treated as diverged (infinity).
DIVERGE_THRESHOLD = 1e20

#: Dispatch output below this (MW) is treated as solver noise.
DISPATCH_NOISE_THRESHOLD_MW = 1e-2

#: Slack generator output above this (MW) in a given hour is treated as
#: genuine structural infeasibility.  Hours exceeding this threshold have their
#: bus price replaced with the generator's variable cost before the variability
#: update, so they contribute a neutral signal rather than a corrupted one.
#: Set well above solver noise (1e-2 MW) but below any real infeasibility event.
VARIABILITY_SLACK_MASK_THRESHOLD_MW = 1.0

#: Default market floor price (EUR/MWh).
DEFAULT_START_PRICE = -500

#: Fallback stop price (EUR/MWh) used as the slack generator marginal cost.
DEFAULT_STOP_PRICE = 3000

# ---------------------------------------------------------------------------
# Output directory names
# ---------------------------------------------------------------------------

RESULTS_CSV_DIR = "results"
RESULTS_NC_FILE = "results.nc"

# ---------------------------------------------------------------------------
# Two-stage solve: objective step-decay
# ---------------------------------------------------------------------------

STEP_DECAY_MIN_ITERATIONS = 2
STEP_DECAY_WINDOW = 5

# ---------------------------------------------------------------------------
# Two-stage solve: slow-capacity-mover diagnostic
# ---------------------------------------------------------------------------

CAP_SLOW_MOVER_WINDOW = 5
CAP_SLOW_MOVER_MIN_PERCENT = 5.0

# ---------------------------------------------------------------------------
# Variability adjustment
# ---------------------------------------------------------------------------

VAR_PROPORTIONAL_GAIN = 2.0
VAR_MARKET_SENSITIVITY = 5.0
VAR_MIN_REFERENCE_PRICE = 10.0  # EUR/MWh
VAR_SECANT_MIN_CAPTURE_PRICE_CHANGE = 0.5  # EUR/MWh
VAR_SECANT_MIN_VARIABILITY_CHANGE = 1e-4
VAR_CHUNK_EQUILIBRIUM_TOLERANCE = 1.0  # EUR/MWh

# ---------------------------------------------------------------------------
# Capacity feedback
# ---------------------------------------------------------------------------

CAP_RETIRE_GAIN = 2.0
CAP_EXPAND_GAIN = 1.0
CAP_MARKET_SENSITIVITY = 2.0
CAP_MIN_REFERENCE_COST = 1000.0  # EUR/MW/yr
CAP_SECANT_MIN_PROFIT_CHANGE = 50.0  # EUR/MW/yr
CAP_SECANT_MIN_PNOM_CHANGE = 1.0  # MW
CAP_SECANT_MAX_STEP_MULTIPLIER_RETIRE = 5.0
CAP_SECANT_MAX_STEP_MULTIPLIER_EXPAND = 4.0
CAP_SECANT_LOOKBACK = 5
CAP_PROFIT_TOLERANCE = 0.05
CAP_DAMPING_THRESHOLD = -50_000.0  # EUR/MW/yr
CAP_SMALL_GENERATOR_THRESHOLD_MW = 1_000.0  # MW
CAP_SMALL_GENERATOR_FRACTION = 0.30
CAP_FAST_RETIRE_SEVERITY = 0.75
CAP_MCQ_RECALC_MIN_MW = 0.01  # MW
CAP_MCQ_ZERO_THRESHOLD_MW = 100.0  # MW
CAP_OSCILLATION_MIN_FLIPS = 3
CAP_OSCILLATION_DAMPING = 0.5
CAP_INSIGNIFICANT_MW = 150.0  # MW
CAP_MIN_ABSOLUTE_STEP_MW = 5.0  # MW
