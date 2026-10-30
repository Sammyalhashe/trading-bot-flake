import importlib
import os
import sys
from pathlib import Path

sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_output_dir_defaults_to_repo_data_dir(monkeypatch):
    monkeypatch.delenv("DERIVATIVES_DATA_DIR", raising=False)
    import backtesting.collect_derivatives_data as collector
    importlib.reload(collector)
    assert collector.OUTPUT_DIR == Path(collector.__file__).parent.parent / "data" / "derivatives"


def test_output_dir_honours_env_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DERIVATIVES_DATA_DIR", str(tmp_path / "derivs"))
    import backtesting.collect_derivatives_data as collector
    importlib.reload(collector)
    assert collector.OUTPUT_DIR == tmp_path / "derivs"


def test_backtester_reads_the_same_data_dir_override(monkeypatch, tmp_path):
    monkeypatch.setenv("DERIVATIVES_DATA_DIR", str(tmp_path / "derivs"))
    import backtesting.backtest_derivatives as backtest_derivatives
    importlib.reload(backtest_derivatives)
    assert backtest_derivatives.DERIVATIVES_DIR == tmp_path / "derivs"
