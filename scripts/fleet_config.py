#!/usr/bin/env python3
"""Single source of truth for the fleet inventory (config/microscopes.json).

Everything that reads, validates, or writes the inventory goes through here so the
schema lives in ONE place. Pure standard library (json, ipaddress, pathlib) so both
the system python3 used by the shell scripts (via the CLI below) and the dashboard's
venv (via `import fleet_config`) can use it without extra dependencies.

Schema (config/microscopes.json):

    {
      "gateway": {
        "dashboard_port": 8000,
        "backbone": { "subnet": "192.168.50.0/24", "gateway_ip": "192.168.50.1" },
        "wifi": {
          "iface": "",                        # "" = auto-detect the radio; set to override
          "mode": "ap",                       # desired mode: "ap" | "client"
          "hotspot": { "ssid": "microscopes", "password": "microscope-lan" }
        }
      },
      "microscopes": [
        { "name": "squid1", "type": "squid",
          "host": "192.168.50.11", "seafront_port": 8000, "proxy_port": 8001 }
      ]
    }

`host` may be any IPv4 address inside the backbone subnet; `name` is any unique
DNS-safe label (no longer restricted to squid<n>). `type` is free-form metadata.

CLI (for the shell scripts):

    fleet_config.py validate                 # exit non-zero + reasons if invalid
    fleet_config.py get gateway.wifi.iface   # print a dotted-path scalar
    fleet_config.py names                     # space-joined scope names
    fleet_config.py host <name>               # a scope's IP ('' if unknown)
    fleet_config.py all-ports                 # dashboard_port + every proxy_port, one per line
    fleet_config.py proxy-ports               # every proxy_port, one per line
    fleet_config.py next-proxy-port           # lowest free proxy_port
    fleet_config.py add <name> <ip> [proxy_port] [--type T] [--seafront-port P]
    fleet_config.py remove <name>
    fleet_config.py set-host <name> <ip>
    fleet_config.py set <dotted.path> <value> # set a gateway.* scalar (e.g. wifi.mode)
"""
import ipaddress
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
CONFIG_PATH = ROOT / "config" / "microscopes.json"

DEFAULT_SUBNET = "192.168.50.0/24"
PROXY_PORT_BASE = 8001          # first auto-assigned proxy port
PROXY_PORT_RANGE = (8001, 8099)  # ports apply-config.sh manages in the firewall


# --- load / save ---------------------------------------------------------------
def load(path: pathlib.Path = CONFIG_PATH) -> dict:
    return json.loads(pathlib.Path(path).read_text())


def save(cfg: dict, path: pathlib.Path = CONFIG_PATH) -> None:
    """Validate then write atomically (temp file + rename on the same filesystem),
    so a crash mid-write never leaves a truncated inventory. Preserves the leading
    `_comment` key since it is just ordinary data that round-trips."""
    validate(cfg)
    p = pathlib.Path(path)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2) + "\n")
    tmp.replace(p)


# --- accessors -----------------------------------------------------------------
def backbone(cfg: dict) -> dict:
    return cfg.get("gateway", {}).get("backbone", {})


def subnet(cfg: dict) -> ipaddress.IPv4Network:
    net = ipaddress.ip_network(backbone(cfg).get("subnet", DEFAULT_SUBNET), strict=False)
    if not isinstance(net, ipaddress.IPv4Network):
        raise ValueError("backbone subnet must be IPv4")
    return net


def by_name(cfg: dict, name: str) -> dict | None:
    return next((m for m in cfg["microscopes"] if m["name"] == name), None)


def next_proxy_port(cfg: dict) -> int:
    used = {m["proxy_port"] for m in cfg["microscopes"]}
    used.add(cfg["gateway"]["dashboard_port"])
    p = PROXY_PORT_BASE
    while p in used:
        p += 1
    return p


# --- validation ----------------------------------------------------------------
def validate(cfg: dict) -> None:
    """Raise ValueError listing every problem. Enforces unique names / IPs /
    proxy_ports, IPs inside the backbone subnet and not equal to the gateway, and
    no proxy_port colliding with the dashboard port."""
    errs: list[str] = []
    try:
        net = subnet(cfg)
    except ValueError as e:
        raise ValueError(f"invalid backbone subnet: {e}") from e

    gw_ip = backbone(cfg).get("gateway_ip")
    dash = cfg.get("gateway", {}).get("dashboard_port")
    names: set[str] = set()
    hosts: set[ipaddress.IPv4Address] = set()
    pports: set[int] = set()

    for m in cfg.get("microscopes", []):
        name = m.get("name", "")
        if not name:
            errs.append("a microscope is missing 'name'")
        elif name in names:
            errs.append(f"duplicate name: {name}")
        names.add(name)

        try:
            ip = ipaddress.ip_address(m["host"])
        except (KeyError, ValueError):
            errs.append(f"{name}: invalid or missing host {m.get('host')!r}")
        else:
            if ip in hosts:
                errs.append(f"duplicate host: {ip}")
            hosts.add(ip)
            if ip not in net:
                errs.append(f"{name}: host {ip} is outside backbone {net}")
            if gw_ip and str(ip) == str(gw_ip):
                errs.append(f"{name}: host {ip} collides with the gateway ({gw_ip})")

        pp = m.get("proxy_port")
        if not isinstance(pp, int):
            errs.append(f"{name}: invalid or missing proxy_port {pp!r}")
        else:
            if pp in pports:
                errs.append(f"duplicate proxy_port: {pp}")
            pports.add(pp)
            if pp == dash:
                errs.append(f"{name}: proxy_port {pp} collides with dashboard_port")

    if errs:
        raise ValueError("invalid fleet config:\n  - " + "\n  - ".join(errs))


# --- mutations (used by the CLI; each validates via save()) --------------------
def add_scope(cfg: dict, name: str, host: str, proxy_port: int | None = None,
              seafront_port: int = 8000, type_: str = "squid") -> dict:
    if by_name(cfg, name) is not None:
        raise ValueError(f"scope already exists: {name}")
    entry = {
        "name": name,
        "type": type_,
        "host": host,
        "seafront_port": seafront_port,
        "proxy_port": proxy_port if proxy_port is not None else next_proxy_port(cfg),
    }
    cfg["microscopes"].append(entry)
    return entry


def remove_scope(cfg: dict, name: str) -> None:
    if by_name(cfg, name) is None:
        raise ValueError(f"no such scope: {name}")
    cfg["microscopes"] = [m for m in cfg["microscopes"] if m["name"] != name]


def set_host(cfg: dict, name: str, host: str) -> None:
    m = by_name(cfg, name)
    if m is None:
        raise ValueError(f"no such scope: {name}")
    m["host"] = host


# --- CLI -----------------------------------------------------------------------
def _print_scalar(cfg: dict, dotted: str) -> None:
    cur: object = cfg
    for key in dotted.split("."):
        if not isinstance(cur, dict) or key not in cur:
            sys.exit(f"no such config path: {dotted}")
        cur = cur[key]
    print(cur)


def _set_scalar(cfg: dict, dotted: str, value: str) -> None:
    # Only gateway.* scalars are settable this way (scope edits go through add/remove/
    # set-host, which validate structure). Coerce ints so ports stay numeric.
    if not dotted.startswith("gateway."):
        sys.exit("set only supports gateway.* paths")
    keys = dotted.split(".")
    cur = cfg
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
    coerced: object = value
    if value.isdigit():
        coerced = int(value)
    cur[keys[-1]] = coerced


def main(argv: list[str]) -> int:
    if not argv:
        sys.exit(__doc__)
    cmd, rest = argv[0], argv[1:]
    cfg = load()

    if cmd == "validate":
        validate(cfg)
        print("ok")
    elif cmd == "get":
        _print_scalar(cfg, rest[0])
    elif cmd == "names":
        print(" ".join(m["name"] for m in cfg["microscopes"]))
    elif cmd == "host":
        m = by_name(cfg, rest[0])
        print(m["host"] if m else "")
    elif cmd == "all-ports":
        print(cfg["gateway"]["dashboard_port"])
        for m in cfg["microscopes"]:
            print(m["proxy_port"])
    elif cmd == "proxy-ports":
        for m in cfg["microscopes"]:
            print(m["proxy_port"])
    elif cmd == "next-proxy-port":
        print(next_proxy_port(cfg))
    elif cmd == "add":
        name, host = rest[0], rest[1]
        proxy_port = None
        seafront_port = 8000
        type_ = "squid"
        positional = []
        i = 2
        while i < len(rest):
            if rest[i] == "--type":
                type_ = rest[i + 1]; i += 2
            elif rest[i] == "--seafront-port":
                seafront_port = int(rest[i + 1]); i += 2
            else:
                positional.append(rest[i]); i += 1
        if positional:
            proxy_port = int(positional[0])
        entry = add_scope(cfg, name, host, proxy_port, seafront_port, type_)
        save(cfg)
        print(f"added {entry['name']} {entry['host']} -> proxy :{entry['proxy_port']}")
    elif cmd == "remove":
        remove_scope(cfg, rest[0])
        save(cfg)
        print(f"removed {rest[0]}")
    elif cmd == "set-host":
        set_host(cfg, rest[0], rest[1])
        save(cfg)
        print(f"{rest[0]} host -> {rest[1]}")
    elif cmd == "set":
        _set_scalar(cfg, rest[0], rest[1])
        save(cfg)
        print(f"{rest[0]} -> {rest[1]}")
    else:
        sys.exit(f"unknown command: {cmd}\n{__doc__}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except ValueError as e:
        sys.exit(str(e))
