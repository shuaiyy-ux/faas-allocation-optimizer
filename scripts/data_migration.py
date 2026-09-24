#!/usr/bin/env python3
"""Validate replacement CSV bundles for the FaaS allocator.

The command is intentionally read-only. It checks file presence, required
columns, basic key integrity, and writes a Markdown report next to the
candidate directory.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "app" / "registry.json"
REPORT_NAME = "VALIDATION_REPORT.md"

ALIASES = {
    "dealer_utilization": {
        "DEALER_CODE": "DEALER_ID",
        "DEALER_NAME": "NAME",
    }
}

OPTIONAL_FILES = {
    "sales_tax_by_state.csv",
    "fleet_inventory_original.csv",
}

ALLOWED_STATUS = {"Incoming", "Grounded", "Transporting", "Delivered"}


def _registry() -> Dict:
    with REGISTRY.open() as f:
        return json.load(f)["datasets"]


def _dataset_file(data_dir: Path, registry_path: str) -> Path:
    return data_dir / Path(registry_path).name


def _read_csv(path: Path) -> pd.DataFrame:
    return pd.read_csv(path, dtype=str, keep_default_na=False)


def _required_columns(name: str, meta: Dict) -> List[str]:
    required = []
    aliases = ALIASES.get(name, {})
    for col in meta["columns"].keys():
        required.append(aliases.get(col, col))
    return required


def _missing_columns(df: pd.DataFrame, required: Iterable[str]) -> List[str]:
    columns = set(df.columns)
    return [col for col in required if col not in columns]


def _non_empty(values: pd.Series) -> pd.Series:
    return values.astype(str).str.strip() != ""


def _zip(values: pd.Series) -> pd.Series:
    return values.astype(str).str.strip().str.zfill(5)


def _subset_check(
    label: str,
    left: pd.Series,
    right: pd.Series,
    failures: List[str],
    warnings: List[str],
    *,
    soft: bool = False,
) -> None:
    left_values = set(left[_non_empty(left)].astype(str))
    right_values = set(right[_non_empty(right)].astype(str))
    missing = sorted(left_values - right_values)
    if not missing:
        return
    msg = f"{label}: {len(missing)} missing value(s), sample {missing[:10]}"
    if soft:
        warnings.append(msg)
    else:
        failures.append(msg)


def _validate(data_dir: Path) -> Tuple[List[str], List[str], Dict[str, pd.DataFrame]]:
    failures: List[str] = []
    warnings: List[str] = []
    frames: Dict[str, pd.DataFrame] = {}
    registry = _registry()

    for name, meta in registry.items():
        path = _dataset_file(data_dir, meta["path"])
        if not path.exists():
            target = warnings if path.name in OPTIONAL_FILES else failures
            target.append(f"Missing file: {path.name}")
            continue
        try:
            df = _read_csv(path)
        except Exception as exc:  # noqa: BLE001
            failures.append(f"Could not read {path.name}: {exc}")
            continue
        missing = _missing_columns(df, _required_columns(name, meta))
        if missing:
            failures.append(f"{path.name}: missing required column(s): {', '.join(missing)}")
        frames[name] = df

    original = data_dir / "fleet_inventory_original.csv"
    if not original.exists():
        warnings.append("Missing optional baseline file: fleet_inventory_original.csv")
    elif "fleet_inventory_original" not in frames:
        frames["fleet_inventory_original"] = _read_csv(original)

    if failures:
        return failures, warnings, frames

    inv = frames["dealer_inventory"]
    util = frames["dealer_utilization"]
    dist = frames["dealer_distance_matrix"]
    faas = frames["faas_eligible_vehicles"]
    zips = frames["zip_centroids"]
    tax = frames["property_tax_by_state"]
    fleet = frames["fleet_inventory"]

    util_dealer_col = "DEALER_ID" if "DEALER_ID" in util.columns else "DEALER_CODE"

    _subset_check(
        "dealer_utilization dealers must exist in dealer_inventory",
        util[util_dealer_col],
        inv["DEALER_CODE"],
        failures,
        warnings,
    )
    _subset_check(
        "dealer_inventory ZIPCODE must exist in zip_centroids",
        _zip(inv["ZIPCODE"]),
        _zip(zips["ZIPCODE"]),
        failures,
        warnings,
    )
    _subset_check(
        "faas_eligible_vehicles ZIPCODE must exist in zip_centroids",
        _zip(faas["ZIPCODE"]),
        _zip(zips["ZIPCODE"]),
        failures,
        warnings,
    )
    _subset_check(
        "dealer_inventory STATE should exist in property_tax_by_state",
        inv["STATE"],
        tax["STATE"],
        failures,
        warnings,
        soft=True,
    )
    _subset_check(
        "fleet_inventory ASSIGNED_DEALER must exist in dealer_inventory",
        fleet["ASSIGNED_DEALER"],
        inv["DEALER_CODE"],
        failures,
        warnings,
    )

    invalid_status = sorted(set(fleet["STATUS"]) - ALLOWED_STATUS)
    if invalid_status:
        failures.append(f"fleet_inventory STATUS has invalid value(s): {invalid_status}")

    expected_pairs = set(zip(faas["DEALER_CODE"], inv["DEALER_CODE"]))
    observed_pairs = set(zip(dist["GROUNDING_DEALER_CODE"], dist["FAAS_DEALER_CODE"]))
    missing_pairs = sorted(expected_pairs - observed_pairs)
    if missing_pairs:
        failures.append(
            "dealer_distance_matrix missing {} required pair(s), sample {}".format(
                len(missing_pairs),
                missing_pairs[:10],
            )
        )

    duplicate_vins = fleet.loc[fleet["VIN"].duplicated(), "VIN"].head(10).tolist()
    if duplicate_vins:
        failures.append(f"fleet_inventory has duplicate VIN(s), sample {duplicate_vins}")

    return failures, warnings, frames


def _write_report(data_dir: Path, failures: List[str], warnings: List[str], frames: Dict[str, pd.DataFrame]) -> Path:
    report = data_dir / REPORT_NAME
    lines = [
        "# Data Validation Report",
        "",
        f"Directory: `{data_dir}`",
        f"Status: `{'FAIL' if failures else 'PASS'}`",
        "",
        "## Files",
        "",
    ]
    for name, df in sorted(frames.items()):
        lines.append(f"- `{name}`: {len(df):,} row(s), {len(df.columns)} column(s)")
    lines.extend(["", "## Failures", ""])
    lines.extend([f"- {item}" for item in failures] or ["- None"])
    lines.extend(["", "## Warnings", ""])
    lines.extend([f"- {item}" for item in warnings] or ["- None"])
    lines.append("")
    report.write_text("\n".join(lines), encoding="utf-8")
    return report


def validate(data_dir: Path) -> int:
    data_dir = data_dir.resolve()
    if not data_dir.is_dir():
        print(f"Not a directory: {data_dir}", file=sys.stderr)
        return 2
    failures, warnings, frames = _validate(data_dir)
    report = _write_report(data_dir, failures, warnings, frames)
    print(f"Wrote {report}")
    if warnings:
        print(f"Warnings: {len(warnings)}")
    if failures:
        print(f"Failures: {len(failures)}", file=sys.stderr)
        return 1
    print("Validation passed")
    return 0


def main(argv: List[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate FaaS CSV bundles")
    sub = parser.add_subparsers(dest="command", required=True)
    validate_parser = sub.add_parser("validate", help="validate a candidate CSV directory")
    validate_parser.add_argument("directory", type=Path)
    args = parser.parse_args(argv)
    if args.command == "validate":
        return validate(args.directory)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
