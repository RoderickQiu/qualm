"""seenot-desktop: SeeNot's intent-aware intervention on macOS, decided by a
Jev-style typed decision model (local Kev, or hosted Jev).

    seenot-desktop probe              print the screen state on every change, no model
    seenot-desktop ask                one reading of the current screen
    seenot-desktop watch              state -> model -> gate -> notification, on every change
    seenot-desktop label              capture the screen and record your ground-truth labels
    seenot-desktop eval               replay labels through the model; precision, recall, latency
    seenot-desktop export             labels -> Kev training JSONL, for a fine-tune
"""

from .cli import main

__all__ = ["main"]
