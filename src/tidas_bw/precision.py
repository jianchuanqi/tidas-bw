"""Explicit decimal-to-float64 policy, shared by exchanges and LCIA factors."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from math import isfinite

from .models import MigrationReport


@dataclass(frozen=True)
class PrecisionPolicy:
    allow_rounding: bool = False
    absolute_tolerance: str = "0"
    relative_tolerance: str = "0"

    def __post_init__(self) -> None:
        for value in (self.absolute_tolerance, self.relative_tolerance):
            try:
                number = Decimal(str(value))
            except InvalidOperation as exc:
                raise ValueError(
                    "Precision tolerances must be finite non-negative numbers"
                ) from exc
            if not number.is_finite() or number < 0:
                raise ValueError("Precision tolerances must be finite non-negative numbers")
        if not self.allow_rounding and any(
            Decimal(str(value)) != 0 for value in (self.absolute_tolerance, self.relative_tolerance)
        ):
            raise ValueError("Non-zero tolerances require allow_rounding=True")

    def convert(
        self, value, report: MigrationReport, *, dataset: str, path: str, kind: str = "exchange"
    ) -> float | None:
        try:
            original = Decimal(str(value))
            result = float(original)
            if not original.is_finite() or not isfinite(result) or (original != 0 and result == 0):
                raise ValueError
        except (InvalidOperation, ValueError, OverflowError, TypeError):
            report.add(
                "error",
                f"invalid_{kind}_amount",
                f"Mapping to float64: non-finite, overflowing or underflowing value {value!r}",
                dataset=dataset,
                path=path,
            )
            return None

        # Strict mode retains the original decimal round-trip contract. This
        # accepts ordinary decimals (e.g. 0.01), not a claim of exact binary storage.
        changed = Decimal(str(result)) != original
        with localcontext() as ctx:
            ctx.prec = max(1100, len(original.as_tuple().digits) + 1100)
            binary = Decimal.from_float(result)
            delta = abs(binary - original)
            relative = delta / abs(original) if original else Decimal(0)
            limit = Decimal(str(self.absolute_tolerance)) + Decimal(
                str(self.relative_tolerance)
            ) * abs(original)
        report.metadata.setdefault("numeric_conversions", []).append(
            {
                "dataset": dataset,
                "path": path,
                "original": str(original),
                "target": repr(result),
                "storage": "float64",
                "exact_binary_value": str(binary),
                "absolute_error": str(delta),
                "relative_error": str(relative),
                "allow_rounding": self.allow_rounding,
                "absolute_tolerance": str(self.absolute_tolerance),
                "relative_tolerance": str(self.relative_tolerance),
            }
        )
        rejected = (changed and not self.allow_rounding) or (self.allow_rounding and delta > limit)
        if rejected or changed:
            code = f"unrepresentable_{kind}_precision" if rejected else f"{kind}_amount_rounded"
            report.add(
                "error" if rejected else "warning",
                code,
                f"Mapping to float64: original {original}; target {result!r}; "
                f"exact binary value {binary}; absolute error {delta:.6e}; relative error {relative:.6e}. "
                + (
                    "Strict decimal round-trip check failed; approximation requires explicit opt-in."
                    if not self.allow_rounding
                    else f"Allowed absolute error = atol + rtol * abs(original) = {limit:.6e}."
                )
                + " Original document preservation does not make the calculation lossless.",
                dataset=dataset,
                path=path,
            )
        return None if rejected else result
