"""Golden-test evaluation harness for the Always-On Ops Agent.

Scores an agent run (the produced ``issues/`` directory) against hand-derived
ground truth in ``golden.json``. The scoring path is fully deterministic and
makes no model calls; an optional ``live`` mode runs the agent first.
"""
