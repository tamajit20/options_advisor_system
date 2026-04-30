# Options Advisor System

> **⚠️ BOUNDARY:** This is a **separate** system from the Stock Analyzer (equity) system in
> `../stock_analyzer_system/`. It has its own database (`OptionsAdvisorDB`), its own Docker
> container, its own port (`5001`), and **must not** import from the equity system.
> See [ARCHITECTURE.txt](ARCHITECTURE.txt) for the full boundary contract.

A rule-based F&O options trading advisor for the Indian market (NSE). Uses end-of-day
bhav copy data only. Generates high-confidence suggestions, manages full trade lifecycle
(suggestion → execution → tracking → exit), simulates ignored suggestions, and calculates
all Indian taxes/charges (brokerage, STT, exchange, SEBI, stamp duty, GST).

## Features

- **7-condition confidence gate** — every condition must pass; even 6/7 = no suggestion.
- **7-layer strategy selector** — IV Rank → Trend → PCR → OI walls → VIX → Expected Move → Viability.
- **Full lifecycle tracking** — suggestion → execution (full / paired-partial / naked) →
  daily HOLD/EXIT instructions → broken-trade advisor with ranked options → exit.
- **Auto trade names** — e.g., `NIFTY-CONDOR-MAY2-26`.
- **Charges calculator** — itemised brokerage, STT, exchange, SEBI, stamp duty, GST.
- **Forward simulation** — ignored suggestions tracked daily with `FULL_VALID` / `ADJUSTED` / `VOID` quality.
- **Date-wise history** — every suggestion (executed or simulated) preserved chronologically.
- **DB-based logging** — `options_system_logs` is the primary log store.
- **UI-editable config** — `options_config` table overrides defaults.
- **Mobile-first responsive dashboard** — distinct dark teal/emerald + amber color scheme.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Initialise database (creates OptionsAdvisorDB + all tables)
python main.py --init-db

# 3. Run scheduler + dashboard
python main.py
```

Dashboard: http://localhost:5001

## Folder Structure

See [ARCHITECTURE.txt](ARCHITECTURE.txt) for module dependency rules.

```
options_advisor_system/
├── ARCHITECTURE.txt        ← boundary contract
├── config.py               ← single source of truth
├── contracts.py            ← inter-module data shapes
├── main.py                 ← entry point
├── database/               ← DB connection + models + log repo
├── downloader/             ← NSE bhav, spot, VIX, FII downloaders
├── engine/                 ← IV, indicators, confidence, strategy, legs, charges
├── simulation/             ← shadow tracking of ignored suggestions
├── scheduler/              ← APScheduler jobs
├── dashboard/              ← Flask app + templates + static
└── alerts/                 ← dashboard + email notifications
```

## Engineering Standards (Mandatory)

1. **No hardcoded values** anywhere except `config.py`.
2. **Every DB op** uses explicit `try / commit / except / rollback / finally / close`.
3. **Every job** uses the `_job_started → success/failed → notify` pattern.
4. **DB-first logging** — `options_system_logs`. File logs are backup only.
5. **Strict module boundaries** — see ARCHITECTURE.txt.

## Disclaimer

This is a **decision-support tool**, not financial advice. All trade execution is manual
through Zerodha. The user is responsible for all trading decisions and outcomes. Options
trading carries significant risk of loss.
