"""
Per-schema setting overrides (NBO_SETTINGS): only the temporary CI schema uses them, so they must be
safe: only known names, always reset to the real values, never leaking from one call into the next.
"""
import pandas as pd
import pytest

import model_training as mt


@pytest.fixture
def settings_table(monkeypatch):
    rows = {"value": pd.DataFrame(columns=["NAME", "VALUE"])}
    monkeypatch.setattr(mt, "DB", "NBO_REBUILD.HM_ML_CI_TEST")
    monkeypatch.setattr(mt, "q", lambda sql: rows["value"])
    yield rows
    rows["value"] = pd.DataFrame(columns=["NAME", "VALUE"])
    mt.apply_settings()


def test_empty_table_means_real_settings(settings_table):
    assert mt.apply_settings() == {}
    assert mt.TASTE["epochs"] == 200 and mt.BASKET["epochs"] == 100 and mt.EVAL_USERS == 20000


def test_overrides_apply_and_reset(settings_table):
    settings_table["value"] = pd.DataFrame({"NAME": ["taste_epochs", "BASKET_EPOCHS"], "VALUE": [10, 5]})
    assert mt.apply_settings() == {"TASTE_EPOCHS": 10, "BASKET_EPOCHS": 5}
    assert mt.TASTE["epochs"] == 10 and mt.BASKET["epochs"] == 5
    assert mt.TASTE["no_components"] == 35                       # untouched settings keep their value
    settings_table["value"] = pd.DataFrame(columns=["NAME", "VALUE"])
    mt.apply_settings()
    assert mt.TASTE["epochs"] == 200 and mt.BASKET["epochs"] == 100


def test_unknown_setting_is_refused(settings_table):
    settings_table["value"] = pd.DataFrame({"NAME": ["LEARNING_RATE"], "VALUE": [1]})
    with pytest.raises(ValueError, match="cannot be overridden"):
        mt.apply_settings()


def test_missing_table_means_real_settings(monkeypatch):
    def broken(sql):
        raise RuntimeError("table does not exist")
    monkeypatch.setattr(mt, "DB", "NBO_REBUILD.HM_ML")
    monkeypatch.setattr(mt, "q", broken)
    assert mt.apply_settings() == {}
    assert mt.TASTE["epochs"] == 200
