"""
lifecycle/
==========

Orchestration package — wires downloader + engine + database together.

Module breakdown:
    download_orchestrator : pulls EOD data into DB
    iv_orchestrator       : computes IVs and IV ranks per underlying/expiry
    suggestion_engine     : generates daily suggestion or NoSuggestion
    trade_executor        : marks suggestion as executed (full/paired/naked)
    resuggestion_engine   : creates a resuggestion when a trade goes broken
    exit_orchestrator     : runs daily exit-decision check on open trades

Boundary: this is the ONLY layer allowed to import from BOTH
`engine` and `database`/`downloader`.
"""
