"""Routines: things the household does every so often (docs/ROUTINES-CONTRACT.md).

`catalog` is the deterministic interval table, `service` is the projection and
completion logic, `estimate` is the model fallback. Nothing in here imports
boto3 or the API layer; both the HTTP routes and the agent tools call in.
"""
