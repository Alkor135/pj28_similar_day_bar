r"""Независимая сверка полных дневных баз с рабочими минутными SQLite-файлами.

Открывает обе базы только для чтения. Другим SQL-запросом группирует исходные
минуты по дате BEGIN + 3 часа и тикеру, выбирает ближайшую действующую серию,
сравнивает полный набор дат, OHLCV, интервалы, число минут, тело и фитили.
Проверяет весь доступный диапазон завершённых интервалов; перед запуском
следует построить дневные базы без ограничения --start/--end. Файлы не меняет,
логи не создаёт, результаты выводит в консоль. Код 0 означает успешную сверку,
код 1 — ошибку чтения либо несовпадение.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe -X utf8 tests\verify_daily_databases.py
    .\.venv\Scripts\python.exe -X utf8 tests\verify_daily_databases.py --assets RTS
    .\.venv\Scripts\python.exe -X utf8 tests\verify_daily_databases.py --input-dir C:\data_quote\m1_two_ticker --daily-dir C:\data_quote\d1
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def verify_asset(input_dir: Path, daily_dir: Path, asset: str) -> tuple[int, str, str]:
    """Сверяет полный ряд asset из daily_dir с минутами input_dir; возвращает число баров и крайние даты."""
    source_path = (input_dir / f"{asset}_m1_two_ticker.db").resolve()
    daily_path = (daily_dir / f"{asset}_d1.db").resolve()
    with closing(sqlite3.connect(daily_path.as_uri() + "?mode=ro", uri=True)) as connection:
        connection.execute("ATTACH DATABASE ? AS m1", (source_path.as_uri() + "?mode=ro",))
        connection.execute("BEGIN")
        if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise AssertionError(f"{asset}: нарушена целостность дневной базы.")
        first, last = connection.execute("SELECT MIN(TRADEDATE), MAX(TRADEDATE) FROM m1.LoadedDays").fetchone()
        start = (date.fromisoformat(first) + timedelta(days=1)).isoformat()
        now = datetime.now(timezone(timedelta(hours=3)))
        last_closed = now.date() - timedelta(days=int(now.hour < 21))
        end = min(last, last_closed.isoformat())
        expected_rows = connection.execute('''
            WITH grouped AS (
                SELECT date(BEGIN, '+3 hours') AS DAY, SECID,
                       MIN(BEGIN) AS FIRST_MINUTE, MAX(BEGIN) AS LAST_MINUTE,
                       MIN(LOW) AS LOW, MAX(HIGH) AS HIGH, SUM(VOLUME) AS VOLUME, COUNT(*) AS MINUTES
                FROM m1.Minute
                WHERE date(BEGIN, '+3 hours') BETWEEN ? AND ?
                GROUP BY DAY, SECID
            )
            SELECT g.DAY, g.SECID, opening.OPEN, g.LOW, g.HIGH, closing.CLOSE,
                   g.VOLUME, g.MINUTES, g.FIRST_MINUTE, g.LAST_MINUTE
            FROM grouped AS g
            JOIN m1.Minute AS opening ON opening.BEGIN=g.FIRST_MINUTE AND opening.SECID=g.SECID
            JOIN m1.Minute AS closing ON closing.BEGIN=g.LAST_MINUTE AND closing.SECID=g.SECID
            WHERE g.SECID=(SELECT SECID FROM m1.Contracts WHERE LSTTRADE>g.DAY ORDER BY LSTTRADE, SECID LIMIT 1)
            ORDER BY g.DAY
        ''', (start, end)).fetchall()
        expected = {row[0]: row[1:] for row in expected_rows}
        connection.row_factory = sqlite3.Row
        actual = {row["TRADEDATE"]: dict(row) for row in connection.execute("SELECT * FROM Daily ORDER BY TRADEDATE")}
        if expected.keys() != actual.keys():
            raise AssertionError(f"{asset}: набор дат отличается; нет {sorted(expected.keys() - actual.keys())[:5]}, "
                                 f"лишние {sorted(actual.keys() - expected.keys())[:5]}.")
        fields = ("SECID", "OPEN", "LOW", "HIGH", "CLOSE", "VOLUME", "MINUTES", "FIRST_MINUTE", "LAST_MINUTE")
        for label, values in expected.items():
            row = actual[label]
            if tuple(row[field] for field in fields) != values:
                raise AssertionError(f"{asset} {label}: тикер, OHLCV или использованные минуты отличаются от исходных.")
            opening, close_price, high, low = (Decimal(str(row[field])) for field in ("OPEN", "CLOSE", "HIGH", "LOW"))
            metrics = (float(close_price - opening), float(high - max(opening, close_price)),
                       float(min(opening, close_price) - low))
            if (row["BODY"], row["UPPER_WICK"], row["LOWER_WICK"]) != metrics:
                raise AssertionError(f"{asset} {label}: неверное тело либо фитили.")
            previous = date.fromisoformat(label) - timedelta(days=1)
            if row["BEGIN"] != f"{previous} 21:00:00" or row["END"] != f"{label} 21:00:00":
                raise AssertionError(f"{asset} {label}: неверные границы интервала.")
        labels = sorted(actual)
        return len(labels), labels[0] if labels else "—", labels[-1] if labels else "—"


def main(argv: list[str] | None = None) -> int:
    """Разбирает argv и сверяет выбранные активы; возвращает 0 при успехе либо 1 при расхождении."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input-dir", type=Path, default=Path(r"C:\data_quote\m1_two_ticker"),
                        help="Папка исходных минутных баз.")
    parser.add_argument("--daily-dir", type=Path, default=ROOT / "data" / "d1",
                        help="Папка полных дневных баз.")
    parser.add_argument("--assets", choices=("RTS", "MIX"), nargs="+", default=["RTS", "MIX"],
                        help="Проверяемые активы; по умолчанию RTS MIX.")
    args = parser.parse_args(argv)
    try:
        for asset in dict.fromkeys(args.assets):
            count, first, last = verify_asset(args.input_dir, args.daily_dir, asset)
            print(f"{asset}: сверено {count} баров за {first}..{last}; расхождений нет.", flush=True)
    except (AssertionError, sqlite3.Error, OSError, ValueError, TypeError) as error:
        print(f"Ошибка сверки: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
