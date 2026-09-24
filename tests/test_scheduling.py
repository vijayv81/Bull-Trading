import datetime as dt

import pytest

from trading_agent.scheduling import all_checkpoint_crons_utc, checkpoint_cron_utc, is_dst_active


def test_pre_open_cron_during_edt():
    assert checkpoint_cron_utc("pre_open", dt.date(2026, 7, 1)) == "45 12 * * 1-5"


def test_pre_open_cron_during_est():
    assert checkpoint_cron_utc("pre_open", dt.date(2026, 1, 15)) == "45 13 * * 1-5"


def test_all_checkpoints_shift_together_across_dst_boundary():
    edt = all_checkpoint_crons_utc(dt.date(2026, 7, 1))
    est = all_checkpoint_crons_utc(dt.date(2026, 1, 15))
    assert edt.keys() == est.keys()
    for name in edt:
        edt_hour = int(edt[name].split()[1])
        est_hour = int(est[name].split()[1])
        assert est_hour == (edt_hour + 1) % 24


def test_is_dst_active():
    assert is_dst_active(dt.date(2026, 7, 1)) is True
    assert is_dst_active(dt.date(2026, 1, 15)) is False


def test_dst_transition_boundary_fall_back_2026():
    assert is_dst_active(dt.date(2026, 10, 30)) is True
    assert is_dst_active(dt.date(2026, 11, 2)) is False


def test_dst_transition_boundary_spring_forward_2027():
    assert is_dst_active(dt.date(2027, 3, 13)) is False
    assert is_dst_active(dt.date(2027, 3, 15)) is True


def test_unknown_checkpoint_raises():
    with pytest.raises(ValueError):
        checkpoint_cron_utc("nonexistent")
