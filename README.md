# NI Sync GUI (PyQt6)

Standalone GUI logger for NI DAQ, microscope sync, encoder, and Arduino digital I/O.

## What it does

- Uses microscope sync pulse as sampling clock (default safe mode).
- Reads encoder + selected digital input channels.
- Sends configurable timed output pulses.
- Writes CSV in near real-time.
- Defaults are aligned with `NI_V1.3_encoder.py`.

## Requirements

- Windows with NI-DAQmx installed.
- NI device tested target: USB-6421 / PCIe-6341.
- Python 3.10+ recommended.

Install:

```bash
pip install -r requirements.txt
```

Run:

```bash
python NI_GUI.py
```

Use `View` in the top row to open a saved CSV and plot position/velocity and output-event spans.

## CSV format behavior

- By default, columns follow original style:
  - `Frame_ID, Time_s, Arduino_State, Raw_Ticks_Signed, Zeroed_Dist_cm, Smoothed_Vel_cm_s`
- If GUI input/output definitions change, CSV columns change accordingly:
  - input channel names are inserted as columns
  - output event marker columns are appended

## Safe sampling policy in this build

- Input channels should use **Follow Sync** mode so each row corresponds to one microscope frame.
- **Fixed Hz** mode is intentionally blocked in this version to avoid mixed-timing misalignment.