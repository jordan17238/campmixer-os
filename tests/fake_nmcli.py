#!/usr/bin/env python3
"""A pretend nmcli (and iw) for test_netconf.py.

It keeps one Wi-Fi radio's state in a JSON file and logs every call. Settings in that file:
  dev, state, conn     what "device status" reports for the radio
  visible              network names a scan finds
  fail                 profiles that cannot be brought up
  drop_at              "device status" call number at which the connection is lost
  timeline             {"call number": {"state": ..., "conn": ..., "visible": ..., "stations": ...}} changes over time
  scan_refusals        how many scans NetworkManager refuses first
  stations             phones on the own network ("?" makes iw fail)
"""
import json
import os
import sys

path = os.environ["FAKE_NM_STATE"]
with open(path) as handle:
    state = json.load(handle)
args = sys.argv[1:]
code = 0

if args[:1] == ["dev"] and args[2:] == ["station", "dump"]:  # called as iw
    state["iw_calls"] = state.get("iw_calls", 0) + 1
    if state.get("stations", 0) == "?":
        code = 237
    else:
        for index in range(state.get("stations", 0)):
            print("Station 02:00:00:00:00:%02x (on %s)" % (index, args[1]))
            print("\tinactive time:\t20 ms")
elif args[-2:] == ["device", "status"]:
    state["calls"].append(args)
    state["polls"] += 1
    state.update(state.get("timeline", {}).get(str(state["polls"]), {}))
    if state.get("drop_at") and state["polls"] >= state["drop_at"]:
        state["state"], state["conn"] = "disconnected", ""
        state["drop_at"] = 0
    print("eth0:ethernet:connected:campmixer-eth")
    print("lo:loopback:unmanaged:")
    if state.get("dev"):
        print("%s:wifi:%s:%s" % (state["dev"], state["state"], state["conn"]))
else:
    state["calls"].append(args)
    if "down" in args:
        if state["conn"] == args[args.index("id") + 1]:
            state["state"], state["conn"] = "disconnected", ""
        else:
            code = 10
            print("Error: '%s' is not an active connection." % args[args.index("id") + 1], file=sys.stderr)
    elif "list" in args:
        if state.get("scan_refusals", 0) > 0:
            state["scan_refusals"] -= 1
            code = 1
            print("Error: Scanning not allowed while unavailable or activating", file=sys.stderr)
        else:
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
