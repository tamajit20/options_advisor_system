"""
Custom exception hierarchy for the options advisor.

Three-tier model:
    RecoverableError → log + retry; the calling job should continue.
    JobFailure       → this job failed; mark FAILED in options_job_log,
                       notify user, downstream jobs depending on it SKIP.
    CriticalError    → infrastructure-level (DB unreachable, disk full);
                       email immediately, halt scheduler if needed.

Every layer should catch broad `Exception` only at the top of a job;
elsewhere prefer the specific subclass below.
"""

from __future__ import annotations


class OptionsAdvisorError(Exception):
    """Base class for all options-advisor errors."""


class RecoverableError(OptionsAdvisorError):
    """Transient error — retry is appropriate (network blip, lock, etc.)."""


class JobFailure(OptionsAdvisorError):
    """A scheduled job has failed but the system as a whole is still healthy."""


class CriticalError(OptionsAdvisorError):
    """Infrastructure-level failure (DB unreachable, config invalid, etc.)."""


class ConfigError(CriticalError):
    """Misconfiguration detected at startup or read time."""


class DataIntegrityError(JobFailure):
    """Downloaded data failed validation (missing columns, bad date, etc.)."""


class NoDataError(JobFailure):
    """Required upstream data is not available for the requested date."""


class StrategyVeto(OptionsAdvisorError):
    """Used INTERNALLY by the strategy selector to abort with a reason.

    NOT a failure — the system is correctly choosing not to suggest. Caught
    by the suggestion engine and converted to a `NoSuggestion` record.
    """


# ---------------------------------------------------------------------------
# Provider-layer errors
# ---------------------------------------------------------------------------
class ProviderError(JobFailure):
    """A market-data provider call failed in a way that doesn't necessarily
    require user intervention (network blip, rate limit, instrument missing).

    Callers should catch and fall back to the EOD provider when possible.
    """


class TokenExpiredError(CriticalError):
    """The provider's auth token is no longer valid (e.g. Kite 403 / TokenException).

    Distinct from `ProviderError` because it ALWAYS requires user action
    (re-login on dashboard) and must NEVER be retried automatically.
    """
