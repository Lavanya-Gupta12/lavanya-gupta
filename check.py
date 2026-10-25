
import sys
import re
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


def parse_nmea_checksum(sentence):
    """Validate the XOR checksum of an NMEA sentence."""
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
    """Convert NMEA hhmmss.ss into seconds since midnight."""
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
    """Convert RMC ddmmyy into a UTC-midnight timestamp."""
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


def format_utc(value):
    """Format a timestamp as YYYY-MM-DD HH:MM in UTC."""
    if value is None or pd.isna(value):
        return ""

    timestamp = pd.Timestamp(value)

    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    else:
        timestamp = timestamp.tz_convert("UTC")

    return timestamp.strftime("%Y-%m-%d %H:%M")


def add_flag(
    flags,
    station_id,
    channel,
    start,
    end,
    issue,
    action,
    note,
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
    """
    Read GPS records in file order.

    Valid RMC records with status A provide authoritative GPS UTC.
    GGA records are checksum-validated but cannot establish a date.
    Invalid sentences are ignored.
    """
    records = []

    if not path.exists():
        return pd.DataFrame(
            columns=[
                "gps_row",
                "logger_time",
                "gps_time",
                "sentence_type",
            ]
        )

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

            body = sentence[1:sentence.rfind("*")]
            fields = body.split(",")
            sentence_type = fields[0] if fields else ""

            if sentence_type != "GPRMC":
                continue

            if len(fields) < 10 or fields[2].strip() != "A":
                continue

            seconds = parse_nmea_time(fields[1])
            date = parse_nmea_date(fields[9])

            if seconds is None or date is None:
                continue

            gps_time = date + pd.to_timedelta(seconds, unit="s")

            records.append({
                "gps_row": gps_row,
                "logger_time": logger_time,
                "gps_time": gps_time,
                "sentence_type": sentence_type,
            })

    return pd.DataFrame(records, columns=[
        "gps_row",
        "logger_time",
        "gps_time",
        "sentence_type",
    ])


def split_weather_segments(df):
    """
    Split original file-order rows at every backward timestamp jump.
    Invalid timestamps break the sequence.
    """
    segments = []
    current = []
    previous = None

    for index, timestamp in zip(
        df.index, df["timestamp_utc"]
    ):
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


def split_gps_segments(gps):
    """
    Split GPS records in file order at backward jumps in logger time.
    """
    if gps.empty:
        return []

    segments = []
    current = []
    previous = None

    for index, row in gps.iterrows():
        timestamp = row["logger_time"]

        if previous is not None and timestamp < previous:
            segments.append(current)
            current = []

        current.append(index)
        previous = timestamp

    if current:
        segments.append(current)

    return segments


def estimate_segment_offset(weather, gps, tolerance_seconds):
    """
    Match weather rows to GPS rows by segment position and nearest
    logger timestamp within that segment.

    Return median(logger time - GPS time) in seconds, or None if
    insufficient matching GPS evidence exists.
    """
    if weather.empty or gps.empty:
        return None

    weather = weather.sort_values(
        "_file_row", kind="stable"
    ).copy()

    gps = gps.sort_values("gps_row", kind="stable").copy()

    # Match on logger timestamps within this segment only.
    # This prevents a later local-time segment from matching an earlier
    # UTC segment merely because their timestamps overlap.
    left = weather[
        ["_file_row", "timestamp_utc"]
    ].sort_values("timestamp_utc")

    right = gps[
        ["logger_time", "gps_time", "gps_row"]
    ].sort_values("logger_time")

    matched = pd.merge_asof(
        left,
        right,
        left_on="timestamp_utc",
        right_on="logger_time",
        direction="nearest",
        tolerance=pd.Timedelta(seconds=tolerance_seconds),
    )

    matched = matched.dropna(subset=["gps_time"])

    if matched.empty:
        return None

    offsets = (
        matched["timestamp_utc"] - matched["gps_time"]
    ).dt.total_seconds()

    offsets = offsets.replace([np.inf, -np.inf], np.nan).dropna()

    if offsets.empty:
        return None

    return float(np.median(offsets))


def flag_duplicate_rows(df, station_id, flags):
    """
    Flag exact repeated copies, grouping consecutive repeated rows
    into a single flag with a row count.

    The original row is retained. A repeated copy is flagged as
    correct with the required note.
    """
    if df.empty:
        return df

    value_columns = [
        col for col in df.columns
        if col not in {"_file_row", "_segment"}
    ]

    duplicate_mask = df.duplicated(
        subset=value_columns, keep="first"
    )

    duplicate_indices = df.index[duplicate_mask].tolist()

    if not duplicate_indices:
        return df

    # Consecutive duplicate records are grouped by adjacent source-row
    # positions. Non-consecutive repetitions remain separate groups.
    groups = []
    current = [duplicate_indices[0]]

    for index in duplicate_indices[1:]:
        previous = current[-1]

        if index == previous + 1:
            current.append(index)
        else:
            groups.append(current)
            current = [index]

    groups.append(current)

    for group in groups:
        first_time = df.loc[group[0], "_corrected_timestamp"]
        last_time = df.loc[group[-1], "_corrected_timestamp"]

        add_flag(
            flags,
            station_id,
            "all",
            first_time,
            last_time,
            "exact duplicate row(s)",
            "correct",
            f"drop the repeated copy; {len(group)} repeated row(s).",
        )

    return df.drop(index=duplicate_indices).copy()


def flag_conflicting_timestamps(df, station_id, flags):
    """
    Exclude rows sharing corrected UTC timestamps when their values
    differ. Exact duplicates should already have been removed.
    """
    if df.empty:
        return df

    conflicting = df[
        df.duplicated("_corrected_timestamp", keep=False)
    ]

    if conflicting.empty:
        return df

    conflicting_times = sorted(
        conflicting["_corrected_timestamp"].unique()
    )

    for timestamp in conflicting_times:
        timestamp = pd.Timestamp(timestamp)

        add_flag(
            flags,
            station_id,
            "all",
            timestamp,
            timestamp,
            "conflicting duplicate timestamp",
            "exclude",
            "Different readings share the same corrected UTC timestamp.",
        )

    return df[
        ~df["_corrected_timestamp"].isin(conflicting_times)
    ].copy()


def flag_gaps(df, station_id, interval_minutes, flags):
    """Flag missing expected intervals after clock correction."""
    if len(df) < 2:
        return

    df = df.sort_values(
        "_corrected_timestamp", kind="stable"
    ).reset_index(drop=True)

    expected = pd.Timedelta(minutes=interval_minutes)

    for i in range(1, len(df)):
        previous = df.loc[i - 1, "_corrected_timestamp"]
        current = df.loc[i, "_corrected_timestamp"]

        delta = current - previous

        if delta <= expected * 1.5:
            continue

        # Expected missing sample times, not the bounding observations.
        missing_start = previous + expected
        missing_end = current - expected

        if missing_start > missing_end:
            continue

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
                f"the gap between valid observations is "
                f"{delta.total_seconds() / 60:g} minutes."
            ),
        )


def process_station(
    station_id,
    data_path,
    gps_path,
    interval_minutes,
    flags,
):
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

    invalid = raw[raw["timestamp_utc"].isna()]

    for _, row in invalid.iterrows():
        add_flag(
            flags, station_id, "all", pd.NaT, pd.NaT,
            "invalid logger timestamp", "exclude",
            f"CSV row {int(row['_file_row']) + 2} has an invalid timestamp.",
        )

    raw = raw[raw["timestamp_utc"].notna()].copy()

    if raw.empty:
        return

    # Split BEFORE any duplicate handling, so a clock reset cannot
    # disappear when overlapping timestamps are removed.
    weather_segments = split_weather_segments(raw)
    gps = load_gps(gps_path)
    gps_segment_indices = split_gps_segments(gps)

    gps_segments = [
        gps.loc[indices].copy()
        for indices in gps_segment_indices
    ]

    interval_seconds = interval_minutes * 60.0
    tolerance_seconds = max(120.0, interval_seconds * 0.5)

    corrected_segments = []

    for segment_number, indices in enumerate(
        weather_segments
    ):
        segment = raw.loc[indices].copy()
        segment["_segment"] = segment_number
        segment["_corrected_timestamp"] = segment["timestamp_utc"]

        # Match each weather segment to the GPS segment with the closest
        # file-order position. Logger timestamps alone are insufficient
        # when both clocks jump or move to local time.
        gps_segment = pd.DataFrame()

        if gps_segments:
            weather_position = float(
                segment["_file_row"].median()
            )

            def gps_distance(candidate):
                return abs(
                    float(candidate["gps_row"].median())
                    - weather_position
                )

            # GPS and weather files need not contain the same number of
            # rows. Select by normalized position in their respective
            # files, then estimate the offset using timestamp matches.
            weather_fraction = (
                weather_position / max(len(raw) - 1, 1)
            )

            distances = []
            for candidate in gps_segments:
                gps_position = float(candidate["gps_row"].median())
                gps_fraction = gps_position / max(
                    int(gps["gps_row"].max()), 1
                )
                distances.append(
                    abs(weather_fraction - gps_fraction)
                )

            candidate_index = int(np.argmin(distances))
            gps_segment = gps_segments[candidate_index]

        offset_seconds = estimate_segment_offset(
            segment,
            gps_segment,
            tolerance_seconds,
        )

        # Require the two-minute threshold and use the segment's median
        # logger-minus-GPS offset.
        if (
            offset_seconds is not None
            and abs(offset_seconds) >= 120.0
        ):
            shift = pd.to_timedelta(offset_seconds, unit="s")

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
                    f"Shifted timestamps by {-offset_seconds:.1f} seconds; "
                    f"median logger-minus-RMC offset was "
                    f"{offset_seconds:.1f} seconds in clock segment "
                    f"{segment_number + 1}."
                ),
            )

        corrected_segments.append(segment)

    if not corrected_segments:
        return

    corrected = pd.concat(
        corrected_segments, ignore_index=True
    )

    # Check duplicates only after each segment has its corrected UTC times.
    # Preserve original measurement columns for exact-row comparison.
    corrected = flag_duplicate_rows(
        corrected, station_id, flags
    )

    corrected = flag_conflicting_timestamps(
        corrected, station_id, flags
    )

    # Do not compare across a clock-reset boundary when assessing gaps.
    for _, segment in corrected.groupby(
        "_segment", sort=False
    ):
        flag_gaps(
            segment,
            station_id,
            interval_minutes,
            flags,
        )


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
