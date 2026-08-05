"""Lambda entrypoint. Terraform points the API function at `airhead.handler.handler`."""

from __future__ import annotations

from mangum import Mangum

from airhead.api.app import app

handler = Mangum(app)
