"""burnrate: blackbox SLO monitoring with error-budget burn-rate alerting."""

__version__ = "0.1.0"

from .config import Config, load_config
from .models import ProbeResult, Severity, SliKind, Slo, Target

__all__ = [
    "__version__",
    "Config",
    "load_config",
    "ProbeResult",
    "Severity",
    "SliKind",
    "Slo",
    "Target",
]
