"""Main entry point for the probot-smu-keysight machine edge service.

PUDA edge service for the probot Keysight SMU + Pico light (co-located because
several measurements drive the light inline during the SMU acquisition). The
stage is a separate edge service. Mirrors the Vipsa machine-template ``main.py``.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import psutil
from pydantic_settings import BaseSettings, SettingsConfigDict
from puda import EdgeNatsClient, EdgeRunner

from probot_drivers import SMUKeysightProbotMachine

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    force=True,
)
logging.getLogger("probot_drivers").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)


class Config(BaseSettings):
    machine_id: str
    nats_servers: str
    # Keysight SMU: explicit VISA address preferred; device_no is the fallback index.
    keysight_address: str | None = None
    keysight_device_no: int = 0
    # Pico G2V light controller.
    pico_ip: str | None = None
    pico_id: str | None = None
    # Writable parameter / data directories (defaults inside the machine if unset).
    param_dir: str | None = None
    data_dir: str | None = None

    model_config = SettingsConfigDict(
        env_file=Path(__file__).resolve().parent / ".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    @property
    def nats_server_list(self) -> list[str]:
        return [s.strip() for s in self.nats_servers.split(",") if s.strip()]


def load_config() -> Config:
    try:
        return Config()
    except Exception as e:
        logger.error("Failed to load configuration: %s", e, exc_info=True)
        sys.exit(1)


async def main():
    config = load_config()
    logger.info("Config loaded for %s", config.machine_id)
    logger.info("Full config: %s", config.model_dump())

    logger.info("Initializing machine driver")
    driver = SMUKeysightProbotMachine(
        smu_address=config.keysight_address,
        smu_device_no=config.keysight_device_no,
        pico_ip=config.pico_ip,
        pico_id=config.pico_id,
        param_dir=config.param_dir,
        data_dir=config.data_dir,
    )
    driver.startup()
    logger.info("Machine driver initialized successfully")

    logger.info("Connecting to NATS at %s", config.nats_servers)
    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    async def telemetry_handler():
        # The SMU/light machine has no spatial position, so only heartbeat + health.
        await edge_nats_client.publish_heartbeat()
        sensor = None
        if hasattr(psutil, "sensors_temperatures"):
            all_temps = psutil.sensors_temperatures() or {}
            sensor = next(
                (v[0] for k in ("coretemp", "cpu_thermal", "k10temp", "acpitz") if (v := all_temps.get(k))),
                None,
            )
        await edge_nats_client.publish_health({
            "cpu": psutil.cpu_percent(interval=None),
            "mem": psutil.virtual_memory().percent,
            "temp": sensor.current if sensor else None,
        })

    runner = EdgeRunner(
        nats_client=edge_nats_client,
        machine_driver=driver,
        telemetry_handler=telemetry_handler,
        state_handler=lambda: {},
    )
    await runner.connect()
    logger.info("NATS client initialized successfully")
    logger.info(
        "==================== %s Edge Service Ready. Publishing telemetry... ====================",
        config.machine_id,
    )
    await runner.run()


if __name__ == "__main__":
    while True:
        try:
            asyncio.run(main())
        except KeyboardInterrupt:
            logger.warning("Gracefully stopping...")
            sys.exit(0)
        except Exception as e:
            logger.error("Fatal error: %s", e, exc_info=True)
            time.sleep(5)
