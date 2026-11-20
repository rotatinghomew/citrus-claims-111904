"""Mixed-load shortage reconciliation.

Public entry points::

    from recon import reconcile, render
    bundle = reconcile()                  # latest visible documents
    bundle = reconcile(as_of=datetime)    # replay an earlier review
    text = render(bundle)                 # markdown report
"""
from .engine import reconcile, Reconciliation

__all__ = ["reconcile", "Reconciliation"]
__version__ = "0.1.0"
