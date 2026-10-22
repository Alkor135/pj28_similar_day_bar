r"""Создание автономного HTML-отчёта из CSV/JSON проверки похожих дневных баров.

Читает <ASSET>_trades.csv и <ASSET>_summary.json, сверяет результат, просадку,
счётчики и направление сделок с учётом сохранения дробных сумм как float.
Строит накопленный PnL на даты целевых баров,
просадку и таблицу показателей. Фактические окно и веса берутся из JSON.
Все данные, стили и код графиков встраиваются в единственный HTML-файл:
он открывается в обычном браузере без интернета, Python и соседних файлов.
Комиссии и проскальзывание не добавляются; результат остаётся в пунктах.

По умолчанию читает data\similar_bars в корне проекта и атомарно заменяет
pnl_report.html в этой же папке. --input-dir выбирает другой эксперимент,
--assets ограничивает активы, --output задаёт путь отчёта, --title — заголовок.
Для генерации нужен расположенный рядом pnl_report_template.html.
Используется только стандартная библиотека Python 3.10+, сеть и логи не нужны.
Исходные CSV/JSON не изменяются; при ошибке прежний отчёт сохраняется.

Примеры запуска из корня проекта:
    .\.venv\Scripts\python.exe similar_bars\build_pnl_report.py
    .\.venv\Scripts\python.exe similar_bars\build_pnl_report.py --assets RTS
    .\.venv\Scripts\python.exe similar_bars\build_pnl_report.py --input-dir data\experiment --output data\experiment\report.html
    .\.venv\Scripts\python.exe similar_bars\build_pnl_report.py --title "Похожие бары: веса 2 / 1 / 1 / 0.5"
    .\.venv\Scripts\python.exe similar_bars\build_pnl_report.py --help
"""

from __future__ import annotations

import argparse
import csv
import html
import json
import math
import os
import re
import sys
import tempfile
from collections import Counter
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = Path(__file__).with_name("pnl_report_template.html")
STATUSES = {"evaluated", "pending", "insufficient_history", "no_matching_direction", "flat_prediction"}
REQUIRED_COLUMNS = {"signal_date", "target_date", "status", "prediction", "target_body", "pnl_points",
                    "cumulative_points", "drawdown_points", "correct"}
WEIGHT_NAMES = ("body", "upper_wick", "lower_wick", "volume")


def number(value: object, label: str) -> Decimal:
    """Преобразует value в конечный Decimal; label поясняет ошибку, результатом является число."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as error:
        raise ValueError(f"{label}: требуется число, получено {value!r}.") from error
    if not result.is_finite() or not math.isfinite(float(result)):
        raise ValueError(f"{label}: требуется конечное число.")
    return result


def canonical_date(value: str) -> str:
    """Проверяет дату value в каноническом формате YYYY-MM-DD и возвращает её строку."""
    if date.fromisoformat(value).isoformat() != value:
        raise ValueError(f"Неверный формат даты: {value!r}.")
    return value


def load_asset(input_dir: Path, asset: str) -> dict:
    """Читает CSV/JSON актива asset из input_dir, учитывая округление float; возвращает проверенные показатели и кривую."""
    with (input_dir / f"{asset}_summary.json").open(encoding="utf-8") as stream:
        summary = json.load(stream)
    if not isinstance(summary, dict) or summary.get("asset") != asset:
        raise ValueError(f"{asset}: сводка принадлежит другому активу или имеет неверный формат.")
    weights = summary.get("weights", {})
    if not isinstance(weights, dict) or set(weights) != set(WEIGHT_NAMES):
        raise ValueError(f"{asset}: в сводке нужны четыре веса признаков.")
    values = [number(weights[key], f"{asset}: вес {key}") for key in WEIGHT_NAMES]
    if min(values) < 0 or max(values) == 0:
        raise ValueError(f"{asset}: веса должны быть неотрицательными, хотя бы один — положительным.")
    if type(summary.get("lookback")) is not int or summary["lookback"] < 1:
        raise ValueError(f"{asset}: неверный размер окна в сводке.")
    with (input_dir / f"{asset}_trades.csv").open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream, delimiter=";")
        if not REQUIRED_COLUMNS.issubset(reader.fieldnames or []):
            raise ValueError(f"{asset}: в CSV отсутствуют обязательные столбцы.")
        rows = list(reader)
    if not rows:
        raise ValueError(f"{asset}: CSV не содержит сигналов.")
    cumulative = peak = maximum_drawdown = gains = losses = Decimal(0)
    counts = Counter()
    previous_signal = previous_target = ""
    first_target = last_target = None
    hits = misses = flat = losing_streak = longest_streak = 0
    trade_results = []
    points = [[canonical_date(rows[0]["signal_date"]), 0, 0]]
    for row in rows:
        signal = canonical_date(row["signal_date"])
        target = canonical_date(row["target_date"]) if row["target_date"] else None
        status = row["status"]
        if signal <= previous_signal or (target is not None and (target <= signal or target <= previous_target)):
            raise ValueError(f"{asset} {signal}: нарушен порядок дат сигналов или целей.")
        if status not in STATUSES:
            raise ValueError(f"{asset} {signal}: неизвестный статус {status!r}.")
        pnl = number(row["pnl_points"], f"{asset} {signal}: результат")
        if status == "evaluated":
            prediction = number(row["prediction"], "Прогноз")
            if target is None or prediction not in (-1, 1):
                raise ValueError(f"{asset} {signal}: оценённый прогноз должен иметь цель и направление ±1.")
            if pnl != prediction * number(row["target_body"], "Тело цели"):
                raise ValueError(f"{asset} {signal}: результат не равен прогнозу, умноженному на тело цели.")
            expected_correct = "True" if pnl > 0 else "False" if pnl < 0 else ""
            if row["correct"] != expected_correct:
                raise ValueError(f"{asset} {signal}: признак верного прогноза не согласован с результатом.")
            hits += int(pnl > 0)
            misses += int(pnl < 0)
            flat += int(pnl == 0)
            gains += max(pnl, 0)
            losses -= min(pnl, 0)
            trade_results.append(pnl)
            losing_streak = losing_streak + 1 if pnl < 0 else 0
            longest_streak = max(longest_streak, losing_streak)
            first_target = first_target or target
            last_target = target
        elif pnl != 0:
            raise ValueError(f"{asset} {signal}: пропущенный или ожидающий прогноз должен иметь нулевой результат.")
        if status == "pending" and target is not None:
            raise ValueError(f"{asset} {signal}: ожидающий прогноз не должен иметь целевого бара.")
        cumulative += pnl
        peak = max(peak, cumulative)
        drawdown = peak - cumulative
        maximum_drawdown = max(maximum_drawdown, drawdown)
        # Расчёт сохраняет суммы Decimal через float; сверяем то же представление без произвольного допуска.
        if number(row["cumulative_points"], "Накопленный результат") != Decimal(str(float(cumulative))):
            raise ValueError(f"{asset} {signal}: неверный накопленный результат в CSV.")
        if number(row["drawdown_points"], "Просадка") != Decimal(str(float(drawdown))):
            raise ValueError(f"{asset} {signal}: неверная просадка в CSV.")
        if target is not None:
            points.append([target, float(cumulative), float(drawdown)])
            previous_target = target
        counts[status] += 1
        previous_signal = signal
    expected = {
        "signals": len(rows), "trades": len(trade_results), "hits": hits, "misses": misses, "flat_actual": flat,
        "points": cumulative, "gross_profit_points": gains, "gross_loss_points": losses,
        "max_drawdown_points": maximum_drawdown, "insufficient_history": counts["insufficient_history"],
        "no_matching_direction": counts["no_matching_direction"], "flat_prediction": counts["flat_prediction"],
        "pending_forecasts": counts["pending"],
    }
    for key, value in expected.items():
        saved_value = Decimal(str(float(value))) if isinstance(value, Decimal) else value
        if number(summary.get(key), f"{asset}: {key}") != saved_value:
            raise ValueError(f"{asset}: CSV и JSON расходятся по показателю {key}.")
    for key, value in (("first_signal_date", rows[0]["signal_date"]), ("last_signal_date", rows[-1]["signal_date"]),
                       ("first_target_date", first_target), ("last_target_date", last_target)):
        if summary.get(key) != value:
            raise ValueError(f"{asset}: CSV и JSON расходятся по границе {key}.")
    checked = {key: float(value) if isinstance(value, Decimal) else value for key, value in expected.items()}
    checked.update(
        asset=asset, lookback=summary["lookback"], weights={key: float(weights[key]) for key in WEIGHT_NAMES},
        first_signal_date=rows[0]["signal_date"], last_signal_date=rows[-1]["signal_date"],
        first_target_date=first_target, last_target_date=last_target,
        accuracy_percent=100 * hits / (hits + misses) if hits + misses else None,
        profit_factor=float(gains / losses) if losses else None,
        mean_trade=float(cumulative / len(trade_results)) if trade_results else None,
        mean_win=float(gains / hits) if hits else None, mean_loss=float(losses / misses) if misses else None,
        best_trade=float(max(trade_results)) if trade_results else None,
        worst_trade=float(min(trade_results)) if trade_results else None,
        recovery_factor=float(cumulative / maximum_drawdown) if maximum_drawdown else None,
        longest_losing_streak=longest_streak,
        skipped_signals=sum(counts[key] for key in ("insufficient_history", "no_matching_direction", "flat_prediction")),
    )
    return {"summary": checked, "points": points}


def format_number(value: float | None, decimals: int = 0) -> str:
    """Форматирует value с decimals знаками для русского отчёта; при отсутствии значения возвращает тире."""
    return "—" if value is None else f"{value:,.{decimals}f}".replace(",", "\u202f").replace(".", ",")


def render_report(data: dict, title: str) -> str:
    """Встраивает проверенные data и экранированный title в локальный шаблон; возвращает полный автономный HTML."""
    metrics = [
        ("Итог, пункты", "points", 0), ("Проверено прогнозов", "trades", 0),
        ("Угадано / ошибок / нулевых тел", "counts", 0),
        ("Точность без нулевых тел, %", "accuracy_percent", 2), ("Прибыль-фактор", "profit_factor", 3),
        ("Сумма выигрышей, пункты", "gross_profit_points", 0), ("Сумма проигрышей, пункты", "gross_loss_points", 0),
        ("Средний результат, пункты / прогноз", "mean_trade", 2), ("Средний выигрыш, пункты", "mean_win", 2),
        ("Средний проигрыш, пункты", "mean_loss", 2), ("Лучший / худший результат, пункты", "best_worst", 0),
        ("Максимальная просадка, пункты", "max_drawdown_points", 0),
        ("Итог / максимальная просадка", "recovery_factor", 2),
        ("Максимум убыточных прогнозов подряд", "longest_losing_streak", 0),
        ("Пропущено сигналов", "skipped_signals", 0), ("Ожидает следующего бара", "pending_forecasts", 0),
    ]
    table_rows = []
    for label, field, decimals in metrics:
        cells = [f'<th scope="row">{label}</th>']
        for asset in data.values():
            summary = asset["summary"]
            if field == "counts":
                value = " / ".join(str(summary[key]) for key in ("hits", "misses", "flat_actual"))
            elif field == "best_worst":
                value = " / ".join(format_number(summary[key]) for key in ("best_trade", "worst_trade"))
            else:
                value = format_number(summary[field], decimals)
            cells.append(f'<td class="numeric">{value}</td>')
        table_rows.append("<tr>" + "".join(cells) + "</tr>")
    parameters = []
    legend = []
    for asset, item in data.items():
        summary = item["summary"]
        weights = " / ".join(f"{summary['weights'][key]:g}" for key in WEIGHT_NAMES)
        parameters.append(f"<div><b>{asset}</b>: сигналы {summary['first_signal_date']}–{summary['last_signal_date']}; "
                          f"окно {summary['lookback']}; веса {weights}</div>")
        legend.append(f'<button type="button" data-series="{asset}" aria-pressed="true">'
                      f'<span class="swatch {asset.lower()}" aria-hidden="true"></span>{asset}</button>')
    replacements = {
        "TITLE": html.escape(title), "PARAMETERS": "\n".join(parameters), "LEGEND": "\n".join(legend),
        "HEADERS": "".join(f'<th scope="col" class="numeric">{asset}</th>' for asset in data),
        "METRICS": "\n".join(table_rows),
        "DATA": json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("<", "\\u003c"),
    }

    def substitute(match: re.Match) -> str:
        """Возвращает подготовленное значение для плейсхолдера match без повторной обработки вставленных данных."""
        return replacements[match.group(1)]

    return re.sub(r"@@REPORT_(TITLE|PARAMETERS|LEGEND|HEADERS|METRICS|DATA)@@", substitute,
                  TEMPLATE.read_text(encoding="utf-8"))


def save_report(output: Path, document: str) -> None:
    """Атомарно заменяет output документом document в UTF-8; удаляет временный файл при ошибке, результата нет."""
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent, prefix=".pnl_report_",
                                         suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(document)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    """Создаёт отчёт по argv; возвращает 0 при успехе, 1 при ошибке файлов/данных, 130 при прерывании."""
    parser = argparse.ArgumentParser(description=__doc__, add_help=False,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("-h", "--help", action="help", help="Показать справку и завершить работу.")
    parser.add_argument("--input-dir", type=Path, default=ROOT / "data" / "similar_bars", help="Папка CSV/JSON эксперимента.")
    parser.add_argument("--assets", nargs="+", choices=("RTS", "MIX"), default=["RTS", "MIX"], help="Активы; по умолчанию RTS MIX.")
    parser.add_argument("--output", type=Path, help="HTML-файл; по умолчанию pnl_report.html в папке --input-dir.")
    parser.add_argument("--title", default="Похожие дневные бары: PnL", help="Заголовок отчёта.")
    args = parser.parse_args(argv)
    try:
        assets = list(dict.fromkeys(args.assets))
        output = (args.output if args.output is not None else args.input_dir / "pnl_report.html").resolve()
        sources = [args.input_dir / f"{asset}_{suffix}" for asset in assets for suffix in ("trades.csv", "summary.json")]
        for source in [*sources, TEMPLATE, Path(__file__)]:
            if output == source.resolve() or (output.exists() and source.exists() and output.samefile(source)):
                raise ValueError("Путь --output совпадает с исходным файлом: выберите другое имя отчёта.")
        data = {asset: load_asset(args.input_dir, asset) for asset in assets}
        save_report(output, render_report(data, args.title))
        print(f"Автономный HTML-отчёт сохранён: {output}", flush=True)
        return 0
    except KeyboardInterrupt:
        print("Создание отчёта прервано пользователем.", file=sys.stderr)
        return 130
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as error:
        print(f"Ошибка создания отчёта: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
