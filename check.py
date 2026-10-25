
import sys
import re
from pathlib import Path
from collections import defaultdict

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


def parse_nmea_checksum(sentence):
    """Return True if an NMEA sentence has a valid XOR checksum."""
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
    """Parse NMEA hhmmss.ss into a time of day."""
    match = re.fullmatch(r"(\d{2})(\d{2})(\d{2}(?:\.\d+)?)", value.strip())

    if not match:
        return None

    hour, minute = int(match.group(1)), int(match.group(2))
    seconds = float(match.group(3))

    if hour > 23 or minute > 59 or seconds >= 60:
        return None

    return hour * 3600 + minute * 60 + seconds


def parse_nmea_date(value):
    """Parse RMC date ddmmyy using the conventional NMEA year pivot."""
    if not re.fullmatch(r"\d{6}", value.strip()):
        return None

    day, month, year = int(value[:2]), int(value[2:4]), int(value[4:6])
    year += 2000 if year < 80 else 1900

    try:
        return pd.Timestamp(year=year, month=month, day=day)
    except ValueError:
        return None


def parse_gps_file(path):
    """
    Read logger_timestamp,$GPRMC or $GPGGA sentence records.
    Only checksum-valid RMC records with status A establish GPS UTC.
    """
    records = []

    if not path.exists():
        return pd.DataFrame(
            columns=["logger_time", "gps_time", "sentence_type"]
        )

    with path.open("r", encoding="utf-8-sig", errors="replace") as file:
        for line in file:
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

            if pd.isna(logger_time) or not parse_nmea_checksum(sentence):
                continue

            fields = sentence[1:sentence.rfind("*")].split(",")
            sentence_type = fields[0] if fields else ""

            if sentence_type == "GPRMC":
                if len(fields) < 10 or fields[2].strip() != "A":
                    continue

                seconds = parse_nmea_time(fields[1])
                date = parse_nmea_date(fields[9])

                if seconds is None or date is None:
                    continue

                midnight = date.tz_localize("UTC")
                gps_time = midnight + pd.to_timedelta(seconds, unit="s")

                # RMC dates are authoritative; do not silently replace them
                # with the logger date.
                records.append({
                    "logger_time": logger_time,
                    "gps_time": gps_time,
                    "sentence_type": "GPRMC",
                })

            elif sentence_type == "GPGGA":
                # GGA has no date. It cannot independently establish UTC.
                # Checksum-valid GGA records are intentionally not used
                # to estimate the logger's UTC offset.
                continue

    result = pd.DataFrame(records)

    if result.empty:
        return pd.DataFrame(
            columns=["logger_time", "gps_time", "sentence_type"]
        )

    return result.sort_values("logger_time", kind="stable").reset_index(drop=True)


def format_utc(value):
    """Format a timestamp in UTC to YYYY-MM-DD HH:MM."""
    if pd.isna(value):
        return ""

    value = pd.Timestamp(value)

    if value.tzinfo is None:
        value = value.tz_localize("UTC")
    else:
        value = value.tz_convert("UTC")

    return value.strftime("%Y-%m-%d %H:%M")


def add_flag(flags, station_id, channel, start, end,
             issue, action, note):
    flags.append({
        "station_id": str(station_id),
        "channel": channel,
        "start_utc": format_utc(start),
        "end_utc": format_utc(end),
        "issue": issue,
        "action": action,
        "note": note,
    })


def split_clock_jumps(df):
    """
    Split chronologically ordered file rows whenever the logger timestamp
    moves backwards. Each resulting segment is independently clock-checked.
    """
    if df.empty:
        return []

    timestamps = df["timestamp_utc"]
    valid = timestamps.notna()

    segments = []
    current = []
    previous = None

    for index in df.index:
        timestamp = timestamps.loc[index]

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


def process_station(station_id, data_path, gps_path,
                    interval_minutes, flags):
    if not data_path.exists():
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "missing station data file", "exclude",
            "The station CSV file does not exist.",
        )
        return

    try:
        df = pd.read_csv(data_path)
    except Exception as exc:
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "unreadable station data file", "exclude",
            f"Could not read the CSV file: {exc}",
        )
        return

    if "timestamp_utc" not in df.columns:
        add_flag(
            flags, station_id, "metadata", pd.NaT, pd.NaT,
            "missing timestamp column", "exclude",
            "The required timestamp_utc column is absent.",
        )
        return

    df["_original_row"] = np.arange(len(df))
    df["timestamp_utc"] = pd.to_datetime(
        df["timestamp_utc"], errors="coerce", utc=True
    )

    # A timestamp that cannot be parsed cannot be assigned a reliable
    # UTC interval.
    for index in df.index[df["timestamp_utc"].isna()]:
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "invalid logger timestamp", "exclude",
            f"Timestamp in CSV row {index + 2} could not be parsed.",
        )

    valid_df = df.loc[df["timestamp_utc"].notna()].copy()

    if valid_df.empty:
        return

    # Exact duplicates are rows identical across all original columns.
    # A repeated copy is flagged as correct; retain the first occurrence.
    original_columns = [
        col for col in df.columns
        if col != "_original_row"
    ]
    duplicate_mask = valid_df.duplicated(
        subset=original_columns, keep="first"
    )

    for index in valid_df.index[duplicate_mask]:
        timestamp = valid_df.at[index, "timestamp_utc"]
        add_flag(
            flags, station_id, "all", timestamp, timestamp,
            "exact duplicate row", "correct",
            "drop the repeated copy",
        )

    valid_df = valid_df.loc[~duplicate_mask].copy()

    # Distinct records with the same timestamp are ambiguous. Flag every
    # conflicting timestamp group and exclude the observations at that time.
    conflicting_times = set(
        valid_df.loc[
            valid_df.duplicated("timestamp_utc", keep=False),
            "timestamp_utc",
        ].tolist()
    )

    for timestamp in sorted(conflicting_times):
        add_flag(
            flags, station_id, "all", timestamp, timestamp,
            "conflicting duplicate timestamp", "exclude",
            "Multiple rows share this timestamp but contain different values.",
        )

    valid_df = valid_df.loc[
        ~valid_df["timestamp_utc"].isin(conflicting_times)
    ].copy()

    if valid_df.empty:
        return

    # Read valid GPS time observations. Match each logger record to its
    # closest RMC logger timestamp, using a tolerance based on the sampling
    # interval. Each logger-clock segment is estimated separately.
    gps = parse_gps_file(gps_path)

    valid_df = valid_df.sort_values(
        "_original_row", kind="stable"
    ).copy()

    segments = split_clock_jumps(valid_df)

    corrected_parts = []

    interval_seconds = float(interval_minutes) * 60.0
    gps_tolerance_seconds = max(120.0, interval_seconds * 0.5)

    for segment_number, indices in enumerate(segments, start=1):
        segment = valid_df.loc[indices].copy()
        segment["_corrected_timestamp"] = segment["timestamp_utc"]

        if not gps.empty:
            segment_start = segment["timestamp_utc"].min()
            segment_end = segment["timestamp_utc"].max()

            candidates = gps.loc[
                gps["logger_time"].between(
                    segment_start - pd.Timedelta(seconds=gps_tolerance_seconds),
                    segment_end + pd.Timedelta(seconds=gps_tolerance_seconds),
                )
            ].copy()

            if not candidates.empty:
                # Match each station timestamp to the nearest GPS logger
                # timestamp. Use each valid GPS fix at most once.
                left = segment[["_original_row", "timestamp_utc"]].copy()
                left = left.sort_values("timestamp_utc")

                right = candidates[
                    ["logger_time", "gps_time"]
                ].sort_values("logger_time")

                matched = pd.merge_asof(
                    left,
                    right,
                    left_on="timestamp_utc",
                    right_on="logger_time",
                    direction="nearest",
                    tolerance=pd.Timedelta(
                        seconds=gps_tolerance_seconds
                    ),
                )

                matched = matched.dropna(subset=["gps_time"])

                if not matched.empty:
                    offsets = (
                        matched["timestamp_utc"] - matched["gps_time"]
                    ).dt.total_seconds()

                    offset_seconds = float(np.median(offsets))

                    # Correct only when the median logger-GPS offset is
                    # at least two minutes in magnitude.
                    if abs(offset_seconds) >= 120.0:
                        shift = pd.to_timedelta(
                            offset_seconds, unit="s"
                        )

                        segment["_corrected_timestamp"] = (
                            segment["timestamp_utc"] - shift
                        )

                        add_flag(
                            flags,
                            station_id,
                            "timestamp_utc",
                            segment["_corrected_timestamp"].min(),
                            segment["_corrected_timestamp"].max(),
                            "logger clock offset",
                            "correct",
                            (
                                f"Shifted timestamps by "
                                f"{-offset_seconds:.1f} seconds; "
                                f"median logger-minus-RMC offset was "
                                f"{offset_seconds:.1f} seconds in "
                                f"clock segment {segment_number}."
                            ),
                        )

        corrected_parts.append(segment)

    corrected = pd.concat(corrected_parts, ignore_index=True)

    # Check sampling gaps after clock correction. Sort by true corrected UTC
    # time; do not bridge invalid timestamps or invent missing measurements.
    corrected = corrected.sort_values(
        "_corrected_timestamp", kind="stable"
    ).reset_index(drop=True)

    corrected_times = corrected["_corrected_timestamp"]
    expected = pd.Timedelta(minutes=float(interval_minutes))

    if len(corrected) >= 2:
        for i in range(1, len(corrected)):
            previous = corrected_times.iloc[i - 1]
            current = corrected_times.iloc[i]

            delta = current - previous

            if delta > expected * 1.5:
                # Report the expected missing slots between observations.
                missing_start = previous + expected
                missing_end = current - expected

                add_flag(
                    flags,
                    station_id,
                    "all",
                    missing_start,
                    missing_end,
                    "missing logger interval(s)",
                    "exclude",
                    (
                        f"Expected {interval_minutes:g}-minute sampling; "
                        f"observed a {delta.total_seconds() / 60.0:g}-"
                        f"minute gap between valid timestamps."
                    ),
                )


def main():
    if len(sys.argv) != 2:
        print("Usage: python check.py <pack_folder>", file=sys.stderr)
        return 2

    pack = Path(sys.argv[1])

    if not pack.is_dir():
        print(f"Error: pack folder not found: {pack}", file=sys.stderr)
        return 2

    stations_file = pack / "stations.csv"

    if not stations_file.exists():
        print(
            f"Error: stations.csv not found in {pack}",
            file=sys.stderr,
        )
        return 2

    try:
        stations = pd.read_csv(stations_file)
    except Exception as exc:
        print(f"Error reading stations.csv: {exc}", file=sys.stderr)
        return 2

    required = {"station_id", "logger_interval_min"}

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

        interval = pd.to_numeric(
            station["logger_interval_min"], errors="coerce"
        )

        if pd.isna(interval) or interval <= 0:
            add_flag(
                flags, station_id, "metadata", pd.NaT, pd.NaT,
                "invalid logger interval", "exclude",
                "logger_interval_min must be a positive number.",
            )
            continue

        process_station(
            station_id=station_id,
            data_path=pack / "data" / f"{station_id}.csv",
            gps_path=pack / "gps" / f"{station_id}.nmea",
            interval_minutes=float(interval),
            flags=flags,
        )

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
