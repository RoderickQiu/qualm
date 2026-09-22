"""seenot-desktop: SeeNot's intent-aware intervention on macOS, decided by a
Jev-style typed decision model (local Kev, or hosted Jev).

    seenot-desktop probe              print the screen state on every change, no model
    seenot-desktop ask                one reading of the current screen
    seenot-desktop app                menu bar app: watch, and step in when a rule is hit
    seenot-desktop watch              the same loop in the terminal, printing every judgement
    seenot-desktop label              capture the screen and record your ground-truth labels
    seenot-desktop eval               replay labels through the model; precision, recall, latency
    seenot-desktop harvest            your answers to interventions -> label records
    seenot-desktop export             labels -> Kev training JSONL, for a fine-tune
"""

from .cli import main

__all__ = ["main"]
