r"""Загрузка минутных свечей двух ближайших фьючерсов каждого актива RTS и MIX.

За каждую календарную дату читаются все страницы дневной истории MOEX ISS.
Если истории нет, состав контрактов ищется в предыдущие 14 календарных дней,
затем в следующие 14 завершённых дней. Пустая history не означает пустые свечи:
сессии выходных могут относиться к следующему торговому дню.
На RFUD исключаются календарные спреды; реальные SHORTNAME и LSTTRADE берутся
из описаний контрактов. Выбираются две серии с минимальной LSTTRADE >= даты.
Их минутные свечи запрашиваются с interval=1 с 00:00:00 до 23:59:59 по Москве.
Архивные SECID сохраняются целиком. Свечи без сделок не создаются искусственно.
Две серии и контрольные суммы их свечей записываются одной транзакцией SQLite.

Базы: C:\data_quote\m1_two_ticker\RTS_m1_two_ticker.db и MIX_m1_two_ticker.db.
По умолчанию период начинается 2026-09-01 и заканчивается вчера по Москве.
Повторный запуск проверяет весь период: целые дни пропускаются, пропуски
и изменённые данные загружаются вновь. Пустые выходные кешируются только после
успешных пустых ответов candles обеих серий; остальные пустые выборки
перепроверяются при каждом запуске. --recheck повторяет загрузку всех дат,
--recheck-empty-weekends повторяет только подтверждённые пустые выходные.
Журналы находятся только в logs в корне проекта, перезаписываются при запуске
и достижении 5 МиБ без архивов. Используется только стандартная библиотека.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --start 2020-01-01
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --start 2026-09-01 --end 2026-09-30
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --assets RTS --timeout 30 --retries 5
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --data-dir C:\data_quote\m1_two_ticker --recheck
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --recheck-empty-weekends
    .\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --help
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import math
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATA_DIR = Path(r"C:\data_quote\m1_two_ticker")
DEFAULT_START = date(2026, 9, 1)
LOG_DIR = ROOT / "logs"
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")
ISS_BASE = "https://iss.moex.com/iss"
HISTORY_PATH = "/history/engines/futures/markets/forts/securities.json"
HISTORY_COLUMNS = ("BOARDID", "TRADEDATE", "SECID", "ASSETCODE", "NUMTRADES")
CANDLE_COLUMNS = ("begin", "end", "open", "low", "high", "close", "volume", "value")
MINUTE_COLUMNS = ("TRADEDATE", "SECID", "BEGIN", "END", "OPEN", "LOW", "HIGH", "CLOSE",
                  "VOLUME", "VALUE", "SHORTNAME", "LSTTRADE")
LOGGER = logging.getLogger("moex_m1_two_ticker")


class DataError(RuntimeError):
    """Обозначает неполные данные, которые нельзя считать загруженным днём."""


class ISSRequestError(RuntimeError):
    """Обозначает сетевой отказ либо нарушение структуры ответа ISS."""


@dataclass
class DownloadResult:
    """Хранит число готовых, записанных, пустых и ошибочных дней и записанных свечей."""

    existing_days: int = 0
    written_days: int = 0
    empty_days: int = 0
    cached_empty_days: int = 0
    failed_days: int = 0
    candles: int = 0


def yesterday_moscow(now: datetime | None = None) -> date:
    """Возвращает вчера по Москве; now задаёт необязательный момент с часовым поясом."""
    instant = now if now is not None else datetime.now(MOSCOW)
    return instant.astimezone(MOSCOW).date() - timedelta(days=1)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Проверяет параметры argv или командной строки и возвращает настройки загрузки."""
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START,
                        help="Первая календарная дата включительно (2026-09-01).")
    parser.add_argument("--end", type=date.fromisoformat, default=yesterday_moscow(),
                        help="Последняя дата включительно (вчера по Москве).")
    parser.add_argument("--assets", nargs="+", choices=("RTS", "MIX"), default=["RTS", "MIX"],
                        help="Базовые активы ISS (по умолчанию RTS MIX).")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR,
                        help=f"Папка двух SQLite-баз ({DEFAULT_DATA_DIR}).")
    parser.add_argument("--timeout", type=float, default=20, help="Тайм-аут запроса в секундах (20).")
    parser.add_argument("--retries", type=int, default=4, help="Число попыток запроса, включая первую (4).")
    parser.add_argument("--pause", type=float, default=0.05, help="Пауза после успешного запроса, секунд (0.05).")
    parser.add_argument("--recheck", action="store_true", help="Повторно загрузить все даты периода.")
    parser.add_argument("--recheck-empty-weekends", action="store_true",
                        help="Повторить запросы подтверждённых пустых выходных.")
    args = parser.parse_args(argv)
    if args.start > args.end or args.end > yesterday_moscow():
        parser.error("Требуется --start <= --end <= вчерашней московской даты.")
    if not math.isfinite(args.timeout) or args.timeout <= 0 or args.retries < 1:
        parser.error("--timeout должен быть положительным конечным числом, --retries — целым от 1.")
    if not math.isfinite(args.pause) or args.pause < 0:
        parser.error("--pause должен быть конечным неотрицательным числом.")
    args.assets = list(dict.fromkeys(args.assets))
    return args


class OverwriteFileHandler(logging.FileHandler):
    """Перезаписывает журнал при открытии и достижении лимита без резервных копий."""

    def __init__(self, filename: Path, max_bytes: int = 5 * 1024 * 1024):
        """Открывает filename в UTF-8; max_bytes ограничивает размер единственного файла."""
        self.max_bytes = max_bytes
        super().__init__(filename, mode="w", encoding="utf-8")

    def emit(self, record: logging.LogRecord) -> None:
        """Записывает record, при переполнении очищая файл; слишком длинное сообщение обрезается."""
        try:
            message = (self.format(record) + "\n").encode("utf-8")
            if len(message) > self.max_bytes:
                message = message[:self.max_bytes].decode("utf-8", errors="ignore").encode("utf-8")
            self.stream.flush()
            if self.stream.seek(0, 2) + len(message) > self.max_bytes:
                self.stream.seek(0)
                self.stream.truncate()
            # Запись через buffer исключает расширение LF до CRLF в Windows.
            self.stream.buffer.write(message)
            self.stream.buffer.flush()
        except Exception:
            self.handleError(record)


def close_logging() -> None:
    """Закрывает обработчики журнала загрузчика и освобождает файловые дескрипторы."""
    for handler in LOGGER.handlers[:]:
        LOGGER.removeHandler(handler)
        handler.close()


def configure_logging() -> None:
    """Перезаписывает общий журнал и журнал ошибок исключительно в logs корня проекта."""
    close_logging()
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    LOGGER.setLevel(logging.INFO)
    LOGGER.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    LOGGER.addHandler(console)
    for name, level in (("m1_two_ticker.log", logging.INFO),
                        ("m1_two_ticker_errors.log", logging.WARNING)):
        handler = OverwriteFileHandler(LOG_DIR / name)
        handler.setLevel(level)
        handler.setFormatter(formatter)
        LOGGER.addHandler(handler)


def table_rows(payload: dict[str, Any], name: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
    """Преобразует таблицу name из payload в строки, проверяя столбцы required даже при пустом data."""
    table = payload.get(name)
    if not isinstance(table, dict):
        raise ISSRequestError(f"Отсутствует таблица {name}.")
    columns, data = table.get("columns"), table.get("data")
    if (not isinstance(columns, list) or not columns
            or not all(isinstance(column, str) for column in columns)
            or len(set(columns)) != len(columns) or not set(required).issubset(columns)
            or not isinstance(data, list)):
        raise ISSRequestError(f"Неверные столбцы или данные таблицы {name}.")
    if any(not isinstance(row, list) or len(row) != len(columns) for row in data):
        raise ISSRequestError(f"Неверная строка таблицы {name}.")
    return [dict(zip(columns, row)) for row in data]


class ISSClient:
    """Читает MOEX ISS с повторами запросов и проверкой всех страниц."""

    def __init__(self, timeout: float = 20, retries: int = 4, pause: float = 0.05):
        """Сохраняет timeout в секундах, число попыток retries и паузу pause после запроса."""
        self.timeout, self.retries, self.pause = timeout, retries, pause

    def get_json(self, path: str, parameters: dict[str, Any]) -> dict[str, Any]:
        """Получает JSON по path с parameters; после исчерпания попыток выдаёт ISSRequestError с URL."""
        url = ISS_BASE + path + "?" + urlencode({"iss.meta": "off", **parameters})
        request = Request(url, headers={"User-Agent": "MOEX-m1-two-ticker/1.0", "Accept": "application/json"})
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    payload = json.loads(response.read().decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("Корень JSON не является объектом.")
                if self.pause:
                    time.sleep(self.pause)
                return payload
            except (URLError, OSError, HTTPException, ValueError) as error:
                permanent = isinstance(error, HTTPError) and 400 <= error.code < 500 and error.code not in (408, 429)
                if permanent or attempt + 1 == self.retries:
                    raise ISSRequestError(f"Не удалось получить {url}: {error}") from error
                delay = min(2 ** attempt, 30)
                LOGGER.warning("Запрос %s: попытка %s/%s не удалась: %s; повтор через %s с.",
                               url, attempt + 1, self.retries, error, delay)
                time.sleep(delay)
        raise ISSRequestError(f"Запрос не выполнен: {url}")

    def all_rows(self, path: str, parameters: dict[str, Any], name: str,
                 required: tuple[str, ...]) -> list[dict[str, Any]]:
        """Возвращает все строки name по path с parameters и required; выявляет повтор и обрыв страниц."""
        result, previous, start, expected_total = [], None, 0, None
        while True:
            page_parameters = {**parameters, "start": start}
            payload = self.get_json(path, page_parameters)
            try:
                rows = table_rows(payload, name, required)
                cursor_name = name + ".cursor"
                if cursor_name in payload:
                    cursors = table_rows(payload, cursor_name, ("INDEX", "TOTAL", "PAGESIZE"))
                    if len(cursors) != 1:
                        raise ISSRequestError(f"Неверный курсор {cursor_name}.")
                    cursor = cursors[0]
                    if any(isinstance(cursor[key], bool) or not isinstance(cursor[key], int)
                           or cursor[key] < 0 for key in ("INDEX", "TOTAL", "PAGESIZE")):
                        raise ISSRequestError(f"Неверные числа в {cursor_name}.")
                    if cursor["INDEX"] != start or (expected_total is not None and cursor["TOTAL"] != expected_total):
                        raise ISSRequestError(f"Изменился курсор {cursor_name}.")
                    expected_total = cursor["TOTAL"]
                if not rows:
                    if expected_total is not None and start < expected_total:
                        raise ISSRequestError("Пустая страница до конца таблицы.")
                    return result
                if rows == previous:
                    raise ISSRequestError("ISS повторил предыдущую страницу.")
                result.extend(rows)
                start += len(rows)
                if expected_total is not None:
                    if start > expected_total:
                        raise ISSRequestError("Число строк превышает TOTAL курсора.")
                    if start == expected_total:
                        return result
                previous = rows
            except ISSRequestError as error:
                url = ISS_BASE + path + "?" + urlencode(page_parameters)
                raise ISSRequestError(f"{error} URL: {url}; ответ: {json.dumps(payload, ensure_ascii=False)}") from error

    def history(self, asset: str, day: date) -> list[dict[str, Any]]:
        """Возвращает отдельные контракты asset на RFUD за day, исключая спреды после чтения всех страниц."""
        rows = self.all_rows(HISTORY_PATH, {
            "date": day.isoformat(), "assetcode": asset, "iss.only": "history,history.cursor",
            "history.columns": ",".join(HISTORY_COLUMNS),
        }, "history", HISTORY_COLUMNS)
        for row in rows:
            if (any(not isinstance(row[key], str) or not row[key].strip()
                    for key in ("SECID", "BOARDID", "TRADEDATE")) or row["TRADEDATE"] != day.isoformat()):
                raise ISSRequestError(f"{asset} {day}: неверные идентификаторы истории: {row!r}")
        contract_assets = {row["SECID"]: row["ASSETCODE"] for row in rows
                           if row["BOARDID"] == "RFUD" and isinstance(row["ASSETCODE"], str) and row["ASSETCODE"]}
        filtered = []
        for row in rows:
            if row["BOARDID"] != "RFUD":
                continue
            secid = row["SECID"]
            spread = any(secid.startswith(first) and secid[len(first):] != first
                         and contract_assets.get(secid[len(first):]) == base
                         for first, base in contract_assets.items())
            if spread:
                LOGGER.info("%s %s: исключён календарный спред %s.", asset, day, secid)
                continue
            if not isinstance(row["ASSETCODE"], str) or not row["ASSETCODE"].strip():
                raise ISSRequestError(f"{asset} {day}: неверный ASSETCODE: {row!r}")
            if row["ASSETCODE"] == asset:
                trades = row["NUMTRADES"]
                if (isinstance(trades, bool) or not isinstance(trades, (int, float))
                        or not math.isfinite(trades) or trades < 0 or int(trades) != trades):
                    raise ISSRequestError(f"{asset} {day}: неверный NUMTRADES: {row!r}")
                filtered.append(row)
        return filtered

    def contract(self, asset: str, secid: str) -> dict[str, str]:
        """Получает реальные SHORTNAME и LSTTRADE полного secid и проверяет принадлежность asset."""
        payload = self.get_json(f"/securities/{quote(secid, safe='')}.json", {"iss.only": "description"})
        description = {row["name"]: row["value"] for row in table_rows(payload, "description", ("name", "value"))}
        if description.get("ASSETCODE") not in (None, asset):
            raise DataError(f"{secid}: другой базовый актив в описании.")
        shortname, expiry = description.get("SHORTNAME"), description.get("LSTTRADE")
        if not isinstance(shortname, str) or not shortname.strip():
            raise DataError(f"{secid}: отсутствует SHORTNAME.")
        try:
            expiry = date.fromisoformat(expiry).isoformat()
        except (TypeError, ValueError) as error:
            raise DataError(f"{secid}: отсутствует корректная LSTTRADE.") from error
        return {"SECID": secid, "SHORTNAME": shortname, "LSTTRADE": expiry}

    def candles(self, secid: str, day: date) -> list[dict[str, Any]]:
        """Возвращает все минутные свечи secid за календарную дату day по московскому времени."""
        path = f"/engines/futures/markets/forts/boards/RFUD/securities/{quote(secid, safe='')}/candles.json"
        return self.all_rows(path, {
            "from": f"{day} 00:00:00", "till": f"{day} 23:59:59", "interval": 1,
            "iss.only": "candles", "candles.columns": ",".join(CANDLE_COLUMNS),
        }, "candles", CANDLE_COLUMNS)


def choose_pair(day: date, history: list[dict[str, Any]],
                contracts: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    """Выбирает два ближайших на day контракта из history по descriptions contracts, включая день экспирации."""
    candidates, seen = [], set()
    for row in history:
        secid = row["SECID"]
        if secid in seen:
            raise DataError(f"{day}: повтор контракта {secid}.")
        seen.add(secid)
        try:
            info = contracts[secid]
            expiry = date.fromisoformat(info["LSTTRADE"])
            if not info["SHORTNAME"]:
                raise ValueError("Пустой SHORTNAME")
        except (KeyError, TypeError, ValueError) as error:
            raise DataError(f"{secid}: неверное описание контракта.") from error
        if expiry >= day:
            candidates.append({**row, **info})
    candidates.sort(key=lambda row: (row["LSTTRADE"], row["SECID"]))
    if len(candidates) < 2:
        raise DataError(f"{day}: найдено {len(candidates)} действующих контрактов, требуется два.")
    return candidates[:2]


def normalize_candles(day: date, candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Проверяет свечи candles за day и возвращает упорядоченные значения без заполнения временных разрывов."""
    normalized, seen = [], set()
    for raw in candles:
        try:
            row = {key: raw[key] for key in CANDLE_COLUMNS}
            begin, end = datetime.fromisoformat(row["begin"]), datetime.fromisoformat(row["end"])
            if (begin.tzinfo or end.tzinfo or begin.date() != day or begin.second or begin.microsecond
                    or not begin <= end < begin + timedelta(minutes=1) or end.microsecond):
                raise ValueError("Неверные границы минутной свечи.")
            row["begin"], row["end"] = begin.isoformat(sep=" "), end.isoformat(sep=" ")
            if row["begin"] in seen:
                raise ValueError("Повтор минутной свечи.")
            seen.add(row["begin"])
            for key in ("open", "low", "high", "close", "volume", "value"):
                value = row[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"Неверное числовое значение {key}.")
                row[key] = float(value)
            if row["volume"] < 0 or not row["volume"].is_integer() or row["value"] < 0:
                raise ValueError("Отрицательный объём/оборот либо дробный объём.")
            row["volume"] = int(row["volume"])
            if (row["low"] <= 0 or row["low"] > min(row["open"], row["close"])
                    or row["high"] < max(row["open"], row["close"])):
                raise ValueError("Нарушен порядок OHLC.")
            normalized.append(row)
        except (KeyError, TypeError, ValueError, OverflowError) as error:
            raise DataError(f"{day}: неверная минутная свеча: {error}; исходная строка: {raw!r}") from error
    return sorted(normalized, key=lambda row: row["begin"])


def initialize_database(connection: sqlite3.Connection, asset: str) -> None:
    """Создаёт таблицы в connection и проверяет схему, интервал, часовой пояс и владельца asset."""
    with connection:
        connection.execute("CREATE TABLE IF NOT EXISTS Metadata (KEY TEXT PRIMARY KEY, VALUE TEXT NOT NULL)")
        expected = {"ASSETCODE": asset, "INTERVAL": "1", "TIMEZONE": "Europe/Moscow", "SCHEMA_VERSION": "1"}
        stored = dict(connection.execute("SELECT KEY, VALUE FROM Metadata"))
        if stored and any(stored.get(key) != value for key, value in expected.items()):
            raise DataError(f"Несовместимая БД: {stored!r}; ожидается {expected!r}.")
        connection.execute('''CREATE TABLE IF NOT EXISTS Minute (
            TRADEDATE TEXT NOT NULL, SECID TEXT NOT NULL, BEGIN TEXT NOT NULL, END TEXT NOT NULL,
            OPEN REAL NOT NULL, LOW REAL NOT NULL, HIGH REAL NOT NULL, CLOSE REAL NOT NULL,
            VOLUME INTEGER NOT NULL CHECK(VOLUME >= 0), VALUE REAL NOT NULL CHECK(VALUE >= 0),
            SHORTNAME TEXT NOT NULL, LSTTRADE TEXT NOT NULL,
            PRIMARY KEY (BEGIN, SECID), CHECK(substr(BEGIN, 1, 10) = TRADEDATE),
            CHECK(LSTTRADE >= TRADEDATE), CHECK(LOW > 0 AND LOW <= OPEN AND LOW <= CLOSE),
            CHECK(HIGH >= OPEN AND HIGH >= CLOSE)
        )''')
        schema = connection.execute("PRAGMA table_info(Minute)").fetchall()
        keys = [row[1] for row in sorted(schema, key=lambda row: row[5]) if row[5]]
        if tuple(row[1] for row in schema) != MINUTE_COLUMNS or keys != ["BEGIN", "SECID"]:
            raise DataError("Несовместимая таблица Minute; укажите новую папку данных.")
        if not stored and connection.execute("SELECT 1 FROM Minute LIMIT 1").fetchone():
            raise DataError("Непустая база не содержит метаданных владельца.")
        connection.executemany("INSERT OR IGNORE INTO Metadata VALUES (?, ?)", expected.items())
        connection.execute('''CREATE TABLE IF NOT EXISTS Contracts (
            SECID TEXT PRIMARY KEY, SHORTNAME TEXT NOT NULL, LSTTRADE TEXT NOT NULL
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS LoadedDays (
            TRADEDATE TEXT NOT NULL, SECID TEXT NOT NULL,
            CANDLES INTEGER NOT NULL CHECK(CANDLES >= 0), DIGEST TEXT NOT NULL,
            EMPTY_RESPONSE INTEGER NOT NULL CHECK(EMPTY_RESPONSE IN (0, 1)), CHECKED_AT TEXT NOT NULL,
            PRIMARY KEY (TRADEDATE, SECID)
        )''')
        connection.execute('''CREATE TABLE IF NOT EXISTS EmptyWeekends (
            TRADEDATE TEXT PRIMARY KEY, CHECKED_AT TEXT NOT NULL,
            CHECK(strftime('%w', TRADEDATE) IN ('0', '6'))
        )''')
        connection.execute("CREATE INDEX IF NOT EXISTS Minute_DAY ON Minute(TRADEDATE, SECID)")
        connection.execute("CREATE INDEX IF NOT EXISTS Minute_SECID_BEGIN ON Minute(SECID, BEGIN)")


def cached_contract(connection: sqlite3.Connection, client: ISSClient, asset: str, secid: str) -> dict[str, str]:
    """Возвращает описание secid актива asset из connection либо получает через client и сохраняет в кеше."""
    stored = connection.execute("SELECT SECID, SHORTNAME, LSTTRADE FROM Contracts WHERE SECID=?", (secid,)).fetchone()
    if stored:
        return dict(zip(("SECID", "SHORTNAME", "LSTTRADE"), stored))
    info = client.contract(asset, secid)
    with connection:
        connection.execute("INSERT INTO Contracts VALUES (?, ?, ?)", (secid, info["SHORTNAME"], info["LSTTRADE"]))
    return info


def calendar_pair(connection: sqlite3.Connection, client: ISSClient, asset: str,
                  day: date, history_cache: dict[date, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Находит пару asset для календарного day по истории и соседним датам; connection и history_cache кешируют описания и ответы client."""
    dates = [day]
    dates.extend(day - timedelta(days=offset) for offset in range(1, 15))
    dates.extend(day + timedelta(days=offset) for offset in range(1, 15)
                 if day + timedelta(days=offset) <= yesterday_moscow())
    for reference in dates:
        if reference not in history_cache:
            history_cache[reference] = client.history(asset, reference)
        history = history_cache[reference]
        if not history:
            continue
        contracts = {row["SECID"]: cached_contract(connection, client, asset, row["SECID"])
                     for row in history}
        eligible = [row for row in history if date.fromisoformat(contracts[row["SECID"]]["LSTTRADE"]) >= day]
        if len(eligible) < 2:
            continue
        pair = choose_pair(day, history, contracts)
        if reference != day:
            LOGGER.info("%s %s: состав контрактов взят из истории %s; запрашиваются свечи календарной даты %s.",
                        asset, day, reference, day)
        return pair
    raise DataError(f"{asset} {day}: не удалось определить две действующие серии по истории даты "
                    "и соседним датам в пределах 14 дней; пустота свечей не подтверждена.")


def candle_digest(rows: list[tuple]) -> str:
    """Возвращает SHA-256 упорядоченных строк rows таблицы Minute для проверки сохранённых значений."""
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def complete_day(connection: sqlite3.Connection, day: date, allow_empty: bool = False) -> bool:
    """Проверяет в connection две отметки day и контрольные суммы; allow_empty допускает подтверждённый пустой выходной."""
    markers = connection.execute("SELECT SECID, CANDLES, DIGEST, EMPTY_RESPONSE FROM LoadedDays WHERE TRADEDATE=?",
                                 (day.isoformat(),)).fetchall()
    if len(markers) != 2:
        return False
    secids = {row[0] for row in markers}
    existing = {row[0] for row in connection.execute("SELECT DISTINCT SECID FROM Minute WHERE TRADEDATE=?",
                                                   (day.isoformat(),))}
    if not existing.issubset(secids):
        return False
    for secid, count, digest, empty_response in markers:
        rows = connection.execute("SELECT * FROM Minute WHERE TRADEDATE=? AND SECID=? ORDER BY BEGIN",
                                  (day.isoformat(), secid)).fetchall()
        if (count != len(rows) or bool(empty_response) != (not rows)
                or (not rows and not allow_empty) or candle_digest(rows) != digest):
            return False
    return True


def save_day(connection: sqlite3.Connection, day: date, pair: list[dict[str, Any]],
             quotes: dict[str, list[dict[str, Any]]]) -> int:
    """Атомарно заменяет в connection свечи quotes и отметки pair за day, сохраняя прежние непустые данные при пустом ответе; возвращает число свечей."""
    if len(pair) != 2 or len({row["SECID"] for row in pair}) != 2:
        raise DataError("Для записи требуются два разных контракта.")
    prepared, markers = [], []
    for info in pair:
        secid = info["SECID"]
        candles = normalize_candles(day, quotes[secid])
        if not candles and connection.execute("SELECT 1 FROM Minute WHERE TRADEDATE=? AND SECID=? LIMIT 1",
                                               (day.isoformat(), secid)).fetchone():
            raise DataError(f"{day} {secid}: неожиданно пустой ответ candles; ранее сохранённые свечи не удалены.")
        rows = [(day.isoformat(), secid, row["begin"], row["end"], row["open"], row["low"],
                 row["high"], row["close"], row["volume"], row["value"], info["SHORTNAME"], info["LSTTRADE"])
                for row in candles]
        prepared.extend(rows)
        markers.append((day.isoformat(), secid, len(rows), candle_digest(rows), int(not rows),
                        datetime.now(MOSCOW).isoformat(timespec="seconds")))
    with connection:
        connection.execute("DELETE FROM Minute WHERE TRADEDATE=?", (day.isoformat(),))
        connection.execute("DELETE FROM LoadedDays WHERE TRADEDATE=?", (day.isoformat(),))
        connection.executemany("INSERT INTO Minute VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", prepared)
        connection.executemany("INSERT INTO LoadedDays VALUES (?, ?, ?, ?, ?, ?)", markers)
        connection.execute("DELETE FROM EmptyWeekends WHERE TRADEDATE=?", (day.isoformat(),))
        if not prepared and day.weekday() >= 5:
            connection.execute("INSERT INTO EmptyWeekends VALUES (?, ?)",
                               (day.isoformat(), datetime.now(MOSCOW).isoformat(timespec="seconds")))
    return len(prepared)


def download_missing(connection: sqlite3.Connection, client: ISSClient, asset: str,
                     start: date, end: date, recheck: bool = False,
                     recheck_empty_weekends: bool = False) -> DownloadResult:
    """Докачивает asset за start..end через client в connection; флаги управляют перепроверкой; возвращает счётчики."""
    result = DownloadResult()
    history_cache: dict[date, list[dict[str, Any]]] = {}
    day = start
    while day <= end:
        empty_weekend = bool(connection.execute("SELECT 1 FROM EmptyWeekends WHERE TRADEDATE=?",
                                                (day.isoformat(),)).fetchone())
        if (not recheck and not (empty_weekend and recheck_empty_weekends)
                and complete_day(connection, day, allow_empty=empty_weekend)):
            if empty_weekend:
                result.cached_empty_days += 1
            else:
                result.existing_days += 1
        else:
            pair = []
            try:
                pair = calendar_pair(connection, client, asset, day, history_cache)
                quotes = {row["SECID"]: client.candles(row["SECID"], day) for row in pair}
                count = save_day(connection, day, pair, quotes)
                result.written_days += 1
                result.candles += count
                if not count:
                    result.empty_days += 1
                LOGGER.info("%s %s: %s и %s; записано минутных свечей: %s.",
                            asset, day, pair[0]["SECID"], pair[1]["SECID"], count)
                if any(not values for values in quotes.values()):
                    LOGGER.info("%s %s: пустые календарные выборки сохраняются без искусственных свечей; "
                                "будни и дни с одной непустой серией будут проверены снова.", asset, day)
            except DataError as error:
                result.failed_days += 1
                LOGGER.error("%s %s: %s; пара: %s", asset, day, error, json.dumps(pair, ensure_ascii=False))
        day += timedelta(days=1)
    return result


def main(argv: list[str] | None = None) -> int:
    """Выполняет загрузку выбранных активов для argv; возвращает 0, 1 при ошибке или 130 при прерывании."""
    args = parse_arguments(argv)
    status = 0
    try:
        configure_logging()
        args.data_dir.mkdir(parents=True, exist_ok=True)
        LOGGER.info("Период %s — %s, активы: %s, папка данных: %s.",
                    args.start, args.end, ", ".join(args.assets), args.data_dir)
        for asset in args.assets:
            database = args.data_dir / f"{asset}_m1_two_ticker.db"
            try:
                with closing(sqlite3.connect(database, timeout=30)) as connection:
                    initialize_database(connection, asset)
                    client = ISSClient(args.timeout, args.retries, args.pause)
                    result = download_missing(connection, client, asset, args.start, args.end,
                                              args.recheck, args.recheck_empty_weekends)
                LOGGER.info("%s: БД %s; готовых дней: %s; записано дней: %s, свечей: %s; "
                            "пустых ответов: %s; пропущено пустых выходных: %s; неполных дней: %s.",
                            asset, database, result.existing_days, result.written_days, result.candles,
                            result.empty_days, result.cached_empty_days, result.failed_days)
                if result.failed_days:
                    status = 1
            except (ISSRequestError, DataError, sqlite3.Error, OSError) as error:
                LOGGER.error("%s: загрузка остановлена: %s. Записанные дни сохранены; повторите команду для докачки.",
                             asset, error)
                status = 1
        return status
    except KeyboardInterrupt:
        LOGGER.warning("Загрузка прервана; записанные дни сохранены. Повторите команду для докачки.")
        return 130
    except (OSError, sqlite3.Error):
        LOGGER.exception("Не удалось открыть папку данных, базу или журналы.")
        return 1
    finally:
        close_logging()


if __name__ == "__main__":
    raise SystemExit(main())
