r"""Проверки дневных баров 21:00–21:00 и перехода между контрактами.

Создаёт минутные базы с искусственными котировками в памяти либо внутри tests.
Проверяет OHLCV, тело и фитили, границы интервала, экспирацию, полноту загрузки,
повторную запись и запуск команды. Рабочие базы и сеть не используются.

Запуск из корня проекта:
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -p test_daily_bars.py -v
    .\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import io
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing, redirect_stderr, redirect_stdout
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from MOEX_ISS_API_quote_downloader_m1_two_ticker import update_futures_m1_two_ticker as minute


def prepare_source(connection: sqlite3.Connection, asset: str = "RTS") -> None:
    """Создаёт в connection минутную схему asset и три последовательных контракта."""
    minute.initialize_database(connection, asset)
    prefix = "RI" if asset == "RTS" else "MX"
    connection.executemany("INSERT INTO Contracts VALUES (?, ?, ?)", [
        (prefix + "U6", "Сентябрь", "2026-09-17"),
        (prefix + "Z6", "Декабрь", "2026-12-17"),
        (prefix + "H7", "Март", "2027-03-18"),
    ])
    connection.commit()


def add_minute(connection: sqlite3.Connection, begin: str, secid: str = "RIU6",
               open_: float = 100, high: float = 105, low: float = 95,
               close: float = 102, volume: int = 3) -> None:
    """Добавляет в connection свечу secid с временем begin, заданными OHLC и volume."""
    shortname, expiry = connection.execute(
        "SELECT SHORTNAME, LSTTRADE FROM Contracts WHERE SECID=?", (secid,)).fetchone()
    connection.execute("INSERT INTO Minute VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                       (begin[:10], secid, begin, begin[:-2] + "59", open_, low, high,
                        close, volume, 1000, shortname, expiry))
    connection.commit()


def mark_days(connection: sqlite3.Connection, first: date = date(2026, 9, 15),
              last: date = date(2026, 9, 19)) -> None:
    """Отмечает обе ближайшие серии дат first..last с фактическими счётчиками connection."""
    day = first
    while day <= last:
        pair = connection.execute(
            "SELECT SECID FROM Contracts WHERE LSTTRADE>=? ORDER BY LSTTRADE, SECID LIMIT 2",
            (day.isoformat(),)).fetchall()
        for (secid,) in pair:
            rows = connection.execute(
                "SELECT * FROM Minute WHERE TRADEDATE=? AND SECID=? ORDER BY BEGIN",
                (day.isoformat(), secid)).fetchall()
            connection.execute("INSERT OR REPLACE INTO LoadedDays VALUES (?, ?, ?, ?, ?, ?)",
                               (day.isoformat(), secid, len(rows), minute.candle_digest(rows),
                                int(not rows), "2026-09-20T00:00:00+03:00"))
        day += timedelta(days=1)
    connection.commit()


class DailyBarsTests(unittest.TestCase):
    """Проверяет реальные выборки SQLite, расчёты и атомарное сохранение дневных баров."""

    def setUp(self) -> None:
        """Импортирует агрегатор и создаёт изолированную минутную базу для каждой проверки."""
        try:
            self.daily = importlib.import_module("daily_bars.build_daily_bars")
        except ModuleNotFoundError:
            self.fail("Агрегатор дневных баров ещё не реализован")
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        prepare_source(self.connection)

    def build(self, start: date = date(2026, 9, 16), end: date = date(2026, 9, 16)):
        """Возвращает результат агрегации self.connection для дат окончания start..end."""
        mark_days(self.connection)
        return self.daily.build_daily_bars(self.connection, "RTS", start, end)

    def test_21_boundary_and_ohlcv(self) -> None:
        """Свеча в 21:00 включается слева и исключается справа; OHLCV берутся по времени."""
        add_minute(self.connection, "2026-09-15 20:59:00", open_=800, high=900, low=700, close=850)
        add_minute(self.connection, "2026-09-15 21:00:00", open_=100, high=106, low=98, close=104, volume=2)
        add_minute(self.connection, "2026-09-16 20:59:00", open_=104, high=110, low=97, close=108, volume=5)
        add_minute(self.connection, "2026-09-16 21:00:00", open_=800, high=900, low=700, close=850)
        bar, = self.build().bars
        self.assertEqual((bar.begin, bar.end), ("2026-09-15 21:00:00", "2026-09-16 21:00:00"))
        self.assertEqual((bar.trade_date, bar.secid), ("2026-09-16", "RIU6"))
        self.assertEqual((bar.open, bar.high, bar.low, bar.close, bar.volume), (100, 110, 97, 108, 7))
        self.assertEqual((bar.body, bar.upper_wick, bar.lower_wick), (8, 2, 3))
        self.assertEqual((bar.minutes, bar.first_minute, bar.last_minute),
                         (2, "2026-09-15 21:00:00", "2026-09-16 20:59:00"))

    def test_bearish_doji_and_fractional_prices(self) -> None:
        """Падающее тело отрицательно, у доджи ноль, десятичные цены не дают ложных фитилей."""
        cases = [(110, 113, 96, 100, (-10, 3, 4)),
                 (100, 107, 98, 100, (0, 7, 2)),
                 (100.1, 100.5, 100.0, 100.3, (0.2, 0.2, 0.1))]
        for open_, high, low, close, expected in cases:
            with self.subTest(expected=expected):
                self.connection.execute("DELETE FROM Minute")
                add_minute(self.connection, "2026-09-16 10:00:00", open_=open_,
                           high=high, low=low, close=close)
                bar, = self.build().bars
                self.assertEqual((bar.body, bar.upper_wick, bar.lower_wick), expected)

    def test_expiry_switches_entire_interval_without_mixing(self) -> None:
        """В день экспирации весь бар, включая предыдущий вечер, берётся из следующей серии."""
        add_minute(self.connection, "2026-09-16 10:00:00", close=101)
        add_minute(self.connection, "2026-09-16 21:00:00", open_=900, high=999, low=800, close=950, volume=999)
        add_minute(self.connection, "2026-09-17 18:59:00", open_=900, high=999, low=800, close=950, volume=999)
        add_minute(self.connection, "2026-09-16 21:00:00", "RIZ6", open_=200, high=210, low=190, close=205, volume=2)
        add_minute(self.connection, "2026-09-17 20:59:00", "RIZ6", open_=205, high=215, low=195, close=212, volume=5)
        add_minute(self.connection, "2026-09-17 21:00:00", "RIZ6", open_=212, high=220, low=210, close=218, volume=7)
        result = self.build(end=date(2026, 9, 18))
        self.assertEqual([bar.secid for bar in result.bars], ["RIU6", "RIZ6", "RIZ6"])
        expiry_bar = result.bars[1]
        self.assertEqual((expiry_bar.open, expiry_bar.high, expiry_bar.low, expiry_bar.close,
                          expiry_bar.volume, expiry_bar.minutes), (200, 215, 190, 212, 7, 2))

    def test_empty_intervals_are_omitted_and_weekend_trades_kept(self) -> None:
        """Пустые интервалы не создают свечей, а реальные субботние минуты учитываются."""
        add_minute(self.connection, "2026-09-19 12:00:00", "RIZ6")
        result = self.build(start=date(2026, 9, 18), end=date(2026, 9, 19))
        self.assertEqual([bar.trade_date for bar in result.bars], ["2026-09-19"])
        self.assertEqual(result.empty_intervals, 1)

    def test_default_range_uses_only_covered_intervals(self) -> None:
        """Края диапазона берутся из LoadedDays; вечер последней даты не образует незавершённый бар."""
        add_minute(self.connection, "2026-09-15 10:00:00")
        add_minute(self.connection, "2026-09-16 10:00:00")
        add_minute(self.connection, "2026-09-19 21:00:00", "RIZ6")
        mark_days(self.connection)
        result = self.daily.build_daily_bars(self.connection, "RTS")
        self.assertEqual((result.start, result.end), (date(2026, 9, 16), date(2026, 9, 19)))
        self.assertEqual([bar.trade_date for bar in result.bars], ["2026-09-16"])

    def test_missing_day_and_deleted_minutes_rejected(self) -> None:
        """Пропуск отметки загрузки или удаление минуты обнаруживаются до сохранения результата."""
        add_minute(self.connection, "2026-09-16 10:00:00")
        mark_days(self.connection)
        self.connection.execute("DELETE FROM LoadedDays WHERE TRADEDATE='2026-09-15'")
        with self.assertRaises(self.daily.DataError):
            self.daily.build_daily_bars(self.connection, "RTS", date(2026, 9, 16), date(2026, 9, 16))
        mark_days(self.connection)
        self.connection.execute("DELETE FROM Minute")
        with self.assertRaises(self.daily.DataError):
            self.daily.build_daily_bars(self.connection, "RTS", date(2026, 9, 16), date(2026, 9, 16))

    def test_missing_selected_contract_is_not_replaced_by_farther_one(self) -> None:
        """Отсутствие выбранной серии в LoadedDays не разрешает подменить её дальней."""
        add_minute(self.connection, "2026-09-17 10:00:00", "RIZ6")
        mark_days(self.connection)
        self.connection.execute("UPDATE LoadedDays SET SECID='RIH7' WHERE SECID='RIZ6' AND TRADEDATE='2026-09-16'")
        with self.assertRaises(self.daily.DataError):
            self.daily.build_daily_bars(self.connection, "RTS", date(2026, 9, 17), date(2026, 9, 17))

    def test_uncovered_and_unclosed_dates_rejected(self) -> None:
        """Явно заданная дата без предыдущего дня, без следующего дня или до 21:00 недопустима."""
        mark_days(self.connection)
        for day in (date(2026, 9, 15), date(2026, 9, 20)):
            with self.subTest(day=day), self.assertRaises(self.daily.DataError):
                self.daily.build_daily_bars(self.connection, "RTS", day, day)
        now = datetime(2026, 9, 16, 20, 59, tzinfo=timezone(timedelta(hours=3)))
        with self.assertRaises(self.daily.DataError):
            self.daily.build_daily_bars(self.connection, "RTS", date(2026, 9, 16), date(2026, 9, 16), now=now)

    def test_asset_metadata_is_checked(self) -> None:
        """Минутная база RTS не может использоваться для построения ряда MIX."""
        mark_days(self.connection)
        with self.assertRaises(self.daily.DataError):
            self.daily.build_daily_bars(self.connection, "MIX")

    def test_save_is_repeatable_and_rebuilds_changed_period(self) -> None:
        """Повтор не дублирует бары, пересчёт обновляет цены, пустой интервал удаляет старую свечу."""
        add_minute(self.connection, "2026-09-16 10:00:00")
        with closing(sqlite3.connect(":memory:")) as target:
            result = self.build()
            self.daily.save_daily_bars(target, "RTS", result)
            self.daily.save_daily_bars(target, "RTS", result)
            self.assertEqual(target.execute("SELECT COUNT(*) FROM Daily").fetchone(), (1,))
            self.connection.execute("UPDATE Minute SET CLOSE=101")
            self.daily.save_daily_bars(target, "RTS", self.build())
            self.assertEqual(target.execute("SELECT CLOSE, BODY FROM Daily").fetchone(), (101, 1))
            self.connection.execute("DELETE FROM Minute")
            self.daily.save_daily_bars(target, "RTS", self.build())
            self.assertEqual(target.execute("SELECT COUNT(*) FROM Daily").fetchone(), (0,))

    def test_save_error_rolls_back_and_wrong_asset_rejected(self) -> None:
        """Ошибка вставки сохраняет прежний бар целиком; чужая выходная база не изменяется."""
        add_minute(self.connection, "2026-09-16 10:00:00")
        with closing(sqlite3.connect(":memory:")) as target:
            result = self.build()
            self.daily.save_daily_bars(target, "RTS", result)
            before = target.execute("SELECT * FROM Daily").fetchall()
            target.execute("CREATE TRIGGER reject_daily BEFORE INSERT ON Daily BEGIN SELECT RAISE(ABORT, 'Отказ'); END")
            target.commit()
            with self.assertRaises(sqlite3.IntegrityError):
                self.daily.save_daily_bars(target, "RTS", result)
            self.assertEqual(target.execute("SELECT * FROM Daily").fetchall(), before)
            with self.assertRaises(self.daily.DataError):
                self.daily.save_daily_bars(target, "MIX", result)
            self.assertEqual(target.execute("SELECT * FROM Daily").fetchall(), before)

    def test_cli_builds_both_assets_without_modifying_sources(self) -> None:
        """Команда создаёт RTS/MIX, сохраняет исходные файлы и успешно повторяет расчёт."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            root = Path(temporary)
            input_dir, output_dir = root / "m1", root / "d1"
            input_dir.mkdir()
            snapshots = {}
            for asset, secid in (("RTS", "RIU6"), ("MIX", "MXU6")):
                path = input_dir / f"{asset}_m1_two_ticker.db"
                with closing(sqlite3.connect(path)) as connection:
                    prepare_source(connection, asset)
                    add_minute(connection, "2026-09-16 10:00:00", secid)
                    mark_days(connection)
                snapshots[path] = path.read_bytes()
            args = ["--input-dir", str(input_dir), "--output-dir", str(output_dir),
                    "--start", "2026-09-16", "--end", "2026-09-16"]
            for _ in range(2):
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertEqual(self.daily.main(args), 0)
            for asset in ("RTS", "MIX"):
                with closing(sqlite3.connect(output_dir / f"{asset}_d1.db")) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*), MIN(BODY) FROM Daily").fetchone(), (1, 2))
            for path, snapshot in snapshots.items():
                self.assertEqual(path.read_bytes(), snapshot)

    def test_cli_missing_source_does_not_create_empty_databases(self) -> None:
        """При отсутствующих минутных файлах возвращается код 1 и выходные базы не создаются."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            output = Path(temporary) / "d1"
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                result = self.daily.main(["--input-dir", temporary, "--output-dir", str(output)])
            self.assertEqual(result, 1)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
