# Third-party notices

The original source in this project is licensed under **GNU GPL version 2 or, at your option, any later version (GPL-2.0-or-later)**. See `LICENSE`. Existing notices in external or derived files remain applicable. This notice does not change the licenses of third-party components.

## Keychron / QMK firmware

The firmware is based on [Keychron's QMK fork, commit 618127a725a1773e85f13455602cf6f72ab4de17](https://github.com/Keychron/qmk_firmware/tree/618127a725a1773e85f13455602cf6f72ab4de17). Copyright belongs to the upstream contributors. QMK source and derivative firmware use GPL-2.0-or-later, subject to individual source notices. The upstream GPL text is retained in `firmware/LICENSE` and copied to the project `LICENSE`.

`firmware/patch_qmk.py` applies guarded changes to that exact checkout. `firmware/src/` contains this project's additional firmware code. The source archive, submodule revisions and build tools are recorded with URLs and SHA-256 checksums in `firmware/upstream.lock.json`.

ChibiOS, ChibiOS-Contrib, LUFA and printf are downloaded from their recorded upstream repositories during build preparation. Their respective copyright notices and licenses are retained in the downloaded sources. The public source export does not redistribute those source archives or tool binaries. A future binary release must package the corresponding source and applicable third-party notices; this export is not a binary release.

## VIA definitions

`firmware/recovery/k10_pro_us_rgb_v1.0.json` is the K10 Pro ANSI RGB definition obtained from [Keychron's firmware and JSON download page](https://www.keychron.com/pages/firmware-and-json-files-of-the-keychron-qmk-k-pro-and-k-max-series-keyboards). It is retained unchanged as reference data with attribution to Keychron. This project does not claim authorship of that definition.

`firmware/recovery/k10_pro_ansi_rgb_orca_led_618127a_via.json` is a modification of that definition, made on 2026-10-04. Only the first six `customKeycodes` labels are reordered to match the pinned firmware's custom-keycode enumeration. `custom-via-definition-manifest.json` records the original and modified checksums. This does not make the modified definition an official Keychron release, and this project's license does not grant additional rights in third-party data.

## Protocol research

[bkovacev/keychron-macos](https://github.com/bkovacev/keychron-macos), particularly its K10 Pro research, informed investigation of the Bluetooth host-LED transport. The public export contains this project's own implementation; it does not bundle that project's app or source tree.

## Build and runtime dependencies

The native apps use macOS frameworks supplied by Apple. The Python host uses the Python standard library. Firmware build preparation downloads the Arm GNU toolchain, dfu-util and pinned Python build packages; these keep their own licenses and are not included in the public source export. Exact package versions are in `firmware/requirements-build.txt`.

The installed Orca app is neither included nor modified by this repository. Orca, Keychron and product names are used to identify compatibility; no affiliation or endorsement is claimed.
