"""The LWIP socket budget for ESPectre's direct HTTP/SSE server.

ESPHome sizes CONFIG_LWIP_MAX_SOCKETS from the components that call
consume_sockets() during validation and logs the itemised result. An
external component takes no part in that count, so ESPectre's direct
server — up to max_event_clients + 5 = 7 sockets from the same shared
pool — is invisible to it. The overcommit only shows up under load, and
the first casualty is the SSE stream the Calibration Lab records from.
"""

import os
import sys
import tempfile
from pathlib import Path

import yaml

os.environ.setdefault("ECHOLOT_DATA_DIR", tempfile.mkdtemp(prefix="echolot-tests-"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import builder  # noqa: E402
from app.board_registry import BOARDS  # noqa: E402
from app.devices import BuildStatus, Device, DeviceCreate  # noqa: E402

OPTION = "CONFIG_LWIP_MAX_SOCKETS"

#: ESPHome's own worst case observed in a real build log (TCP 11 + UDP 3 +
#: TCP_LISTEN 3, with web_server and captive_portal on).
ESPHOME_WORST_CASE = 17
#: direct_http_service_esp_idf.cpp: max_open_sockets = max_event_clients + 5,
#: and max_event_clients is rejected above 2.
ESPECTRE_DIRECT_SOCKETS = 7


def device(board: str = "esp32c6", *, direct_api: bool = True) -> Device:
    dev = Device(
        id="probe",
        created_at=1,
        updated_at=2,
        status=BuildStatus.IDLE,
        config=DeviceCreate(
            name="probe",
            board=board,
            wifi_ssid="netz",
            wifi_password="passwort123",
            direct_api=direct_api,
        ),
    )
    dev.api_encryption_key = "k" * 44
    dev.ota_password = "p" * 32
    return dev


def framework(dev: Device) -> dict:
    return yaml.safe_load(builder.render_yaml(dev))["esp32"]["framework"]


def test_the_budget_covers_espectres_server_on_top_of_esphomes_own():
    options = framework(device())["sdkconfig_options"]
    assert int(options[OPTION]) >= ESPHOME_WORST_CASE + ESPECTRE_DIRECT_SOCKETS


def test_a_device_without_the_direct_api_is_left_to_esphome():
    """Nothing unbudgeted is running, so overriding the calculation would
    only risk falling behind it later."""
    assert OPTION not in framework(device(direct_api=False)).get("sdkconfig_options", {})


def test_every_board_renders_valid_yaml_with_the_option():
    for board in BOARDS:
        options = framework(device(board))["sdkconfig_options"]
        assert options[OPTION] == "24", board


def test_the_value_is_a_string():
    """ESP-IDF sdkconfig values are strings; an int reaches the generated
    sdkconfig as `y`/`n` handling rather than a number."""
    assert isinstance(framework(device())["sdkconfig_options"][OPTION], str)
