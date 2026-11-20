# Open questions

## Brightness after the lens change (2026-11-20)

The camera moved from the kit 16-50 to a Viltrox 27mm f/1.2, then to ISO 100 and 1/60 s. A light sweep and a walk by eye set the new levels in `config.toml`: day 15%, picked by eye at midday, and `max_stops_under = 0.6` so closed-loop holds that look. Two values are still defaults, not checked by eye:

- **`night_brightness = 6`.** Scaled from the measured gain (2.7× per percent) so the face matches the look set by eye on 2026-10-29. Not checked after dark. Revisit at night by eye in OBS.
- **`target_luma = 0.263` (kept).** The October night look. By day the face is now held about 1.8 stops brighter than this, by ratio mode. If the night look feels dim next to the daytime one, raise it in steps of about 0.03 and watch what `keylight auto` says.
- **Summer daylight.** `day_brightness = 15` comes from one November reading. A brighter summer room may need more. Revisit after a bright summer call.
