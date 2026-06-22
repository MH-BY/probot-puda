"""Main entry point for the stage-probot machine edge service.

PUDA edge service for the probot Ender 3-axis "ProbeBot" stage. Mirrors the Vipsa
machine-template ``main.py``. Exposes the stage primitives (cell_coordinates,
move_to, move_to_cell, probe/unprobe, move_to_cell1/81, move_to_safeposition) and
publishes the live stage position as telemetry.
"""

import asyncio
import logging
import sys
import time
from pathlib import Path

import psutil
from pydantic_settings import BaseSettings, SettingsConfigDict
from puda import EdgeNatsClient, EdgeRunner

from probot_drivers import StageProbot

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
    stage_port: str = "COM3"
    stage_safe_height: float = 80

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
    driver = StageProbot(
        port=config.stage_port,
        safe_height=config.stage_safe_height,
    )
    driver.startup()
    logger.info("Machine driver initialized successfully")

    logger.info("Connecting to NATS at %s", config.nats_servers)
    edge_nats_client = EdgeNatsClient(
        servers=config.nats_server_list,
        machine_id=config.machine_id,
    )

    async def telemetry_handler():
        await edge_nats_client.publish_heartbeat()
        await edge_nats_client.publish_position(driver.get_position())
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
