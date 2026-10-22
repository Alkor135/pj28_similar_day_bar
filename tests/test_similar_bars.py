r"""Проверки поиска похожего бара и последовательного прогноза без будущих данных.

Использует искусственные свечи и временные SQLite-файлы внутри tests.
Проверяет ручные веса, окно истории, направление, ничьи, нулевые тела,
подсчёт пунктов и просадки, даты сигнала и сохранение исходных баз.
Сеть и рабочие котировки не используются, файловые журналы не создаются.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_similar_bars.py -v
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class SimilarBarsTests(unittest.TestCase):
    """Проверяет прогноз, который изменился бы при ошибке окна, весов или сдвига результата."""

    def setUp(self) -> None:
        """Загружает модуль проверки идеи; отсутствие реализации сообщает как отказ теста."""
        try:
            self.module = importlib.import_module("similar_bars.backtest_similar_bars")
        except ModuleNotFoundError:
            self.fail("Проверка торговой идеи ещё не реализована")

    def bars(self, bodies: list[float], uppers: list[float] | None = None,
             volumes: list[int] | None = None):
        """Возвращает последовательные бары с заданными телами, верхними фитилями и объёмами."""
        return [self.module.Bar((date(2020, 1, 1) + timedelta(days=i)).isoformat(),
                                "RIH0", body, uppers[i] if uppers else 2, 3,
                                volumes[i] if volumes else 100)
                for i, body in enumerate(bodies)]

    def test_relative_distance_and_weights(self) -> None:
        """Ручные веса меняют среднее; нулевой вес полностью исключает признак."""
        first, second = self.bars([10, 20], [0, 4], [100, 200])
        self.assertAlmostEqual(self.module.candle_distance(first, second), 0.5)
        self.assertAlmostEqual(self.module.candle_distance(first, second, (3, 0, 0, 1)), 0.5)
        self.assertAlmostEqual(self.module.candle_distance(first, second, (3, 1, 0, 0)), 0.625)
        self.assertAlmostEqual(self.module.candle_distance(first, second, (0, 1, 0, 1)), 0.75)
        self.assertAlmostEqual(self.module.candle_distance(first, second, (6, 2, 0, 0)), 0.625)
        self.assertEqual(self.module.candle_distance(first, second, (0, 0, 1, 0)), 0)
        self.assertEqual(self.module.candle_distance(first, first), 0)

    def test_manual_weights_change_selected_analog(self) -> None:
        """Вес объёма выбирает иной аналог и меняет прогноз по сравнению с весом тела."""
        bars = self.bars([10, -4, 30, 10, -8], volumes=[1000, 200, 100, 100, 500])
        body = self.module.backtest(bars, lookback=3, start=date(2020, 1, 4), weights=(1, 0, 0, 0))
        volume = self.module.backtest(bars, lookback=3, start=date(2020, 1, 4), weights=(0, 0, 0, 1))
        self.assertEqual((body.rows[0]["analog_date"], body.rows[0]["prediction"]), ("2020-01-01", -1))
        self.assertEqual((volume.rows[0]["analog_date"], volume.rows[0]["prediction"]), ("2020-01-03", 1))
        self.assertEqual((body.rows[0]["pnl_points"], volume.rows[0]["pnl_points"]), (8, -8))

    def test_opposite_direction_is_never_selected(self) -> None:
        """Точное совпадение размеров падающего бара не заменяет растущий аналог."""
        result = self.module.backtest(self.bars([-10, 25, 10, -7]), lookback=2)
        row = result.rows[2]
        self.assertEqual(row["analog_date"], "2020-01-02")
        self.assertEqual(row["matched_candidates"], 1)
        self.assertEqual(row["prediction"], 1)
        self.assertEqual(row["pnl_points"], -7)

    def test_tie_uses_most_recent_analog(self) -> None:
        """При одинаковом расстоянии берётся ближайший по времени прошлый бар."""
        row = self.module.backtest(self.bars([10, 10, 10, -6]), lookback=2).rows[2]
        self.assertEqual(row["analog_date"], "2020-01-02")
        self.assertEqual(row["analog_next_date"], row["signal_date"])

    def test_only_previous_twenty_bars_are_candidates(self) -> None:
        """Точное совпадение за пределами 20 предыдущих баров исключается из поиска."""
        bars = self.bars([10] + [20] * 20 + [10, -7])
        row = self.module.backtest(bars, start=date(2020, 1, 22)).rows[0]
        self.assertEqual(row["analog_date"], "2020-01-21")
        self.assertEqual(row["matched_candidates"], 20)
        self.assertEqual(row["pnl_points"], -7)

    def test_future_changes_do_not_change_match_or_forecast(self) -> None:
        """Изменение целевого и более поздних баров меняет результат, но не аналог и прогноз."""
        before = self.module.backtest(self.bars([10, -3, 12, -8, 15]), lookback=2).rows[2]
        after = self.module.backtest(self.bars([10, -3, 12, 9000, -9000]), lookback=2).rows[2]
        for field in ("analog_date", "distance", "prediction", "analog_next_date"):
            self.assertEqual(before[field], after[field])
        self.assertEqual((before["pnl_points"], after["pnl_points"]), (8, -9000))

    def test_start_keeps_history_and_end_limits_signal_dates(self) -> None:
        """Начало периода сохраняет предысторию, а результат после end учитывается для сигнала end."""
        result = self.module.backtest(self.bars([10, -3, 12, -8]), lookback=2,
                                      start=date(2020, 1, 3), end=date(2020, 1, 3))
        self.assertEqual(len(result.rows), 1)
        self.assertEqual((result.rows[0]["target_date"], result.summary["points"]), ("2020-01-04", 8))

    def test_no_same_direction_and_warmup_are_skipped(self) -> None:
        """Неполная история и отсутствие нужного направления не создают фиктивные сделки."""
        result = self.module.backtest(self.bars([-2, -3, 10, -8]), lookback=2)
        self.assertEqual(result.rows[0]["status"], "insufficient_history")
        self.assertEqual(result.rows[2]["status"], "no_matching_direction")
        self.assertEqual(result.rows[2]["pnl_points"], 0)
        self.assertEqual(result.summary["trades"], 0)
        self.assertIsNone(result.summary["accuracy_percent"])

    def test_flat_successor_skips_prediction(self) -> None:
        """Нулевое тело после выбранного аналога приводит к пропуску прогноза."""
        row = self.module.backtest(self.bars([10, 0, 12, -8]), lookback=2).rows[2]
        self.assertEqual((row["status"], row["prediction"], row["pnl_points"]), ("flat_prediction", 0, 0))

    def test_flat_target_has_zero_result(self) -> None:
        """Нулевое тело целевого бара не считается ни выигрышем, ни проигрышем."""
        result = self.module.backtest(self.bars([10, -3, 12, 0]), lookback=2)
        self.assertEqual(result.rows[2]["status"], "evaluated")
        self.assertEqual(result.summary["flat_actual"], 1)
        self.assertEqual((result.summary["hits"], result.summary["misses"], result.summary["points"]), (0, 0, 0))

    def test_scoring_and_drawdown_start_from_zero(self) -> None:
        """Итог использует тело цели; просадка учитывает убыток относительно начального нуля."""
        result = self.module.backtest(self.bars([10, 12, -7, 5, -3]), lookback=1)
        self.assertEqual([row["pnl_points"] for row in result.rows], [0, -7, 0, 0, 0])
        self.assertEqual(result.summary["points"], -7)
        self.assertEqual(result.summary["max_drawdown_points"], 7)
        self.assertEqual(result.summary["misses"], 1)
        self.assertEqual(result.summary["always_long_points"], -7)

    def test_latest_bar_has_forecast_without_result(self) -> None:
        """Последний бар даёт ожидающий прогноз без вымышленного результата следующей свечи."""
        result = self.module.backtest(self.bars([10, -3, 12]), lookback=2)
        row = result.rows[-1]
        self.assertEqual((row["status"], row["prediction"], row["target_date"]), ("pending", -1, None))
        self.assertEqual(result.summary["pending_forecasts"], 1)
        self.assertEqual(result.summary["trades"], 0)

    def test_bad_weights_and_lookback_are_rejected(self) -> None:
        """Отрицательные, бесконечные, нулевые суммарные веса и пустое окно отклоняются."""
        for weights in ((0, 0, 0, 0), (-1, 1, 1, 1), (float("nan"), 1, 1, 1),
                        (float("inf"), 1, 1, 1), (1, 2, 3)):
            with self.subTest(weights=weights), self.assertRaises(ValueError):
                self.module.backtest(self.bars([1, 2]), lookback=1, weights=weights)
        with self.assertRaises(ValueError):
            self.module.backtest(self.bars([1, 2]), lookback=0)

    def create_database(self, path: Path, asset: str) -> None:
        """Создаёт дневную SQLite-базу asset по настоящей схеме агрегатора для проверки команды."""
        from daily_bars.build_daily_bars import BuildResult, DailyBar, save_daily_bars
        rows = []
        for bar in self.bars([10, -3, 12, -8]):
            opening, closing_price = 100, 100 + bar.body
            previous = date.fromisoformat(bar.trade_date) - timedelta(days=1)
            rows.append(DailyBar(bar.trade_date, bar.secid, f"{previous} 21:00:00",
                                 f"{bar.trade_date} 21:00:00", opening,
                                 min(opening, closing_price) - 3, max(opening, closing_price) + 2,
                                 closing_price, 100, bar.body, 2, 3, 1,
                                 f"{bar.trade_date} 10:00:00", f"{bar.trade_date} 10:00:00",
                                 "Тестовый контракт", "2020-03-19"))
        with closing(sqlite3.connect(path)) as connection:
            save_daily_bars(connection, asset, BuildResult(asset, date(2020, 1, 1), date(2020, 1, 4), rows, 0))

    def test_cli_exports_repeatably_and_keeps_sources_unchanged(self) -> None:
        """Команда сохраняет расчёт и веса, повторно заменяет отчёт и не меняет исходные базы."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            root = Path(temporary)
            output = root / "results"
            snapshots = {}
            for asset in ("RTS", "MIX"):
                path = root / f"{asset}_d1.db"
                self.create_database(path, asset)
                snapshots[path] = path.read_bytes()
            args = ["--input-dir", str(root), "--output-dir", str(output), "--lookback", "2",
                    "--start", "2020-01-03", "--body-weight", "3", "--volume-weight", "0"]
            for _ in range(2):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(self.module.main(args), 0)
            for asset in ("RTS", "MIX"):
                summary = json.loads((output / f"{asset}_summary.json").read_text(encoding="utf-8"))
                self.assertEqual((summary["points"], summary["trades"], summary["hits"]), (8, 1, 1))
                self.assertEqual(summary["weights"], {"body": 3, "upper_wick": 1, "lower_wick": 1, "volume": 0})
                with (output / f"{asset}_trades.csv").open(encoding="utf-8-sig", newline="") as stream:
                    rows = list(csv.DictReader(stream, delimiter=";"))
                self.assertEqual(len(rows), 2)
                self.assertEqual(rows[0]["pnl_points"], "8.0")
                self.assertEqual(snapshots[root / f"{asset}_d1.db"], (root / f"{asset}_d1.db").read_bytes())

    def test_wrong_metadata_and_invalid_values_are_rejected(self) -> None:
        """Ошибочные метаданные и отрицательные фитили не попадают в расчёт сходства."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            path = Path(temporary) / "RTS_d1.db"
            self.create_database(path, "RTS")
            with closing(sqlite3.connect(path)) as connection:
                with self.assertRaises(self.module.DataError):
                    self.module.load_bars(connection, "MIX")
        bars = self.bars([1, 2, 3])
        bars[1] = self.module.Bar("2020-01-02", "RIH0", 2, -1, 2, 100)
        with self.assertRaises(ValueError):
            self.module.backtest(bars, lookback=1)

    def test_invalid_cli_parameters_fail_before_reading_databases(self) -> None:
        """Команда отклоняет неверные веса, окно и период с кодом 2 ещё до чтения баз."""
        for arguments in (["--body-weight", "-1"], ["--volume-weight", "nan"],
                          ["--body-weight", "0", "--upper-wick-weight", "0",
                           "--lower-wick-weight", "0", "--volume-weight", "0"],
                          ["--lookback", "0"], ["--start", "2020-03-02", "--end", "2020-03-01"]):
            with self.subTest(arguments=arguments), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    self.module.main(arguments)
                self.assertEqual(raised.exception.code, 2)

    def test_missing_sources_do_not_create_output(self) -> None:
        """При отсутствующих базах возвращается ошибка без создания отчётов и пустых SQLite-файлов."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            output = Path(temporary) / "results"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                code = self.module.main(["--input-dir", temporary, "--output-dir", str(output)])
            self.assertEqual(code, 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
