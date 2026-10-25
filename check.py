
import sys
import re
import math
from pathlib import Path

import numpy as np
import pandas as pd


OUTPUT_COLUMNS = [
    "station_id",
    "channel",
    "start_utc",
    "end_utc",
    "issue",
    "action",
    "note",
]

EARTH_RADIUS_M = 6_371_000.0
STUCK_DURATION = pd.Timedelta(hours=6)


def parse_nmea_checksum(sentence):
    sentence = sentence.strip()

    if not sentence.startswith("$") or "*" not in sentence:
        return False

    body, checksum = sentence[1:].rsplit("*", 1)

    if not re.fullmatch(r"[0-9A-Fa-f]{2}", checksum):
        return False

    value = 0
    for char in body:
        value ^= ord(char)

    return value == int(checksum, 16)


def parse_nmea_time(value):
    match = re.fullmatch(
        r"(\d{2})(\d{2})(\d{2}(?:\.\d+)?)",
        value.strip(),
    )

    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))
    seconds = float(match.group(3))

    if hour > 23 or minute > 59 or seconds >= 60:
        return None

    return hour * 3600 + minute * 60 + seconds


def parse_nmea_date(value):
    if not re.fullmatch(r"\d{6}", value.strip()):
        return None

    day = int(value[:2])
    month = int(value[2:4])
    year = int(value[4:6])
    year += 2000 if year < 80 else 1900

    try:
        return pd.Timestamp(
            year=year, month=month, day=day, tz="UTC"
        )
    except ValueError:
        return None


def parse_coordinate(value, hemisphere, is_latitude):
    try:
        number = float(value)
    except (ValueError, TypeError):
        return None

    degrees = int(number // 100)
    minutes = number - degrees * 100

    if not 0 <= minutes < 60:
        return None

    result = degrees + minutes / 60.0
    hemisphere = hemisphere.strip().upper()

    if is_latitude:
        if hemisphere not in ("N", "S") or result > 90:
            return None
        if hemisphere == "S":
            result = -result
    else:
        if hemisphere not in ("E", "W") or result > 180:
            return None
        if hemisphere == "W":
            result = -result

    return result


def format_utc(value):
    if value is None or pd.isna(value):
        return ""

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    return timestamp.strftime("%Y-%m-%d %H:%M")


def add_flag(
    flags, station_id, channel, start, end, issue, action, note
):
    flags.append({
        "station_id": str(station_id),
        "channel": channel,
        "start_utc": format_utc(start),
        "end_utc": format_utc(end),
        "issue": issue,
        "action": action,
        "note": note,
    })


def load_gps(path):
    columns = [
        "gps_row", "logger_time", "sentence_type",
        "gps_time", "lat", "lon",
    ]
    records = []

    if not path.exists():
        return pd.DataFrame(columns=columns)

    with path.open(
        "r", encoding="utf-8-sig", errors="replace"
    ) as file:
        for gps_row, line in enumerate(file):
            line = line.strip()

            if not line:
                continue

            parts = line.split(",", 1)
            if len(parts) != 2:
                continue

            logger_time = pd.to_datetime(
                parts[0].strip(), errors="coerce", utc=True
            )
            sentence = parts[1].strip()

            if pd.isna(logger_time):
                continue

            if not parse_nmea_checksum(sentence):
                continue

            fields = sentence[
                1:sentence.rfind("*")
            ].split(",")

            sentence_type = fields[0] if fields else ""
            gps_time = pd.NaT
            lat = None
            lon = None

            if sentence_type == "GPRMC":
                if len(fields) < 10 or fields[2].strip() != "A":
                    continue

                seconds = parse_nmea_time(fields[1])
                date = parse_nmea_date(fields[9])

                if seconds is None or date is None:
                    continue

                gps_time = date + pd.to_timedelta(seconds, unit="s")
                lat = parse_coordinate(fields[3], fields[4], True)
                lon = parse_coordinate(fields[5], fields[6], False)

            elif sentence_type == "GPGGA":
                if len(fields) < 10:
                    continue

                try:
                    fix_quality = int(fields[6])
                except (ValueError, TypeError):
                    continue

                if fix_quality <= 0:
                    continue

                lat = parse_coordinate(fields[2], fields[3], True)
                lon = parse_coordinate(fields[4], fields[5], False)

            else:
                continue

            records.append({
                "gps_row": gps_row,
                "logger_time": logger_time,
                "sentence_type": sentence_type,
                "gps_time": gps_time,
                "lat": lat,
                "lon": lon,
            })

    return pd.DataFrame(records, columns=columns)


def split_segments(df, timestamp_column, order_column):
    segments = []
    current = []
    previous = None

    ordered = df.sort_values(order_column, kind="stable")

    for index, row in ordered.iterrows():
        timestamp = row[timestamp_column]

        if pd.isna(timestamp):
            if current:
                segments.append(current)
                current = []
            previous = None
            continue

        if previous is not None and timestamp < previous:
            segments.append(current)
            current = []

        current.append(index)
        previous = timestamp

    if current:
        segments.append(current)

    return segments


def estimate_segment_offset(weather, gps, tolerance_seconds):
    if weather.empty or gps.empty:
        return None

    gps = gps.loc[
        gps["sentence_type"].eq("GPRMC")
        & gps["gps_time"].notna()
    ].copy()

    if gps.empty:
        return None

    left = weather[
        ["_file_row", "timestamp_utc"]
    ].sort_values("timestamp_utc")

    right = gps[
        ["logger_time", "gps_time"]
    ].sort_values("logger_time")

    matched = pd.merge_asof(
        left,
        right,
        left_on="timestamp_utc",
        right_on="logger_time",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_seconds),
    ).dropna(subset=["gps_time"])

    if matched.empty:
        return None

    offsets = (
        matched["timestamp_utc"] - matched["gps_time"]
    ).dt.total_seconds().dropna()

    return float(np.median(offsets)) if len(offsets) else None


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(
        math.radians, [lat1, lon1, lat2, lon2]
    )

    dlat = lat2 - lat1
    dlon = lon2 - lon1

    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2)
        * math.sin(dlon / 2) ** 2
    )

    return 2 * EARTH_RADIUS_M * math.asin(
        min(1.0, math.sqrt(a))
    )


def solar_elevation_deg(timestamp, latitude, longitude):
    """Approximate solar elevation in degrees."""
    timestamp = pd.Timestamp(timestamp)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    day = timestamp.dayofyear
    hour = (
        timestamp.hour
        + timestamp.minute / 60
        + timestamp.second / 3600
        + timestamp.microsecond / 3.6e9
    )

    gamma = 2 * math.pi / 365 * (
        day - 1 + (hour - 12) / 24
    )

    equation_of_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(gamma)
        - 0.032077 * math.sin(gamma)
        - 0.014615 * math.cos(2 * gamma)
        - 0.040849 * math.sin(2 * gamma)
    )

    declination = (
        0.006918
        - 0.399912 * math.cos(gamma)
        + 0.070257 * math.sin(gamma)
        - 0.006758 * math.cos(2 * gamma)
        + 0.000907 * math.sin(2 * gamma)
        - 0.002697 * math.cos(3 * gamma)
        + 0.00148 * math.sin(3 * gamma)
    )

    solar_minutes = (
        hour * 60 + equation_of_time + 4 * longitude
    )

    hour_angle = math.radians(solar_minutes / 4 - 180)
    lat_rad = math.radians(latitude)

    sin_elevation = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination)
        * math.cos(hour_angle)
    )

    return math.degrees(
        math.asin(max(-1.0, min(1.0, sin_elevation)))
    )


def check_impossible_values(df, station_id, flags):
    checks = {
        "temp_c": lambda s: (s < -45) | (s > 50),
        "rh_pct": lambda s: (s < 0) | (s > 100),
        "rain_mm": lambda s: s < 0,
        "wind_kmh": lambda s: s < 0,
        "ghi_wm2": lambda s: s < 0,
    }

    for channel, predicate in checks.items():
        if channel not in df.columns:
            continue

        values = pd.to_numeric(df[channel], errors="coerce")
        invalid = predicate(values).fillna(False)

        if channel == "temp_c":
            sensor_codes = values.isin([-40, 85])

            for index in df.index[sensor_codes.fillna(False)]:
                timestamp = df.at[index, "_corrected_timestamp"]
                add_flag(
                    flags, station_id, channel,
                    timestamp, timestamp,
                    "temperature sensor error code", "exclude",
                    f"Temperature is {values.at[index]:g} C, "
                    "a standard sensor error code (-40 or 85).",
                )

            invalid = invalid & ~sensor_codes

        for index in df.index[invalid]:
            timestamp = df.at[index, "_corrected_timestamp"]
            value = values.at[index]

            reason = {
                "temp_c": "temperature outside -45..50 C",
                "rh_pct": "relative humidity outside 0..100%",
            }.get(channel, f"negative {channel} reading")

            add_flag(
                flags, station_id, channel,
                timestamp, timestamp,
                reason, "exclude",
                f"Observed value: {value:g}.",
            )


def check_temperature_spikes(df, station_id, flags):
    if "temp_c" not in df.columns or df.empty:
        return

    values = pd.to_numeric(df["temp_c"], errors="coerce")

    # Sensor error codes have their own flags, not spike flags.
    error_codes = values.isin([-40, 85]).fillna(False)

    for position, index in enumerate(df.index):
        if error_codes.loc[index]:
            continue

        if pd.isna(values.loc[index]):
            continue

        start = max(0, position - 3)
        end = min(len(df), position + 4)

        neighbours = values.iloc[start:end].dropna()
        neighbours = neighbours.drop(
            index=df.index[position], errors="ignore"
        )

        # Remove error codes from the reference median as well.
        neighbours = neighbours[~neighbours.isin([-40, 85])]

        if len(neighbours) < 3:
            continue

        median = float(neighbours.median())

        if abs(float(values.loc[index]) - median) > 10:
            timestamp = df.at[index, "_corrected_timestamp"]

            add_flag(
                flags, station_id, "temp_c",
                timestamp, timestamp,
                "temperature spike", "exclude",
                f"Temperature {values.loc[index]:g} C differs "
                f"from nearby median {median:.2f} C by more than 10 C.",
            )


def check_stuck_sensors(df, station_id, interval_minutes, flags):
    if df.empty:
        return

    required_duration = STUCK_DURATION

    # Humidity saturation is handled separately, because values flickering
    # between 98-100 and above 100 are evidence of a saturation fault.
    if "rh_pct" in df.columns:
        humidity = pd.to_numeric(df["rh_pct"], errors="coerce")
        times = df["_corrected_timestamp"]

        run = []

        def finish_humidity_run(indices):
            if not indices:
                return

            first_time = times.loc[indices[0]]
            last_time = times.loc[indices[-1]]
            values = humidity.loc[indices]

            if (
                last_time - first_time >= required_duration
                and (values > 100).any()
            ):
                add_flag(
                    flags, station_id, "rh_pct",
                    first_time, last_time,
                    "humidity sensor stuck at saturation",
                    "exclude",
                    (
                        "Humidity remained at or above 98% for at least "
                        "6 hours and included readings above 100%; "
                        "likely saturation or sensor fault."
                    ),
                )

        previous_time = None

        for index in df.index:
            value = humidity.loc[index]
            timestamp = times.loc[index]

            is_saturated = pd.notna(value) and value >= 98

            consecutive = (
                previous_time is not None
                and timestamp - previous_time
                <= pd.Timedelta(minutes=interval_minutes * 1.5)
            )

            if not is_saturated or (run and not consecutive):
                finish_humidity_run(run)
                run = []

            if is_saturated:
                run.append(index)

            previous_time = timestamp if is_saturated else None

        finish_humidity_run(run)

    for channel in [
        "temp_c", "rh_pct", "pressure_hpa", "wind_dir_deg"
    ]:
        if channel not in df.columns:
            continue

        values = pd.to_numeric(df[channel], errors="coerce")
        times = df["_corrected_timestamp"]
        run = []
        previous_value = None
        previous_time = None

        def finish_exact_run(indices):
            if not indices:
                return

            first_time = times.loc[indices[0]]
            last_time = times.loc[indices[-1]]

            if last_time - first_time >= required_duration:
                value = values.loc[indices[0]]

                add_flag(
                    flags, station_id, channel,
                    first_time, last_time,
                    "stuck sensor", "exclude",
                    f"{channel} remained exactly {value:g} "
                    f"for {(last_time - first_time).total_seconds() / 3600:.2f} "
                    "hours across consecutive readings.",
                )

        for index in df.index:
            value = values.loc[index]
            timestamp = times.loc[index]

            if pd.isna(value) or pd.isna(timestamp):
                finish_exact_run(run)
                run = []
                previous_value = None
                previous_time = None
                continue

            consecutive = (
                previous_time is not None
                and timestamp - previous_time
                <= pd.Timedelta(minutes=interval_minutes * 1.5)
            )

            # Do not report exact-value humidity runs when they are part
            # of a saturation fault already handled above.
            humidity_saturation = (
                channel == "rh_pct" and value >= 98
            )

            if humidity_saturation:
                finish_exact_run(run)
                run = []
                previous_value = None
                previous_time = None
                continue

            if (
                not run
                or value != previous_value
                or not consecutive
            ):
                finish_exact_run(run)
                run = [index]
            else:
                run.append(index)

            previous_value = value
            previous_time = timestamp

        finish_exact_run(run)


def check_sunlight_at_night(
    df, station_id, latitude, longitude, interval_minutes, flags
):
    if "ghi_wm2" not in df.columns:
        return

    if pd.isna(latitude) or pd.isna(longitude):
        return

    ghi = pd.to_numeric(df["ghi_wm2"], errors="coerce")
    midpoint_offset = pd.Timedelta(minutes=interval_minutes / 2)

    night_rows = []

    for index in df.index:
        value = ghi.loc[index]

        if pd.isna(value) or value <= 20:
            continue

        # Logger timestamp marks the END of the measurement interval.
        midpoint = (
            df.at[index, "_corrected_timestamp"] - midpoint_offset
        )

        elevation = solar_elevation_deg(
            midpoint, latitude, longitude
        )

        if elevation < -3:
            night_rows.append({
                "index": index,
                "time": df.at[index, "_corrected_timestamp"],
                "midpoint": midpoint,
                "ghi": float(value),
                "elevation": elevation,
                "day": midpoint.date(),
            })

    if not night_rows:
        return

    night_df = pd.DataFrame(night_rows)

    # Require at least three qualifying readings on the same UTC day.
    for day, group in night_df.groupby("day", sort=True):
        if len(group) < 3:
            continue

        add_flag(
            flags,
            station_id,
            "ghi_wm2",
            group["time"].min(),
            group["time"].max(),
            "sunlight at night",
            "exclude",
            (
                f"{len(group)} readings that day exceeded 20 W/m2 "
                "with midpoint solar elevation below -3 degrees; "
                f"elevation range {group['elevation'].min():.2f} "
                f"to {group['elevation'].max():.2f} degrees."
            ),
        )


def check_gps_position(
    gps, station_id, station_lat, station_lon, flags, df
):
    if (
        pd.isna(station_lat)
        or pd.isna(station_lon)
        or gps.empty
    ):
        return

    fixes = gps.dropna(subset=["lat", "lon"])

    if fixes.empty:
        return

    median_lat = float(fixes["lat"].median())
    median_lon = float(fixes["lon"].median())

    distance = haversine_m(
        float(station_lat), float(station_lon),
        median_lat, median_lon,
    )

    if distance > 500:
        add_flag(
            flags, station_id, "metadata",
            df["_corrected_timestamp"].min() if not df.empty else pd.NaT,
            df["_corrected_timestamp"].max() if not df.empty else pd.NaT,
            "GPS position differs from station metadata",
            "correct",
            (
                f"Median valid GPS position is {median_lat:.6f}, "
                f"{median_lon:.6f}; {distance:.0f} m from stations.csv "
                f"position {float(station_lat):.6f}, "
                f"{float(station_lon):.6f}."
            ),
        )


def check_gaps(df, station_id, interval_minutes, flags):
    if len(df) < 2:
        return

    df = df.sort_values(
        "_corrected_timestamp", kind="stable"
    )

    expected = pd.Timedelta(minutes=interval_minutes)
    timestamps = df["_corrected_timestamp"].tolist()

    for previous, current in zip(timestamps[:-1], timestamps[1:]):
        delta = current - previous

        if delta <= expected * 1.5:
            continue

        missing_start = previous + expected
        missing_end = current - expected

        if missing_start <= missing_end:
            add_flag(
                flags, station_id, "all",
                missing_start, missing_end,
                "missing logger interval(s)", "exclude",
                (
                    f"Expected {interval_minutes:g}-minute sampling; "
                    f"the gap between valid observations is "
                    f"{delta.total_seconds() / 60:g} minutes."
                ),
            )


def merge_consecutive_flags(flags, interval_minutes):
    """Merge nearby flags of the same issue without merging across long gaps."""
    if not flags:
        return []

    df = pd.DataFrame(flags, columns=OUTPUT_COLUMNS)

    df["_start"] = pd.to_datetime(
        df["start_utc"], errors="coerce", utc=True
    )
    df["_end"] = pd.to_datetime(
        df["end_utc"], errors="coerce", utc=True
    )

    df["_interval"] = df["station_id"].map(
        interval_minutes
    ).fillna(10.0)

    group_columns = [
        "station_id", "channel", "issue", "action"
    ]

    output = []

    for _, group in df.groupby(
        group_columns, sort=False, dropna=False
    ):
        group = group.sort_values(
            ["_start", "_end"], kind="stable",
            na_position="last",
        )

        current = None

        for _, row in group.iterrows():
            start = row["_start"]
            end = row["_end"]

            if pd.isna(start):
                if current is not None:
                    output.append(current)
                    current = None
                output.append(row.to_dict())
                continue

            if current is None:
                current = row.to_dict()
                current["_notes"] = [str(row["note"])]
                continue

            previous_end = current["_end"]
            tolerance = pd.Timedelta(
                minutes=max(
                    float(row["_interval"]),
                    float(current["_interval"]),
                )
            )

            if (
                pd.notna(previous_end)
                and start <= previous_end + tolerance
            ):
                if pd.notna(end) and (
                    pd.isna(current["_end"])
                    or end > current["_end"]
                ):
                    current["_end"] = end

                note = str(row["note"])
                if note not in current["_notes"]:
                    current["_notes"].append(note)
            else:
                output.append(current)
                current = row.to_dict()
                current["_notes"] = [str(row["note"])]

        if current is not None:
            output.append(current)

    result = []

    for row in output:
        notes = row.get("_notes", [str(row.get("note", ""))])

        result.append({
            "station_id": row["station_id"],
            "channel": row["channel"],
            "start_utc": format_utc(row.get("_start")),
            "end_utc": format_utc(row.get("_end")),
            "issue": row["issue"],
            "action": row["action"],
            "note": "; ".join(notes),
        })

    return result


def process_station(station, pack, flags):
    station_id = str(station["station_id"]).strip()

    interval = pd.to_numeric(
        station.get("logger_interval_min"), errors="coerce"
    )

    if pd.isna(interval) or interval <= 0:
        add_flag(
            flags, station_id, "metadata", pd.NaT, pd.NaT,
            "invalid logger interval", "exclude",
            "logger_interval_min must be positive.",
        )
        return

    interval = float(interval)

    data_path = pack / "data" / f"{station_id}.csv"
    gps_path = pack / "gps" / f"{station_id}.nmea"

    if not data_path.exists():
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "missing station data file", "exclude",
            "The station CSV file does not exist.",
        )
        return

    try:
        raw = pd.read_csv(data_path)
    except Exception as exc:
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "unreadable station data file", "exclude",
            f"Could not read station CSV: {exc}",
        )
        return

    if "timestamp_utc" not in raw.columns:
        add_flag(
            flags, station_id, "metadata", pd.NaT, pd.NaT,
            "missing timestamp column", "exclude",
            "The required timestamp_utc column is absent.",
        )
        return

    raw["_file_row"] = np.arange(len(raw))
    raw["timestamp_utc"] = pd.to_datetime(
        raw["timestamp_utc"], errors="coerce", utc=True
    )

    for _, row in raw[raw["timestamp_utc"].isna()].iterrows():
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "invalid logger timestamp", "exclude",
            f"CSV row {int(row['_file_row']) + 2} has an invalid timestamp.",
        )

    raw = raw[raw["timestamp_utc"].notna()].copy()

    if raw.empty:
        return

    gps = load_gps(gps_path)

    weather_segments = split_segments(
        raw, "timestamp_utc", "_file_row"
    )
    gps_segments_indices = split_segments(
        gps, "logger_time", "gps_row"
    )
    gps_segments = [
        gps.loc[indices].copy()
        for indices in gps_segments_indices
    ]

    corrected_segments = []
    tolerance_seconds = max(120.0, interval * 30.0)

    # Clock correction occurs before duplicate/conflict detection.
    for segment_number, indices in enumerate(weather_segments):
        segment = raw.loc[indices].copy()
        segment["_segment"] = segment_number
        segment["_corrected_timestamp"] = segment["timestamp_utc"]

        gps_segment = (
            gps_segments[segment_number]
            if segment_number < len(gps_segments)
            else pd.DataFrame(columns=gps.columns)
        )

        offset_seconds = estimate_segment_offset(
            segment, gps_segment, tolerance_seconds
        )

        if (
            offset_seconds is not None
            and abs(offset_seconds) >= 120
        ):
            shift_seconds = -offset_seconds

            segment["_corrected_timestamp"] = (
                segment["timestamp_utc"]
                + pd.to_timedelta(shift_seconds, unit="s")
            )

            add_flag(
                flags, station_id, "all",
                segment["_corrected_timestamp"].min(),
                segment["_corrected_timestamp"].max(),
                "logger clock offset", "correct",
                (
                    f"Shifted timestamps by "
                    f"{shift_seconds / 3600:+.2f} h "
                    f"({shift_seconds:+.0f} seconds); median "
                    f"logger-minus-RMC offset was "
                    f"{offset_seconds:+.0f} seconds in clock "
                    f"segment {segment_number + 1}."
                ),
            )

        corrected_segments.append(segment)

    if not corrected_segments:
        return

    corrected = pd.concat(
        corrected_segments, ignore_index=True
    )

    # Remove exact duplicates after clock correction.
    original_columns = [
        col for col in raw.columns if col != "_file_row"
    ]

    duplicate_mask = corrected.duplicated(
        subset=original_columns, keep="first"
    )

    duplicate_indices = corrected.index[duplicate_mask].tolist()

    if duplicate_indices:
        duplicate_rows = corrected.loc[duplicate_indices].copy()
        duplicate_rows = duplicate_rows.sort_values(
            "_file_row", kind="stable"
        )

        groups = []
        run = [duplicate_rows.index[0]]

        for index in duplicate_rows.index[1:]:
            if (
                corrected.at[index, "_file_row"]
                == corrected.at[run[-1], "_file_row"] + 1
            ):
                run.append(index)
            else:
                groups.append(run)
                run = [index]

        groups.append(run)

        for run in groups:
            add_flag(
                flags, station_id, "all",
                corrected.loc[run, "_corrected_timestamp"].min(),
                corrected.loc[run, "_corrected_timestamp"].max(),
                "exact duplicate row(s)", "correct",
                f"drop the repeated copy; {len(run)} repeated row(s).",
            )

        corrected = corrected.drop(index=duplicate_indices).copy()

    # Conflicting timestamps are evaluated using corrected UTC.
    conflicting = corrected[
        corrected.duplicated(
            "_corrected_timestamp", keep=False
        )
    ]

    for timestamp in sorted(
        conflicting["_corrected_timestamp"].unique()
    ):
        timestamp = pd.Timestamp(timestamp)

        add_flag(
            flags, station_id, "all",
            timestamp, timestamp,
            "conflicting duplicate timestamp", "exclude",
            "Different readings share the same corrected UTC timestamp.",
        )

    if not conflicting.empty:
        corrected = corrected[
            ~corrected["_corrected_timestamp"].isin(
                conflicting["_corrected_timestamp"]
            )
        ].copy()

    corrected = corrected.sort_values(
        "_corrected_timestamp", kind="stable"
    ).reset_index(drop=True)

    # Part 2: single-station checks after clock fix and deduplication.
    check_impossible_values(corrected, station_id, flags)
    check_temperature_spikes(corrected, station_id, flags)

    for _, segment in corrected.groupby("_segment", sort=False):
        segment = segment.sort_values(
            "_corrected_timestamp", kind="stable"
        )
        check_stuck_sensors(
            segment, station_id, interval, flags
        )

    try:
        station_lat = float(station["lat"])
        station_lon = float(station["lon"])
    except (ValueError, TypeError, KeyError):
        station_lat = np.nan
        station_lon = np.nan

    check_sunlight_at_night(
        corrected, station_id,
        station_lat, station_lon, interval, flags,
    )

    check_gps_position(
        gps, station_id, station_lat, station_lon,
        flags, corrected,
    )

    # Gap checks stay within original clock segments.
    for _, segment in corrected.groupby("_segment", sort=False):
        check_gaps(segment, station_id, interval, flags)


def main():
    if len(sys.argv) != 2:
        print(
            "Usage: python check.py <pack_folder>",
            file=sys.stderr,
        )
        return 2

    pack = Path(sys.argv[1])

    if not pack.is_dir():
        print(f"Error: pack folder not found: {pack}", file=sys.stderr)
        return 2

    stations_path = pack / "stations.csv"

    if not stations_path.exists():
        print(
            f"Error: stations.csv not found in {pack}",
            file=sys.stderr,
        )
        return 2

    try:
        stations = pd.read_csv(stations_path)
    except Exception as exc:
        print(f"Error reading stations.csv: {exc}", file=sys.stderr)
        return 2

    required = {
        "station_id", "logger_interval_min", "lat", "lon"
    }

    if not required.issubset(stations.columns):
        missing = sorted(required - set(stations.columns))
        print(
            f"Error: stations.csv missing columns: {', '.join(missing)}",
            file=sys.stderr,
        )
        return 2

    flags = []

    for _, station in stations.iterrows():
        station_id = str(station["station_id"]).strip()

        if not station_id or station_id.lower() == "nan":
            continue

        process_station(station, pack, flags)

    intervals = {}

    for _, station in stations.iterrows():
        station_id = str(station["station_id"]).strip()
        interval = pd.to_numeric(
            station["logger_interval_min"], errors="coerce"
        )

        if pd.notna(interval) and interval > 0:
            intervals[station_id] = float(interval)

    flags = merge_consecutive_flags(flags, intervals)

    result = pd.DataFrame(flags, columns=OUTPUT_COLUMNS)

    if not result.empty:
        result = result.sort_values(
            ["station_id", "start_utc", "channel", "issue"],
            kind="stable",
            na_position="last",
        )

    output_path = Path.cwd() / "flags.csv"
    result.to_csv(output_path, index=False)

    print(f"Wrote {len(result)} flags to {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
