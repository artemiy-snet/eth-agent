from decimal import Decimal  # noqa: F401
import pytest

from agent.live.journal import load_state, save_state
from agent.strategy import v3


def _state():
    return v3.V3State(bots=[])


def test_empty_file_restored_from_bak(tmp_path):
    p = tmp_path / "state_live_Cm.json"
    save_state(p, _state(), {"a": 1})
    save_state(p, _state(), {"a": 2})        # тепер є .bak з a=1
    p.write_text("")                          # телефон вимкнувся — файл порожній
    st, extra = load_state(p)
    assert extra == {"a": 1}
    assert (tmp_path / "state_live_Cm.json.broken").exists()


def test_empty_file_without_bak_is_clear_error_then_clean_start(tmp_path):
    p = tmp_path / "state_live_Cm.json"
    p.write_text("")
    with pytest.raises(RuntimeError, match="зіпсований"):
        load_state(p)
    assert load_state(p) is None              # другий запуск — чистий стан
