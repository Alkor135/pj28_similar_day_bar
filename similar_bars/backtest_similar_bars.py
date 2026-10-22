r"""Последовательная проверка прогноза по наиболее похожему прошлому дневному бару.

Для закрытого бара t выбирает аналог j среди ровно --lookback предыдущих
баров с тем же знаком тела. Расстояние — средневзвешенное относительное
различие |a-b|/max(a,b) для |BODY|, UPPER_WICK, LOWER_WICK и VOLUME;
два нуля дают различие 0. Четыре веса задаются вручную; нулевой вес исключает
признак. При равенстве выбирается более свежий аналог. Прогноз — знак тела
бара j+1, который уже известен к закрытию t, включая случай j=t-1.
Результат — знак прогноза, умноженный на BODY бара t+1, в пунктах котировки.
До накопления полного окна, без аналога и при нулевом прогнозе сделки нет.
Нулевое тело цели даёт 0 пунктов; последний прогноз без цели остаётся pending.

--start/--end ограничивают даты СИГНАЛОВ включительно. История до --start
сохраняется для поиска; цель после --end учитывается, если она есть в базе.
Пустые календарные интервалы не добавляются: следующий бар — следующая
сохранённая строка Daily. Комиссии, проскальзывание и разрывы между барами
не учитываются. Это проверка направления и суммы тел, без пересчёта в деньги.

Читает data\d1\{RTS,MIX}_d1.db только для чтения. В data\similar_bars
перезаписывает <ASSET>_trades.csv (UTF-8 BOM, разделитель ;) и
<ASSET>_summary.json с параметрами, результатом, точностью и просадкой.
Каждый файл заменяется атомарно; два файла не образуют общую транзакцию.
Логи не создаёт, зависимости — стандартная библиотека Python 3.10+.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe similar_bars\backtest_similar_bars.py --start 2020-03-01
    .\.venv\Scripts\python.exe similar_bars\backtest_similar_bars.py --assets RTS --lookback 20 --body-weight 2 --upper-wick-weight 1 --lower-wick-weight 1 --volume-weight 0.5
    .\.venv\Scripts\python.exe similar_bars\backtest_similar_bars.py --start 2020-03-01 --end 2020-12-31 --volume-weight 0
    .\.venv\Scripts\python.exe similar_bars\backtest_similar_bars.py --input-dir C:\data_quote\d1 --output-dir data\experiment
    .\.venv\Scripts\python.exe similar_bars\backtest_similar_bars.py --help
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import closing
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_WEIGHTS = (1.0, 1.0, 1.0, 1.0)
WEIGHT_NAMES = ("body", "upper_wick", "lower_wick", "volume")
CSV_COLUMNS = (
    "signal_date", "signal_secid", "signal_body", "signal_upper_wick", "signal_lower_wick", "signal_volume",
    "matched_candidates", "analog_date", "analog_secid", "analog_body", "analog_upper_wick",
    "analog_lower_wick", "analog_volume", "distance", "analog_next_date", "analog_next_body",
    "prediction", "target_date", "target_secid", "target_body", "actual_direction", "correct",
    "pnl_points", "cumulative_points", "drawdown_points", "status",
)


class DataError(ValueError):
    """Сообщает об отсутствующих котировках, несовместимых метаданных или неверных свечах."""


@dataclass(frozen=True)
class Bar:
    """Хранит дату, контракт, тело со знаком, два неотрицательных фитиля и объём дневного бара."""

    trade_date: str
    secid: str
    body: float
    upper_wick: float
    lower_wick: float
    volume: int


@dataclass(frozen=True)
class BacktestResult:
    """Содержит строки сигналов для CSV и итоговые показатели с параметрами расчёта для JSON."""

    rows: list[dict]
    summary: dict


def direction(body: float) -> int:
    """Возвращает направление заданного тела body: 1 вверх, -1 вниз, 0 без изменения."""
    return (body > 0) - (body < 0)


def validate_weights(weights: tuple[float, ...]) -> tuple[float, ...]:
    """Проверяет четыре конечных неотрицательных weights с положительной суммой; возвращает нормированные веса."""
    if len(weights) != 4 or any(not math.isfinite(value) or value < 0 for value in weights):
        raise ValueError("Нужны четыре конечных неотрицательных веса: тело, верхний фитиль, нижний фитиль, объём.")
    largest = max(weights)
    if largest == 0:
        raise ValueError("Хотя бы один вес должен быть больше нуля.")
    scaled = tuple(value / largest for value in weights)
    total = math.fsum(scaled)
    return tuple(value / total for value in scaled)


def candle_distance(first: Bar, second: Bar, weights: tuple[float, ...] = DEFAULT_WEIGHTS) -> float:
    """Возвращает средневзвешенное относительное расстояние first и second по весам weights, без фильтра направления."""
    normalized = validate_weights(weights)
    left = (abs(first.body), first.upper_wick, first.lower_wick, first.volume)
    right = (abs(second.body), second.upper_wick, second.lower_wick, second.volume)
    return math.fsum(weight * (abs(a - b) / max(a, b) if max(a, b) else 0)
                     for a, b, weight in zip(left, right, normalized) if weight > 0)


def validate_bars(bars: list[Bar]) -> None:
    """Проверяет даты, порядок, конечность признаков и неотрицательность фитилей/объёма списка bars; результата нет."""
    if not bars:
        raise DataError("В таблице Daily нет баров.")
    previous = ""
    for bar in bars:
        if date.fromisoformat(bar.trade_date).isoformat() != bar.trade_date or bar.trade_date <= previous:
            raise DataError("Бары должны иметь уникальные даты YYYY-MM-DD и идти по возрастанию.")
        if not bar.secid or any(not math.isfinite(value) for value in
                                (bar.body, bar.upper_wick, bar.lower_wick, bar.volume)):
            raise DataError(f"{bar.trade_date}: пустой контракт или неконечный признак.")
        if min(bar.upper_wick, bar.lower_wick, bar.volume) < 0 or int(bar.volume) != bar.volume:
            raise DataError(f"{bar.trade_date}: фитили и объём должны быть неотрицательны, объём — целым.")
        previous = bar.trade_date


def load_bars(connection: sqlite3.Connection, asset: str) -> list[Bar]:
    """Читает и проверяет дневную connection для asset; возвращает все бары по дате, сохраняя предысторию."""
    expected = {"ASSETCODE": asset, "INTERVAL": "D1", "TIMEZONE": "Europe/Moscow", "SCHEMA_VERSION": "1",
                "DAY_START": "21:00:00", "DATE_LABEL": "END_DATE",
                "CONTRACT_SELECTION": "LSTTRADE_GT_SESSION_END_DATE"}
    metadata = dict(connection.execute("SELECT KEY, VALUE FROM Metadata"))
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise DataError(f"Несовместимые метаданные дневной базы {asset}; ожидается {expected!r}.")
    bars = [Bar(*row) for row in connection.execute(
        "SELECT TRADEDATE, SECID, BODY, UPPER_WICK, LOWER_WICK, VOLUME FROM Daily ORDER BY TRADEDATE")]
    validate_bars(bars)
    return bars


def backtest(bars: list[Bar], lookback: int = 20, start: date | None = None,
             end: date | None = None, weights: tuple[float, ...] = DEFAULT_WEIGHTS) -> BacktestResult:
    """Проверяет bars по окну lookback и weights для сигналов start..end; возвращает детализацию и статистику."""
    validate_weights(weights)
    validate_bars(bars)
    if not isinstance(lookback, int) or isinstance(lookback, bool) or lookback < 1:
        raise ValueError("Окно --lookback должно быть целым числом не меньше 1.")
    if start is not None and end is not None and start > end:
        raise ValueError("Начальная дата не может быть позже конечной.")
    first = start.isoformat() if start else bars[0].trade_date
    last = end.isoformat() if end else bars[-1].trade_date
    rows = []
    cumulative = peak = max_drawdown = Decimal(0)
    for index, current in enumerate(bars):
        if not first <= current.trade_date <= last:
            continue
        target = bars[index + 1] if index + 1 < len(bars) else None
        row = dict.fromkeys(CSV_COLUMNS)
        row.update(signal_date=current.trade_date, signal_secid=current.secid, signal_body=current.body,
                   signal_upper_wick=current.upper_wick, signal_lower_wick=current.lower_wick,
                   signal_volume=current.volume, matched_candidates=0, pnl_points=0.0,
                   cumulative_points=float(cumulative), drawdown_points=float(peak - cumulative))
        if target is not None:
            row.update(target_date=target.trade_date, target_secid=target.secid, target_body=target.body,
                       actual_direction=direction(target.body))
        if index < lookback:
            row["status"] = "insufficient_history"
        else:
            candidates = [j for j in range(index - 1, index - lookback - 1, -1)
                          if direction(bars[j].body) == direction(current.body)]
            row["matched_candidates"] = len(candidates)
            if not candidates:
                row["status"] = "no_matching_direction"
            else:
                # Самый свежий кандидат идёт первым; min сохраняет его при точном равенстве расстояний.
                analog_index = min(candidates, key=lambda j: candle_distance(current, bars[j], weights))
                analog, successor = bars[analog_index], bars[analog_index + 1]
                prediction = direction(successor.body)
                row.update(analog_date=analog.trade_date, analog_secid=analog.secid,
                           analog_body=analog.body, analog_upper_wick=analog.upper_wick,
                           analog_lower_wick=analog.lower_wick, analog_volume=analog.volume,
                           distance=candle_distance(current, analog, weights),
                           analog_next_date=successor.trade_date, analog_next_body=successor.body,
                           prediction=prediction)
                if prediction == 0:
                    row["status"] = "flat_prediction"
                elif target is None:
                    row["status"] = "pending"
                else:
                    pnl = Decimal(str(target.body)) * prediction
                    cumulative += pnl
                    peak = max(peak, cumulative)
                    drawdown = peak - cumulative
                    max_drawdown = max(max_drawdown, drawdown)
                    row.update(status="evaluated", correct=prediction == direction(target.body) if target.body else None,
                               pnl_points=float(pnl), cumulative_points=float(cumulative), drawdown_points=float(drawdown))
        rows.append(row)
    if not rows:
        raise DataError(f"Нет сигналов за {first}..{last}; в базе есть {bars[0].trade_date}..{bars[-1].trade_date}.")
    evaluated = [row for row in rows if row["status"] == "evaluated"]
    statuses = Counter(row["status"] for row in rows)
    hits = sum(row["correct"] is True for row in evaluated)
    misses = sum(row["correct"] is False for row in evaluated)
    gains = sum((Decimal(str(row["pnl_points"])) for row in evaluated if row["pnl_points"] > 0), Decimal(0))
    losses = -sum((Decimal(str(row["pnl_points"])) for row in evaluated if row["pnl_points"] < 0), Decimal(0))
    long_points = sum((Decimal(str(row["target_body"])) for row in evaluated), Decimal(0))
    summary = {
        "lookback": lookback, "weights": dict(zip(WEIGHT_NAMES, weights)),
        "distance_method": "weighted_relative_max", "direction_filter": "same_sign",
        "tie_break": "most_recent", "period_applies_to": "signal_dates",
        "requested_start": start.isoformat() if start else None, "requested_end": end.isoformat() if end else None,
        "source_bars": len(bars), "first_signal_date": rows[0]["signal_date"],
        "last_signal_date": rows[-1]["signal_date"], "signals": len(rows), "trades": len(evaluated),
        "first_target_date": evaluated[0]["target_date"] if evaluated else None,
        "last_target_date": evaluated[-1]["target_date"] if evaluated else None,
        "hits": hits, "misses": misses, "flat_actual": len(evaluated) - hits - misses,
        "accuracy_percent": 100 * hits / (hits + misses) if hits + misses else None,
        "points": float(cumulative), "gross_profit_points": float(gains), "gross_loss_points": float(losses),
        "profit_factor": float(gains / losses) if losses else None,
        "max_drawdown_points": float(max_drawdown), "always_long_points": float(long_points),
        "always_short_points": float(-long_points), "insufficient_history": statuses["insufficient_history"],
        "no_matching_direction": statuses["no_matching_direction"], "flat_prediction": statuses["flat_prediction"],
        "pending_forecasts": statuses["pending"],
    }
    return BacktestResult(rows, summary)


def save_results(result: BacktestResult, asset: str, source_path: Path, output_dir: Path) -> None:
    """Перезаписывает CSV и JSON результата result для asset из source_path в output_dir; каждый файл атомарен."""
    output_dir.mkdir(parents=True, exist_ok=True)
    summary = {"asset": asset, "source_path": str(source_path), **result.summary}
    for suffix, encoding in (("trades.csv", "utf-8-sig"), ("summary.json", "utf-8")):
        target = output_dir / f"{asset}_{suffix}"
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding=encoding, newline="", dir=output_dir,
                                             prefix=f".{asset}_", suffix=".tmp", delete=False) as stream:
                temporary_path = Path(stream.name)
                if suffix.endswith("csv"):
                    writer = csv.DictWriter(stream, fieldnames=CSV_COLUMNS, delimiter=";")
                    writer.writeheader()
                    writer.writerows(result.rows)
                else:
                    json.dump(summary, stream, ensure_ascii=False, indent=2, allow_nan=False)
                    stream.write("\n")
            os.replace(temporary_path, target)
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    """Разбирает argv либо командную строку и проверяет даты, окно и ручные веса; возвращает параметры запуска."""
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "data" / "d1", help="Папка дневных SQLite-баз.")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "data" / "similar_bars", help="Папка CSV и JSON.")
    parser.add_argument("--assets", choices=("RTS", "MIX"), nargs="+", default=["RTS", "MIX"], help="Активы: RTS MIX.")
    parser.add_argument("--lookback", type=int, default=20, help="Число предыдущих баров; по умолчанию 20.")
    parser.add_argument("--start", type=date.fromisoformat, help="Первая дата сигнала включительно.")
    parser.add_argument("--end", type=date.fromisoformat, help="Последняя дата сигнала включительно.")
    for name, label in zip(WEIGHT_NAMES, ("тела", "верхнего фитиля", "нижнего фитиля", "объёма")):
        parser.add_argument(f"--{name.replace('_', '-')}-weight", type=float, default=1.0,
                            help=f"Вес {label}; по умолчанию 1, значение 0 исключает признак.")
    args = parser.parse_args(argv)
    args.weights = tuple(getattr(args, f"{name}_weight") for name in WEIGHT_NAMES)
    try:
        validate_weights(args.weights)
        if args.lookback < 1:
            raise ValueError("--lookback должен быть не меньше 1.")
        if args.start is not None and args.end is not None and args.start > args.end:
            raise ValueError("--start не может быть позже --end.")
    except ValueError as error:
        parser.error(str(error))
    args.assets = list(dict.fromkeys(args.assets))
    return args


def main(argv: list[str] | None = None) -> int:
    """Проверяет идею по argv для выбранных активов; возвращает 0 при успехе, 1 при ошибке, 130 при прерывании."""
    args = parse_arguments(argv)
    failed = False
    try:
        for asset in args.assets:
            source_path = (args.input_dir / f"{asset}_d1.db").resolve()
            try:
                if not source_path.is_file():
                    raise DataError(f"Дневная база не найдена: {source_path}")
                with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as connection:
                    connection.execute("BEGIN")
                    bars = load_bars(connection, asset)
                result = backtest(bars, args.lookback, args.start, args.end, args.weights)
                save_results(result, asset, source_path, args.output_dir.resolve())
                summary = result.summary
                accuracy = f"{summary['accuracy_percent']:.2f}%" if summary["accuracy_percent"] is not None else "—"
                print(f"{asset}: сигналы {summary['first_signal_date']}..{summary['last_signal_date']}; "
                      f"сделок {summary['trades']}, угадано {summary['hits']}, ошибок {summary['misses']}, "
                      f"нулевых тел {summary['flat_actual']}; точность {accuracy}; "
                      f"итог {summary['points']:+g} п., макс. просадка {summary['max_drawdown_points']:g} п.; "
                      f"ожидают следующего бара {summary['pending_forecasts']}. "
                      f"Отчёты: {args.output_dir.resolve()}", flush=True)
            except (sqlite3.Error, OSError, ValueError, TypeError, OverflowError) as error:
                print(f"{asset}: ошибка: {error}", file=sys.stderr, flush=True)
                failed = True
    except KeyboardInterrupt:
        print("Расчёт прерван пользователем.", file=sys.stderr)
        return 130
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
