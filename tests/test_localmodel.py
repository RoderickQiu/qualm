"""The local model's first start and life: pinned downloads, a server that
fails (said in words, tried again later and later), one that stops, the
disk, the port, the download that goes on where it stopped. Fake servers
are small Python scripts on a free port (never 8009); no model, no network."""

import errno
import socket
import sys
import threading
import time
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from qualm import kevserve, localmodel, paths
from qualm.localmodel import WORDS, ManagedServer, server_command


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("QUALM_HOME", str(tmp_path / "home"))
    # Unset for the test, and as they were after it (the app sets KEV_URL
    # itself when it moves to another port).
    for name in ("KEV_URL", "HF_ENDPOINT", "QUALM_BUILD"):
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
    monkeypatch.setattr(paths, "LOGS", tmp_path / "Logs")
    monkeypatch.chdir(tmp_path)
    return tmp_path / "home"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    assert port not in (8009, 8765)
    return port


@pytest.fixture
def fake(home, tmp_path, monkeypatch):
    """A ManagedServer whose "server" is a Python script, on a free port; the runtime counts as installed."""
    monkeypatch.setattr(localmodel, "runtime_ready", lambda: True)
    monkeypatch.setattr(localmodel, "unsupported", lambda: None)
    monkeypatch.setattr(localmodel, "disk_short", lambda: None)
    said = []

    def make(code: str):
        monkeypatch.setattr(localmodel, "server_command",
                            lambda port: ([sys.executable, "-c", code, str(port)], {}, tmp_path))
        server = ManagedServer(said.append, port=free_port())
        server.poll = 0.05
        return server

    make.said = said
    return make


SERVE = """
import json, sys, threading, time
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def do_GET(self):
        body = json.dumps({"models": [{"name": "kev-latest", "run": "fake"}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(body))); self.end_headers()
        self.wfile.write(body)
srv = HTTPServer(("127.0.0.1", int(sys.argv[1])), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
time.sleep(1.5)
sys.exit(1)
"""


def test_kev_4b_is_pinned_and_an_existing_install_needs_nothing_new(home, monkeypatch):
    # The folder an install from before the pin saved its 8-bit copy in: these
    # exact revisions, so it's used as it is, with no new download.
    assert localmodel.saved_copy().name == \
        "485ace870359-Qwen--Qwen3.5-4B-Base@1001bb4d826a52d1f399e183466143f4da7b741b-q8g64"
    assert localmodel.MODEL.endswith("@485ace8703592fcf405488b262449990824cfed1")
    assert localmodel.PREBUILT.endswith("@f56017669221a6baab2b380c0b0c06ef826d0897")
    monkeypatch.setattr(localmodel, "can_build", lambda: False)
    for model in (localmodel.MODEL, "jaredpalmer/kev-4b"):  # `qualm serve --model jaredpalmer/kev-4b` too
        argv, env, _ = server_command(model)
        assert argv[-3] == localmodel.MODEL and env["QUALM_PREBUILT"] == localmodel.PREBUILT
    # A runtime installed from the git URL of the same commit is the same code.
    marker = home / "kev-env" / ".qualm-installed"
    marker.parent.mkdir(parents=True)
    marker.write_text(f"kev[serve] @ git+https://github.com/jaredpalmer/kev@{localmodel.KEV_COMMIT}\n")
    assert localmodel.runtime_ready()
    marker.write_text("kev[serve] @ git+https://github.com/jaredpalmer/kev@" + "0" * 40 + "\n")
    assert not localmodel.runtime_ready()
    assert "git+" not in localmodel.KEV_SPEC and not localmodel.needs_git()  # installs without git


def test_building_here_only_with_room_for_it(home, monkeypatch):
    monkeypatch.setattr(localmodel, "can_build", lambda: False)
    assert server_command()[1]["QUALM_BUILD"] == "0"
    monkeypatch.setattr(localmodel, "can_build", lambda: True)
    assert server_command()[1]["QUALM_BUILD"] == "1"
    (localmodel.saved_copy() / "model.safetensors").parent.mkdir(parents=True)
    (localmodel.saved_copy() / "model.safetensors").write_bytes(b"")
    assert "QUALM_BUILD" not in server_command()[1]  # already saved: nothing to build or fetch
    assert "QUALM_BUILD" not in server_command("someone/other-model")[1] and \
        "QUALM_PREBUILT" not in server_command("someone/other-model")[1]


def test_room_to_build(monkeypatch):
    monkeypatch.setattr(localmodel, "memory", lambda: localmodel.Memory(24, 12, 0, 0))  # 12 GB spare: no
    monkeypatch.setattr(localmodel, "disk_free_gb", lambda: 100.0)
    assert not localmodel.can_build()
    monkeypatch.setattr(localmodel, "memory", lambda: localmodel.Memory(64, 12, 0, 0))
    assert localmodel.can_build()
    monkeypatch.setattr(localmodel, "disk_free_gb", lambda: 10.0)  # the bf16 base wouldn't fit
    assert not localmodel.can_build()


def test_a_mirror_from_settings_reaches_the_server(home, monkeypatch):
    monkeypatch.setattr(localmodel, "can_build", lambda: False)
    home.mkdir()
    (home / "rules.toml").write_text('[settings]\nhf_endpoint = "https://hf-mirror.com"\n')
    assert server_command()[1]["HF_ENDPOINT"] == "https://hf-mirror.com"
    monkeypatch.setenv("HF_ENDPOINT", "https://mine.example")  # the environment wins
    assert server_command()[1]["HF_ENDPOINT"] == "https://mine.example"
    monkeypatch.delenv("HF_ENDPOINT")
    (home / "rules.toml").write_text('[settings]\nhf_endpoint = "hf-mirror.com"\n')  # not an address: refused
    assert "HF_ENDPOINT" not in server_command()[1]
    from qualm.rules import load_config

    # Plain http (the weights come through it) and an address with junk after it are refused, with why.
    for bad in ("http://hf-mirror.example", "https://hf-mirror.com junk"):
        (home / "rules.toml").write_text(f'[settings]\nhf_endpoint = "{bad}"\n')
        with pytest.raises(ValueError, match="https address"):
            load_config(home / "rules.toml")
        assert "HF_ENDPOINT" not in server_command()[1]
    (home / "rules.toml").write_text('[settings]\nhf_endpoint = "https://mirror.example/hf"\n')  # a path is fine
    assert server_command()[1]["HF_ENDPOINT"] == "https://mirror.example/hf"


def test_the_prebuilt_copys_hash_is_qualms_own_not_the_mirrors(home, monkeypatch):
    monkeypatch.setattr(localmodel, "can_build", lambda: False)
    env = server_command()[1]
    assert (env["QUALM_PREBUILT_SHA256"], env["QUALM_PREBUILT_BYTES"]) == (localmodel.PREBUILT_SHA256, "4469640165")
    prov = {"model.safetensors": {"sha256": "whatever-the-mirror-says", "bytes": 1}}
    for name in ("QUALM_PREBUILT_SHA256", "QUALM_PREBUILT_BYTES"):  # what kevserve gets
        monkeypatch.setenv(name, env[name])
    assert kevserve.expected(prov) == (localmodel.PREBUILT_SHA256, 4_469_640_165)
    for name in ("QUALM_PREBUILT_SHA256", "QUALM_PREBUILT_BYTES"):
        monkeypatch.delenv(name)
    assert kevserve.expected(prov) == ("whatever-the-mirror-says", 1)  # another copy: its own provenance
    monkeypatch.setenv("QUALM_PREBUILT", "me/my-copy@abc")
    assert "QUALM_PREBUILT_SHA256" not in server_command()[1]


def test_a_failing_start_says_why_and_waits_longer_each_time(fake):
    server = fake("print('qualm: error offline LocalEntryNotFoundError: no connection', flush=True); raise SystemExit(3)")
    waits = [server._attempt() for _ in range(5)]
    assert waits == [5, 15, 60, 300, 300]
    first = "downloading the model (about 5 GB, the first time only)…"
    assert fake.said[:4] == [first, "no internet, or Hugging Face is blocked here; retrying in 5 s",
                             first, "no internet, or Hugging Face is blocked here; retrying in 15 s"]
    assert fake.said[-1] == "no internet, or Hugging Face is blocked here; retrying in 5 min"
    assert server.failing and "hf_endpoint" in server.hint  # the tooltip says what to try


def test_a_mirror_that_cant_be_reached_is_named(fake, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.example")
    server = fake("print('qualm: error offline ConnectError: [Errno 8] nodename nor servname provided', flush=True); "
                  "raise SystemExit(3)")
    assert server._attempt() == 5
    assert fake.said[-1] == "can't download the model from hf-mirror.example; retrying in 5 s"  # not "no internet"
    assert "https://hf-mirror.example" in server.hint and "hf_endpoint=" in server.hint


def test_a_mirror_serving_another_file_is_named_and_not_tried_again(fake, monkeypatch):
    monkeypatch.setenv("HF_ENDPOINT", "https://hf-mirror.example")
    starts = fake.said
    server = fake("print('qualm: error wrong WrongFile: https://hf-mirror.example/... is 67,108,864 bytes, not "
                  "4,469,640,165', flush=True); raise SystemExit(3)")
    assert server._attempt() == 5
    assert fake.said[-1] == "hf-mirror.example serves the wrong model file: set another mirror"  # not "damaged"
    assert "https://hf-mirror.example" in server.hint and "hf_endpoint=" in server.hint
    # The same mirror would serve the same file: nothing is started until another one is set.
    n = len(starts)
    assert server._attempt() == localmodel.WRONG_WAIT and len(starts) == n
    monkeypatch.setenv("HF_ENDPOINT", "https://another-mirror.example")
    server._attempt()
    assert starts[n] == "downloading the model (about 5 GB, the first time only)…"  # tried again, from its start
    # Without a mirror it's something on the way (a proxy, a Wi-Fi sign-in page): said, and tried later.
    monkeypatch.delenv("HF_ENDPOINT")
    server = fake("print('qualm: error wrong WrongFile: ...', flush=True); raise SystemExit(3)")
    assert [server._attempt(), server._attempt()] == [5, 15]
    assert fake.said[-1] == "the download sent another file, not the model; retrying in 15 s"


def test_killed_for_memory_and_crashes_in_words(fake):
    server = fake("import os; os.kill(os.getpid(), 9)")
    assert server._attempt() == 5
    assert fake.said[-1] == "the model server was stopped by macOS, likely out of memory; retrying in 5 s"
    server = fake("raise SystemExit(1)")
    server._attempt(), server._attempt()
    assert fake.said[-1] == "the model server stopped (exit 1); retrying in 15 s"
    server._attempt()
    assert fake.said[-1].startswith("the model server keeps stopping: see ") and fake.said[-1].endswith("kev.log")


def test_another_start_in_progress_is_waited_for_not_counted(fake):
    server = fake("print('qualm: error running another model server is already running', flush=True); raise SystemExit(3)")
    assert [server._attempt(), server._attempt()] == [15, 15]
    assert fake.said[-1] == WORDS["running"][0] and server.failures == 0


def test_the_download_shows_how_far_it_got(fake, home):
    part = home / "models" / "x-q8g64.partial" / "model.safetensors.part"
    part.parent.mkdir(parents=True)
    with part.open("wb") as f:
        f.truncate(1_200_000_000)  # sparse: 1.2 GB on paper, nothing on disk
    server = fake("import time; print('qualm: downloading 4469640165', flush=True); time.sleep(1)")
    server._attempt()
    assert "downloading the model: 1.2 of 4.5 GB…" in fake.said
    assert localmodel.downloaded() == 1_200_000_000


def test_ready_then_stopped_starts_again_soon(fake):
    server = fake(SERVE)
    assert server._attempt() == 5  # answered, then stopped: the first failure since
    assert "watching" in fake.said and fake.said[-1] == "the model server stopped (exit 1); retrying in 5 s"
    assert not server.ready.is_set()


def test_a_runtime_that_didnt_install_is_tried_again(fake, monkeypatch, home):
    server = fake("raise SystemExit(0)")
    monkeypatch.setattr(localmodel, "runtime_ready", lambda: False)
    calls = []

    def install(say):
        calls.append(1)
        raise RuntimeError("uv pip failed: error: Request failed after 3 retries\n  Caused by: tcp connect error")

    monkeypatch.setattr(localmodel, "install_runtime", install)
    assert [server._attempt(), server._attempt()] == [5, 15] and len(calls) == 2
    assert fake.said[-1] == "can't download the model's runtime: offline, or blocked?; retrying in 15 s"
    assert "tcp connect error" in localmodel.log_file().read_text()  # the whole error, for whoever looks


def fake_uv(monkeypatch) -> list:
    """uv, recording what it's asked to run instead of installing anything."""
    ran = []

    def run(argv, **kw):
        ran.append(argv)
        if argv[1] == "venv":
            paths.Path(argv[-1]).mkdir(parents=True, exist_ok=True)
        return localmodel.subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(localmodel.paths, "uv", lambda: "/fake/uv")
    monkeypatch.setattr(localmodel.subprocess, "run", run)
    return ran


def test_kev_from_a_copy_of_its_archive_where_github_is_blocked(home, monkeypatch):
    hint = WORDS["runtime"][1]  # uv.toml mirrors PyPI and Python only: the hint doesn't pretend otherwise
    assert "nothing there redirects GitHub" in hint and f"QUALM_KEV_ARCHIVE=<a copy of {localmodel.KEV_ARCHIVE}>" in hint
    ran = fake_uv(monkeypatch)
    monkeypatch.setenv("QUALM_KEV_ARCHIVE", "https://mirror.example/kev.zip")  # doesn't name the commit
    localmodel.install_runtime(lambda line: None)
    assert ran[-1][-1] == "kev[serve] @ https://mirror.example/kev.zip"
    assert localmodel.runtime_ready()  # installed: not again at the next start
    monkeypatch.delenv("QUALM_KEV_ARCHIVE")
    assert localmodel.kev_spec() == localmodel.KEV_SPEC


def test_a_second_runtime_install_waits_for_the_first(home, monkeypatch):
    import fcntl

    ran, said = fake_uv(monkeypatch), []
    home.mkdir()
    with (home / "kev-env.lock").open("a") as first:
        fcntl.flock(first, fcntl.LOCK_EX)  # the app's install, under way
        second = threading.Thread(target=localmodel.install_runtime, args=(said.append,))  # `qualm serve`'s
        second.start()
        time.sleep(0.3)
        assert said == ["another Qualm is installing the local model's runtime; waiting for it…"] and ran == []
        localmodel._marker().parent.mkdir(parents=True)
        localmodel._marker().write_text(f"{localmodel.KEV_SPEC}\ncommit {localmodel.KEV_COMMIT}\n")
    second.join(5)
    assert not second.is_alive() and ran == []  # the first installed it: nothing left to do


def test_stop_ends_the_server_and_its_last_words(fake):
    server = fake("import time; time.sleep(60)")
    server.start()
    for _ in range(100):
        if server.proc is not None:
            break
        time.sleep(0.05)
    proc = server.proc
    server.stop()
    assert proc.poll() is not None
    n = len(fake.said)
    time.sleep(0.3)
    assert len(fake.said) == n  # no "the model server stopped (exit -15)" after Model > Hosted


def test_a_model_server_already_running_is_used_and_left_alone(fake, monkeypatch):
    server = fake("raise SystemExit(0)")
    srv = ThreadingHTTPServer(("127.0.0.1", server.port), _Models)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    started = []
    monkeypatch.setattr(localmodel, "server_command", lambda port: started.append(port))
    done = threading.Thread(target=lambda: started.append(("wait", server._attempt())))
    done.start()
    time.sleep(0.3)
    assert server.ready.is_set() and fake.said == ["using the model server already running"]
    srv.shutdown()
    srv.server_close()
    server.stop()
    done.join(10)
    assert started == [("wait", 0)]  # never started one of its own


class _Models(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = b'{"models": [{"name": "kev-latest"}]}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _NotAModel(_Models):
    def do_GET(self):
        self.send_response(404)
        self.send_header("Content-Length", "0")
        self.end_headers()


def test_a_port_held_by_another_app_moves_the_model(fake, monkeypatch):
    server = fake("raise SystemExit(0)")
    taken = server.port
    srv = ThreadingHTTPServer(("127.0.0.1", taken), _NotAModel)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        assert localmodel.probe(taken) == "other"
        server._pick_port()
        assert server.port != taken and localmodel.os.environ["KEV_URL"] == f"http://127.0.0.1:{server.port}"
        assert server._note == f"port {taken} is used by another app: the model runs on {server.port}"
    finally:
        srv.shutdown()
        srv.server_close()
    assert localmodel.probe(free_port()) == "none"


def test_kev_url_elsewhere_is_never_started_here(fake, monkeypatch):
    monkeypatch.setenv("KEV_URL", "http://gpu-box.local:8009")
    server = fake("raise SystemExit(0)")
    started = []
    monkeypatch.setattr(localmodel, "server_command", lambda port: started.append(port))
    monkeypatch.setattr(localmodel, "probe", lambda port, url=None, timeout=10: "none")
    assert server._attempt() == 5 and started == []
    assert fake.said == ["the model server at gpu-box.local doesn't answer; retrying in 5 s"]
    monkeypatch.setenv("KEV_URL", "http://127.0.0.1:18411")  # this Mac: that's where ours runs
    assert ManagedServer(print).port == 18411


def quick_looks(monkeypatch):
    """probe() and find_server() wait 0.3 s for an answer, not seconds."""
    look = localmodel._look
    monkeypatch.setattr(localmodel, "_look", lambda url, timeout, given: look(url, 0.3, given))


def test_a_listener_that_never_answers_isnt_taken_for_the_model_server(fake, monkeypatch):
    server = fake("raise SystemExit(0)")
    quick_looks(monkeypatch)
    taken = server.port
    with socket.socket() as quiet:
        quiet.bind(("127.0.0.1", taken))
        quiet.listen(64)  # takes the connection, never says a word
        monkeypatch.setattr(localmodel, "_kev_listening", lambda port: False)
        assert localmodel.probe(taken) == "other"
        server._pick_port()
        assert server.port != taken and server._note.startswith(f"port {taken} is used by another app")
        # A Kev server that's slow to answer (a Mac deep in swap) is busy, not another app: used, not moved.
        monkeypatch.setattr(localmodel, "_kev_listening", lambda port: port == taken)
        assert localmodel.probe(taken) == "model"
        # So is a slow answer at KEV_URL: someone's own model server.
        monkeypatch.setattr(localmodel, "_kev_listening", lambda port: False)
        assert localmodel.probe(url=f"http://127.0.0.1:{taken}") == "model"


class _SlowModels(_Models):
    def do_GET(self):
        time.sleep(0.5)  # past the first look's 0.3 s, well inside the second's
        super().do_GET()


def test_a_busy_model_server_behind_a_tunnel_is_used_not_moved_from(fake, monkeypatch):
    # ssh -L 8009:gpu-box:8009, or a Docker forward: the listener isn't named kevserve, and the model it
    # carries is busy. It answers as a model server does, given longer: used, not taken for another app.
    server = fake("raise SystemExit(0)")
    quick_looks(monkeypatch)
    monkeypatch.setattr(localmodel, "_kev_listening", lambda port: False)
    srv = ThreadingHTTPServer(("127.0.0.1", server.port), _SlowModels)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        taken = server.port
        assert localmodel.probe(taken) == "model"
        server._pick_port()
        assert server.port == taken and not server._note
    finally:
        srv.shutdown()
        srv.server_close()


def test_status_and_doctor_agree_on_why_nothing_answers(home, monkeypatch, capsys):
    from qualm import cli, doctor, review

    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    port = free_port()
    monkeypatch.setattr(localmodel, "PORT", port)  # standing in for 8009
    monkeypatch.setattr(review, "app_running", lambda data, settings=None: {"pid": 1})
    foreign = ThreadingHTTPServer(("127.0.0.1", port), _NotAModel)
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    try:
        with pytest.raises(SystemExit):
            cli.main(["status"])
        out = capsys.readouterr().out
        assert f"model: on this Mac, not answering: port {port} is used by another app, not a model server." in out
        assert "starting it" not in out and "downloads" not in out  # no download to wait for
        with pytest.raises(SystemExit):
            cli.main(["status", "--json"])
        assert f'"error": "port {port} is used by another app, not a model server"' in capsys.readouterr().out
        checks = {c["check"]: c for c in doctor.checks(str(home / "rules.toml"), str(home / "data"))}
        assert checks["Model server"]["found"] == f"port {port} is used by another app"
    finally:
        foreign.shutdown()
        foreign.server_close()
    # KEV_URL on another machine that's down: that host doesn't answer; nothing here is starting it.
    monkeypatch.setenv("KEV_URL", "http://192.0.2.1:8009")
    monkeypatch.setattr(localmodel, "_look", lambda url, timeout, given: ("none", None))
    with pytest.raises(SystemExit):
        cli.main(["status"])
    out = capsys.readouterr().out
    assert "model: the model server at 192.0.2.1 doesn't answer (KEV_URL=http://192.0.2.1:8009)" in out
    assert "on this Mac" not in out


def test_a_remote_model_server_thats_down_is_unreachable_not_running(fake, monkeypatch):
    monkeypatch.setenv("KEV_URL", "http://192.0.2.1:8009")  # off the network: the connection never opens

    def blackholed(address, timeout=None, *rest):
        raise TimeoutError("timed out")

    monkeypatch.setattr(localmodel.socket, "create_connection", blackholed)
    server = fake("raise SystemExit(0)")
    assert localmodel.probe(url="http://192.0.2.1:8009", timeout=0.3) == "none"
    assert server._attempt() == 5 and not server.ready.is_set()
    assert fake.said == ["the model server at 192.0.2.1 doesn't answer; retrying in 5 s"]


def test_terminal_commands_use_the_port_the_app_moved_to(fake, monkeypatch):
    from qualm import decide

    monkeypatch.delenv("QUALM_BACKEND", raising=False)
    server = fake("raise SystemExit(0)")
    taken = server.port
    monkeypatch.setattr(localmodel, "PORT", taken)  # standing in for 8009
    foreign = ThreadingHTTPServer(("127.0.0.1", taken), _NotAModel)
    threading.Thread(target=foreign.serve_forever, daemon=True).start()
    monkeypatch.setattr(server, "_run", lambda: None)  # the port and its note only
    server.start()
    moved = f"http://127.0.0.1:{server.port}"
    model = ThreadingHTTPServer(("127.0.0.1", server.port), _Models)
    threading.Thread(target=model.serve_forever, daemon=True).start()
    try:
        monkeypatch.delenv("KEV_URL")  # a terminal: the app's own KEV_URL isn't there
        assert localmodel.server_url() == moved
        assert localmodel.find_server() == (moved, {"name": "kev-latest"})  # status and doctor
        assert decide.make_client(None)._config.base_url == moved  # rules test, allow test, eval
        server.stop()
        assert localmodel.server_url() == f"http://127.0.0.1:{taken}"  # gone with the app's server
        with pytest.raises(RuntimeError, match=f"port {taken} is used by another app, not a model server"):
            decide.make_client(None)  # nothing on screen goes to the other app
        (localmodel.paths.models_dir() / "server.json").write_text(f'{{"url": "{moved}", "pid": 999999}}')
        assert localmodel.server_url() == f"http://127.0.0.1:{taken}"  # a note left by an app that's gone
    finally:
        for srv in (foreign, model):
            srv.shutdown()
            srv.server_close()


def test_a_busy_model_server_is_up_and_the_download_is_only_for_a_first_start(home, monkeypatch, capsys):
    from qualm import cli, personalize, review

    quick_looks(monkeypatch)
    with socket.socket() as quiet:
        quiet.bind(("127.0.0.1", 0))
        quiet.listen(64)
        monkeypatch.setenv("KEV_URL", f"http://127.0.0.1:{quiet.getsockname()[1]}")  # answers after the 0.3 s
        assert localmodel.find_server()[1] == {"busy": True}
        assert personalize._model_status("kev") == {"reachable": True, "url": localmodel.os.environ["KEV_URL"],
                                                     "busy": True}
    monkeypatch.setattr(review, "app_running", lambda data, settings=None: {"pid": 1})
    monkeypatch.setattr(localmodel, "find_server", lambda: ("http://127.0.0.1:18890", {"busy": True}))
    cli.main(["status"])  # no exit code: it's up
    assert "model: on this Mac, up at http://127.0.0.1:18890, busy answering" in capsys.readouterr().out
    monkeypatch.setattr(localmodel, "find_server", lambda: ("http://127.0.0.1:18890", None))
    with pytest.raises(SystemExit):
        cli.main(["status"])
    assert "a first start downloads about 6 GB" in capsys.readouterr().out  # the runtime too, as setup says
    localmodel.saved_copy().mkdir(parents=True)
    (localmodel.saved_copy() / "model.safetensors").write_bytes(b"")
    with pytest.raises(SystemExit):
        cli.main(["status"])
    out = capsys.readouterr().out
    assert "not answering yet at http://127.0.0.1:18890: the app is starting it;" in out and "5 GB" not in out


def test_an_unsupported_mac_stops_trying(fake, monkeypatch):
    server = fake("raise SystemExit(0)")
    monkeypatch.setattr(localmodel, "unsupported", lambda: "The local model needs macOS 14 or later.")
    assert server._attempt() is None
    assert fake.said == [WORDS["unsupported"][0]] and "macOS 14" in server.hint


def test_every_status_line_fits_the_menu(monkeypatch):
    monkeypatch.setattr(paths, "LOGS", paths.Path.home() / "Library" / "Logs" / "Qualm")
    log = "~/Library/Logs/Qualm/kev.log"
    lines = [t for t, _ in WORDS.values()]
    lines += [localmodel._install_words(e)[0] for e in ("No space left on device", "tcp connect error", "odd", "uv is needed")]
    lines += [localmodel._exit_words(c, None, n)[0] for c, n in ((-9, 1), (1, 1), (-15, 2), (255, 3))]
    lines += [localmodel._exit_words(3, (why, ""), 1, "https://a-mirror-with-a-rather-long-name.example.com")[0]
              for why in ("offline", "wrong", "damaged")]
    said = []
    monkeypatch.setenv("KEV_URL", "http://a-gpu-box-with-a-long-hostname.example.com:8009")
    monkeypatch.setattr(localmodel, "probe", lambda port, url=None, timeout=10: "none")
    remote = ManagedServer(said.append)
    remote.failures = 3  # the next wait is the longest
    remote._attempt()
    lines += said
    longest = max((localmodel._when(w) for w in localmodel.BACKOFF), key=len)
    lines = [t.replace("{wait}", longest).replace("{log}", log) for t in lines]
    lines += ["not enough disk space for the model: needs 7 GB free, 123.4 GB left",
              "downloading the model: 12.3 of 14.5 GB…",
              "building the model here (first time: a 9 GB download, 16 GB of memory)…",
              "installing the local model's runtime (Kev, PyTorch, MLX: about 1 GB)…",
              f"downloading the model (about {localmodel.DOWNLOAD_GB:.0f} GB, the first time only)…",
              "port 8009 is used by another app: the model runs on 8010"]
    too_long = [t for t in lines if len(t) > 80]
    assert not too_long


def test_kev_log_is_set_aside_when_it_grows(home):
    paths.LOGS.mkdir(parents=True)
    with localmodel.log_file().open("wb") as f:
        f.truncate(localmodel.LOG_MAX + 1)
    localmodel._open_log().close()
    assert localmodel.log_file().stat().st_size == 0
    assert (paths.LOGS / "kev.log.1").stat().st_size == localmodel.LOG_MAX + 1


def test_disk_space_for_what_the_first_start_still_downloads(home, monkeypatch):
    monkeypatch.setattr(localmodel, "disk_free_gb", lambda: 3.0)
    assert localmodel.disk_needed_gb() == 6.0  # the runtime and the weights
    assert localmodel.disk_short() == "not enough disk space for the model: needs 7 GB free, 3.0 GB left"
    part = home / "models" / "x-q8g64.partial" / "model.safetensors.part"
    part.parent.mkdir(parents=True)
    with part.open("wb") as f:
        f.truncate(4_000_000_000)  # most of it downloaded already
    assert localmodel.disk_needed_gb() == pytest.approx(2.0)
    assert localmodel.disk_short() is None


def test_unsupported_macs(monkeypatch):
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: True)
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("13.6.1", ("", "", ""), "arm64"))
    assert "macOS 14" in localmodel.unsupported()
    monkeypatch.setattr(localmodel.platform, "mac_ver", lambda: ("14.0", ("", "", ""), "arm64"))
    assert localmodel.unsupported() is None
    monkeypatch.setattr(localmodel, "apple_silicon", lambda: False)
    assert "Apple silicon" in localmodel.unsupported()


# -- kevserve's own helpers (the rest runs inside the model's runtime) -------------


def named(name: str, **attrs) -> Exception:
    e = type(name, (Exception,), {})()
    e.__dict__.update(attrs)
    return e


def test_why_a_start_failed_in_one_word():
    response = lambda code: type("R", (), {"status_code": code})()  # noqa: E731
    assert kevserve.reason(named("LocalEntryNotFoundError")) == "offline"
    assert kevserve.reason(urllib.error.URLError(ConnectionRefusedError())) == "offline"
    assert kevserve.reason(named("HfHubHTTPError", response=response(403))) == "blocked"
    assert kevserve.reason(named("HfHubHTTPError", response=response(429))) == "busy"
    assert kevserve.reason(named("RepositoryNotFoundError", response=response(401))) == "missing"
    assert kevserve.reason(OSError(errno.ENOSPC, "No space left on device")) == "disk"
    assert kevserve.reason(PermissionError(errno.EACCES, "Permission denied")) == "denied"
    assert kevserve.reason(MemoryError()) == "memory"
    try:  # what caused it counts: a library's own error around a full disk
        try:
            raise OSError(errno.ENOSPC, "No space left on device")
        except OSError as e:
            raise RuntimeError("couldn't save") from e
    except RuntimeError as e:
        assert kevserve.reason(e) == "disk"
    assert kevserve.reason(ValueError("something else")) == "error"


def test_one_server_per_models_folder(tmp_path):
    first = kevserve.lock(tmp_path / "models")
    assert first is not None and kevserve.lock(tmp_path / "models") is None  # `qualm serve` while the app starts one
    first.close()
    assert kevserve.lock(tmp_path / "models") is not None


def test_no_second_model_loads_where_one_answers_or_the_port_is_taken(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("QUALM_PARENT", raising=False)
    assert kevserve.holder(free_port()) is None
    for handler, there, reason in ((_Models, "model", "running"), (_NotAModel, "other", "port")):
        srv = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        port = srv.server_address[1]
        try:
            assert kevserve.holder(port) == there
            # The old app's server holds no lock of this folder: the port says it all.
            monkeypatch.setenv("QUALM_MODEL_CACHE", str(tmp_path / reason))
            monkeypatch.setattr(sys, "argv", ["kevserve.py", "--run", localmodel.MODEL, "--port", str(port)])
            with pytest.raises(SystemExit) as e:
                kevserve.main()  # before MLX is imported (it isn't even installed here), let alone any weights
            assert e.value.code == kevserve.EXIT and f"qualm: error {reason} " in capsys.readouterr().out
        finally:
            srv.shutdown()
            srv.server_close()


def test_qualm_serve_never_starts_a_second_model(home, monkeypatch):
    from qualm import autostart

    ran = []
    monkeypatch.setattr(localmodel, "install_runtime", lambda say=print: ran.append("install"))
    monkeypatch.setattr(autostart.os, "execvpe", lambda *a: ran.append("exec"))
    monkeypatch.setattr(localmodel, "starting", lambda: True)  # the app's server holds models/
    with pytest.raises(SystemExit, match="already running or getting ready"):
        autostart.serve(port=free_port())
    monkeypatch.setattr(localmodel, "starting", lambda: False)
    for handler, words in ((_Models, "already answers at"), (_NotAModel, "is used by another app")):
        srv = ThreadingHTTPServer(("127.0.0.1", free_port()), handler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        try:
            with pytest.raises(SystemExit, match=words):
                autostart.serve(port=srv.server_address[1])
        finally:
            srv.shutdown()
            srv.server_close()
    assert ran == []  # nothing installed, nothing started


def test_a_file_of_another_size_is_damaged_before_a_byte_is_saved(tmp_path):
    payload = b"x" * 1000  # a mirror's own weights, say: not the 4.5 GB pinned

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            if start >= len(payload):
                self.send_response(416)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(payload) - start))
            self.end_headers()
            self.wfile.write(payload[start:])

    srv = ThreadingHTTPServer(("127.0.0.1", free_port()), Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url, part = f"http://127.0.0.1:{srv.server_address[1]}/model.safetensors", tmp_path / "model.safetensors.part"
    try:
        with pytest.raises(kevserve.Damaged) as e:
            kevserve.download(url, part, 5000)
        assert kevserve.reason(e.value) == "wrong" and not part.exists()  # the app says so, not "damaged"
        part.write_bytes(payload)  # all of it, from an earlier try: the server has nothing more
        with pytest.raises(kevserve.Damaged):
            kevserve.download(url, part, 5000)
        kevserve.download(url, tmp_path / "right.part", 1000)  # the size expected: downloaded
    finally:
        srv.shutdown()
        srv.server_close()
    assert (tmp_path / "right.part").read_bytes() == payload


def test_pruning_removes_only_the_copies_qualm_made(tmp_path):
    base = "Qwen--Qwen3.5-4B-Base@1001bb4d826a52d1f399e183466143f4da7b741b"

    def copy(name):
        (tmp_path / name).mkdir()
        return tmp_path / name

    new = copy(f"485ace870359-{base}-q8g64")
    kevserve.mark(copy(f"7fe7aaaaaaaa-{base}-q8g64"))  # an earlier pinned copy Qualm downloaded
    (copy(f"068dbbbbbbbb-{base}-q8g64.partial") / "model.safetensors.part").write_bytes(b"x")  # one it left unfinished
    kept = [new.name, f"kev-{base}-q8g64", f"adapterbump1-{base}-q8g64",  # `qualm serve --model` builds
            f"kev-{base}-q8g64.partial", f"7fe7aaaaaaaa-{base}-q4g64"]  # a dev build cut short; other bits
    for name in kept[1:]:
        (copy(name) / "model.safetensors").write_bytes(b"x")
    kevserve.mark(tmp_path / kept[-1])
    kevserve.prune(new)
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(kept)


def test_a_download_cut_off_goes_on_where_it_stopped(tmp_path):
    payload = bytes(range(256)) * 4096  # 1 MB
    seen = []

    class Hub(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            rng = self.headers.get("Range")
            seen.append(rng)
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            self.send_response(206 if start else 200)
            self.send_header("Content-Length", str(len(payload) - start))
            self.end_headers()
            if len(seen) == 1:  # the first connection drops halfway
                self.wfile.write(payload[:len(payload) // 2])
                self.wfile.flush()
                self.connection.shutdown(socket.SHUT_RDWR)
                return
            self.wfile.write(payload[start:])

    srv = ThreadingHTTPServer(("127.0.0.1", free_port()), Hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url, part = f"http://127.0.0.1:{srv.server_address[1]}/model.safetensors", tmp_path / "model.safetensors.part"
    try:
        with pytest.raises(Exception) as cut:
            kevserve.download(url, part, len(payload))
        assert kevserve.reason(cut.value) == "offline"
        assert part.stat().st_size == len(payload) // 2  # kept for the next try
        kevserve.download(url, part, len(payload))
        kevserve.download(url, part, len(payload))  # complete: not fetched again
    finally:
        srv.shutdown()
        srv.server_close()
    assert part.read_bytes() == payload
    assert seen == [None, f"bytes={len(payload) // 2}-"]
