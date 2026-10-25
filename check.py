#!/usr/bin/env python3
"""Weather-station data quality checks.

Usage:
    python check.py <pack_folder>

Writes flags.csv to the current working directory.
"""

import sys
import math
from pathlib import Path
from datetime import datetime, timedelta, timezone

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

WEATHER_COLUMNS = [
    "timestamp_utc",
    "temp_c",
    "rh_pct",
    "pressure_hpa",
    "wind_kmh",
    "wind_dir_deg",
    "rain_mm",
    "ghi_wm2",
]

SATURATION_ISSUE = "humidity sensor stuck at saturation"
RH_RANGE_ISSUE = "relative humidity outside 0..100%"


# ---------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------

def parse_utc(value):
    """Parse a timestamp as UTC, returning NaT if invalid."""
    result = pd.to_datetime(value, errors="coerce", utc=True)
    return result


def timestamp_text(value):
    """Format timestamps consistently in UTC."""
    if value is None or pd.isna(value):
        return ""
    value = pd.Timestamp(value)
    if value.tzinfo is None:
        value = value.tz_localize("UTC")
    else:
        value = value.tz_convert("UTC")
    return value.strftime("%Y-%m-%d %H:%M")


def add_flag(flags, station_id, channel, start, end, issue, action, note):
    """Append one flag. start/end may be None for metadata-only flags."""
    flags.append({
        "station_id": str(station_id),
        "channel": channel,
        "start_utc": start,
        "end_utc": end,
        "issue": issue,
        "action": action,
        "note": note,
    })


def haversine_m(lat1, lon1, lat2, lon2):
    """Great-circle distance between two points, in metres."""
    radius = 6_371_000.0
    p1 = math.radians(float(lat1))
    p2 = math.radians(float(lat2))
    dp = math.radians(float(lat2) - float(lat1))
    dl = math.radians(float(lon2) - float(lon1))

    a = (
        math.sin(dp / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    )
    return 2 * radius * math.asin(min(1.0, math.sqrt(a)))


# ---------------------------------------------------------------------
# NMEA GPS parsing
# ---------------------------------------------------------------------

def parse_nmea_checksum(sentence):
    """Return True only if an NMEA sentence has a valid checksum."""
    sentence = sentence.strip()
    if not sentence.startswith("$") or "*" not in sentence:
        return False

    body, checksum_text = sentence[1:].rsplit("*", 1)
    try:
        expected = int(checksum_text[:2], 16)
    except (ValueError, TypeError):
        return False

    actual = 0
    for char in body:
        actual ^= ord(char)
    return actual == expected


def parse_nmea_time(value):
    """Parse the hhmmss[.fraction] time field from an NMEA sentence."""
    if not value or len(value) < 6:
        return None

    try:
        hour = int(value[0:2])
        minute = int(value[2:4])
        second_float = float(value[4:])
        second = int(second_float)
        microsecond = int(round((second_float - second) * 1_000_000))
        return (hour, minute, second, microsecond)
    except (ValueError, TypeError):
        return None


def parse_nmea_date(value):
    """Parse an NMEA RMC date field, formatted ddmmyy."""
    if not value or len(value) != 6:
        return None

    try:
        day = int(value[0:2])
        month = int(value[2:4])
        year = int(value[4:6])
        year += 2000 if year < 80 else 1900
        return year, month, day
    except (ValueError, TypeError):
        return None


def parse_coordinate(value, hemisphere):
    """Convert an NMEA latitude or longitude to signed decimal degrees."""
    if not value or not hemisphere:
        return np.nan

    try:
        raw = float(value)
        degrees = int(raw // 100)
        minutes = raw - degrees * 100
        result = degrees + minutes / 60.0

        if hemisphere.upper() in ("S", "W"):
            result = -result
        elif hemisphere.upper() not in ("N", "E"):
            return np.nan

        return result
    except (ValueError, TypeError):
        return np.nan


def parse_rmc_datetime(fields):
    """Return the UTC datetime from a validly formatted RMC fields list."""
    if len(fields) <= 9:
        return None

    time_parts = parse_nmea_time(fields[1])
    date_parts = parse_nmea_date(fields[9])

    if time_parts is None or date_parts is None:
        return None

    try:
        year, month, day = date_parts
        hour, minute, second, microsecond = time_parts
        return datetime(
            year, month, day, hour, minute, second, microsecond,
            tzinfo=timezone.utc,
        )
    except ValueError:
        return None


def load_gps(path):
    """Load checksum-valid GPS records from a station's NMEA file.

    Each line is expected to contain:
        logger_timestamp,$GPRMC...
    or:
        logger_timestamp,$GPGGA...

    Only RMC records with status A are used for clock correction.
    Valid GGA fixes can be used for station-position checks.
    """
    records = []

    if not path.exists():
        return pd.DataFrame(columns=[
            "logger_timestamp", "gps_time", "lat", "lon", "line_number"
        ])

    with path.open("r", encoding="utf-8", errors="replace") as file:
        for line_number, line in enumerate(file):
            line = line.strip()
            if not line:
                continue

            if "," not in line:
                continue

            logger_text, sentence = line.split(",", 1)
            logger_timestamp = parse_utc(logger_text.strip())

            if pd.isna(logger_timestamp):
                continue

            sentence = sentence.strip()
            if not parse_nmea_checksum(sentence):
                continue

            fields = sentence.split(",")
            sentence_type = fields[0][-3:]
            gps_time = pd.NaT
            lat = np.nan
            lon = np.nan

            if sentence_type == "RMC":
                # RMC status must be A (active/valid).
                if len(fields) <= 9 or fields[2].upper() != "A":
                    continue

                parsed_time = parse_rmc_datetime(fields)
                if parsed_time is None:
                    continue

                gps_time = pd.Timestamp(parsed_time)
                lat = parse_coordinate(fields[3], fields[4])
                lon = parse_coordinate(fields[5], fields[6])

            elif sentence_type == "GGA":
                # GGA fix quality must be greater than zero.
                if len(fields) <= 6:
                    continue

                try:
                    fix_quality = int(fields[6])
                except (ValueError, TypeError):
                    continue

                if fix_quality <= 0:
                    continue

                lat = parse_coordinate(fields[2], fields[3])
                lon = parse_coordinate(fields[4], fields[5])

            else:
                continue

            records.append({
                "logger_timestamp": logger_timestamp,
                "gps_time": gps_time,
                "lat": lat,
                "lon": lon,
                "line_number": line_number,
            })

    gps = pd.DataFrame(records)
    if gps.empty:
        return pd.DataFrame(columns=[
            "logger_timestamp", "gps_time", "lat", "lon", "line_number"
        ])

    return gps.sort_values("line_number").reset_index(drop=True)


# ---------------------------------------------------------------------
# Clock-jump detection and correction
# ---------------------------------------------------------------------

def split_segments(df):
    """Split rows whenever the original logger timestamp moves backwards."""
    if df.empty:
        return []

    timestamps = df["timestamp_utc"]
    segment_ids = []
    segment_id = 0
    previous = None

    for timestamp in timestamps:
        if pd.isna(timestamp):
            segment_ids.append(segment_id)
            continue

        if previous is not None and timestamp < previous:
            segment_id += 1

        segment_ids.append(segment_id)
        previous = timestamp

    result = df.copy()
    result["_segment"] = segment_ids

    return [
        result[result["_segment"] == segment].copy()
        for segment in sorted(result["_segment"].unique())
    ]


def split_gps_segments(gps):
    """Split GPS records at backwards jumps in their logger timestamps."""
    if gps.empty:
        return []

    result = gps.copy()
    result["_gps_segment"] = (
        result["logger_timestamp"].diff().lt(pd.Timedelta(0)).cumsum()
    )

    return [
        result[result["_gps_segment"] == segment].copy()
        for segment in sorted(result["_gps_segment"].unique())
    ]


def estimate_segment_offset(weather_segment, gps_segment, interval_minutes):
    """Estimate logger_timestamp minus GPS time for one segment."""
    if weather_segment.empty or gps_segment.empty:
        return pd.Timedelta(0)

    valid_gps = gps_segment.dropna(subset=["gps_time", "logger_timestamp"])
    if valid_gps.empty:
        return pd.Timedelta(0)

    weather_times = weather_segment[["timestamp_utc"]].dropna().copy()
    if weather_times.empty:
        return pd.Timedelta(0)

    weather_times = weather_times.sort_values("timestamp_utc")
    gps_times = valid_gps[
        ["logger_timestamp", "gps_time"]
    ].sort_values("logger_timestamp")

    tolerance = pd.Timedelta(
        seconds=max(120, interval_minutes * 30)
    )

    matched = pd.merge_asof(
        weather_times,
        gps_times,
        left_on="timestamp_utc",
        right_on="logger_timestamp",
        direction="nearest",
        tolerance=tolerance,
    )

    matched = matched.dropna(subset=["gps_time", "logger_timestamp"])
    if matched.empty:
        return pd.Timedelta(0)

    offsets = (
        matched["logger_timestamp"] - matched["gps_time"]
    ).dropna()

    if offsets.empty:
        return pd.Timedelta(0)

    return offsets.median()


def correct_clock(weather, gps, station_id, interval_minutes, flags):
    """Correct weather timestamps using GPS offsets, segment by segment."""
    weather = weather.copy()
    weather["_file_row"] = np.arange(len(weather))
    weather["_corrected_timestamp"] = weather["timestamp_utc"]

    weather_segments = split_segments(weather)
    gps_segments = split_gps_segments(gps)

    # Match segments by their sequence/order in the files. If the GPS file
    # contains fewer segments, use the closest available segment in order.
    for segment_index, weather_segment in enumerate(weather_segments):
        if not gps_segments:
            break

        gps_index = min(segment_index, len(gps_segments) - 1)
        gps_segment = gps_segments[gps_index]

        offset = estimate_segment_offset(
            weather_segment, gps_segment, interval_minutes
        )

        # Only correct real clock errors (>= 2 min). GPS-vs-logger
        # differences of a second or two are normal and must not shift data.
        if abs(offset.total_seconds()) < 120:
            continue
        # Round to whole minutes: GPS sentences carry a second or two of noise.
        offset = pd.Timedelta(minutes=round(offset.total_seconds() / 60))

        shift = -offset
        row_indices = weather_segment.index
        weather.loc[row_indices, "_corrected_timestamp"] = (
            weather.loc[row_indices, "timestamp_utc"] + shift
        )

        corrected = weather.loc[row_indices, "_corrected_timestamp"]
        add_flag(
            flags,
            station_id,
            "all",
            corrected.min(),
            corrected.max(),
            "logger clock offset",
            "correct",
            f"GPS time shows the logger clock is {abs(offset.total_seconds()) / 3600:.2f} h "
            f"{'ahead of' if offset.total_seconds() > 0 else 'behind'} UTC in this part of the file; "
            f"shift timestamps by {shift.total_seconds() / 3600:+.2f} h.",
        )

    return weather


# ---------------------------------------------------------------------
# Part 1: duplicates, conflicts, and gaps
# ---------------------------------------------------------------------

def handle_duplicates_and_conflicts(weather, station_id, flags):
    """Flag exact duplicate copies and exclude conflicting timestamps."""
    weather = weather.copy()

    raw_value_columns = [
        column for column in WEATHER_COLUMNS if column in weather.columns
    ]

    # Exact duplicates mean identical original timestamp and sensor values.
    duplicate_mask = weather.duplicated(
        subset=raw_value_columns, keep="first"
    )

    duplicate_rows = weather[duplicate_mask].copy()
    if not duplicate_rows.empty:
        for _, row in duplicate_rows.iterrows():
            timestamp = row["_corrected_timestamp"]
            add_flag(
                flags,
                station_id,
                "all",
                timestamp,
                timestamp,
                "exact duplicate row",
                "correct",
                "drop the repeated copy",
            )

    weather = weather.loc[~duplicate_mask].copy()

    # Conflicting rows share a corrected timestamp but have different values.
    conflicting_timestamps = weather.groupby(
        "_corrected_timestamp", dropna=True
    )[raw_value_columns].apply(
        lambda group: len(group.drop_duplicates()) > 1
    )

    conflict_times = conflicting_timestamps[
        conflicting_timestamps
    ].index

    if len(conflict_times):
        for timestamp in conflict_times:
            add_flag(
                flags,
                station_id,
                "all",
                timestamp,
                timestamp,
                "conflicting values at same timestamp",
                "exclude",
                "multiple different rows share this timestamp",
            )

        weather = weather[
            ~weather["_corrected_timestamp"].isin(conflict_times)
        ].copy()

    return weather


def check_gaps(weather, station_id, interval_minutes, flags):
    """Flag gaps larger than the configured logger interval."""
    if weather.empty:
        return

    expected = pd.Timedelta(minutes=interval_minutes)
    ordered = weather.dropna(
        subset=["_corrected_timestamp"]
    ).sort_values("_corrected_timestamp")

    if len(ordered) < 2:
        return

    timestamps = ordered["_corrected_timestamp"].reset_index(drop=True)

    for i in range(1, len(timestamps)):
        previous = timestamps.iloc[i - 1]
        current = timestamps.iloc[i]
        difference = current - previous

        if difference > expected * 1.5:
            missing_intervals = max(
                0, int(round(difference / expected)) - 1
            )
            add_flag(
                flags,
                station_id,
                "all",
                previous + expected,
                current - expected,
                "data gap",
                "exclude",
                f"{missing_intervals} expected interval(s) missing",
            )


# ---------------------------------------------------------------------
# Part 2: impossible values
# ---------------------------------------------------------------------

def check_impossible_values(weather, station_id, flags):
    """Flag impossible sensor values, including temperature error codes."""
    checks = [
        ("temp_c", lambda s: (s < -45) | (s > 50),
         "temperature outside -45..50 C"),
        ("rh_pct", lambda s: (s < 0) | (s > 100),
         RH_RANGE_ISSUE),
        ("rain_mm", lambda s: s < 0, "negative rainfall"),
        ("wind_kmh", lambda s: s < 0, "negative wind speed"),
        ("ghi_wm2", lambda s: s < 0, "negative solar irradiance"),
    ]

    # Standard temperature sensor error codes get their own issue.
    temperature_codes = weather["temp_c"].isin([-40, 85])
    for _, row in weather[temperature_codes].iterrows():
        timestamp = row["_corrected_timestamp"]
        add_flag(
            flags,
            station_id,
            "temp_c",
            timestamp,
            timestamp,
            "temperature sensor error code",
            "exclude",
            f"temperature error code: {row['temp_c']:g} C",
        )

    for channel, predicate, issue in checks:
        values = weather[channel]
        mask = predicate(values)

        if channel == "temp_c":
            mask = mask & ~temperature_codes

        for _, row in weather[mask.fillna(False)].iterrows():
            timestamp = row["_corrected_timestamp"]
            value = row[channel]
            add_flag(
                flags,
                station_id,
                channel,
                timestamp,
                timestamp,
                issue,
                "exclude",
                f"observed value: {value}",
            )


# ---------------------------------------------------------------------
# Temperature spikes
# ---------------------------------------------------------------------

def check_temperature_spikes(weather, station_id, flags):
    """Flag temperatures more than 10 C from a nearby-reading median."""
    ordered = weather.sort_values("_corrected_timestamp").reset_index(
        drop=True
    )
    values = pd.to_numeric(ordered["temp_c"], errors="coerce")

    for i, value in enumerate(values):
        if pd.isna(value) or value in (-40, 85):
            continue

        start = max(0, i - 3)
        end = min(len(values), i + 4)
        neighbours = values.iloc[start:end].copy()

        # Exclude the current row and known temperature error codes.
        neighbours = neighbours.drop(index=i, errors="ignore")
        neighbours = neighbours[
            ~neighbours.isin([-40, 85])
        ].dropna()

        if len(neighbours) < 3:
            continue

        median = neighbours.median()
        if abs(value - median) > 10:
            timestamp = ordered.loc[i, "_corrected_timestamp"]
            add_flag(
                flags,
                station_id,
                "temp_c",
                timestamp,
                timestamp,
                "temperature spike",
                "exclude",
                f"{value:g} C differs from nearby median {median:g} C",
            )


# ---------------------------------------------------------------------
# Stuck sensors and humidity saturation
# ---------------------------------------------------------------------

def check_stuck_sensors(weather, station_id, interval_minutes, flags):
    """Detect long exact-value runs and high humidity saturation runs."""
    ordered = weather.sort_values("_corrected_timestamp").reset_index(
        drop=True
    )
    minimum_duration = pd.Timedelta(hours=6)
    interval = pd.Timedelta(minutes=interval_minutes)

    # Special humidity saturation rule:
    # RH >= 98% for at least 6 hours, with at least one value > 100%.
    rh = pd.to_numeric(ordered["rh_pct"], errors="coerce")
    run_start = None
    run_values = []

    def finish_humidity_run(run_end_index):
        nonlocal run_start, run_values

        if run_start is None or not run_values:
            run_start = None
            run_values = []
            return

        start_time = ordered.loc[run_start, "_corrected_timestamp"]
        end_time = ordered.loc[run_end_index, "_corrected_timestamp"]
        run_series = pd.Series(run_values)

        if (
            end_time - start_time >= minimum_duration
            and (run_series > 100).any()
        ):
            add_flag(
                flags,
                station_id,
                "rh_pct",
                start_time,
                end_time,
                SATURATION_ISSUE,
                "exclude",
                "RH remained at or above 98% for at least 6 hours, "
                "including values above 100%",
            )

        run_start = None
        run_values = []

    for i, value in enumerate(rh):
        timestamp = ordered.loc[i, "_corrected_timestamp"]
        eligible = pd.notna(value) and value >= 98

        if not eligible:
            if run_start is not None:
                finish_humidity_run(i - 1)
            continue

        if run_start is None:
            run_start = i
            run_values = [value]
            continue

        previous_time = ordered.loc[i - 1, "_corrected_timestamp"]
        if pd.isna(timestamp) or pd.isna(previous_time):
            finish_humidity_run(i - 1)
            run_start = i
            run_values = [value]
        elif timestamp - previous_time > interval * 1.5:
            finish_humidity_run(i - 1)
            run_start = i
            run_values = [value]
        else:
            run_values.append(value)

    if run_start is not None:
        finish_humidity_run(len(ordered) - 1)

    # Exact repeated-value runs in the listed channels.
    for channel in (
        "temp_c",
        "rh_pct",
        "pressure_hpa",
        "wind_dir_deg",
    ):
        values = pd.to_numeric(ordered[channel], errors="coerce")
        run_start = 0

        while run_start < len(ordered):
            value = values.iloc[run_start]
            if pd.isna(value):
                run_start += 1
                continue

            run_end = run_start
            while run_end + 1 < len(ordered):
                next_value = values.iloc[run_end + 1]
                if pd.isna(next_value) or next_value != value:
                    break

                time_now = ordered.loc[run_end, "_corrected_timestamp"]
                time_next = ordered.loc[run_end + 1, "_corrected_timestamp"]

                if (
                    pd.isna(time_now)
                    or pd.isna(time_next)
                    or time_next - time_now > interval * 1.5
                ):
                    break

                run_end += 1

            start_time = ordered.loc[run_start, "_corrected_timestamp"]
            end_time = ordered.loc[run_end, "_corrected_timestamp"]

            # The humidity saturation rule above handles this case as one
            # whole-run flag. Do not create a second generic stuck flag
            # for values inside a qualifying saturation run.
            is_saturation_run = (
                channel == "rh_pct"
                and value >= 98
                and end_time - start_time >= minimum_duration
            )

            if (
                run_end > run_start
                and end_time - start_time >= minimum_duration
                and not is_saturation_run
            ):
                add_flag(
                    flags,
                    station_id,
                    channel,
                    start_time,
                    end_time,
                    "sensor stuck at constant value",
                    "exclude",
                    f"constant value {value:g} for "
                    f"{(end_time - start_time).total_seconds() / 3600:.1f} h",
                )

            run_start = run_end + 1


# ---------------------------------------------------------------------
# Solar elevation and nighttime sunlight
# ---------------------------------------------------------------------

def solar_elevation_deg(timestamp, latitude, longitude):
    """Approximate solar elevation for a UTC timestamp and coordinates."""
    timestamp = pd.Timestamp(timestamp)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    day_of_year = timestamp.dayofyear
    hour = (
        timestamp.hour
        + timestamp.minute / 60.0
        + timestamp.second / 3600.0
    )

    fractional_year = (
        2 * math.pi / 365
    ) * (day_of_year - 1 + (hour - 12) / 24)

    equation_time = 229.18 * (
        0.000075
        + 0.001868 * math.cos(fractional_year)
        - 0.032077 * math.sin(fractional_year)
        - 0.014615 * math.cos(2 * fractional_year)
        - 0.040849 * math.sin(2 * fractional_year)
    )

    declination = (
        0.006918
        - 0.399912 * math.cos(fractional_year)
        + 0.070257 * math.sin(fractional_year)
        - 0.006758 * math.cos(2 * fractional_year)
        + 0.000907 * math.sin(2 * fractional_year)
        - 0.002697 * math.cos(3 * fractional_year)
        + 0.00148 * math.sin(3 * fractional_year)
    )

    true_solar_minutes = (
        hour * 60 + equation_time + 4 * longitude
    ) % 1440

    hour_angle = math.radians(true_solar_minutes / 4 - 180)
    lat_rad = math.radians(latitude)

    cosine_zenith = (
        math.sin(lat_rad) * math.sin(declination)
        + math.cos(lat_rad) * math.cos(declination)
        * math.cos(hour_angle)
    )
    cosine_zenith = max(-1.0, min(1.0, cosine_zenith))

    zenith = math.degrees(math.acos(cosine_zenith))
    return 90 - zenith


def check_sunlight_at_night(
    weather, station_id, latitude, longitude, interval_minutes, flags
):
    """Flag GHI > 20 W/m2 at night, using interval midpoint timestamps."""
    if pd.isna(latitude) or pd.isna(longitude):
        return

    ordered = weather.dropna(
        subset=["_corrected_timestamp"]
    ).sort_values("_corrected_timestamp").copy()

    if ordered.empty:
        return

    ordered["_midpoint"] = (
        ordered["_corrected_timestamp"]
        - pd.Timedelta(minutes=interval_minutes / 2)
    )

    night_mask = []
    for _, row in ordered.iterrows():
        ghi = row["ghi_wm2"]
        if pd.isna(ghi) or ghi <= 20:
            night_mask.append(False)
            continue

        elevation = solar_elevation_deg(
            row["_midpoint"], latitude, longitude
        )
        night_mask.append(elevation < -3)

    ordered["_night_sunlight"] = night_mask
    ordered["_utc_day"] = ordered["_midpoint"].dt.strftime("%Y-%m-%d")

    qualifying = ordered[ordered["_night_sunlight"]]

    # Require at least three qualifying readings per UTC day.
    for _, day_group in qualifying.groupby("_utc_day"):
        if len(day_group) < 3:
            continue

        add_flag(
            flags,
            station_id,
            "ghi_wm2",
            day_group["_corrected_timestamp"].min(),
            day_group["_corrected_timestamp"].max(),
            "sunlight recorded at night",
            "exclude",
            f"{len(day_group)} readings with GHI > 20 W/m2 "
            "while solar elevation was below -3 degrees",
        )


# ---------------------------------------------------------------------
# GPS position check
# ---------------------------------------------------------------------

def check_gps_position(gps, station_row, station_id, flags, weather=None):
    """Flag a station whose median valid GPS position is >500 m from metadata."""
    if gps.empty:
        return

    latitudes = pd.to_numeric(gps["lat"], errors="coerce").dropna()
    longitudes = pd.to_numeric(gps["lon"], errors="coerce").dropna()

    if latitudes.empty or longitudes.empty:
        return

    station_lat = pd.to_numeric(
        pd.Series([station_row.get("lat")]), errors="coerce"
    ).iloc[0]
    station_lon = pd.to_numeric(
        pd.Series([station_row.get("lon")]), errors="coerce"
    ).iloc[0]

    if pd.isna(station_lat) or pd.isna(station_lon):
        return

    median_lat = latitudes.median()
    median_lon = longitudes.median()

    distance = haversine_m(
        station_lat, station_lon, median_lat, median_lon
    )

    if distance > 500:
        start = end = None
        if weather is not None and not weather.empty:
            start = weather["_corrected_timestamp"].min()
            end = weather["_corrected_timestamp"].max()
        add_flag(
            flags,
            station_id,
            "metadata",
            start,
            end,
            "GPS position differs from station metadata",
            "correct",
            f"median valid GPS position {median_lat:.4f}, {median_lon:.4f} is "
            f"{distance / 1000:.1f} km from stations.csv ({station_lat:.4f}, {station_lon:.4f}) "
            f"and steady all period; fix the metadata, the data itself is fine.",
        )


# ---------------------------------------------------------------------
# Flag filtering and merging
# ---------------------------------------------------------------------

def suppress_rh_range_flags_inside_saturation(flags):
    """Remove RH range flags fully contained in a same-station saturation flag.

    A range flag is removed only when:
      - station_id matches;
      - both flags refer to channel rh_pct;
      - the issue is relative humidity outside 0..100%;
      - its start and end are both within the saturation flag's time range.
    """
    saturation_flags = [
        flag for flag in flags
        if flag["issue"] == SATURATION_ISSUE
        and flag["channel"] == "rh_pct"
        and flag["start_utc"] is not None
        and flag["end_utc"] is not None
        and not pd.isna(flag["start_utc"])
        and not pd.isna(flag["end_utc"])
    ]

    filtered = []

    for flag in flags:
        if (
            flag["issue"] != RH_RANGE_ISSUE
            or flag["channel"] != "rh_pct"
            or flag["start_utc"] is None
            or flag["end_utc"] is None
            or pd.isna(flag["start_utc"])
            or pd.isna(flag["end_utc"])
        ):
            filtered.append(flag)
            continue

        contained = any(
            saturation["station_id"] == flag["station_id"]
            and saturation["channel"] == flag["channel"]
            and saturation["start_utc"] <= flag["start_utc"]
            and saturation["end_utc"] >= flag["end_utc"]
            for saturation in saturation_flags
        )

        if not contained:
            filtered.append(flag)

    return filtered


def merge_consecutive_flags(flags, interval_minutes=10):
    """Merge overlapping or consecutive flags with the same identity."""
    if not flags:
        return []

    interval = pd.Timedelta(minutes=interval_minutes)
    sortable = []

    for flag in flags:
        item = flag.copy()
        item["_start"] = item.get("start_utc")
        item["_end"] = item.get("end_utc")
        item["_notes"] = [str(item.get("note", ""))]
        item["_count"] = 1
        sortable.append(item)

    sortable.sort(key=lambda item: (
        item["station_id"],
        item["channel"],
        item["issue"],
        pd.Timestamp.min.tz_localize("UTC")
        if item["_start"] is None or pd.isna(item["_start"])
        else pd.Timestamp(item["_start"]),
    ))

    merged = []

    for item in sortable:
        if not merged:
            merged.append(item)
            continue

        previous = merged[-1]

        same_identity = (
            previous["station_id"] == item["station_id"]
            and previous["channel"] == item["channel"]
            and previous["issue"] == item["issue"]
            and previous["action"] == item["action"]
        )

        previous_start = previous["_start"]
        previous_end = previous["_end"]
        current_start = item["_start"]
        current_end = item["_end"]

        both_timed = all(
            value is not None and not pd.isna(value)
            for value in (
                previous_start, previous_end, current_start, current_end
            )
        )

        if same_identity and both_timed and current_start <= previous_end + interval:
            previous["_end"] = max(previous_end, current_end)
            previous["_count"] += item["_count"]
            previous["_notes"].extend(item["_notes"])
        else:
            merged.append(item)

    result = []

    for item in merged:
        start = item["_start"]
        end = item["_end"]
        note = str(item.get("note", ""))

        if item["_count"] > 1:
            if item["issue"] == "exact duplicate row":
                note = (
                    "drop the repeated copy; "
                    f"{item['_count']} repeated row(s)"
                )
            elif item["_notes"]:
                unique_notes = list(dict.fromkeys(item["_notes"]))
                if len(unique_notes) == 1:
                    note = unique_notes[0]
                else:
                    note = "; ".join(unique_notes)

        result.append({
            "station_id": item["station_id"],
            "channel": item["channel"],
            "start_utc": timestamp_text(start),
            "end_utc": timestamp_text(end),
            "issue": item["issue"],
            "action": item["action"],
            "note": note,
        })

    return result


# ---------------------------------------------------------------------
# Station processing and main
# ---------------------------------------------------------------------

def process_station(pack_folder, station_row):
    station_id = str(station_row["station_id"])
    data_path = pack_folder / "data" / f"{station_id}.csv"
    gps_path = pack_folder / "gps" / f"{station_id}.nmea"
    flags = []

    if not data_path.exists():
        add_flag(
            flags, station_id, "all", None, None,
            "missing weather data file", "exclude",
            f"file not found: {data_path}",
        )
        return flags

    weather = pd.read_csv(data_path)

    missing_columns = [
        column for column in WEATHER_COLUMNS
        if column not in weather.columns
    ]
    if missing_columns:
        add_flag(
            flags, station_id, "all", None, None,
            "missing required columns", "exclude",
            ", ".join(missing_columns),
        )
        return flags

    weather["timestamp_utc"] = weather["timestamp_utc"].apply(parse_utc)

    for column in WEATHER_COLUMNS[1:]:
        weather[column] = pd.to_numeric(
            weather[column], errors="coerce"
        )

    interval_value = pd.to_numeric(
        pd.Series([station_row.get("logger_interval_min", 10)]),
        errors="coerce",
    ).iloc[0]
    interval_minutes = (
        int(interval_value)
        if pd.notna(interval_value) and interval_value > 0
        else 10
    )

    gps = load_gps(gps_path)

    # Part 1: correct clock before duplicate, conflict, and gap checks.
    weather = correct_clock(
        weather, gps, station_id, interval_minutes, flags
    )
    weather = handle_duplicates_and_conflicts(
        weather, station_id, flags
    )
    check_gaps(weather, station_id, interval_minutes, flags)

    # Part 2: data-quality checks on corrected timestamps.
    check_impossible_values(weather, station_id, flags)
    check_temperature_spikes(weather, station_id, flags)
    check_stuck_sensors(
        weather, station_id, interval_minutes, flags
    )

    latitude = pd.to_numeric(
        pd.Series([station_row.get("lat")]), errors="coerce"
    ).iloc[0]
    longitude = pd.to_numeric(
        pd.Series([station_row.get("lon")]), errors="coerce"
    ).iloc[0]

    check_sunlight_at_night(
        weather,
        station_id,
        latitude,
        longitude,
        interval_minutes,
        flags,
    )
    check_gps_position(gps, station_row, station_id, flags, weather)

    return flags


def main():
    if len(sys.argv) != 2:
        print("Usage: python check.py <pack_folder>", file=sys.stderr)
        sys.exit(1)

    pack_folder = Path(sys.argv[1])

    stations_path = pack_folder / "stations.csv"
    if not stations_path.exists():
        print(
            f"Error: stations.csv not found in {pack_folder}",
            file=sys.stderr,
        )
        sys.exit(1)

    stations = pd.read_csv(stations_path, dtype={"station_id": str})

    all_flags = []
    for _, station_row in stations.iterrows():
        all_flags.extend(process_station(pack_folder, station_row))

    # IMPORTANT: suppress RH out-of-range flags fully contained within
    # a same-station/channel humidity saturation flag before merging.
    all_flags = suppress_rh_range_flags_inside_saturation(all_flags)

    interval_values = pd.to_numeric(
        stations.get(
            "logger_interval_min",
            pd.Series([10] * len(stations)),
        ),
        errors="coerce",
    ).dropna()

    default_interval = (
        int(interval_values.median()) if not interval_values.empty else 10
    )

    merged_flags = merge_consecutive_flags(
        all_flags, interval_minutes=default_interval
    )

    output = pd.DataFrame(merged_flags, columns=OUTPUT_COLUMNS)
    output.to_csv("flags.csv", index=False)

    print(f"Wrote {len(output)} flags to flags.csv")


if __name__ == "__main__":
    main()