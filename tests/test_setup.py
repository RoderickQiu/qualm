"""`qualm serve` / `install` build the right server command; `doctor` reports in words."""

from pathlib import Path

import pytest

from qualm.autostart import server_command
from qualm.doctor import FAIL, OK, WARN, report

REPO = Path(__file__).resolve().parents[1]


def kev(tmp_path):
    (tmp_path / "kev").mkdir(exist_ok=True)
    (tmp_path / "kev" / "serve.py").write_text("")
    return tmp_path


def test_the_server_is_8_bit_by_default_and_bf16_on_request(tmp_path):
    argv, env = server_command(REPO, kev(tmp_path))
    assert env["KEV_QUANT_BITS"] == "8" and argv[-4:] == ["--run", "jaredpalmer/kev-4b", "--port", "8009"]
    assert str(REPO / "experiments" / "serve_capped.py") in argv
    assert "KEV_QUANT_BITS" not in server_command(REPO, kev(tmp_path), bits=16)[1]


def test_no_kev_repo_says_how_to_get_it(tmp_path):
    with pytest.raises(SystemExit, match="git clone https://github.com/jaredpalmer/kev"):
        server_command(REPO, tmp_path)


def test_doctor_report():
    rows = [{"check": "A", "status": OK, "found": "fine", "fix": ""},
            {"check": "B", "status": WARN, "found": "meh", "fix": "do this"}]
    assert report(rows).splitlines() == ["✓ A: fine", "! B: meh", "    do this", "",
                                         "Working; the notes above would make it better."]
    assert report(rows + [{"check": "C", "status": FAIL, "found": "no", "fix": "that"}]).endswith("1 to fix before Qualm can work.")
