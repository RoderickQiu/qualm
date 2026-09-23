"""seenot-desktop: SeeNot's intent-aware intervention on macOS, decided by a
Jev-style typed decision model (local Kev, or hosted Jev).

    seenot-desktop probe              print the screen state on every change, no model
    seenot-desktop ask                one reading of the current screen
    seenot-desktop app                menu bar app: watch, and step in when a rule is hit
    seenot-desktop watch              the same loop in the terminal, printing every judgement
    seenot-desktop review             every judgement it made; --fix the wrong ones
    seenot-desktop label              capture the screen and record your ground-truth labels
    seenot-desktop eval               replay labels through the model; precision, recall, latency
    seenot-desktop harvest            your answers to interventions -> label records
    seenot-desktop install            start the model server and the app at login (uninstall: remove)
    seenot-desktop export             labels -> Kev training JSONL, for a fine-tune

Make it yours (each takes --help and --json; each change takes --dry-run):

    seenot-desktop rules list|show|add|set|on|off|remove|starters
    seenot-desktop rules test|label|tune   try a rule on your recent screens, then set its threshold
    seenot-desktop allow list|add|set|on|off|remove   kinds of page never flagged
    seenot-desktop except list|add|remove  things that look like a rule but are fine
    seenot-desktop never list|add|remove   apps and sites where no rule fires
    seenot-desktop settings show|set       budgets, apps never read, sites never judged, the question limit
    seenot-desktop config export|apply|check|undo   the whole config as JSON, changed in one checked step
    seenot-desktop schema                  every field, its type, default and meaning
    seenot-desktop status                  app running? model up? config ok?
"""

from .cli import main

__all__ = ["main"]
