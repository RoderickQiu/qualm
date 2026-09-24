"""The CLI an agent relies on: JSON errors and exit codes everywhere, help, `status` on the
model in use, pause spans, the dashboard's port, and the command the agent prompt gives.
No model, no screen: the model and the dashboard are made-up or pointed at nothing."""

import json
import socket
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from qualm import agent, autostart, keychain, paths, personalize, review


def dead_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]  # closed again: nothing answers there


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A home of its own, the model and the dashboard somewhere nothing answers."""
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("KEV_URL", f"http://127.0.0.1:{dead_port()}")
    monkeypatch.setenv("QUALM_DASHBOARD_PORT", str(dead_port()))
    monkeypatch.delenv("TYPESAFE_API_KEY", raising=False)  # a key in the shell would send `status` to the keychain
    monkeypatch.chdir(tmp_path)
    return tmp_path


def qualm(env, *args, stdin=""):
    """The real command, in its own process."""
    return subprocess.run([sys.executable, "-m", "qualm.cli", *args], capture_output=True, text=True, cwd=env,
                          input=stdin, env={**__import__("os").environ})


def test_plain_qualm_prints_the_help_starting_with_setup(env):
    out = qualm(env)
    assert out.returncode == 0 and "qualm setup" in out.stdout and "start here" in out.stdout


def test_json_errors_everywhere(env):
    def err(*args):
        out = qualm(env, *args, "--json")
        return out.returncode, json.loads(out.stdout)["error"]

    code, e = err("pause", "abc", "--bogus")  # argparse's own errors too
    assert code == 2 and e["code"] == "invalid" and "--bogus" in e["message"]
    code, e = err("eval")  # no JSON output: it says so, as JSON
    assert code == 2 and "no JSON output" in e["message"]
    code, e = err("rules", "set", "social", "patterns+=[unclosed")
    assert code == 2 and "regular expression" in e["message"]
    code, e = err("config", "apply", "missing.json")
    assert code == 3 and e["code"] == "not_found"
    for bad, says in (("null", "a JSON object"), ('{"rules": {"a": 1}}', "a list of entries"),
                      ('{"rules": [{"id": "new", "description": "x"}]}', "needs a kind"),
                      ('{"rules": [{"id": "social", "threshold": "0.3"}]}', "must be a number")):
        (env / "in.json").write_text(bad)
        code, e = err("config", "apply", "in.json")
        assert code == 2 and says in e["message"], bad
    (env / "in.json").write_text('{"rules": [{"id": "social", "threshold": null}]}')  # null: back to the default
    out = qualm(env, "config", "apply", "in.json", "--dry-run", "--json")
    assert out.returncode == 0 and "-threshold" in json.loads(out.stdout)["diff"]
    code, e = err("settings", "set", "max_questions=0")
    assert code == 2 and "at least 4" in e["message"]  # invalid, not "over the limit"
    assert json.loads(qualm(env, "guide", "--json").stdout)["guide"].startswith("# Driving Qualm")


def test_export_writes_into_the_data_folder_and_says_when_there_are_no_labels(env):
    out = qualm(env, "export")
    assert out.returncode == 3 and "no labels" in out.stderr and "Traceback" not in out.stderr
    data = env / "home" / "data"
    data.mkdir()
    rec = {"screen": {"app": "Safari", "bundle_id": "com.apple.Safari", "window_title": "x", "url": "https://x.com/1",
                      "text": ["x"]}, "labels": {"page_kind": "feed"}}
    (data / "labels.jsonl").write_text(json.dumps(rec) + "\n" + '{"screen": {"app": "Safari"}}\n')
    out = json.loads(qualm(env, "export", "--json").stdout)
    assert out["records"] == 1 and out["skipped"] == 1  # a line that isn't a label record doesn't stop it
    assert out["out"] == str(data / "train.jsonl") and (data / "train.jsonl").exists()
    assert "(skipped 1 line that isn't a label record" in qualm(env, "export").stdout


def run(args_list, capsys):
    """A command in-process: (exit code, what it printed; with --json only stdout, the JSON)."""
    from qualm import cli

    try:
        cli.main(args_list)
        code = 0
    except SystemExit as e:
        code = e.code
    out = capsys.readouterr()
    return code, out.out if "--json" in args_list else out.out + out.err


def test_status_is_about_the_model_in_use(env, capsys, monkeypatch):
    import typesafe_sdk

    monkeypatch.setattr(keychain, "api_key", lambda: None)
    code, out = run(["settings", "set", "backend=jev"], capsys)
    assert code == 2 and "setup --backend jev" in out  # no key: refused, not saved
    monkeypatch.setattr(keychain, "api_key", lambda: "k")
    assert run(["settings", "set", "backend=jev"], capsys)[0] == 0
    monkeypatch.setattr(keychain, "api_key", lambda: None)
    code, out = run(["status"], capsys)
    assert code == 5 and "hosted by TypeSafe (Jev): no key yet" in out and "HANDOFF" not in out

    class Answering:
        def __init__(self, **kw):
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def list(self):
            return []

    monkeypatch.setattr(keychain, "api_key", lambda: "k")
    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", Answering)
    code, out = run(["status", "--json"], capsys)
    d = json.loads(out)
    assert d["backend"] == "jev" and d["model"] == {"reachable": True, "key": True, "key_saved": True}
    assert code == 5 and not d["app_running"]  # only because the app isn't running
    assert d["error"]["code"] == "unreachable" and d["error"]["message"].startswith("app: not running: run `")


def test_status_json_comes_with_an_error_whenever_it_exits_non_zero(env, capsys):
    code, out = run(["status", "--json"], capsys)  # no app, and the model nowhere
    d = json.loads(out)
    assert code == 5 and d["error"]["code"] == "unreachable" and d["config"]["ok"]
    assert "app: not running" in d["error"]["message"] and "model: on this Mac, not running" in d["error"]["message"]
    rules = env / "home" / "rules.toml"
    rules.write_text(rules.read_text() + "\n[setting]\n")
    code, out = run(["status", "--json"], capsys)
    d = json.loads(out)
    assert code == 2 and d["error"]["code"] == "invalid" and d["error"]["message"].startswith("config: broken: ")
    assert ".; " not in d["error"]["message"] and "; app: not running" in d["error"]["message"]  # one stop, not two


def test_status_reads_the_last_judgement_from_the_end(env, capsys):
    data = env / "home" / "data"
    data.mkdir(parents=True)
    line = {"at": "2026-09-23T10:00:00", "screen": {"app": "Safari", "window_title": "x" * 500}, "decisions": []}
    last = {"at": "2026-09-23T23:59:00", "screen": {"app": "Notes", "window_title": "the last one"}, "decisions": []}
    # Months of lines, the last complete one, and one the app is writing as status reads.
    (data / "judgements.jsonl").write_text((json.dumps(line) + "\n") * 400 + json.dumps(last) + "\n" + '{"at": "half-writ')
    code, out = run(["status", "--json"], capsys)
    assert json.loads(out)["last_judgement"]["title"] == "the last one"


def test_the_app_is_known_by_its_answer_not_by_its_port(env, monkeypatch):
    class Other(BaseHTTPRequestHandler):  # another program on the dashboard's port (AnkiConnect's default is 8765)
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

    other = HTTPServer(("127.0.0.1", 0), Other)
    threading.Thread(target=other.serve_forever, daemon=True).start()
    taken = other.server_address[1]
    monkeypatch.setenv("QUALM_DASHBOARD_PORT", str(taken))
    data, rules = env / "data", env / "rules.toml"
    rules.write_text((Path(review.__file__).parent / "rules.example.toml").read_text())
    assert review.dashboard_port() == taken and review.app_running(data) is None
    server = review.start_dashboard(data, rules, taken)  # the app, as it starts
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        assert port != taken and json.loads((data / review.RUNTIME).read_text())["port"] == port
        assert review.dashboard_url(data) == f"http://127.0.0.1:{port}/"
        assert review.app_running(data)["data"] == str(data.resolve())
        assert review.app_running(env / "another-home" / "data") is None  # another Qualm folder's app isn't this one
    finally:
        server.shutdown()
        other.shutdown()


def test_an_app_from_before_api_hello_is_the_main_installs_never_a_qualm_homes(env, capsys, monkeypatch):
    asked = []

    class Older(BaseHTTPRequestHandler):  # a Qualm from before /api/hello: its page is all it has
        def log_message(self, *a):
            pass

        def do_GET(self):
            asked.append(self.path)
            self.send_response(404 if self.path == "/api/hello" else 200)
            self.end_headers()
            self.wfile.write(b"" if self.path == "/api/hello" else b"<html><head><title>Qualm review</title>")

    older = HTTPServer(("127.0.0.1", 0), Older)
    threading.Thread(target=older.serve_forever, daemon=True).start()
    monkeypatch.setenv("QUALM_DASHBOARD_PORT", str(older.server_address[1]))
    try:
        code, out = run(["status"], capsys)  # a QUALM_HOME no app watches: not "running", not "starting its model"
        assert paths.custom_home() and "app: not running" in out and "the app is starting it" not in out
        assert asked == ["/api/hello"]  # its page isn't even fetched
        monkeypatch.setattr(paths, "custom_home", lambda: False)
        assert review.app_running(env / "home" / "data")["older"]
        code, out = run(["status", "--json"], capsys)  # an agent reads it: the older app can't load new keys
        assert json.loads(out)["app_older"] is True and "an older copy" in run(["status"], capsys)[1]
        code, out = run(["pause", "2h", "--from", "tomorrow 10:00", "--json"], capsys)
        assert code == 0 and "older copy, which knows no planned pauses" in json.loads(out)["warnings"][-1]
    finally:
        older.shutdown()


def test_the_hosted_model_needs_a_saved_key_however_it_is_switched_on(env, capsys, monkeypatch):
    import typesafe_sdk

    home = env / "home"
    monkeypatch.setattr(keychain, "api_key", lambda: None)
    (env / "c.json").write_text('{"settings": {"backend": "jev"}}')
    code, out = run(["config", "apply", "c.json", "--json"], capsys)
    assert code == 2 and "setup --backend jev" in json.loads(out)["error"]["message"]
    assert "backend" not in (home / "rules.toml").read_text().split("[settings]")[1].split("[[")[0]
    # A key only in this shell's TYPESAFE_API_KEY: taken, but Qualm.app started from Finder or at login won't have it.
    monkeypatch.setattr(keychain, "api_key", lambda: "k")
    monkeypatch.setattr(keychain, "unsaved", lambda: True)
    code, out = run(["settings", "set", "backend=jev", "--json"], capsys)
    assert code == 0 and "setup --backend jev` saves it in your keychain" in json.loads(out)["warnings"][0]
    checks = {c["check"]: c for c in json.loads(run(["doctor", "--json"], capsys)[1])["checks"]}
    assert checks["TypeSafe API key"]["status"] == "warn" and "keychain" in checks["TypeSafe API key"]["fix"]

    class Refused:
        def __init__(self, **kw):
            self.models = self

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def list(self):
            raise typesafe_sdk.TypeSafeAuthenticationError(401, {}, {}, "invalid api key", "GET /v1/models")

    monkeypatch.setattr(typesafe_sdk, "TypeSafeClient", Refused)
    code, out = run(["status"], capsys)
    assert code == 5 and "TypeSafe refused the key" in out and "didn't answer" not in out
    assert "--key -" in out and "only in TYPESAFE_API_KEY" in out


def test_rules_list_counts_the_pages_let_through_and_lists_them_only_when_asked(env, capsys):
    data = env / "home" / "data"
    data.mkdir(parents=True)
    page = {"rule": "social", "url": "https://www.reddit.com/r/AskDocs/comments/abc123/my_test_results/",
            "title": "Dr. Lee - test results - Reddit", "at": "2026-09-23T10:00:00"}
    (data / "exceptions.jsonl").write_text(json.dumps(page) + "\n")
    d = json.loads(run(["rules", "list", "--json"], capsys)[1])  # no rules.toml: the starters, and it says so
    assert d["warnings"] == [f"created {env / 'home' / 'rules.toml'} from the starter rules (rules.example.toml)"]
    run(["except", "add", "videos", "a lecture"], capsys)
    code, out = run(["rules", "list", "--json"], capsys)
    d = json.loads(out)
    assert code == 0 and "AskDocs" not in out and "Dr. Lee" not in out
    assert d["not_this_one"] == {"social": 1} and [x["text"] for x in d["exceptions"]] == ["a lecture"]
    assert "pages let through with Not this one: social 1" in run(["rules", "list"], capsys)[1]
    d = json.loads(run(["rules", "list", "--pages", "--json"], capsys)[1])
    assert any(x.get("url") == page["url"] for x in d["exceptions"])
    text = run(["rules", "list", "--pages"], capsys)[1]  # listed in words too, as its help says
    assert f"social       {page['title']}  <{page['url']}>  (not this one)" in text


def test_pause_and_focus_mend_a_session_file_with_a_time_no_clock_can_show(env, capsys):
    """A millisecond timestamp (agents have written them) or 1e20 counts as not set, and the next write drops it."""
    from qualm.policy import Policy, read_session
    from qualm.rules import Settings

    data = env / "home" / "data"
    data.mkdir(parents=True)
    for bad in ({"paused_until": 1790260000000}, {"paused_until": 1e20}, {"pause_later": {"from": 1, "until": 1e20}},
                {"focus": {"intent": "x", "started": 1, "until": 1e300}}):
        (data / "session.json").write_text(json.dumps(bad))
        assert not Policy(Settings(), [], data).paused() and read_session(data)["focus"] is None
        assert "out of range" not in run(["status"], capsys)[1] and run(["focus", "--stop"], capsys)[0] == 0
        code, out = run(["pause", "--stop"], capsys)
        assert code == 0 and "not paused: watching" in out, bad
        assert json.loads((data / "session.json").read_text()) == {"paused_until": 0.0, "pause_later": None, "focus": None}
        assert run(["pause", "10"], capsys)[0] == 0


def test_odd_numbers_and_sites_are_refused_by_name(env, capsys):
    code, out = run(["rules", "tune", "social", "--precision", "5", "--json"], capsys)
    assert code == 2 and "between 0 and 1" in json.loads(out)["error"]["message"]
    code, out = run(["never", "add", "--site", "not a site", "--json"], capsys)
    assert code == 2 and "write a domain" in json.loads(out)["error"]["message"]
    assert not (env / "home" / "data" / "exceptions.jsonl").exists()


def test_pause_takes_spans_and_an_end_up_to_a_week(env, capsys):
    now = datetime.now()
    for arg, minutes in (("2d", 2 * 24 * 60), ("90m", 90), ("2h", 120), ("45", 45)):
        code, out = run(["pause", arg, "--json"], capsys)
        until = datetime.fromisoformat(json.loads(out)["paused_until"])
        assert code == 0 and abs((until - now).total_seconds() / 60 - minutes) < 2, arg
    code, out = run(["pause", "--until", "mon 09:00", "--json"], capsys)
    until = datetime.fromisoformat(json.loads(out)["paused_until"])
    assert until.weekday() == 0 and until.hour == 9 and timedelta(0) < until - now <= timedelta(days=7)
    assert run(["pause", "8d"], capsys)[0] == 2
    assert run(["pause", "--until", "someday"], capsys)[0] == 2


def test_pause_until_takes_an_offset_and_refuses_odd_input_cleanly(env, capsys):
    from datetime import timezone

    then = (datetime.now(timezone.utc) + timedelta(hours=30)).replace(second=0, microsecond=0)
    code, out = run(["pause", "--until", then.astimezone(timezone(timedelta(hours=2))).isoformat(), "--json"], capsys)
    assert code == 0 and datetime.fromisoformat(json.loads(out)["paused_until"]) == then.astimezone().replace(tzinfo=None)
    for bad in (["--until", ""], ["--until", "25:00"], ["--until", "0001-01-01T00:00+14:00"], ["--from", "sat"],
                ["--from", ""], [""], ["--from", "tomorrow 18:00", "--until", "tomorrow 09:00"]):
        code, out = run(["pause", *bad, "--json"], capsys)
        assert code == 2 and json.loads(out)["error"]["code"] == "invalid", bad


@pytest.mark.parametrize("today, from_, until, start, end, under_way", [
    (datetime(2026, 9, 24, 2, 16), "sat 00:00", "mon 09:00", datetime(2026, 9, 26), datetime(2026, 9, 28, 9), False),
    (datetime(2026, 9, 25, 23, 0), "sat 00:00", "mon 09:00", datetime(2026, 9, 26), datetime(2026, 9, 28, 9), False),
    # Monday before 9 and Sunday: the coming weekend, and a note on the one under way (`pause --until` pauses it).
    (datetime(2026, 9, 28, 8, 0), "sat 00:00", "mon 09:00", datetime(2026, 10, 3), datetime(2026, 10, 5, 9), True),
    (datetime(2026, 9, 27, 15, 0), "sat 00:00", "mon 09:00", datetime(2026, 10, 3), datetime(2026, 10, 5, 9), True),
    # "Next week off", asked on a Thursday: next week, not Thursday and Friday.
    (datetime(2026, 9, 24, 4, 41), "mon 00:00", "sat 00:00", datetime(2026, 9, 28), datetime(2026, 10, 3), True),
    (datetime(2026, 9, 24, 4, 41), "mon", "fri 18:00", datetime(2026, 9, 28), datetime(2026, 10, 2, 18), True),
    (datetime(2026, 9, 24, 4, 41), "fri 09:00", "thu 09:00", datetime(2026, 9, 25, 9), datetime(2026, 10, 1, 9), True),
    (datetime(2026, 9, 24, 12, 0), "22:00", "07:00", datetime(2026, 9, 24, 22), datetime(2026, 9, 25, 7), False),
    (datetime(2026, 9, 24, 12, 0, 30), "thu 12:00", "fri 09:00", None, datetime(2026, 9, 25, 9), False),  # the minute it is: now
])
def test_a_pause_from_later_starts_at_the_next_such_moment(monkeypatch, today, from_, until, start, end, under_way):
    import argparse

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return today

    monkeypatch.setattr(personalize, "datetime", Clock)
    monkeypatch.setitem(personalize.SESSION, "warnings", [])
    got, minutes = personalize._pause_span(argparse.Namespace(minutes=None, from_=from_, until=until))
    assert (got, (got or today) + timedelta(minutes=minutes)) == (start, end)
    notes = personalize.SESSION["warnings"]
    assert bool(notes) == under_way and all(f"pause --until '{until}'` pauses it from now" in n for n in notes)


@pytest.mark.parametrize("args, says", [
    ({"from_": "2026-10-10 09:00", "until": "2026-10-11 09:00"}, "a pause can start at most 7 days ahead"),
    ({"from_": "2026-09-20 09:00", "until": "mon 09:00"}, "is already past: leave --from out to pause from now"),
    ({"from_": "today 01:00", "until": "mon 09:00"}, "is already past"),
    ({"until": "2026-09-20 09:00"}, "--until '2026-09-20 09:00' is already past"),
    ({"from_": "tomorrow 18:00", "until": "tomorrow 09:00"}, "comes before --from"),
    ({"from_": "sat", "until": "2026-10-20 09:00"}, "pause for 1 minute to 7 days"),
])
def test_a_pause_that_cant_be_says_why(env, capsys, monkeypatch, args, says):
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 24, 4, 41)

    monkeypatch.setattr(personalize, "datetime", Clock)
    argv = [x for k, v in args.items() for x in ("--from" if k == "from_" else "--until", v)]
    code, out = run(["pause", *argv, "--json"], capsys)
    assert code == 2 and says in json.loads(out)["error"]["message"], args


def test_a_pause_can_start_later_and_says_the_day(env, capsys, monkeypatch):
    from qualm.policy import Policy, clock, read_session, write_session
    from qualm.rules import Settings

    now = datetime.now()
    data = env / "home" / "data"
    # "This weekend": the coming one, whatever the day; nothing paused now, the working days stay watched.
    code, out = run(["pause", "--from", "sat 00:00", "--until", "mon 09:00", "--json"], capsys)
    sj = json.loads(out)
    start, until = (datetime.fromisoformat(sj["pause_later"][k]) for k in ("from", "until"))
    assert code == 0 and until.weekday() == 0 and until.hour == 9 and until - start == timedelta(days=2, hours=9)
    assert not sj["paused"] and start.weekday() == 5 and start.hour == 0 and now < start <= now + timedelta(days=7)
    run(["pause", "--stop"], capsys)
    # Planned for tomorrow: said with the day; a pause now keeps it; --stop cancels both.
    code, out = run(["pause", "3h", "--from", "tomorrow 10:00"], capsys)
    assert code == 0 and f"Qualm watches until {clock((now + timedelta(days=1)).replace(hour=10, minute=0).timestamp())}" in out
    assert run(["pause", "20"], capsys)[0] == 0
    s = read_session(data)
    assert s["paused_until"] and s["pause_later"]["until"] - s["pause_later"]["from"] == 3 * 3600
    code, out = run(["status"], capsys)
    assert "a pause is planned from " in out
    run(["focus", "write", "--minutes", "10"], capsys)  # ends the pause running now, not the one planned
    assert read_session(data)["pause_later"] and not read_session(data)["paused_until"]
    code, out = run(["pause", "--stop"], capsys)
    assert code == 0 and "cancelled the pause planned from" in out and read_session(data)["pause_later"] is None
    # Once the planned start comes, it's a pause like any other, for the app too.
    t = datetime.now().timestamp()
    write_session(data, pause_later={"from": t + 3600, "until": t + 7200})
    policy = Policy(Settings(), [], data)
    assert not policy.paused()
    monkeypatch.setattr("qualm.policy.time.time", lambda: t + 3601)
    assert policy.paused() and policy.paused_until == t + 7200 and policy.precheck("com.x", "").reason == "paused"
    # The day wherever an end is shown: today, this week, later.
    assert clock(t, now=t) == datetime.fromtimestamp(t).strftime("%H:%M")
    assert clock(t + 3 * 86400, now=t) == datetime.fromtimestamp(t + 3 * 86400).strftime("%a %H:%M")
    assert clock(t + 10 * 86400, now=t).startswith(datetime.fromtimestamp(t + 10 * 86400).strftime("%b "))


def test_a_focus_session_ends_a_pause_and_says_so(env, capsys):
    run(["pause", "60"], capsys)
    code, out = run(["focus", "write", "the", "report", "--minutes", "30"], capsys)
    assert code == 0 and "This ended the pause" in out
    sj = personalize._session_json(env / "home" / "data")
    assert not sj["paused"] and sj["focus"]["intent"] == "write the report"


def test_focus_and_pause_from_two_processes_keep_both(env):
    code = ("import sys, time; from qualm.policy import write_session; "
            "write_session(sys.argv[1], **({'focus': {'intent': 'x', 'started': time.time(), 'until': time.time() + 1800}} "
            "if sys.argv[2] == 'focus' else {'paused_until': time.time() + 1200}))")
    for i in range(5):
        data = env / f"d{i}"
        procs = [subprocess.Popen([sys.executable, "-c", code, str(data), which]) for which in ("focus", "pause")]
        [p.wait() for p in procs]
        s = json.loads((data / "session.json").read_text())
        assert s["focus"] and s["paused_until"], s


def test_rules_list_shows_everything_that_decides_what_fires(env, capsys):
    run(["never", "add", "--app", "com.apple.dt.Xcode", "--name", "Xcode"], capsys)
    run(["except", "add", "social", "a lecture"], capsys)
    code, out = run(["rules", "list", "--json"], capsys)
    d = json.loads(out)
    assert [c["id"] for c in d["allow"]] == ["shopping", "music", "chat"]
    assert d["never"][0]["app"] == "com.apple.dt.Xcode" and d["exceptions"][0]["text"] == "a lecture"
    text = run(["rules", "list"], capsys)[1]
    assert "Never flagged" in text and "Xcode" in text


def test_the_agent_gets_a_command_a_stock_shell_can_run(tmp_path, monkeypatch):
    shim = tmp_path / "bin" / "qualm"
    monkeypatch.setattr(autostart, "SHIM", shim)
    monkeypatch.setattr(paths, "custom_home", lambda: False)  # the main Qualm folder
    monkeypatch.setattr(paths, "bundle", lambda: Path("/Applications/Qualm.app"))
    assert agent.command() == "/Applications/Qualm.app/Contents/MacOS/Qualm -m qualm"  # no `qualm` command
    shim.parent.mkdir()
    shim.write_text('#!/bin/sh\nexec "/Applications/Qualm.app/Contents/MacOS/Qualm" -m qualm "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")  # a stock shell has no ~/.local/bin
    assert agent.command() == str(shim)
    monkeypatch.setenv("PATH", f"{shim.parent}:/usr/bin:/bin")
    assert agent.command() == "qualm"
    monkeypatch.setattr(paths, "bundle", lambda: None)  # a checkout in a folder with a space
    repo = tmp_path / "My Projects" / "qualm"
    (repo / "src" / "qualm").mkdir(parents=True)
    (repo / "pyproject.toml").write_text("")
    monkeypatch.setattr(agent, "__file__", str(repo / "src" / "qualm" / "agent.py"))
    cmd = agent.command()
    words = subprocess.run(["/bin/zsh", "-fc", f'printf "%s\\n" {cmd}'], capture_output=True, text=True).stdout.split("\n")
    assert words[:5] == ["uv", "run", "--project", str(repo), "qualm"]
    # Another Qualm folder: every hint names it, or the agent would change the main one.
    home = tmp_path / "Second profile"
    monkeypatch.setenv("QUALM_HOME", str(home))
    monkeypatch.setattr(paths, "custom_home", lambda: True)
    cmd = agent.command()
    env = subprocess.run(["/bin/zsh", "-fc", f'{cmd.split(" uv ")[0]} /usr/bin/env'], capture_output=True, text=True).stdout
    assert f"QUALM_HOME={home}\n" in env and cmd.endswith(f"uv run --project '{repo}' qualm")
    assert f"writing `{cmd}` wherever it says `qualm`" in agent.prompt(tmp_path)


def test_the_agent_prompt_asks_for_the_users_language_and_says_what_leaves(tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "bundle", lambda: None)
    text = agent.prompt(tmp_path)
    assert "language I write in" in text and "your provider" in text
    assert f"writing `{agent.command()}` wherever it says `qualm`" in text


def test_the_prompt_and_the_guide_say_what_to_do_with_nothing_to_score(tmp_path, monkeypatch):
    # Day-0 agents saved nothing: the prompt said "score each change before saving" and there were no screens.
    monkeypatch.setattr(paths, "bundle", lambda: None)
    assert "nothing to score it on yet, do what the guide says for a first day" in agent.prompt(tmp_path)
    guide = personalize.guide()
    assert "## The first day: nothing to score yet" in guide and "`no_screens`" in guide


def test_every_command_the_guide_names_exists(monkeypatch, capsys):
    import re

    from qualm import cli

    monkeypatch.setattr(sys, "argv", ["qualm"])
    guide, groups = personalize.guide(), ("rules", "allow", "except", "never", "settings", "config")
    named = {(a, b) if a in groups else (a,) for a, b in re.findall(r"\bqualm ([a-z]+)(?: ([a-z]+))?", guide)}
    named |= set(re.findall(r"`(rules|allow|except|never|settings|config) ([a-z]+)", guide))  # `rules test`, `config undo`
    assert len(named) > 25
    for argv in sorted(named):
        with pytest.raises(SystemExit) as e:
            cli.main([*argv, "--help"])
        assert e.value.code == 0, argv
    capsys.readouterr()


def test_status_asks_the_model_server_doctor_finds(monkeypatch):
    from qualm import localmodel, personalize

    moved = ("http://127.0.0.1:8011", {"name": "jaredpalmer/kev-4b", "run": "kev-4b-q8"})  # 8009 was taken
    monkeypatch.setattr(localmodel, "find_server", lambda: moved)
    assert personalize._model_status("kev") == {"reachable": True, "url": moved[0], "id": "kev-4b-q8"}
