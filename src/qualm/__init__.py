"""qualm: intent-aware screen intervention on macOS (a desktop take on SeeNot), decided by a
Jev-style typed decision model (local Kev, or hosted Jev).

    qualm setup              start here: where the model runs, the permission, your rules, start at login
    qualm app                menu bar app: watch, and step in when a rule is hit
    qualm status             app running? model up? config ok?
    qualm doctor             is everything in place? each problem with its fix
    qualm focus WHAT         a focus session: every rule steps in, and the pop-up reminds you; --stop ends it
    qualm pause [TIME]       pause every rule: 30 (minutes, the default), 2h, 2d, --until "mon 09:00"; --stop resumes
    qualm review             every judgement it made; --fix the wrong ones; --web opens the dashboard
    qualm week               how this week went: the dashboard's Insights numbers (--json for agents)
    qualm guide              how an AI agent should change your rules (Claude Code, Codex, ...)
    qualm install            start the app at every login (uninstall: stop; uninstall --all: remove everything)
    qualm version            this copy's version; --check says whether a newer one is out
    qualm serve              the local model server (Kev-4B, 8-bit), in this terminal; the app starts its own

For trying and training the model:

    qualm app --demo [KIND]  show the pop-up once: deny, feed, checkin, timesup, focus, or the focus prompt
    qualm probe              print the screen state on every change, no model
    qualm ask                one reading of the current screen
    qualm watch              the app's loop in the terminal, printing every judgement
    qualm label              capture the screen and record your ground-truth labels
    qualm eval               replay labels through the model; precision, recall, latency
    qualm harvest            your answers to interventions -> label records
    qualm export             labels -> Kev training JSONL, for a fine-tune

Make it yours: easiest with an AI agent that can run commands (Claude Code, Codex, ...).
Tell it `run qualm guide`, then what you want. Each command takes --help and --json;
each change takes --dry-run:

    qualm rules list|show|add|set|on|off|remove|starters
    qualm rules test|label|tune   try a rule on your recent screens, then set its threshold
    qualm allow list|test|add|set|on|off|remove   kinds of page never flagged
    qualm except list|add|remove  things that look like a rule but are fine
    qualm never list|add|remove   apps and sites where no rule fires
    qualm settings show|set       apps never read, sites never judged, check-in waits, the question limit
    qualm config export|apply|check|undo|migrate   the whole config as JSON, changed in one checked step
    qualm schema                  every field, its type, default and meaning
"""

from .cli import main

__all__ = ["main"]
