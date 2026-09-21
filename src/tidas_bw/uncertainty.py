"""TIDAS → Brightway uncertainty conversion per the TIDAS 0.2.0 specification.

Conversion source: tiangong-lca/tidas-spec, ``assets/tidas/schemas``, the
specification text of the ``relativeStandardDeviation95In`` field:

- **normal**: the field states ``2*SD`` as a percentage of the mean value
  ("Mean value plus 2*SD equals 97.5% value"), so
  ``sigma = |mean| * field / 200``.
- **log-normal**: the field states ``SDg**2`` as a percentage, and the TIDAS
  mean value acts as the geometric mean (median): ``loc = ln(mean)``,
  ``scale = ln(SDg) = 0.5 * ln(field / 100)``.
- **uniform**: ``minimum``/``maximum`` define the distribution completely;
  the TIDAS amount must lie within that interval.
- **triangular**: the specification defines no mode parameter and does not
  state whether the TIDAS amount is the distribution mean or the mode, so the
  distribution cannot be represented in Brightway without assuming unstated
  semantics; it is rejected with a precise reason.
- **undefined**: no uncertainty; maps to Brightway uncertainty type 0.

Field values use the ILCD ``Perc`` type: percentage numbers, e.g. "20"
means 20%. The specification requires the ``relativeStandardDeviation95In``
field to remain empty for uniform and triangular distributions, and defines
the minimum/maximum fields only for uniform and triangular distributions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, DecimalException, InvalidOperation, localcontext
from math import isfinite, log
from typing import Any

from stats_arrays import LognormalUncertainty, NormalUncertainty, UniformUncertainty

DISTRIBUTIONS = {"undefined", "log-normal", "normal", "uniform", "triangular"}


@dataclass(frozen=True)
class UncertaintyMapping:
    """Result of converting one TIDAS uncertainty declaration.

    ``fields`` is a stats_arrays/Brightway uncertainty dict (empty when the
    distribution is undefined or degenerate). ``notes`` are human-readable
    info lines. ``error`` is a precise rejection reason; when it is set the
    caller must stop with an error instead of mapping the exchange.
    """

    fields: dict[str, Any] = field(default_factory=dict)
    notes: tuple[str, ...] = ()
    error: str | None = None


def convert_uncertainty(
    *,
    distribution: Any,
    rsd95: Any,
    minimum: Any,
    maximum: Any,
    amount: float,
) -> UncertaintyMapping:
    """Convert TIDAS uncertainty fields to Brightway uncertainty fields."""
    amount = float(amount)
    if not isfinite(amount):
        return UncertaintyMapping(error="Uncertainty amount must be finite")
    distribution_type = str(distribution or "").strip().lower()
    has_rsd = _has_value(rsd95)
    has_min = _has_value(minimum)
    has_max = _has_value(maximum)

    if not distribution_type or distribution_type == "undefined":
        if has_rsd or has_min or has_max:
            return UncertaintyMapping(
                error=(
                    "Uncertainty fields are present but uncertaintyDistributionType is "
                    "undefined or missing; the intended distribution cannot be determined"
                )
            )
        return UncertaintyMapping()

    if distribution_type not in DISTRIBUTIONS:
        return UncertaintyMapping(
            error=(
                f"Unknown uncertaintyDistributionType {distribution!r}; the TIDAS "
                "specification allows undefined, log-normal, normal, uniform, and triangular"
            )
        )

    if distribution_type == "triangular":
        return UncertaintyMapping(
            error=(
                "Triangular distributions cannot be mapped: the TIDAS specification defines "
                "no mode parameter and does not state whether the TIDAS amount is the "
                "distribution mean or the mode, so the Brightway representation would "
                "assume semantics the specification does not define"
            )
        )

    if distribution_type in {"normal", "log-normal"} and (has_min or has_max):
        return UncertaintyMapping(
            error=(
                "The TIDAS specification defines minimum/maximum uncertainty fields only "
                "for uniform and triangular distributions; they must remain empty for "
                f"{distribution_type} distributions"
            )
        )

    if distribution_type == "uniform":
        if has_rsd:
            return UncertaintyMapping(
                error="relativeStandardDeviation95In must remain empty for uniform distributions"
            )
        return _convert_uniform(minimum, maximum, amount)

    rsd_value = _decimal(rsd95)
    if rsd_value is None or rsd_value < 0:
        return UncertaintyMapping(
            error=f"relativeStandardDeviation95In is required for {distribution_type} "
            f"distributions and must be a non-negative percentage, got {rsd95!r}"
        )

    try:
        with localcontext() as ctx:
            ctx.prec = max(34, len(rsd_value.as_tuple().digits) + 16)
            if distribution_type == "normal":
                return _convert_normal(rsd_value, amount)
            return _convert_lognormal(rsd_value, amount)
    except (DecimalException, OverflowError, ValueError):
        return UncertaintyMapping(
            error="Uncertainty parameter conversion exceeds the supported numeric range"
        )


def _convert_normal(rsd_value: Decimal, amount: float) -> UncertaintyMapping:
    if rsd_value == 0:
        return UncertaintyMapping(
            notes=(
                f"relativeStandardDeviation95In {rsd_value} implies zero standard "
                "deviation; the exchange is mapped as deterministic",
            ),
        )
    if amount == 0:
        return UncertaintyMapping(
            error="A positive relative deviation at zero mean does not define an absolute standard deviation"
        )
    sigma = float(rsd_value * Decimal(str(abs(amount))) / Decimal(200))
    if not isfinite(sigma) or sigma <= 0:
        return UncertaintyMapping(
            error="Normal standard deviation overflows or underflows float64; refusing deterministic simplification"
        )
    return UncertaintyMapping(
        fields={
            "uncertainty type": NormalUncertainty.id,
            "loc": amount,
            "scale": sigma,
        }
    )


def _convert_lognormal(rsd_value: Decimal, amount: float) -> UncertaintyMapping:
    if amount <= 0:
        return UncertaintyMapping(
            error="Log-normal distributions require a positive amount; a log-normal "
            f"distribution on {amount!r} is undefined"
        )
    sdg_squared = rsd_value / Decimal(100)
    if sdg_squared <= 0:
        return UncertaintyMapping(
            error=f"relativeStandardDeviation95In {rsd_value} does not describe a valid "
            "SDg^2 percentage"
        )
    if sdg_squared == 1.0:
        return UncertaintyMapping(
            notes=(
                "relativeStandardDeviation95In implies SDg = 1 (no uncertainty); the "
                "exchange is mapped as deterministic",
            ),
        )
    if sdg_squared < 1.0:
        return UncertaintyMapping(
            error=f"relativeStandardDeviation95In {rsd_value} implies SDg^2 = {sdg_squared} "
            "< 1; the specification states SDg^2, which cannot be below 1 for an "
            "uncertain log-normal distribution"
        )
    # Decimal ln avoids rounding a small but positive deviation down to SDg=1.
    scale = float(sdg_squared.ln() / 2)
    if not isfinite(scale) or scale <= 0:
        return UncertaintyMapping(error="Log-normal scale overflows or underflows float64")
    return UncertaintyMapping(
        fields={
            "uncertainty type": LognormalUncertainty.id,
            "loc": log(amount),
            "scale": scale,
        }
    )


def _convert_uniform(minimum: Any, maximum: Any, amount: float) -> UncertaintyMapping:
    if not _has_value(minimum) or not _has_value(maximum):
        return UncertaintyMapping(
            error="Uniform distributions require both minimum and maximum uncertainty fields"
        )
    low = _decimal(minimum)
    high = _decimal(maximum)
    if low is None or high is None:
        return UncertaintyMapping(
            error=f"Uniform minimum/maximum must be numbers, got {minimum!r}/{maximum!r}"
        )
    if low > high:
        return UncertaintyMapping(
            error=f"Uniform minimum {minimum!r} is greater than maximum {maximum!r}"
        )
    bounds = (float(low), float(high))
    if any(
        not isfinite(v) or (d != 0 and v == 0) for d, v in zip((low, high), bounds, strict=True)
    ):
        return UncertaintyMapping(error="Uniform bounds overflow or underflow float64")
    if low != high and bounds[0] == bounds[1]:
        return UncertaintyMapping(error="Uniform interval collapses in float64")
    if amount < bounds[0] or amount > bounds[1]:
        return UncertaintyMapping(
            error=f"Uniform minimum/maximum define the interval [{low}, {high}], which "
            f"does not contain the exchange amount {amount!r}"
        )
    if low == high:
        return UncertaintyMapping(
            notes=("Uniform interval has zero width; mapped as deterministic",)
        )
    return UncertaintyMapping(
        fields={
            "uncertainty type": UniformUncertainty.id,
            "minimum": float(low),
            "maximum": float(high),
        }
    )


def _decimal(value: Any) -> Decimal | None:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not result.is_finite():
        return None
    return result


def _has_value(value: Any) -> bool:
    return value is not None and (not isinstance(value, str) or bool(value.strip()))
