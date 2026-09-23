"""qualm: intent-aware screen intervention on macOS (a desktop take on SeeNot), decided by a
Jev-style typed decision model (local Kev, or hosted Jev).

    qualm probe              print the screen state on every change, no model
    qualm ask                one reading of the current screen
    qualm app                menu bar app: watch, and step in when a rule is hit
    qualm watch              the same loop in the terminal, printing every judgement
    qualm review             every judgement it made; --fix the wrong ones
    qualm label              capture the screen and record your ground-truth labels
    qualm eval               replay labels through the model; precision, recall, latency
    qualm harvest            your answers to interventions -> label records
    qualm install            start the model server and the app at login (uninstall: remove)
    qualm export             labels -> Kev training JSONL, for a fine-tune

Make it yours (each takes --help and --json; each change takes --dry-run):

    qualm rules list|show|add|set|on|off|remove|starters
    qualm rules test|label|tune   try a rule on your recent screens, then set its threshold
    qualm allow list|add|set|on|off|remove   kinds of page never flagged
    qualm except list|add|remove  things that look like a rule but are fine
    qualm never list|add|remove   apps and sites where no rule fires
    qualm settings show|set       budgets, apps never read, sites never judged, the question limit
    qualm config export|apply|check|undo   the whole config as JSON, changed in one checked step
    qualm schema                  every field, its type, default and meaning
    qualm status                  app running? model up? config ok?
"""

from .cli import main

__all__ = ["main"]
