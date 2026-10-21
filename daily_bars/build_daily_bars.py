r"""Построение единого дневного ряда RTS и MIX из минутных SQLite-баз.

Каждый интервал длится календарные сутки: [21:00 предыдущей даты, 21:00 даты
окончания) по Москве. Для всего интервала выбирается единственный ближайший
контракт с LSTTRADE строго позже даты окончания: экспирирующая в этот день
серия уже не действует к 21:00. Подмена отсутствующей серии дальней запрещена.
Open/Close берутся из первой/последней минуты выбранной серии, High/Low —
экстремумы, Volume — сумма объёмов. BODY = Close - Open,
UPPER_WICK = High - max(Open, Close), LOWER_WICK = min(Open, Close) - Low.
Показатели выражены в пунктах котировки, без корректировки цен при переходе.

По умолчанию обрабатываются оба актива и весь подтверждённый диапазон исходных
баз C:\data_quote\m1_two_ticker. Для каждого бара нужны LoadedDays обеих
календарных дат; проверяются пары контрактов и количества минут. Пустые
интервалы пропускаются, незавершённый правый край не сохраняется. --start и
--end обозначают даты окончания баров включительно, а не даты первых минут.

Исходные базы открываются только для чтения. Результаты: data\d1\RTS_d1.db
и data\d1\MIX_d1.db относительно корня проекта. Запрошенный период заменяется
одной транзакцией на актив, остальные даты сохраняются. Вывод идёт в консоль,
файлы журналов не создаются. Используется стандартная библиотека Python 3.10+.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --start 2020-01-02
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --assets RTS
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --start 2026-09-01 --end 2026-09-30
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --input-dir C:\data_quote\m1_two_ticker --output-dir C:\data_quote\d1
    .\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --help
"""

from __future__ import annotations

import argparse
import math
import sqlite3
import sys
from contextlib import closing
from dataclasses import astuple, dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT_DIR = Path(r"C:\data_quote\m1_two_ticker")
DEFAULT_OUTPUT_DIR = ROOT / "data" / "d1"
MOSCOW = timezone(timedelta(hours=3), name="Europe/Moscow")
DAY_BOUNDARY = time(21)
DAILY_COLUMNS = ("TRADEDATE", "SECID", "BEGIN", "END", "OPEN", "LOW", "HIGH", "CLOSE",
                 "VOLUME", "BODY", "UPPER_WICK", "LOWER_WICK", "MINUTES", "FIRST_MINUTE",
                 "LAST_MINUTE", "SHORTNAME", "LSTTRADE")


class DataError(RuntimeError):
    """Сообщает о несовместимой базе или недостаточных данных для целого интервала."""


@dataclass(frozen=True)
class DailyBar:
    """Хранит один бар, его календарные границы, единственный тикер и параметры свечи."""

    trade_date: str
    secid: str
    begin: str
    end: str
    open: float
    low: float
    high: float
    close: float
    volume: int
    body: float
    upper_wick: float
    lower_wick: float
    minutes: int
    first_minute: str
    last_minute: str
    shortname: str
    lsttrade: str


@dataclass(frozen=True)
class BuildResult:
    """Содержит актив, включительный диапазон дат окончания, бары и число пустых интервалов."""

    asset: str
    start: date
    end: date
    bars: list[DailyBar]
    empty_intervals: int


def validate_source(connection: sqlite3.Connection, asset: str) -> list[tuple[str, str, str]]:
    """Проверяет минутную connection для asset; возвращает (SECID, SHORTNAME, LSTTRADE) по экспирации."""
    expected = {"ASSETCODE": asset, "INTERVAL": "1", "TIMEZONE": "Europe/Moscow", "SCHEMA_VERSION": "1"}
    stored = dict(connection.execute("SELECT KEY, VALUE FROM Metadata"))
    if any(stored.get(key) != value for key, value in expected.items()):
        raise DataError(f"Несовместимая минутная база: {stored!r}; ожидается {expected!r}.")
    contracts = connection.execute(
        "SELECT SECID, SHORTNAME, LSTTRADE FROM Contracts ORDER BY LSTTRADE, SECID").fetchall()
    if not contracts:
        raise DataError("В минутной базе отсутствуют описания контрактов.")
    for secid, shortname, expiry in contracts:
        if not secid or not shortname or date.fromisoformat(expiry).isoformat() != expiry:
            raise DataError(f"Некорректное описание контракта {secid!r}.")
    return contracts


def resolve_period(connection: sqlite3.Connection, start: date | None, end: date | None,
                   now: datetime | None) -> tuple[date, date]:
    """Определяет полный диапазон connection с учётом start/end и московского момента now; возвращает даты."""
    first, last = connection.execute("SELECT MIN(TRADEDATE), MAX(TRADEDATE) FROM LoadedDays").fetchone()
    if first is None:
        raise DataError("В минутной базе нет подтверждённых загруженных дат LoadedDays.")
    first_possible = date.fromisoformat(first) + timedelta(days=1)
    last_loaded = date.fromisoformat(last)
    instant = now if now is not None else datetime.now(MOSCOW)
    if instant.tzinfo is None:
        raise DataError("Момент now должен содержать часовой пояс.")
    instant = instant.astimezone(MOSCOW)
    last_closed = instant.date() - timedelta(days=int(instant.time() < DAY_BOUNDARY))
    first_day = start if start is not None else first_possible
    last_day = end if end is not None else min(last_loaded, last_closed)
    if first_day > last_day:
        raise DataError("Нет полных интервалов: начальная дата позже конечной.")
    if first_day < first_possible or last_day > last_loaded:
        raise DataError(f"Для полных интервалов допустимы даты {first_possible}..{last_loaded}; "
                        "нужны загруженные предыдущая и текущая календарные даты.")
    if last_day > last_closed:
        raise DataError(f"Бар {last_day} ещё не завершён: последняя закрытая граница — {last_closed} 21:00 МСК.")
    return first_day, last_day


def validate_coverage(connection: sqlite3.Connection, contracts: list[tuple[str, str, str]],
                      start: date, end: date) -> set[tuple[str, str]]:
    """Проверяет пары и счётчики минут connection за start-1..end; возвращает подтверждённые (дата, тикер)."""
    first = (start - timedelta(days=1)).isoformat()
    last = end.isoformat()
    markers = {(day, secid): (count, empty) for day, secid, count, empty in connection.execute(
        "SELECT TRADEDATE, SECID, CANDLES, EMPTY_RESPONSE FROM LoadedDays WHERE TRADEDATE BETWEEN ? AND ?",
        (first, last))}
    counts = {(day, secid): count for day, secid, count in connection.execute(
        "SELECT TRADEDATE, SECID, COUNT(*) FROM Minute WHERE TRADEDATE BETWEEN ? AND ? GROUP BY TRADEDATE, SECID",
        (first, last))}
    day = start - timedelta(days=1)
    while day <= end:
        label = day.isoformat()
        expected = {secid for secid, _, expiry in [row for row in contracts if row[2] >= label][:2]}
        actual = {secid for marker_day, secid in markers if marker_day == label}
        if len(expected) != 2 or actual != expected:
            raise DataError(f"{label}: нет подтверждённой загрузки обеих ближайших серий; "
                            f"ожидаются {sorted(expected)}, найдены {sorted(actual)}.")
        day += timedelta(days=1)
    for key, (count, empty) in markers.items():
        if not isinstance(count, int) or count < 0 or count != counts.get(key, 0) or empty != int(count == 0):
            raise DataError(f"{key[0]} {key[1]}: количество минут не совпадает с LoadedDays; "
                            "повторите минутную загрузку.")
    if counts.keys() - markers.keys():
        raise DataError("В минутной базе есть серии без отметок LoadedDays.")
    return set(markers)


def aggregate_interval(connection: sqlite3.Connection, day: date,
                       contract: tuple[str, str, str]) -> DailyBar | None:
    """Агрегирует в connection только contract за [day-1 21:00, day 21:00); возвращает бар либо None без сделок."""
    secid, shortname, expiry = contract
    begin = datetime.combine(day - timedelta(days=1), DAY_BOUNDARY).isoformat(sep=" ")
    end = datetime.combine(day, DAY_BOUNDARY).isoformat(sep=" ")
    count, first, last, low, high, volume = connection.execute(
        "SELECT COUNT(*), MIN(BEGIN), MAX(BEGIN), MIN(LOW), MAX(HIGH), SUM(VOLUME) "
        "FROM Minute WHERE SECID=? AND BEGIN>=? AND BEGIN<?", (secid, begin, end)).fetchone()
    if not count:
        return None
    open_, = connection.execute("SELECT OPEN FROM Minute WHERE BEGIN=? AND SECID=?", (first, secid)).fetchone()
    close, = connection.execute("SELECT CLOSE FROM Minute WHERE BEGIN=? AND SECID=?", (last, secid)).fetchone()
    if not all(isinstance(value, (int, float)) and math.isfinite(value) for value in (open_, low, high, close)):
        raise DataError(f"{day} {secid}: некорректные цены OHLC.")
    if low <= 0 or low > min(open_, close) or high < max(open_, close):
        raise DataError(f"{day} {secid}: нарушены границы OHLC.")
    if not isinstance(volume, int) or volume < 0:
        raise DataError(f"{day} {secid}: объём должен быть целым неотрицательным числом.")
    opening, closing, maximum, minimum = (Decimal(str(value)) for value in (open_, close, high, low))
    return DailyBar(day.isoformat(), secid, begin, end, open_, low, high, close, volume,
                    float(closing - opening), float(maximum - max(opening, closing)),
                    float(min(opening, closing) - minimum), count, first, last, shortname, expiry)


def build_daily_bars(connection: sqlite3.Connection, asset: str, start: date | None = None,
                     end: date | None = None, now: datetime | None = None) -> BuildResult:
    """Строит из connection ряд asset за даты окончания start..end; now ограничивает завершённость интервалов."""
    contracts = validate_source(connection, asset)
    first_day, last_day = resolve_period(connection, start, end, now)
    markers = validate_coverage(connection, contracts, first_day, last_day)
    bars = []
    empty = 0
    day = first_day
    while day <= last_day:
        label = day.isoformat()
        contract = next((row for row in contracts if row[2] > label), None)
        if contract is None:
            raise DataError(f"{label}: нет контракта, действующего до окончания интервала.")
        for source_day in (day - timedelta(days=1), day):
            if (source_day.isoformat(), contract[0]) not in markers:
                raise DataError(f"{label}: выбранный контракт {contract[0]} не загружен за {source_day}.")
        bar = aggregate_interval(connection, day, contract)
        if bar is None:
            empty += 1
        else:
            bars.append(bar)
        day += timedelta(days=1)
    return BuildResult(asset, first_day, last_day, bars, empty)


def save_daily_bars(connection: sqlite3.Connection, asset: str, result: BuildResult) -> None:
    """Атомарно заменяет в выходной connection период result актива asset; несовместимую базу отклоняет."""
    if result.asset != asset:
        raise DataError(f"Результат {result.asset} нельзя сохранить как {asset}.")
    expected = {"ASSETCODE": asset, "INTERVAL": "D1", "TIMEZONE": "Europe/Moscow", "SCHEMA_VERSION": "1",
                "DAY_START": "21:00:00", "DATE_LABEL": "END_DATE",
                "CONTRACT_SELECTION": "LSTTRADE_GT_SESSION_END_DATE"}
    with connection:
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if tables and not {"Metadata", "Daily"}.issubset(tables):
            raise DataError("Выходной файл содержит другую схему; укажите отдельную папку дневных баз.")
        connection.execute("CREATE TABLE IF NOT EXISTS Metadata (KEY TEXT PRIMARY KEY, VALUE TEXT NOT NULL)")
        stored = dict(connection.execute("SELECT KEY, VALUE FROM Metadata"))
        if tables and any(stored.get(key) != value for key, value in expected.items()):
            raise DataError(f"Несовместимая дневная база: {stored!r}; ожидается {expected!r}.")
        connection.execute('''CREATE TABLE IF NOT EXISTS Daily (
            TRADEDATE TEXT PRIMARY KEY, SECID TEXT NOT NULL, BEGIN TEXT NOT NULL, END TEXT NOT NULL,
            OPEN REAL NOT NULL, LOW REAL NOT NULL, HIGH REAL NOT NULL, CLOSE REAL NOT NULL,
            VOLUME INTEGER NOT NULL CHECK(VOLUME >= 0), BODY REAL NOT NULL,
            UPPER_WICK REAL NOT NULL CHECK(UPPER_WICK >= 0), LOWER_WICK REAL NOT NULL CHECK(LOWER_WICK >= 0),
            MINUTES INTEGER NOT NULL CHECK(MINUTES > 0), FIRST_MINUTE TEXT NOT NULL, LAST_MINUTE TEXT NOT NULL,
            SHORTNAME TEXT NOT NULL, LSTTRADE TEXT NOT NULL CHECK(LSTTRADE > TRADEDATE),
            CHECK(LOW > 0 AND LOW <= OPEN AND LOW <= CLOSE AND HIGH >= OPEN AND HIGH >= CLOSE),
            CHECK(substr(END, 1, 10) = TRADEDATE AND substr(BEGIN, 12) = '21:00:00'),
            CHECK(END = datetime(BEGIN, '+1 day')),
            CHECK(FIRST_MINUTE >= BEGIN AND LAST_MINUTE >= FIRST_MINUTE AND LAST_MINUTE < END)
        )''')
        schema = connection.execute("PRAGMA table_info(Daily)").fetchall()
        if tuple(row[1] for row in schema) != DAILY_COLUMNS or [row[1] for row in schema if row[5]] != ["TRADEDATE"]:
            raise DataError("Несовместимая таблица Daily; укажите новую папку результатов.")
        connection.executemany("INSERT OR IGNORE INTO Metadata VALUES (?, ?)", expected.items())
        connection.execute("DELETE FROM Daily WHERE TRADEDATE BETWEEN ? AND ?",
                           (result.start.isoformat(), result.end.isoformat()))
        connection.executemany("INSERT INTO Daily VALUES (" + ",".join("?" for _ in DAILY_COLUMNS) + ")",
                               (astuple(bar) for bar in result.bars))
        connection.execute("CREATE INDEX IF NOT EXISTS Daily_SECID_DATE ON Daily(SECID, TRADEDATE)")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает argv либо командную строку; возвращает папки, активы и даты окончания баров."""
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument("--input-dir", type=Path, default=DEFAULT_INPUT_DIR,
                        help="Папка минутных баз (C:\\data_quote\\m1_two_ticker).")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="Папка дневных баз (data\\d1 в корне проекта).")
    parser.add_argument("--assets", nargs="+", choices=("RTS", "MIX"), default=["RTS", "MIX"],
                        help="Базовые активы; по умолчанию RTS MIX.")
    parser.add_argument("--start", type=date.fromisoformat, help="Первая дата окончания бара включительно.")
    parser.add_argument("--end", type=date.fromisoformat, help="Последняя дата окончания бара включительно.")
    args = parser.parse_args(argv)
    if args.start is not None and args.end is not None and args.start > args.end:
        parser.error("--start не может быть позже --end.")
    args.assets = list(dict.fromkeys(args.assets))
    return args


def main(argv: list[str] | None = None) -> int:
    """Выполняет argv для обоих активов; возвращает 0 при успехе, 1 при ошибке, 130 при прерывании."""
    args = parse_arguments(argv)
    failed = False
    try:
        for asset in args.assets:
            source_path = (args.input_dir / f"{asset}_m1_two_ticker.db").resolve()
            target_path = (args.output_dir / f"{asset}_d1.db").resolve()
            try:
                if not source_path.is_file():
                    raise DataError(f"Минутная база не найдена: {source_path}")
                if target_path.exists() and source_path.samefile(target_path):
                    raise DataError("Исходная и выходная базы должны быть разными файлами.")
                print(f"{asset}: чтение {source_path}", flush=True)
                with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
                    source.execute("BEGIN")  # Один согласованный снимок исходных таблиц на весь расчёт.
                    result = build_daily_bars(source, asset, args.start, args.end)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                with closing(sqlite3.connect(target_path)) as target:
                    save_daily_bars(target, asset, result)
                print(f"{asset}: {result.start}..{result.end}; баров {len(result.bars)}, "
                      f"пустых интервалов {result.empty_intervals}; сохранено в {target_path}", flush=True)
            except (DataError, sqlite3.Error, OSError, ValueError) as error:
                print(f"{asset}: ошибка: {error}", file=sys.stderr, flush=True)
                failed = True
    except KeyboardInterrupt:
        print("Расчёт прерван пользователем.", file=sys.stderr)
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
