# Central microscope config store

One subdir per scope, matching the names in `../config/microscopes.json`. Each
holds that box's `config.json` (current seafront handle-key schema, e.g.
`system.microscope_name`). This is the **source of truth**; edit here (or in the
dashboard) and run `../scripts/push-config.sh <scope>` to sync it to the box and
restart seafront.

This is a PUSH model, not a network mount: each scope keeps its own local copy,
so it still boots with its last-good config if the gateway or network is down.

`config.json` files are gitignored by default (they hold per-box hardware
calibration); commit them explicitly if you want them versioned.
