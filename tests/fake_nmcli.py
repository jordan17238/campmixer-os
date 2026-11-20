#!/usr/bin/env python3
"""A pretend nmcli for test_netconf.py. It keeps one Wi-Fi radio's state in a JSON file and logs every call."""
import json
import os
import sys

path = os.environ["FAKE_NM_STATE"]
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
state["calls"].append(args)
code = 0

if args[-2:] == ["device", "status"]:
    state["polls"] += 1
    if state.get("drop_at") and state["polls"] >= state["drop_at"]:
        state["state"], state["conn"] = "disconnected", ""
    print("eth0:ethernet:connected:campmixer-eth")
    print("lo:loopback:unmanaged:")
    if state.get("dev"):
        print("%s:wifi:%s:%s" % (state["dev"], state["state"], state["conn"]))
elif "down" in args:
    state["state"], state["conn"] = "disconnected", ""
elif "list" in args:
    for name in state["visible"]:
        print(name.replace("\\", "\\\\").replace(":", "\\:"))
elif "up" in args:
    profile = args[args.index("id") + 1]
    if profile in state.get("fail", []):
        code = 4
        print("Error: Connection activation failed: Secrets were required, but not provided.", file=sys.stderr)
    else:
        state["state"], state["conn"] = "connected", profile

with open(path, "w") as handle:
    json.dump(state, handle)
sys.exit(code)
