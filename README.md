# SailinGrace — Pi relay bundle

Boat-side **relay** files only: the signalk-server delta logger, the UPS
monitor, and setup/uninstall. This does **not** contain the SailinGrace
routing app — that runs on a laptop and connects to this Pi over SignalK.

Deploy:
    bash scripts/setup_pi.sh        # installs venv + deps + units, enables logger
                                    # then follow the printed signalk-server steps
Remove:
    sudo bash scripts/uninstall.sh --confirm
