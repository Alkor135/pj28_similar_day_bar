# pj28_similar_day_bar

Проект для тестирования стратегии торговли фьючерсами RTS и MIX на Московской
бирже по похожим дневным свечам.

Дневные свечи формируются из минутных данных с 21:00 включительно до 21:00
следующей календарной даты исключительно. Рассчитываются OHLCV, тело свечи
со знаком, верхний и нижний фитили в пунктах. Планируется анализировать похожие
свечи и изменение цены в пунктах на следующей свече.

## Текущее состояние

Подготовлено виртуальное окружение `.venv` на Python 3.14.8 и настроен его выбор
в VS Code. В окружении доступен `pip`; сторонние зависимости пока не установлены.

Реализован `MOEX_ISS_API_quote_downloader_m1_two_ticker/update_futures_m1_two_ticker.py`:
одна команда загружает минутные свечи двух ближайших к экспирации контрактов
каждого выбранного базового актива ISS. По умолчанию это RTS и MIX;
другие инструменты добавляются через `--assets` без изменения скрипта.
Данные сохраняются в отдельных SQLite-базах в
`C:\data_quote\m1_two_ticker`. Доступны повторная докачка пропусков и проверка
целостности сохранённых дней.

Запуск из корня проекта:

```powershell
.\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py
```

Пример загрузки RTS, MIX, BR и Si:

```powershell
.\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py --assets RTS MIX BR Si
```

`--assets` задаёт полный набор активов этого запуска. Для каждого создаётся
собственная база `<ASSET>_m1_two_ticker.db`; коды приводятся к верхнему
регистру, поэтому для `Si` файл называется `SI_m1_two_ticker.db`.

По умолчанию период начинается `2026-09-01` и заканчивается вчера по Москве.
Параметры, схема баз и примеры запуска описаны в
[README загрузчика](MOEX_ISS_API_quote_downloader_m1_two_ticker/README.md).

Реализован `daily_bars/build_daily_bars.py`: строит единый дневной ряд каждого
актива, выбирая для всего интервала ближайший контракт с экспирацией позже
даты окончания бара. В день экспирации весь бар, включая предыдущий вечер,
строится по следующей серии. Минуты разных тикеров никогда не смешиваются.

```powershell
.\.venv\Scripts\python.exe daily_bars\build_daily_bars.py
.\.venv\Scripts\python.exe daily_bars\build_daily_bars.py --start 2026-09-01 --end 2026-09-30
```

Исходные базы открываются только для чтения. Результаты сохраняются в
`data\d1\RTS_d1.db` и `data\d1\MIX_d1.db`. По умолчанию пересчитывается весь
доступный диапазон полных интервалов; `--start` и `--end` задают даты окончания
баров. Проверяются загрузка обеих исходных дат и количества минут; пустые
интервалы пропускаются. Повторный запуск атомарно заменяет запрошенный период.
Параметры, схема и правило перехода описаны в
[README дневных баров](daily_bars/README.md).

## Работа в VS Code

Откройте папку `C:\Users\Alkor\VSCode\pj28_similar_day_bar` в VS Code.
В `.vscode/settings.json` задан интерпретатор `.venv\Scripts\python.exe`
и включена активация окружения в новых терминалах расширения Python.

Если ранее в VS Code уже был выбран другой интерпретатор, выполните
`Python: Select Interpreter` и выберите `.venv\Scripts\python.exe`.

Команды для PowerShell из корня проекта:

```powershell
Set-Location 'C:\Users\Alkor\VSCode\pj28_similar_day_bar'
.\.venv\Scripts\Activate.ps1
python --version
python -m pip --version
```

Интерпретатор можно запускать напрямую без активации окружения:

```powershell
.\.venv\Scripts\python.exe --version
.\.venv\Scripts\python.exe -m pip --version
```

Для выхода из активированного окружения используйте `deactivate`.
Папка `.venv` исключена из Git через `.gitignore`.

## Структура

```text
pj28_similar_day_bar/
├── .venv/                                        # Виртуальное окружение
├── .vscode/
│   └── settings.json                             # Интерпретатор VS Code
├── MOEX_ISS_API_quote_downloader_m1_two_ticker/
│   ├── update_futures_m1_two_ticker.py             # Загрузка выбранных активов
│   └── README.md                                 # Параметры и схема баз
├── tests/                                        # Автоматические проверки
├── daily_bars/                                   # Агрегация 21:00–21:00
│   ├── build_daily_bars.py                        # Единый ряд каждого актива
│   └── README.md                                 # Параметры и схема результата
├── data/d1/                                      # Производные базы, вне Git
├── logs/                                         # Журналы последнего запуска
└── README.md
```

Тестовые скрипты находятся в `tests/` в корне проекта:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

Логи минутного загрузчика сохраняются в `logs/` в корне проекта с постоянными именами файлов,
перезаписью при запуске и ограничением размера без накопления архивов.
Агрегатор дневных баров выводит сообщения в консоль, файлов журналов не создаёт.
