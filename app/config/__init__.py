"""Config layer: pydantic-settings loaders for env-sourced configuration.

All external URLs (Glific, Frappe, Redis) and secrets are read from env,
never hardcoded. See settings.py for the Settings model.
"""
