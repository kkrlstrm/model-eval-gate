"""Workload classes: what work you do, and which models could plausibly do it."""
from .cluster import (WorkloadClass, from_calls, from_harness, counterfactual,  # noqa: F401
                      summarize)
from .propose import fetch_catalog, propose, requirements, filter_capable  # noqa: F401
