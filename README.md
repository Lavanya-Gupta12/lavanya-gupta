Lavanya Gupta

lavanya.gupta.btech2023@sitpune.edu.in

Video: https://youtu.be/MRyOePxe5oc

Run: `python check.py <pack_folder>`

## Files
- `check.py`: runs the data-quality checks and writes `flags.csv` to the current folder.
- `flags.csv`: 17 flags for the given pack.
- `report.html`: one-page report for the network coordinator.
- `DECISIONS.md`: my decisions and reasoning.
- `ai_logs/`: complete exports of my AI chats (Claude and ChatGPT).

## Requirements
Python 3 with `pandas` and `numpy` (`pip install pandas numpy`). The data pack is not included in this repository.

## Checks (in order)
1. **GPS clock correction:** split each file where the logger clock jumps backwards; compare logger time with valid GPS (RMC, status A, good checksum) time; shift a segment if it is 2+ minutes off.
2. **Exact duplicates:** rows identical to earlier rows; drop the copy.
3. **Conflicting timestamps:** same time, different values; exclude.
4. **Gaps:** missing 10-minute intervals; exclude, never infill.
5. **Impossible values:** out-of-range readings; -40/85 °C flagged as sensor error codes.
6. **Temperature spikes:** more than 10 °C from the nearby median.
7. **Stuck sensors:** the exact same value for 6+ hours.
8. **Humidity stuck at saturation:** 98%+ for 6+ hours including values above 100%.
9. **Sunlight at night:** GHI > 20 W/m² with the sun below -3° (interval midpoint).
10. **GPS position:** median GPS fix more than 500 m from `stations.csv` means a metadata error.
11. **Pressure step:** a sudden lasting 2+ hPa jump against the network (anomalies, so altitude doesn't matter).
12. **Wind scale change:** the station/network wind ratio changes by 1.4x+ (e.g. m/s logged as km/h = 3.6x).
13. **Blocked rain gauge:** 0 mm while the network had 5+ mm; plus the burst of released water when cleared.
14. **Radiation-shield bias:** daytime-only warm drift against neighbours, nights unchanged.
15. **Keep flags:** cold explained by altitude (6.5 °C/km); rain on dry days after freezing nights (snowmelt).

## Why clocks are fixed first
Checks 11–15 compare stations at the same moment. A station with a wrong clock would corrupt every comparison, so clocks are corrected before anything else.

## Times
Times in `flags.csv` are true UTC after clock correction.

## Assumptions and limits
- Shield-bias detection needs about three weeks of data and uses each station's first 14 days as "normal"; a shield bad from day one would not be seen.
- One wind-scale change per station is assumed.
- Wind direction is checked only for stuck values.
- Runtime is about one minute.