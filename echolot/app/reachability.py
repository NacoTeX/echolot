"""Can we reach a device on the network, and what is answering?

The question this exists to settle: a flashed device that Home Assistant
has not adopted looks exactly like a device that never joined the Wi-Fi.
Both show up as "nicht verfügbar" and neither tells you which it is. A TCP
probe separates them — if port 6053 answers, the device is alive and it is
Home Assistant's adoption that is missing, not the device.

Ports probed:
  6053   ESPHome's native API. The one Home Assistant connects to.
  80     The device's own status page, present when web_server is enabled.
  62587  ESPectre's Direct HTTP/SSE surface, present when direct_api is on.
"""

import asyncio
import socket

API_PORT = 6053
WEB_PORT = 80
DIRECT_PORT = 62587

#: Long enough for a sleepy ESP on a busy network, short enough that a
#: dead address does not stall the UI.
TIMEOUT = 3.0


async def _probe(host: str, port: int, timeout: float) -> bool:
    try:
        _, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    try:
        await writer.wait_closed()
    except OSError:
        pass
    return True


async def _resolve(host: str) -> str | None:
    """Resolve a hostname without blocking the event loop.

    `.local` names need mDNS, which a container often cannot do — telling
    "name does not resolve" apart from "host does not answer" is the
    difference between fixing DNS and fixing the network.
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, type=socket.SOCK_STREAM), timeout=TIMEOUT
        )
    except (OSError, asyncio.TimeoutError):
        return None
    return infos[0][4][0] if infos else None


async def check(host: str, timeout: float = TIMEOUT) -> dict:
    """Probe one device and describe what answered."""
    resolved = await _resolve(host)
    if resolved is None:
        return {
            "host": host,
            "resolved": None,
            "api": False,
            "web": False,
            "reachable": False,
            "verdict": "unresolved",
        }

    api, web, direct = await asyncio.gather(
        _probe(resolved, API_PORT, timeout),
        _probe(resolved, WEB_PORT, timeout),
        _probe(resolved, DIRECT_PORT, timeout),
    )
    return {
        "host": host,
        "resolved": resolved,
        "api": api,
        "web": web,
        "direct": direct,
        "reachable": api or web or direct,
        # The verdict turns on port 6053: that is the one Home Assistant
        # needs. The other two only tell us the device is alive.
        "verdict": "ok" if api else ("web_only" if (web or direct) else "silent"),
    }


#: What each verdict means, in the words the UI shows.
VERDICT_MESSAGES = {
    "unresolved": (
        "Der Name lässt sich nicht auflösen. Bei einem .local-Namen heißt das "
        "meist, dass mDNS nicht bis hierher durchkommt — trag stattdessen die "
        "IP-Adresse des Geräts ein."
    ),
    "silent": (
        "Die Adresse ist auflösbar, aber weder Port 6053 noch Port 80 antworten. "
        "Das Gerät ist entweder aus, hängt in einem anderen Netz, oder das WLAN "
        "trennt seine Clients voneinander (Client-Isolation)."
    ),
    "web_only": (
        "Das Gerät antwortet, aber nicht auf der ESPHome-API (Port 6053). "
        "Öffne http://{host}/ im Browser — dort steht, woran es hakt."
    ),
    "ok": (
        "Das Gerät antwortet auf der ESPHome-API. Wenn es in Home Assistant "
        "trotzdem fehlt, liegt es an der Übernahme, nicht am Netz: Einstellungen "
        "→ Geräte & Dienste → Integration hinzufügen → ESPHome, Host {host}, "
        "Port 6053."
    ),
}


def explain(result: dict) -> str:
    return VERDICT_MESSAGES[result["verdict"]].format(host=result.get("resolved") or result["host"])
