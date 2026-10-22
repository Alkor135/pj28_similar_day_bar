r"""Проверки автономного HTML-отчёта по результатам проверки похожих баров.

Формирует искусственные CSV/JSON в tests, запускает генератор отчёта,
проверяет реальные показатели, привязку PnL к датам целевых баров,
нулевые результаты, произвольные веса, независимость от сети и защиту
исходных файлов. Временные файлы удаляются, логи и сеть не используются.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_pnl_report.py -v
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import re
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from similar_bars.backtest_similar_bars import Bar, backtest, save_results


class PnlReportTests(unittest.TestCase):
    """Проверяет сохранённый отчёт и отказ при ошибочных входах без имитации зависимостей."""

    def setUp(self) -> None:
        """Загружает генератор и создаёт временную папку только внутри tests; результата нет."""
        try:
            self.report = importlib.import_module("similar_bars.build_pnl_report")
        except ModuleNotFoundError:
            self.fail("Генератор автономного отчёта ещё не реализован")
        temporary = tempfile.TemporaryDirectory(dir=ROOT / "tests")
        self.addCleanup(temporary.cleanup)
        self.folder = Path(temporary.name)

    def create_results(self, asset: str = "RTS", bodies: tuple[float, ...] = (10, -3, 12, -8)) -> None:
        """Сохраняет искусственный расчёт asset из bodies с неравными весами в self.folder; результата нет."""
        bars = [Bar(f"2020-01-{i+1:02}", "RIH0", value, 2, 3, 100) for i, value in enumerate(bodies)]
        result = backtest(bars, lookback=2, weights=(2, 1, 1, 0.5))
        save_results(result, asset, self.folder / f"{asset}_d1.db", self.folder)

    def change_csv(self, field: str, value: str) -> None:
        """Заменяет field значением value в оценённой строке RTS CSV для проверки повреждения; результата нет."""
        path = self.folder / "RTS_trades.csv"
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream, delimiter=";")
            rows = list(reader)
            fields = reader.fieldnames
        next(row for row in rows if row["status"] == "evaluated")[field] = value
        with path.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=fields, delimiter=";")
            writer.writeheader()
            writer.writerows(rows)

    def test_metrics_and_target_dates(self) -> None:
        """Отчёт сохраняет фактические веса и использует дату цели, а не более раннюю дату сигнала."""
        self.create_results()
        asset = self.report.load_asset(self.folder, "RTS")
        self.assertEqual(asset["summary"]["weights"], {"body": 2, "upper_wick": 1, "lower_wick": 1, "volume": 0.5})
        self.assertEqual(asset["summary"]["points"], 8)
        self.assertEqual(asset["summary"]["mean_trade"], 8)
        self.assertEqual(asset["summary"]["best_trade"], 8)
        self.assertEqual(asset["summary"]["longest_losing_streak"], 0)
        self.assertEqual(asset["points"][0], ["2020-01-01", 0, 0])
        self.assertEqual(asset["points"][-1], ["2020-01-04", 8, 0])
        self.assertNotIn(["2020-01-03", 8, 0], asset["points"])

    def test_empty_trades_and_zero_results_do_not_divide_by_zero(self) -> None:
        """Отсутствие сделок и нейтральные тела дают нулевую кривую и неопределённые отношения."""
        for bodies in ((1, 2), (10, -3, 12, 0)):
            with self.subTest(bodies=bodies):
                self.create_results(bodies=bodies)
                asset = self.report.load_asset(self.folder, "RTS")
                self.assertEqual(asset["summary"]["points"], 0)
                self.assertIsNone(asset["summary"]["profit_factor"])
                self.assertIsNone(asset["summary"]["recovery_factor"])
                self.assertTrue(all(point[1:] == [0, 0] for point in asset["points"]))
                html = self.report.render_report({"RTS": asset}, "Нулевой результат")
                self.assertIn("Нулевой результат", html)

    def test_losses_and_drawdown_are_derived_from_csv(self) -> None:
        """Убытки и просадка учитывают начальный ноль, средний проигрыш имеет положительный модуль."""
        self.create_results(bodies=(10, -3, 12, 8))
        summary = self.report.load_asset(self.folder, "RTS")["summary"]
        self.assertEqual((summary["points"], summary["max_drawdown_points"], summary["mean_loss"]), (-8, 8, 8))
        self.assertEqual(summary["longest_losing_streak"], 1)
        self.assertEqual(summary["recovery_factor"], -1)

    def test_fractional_points_accept_saved_float_rounding(self) -> None:
        """Дробные пункты не отклоняются из-за штатного округления накопленных Decimal при сохранении как float."""
        self.create_results(bodies=(1, -1, 1.1, -0.12345678901234567, 1.2,
                                    -0.12345678901234567, 1.3, -0.12345678901234567))
        asset = self.report.load_asset(self.folder, "RTS")
        self.assertEqual(asset["summary"]["points"], 2.870370367037037)
        self.assertEqual(asset["points"][-1][1], 2.870370367037037)

    def test_damaged_or_mixed_results_are_rejected(self) -> None:
        """Расхождение CSV со сводкой, неконечные числа и неверный знак результата отклоняются."""
        for field, value in (("cumulative_points", "999"), ("pnl_points", "-8"),
                             ("drawdown_points", "8"), ("pnl_points", "nan"),
                             ("target_date", "2019-12-31")):
            with self.subTest(field=field, value=value):
                self.create_results()
                self.change_csv(field, value)
                with self.assertRaises(ValueError):
                    self.report.load_asset(self.folder, "RTS")

    def test_wrong_asset_and_summary_are_rejected(self) -> None:
        """Сводка чужого актива и неверные счётчики не выдаются за согласованный отчёт."""
        for field, value in (("asset", "MIX"), ("hits", 0), ("points", 999), ("signals", 1)):
            with self.subTest(field=field):
                self.create_results()
                path = self.folder / "RTS_summary.json"
                summary = json.loads(path.read_text(encoding="utf-8"))
                summary[field] = value
                path.write_text(json.dumps(summary), encoding="utf-8")
                with self.assertRaises(ValueError):
                    self.report.load_asset(self.folder, "RTS")

    def test_cli_writes_portable_html_and_preserves_inputs(self) -> None:
        """Команда создаёт полный HTML со встроенными данными, кодом и стилями, сохраняя CSV/JSON."""
        for asset in ("RTS", "MIX"):
            self.create_results(asset)
        snapshots = {path: path.read_bytes() for path in self.folder.iterdir()}
        output = self.folder / "report.html"
        arguments = ["--input-dir", str(self.folder), "--output", str(output)]
        for _ in range(2):
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                self.assertEqual(self.report.main(arguments), 0)
        html = output.read_text(encoding="utf-8")
        self.assertTrue(html.lower().startswith("<!doctype html>"))
        embedded = re.search(r'<script type="application/json" id="pnl-data">(.*?)</script>', html, re.S)
        self.assertIsNotNone(embedded)
        data = json.loads(embedded.group(1))
        self.assertEqual(set(data), {"RTS", "MIX"})
        self.assertEqual(data["RTS"]["summary"]["points"], 8)
        self.assertNotRegex(html, r'<(?:script|link|iframe)[^>]+(?:src|href)=["\'](?:https?:)?//')
        self.assertIn("<svg", html)
        self.assertIn("Прибыль-фактор", html)
        for path, snapshot in snapshots.items():
            self.assertEqual(path.read_bytes(), snapshot)

    def test_single_asset_default_output_and_escaped_title(self) -> None:
        """Выбор только RTS не требует MIX; заголовок с HTML-символами выводится как текст."""
        self.create_results()
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.report.main(["--input-dir", str(self.folder), "--assets", "RTS",
                                     "--title", '<script>alert("X")</script> & отчёт'])
        self.assertEqual(code, 0)
        html = (self.folder / "pnl_report.html").read_text(encoding="utf-8")
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn('<script>alert("X")</script>', html)

    def test_invalid_source_preserves_existing_report(self) -> None:
        """При ошибке чтения прежний отчёт сохраняется без частичной перезаписи."""
        output = self.folder / "report.html"
        output.write_text("Старый отчёт", encoding="utf-8")
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = self.report.main(["--input-dir", str(self.folder), "--output", str(output)])
        self.assertEqual(code, 1)
        self.assertEqual(output.read_text(encoding="utf-8"), "Старый отчёт")

    def test_output_cannot_replace_source(self) -> None:
        """Параметр --output не разрешает перезаписать исходный CSV или JSON."""
        self.create_results()
        for name in ("RTS_trades.csv", "RTS_summary.json"):
            output = self.folder / name
            snapshot = output.read_bytes()
            with self.subTest(name=name), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = self.report.main(["--input-dir", str(self.folder), "--assets", "RTS", "--output", str(output)])
            self.assertEqual(code, 1)
            self.assertEqual(output.read_bytes(), snapshot)


if __name__ == "__main__":
    unittest.main()
