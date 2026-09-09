"""The ESPectre commit Echolot builds against, and what it can do.

Kept together on purpose. "Which commit" and "what does that commit
measure" are one fact, and splitting them is how a capability becomes an
assumption: something claims a mode, nothing checks whether the firmware
under it was ever built to provide one.

Imported by both the device model and the builder, which is why it holds
no imports of its own from `app`.
"""

#: The ESPectre commit this Echolot release builds against.
#:
#: Pinned rather than tracking `main`, so a given Echolot version always
#: produces the same firmware base. Moving it is a deliberate act: bump
#: this, rebuild a device, and check it still senses.
#:
#: Since 0.13.6 CI links a real image for one board per instruction set
#: against exactly this commit (tools/compile_firmware.py), so "pinned"
#: now means verified as well as reproducible — for those two boards, on
#: CI's Linux runner, and not on hardware. See DOCS.md.
ESPECTRE_REF = "ce23b0b61b95b87a75f12681a0e576d8f3df5d1b"

#: What firmware built from that commit measures, stated rather than
#: assumed. These are Echolot's own field names, not ESPectre options: no
#: upstream release advertises them, and inventing the vocabulary here is
#: the point — a mode nothing reports is a mode nothing can select.
#:
#: **supports_router** — CSI taken from traffic between the access point
#: and this device. Everything Echolot has ever built does this, whether
#: the triggering traffic is generated on the device (`internal`) or fed
#: to it (`external`).
#:
#: **supports_peer_tx / supports_peer_rx** — a directed A→B radio link
#: between two sensors, the topology TOMMY describes. Both false, and not
#: as a placeholder: at this commit `csi_traffic_mode: external` is a way
#: to *supply* CSI-triggering traffic, and upstream's own docs describe
#: that traffic as UDP delivered through the access point. A packet sent
#: from A to B's IP address therefore still travels AP→B, and an IP
#: source address is no evidence of the immediate 802.11 sender — so the
#: frames B evaluates are not the A→B path. Espressif's esp-csi examples
#: show a two-chip link is buildable; that is a reason to prototype, not
#: a capability this firmware has.
#:
#: **peer_protocol_version** — which revision of that link a device
#: speaks, so two devices can be told apart from two devices that merely
#: both claim the feature. None while there is nothing to version.
CAPABILITIES: dict = {
    "supports_router": True,
    "supports_peer_tx": False,
    "supports_peer_rx": False,
    "peer_protocol_version": None,
}


def capabilities_for(manifest: dict | None) -> dict:
    """What the image described by this build manifest can do.

    A device flashed a year ago runs the firmware of a year ago, so the
    manifest is the authority for anything already built. `None` — never
    built, or built before 0.13.8 recorded this — falls back to the
    pinned commit: for an unbuilt device that is what the next build
    would produce, and for an older manifest every image Echolot has ever
    shipped measured against the router and nothing else.
    """
    recorded = (manifest or {}).get("firmware_capabilities")
    if not isinstance(recorded, dict):
        return dict(CAPABILITIES)
    return {**CAPABILITIES, **recorded}
