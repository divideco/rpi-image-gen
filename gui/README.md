# rpi-image-gen GUI (PySide6)

Drop this `gui/` folder into the root of your `rpi-image-gen` repository.

Run from repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r gui/requirements.txt
python gui/build_gui.py
```

## New in this build ("next")
- Host architecture auto-detection
- If host is not arm64/aarch64, GUI enables "Cross-build env" by default
  - Adds common ARM64 cross toolchain environment variables to the launched `rpi-image-gen` process
  - Shows warnings if qemu-user-static binaries are missing

Note:
- Cross-building still requires binfmt_misc + qemu-user-static on the host for running ARM binaries in chroot.
- rpi-image-gen notes non-arm64 hosts run via QEMU emulation but are not formally supported.

## Examples → Profiles
- Click **Import upstream examples → profiles** to generate GUI profiles for each folder in `examples/`.

## New in this build (deep integration step)
- Examples browser tab (scans ./examples)
- Robust YAML parsing for device.layer + image.layer (no PyYAML dependency)
- When a config is selected, GUI shows detected layers and can auto-align Target/Layout
- Example import now uses parsed layers (not heuristics)

## New in this build (layer integration)
- New **Layers** tab (runs `rpi-image-gen layer --list/--describe` with current -S)
- Profile has **Extra layers** table (maps to IGconf_layer_* overrides)
- Overrides now accept `section.key=value` OR `IGconf_section_key=value` and are normalized on build
- Device/Image layer overrides use IGconf_device_layer / IGconf_image_layer (correct upstream behavior)
