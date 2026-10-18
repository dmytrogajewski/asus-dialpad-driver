H7606WX input evidence and Doctor design prototypes

This branch is supporting material for discussion, not an upstream runtime
replacement. Read input/proposal.txt and doctor/proposal.txt for limitations.
The main driver is unchanged on this branch.

Required for replay: Python 3 with Python libevdev (pip package libevdev).
Required for the GUI/tests: PySide6 (PySide6-Essentials is sufficient) plus
libevdev. Install those dependencies in your normal Python environment.

From this directory:
    python3 -s input/validate-fixtures.py
    python3 -s doctor/scripts/doctor_gui.py --preview
    python3 -s doctor/scripts/test-doctor.py

For a headless preview, set QT_QPA_PLATFORM=offscreen. Preview mode does not
open devices, stop services, send controls, or write reports.

The Doctor source is copied from the working local prototype. The only
packaging adaptations are the relative TouchFrames import, this launcher,
and SPDX headers. It remains specific to the tested H7606WX device and local
service names. Live capture intentionally stops both DialPad user services,
leaves the controls paused, and writes raw touch data under doctor/reports.
Read doctor/proposal.txt before attempting live checks on a laptop.

Only reduced touchpad input excerpts are published. Relative timing and touch
coordinates remain; absolute timestamps, original tracking IDs, duplicate
legacy axes, scan-clock values, and unrelated unchanged frames were removed.
The full diagnostic recording is not included. Preview images are synthetic
GUI previews, not another hardware test.

Sources are distributed under the repository's GPL-2.0 license.
