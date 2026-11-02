"""Arithmetic policy decimal34-half-even-v1, independent of caller context."""

from decimal import (
    ROUND_HALF_EVEN,
    Context,
    DivisionByZero,
    FloatOperation,
    InvalidOperation,
    Overflow,
)


def arithmetic_context() -> Context:
    # Construct rather than copy the ambient context (including traps and flags).
    return Context(
        prec=34,
        rounding=ROUND_HALF_EVEN,
        Emin=-6143,
        Emax=6144,
        capitals=1,
        clamp=0,
        flags=[],
        traps=[InvalidOperation, DivisionByZero, Overflow, FloatOperation],
    )
