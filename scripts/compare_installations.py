#!/usr/bin/env python3
"""Compare `cool_open_integration` across two Home Assistant installations.

Built as an A/B: one installation running the patched build (WebSocket pointed
at `ws.coolremote.net`) against one still on the released build (pointed at
`api.coolremote.net`, where the handshake 404s forever). It answers one
question with evidence rather than opinion — **is push actually working there,
or is the 5-minute reconciliation poll carrying the entities on its own?**

The tell is cadence. With push, units arrive independently, seconds apart. With
the poll alone, nothing moves between one 5-minute tick and the next. So the
script samples `last_updated` on both installations over the same window and
compares the gaps, alongside the integration version, the config entry state,
the entity counts and any `cool_open_client` entries in the system log.

Read-only throughout: it never calls a service and never writes to either HA.

Reads `HA_<NAME>_URL` / `HA_<NAME>_TOKEN` from `Controlá/.env`.

Usage:
    python3 scripts/compare_installations.py
    python3 scripts/compare_installations.py --seconds 600
    python3 scripts/compare_installations.py --installations DEMO2 SERCOTEL
    python3 scripts/compare_installations.py --no-sample     # inventory only
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONTROLA_ROOT = REPO_ROOT.parent
sys.path.insert(0, str(CONTROLA_ROOT / "scripts"))

try:
    from ha_ws_call import get_installation, load_env, ws_call
except ImportError:  # pragma: no cover - depends on the sibling checkout
    print(
        f"No encuentro ha_ws_call.py en {CONTROLA_ROOT / 'scripts'}. "
        "Este script reutiliza sus helpers de .env y WebSocket.",
        file=sys.stderr,
    )
    raise SystemExit(1)

DOMAIN = "cool_open_integration"
LIBRARY_LOGGER = "cool_open_client"
RECONCILE_SECONDS = 300

# Cloudflare fronts *.controla.cloud and rejects urllib's User-Agent with a
# 403 "error code: 1010". curl's UA sails through.
HTTP_HEADERS = {"User-Agent": "curl/8.5.0"}


def _get_json(inst: dict[str, str], path: str, timeout: int = 45):
    request = urllib.request.Request(
        f"{inst['url']}{path}",
        headers={"Authorization": f"Bearer {inst['token']}", **HTTP_HEADERS},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _safe_ws(inst: dict[str, str], command: str, payload: dict):
    try:
        return ws_call(inst, command, payload, quiet=True).get("result")
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001 - diagnostics must not die here
        return {"__error__": str(error)}


def inventory(name: str, inst: dict[str, str]) -> dict:
    """Everything obtainable without waiting: version, entry, entities, log."""
    facts: dict = {"name": name, "url": inst["url"]}

    manifest = _safe_ws(inst, "manifest/get", {"integration": DOMAIN})
    if isinstance(manifest, dict) and "__error__" not in manifest:
        facts["version"] = manifest.get("version")
        requirements = manifest.get("requirements") or []
        facts["library"] = next(
            (r for r in requirements if "cool" in r.lower()), None
        )
    else:
        facts["version"] = None
        facts["library"] = None

    entries = _safe_ws(inst, "config_entries/get", {})
    facts["entries"] = []
    if isinstance(entries, list):
        facts["entries"] = [
            {"entry_id": e.get("entry_id"), "state": e.get("state"),
             "reason": e.get("reason")}
            for e in entries
            if e.get("domain") == DOMAIN
        ]

    try:
        states = _get_json(inst, "/api/states")
    except Exception as error:  # noqa: BLE001
        facts["error"] = f"no pude leer /api/states: {error}"
        return facts

    entity_ids = _cool_entities(states)
    facts["entities"] = len(entity_ids)
    facts["unavailable"] = sum(
        1 for e in states
        if e["entity_id"] in entity_ids and e["state"] in ("unavailable", "unknown")
    )

    log = _safe_ws(inst, "system_log/list", {})
    facts["log"] = []
    if isinstance(log, list):
        for entry in log:
            if LIBRARY_LOGGER in (entry.get("name") or ""):
                message = (entry.get("message") or [""])[0]
                facts["log"].append(
                    {"level": entry.get("level"), "count": entry.get("count"),
                     "message": message[:160]}
                )
    return facts


def _cool_entities(states: list[dict]) -> set[str]:
    """Identify this integration's climate entities by their device/unit names.

    The entity_ids are derived from CoolAutomation unit names, which differ per
    installation, so match on the CoolAutomation naming rather than a fixed
    prefix — and exclude the locally simulated `demo_demo_*` ones.
    """
    found = set()
    for state in states:
        entity_id = state["entity_id"]
        if not entity_id.startswith("climate."):
            continue
        if "demo_demo" in entity_id or "numa_" in entity_id:
            continue
        attributes = state.get("attributes") or {}
        if "CoolAutomations" == attributes.get("attribution"):
            found.add(entity_id)
            continue
        # Fall back to the naming CoolAutomation hands out for these sites.
        if "sercotel" in entity_id or entity_id.startswith("climate.l1_"):
            found.add(entity_id)
    return found


def sample_cadence(targets: dict[str, dict], seconds: int, interval: int = 15) -> dict:
    """Watch `last_updated` on every installation over one shared window."""
    print(
        f"\nMuestreando {seconds}s (cada {interval}s). "
        f"El poll de reconciliacion es de {RECONCILE_SECONDS}s, "
        "asi que los huecos por debajo de eso solo pueden venir del push.",
        file=sys.stderr,
    )
    previous: dict[str, dict[str, str]] = {}
    moments: dict[str, list[float]] = {name: [] for name in targets}

    for name, inst in targets.items():
        try:
            previous[name] = _snapshot(inst)
        except Exception as error:  # noqa: BLE001
            print(f"  {name}: no pude tomar la muestra inicial: {error}", file=sys.stderr)
            previous[name] = {}

    deadline = time.time() + seconds
    while time.time() < deadline:
        time.sleep(min(interval, max(1, deadline - time.time())))
        for name, inst in targets.items():
            try:
                current = _snapshot(inst)
            except Exception as error:  # noqa: BLE001
                print(f"  {name}: sondeo fallido ({error})", file=sys.stderr)
                continue
            moved = [k for k, v in current.items() if previous[name].get(k) != v]
            if moved:
                moments[name].append(time.time())
                stamp = datetime.now().strftime("%H:%M:%S")
                shown = ", ".join(m.split(".", 1)[1][:26] for m in moved[:3])
                print(
                    f"  {stamp}  {name:<9} {len(moved):>2} entidad(es): {shown}",
                    file=sys.stderr,
                )
            previous[name] = current
    return moments


def _snapshot(inst: dict[str, str]) -> dict[str, str]:
    states = _get_json(inst, "/api/states")
    wanted = _cool_entities(states)
    return {e["entity_id"]: e["last_updated"] for e in states if e["entity_id"] in wanted}


def verdict(name: str, facts: dict, instants: list[float], sampled: bool) -> str:
    # `system_log` only keeps the first line, so the WSServerHandshakeError
    # itself sits in the traceback we cannot see. The loop's own summary line
    # ("WS subscription error; reconnecting in Ns") is the reliable signature.
    broken = [
        entry for entry in facts.get("log", [])
        if any(
            marker in entry["message"].lower()
            for marker in ("handshake", "404", "reconnecting", "subscription error")
        )
    ]
    if broken:
        return (
            f"WS ROTO — bucle de reconexion (count={broken[0]['count']}): "
            f"{broken[0]['message'][:60]}"
        )
    if not sampled:
        return "sin muestrear"
    if len(instants) < 2:
        return f"INCONCLUSO — solo {len(instants)} cambio(s) en la ventana"
    gaps = [instants[i] - instants[i - 1] for i in range(1, len(instants))]
    shortest = min(gaps)
    median = statistics.median(gaps)
    if shortest < RECONCILE_SECONDS * 0.8:
        return (
            f"PUSH ACTIVO — {len(instants)} cambios, hueco minimo {shortest:.0f}s, "
            f"mediana {median:.0f}s"
        )
    return (
        f"SOLO POLL — {len(instants)} cambios, todos a cadencia de "
        f"{median:.0f}s (~{RECONCILE_SECONDS}s)"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--installations", nargs="+", default=["DEMO2", "SERCOTEL"])
    parser.add_argument(
        "--seconds", type=int, default=360,
        help="ventana de muestreo; necesita cubrir al menos un poll de 5 min "
             "para que una instalacion sin push no salga como INCONCLUSO",
    )
    parser.add_argument("--interval", type=int, default=15)
    parser.add_argument("--no-sample", action="store_true",
                        help="solo inventario, sin esperar")
    args = parser.parse_args()

    env = load_env(CONTROLA_ROOT / ".env")
    targets = {name.upper(): get_installation(env, name) for name in args.installations}

    facts = {}
    for name, inst in targets.items():
        print(f"Inventariando {name}...", file=sys.stderr)
        facts[name] = inventory(name, inst)

    moments = {name: [] for name in targets}
    if not args.no_sample:
        moments = sample_cadence(targets, args.seconds, args.interval)

    print("\n" + "=" * 78)
    print(f"{'':<22}" + "".join(f"{n:<28}" for n in targets))
    print("=" * 78)

    def row(label: str, render) -> None:
        print(f"{label:<22}" + "".join(f"{str(render(facts[n])):<28}" for n in targets))

    row("version", lambda f: f.get("version") or "?")
    row("libreria", lambda f: f.get("library") or "?")
    row("entry", lambda f: (f["entries"][0]["state"] if f.get("entries") else "SIN ENTRY"))
    row("entidades", lambda f: f.get("entities", "?"))
    row("unavailable", lambda f: f.get("unavailable", "?"))
    row("log cool_open_client", lambda f: f"{len(f.get('log', []))} entrada(s)")
    print("-" * 78)
    for name in targets:
        print(f"{name:<22}{verdict(name, facts[name], moments[name], not args.no_sample)}")
    print("=" * 78)

    for name in targets:
        for entry in facts[name].get("log", []):
            print(f"\n{name} [{entry['level']}] x{entry['count']}: {entry['message']}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
