# pj28_similar_day_bar

Проект для тестирования стратегии торговли фьючерсами RTS и MIX на Московской
бирже по похожим дневным свечам.

Планируется рассчитывать тело свечи, верхний и нижний фитили, объём, направление
движения и изменение цены в пунктах на следующей свече. Дневные свечи будут
формироваться из минутных данных с заданной границей торгового дня, например
с 21:00 одного дня до 21:00 следующего торгового дня.

## Текущее состояние

Подготовлено виртуальное окружение `.venv` на Python 3.14.8 и настроен его выбор
в VS Code. В окружении доступен `pip`; сторонние зависимости пока не установлены.

Реализован `MOEX_ISS_API_quote_downloader_m1_two_ticker/update_futures_m1_two_ticker.py`:
одна команда загружает минутные свечи двух ближайших к экспирации контрактов
каждого актива RTS и MIX. Данные сохраняются в отдельных SQLite-базах в
`C:\data_quote\m1_two_ticker`. Доступны повторная докачка пропусков и проверка
целостности сохранённых дней.

Запуск из корня проекта:

```powershell
.\.venv\Scripts\python.exe MOEX_ISS_API_quote_downloader_m1_two_ticker\update_futures_m1_two_ticker.py
```

По умолчанию период начинается `2026-09-01` и заканчивается вчера по Москве.
Параметры, схема баз и примеры запуска описаны в
[README загрузчика](MOEX_ISS_API_quote_downloader_m1_two_ticker/README.md).

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
│   ├── update_futures_m1_two_ticker.py             # Загрузка обоих активов
│   └── README.md                                 # Параметры и схема баз
├── tests/                                        # Автоматические проверки
├── logs/                                         # Журналы последнего запуска
└── README.md
```

Тестовые скрипты находятся в `tests/` в корне проекта:

```powershell
.\.venv\Scripts\python.exe -X utf8 -m unittest discover -s tests -v
```

Логи сохраняются в `logs/` в корне проекта с постоянными именами файлов,
перезаписью при запуске и ограничением размера без накопления архивов.
