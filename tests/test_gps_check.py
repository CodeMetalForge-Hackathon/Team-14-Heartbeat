"""GNSS diagnostic parsing and fix validity; no serial devices are opened."""

import pytest

from tools.gps_check import GPSStatus, parse_nmea, valid_coordinate


def nmea(body):
    checksum = 0
    for value in body.encode():
        checksum ^= value
    return f"${body}*{checksum:02X}\r\n".encode()


def gga(*, quality="1", lat="4807.038", lon="01131.000", satellites="08"):
    return nmea(f"GNGGA,123519,{lat},N,{lon},E,{quality},{satellites},0.9,545.4,M,46.9,M,,")


def test_valid_checksum_and_position_are_required_for_fix():
    state = GPSStatus()
    state.feed(gga(), 10)
    assert state.summary(11, 3)["current_fix"]
    corrupted = gga().replace(b"GNGGA", b"GPGGA")
    with pytest.raises(ValueError, match="checksum"):
        parse_nmea(corrupted)
    state.feed(corrupted, 11)
    assert state.invalid == 1 and state.messages["GGA"] == 1
    assert not state.summary(14, 3)["current_fix"]


@pytest.mark.parametrize("change", [
    {"quality": "0"}, {"quality": "6"}, {"lat": ""}, {"lon": ""},
    {"lat": "4861.0"}, {"lat": "9100.0"}, {"lon": "18100.0"},
    {"lat": "nan"}, {"satellites": "00"},
])
def test_no_fix_estimated_or_invalid_coordinates_never_pass(change):
    state = GPSStatus()
    state.feed(gga(**change), 10)
    assert not state.summary(10, 3)["current_fix"]


def test_recent_rmc_void_status_invalidates_a_positive_gga():
    state = GPSStatus()
    state.feed(gga(), 10)
    state.feed(nmea("GNRMC,123519,V,,,,,,,,,,N"), 10)
    assert not state.summary(10, 3)["current_fix"]
    state.feed(nmea("GNRMC,123519,A,4807.038,N,01131.000,E,0,0,230394,,,A"), 11)
    assert state.summary(11, 3)["current_fix"]


def test_fix_loss_clears_old_position_and_report_contains_no_coordinates():
    state = GPSStatus()
    state.feed(gga(), 10)
    state.feed(gga(quality="0", lat="", lon="", satellites="00"), 11)
    report = state.summary(11, 3)
    assert not report["current_fix"] and report["gga_fix_samples"] == 1
    assert "4807" not in str(report) and "01131" not in str(report)


def test_visible_satellites_are_not_added_across_repeated_signal_pages():
    state = GPSStatus()
    for _ in range(2):
        state.feed(nmea("GPGSV,1,1,03,01,40,100,30"), 10)
    state.feed(nmea("GAGSV,1,1,02,01,40,100,30"), 10)
    assert state.summary(10, 3)["max_visible_by_talker"] == {"GP": 3, "GA": 2}


def test_coordinate_directions_must_be_single_matching_hemispheres():
    assert not valid_coordinate("4807.038", "NS", latitude=True)
    assert not valid_coordinate("4807.038", "E", latitude=True)
    assert valid_coordinate("9000.0", "S", latitude=True)
