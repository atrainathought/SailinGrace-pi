# SailinGrace — Pi (boat-side appliance)

**Source of truth for the boat-side Raspberry Pi appliance.** Relay files
only: the signalk-server delta logger, signal discovery, the wifi-join
helper, the config UI, the UPS monitor, and setup/uninstall. This does
**not** contain the SailinGrace routing app — that runs on a laptop and
connects to this Pi over SignalK.

This repo is no longer generated from the main repo; it is the canonical
home for these files. The SailinGrace routing application lives at
<https://github.com/atrainathought/SailinGrace>.

Deploy:
    bash scripts/setup_pi.sh        # installs venv + deps + units, enables logger
                                    # then follow the printed signalk-server steps
Remove:
    sudo bash scripts/uninstall.sh --confirm

See [docs/install_rpi.md](docs/install_rpi.md) for the full hardware and
software bring-up playbook.
