"""Known ESP32 boards for Echolot firmware builds.

The classic ESP32 is selected by `board:`, every other chip by
`variant:`, and none of them pin a framework version — ESPHome's
recommended one is the right default, and pinning it only produces
warnings. `radar_uart_pins` is the (TX, RX) pair suggested for the
HLK-LD2460: pins that are free on the common dev boards and not strapping
pins.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Board:
    key: str
    label: str
    variant: str | None  # ESP-IDF variant; None for the classic ESP32
    chip_family: str  # what ESP Web Tools calls it in a manifest
    esphome_board: str | None = None  # only where a board id is needed
    # Not every chip reaches 240MHz — the C3 and C6 top out lower, and
    # asking for more is a hard config error. None means "leave it to
    # ESPHome".
    cpu_frequency: str | None = None
    experimental: bool = False
    #: Two radios to choose between, so the band is a decision somebody
    #: has to make. Only the C5 has them, and ESPHome enforces that: its
    #: `wifi.band_mode` is declared
    #: `only_on_variant(supported=[VARIANT_ESP32C5])`, so rendering the
    #: key for any other chip is a config error rather than an ignored
    #: line.
    dual_band: bool = False
    #: Where a radar module's UART goes when nobody says otherwise, as
    #: (ESP TX, ESP RX). A suggestion, not a fact about any particular
    #: board: on the chips whose logger runs over the built-in USB
    #: (ESPHome's default for C3, C5, C6 and S3) these are the chip's
    #: UART0 pins, which are then free; on the classic ESP32, whose logger
    #: keeps UART0, they are the pins UART2 is usually routed to. Only the
    #: C5 pair has been wired to a module — on a Waveshare ESP32-C5-Zero,
    #: running the wohnzimmer-radar prototype. Every other pair is a
    #: default to check against the board's pinout.
    radar_uart_pins: tuple[int, int] | None = None

    @property
    def toolchain_package(self) -> str:
        """PlatformIO package holding this chip's cross compiler.

        Mirrors the split in platform-espressif32's own espidf.py: the
        Xtensa cores get one toolchain, every RISC-V core the other. Our
        board keys are the ESP-IDF MCU names, so the test is the same one
        the builder makes.
        """
        return (
            "toolchain-xtensa-esp-elf"
            if self.key in ("esp32", "esp32s2", "esp32s3")
            else "toolchain-riscv32-esp"
        )

    @property
    def compiler_binary(self) -> str:
        """The gcc that must exist inside the toolchain package's bin/.

        PlatformIO only checks that the toolchain *directory* exists before
        putting its bin/ on PATH — never that a compiler is in it. So a
        half-extracted download passes that check and fails much later
        inside CMake. Naming the binary lets us check what PlatformIO does
        not.
        """
        return (
            "xtensa-esp-elf-gcc"
            if self.toolchain_package == "toolchain-xtensa-esp-elf"
            else "riscv32-esp-elf-gcc"
        )


BOARDS: dict[str, Board] = {
    b.key: b
    for b in [
        Board("esp32", "ESP32 (classic)", None, "ESP32",
              esphome_board="esp32dev", cpu_frequency="240MHz",
              radar_uart_pins=(17, 16)),
        Board("esp32c3", "ESP32-C3", "ESP32C3", "ESP32-C3", radar_uart_pins=(21, 20)),
        Board("esp32c5", "ESP32-C5", "ESP32C5", "ESP32-C5", cpu_frequency="240MHz",
              dual_band=True, radar_uart_pins=(11, 12)),
        Board("esp32c6", "ESP32-C6", "ESP32C6", "ESP32-C6", radar_uart_pins=(16, 17)),
        Board("esp32s2", "ESP32-S2", "ESP32S2", "ESP32-S2",
              cpu_frequency="240MHz", radar_uart_pins=(17, 18)),
        Board("esp32s3", "ESP32-S3", "ESP32S3", "ESP32-S3", cpu_frequency="240MHz",
              radar_uart_pins=(43, 44)),
    ]
}


def get_board(key: str) -> Board:
    try:
        return BOARDS[key]
    except KeyError:
        raise ValueError(f"Unbekanntes Board '{key}'. Bekannt sind: {', '.join(BOARDS)}") from None
