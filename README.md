# Social Battery — Tildagon App

Displays your social energy as a car fuel-gauge needle on the Tildagon's round screen.

---

## Display

- **Arc** sweeps 250° from lower-left (empty) to lower-right (full), passing through the top at 50%
- **Colour zones:** red (0–20%), orange (20–50%), green (50–100%)
- **Red needle** points to the current percentage
- **Percentage** shown in large text below the needle pivot
- **Status line** at the top shows `charging` / `draining` / `PAUSED`
- **Pivot dot** turns red while paused
- **12 outer LEDs** light up proportional to battery level and match the arc colour zones

---

## Controls

### UP / DOWN — manual adjustment

| Button | Action |
|--------|--------|
| UP | Manual +1% |
| DOWN | Manual −1% |

### LEFT / RIGHT — direction + speed (click sequences)

All clicks must land within a **650 ms window**. The action fires after the window closes.

| Presses | Direction | Interval | Animation |
|---------|-----------|----------|-----------|
| Single click | Set direction | 30 s / tick | Scrolling arrows + label (~1.2 s) |
| Double-click | Set direction | 10 s / tick | None |
| Triple-click | Set direction | 1 s / tick for 5 s, then reverts to 30 s | None |

A single click after a double-click reverts the interval back to 30 s.

### CONFIRM / CANCEL

| Button | Action |
|--------|--------|
| CONFIRM | Toggle auto-update on / off |
| CANCEL | Minimise app |

---

## Direction animations

Triggered by a single LEFT or RIGHT press:

- **RIGHT (charging):** Green arrows scroll upward, "Charging" label at top (~1.2 s)
- **LEFT (draining):** Red arrows scroll downward, "Battery under load" label at bottom (~1.2 s)

The gauge is restored automatically when the animation ends. The auto-update timer resets so the next tick is always a full interval away.

---

## Low-battery warning

When the battery drops below 20%, "Charge immediately!!" flashes in red three times before returning to the gauge. To avoid being intrusive, the warning only fires on every **other** decrease while below 20% (1st, 3rd, 5th…). The counter resets if the battery recovers above 20%.

---

## LEDs

The 12 outer edge LEDs reflect the battery level and use the same colour zones as the arc:

| Level | LED colour |
|-------|-----------|
| ≥ 50% | Green |
| 20–49% | Orange |
| < 20% | Red |

LEDs turn off one-by-one from the top as the battery drains, and light back up as it charges.

---

## Installing on the badge

Requires [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html):

```bash
pip install mpremote

# Create the app directory on the badge (skip if it already exists)
mpremote mkdir :/apps
mpremote mkdir :/apps/social_battery

# Copy the app files (run from the repo root)
mpremote cp apps/social_battery/app.py      :/apps/social_battery/app.py
mpremote cp apps/social_battery/metadata.json :/apps/social_battery/metadata.json

# Reboot the badge
mpremote reset
```

If you get `Permission denied` on `/dev/ttyACM*`, prefix each command with `sg dialout -c "..."`.
