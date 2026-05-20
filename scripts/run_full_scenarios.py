"""Run all full_scenarios against DNA-PyPSA.

Each scenario is run with dna-pypsa expand using its own network folder and
scenario.json. Results are written to outputs/full_scenarios/<scenario_name>/.

Usage
-----
Run all scenarios::

    python scripts/run_full_scenarios.py

Dry run (builds networks, exports CSVs, no solve)::

    python scripts/run_full_scenarios.py --dry-run

Run specific scenarios::

    python scripts/run_full_scenarios.py --scenario nuclear --scenario solar
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
INPUTS = REPO_ROOT / "inputs" / "full_scenarios"
OUTPUTS = REPO_ROOT / "outputs" / "full_scenarios"

SCENARIO_NAMES = [
    "reference",
    "nuclear",
    "solar",
    "offshore",
    "nuclear_balanced",
    "solar_balanced",
    "offshore_balanced",
]


def run_scenario(name: str, dry_run: bool) -> bool:
    """Run a single scenario with dna-pypsa expand.

    Parameters
    ----------
    name : str
        Scenario name.
    dry_run : bool
        Pass ``--dry-run`` to dna-pypsa.

    Returns
    -------
    bool
        True if the command succeeded.
    """
    network = INPUTS / "networks" / name
    config = INPUTS / "scenarios" / name / f"{name}_scenario.json"
    output = OUTPUTS / name

    if not network.exists():
        logger.error("Network folder not found: %s", network)
        return False
    if not config.exists():
        logger.error("Scenario JSON not found: %s", config)
        return False

    output.mkdir(parents=True, exist_ok=True)

    cmd = [
        "dna-pypsa",
        "expand",
        "-n",
        str(network),
        "-c",
        str(config),
        "-o",
        str(output),
    ]
    if dry_run:
        cmd.append("--dry-run")

    logger.info("Running scenario: %s", name)
    logger.debug("Command: %s", " ".join(cmd))

    result = subprocess.run(cmd)
    if result.returncode != 0:
        logger.error("Scenario %s failed (exit %d)", name, result.returncode)
        return False

    logger.info("Scenario %s complete -> %s", name, output)
    return True


def main() -> None:
    """Entry point."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--scenario",
        action="append",
        dest="scenarios",
        metavar="NAME",
        help="Scenario to run. May be repeated. Defaults to all 7 if omitted.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build networks and export CSVs without solving.",
    )
    args = parser.parse_args()

    scenarios = args.scenarios or SCENARIO_NAMES

    failed = []
    for name in scenarios:
        if not run_scenario(name, dry_run=args.dry_run):
            failed.append(name)

    if failed:
        logger.error("Failed scenarios: %s", ", ".join(failed))
        sys.exit(1)

    logger.info("All %d scenario(s) finished.", len(scenarios))


if __name__ == "__main__":
    main()
