"""Проверки выбора контрактов, пагинации и докачки минутных свечей разных активов.

Тесты используют искусственные ответы ISS и временные SQLite-базы внутри tests.
Сеть и рабочие базы C:\\data_quote не используются.

Запуск из корня проекта:
    .\\.venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import importlib
import io
import json
import logging
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "MOEX_ISS_API_quote_downloader_m1_two_ticker"))
source = importlib.import_module("update_futures_m1_two_ticker")
DAY = date(2026, 9, 1)


def table(name: str, rows: list[dict], columns: list[str] | None = None) -> dict:
    """Возвращает таблицу ISS с именем name, строками rows и необязательными columns."""
    columns = columns or list(rows[0])
    return {name: {"columns": columns, "data": [[row[key] for key in columns] for row in rows]}}


def candle(begin: str = "2026-09-01 21:00:00", volume: int = 3) -> dict:
    """Возвращает корректную минутную свечу с началом begin и объёмом volume."""
    return {"begin": begin, "end": begin[:-2] + "59", "open": 100,
            "low": 99, "high": 103, "close": 102, "volume": volume, "value": 500}


def history(secid: str, asset: str = "RTS", numtrades: int = 1) -> dict:
    """Возвращает строку дневной истории для secid, актива asset и числа сделок numtrades."""
    return {"SECID": secid, "BOARDID": "RFUD", "ASSETCODE": asset,
            "TRADEDATE": DAY.isoformat(), "NUMTRADES": numtrades}


class FixtureClient:
    """Заменяет ISS заранее заданными историями, описаниями и минутными свечами."""

    def __init__(self, asset: str = "RTS", empty_second: bool = False):
        """Готовит три контракта актива asset; empty_second убирает сделки второго контракта."""
        prefix = {"RTS": "RI", "MIX": "MX", "BR": "BR", "SI": "Si", "GOLD": "GD"}[asset]
        self.secids = [prefix + code for code in ("U6", "Z6", "H7")]
        self.rows = [history(secid, asset, 0 if empty_second and index == 1 else 1)
                     for index, secid in enumerate(self.secids)]
        self.infos = {secid: {"SECID": secid, "SHORTNAME": secid,
                             "LSTTRADE": expiry}
                      for secid, expiry in zip(self.secids, ("2026-09-17", "2026-12-17", "2027-03-18"))}
        self.quotes = {secid: ([] if empty_second and index == 1 else [candle()])
                       for index, secid in enumerate(self.secids)}
        self.calls = []

    def history(self, asset: str, day: date) -> list[dict]:
        """Возвращает историю asset за day и запоминает факт запроса."""
        self.calls.append(("history", asset, day))
        return self.rows

    def contract(self, asset: str, secid: str) -> dict:
        """Возвращает описание secid актива asset и запоминает факт запроса."""
        self.calls.append(("contract", asset, secid))
        return self.infos[secid]

    def candles(self, secid: str, day: date) -> list[dict]:
        """Возвращает свечи secid за day либо поднимает заданное в quotes исключение."""
        self.calls.append(("candles", secid, day))
        value = self.quotes[secid]
        if isinstance(value, Exception):
            raise value
        return value


class SelectionTests(unittest.TestCase):
    """Проверяет исторический выбор пары и качество минутных данных."""

    def test_expiry_day_included_and_archive_secid_preserved(self):
        """День экспирации включается, архивный суффикс сохраняется, дальняя серия исключается."""
        client = FixtureClient()
        client.infos["RIU6"]["LSTTRADE"] = DAY.isoformat()
        info = client.infos.pop("RIU6")
        info["SECID"] = "RIU6_2026"
        client.infos["RIU6_2026"] = info
        client.rows[0]["SECID"] = "RIU6_2026"
        pair = source.choose_pair(DAY, client.rows, client.infos)
        self.assertEqual([row["SECID"] for row in pair], ["RIU6_2026", "RIZ6"])
        pair = source.choose_pair(date(2026, 9, 2), client.rows, client.infos)
        self.assertEqual([row["SECID"] for row in pair], ["RIZ6", "RIH7"])

    def test_fewer_than_two_contracts_is_failure(self):
        """Единственный контракт не превращается в успешную пару."""
        client = FixtureClient()
        with self.assertRaises(source.DataError):
            source.choose_pair(DAY, client.rows[:1], client.infos)

    def test_invalid_candles_are_rejected(self):
        """NULL, NaN, дробный объём, неверные OHLC и время не допускаются в БД."""
        invalid = ({"open": None}, {"close": float("nan")}, {"volume": 1.5},
                   {"volume": True}, {"high": 98}, {"low": 104}, {"value": -1},
                   {"begin": "2026-09-01 21:00:01"}, {"end": "2026-09-01 22:00:00"},
                   {"begin": "2026-09-02 21:00:00"})
        for fields in invalid:
            with self.subTest(fields=fields), self.assertRaises(source.DataError):
                source.normalize_candles(DAY, [candle() | fields])

    def test_duplicates_are_not_silently_removed(self):
        """Повтор минуты означает ошибку ответа, а не незаметное удаление дубля."""
        with self.assertRaises(source.DataError):
            source.normalize_candles(DAY, [candle(), candle()])

    def test_gap_in_minutes_is_allowed_and_not_filled(self):
        """Разрыв между сделками остаётся разрывом без искусственных свечей."""
        rows = source.normalize_candles(DAY, [candle(), candle("2026-09-01 21:02:00")])
        self.assertEqual(len(rows), 2)


class PaginationTests(unittest.TestCase):
    """Проверяет все страницы истории и свечей, включая ошибки ответа ISS."""

    def test_short_page_is_not_end_of_candles(self):
        """Свечи читаются до пустой страницы даже при маленьком размере страницы."""
        pages = [table("candles", [candle()]),
                 table("candles", [candle("2026-09-01 21:01:00")]),
                 table("candles", [], list(candle()))]
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", side_effect=pages) as get_json:
            rows = client.candles("RIU6_2026", DAY)
        self.assertEqual(len(rows), 2)
        self.assertEqual([call.args[1]["start"] for call in get_json.call_args_list], [0, 1, 2])
        self.assertIn("RIU6_2026/candles.json", get_json.call_args.args[0])
        self.assertEqual(get_json.call_args.args[1]["interval"], 1)
        self.assertEqual(get_json.call_args.args[1]["till"], "2026-09-01 23:59:59")

    def test_repeated_candle_page_is_failure(self):
        """Повтор страницы обнаруживается и останавливает пагинацию."""
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", return_value=table("candles", [candle()])):
            with self.assertRaises(source.ISSRequestError):
                client.candles("RIU6", DAY)

    def test_missing_columns_on_empty_page_are_failure(self):
        """Пустая таблица без обязательных полей не считается отсутствием сделок."""
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", return_value=table("candles", [], ["begin"])):
            with self.assertRaises(source.ISSRequestError):
                client.candles("RIU6", DAY)

    def test_history_filters_spreads_after_all_pages(self):
        """Спред с NULL ASSETCODE исключается после чтения составляющих на другой странице."""
        spread = history("RIU6RIZ6") | {"ASSETCODE": None}
        pages = [table("history", [spread]), table("history", [history("RIU6"), history("RIZ6")]),
                 table("history", [], list(spread))]
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", side_effect=pages):
            rows = client.history("RTS", DAY)
        self.assertEqual([row["SECID"] for row in rows], ["RIU6", "RIZ6"])

    def test_truncated_history_cursor_is_failure(self):
        """Пустая страница до TOTAL из курсора означает неполученную историю."""
        first = table("history", [history("RIU6")])
        first.update(table("history.cursor", [{"INDEX": 0, "TOTAL": 2, "PAGESIZE": 1}]))
        second = table("history", [], list(history("RIU6")))
        second.update(table("history.cursor", [{"INDEX": 1, "TOTAL": 2, "PAGESIZE": 1}]))
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", side_effect=[first, second]):
            with self.assertRaises(source.ISSRequestError):
                client.history("RTS", DAY)

    def test_unknown_null_asset_is_failure(self):
        """Неизвестная строка с NULL ASSETCODE не маскируется под пустой выходной."""
        row = history("BROKEN") | {"ASSETCODE": None}
        client = source.ISSClient(pause=0)
        with patch.object(client, "get_json", side_effect=[table("history", [row]),
                                                          table("history", [], list(row))]):
            with self.assertRaises(source.ISSRequestError):
                client.history("RTS", DAY)

    def test_asset_case_in_history_and_description_does_not_drop_contract(self):
        """Код Si в ответах ISS должен соответствовать активу SI командной строки без изменения SECID."""
        row = history("SiU6", "Si")
        client = source.ISSClient(pause=0)
        pages = [table("history", [row]), table("history", [], list(row))]
        with patch.object(client, "get_json", side_effect=pages) as get_json:
            self.assertEqual(client.history("SI", DAY), [row])
            self.assertEqual(get_json.call_args_list[0].args[1]["assetcode"], "SI")
        description = table("description", [
            {"name": "ASSETCODE", "value": "Si"},
            {"name": "SHORTNAME", "value": "Si-9.26"},
            {"name": "LSTTRADE", "value": "2026-09-17"},
        ])
        with patch.object(client, "get_json", return_value=description):
            self.assertEqual(client.contract("SI", "SiU6")["SECID"], "SiU6")


class DatabaseTests(unittest.TestCase):
    """Проверяет сохранение пары, повторные запуски и восстановление пропусков."""

    def setUp(self):
        """Открывает изолированную SQLite-базу и готовит клиента с тестовыми свечами."""
        self.connection = sqlite3.connect(":memory:")
        self.addCleanup(self.connection.close)
        source.initialize_database(self.connection, "RTS")
        self.client = FixtureClient()

    def load(self, **kwargs):
        """Запускает докачку единственной даты с дополнительными параметрами kwargs; возвращает счётчики."""
        return source.download_missing(self.connection, self.client, "RTS", DAY, DAY, **kwargs)

    def test_nearest_two_saved_and_repeat_makes_no_network_calls(self):
        """Первый запуск сохраняет две серии, повторный не вызывает ISS."""
        result = self.load()
        self.assertEqual((result.written_days, result.candles), (1, 2))
        self.assertEqual(self.connection.execute("SELECT DISTINCT SECID FROM Minute ORDER BY SECID").fetchall(),
                         [("RIU6",), ("RIZ6",)])
        self.client.calls.clear()
        self.assertEqual(self.load().existing_days, 1)
        self.assertEqual(self.client.calls, [])

    def test_deleted_minute_and_edited_price_are_repaired(self):
        """Докачка обнаруживает удалённую строку и изменение цены даже при прежнем числе свечей."""
        self.load()
        self.connection.execute("DELETE FROM Minute WHERE SECID='RIZ6'")
        self.connection.commit()
        self.assertEqual(self.load().written_days, 1)
        self.connection.execute("UPDATE Minute SET CLOSE=101 WHERE SECID='RIZ6'")
        self.connection.commit()
        self.assertEqual(self.load().written_days, 1)
        self.assertEqual(self.connection.execute("SELECT CLOSE FROM Minute WHERE SECID='RIZ6'").fetchone(), (102,))

    def test_second_contract_network_error_preserves_previous_pair(self):
        """Отказ второй серии при перепроверке сохраняет прежние свечи и отметки целиком."""
        self.load()
        before = self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall()
        markers = self.connection.execute("SELECT * FROM LoadedDays ORDER BY SECID").fetchall()
        self.client.quotes["RIU6"] = [candle(volume=77)]
        self.client.quotes["RIZ6"] = source.ISSRequestError("Тестовый отказ")
        with self.assertRaises(source.ISSRequestError):
            self.load(recheck=True)
        self.assertEqual(self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall(), before)
        self.assertEqual(self.connection.execute("SELECT * FROM LoadedDays ORDER BY SECID").fetchall(), markers)

    def test_empty_recheck_preserves_previous_nonempty_pair(self):
        """Неожиданно пустая серия при перепроверке не удаляет ранее сохранённые реальные свечи."""
        self.load()
        before = self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall()
        self.client.quotes["RIZ6"] = []
        with self.assertLogs(source.LOGGER, level="ERROR"):
            self.assertEqual(self.load(recheck=True).failed_days, 1)
        self.assertEqual(self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall(), before)

    def test_database_insert_error_rolls_back_pair(self):
        """Отказ вставки второй серии откатывает удаление старых свечей и новых отметок."""
        self.load()
        before = self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall()
        self.connection.execute("CREATE TRIGGER reject_quote BEFORE INSERT ON Minute "
                                "WHEN NEW.SECID='RIZ6' BEGIN SELECT RAISE(ABORT, 'Отказ'); END")
        self.connection.commit()
        with self.assertRaises(sqlite3.IntegrityError):
            self.load(recheck=True)
        self.assertEqual(self.connection.execute("SELECT * FROM Minute ORDER BY SECID").fetchall(), before)

    def test_empty_series_is_saved_without_artificial_candles_and_retried(self):
        """Пустая серия получает отметку без выдуманной свечи и перепроверяется при следующем запуске."""
        self.client = FixtureClient(empty_second=True)
        result = self.load()
        self.assertEqual((result.written_days, result.candles), (1, 1))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM LoadedDays").fetchone(), (2,))
        self.client.calls.clear()
        self.assertEqual(self.load().written_days, 1)
        self.assertTrue(self.client.calls)

    def test_daily_trades_do_not_prove_calendar_day_has_candles(self):
        """NUMTRADES торгового дня не запрещает пустой результат за календарный день."""
        self.client.quotes["RIZ6"] = []
        self.assertEqual(self.load().failed_days, 0)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM Minute").fetchone(), (1,))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM LoadedDays").fetchone(), (2,))

    def test_other_asset_database_is_rejected(self):
        """База RTS не разрешает записать данные MIX."""
        with self.assertRaises(source.DataError):
            source.initialize_database(self.connection, "MIX")

    def test_empty_weekday_retried_but_confirmed_weekend_cached(self):
        """Пустой будний день запрашивается снова; выходной кешируется только после обоих запросов candles."""
        self.client.quotes = {secid: [] for secid in self.client.secids}
        self.load()
        self.client.calls.clear()
        self.load()
        self.assertEqual(sum(call[0] == "candles" for call in self.client.calls), 2)
        saturday = date(2026, 9, 5)
        source.download_missing(self.connection, self.client, "RTS", saturday, saturday)
        self.client.calls.clear()
        source.download_missing(self.connection, self.client, "RTS", saturday, saturday)
        self.assertEqual(self.client.calls, [])
        source.download_missing(self.connection, self.client, "RTS", saturday, saturday,
                                recheck_empty_weekends=True)
        self.assertEqual(sum(call[0] == "candles" for call in self.client.calls), 2)

    def test_weekend_candles_loaded_when_daily_history_has_no_weekend_row(self):
        """Субботние свечи загружаются по составу контрактов пятницы даже при пустой субботней history."""
        friday, saturday = date(2026, 9, 4), date(2026, 9, 5)
        original_history = self.client.history

        def calendar_history(asset: str, day: date) -> list[dict]:
            """Возвращает историю asset только за friday; day выходного остаётся пустым."""
            self.client.calls.append(("history", asset, day))
            return self.client.rows if day == friday else []

        self.client.history = calendar_history
        self.client.quotes = {secid: [candle("2026-09-05 10:00:00")] for secid in self.client.secids}
        result = source.download_missing(self.connection, self.client, "RTS", saturday, saturday)
        self.assertEqual((result.written_days, result.candles), (1, 2))
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM EmptyWeekends").fetchone(), (0,))
        self.assertEqual(self.connection.execute("SELECT DISTINCT TRADEDATE FROM Minute").fetchall(), [(str(saturday),)])
        self.client.history = original_history

    def test_missing_contract_history_never_confirms_empty_weekend(self):
        """Отсутствие истории для выбора пары не считается подтверждением пустого выходного."""
        self.client.rows = []
        saturday = date(2026, 9, 5)
        result = source.download_missing(self.connection, self.client, "RTS", saturday, saturday)
        self.assertEqual(result.failed_days, 1)
        self.assertEqual(self.connection.execute("SELECT COUNT(*) FROM EmptyWeekends").fetchone(), (0,))


class CommandLineTests(unittest.TestCase):
    """Проверяет параметры, московскую дату, журналы и запуск произвольного набора активов."""

    def test_assets_accept_additional_codes_and_ignore_case_and_duplicates(self):
        """Новые активы разрешены, повторные коды и разный регистр не создают дубли баз."""
        args = source.parse_arguments(["--assets", "rts", "MiX", "BR", "Si", "gold", "SI", "br"])
        self.assertEqual(args.assets, ["RTS", "MIX", "BR", "SI", "GOLD"])
        self.assertEqual(source.parse_arguments([]).assets, ["RTS", "MIX"])

    def test_asset_codes_reject_path_characters_and_contract_names(self):
        """Вместо кода актива нельзя передать путь, пробелы или название конкретного контракта с точкой."""
        for value in ("../BR", "BR/other", "BR\\other", "Si-12.26", "Si Z6", "", "_", "BR:"):
            with self.subTest(value=value), patch("sys.stderr", new_callable=io.StringIO):
                with self.assertRaises(SystemExit):
                    source.parse_arguments(["--assets", value])

    def test_main_saves_separate_databases_for_additional_assets(self):
        """BR, Si и GOLD проходят полный цикл загрузки пары и получают собственные базы с метаданными."""
        assets = ("BR", "SI", "GOLD")
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            args = ["--data-dir", temporary, "--assets", "BR", "Si", "GOLD",
                    "--start", DAY.isoformat(), "--end", DAY.isoformat()]
            with patch.object(source, "ISSClient", side_effect=[FixtureClient(asset) for asset in assets]), \
                    patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(source.main(args), 0)
            self.assertEqual({path.name for path in Path(temporary).iterdir()},
                             {f"{asset}_m1_two_ticker.db" for asset in assets})
            for asset in assets:
                with closing(sqlite3.connect(Path(temporary) / f"{asset}_m1_two_ticker.db")) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM Minute").fetchone(), (2,))
                    self.assertEqual(connection.execute("SELECT VALUE FROM Metadata WHERE KEY='ASSETCODE'").fetchone(),
                                     (asset,))

    def test_moscow_yesterday_and_current_day_rejected(self):
        """Граница даты учитывает UTC+3, а явная текущая дата запрещена."""
        self.assertEqual(source.yesterday_moscow(datetime(2026, 10, 20, 22, tzinfo=timezone.utc)),
                         date(2026, 10, 20))
        with patch.object(source, "yesterday_moscow", return_value=date(2026, 10, 20)), \
                patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit):
                source.parse_arguments(["--end", "2026-10-21"])
            with self.assertRaises(SystemExit):
                source.parse_arguments(["--timeout", "nan"])

    def test_main_saves_both_databases_and_repeat_skips_them(self):
        """Одна команда создаёт две базы и при повторном запуске не обращается в сеть."""
        with tempfile.TemporaryDirectory(dir=ROOT / "tests") as temporary:
            clients = [FixtureClient("RTS"), FixtureClient("MIX")]
            args = ["--data-dir", temporary, "--start", DAY.isoformat(), "--end", DAY.isoformat()]
            with patch.object(source, "ISSClient", side_effect=clients), \
                    patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(source.main(args), 0)
            for asset in ("RTS", "MIX"):
                with closing(sqlite3.connect(Path(temporary) / f"{asset}_m1_two_ticker.db")) as connection:
                    self.assertEqual(connection.execute("SELECT COUNT(*) FROM Minute").fetchone(), (2,))
            for client in clients:
                client.calls.clear()
            with patch.object(source, "ISSClient", side_effect=clients), \
                    patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(source.main(args), 0)
            self.assertTrue(all(not client.calls for client in clients))

    def test_logging_overwrites_and_truncates_without_archives(self):
        """Журнал перезаписывается при запуске и превышении лимита без архивных копий."""
        log_root = ROOT / "logs"
        log_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=log_root) as temporary:
            path = Path(temporary) / "check.log"
            logger = logging.getLogger("m1_test_log")
            logger.setLevel(logging.INFO)
            handler = source.OverwriteFileHandler(path, max_bytes=90)
            logger.addHandler(handler)
            try:
                logger.info("первое сообщение " * 3)
                logger.info("последнее сообщение " * 3)
            finally:
                logger.removeHandler(handler)
                handler.close()
            self.assertLessEqual(path.stat().st_size, 90)
            self.assertNotIn("первое", path.read_text(encoding="utf-8"))
            handler = source.OverwriteFileHandler(path, max_bytes=90)
            handler.close()
            self.assertEqual(path.read_text(encoding="utf-8"), "")
            self.assertEqual([item.name for item in Path(temporary).iterdir()], ["check.log"])


if __name__ == "__main__":
    unittest.main()
