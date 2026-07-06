# Central microscope config store

One subdir per box, matching the names in `../config/microscopes.json`. Each holds
that box's `config.json` (seafront handle-key schema, e.g. `system.microscope_name`).
This is the **source of truth**; edit here and run `../scripts/push-config.sh <box>`
to sync it to the box (into `~/seafront/config.json`, which the seafront container
mounts) and restart the service.

PUSH model, not a network mount: each box keeps its own local copy, so it still boots
with its last-good config if the gateway or network is down. The previous config is
backed up on the box as `config.json.bak` before overwrite.

`config.json` files are gitignored (per-box hardware calibration); commit explicitly
if you want them versioned.

## Mock profile

A box's `config.json` `microscopes` array should carry **two** profiles: its real one
(placeholder USB IDs until hardware is wired) and the shared **mocroscope** profile
from `mocroscope.profile.json`. The seafront quadlet defaults to `--microscope
mocroscope`, so a freshly-flashed box runs entirely in mock mode with nothing attached.
Flip `SEAFRONT_MICROSCOPE` in the quadlet to the real profile once hardware is present.
