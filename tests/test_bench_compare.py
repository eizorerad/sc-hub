from __future__ import annotations

import json
from pathlib import Path

import pytest

from schub.bench.compare import CompareError, compare


@pytest.fixture(autouse=True)
def project(tmp_path: Path, monkeypatch) -> Path:
    monkeypatch.setenv("SCHUB_PROJECT_DIR", str(tmp_path))
    (tmp_path / "work").mkdir()
    return tmp_path


def test_our_numbers_next_to_the_papers(project: Path, capsys) -> None:
    table = compare({"pearson_delta": 0.43, "mse": 0.0021, "extra": 1.0},
                    {"pearson_delta": 0.45, "mse": 0.0020, "auroc": 0.91},
                    source="Table 2, scGPT row", tolerance={"mse": 0.02}, name="table2")
    rows = {r["metric"]: r for r in table}
    assert rows["pearson_delta"]["verdict"] == "within 10%" and rows["pearson_delta"]["diff"] == pytest.approx(-0.02)
    assert rows["mse"]["verdict"] == "differs" and rows["mse"]["relative"] == pytest.approx(0.05)
    assert rows["auroc"]["verdict"] == "not measured" and rows["auroc"]["ours"] is None
    assert "extra" not in rows  # only the paper's metrics are compared
    printed = capsys.readouterr().out
    assert "Table 2, scGPT row" in printed and "0.43" in printed and "0.45" in printed
    saved = json.loads((project / "work" / "compare" / "table2.json").read_text())
    assert saved["source"] == "Table 2, scGPT row" and len(saved["rows"]) == 3
    assert (project / "work" / "compare" / "table2.csv").read_text().startswith("metric,paper,ours,diff,relative")


def test_bad_inputs_are_refused(project: Path) -> None:
    with pytest.raises(CompareError, match="source"):
        compare({"a": 1.0}, {"a": 1.0}, source="")
    with pytest.raises(CompareError, match="number"):
        compare({"a": "high"}, {"a": 1.0}, source="Fig 2")
    with pytest.raises(CompareError, match="name"):
        compare({"a": 1.0}, {"a": 1.0}, source="Fig 2", name="../x")
    assert compare({"a": 0.0}, {"a": 0.0}, source="Fig 2")[0]["verdict"] == "within 10%"
    [row] = compare({"a": 0.1}, {"a": 0.0}, source="Fig 2")
    assert (row["verdict"], row["relative"]) == ("differs", None)
    with pytest.raises(CompareError, match="finite"):
        compare({"a": float("nan")}, {"a": 1.0}, source="Fig 2")
