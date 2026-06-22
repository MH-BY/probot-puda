"""Post-processing for probot measurements.

* :mod:`pv_param` - photovoltaic J-V parameter extraction (light dependencies).
* :mod:`ht_potdep` - potentiation/depression fitting + Bayesian optimization.
  Imports torch/botorch/gpytorch, so it is imported lazily by the measurement
  routines and requires the ``analysis`` optional dependency group.
"""
