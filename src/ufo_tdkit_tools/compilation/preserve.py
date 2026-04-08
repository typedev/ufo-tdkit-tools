# Copyright 2024 Alexander Lubovenko
# Licensed under the Apache License, Version 2.0

"""
Standalone API for OTF compilation with PS hint preservation.

Provides a high-level wrapper around compile_otf_preserve_optimized()
with rich result reporting and batch processing support.

Extracted from TDKit (src/tdkit/compilers/preserve.py).
"""

from __future__ import annotations

import glob
import logging
import os
import shutil
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Callable, Optional


@dataclass
class PreserveCompileResult:
    """Result of a single preserve-mode OTF compilation."""

    ufo_path: str
    otf_path: Optional[str] = None
    success: bool = False
    error: Optional[str] = None
    warnings: list[str] = field(default_factory=list)
    hints_found: int = 0
    hints_transferred: int = 0
    skipped: bool = False


@dataclass
class BatchCompileResult:
    """Result of a batch preserve-mode compilation."""

    results: list[PreserveCompileResult] = field(default_factory=list)

    @property
    def successful(self) -> int:
        return sum(1 for r in self.results if r.success)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.success and not r.skipped)

    @property
    def skipped_count(self) -> int:
        return sum(1 for r in self.results if r.skipped)

    @property
    def total(self) -> int:
        return len(self.results)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "success": self.successful,
            "failed": self.failed,
            "skipped": self.skipped_count,
            "results": [
                {
                    "ufo": r.ufo_path,
                    "otf": r.otf_path,
                    "status": (
                        "ok" if r.success else ("skipped" if r.skipped else "error")
                    ),
                    "error": r.error,
                    "warnings": r.warnings,
                    "hints_found": r.hints_found,
                    "hints_transferred": r.hints_transferred,
                }
                for r in self.results
            ],
        }


def _check_ufo_has_hints(ufo_path: str) -> tuple[bool, int]:
    """Check if UFO contains com.adobe.type.autohint.v2 hint data.

    Returns (has_hints, glyphs_with_hints).
    """
    from ufo_tdkit_tools.ps_hints.validator import validate_ps_hints

    report = validate_ps_hints(ufo_path)
    return report["glyphs_with_hints"] > 0, report["glyphs_with_hints"]


def preserve_compile(
    ufo_path: str,
    otf_path: str,
    logger: Optional[logging.Logger] = None,
    tx_path: Optional[str] = None,
    makeotfexe_path: Optional[str] = None,
) -> PreserveCompileResult:
    """Compile a single UFO to OTF preserving PS hints.

    Checks for com.adobe.type.autohint.v2 hint data. If found, uses
    hybrid preserve compilation. If not found, skips with error.

    Args:
        ufo_path: Path to UFO source directory.
        otf_path: Path for output OTF file.
        logger: Optional logger instance.
        tx_path: Path to AFDKO tx binary (auto-detected if None).
        makeotfexe_path: Path to AFDKO makeotfexe binary (auto-detected if None).

    Returns:
        PreserveCompileResult with compilation details.
    """
    from ufo_tdkit_tools.compilation.compiler import compile_otf_preserve_optimized

    result = PreserveCompileResult(ufo_path=ufo_path)

    if not os.path.isdir(ufo_path):
        result.error = f"UFO not found: {ufo_path}"
        return result

    # Check for PS hints
    has_hints, hints_count = _check_ufo_has_hints(ufo_path)
    result.hints_found = hints_count

    if not has_hints:
        result.skipped = True
        result.error = "No com.adobe.type.autohint.v2 hint data found"
        return result

    # Resolve tool paths
    _tx = tx_path or shutil.which("tx")
    _mko = makeotfexe_path or shutil.which("makeotfexe")

    if not _tx or not _mko:
        result.error = f"AFDKO tools not found (tx: {_tx}, makeotfexe: {_mko})"
        return result

    # Ensure output directory exists
    os.makedirs(os.path.dirname(os.path.abspath(otf_path)), exist_ok=True)

    # Compile
    compile_stats = {}
    success = compile_otf_preserve_optimized(
        ufo_path,
        otf_path,
        logger=logger,
        tx_path=_tx,
        makeotfexe_path=_mko,
        stats=compile_stats,
    )

    result.success = success
    result.hints_transferred = compile_stats.get("hints_transferred", 0)
    if success:
        result.otf_path = os.path.abspath(otf_path)
    else:
        result.error = "Preserve compilation failed (see logs for details)"

    return result


def _worker_compile(args: tuple) -> PreserveCompileResult:
    """Worker function for parallel batch compilation."""
    ufo_path, otf_path, tx_path, makeotfexe_path = args
    logger = logging.getLogger(f"ufo_tdkit_tools.preserve.{os.path.basename(ufo_path)}")
    return preserve_compile(
        ufo_path, otf_path, logger=logger,
        tx_path=tx_path, makeotfexe_path=makeotfexe_path,
    )


def preserve_compile_batch(
    input_dir: str,
    output_dir: str,
    logger: Optional[logging.Logger] = None,
    parallel: bool = False,
    workers: Optional[int] = None,
    on_progress: Optional[Callable[[str, str], None]] = None,
) -> BatchCompileResult:
    """Compile all UFOs in a directory to OTF with PS hint preservation.

    Args:
        input_dir: Directory containing .ufo files.
        output_dir: Directory for output .otf files.
        logger: Optional logger instance.
        parallel: Use ProcessPoolExecutor for parallel compilation.
        workers: Number of parallel workers (default: cpu_count - 2).
        on_progress: Callback(ufo_name, status) for progress reporting.

    Returns:
        BatchCompileResult with per-file results.
    """
    ufo_list = sorted(glob.glob(os.path.join(input_dir, "*.ufo")))
    if not ufo_list:
        batch = BatchCompileResult()
        if logger:
            logger.error(f"No .ufo files found in {input_dir}")
        return batch

    os.makedirs(output_dir, exist_ok=True)

    # Pre-resolve tool paths
    tx_path = shutil.which("tx")
    makeotfexe_path = shutil.which("makeotfexe")

    # Build task list
    tasks = []
    for ufo_path in ufo_list:
        ufo_name = os.path.basename(ufo_path).replace(".ufo", "")
        otf_path = os.path.join(output_dir, f"{ufo_name}.otf")
        tasks.append((ufo_path, otf_path, tx_path, makeotfexe_path))

    batch = BatchCompileResult()

    if parallel and len(tasks) > 1:
        if workers is None:
            cpu = os.cpu_count() or 4
            workers = max(1, cpu - 2) if cpu > 4 else cpu

        with ProcessPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(_worker_compile, task): task[0] for task in tasks
            }
            for future in as_completed(futures):
                result = future.result()
                batch.results.append(result)
                if on_progress:
                    status = (
                        "ok"
                        if result.success
                        else ("skipped" if result.skipped else "error")
                    )
                    on_progress(os.path.basename(result.ufo_path), status)
    else:
        for task in tasks:
            ufo_path, otf_path, _tx, _mko = task
            result = preserve_compile(
                ufo_path, otf_path, logger=logger,
                tx_path=_tx, makeotfexe_path=_mko,
            )
            batch.results.append(result)
            if on_progress:
                status = (
                    "ok"
                    if result.success
                    else ("skipped" if result.skipped else "error")
                )
                on_progress(os.path.basename(result.ufo_path), status)

    return batch
