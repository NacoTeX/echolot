"""ESPHome component: HLK-LD2460 targets, as one line per report.

Echolot's own component rather than one of the community ones, for a
reason the entity model makes unavoidable: those publish each target's X
and Y as separate sensors, updated only when they change. The module
reports no target IDs, so anything that tracks people across frames
needs both coordinates of every target from the *same* report — and two
independently throttled sensors cannot promise that. The `frame` text
sensor below is that report, whole.

Next to the frame: the module's mounting — "side" or "top", height and
tilt angle — which the module keeps and uses itself. As a select and two
numbers whose state is only ever what the module reads back, so Home
Assistant and Echolot both see what the module holds, not what somebody
asked for.

It is also the only entity meant for Echolot rather than for Home
Assistant, and it is rendered `disabled_by_default` so the recorder does
not write ten rows a second: the add-on reads it straight from the
device over the native API, which delivers states of disabled entities
all the same.

See ld2460_protocol.h for the wire format and what is and is not known
about it.
"""

import esphome.codegen as cg
from esphome.components import number, select, sensor, text_sensor, uart
import esphome.config_validation as cv
from esphome.core import CORE
from esphome.const import (
    CONF_ID,
    ENTITY_CATEGORY_CONFIG,
    ENTITY_CATEGORY_DIAGNOSTIC,
    STATE_CLASS_MEASUREMENT,
    STATE_CLASS_TOTAL_INCREASING,
    UNIT_DEGREES,
    UNIT_METER,
)

CODEOWNERS = ["@NacoTeX"]
DEPENDENCIES = ["uart"]
AUTO_LOAD = ["sensor", "text_sensor", "select", "number"]
# One module per UART, and nothing against two modules on one board.
MULTI_CONF = True

CONF_FRAME = "frame"
CONF_STATUS = "status"
CONF_RADAR_FIRMWARE = "radar_firmware"
CONF_TARGET_COUNT = "target_count"
CONF_RX_BYTES = "rx_bytes"
CONF_REPORT_FRAMES = "report_frames"
CONF_EMPTY_FRAMES = "empty_frames"
CONF_REJECTED_FRAMES = "rejected_frames"
CONF_STALE_AFTER = "stale_after"
CONF_PROBE_INTERVAL = "probe_interval"
CONF_FRAME_INTERVAL = "frame_interval"
CONF_DIAGNOSTICS_INTERVAL = "diagnostics_interval"
CONF_QUIET_MEANS_EMPTY = "quiet_means_empty"
CONF_MOUNT_MODE = "mount_mode"
CONF_MOUNT_HEIGHT = "mount_height"
CONF_MOUNT_ANGLE = "mount_angle"

#: In the module's order: 1 is "side", 2 is "top" (ld2460_protocol.h).
MOUNT_MODES = ["side", "top"]
#: What the firmware writes; ld2460_protocol.h refuses anything else.
HEIGHT_RANGE = (0.5, 5.0, 0.01)
ANGLE_RANGE = (0.0, 90.0, 0.5)

echolot_ld2460_ns = cg.esphome_ns.namespace("echolot_ld2460")
EcholotLd2460 = echolot_ld2460_ns.class_("EcholotLd2460", cg.Component, uart.UARTDevice)
MountModeSelect = echolot_ld2460_ns.class_("MountModeSelect", select.Select, cg.Parented.template(EcholotLd2460))
MountNumber = echolot_ld2460_ns.class_("MountNumber", number.Number, cg.Parented.template(EcholotLd2460))


def _counter(icon):
    return sensor.sensor_schema(
        accuracy_decimals=0,
        state_class=STATE_CLASS_TOTAL_INCREASING,
        entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
        icon=icon,
    )


def _check_timing(config):
    # A probe answered once has to stay valid until the next probe can be
    # answered, or a healthy quiet module would flicker to "unknown"
    # between probes. classify() is given probe + stale for exactly this;
    # a probe interval shorter than the staleness would only mean probing
    # a module that is still in its grace period.
    if config[CONF_PROBE_INTERVAL] < config[CONF_STALE_AFTER]:
        raise cv.Invalid(f"{CONF_PROBE_INTERVAL} must not be shorter than {CONF_STALE_AFTER}")
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(EcholotLd2460),
            cv.Optional(CONF_STALE_AFTER, default="3s"): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_PROBE_INTERVAL, default="5s"): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_FRAME_INTERVAL, default="100ms"): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_DIAGNOSTICS_INTERVAL, default="60s"): cv.positive_time_period_milliseconds,
            cv.Optional(CONF_QUIET_MEANS_EMPTY, default=False): cv.boolean,
            cv.Optional(CONF_FRAME): text_sensor.text_sensor_schema(
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:radar",
            ),
            cv.Optional(CONF_STATUS): text_sensor.text_sensor_schema(
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:lan-connect",
            ),
            cv.Optional(CONF_RADAR_FIRMWARE): text_sensor.text_sensor_schema(
                entity_category=ENTITY_CATEGORY_DIAGNOSTIC,
                icon="mdi:chip",
            ),
            cv.Optional(CONF_TARGET_COUNT): sensor.sensor_schema(
                accuracy_decimals=0,
                state_class=STATE_CLASS_MEASUREMENT,
                icon="mdi:account-group",
            ),
            cv.Optional(CONF_RX_BYTES): _counter("mdi:download-network"),
            cv.Optional(CONF_REPORT_FRAMES): _counter("mdi:counter"),
            cv.Optional(CONF_EMPTY_FRAMES): _counter("mdi:numeric-0-box-outline"),
            cv.Optional(CONF_REJECTED_FRAMES): _counter("mdi:alert-circle-outline"),
            cv.Optional(CONF_MOUNT_MODE): select.select_schema(
                MountModeSelect, entity_category=ENTITY_CATEGORY_CONFIG, icon="mdi:wall"
            ),
            cv.Optional(CONF_MOUNT_HEIGHT): number.number_schema(
                MountNumber, entity_category=ENTITY_CATEGORY_CONFIG, icon="mdi:arrow-expand-vertical",
                unit_of_measurement=UNIT_METER,
            ),
            cv.Optional(CONF_MOUNT_ANGLE): number.number_schema(
                MountNumber, entity_category=ENTITY_CATEGORY_CONFIG, icon="mdi:angle-acute",
                unit_of_measurement=UNIT_DEGREES,
            ),
        }
    )
    .extend(uart.UART_DEVICE_SCHEMA)
    .extend(cv.COMPONENT_SCHEMA),
    _check_timing,
)


def FINAL_VALIDATE_SCHEMA(config):
    # Both directions on a chip: reports come in on RX, and the probe
    # that tells a quiet module from an absent one goes out on TX. On the
    # `host` platform a UART is a port rather than two pins, and ESPHome
    # refuses pins there — which is where tests/test_ld2460_component.py
    # runs this component against a pseudo-terminal.
    on_chip = not CORE.is_host
    return uart.final_validate_device_schema(
        "echolot_ld2460",
        baud_rate=115200,
        require_tx=on_chip,
        require_rx=on_chip,
        data_bits=8,
        parity="NONE",
        stop_bits=1,
    )(config)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await uart.register_uart_device(var, config)

    cg.add(var.set_stale_after(config[CONF_STALE_AFTER]))
    cg.add(var.set_probe_interval(config[CONF_PROBE_INTERVAL]))
    cg.add(var.set_frame_interval(config[CONF_FRAME_INTERVAL]))
    cg.add(var.set_diagnostics_interval(config[CONF_DIAGNOSTICS_INTERVAL]))
    cg.add(var.set_quiet_means_empty(config[CONF_QUIET_MEANS_EMPTY]))

    for key, setter in (
        (CONF_FRAME, "set_frame_text_sensor"),
        (CONF_STATUS, "set_status_text_sensor"),
        (CONF_RADAR_FIRMWARE, "set_radar_firmware_text_sensor"),
    ):
        if key in config:
            entity = await text_sensor.new_text_sensor(config[key])
            cg.add(getattr(var, setter)(entity))

    for key, setter in (
        (CONF_TARGET_COUNT, "set_target_count_sensor"),
        (CONF_RX_BYTES, "set_rx_bytes_sensor"),
        (CONF_REPORT_FRAMES, "set_report_frames_sensor"),
        (CONF_EMPTY_FRAMES, "set_empty_frames_sensor"),
        (CONF_REJECTED_FRAMES, "set_rejected_frames_sensor"),
    ):
        if key in config:
            entity = await sensor.new_sensor(config[key])
            cg.add(getattr(var, setter)(entity))

    if CONF_MOUNT_MODE in config:
        entity = await select.new_select(config[CONF_MOUNT_MODE], options=MOUNT_MODES)
        await cg.register_parented(entity, var)
        cg.add(var.set_mount_mode_select(entity))
    for key, (low, high, step), setter, is_angle in (
        (CONF_MOUNT_HEIGHT, HEIGHT_RANGE, "set_mount_height_number", False),
        (CONF_MOUNT_ANGLE, ANGLE_RANGE, "set_mount_angle_number", True),
    ):
        if key in config:
            entity = await number.new_number(config[key], min_value=low, max_value=high, step=step)
            await cg.register_parented(entity, var)
            cg.add(entity.set_is_angle(is_angle))
            cg.add(getattr(var, setter)(entity))
