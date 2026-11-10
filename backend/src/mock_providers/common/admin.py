"""Admin routes for switching faults on and off. Never registered in production."""

from fastapi import FastAPI

from mock_providers.common.errors import ProviderError
from mock_providers.common.faults import FaultConfig, FaultInjector, FaultState


def register_admin_routes(app: FastAPI, faults: FaultInjector, app_env: str) -> None:
    if app_env == "production":
        return  # the routes simply do not exist, so they return 404

    @app.post("/_admin/faults", response_model=FaultState)
    async def set_faults(config: FaultConfig) -> FaultState:
        if config.mode not in faults.supported_modes:
            raise ProviderError(
                422,
                "FAULT_MODE_UNSUPPORTED",
                f"This provider does not support fault mode '{config.mode}'.",
                retryable=False,
            )
        return await faults.configure(config)

    @app.get("/_admin/faults", response_model=FaultState)
    async def get_faults() -> FaultState:
        return await faults.state()
