"""CLI entry point for dna-pypsa."""

import argparse
import logging
import sys
from datetime import datetime
from pathlib import Path

from dna_pypsa.config import (
    ConfigError,
    get_external_stop_price,
    get_pre_solve,
    get_solver,
    load_config,
)
from dna_pypsa.logging_config import configure_logging
from dna_pypsa.network import build_network
from dna_pypsa.run.run import run_expand, run_iterate

logger = logging.getLogger(__name__)


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    """Add arguments shared by both subcommands.

    Parameters
    ----------
    parser : argparse.ArgumentParser

    """
    parser.add_argument(
        "-n",
        "--network",
        required=True,
        metavar="DIR",
        help="Directory containing a full PyPSA CSV network export.",
    )
    parser.add_argument(
        "-c",
        "--config",
        required=True,
        metavar="FILE",
        help="Path to the scenario config JSON file.",
    )
    parser.add_argument(
        "-o",
        "--output",
        required=True,
        metavar="DIR",
        help="Directory to write results into.",
    )
    parser.add_argument(
        "--solver",
        choices=["highs", "gurobi"],
        default=None,
        help="Solver to use. Overrides the config file value (default: highs).",
    )
    parser.add_argument(
        "--pre-solve",
        choices=["full", "island"],
        default=None,
        help="Investment pre-solve mode. Overrides run.pre_solve in config (default: island).",
    )
    parser.add_argument(
        "--investment-slack-cost",
        type=float,
        default=0.0,
        metavar="COST",
        help="Temporarily reduce slack generator cost during investment pre-solve.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Build the network and export it as CSVs to the output directory, then exit without solving.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build and return the top-level argument parser.

    Returns
    -------
    argparse.ArgumentParser

    """
    parser = argparse.ArgumentParser(
        prog="dna-pypsa",
        description="PyPSA capacity expansion and dispatch runner for the Danish Nuclear Alliance.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    expand_parser = subparsers.add_parser(
        "expand",
        help="Capacity expansion pre-solve followed by a single dispatch solve.",
    )
    _add_common_args(expand_parser)

    iterate_parser = subparsers.add_parser(
        "iterate",
        help=(
            "Capacity expansion pre-solve followed by iterative variability "
            "and capacity adjustment until convergence."
        ),
    )
    _add_common_args(iterate_parser)
    iterate_parser.add_argument(
        "--convergence-threshold",
        type=float,
        default=0.10,
        metavar="PCT",
        help="Objective spread threshold (%%) for convergence checks (default: 0.10).",
    )
    iterate_parser.add_argument(
        "--convergence-window",
        type=int,
        default=3,
        metavar="N",
        help="Rolling window length for objective stability check (default: 3).",
    )
    iterate_parser.add_argument(
        "--min-iterations",
        type=int,
        default=10,
        metavar="N",
        help="Minimum iterations before convergence checks can fire (default: 10).",
    )

    return parser


def main() -> None:
    """Entry point for the dna-pypsa CLI."""
    parser = _build_parser()
    args = parser.parse_args()

    log_level = logging.DEBUG if args.verbose else logging.INFO
    scenario_name = Path(args.config).stem
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_file = Path(args.output) / f"{timestamp}_{scenario_name}.log"
    configure_logging(level=log_level, log_file=log_file)

    try:
        config = load_config(args.config)
    except (ConfigError, FileNotFoundError) as exc:
        logger.error("Config error: %s", exc)
        sys.exit(1)

    solver = args.solver or get_solver(config)
    pre_solve = args.pre_solve or get_pre_solve(config)
    external_stop_price = get_external_stop_price(config)

    try:
        network = build_network(args.network, config)
    except (FileNotFoundError, ConfigError) as exc:
        logger.error("Network build error: %s", exc)
        sys.exit(1)

    output_dir = Path(args.output)

    if args.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)
        network.export_to_csv_folder(output_dir)
        logger.info("Dry run complete. Network exported to %s", output_dir)
        return
    common_kwargs = {
        "solver": solver,
        "output_directory": output_dir,
        "investment_slack_cost": args.investment_slack_cost,
        "pre_solve": pre_solve,
        "external_stop_price": external_stop_price,
    }

    try:
        if args.mode == "expand":
            run_expand(network, **common_kwargs)
        elif args.mode == "iterate":
            run_iterate(
                network,
                **common_kwargs,
                convergence_threshold=args.convergence_threshold,
                convergence_window=args.convergence_window,
                min_iterations=args.min_iterations,
            )
    except ValueError as exc:
        logger.error("Solve failed: %s", exc)
        sys.exit(1)

    logger.info("Done. Results written to %s", output_dir)
