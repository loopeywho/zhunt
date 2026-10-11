"""Shared routing core for Zhunt."""

import os

# LiteLLM downloads its model-price table from GitHub on import, which would tell a
# third party that this machine runs it. Zhunt prices nothing from that table, so
# use LiteLLM's bundled copy unless the user explicitly opts back in.
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")

__version__ = "0.2.0.dev0"
