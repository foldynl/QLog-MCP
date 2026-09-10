"""Shared SQLite fixtures for QLog MCP tests."""

import json
import sqlite3

import pytest


@pytest.fixture
def qlog_database(tmp_path):
    path = tmp_path / "qlog.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE schema_versions (version INTEGER PRIMARY KEY);
        INSERT INTO schema_versions VALUES (40);

        CREATE TABLE station_profiles (
            profile_name TEXT PRIMARY KEY,
            callsign TEXT NOT NULL,
            locator TEXT NOT NULL
        );
        INSERT INTO station_profiles VALUES ('Portable', 'OK1MLG', 'JO70AA');
        INSERT INTO station_profiles VALUES ('Home', 'OK1MLG', 'JO80BB');

        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY,
            start_time TEXT,
            end_time TEXT,
            callsign TEXT NOT NULL,
            rst_sent TEXT,
            rst_rcvd TEXT,
            freq REAL,
            band TEXT,
            mode TEXT,
            submode TEXT,
            dxcc INTEGER,
            country TEXT,
            cont TEXT,
            station_callsign TEXT,
            operator TEXT,
            my_gridsquare TEXT,
            qsl_rcvd TEXT,
            lotw_qsl_rcvd TEXT,
            pota_ref TEXT,
            fields JSON
        );

        CREATE TABLE contacts_autovalue (
            contactid INTEGER PRIMARY KEY REFERENCES contacts(id) ON DELETE CASCADE,
            base_callsign TEXT,
            wavelog_qso_upload_status TEXT,
            wavelog_qso_upload_date TEXT
        );

        CREATE TABLE bands (
            name TEXT PRIMARY KEY,
            start_freq REAL,
            end_freq REAL
        );
        INSERT INTO bands VALUES
            ('40m', 7.0, 7.3),
            ('20m', 14.0, 14.35),
            ('15m', 21.0, 21.45);
        """
    )
    rows = [
        (
            1,
            "2025-12-31T23:59:00Z",
            "2026-01-01T00:01:00Z",
            "JA1AAA",
            "599",
            "579",
            14.025,
            "20m",
            "CW",
            None,
            339,
            "Japan",
            "AS",
            "OK1MLG",
            "OK1MLG",
            "JO70AA",
            "N",
            "Y",
            None,
            json.dumps({"app_qlog_test": "old"}),
        ),
        (
            2,
            "2026-01-15T12:30:00Z",
            "2026-01-15T12:31:00Z",
            "JA2BBB",
            "-10",
            "-12",
            21.074,
            "15m",
            "MFSK",
            "FT8",
            339,
            "Japan",
            "AS",
            "OK1MLG",
            "OK1MLG",
            "JO70AA",
            "N",
            "Y",
            "JA-0001",
            json.dumps({"app_qlog_test": {"value": "new", "type": "S"}}),
        ),
        (
            3,
            "2026-02-20 08:00:00",
            "2026-02-20 08:05:00",
            "JA3CCC",
            "599",
            "599",
            14.035,
            "20m",
            "CW",
            None,
            339,
            "Japan",
            "AS",
            "OK1MLG",
            "OK1MLG",
            "JO70AA",
            "Y",
            "N",
            "",
            "not-json",
        ),
        (
            4,
            "2026-02-21T09:00:00Z",
            "2026-02-21T09:03:00Z",
            "K1ABC",
            "59",
            "59",
            14.250,
            "20m",
            "SSB",
            None,
            291,
            "United States",
            "NA",
            "OK1MLG",
            "OK1MLG",
            "JO80BB",
            "N",
            "N",
            "K-0001",
            None,
        ),
    ]
    connection.executemany(
        "INSERT INTO contacts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
    )
    connection.executemany(
        "INSERT INTO contacts_autovalue VALUES (?, ?, ?, ?)",
        [
            (1, "JA1AAA", "Y", "2026-03-01"),
            (2, "JA2BBB", "N", None),
            (3, "JA3CCC", None, None),
        ],
    )
    connection.commit()
    connection.close()
    return path


@pytest.fixture
def list_qlog_database(qlog_database):
    connection = sqlite3.connect(qlog_database)
    for column in (
        "vucc_grids",
        "usaca_counties",
        "cnty_alt",
        "credit_granted",
        "award_granted",
    ):
        connection.execute(f'ALTER TABLE contacts ADD COLUMN "{column}" TEXT')

    connection.execute(
        """
        UPDATE contacts SET
            pota_ref = 'K-0001, k-0001 , K-4562 @ US-CA',
            vucc_grids = 'JO70AA, jo70aa, JN89BB, JN89BC',
            usaca_counties = 'MA, Franklin : MA,Hampshire',
            cnty_alt = 'NZ_Regions:Hawkes Bay/Wairoa;broken',
            credit_granted = 'DXCC:LOTW & CARD, legacy-award',
            award_granted = 'POTA, pota'
        WHERE id = 1
        """
    )
    connection.execute(
        """
        UPDATE contacts SET
            pota_ref = 'K-1000;K-2000',
            vucc_grids = 'JN89BD, JN89BE',
            usaca_counties = '',
            cnty_alt = ' ',
            credit_granted = 'UNKNOWN:CARD',
            award_granted = NULL
        WHERE id = 3
        """
    )
    connection.commit()
    connection.close()
    return qlog_database


@pytest.fixture
def analytics_qlog_database(list_qlog_database):
    connection = sqlite3.connect(list_qlog_database)
    rows = [
        (
            5,
            "2026-02-21T09:07:00Z",
            "JA1AAA",
            "20m",
            "CW",
            339,
            "japan",
            "AS",
            "Y",
            "K-0001, K-9999",
        ),
        (6, "2026-03-01T23:59:00Z", "X1", "BC", "M", 1, "A", "EU", "N", None),
        (7, "2026-03-02T00:00:00Z", "X2", "C", "M", 2, "AB", "EU", "N", None),
        (8, "2026-03-02T00:09:59Z", "X3", "bc", "m", 3, "a", "EU", "N", None),
        (9, "2026-03-02T00:10:00Z", "X4", "X", "M", 4, None, "EU", "N", None),
        (10, "2026-03-02T00:59:59Z", "X5", "X", "M", 5, "Z", "EU", "N", None),
        (11, "2026-03-02T01:00:00Z", "X6", "X", "M", 6, "Z", "EU", "N", None),
    ]
    connection.executemany(
        """
        INSERT INTO contacts (
            id, start_time, callsign, band, mode, dxcc, country, cont, qsl_rcvd,
            pota_ref, station_callsign, operator, my_gridsquare
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'OK1MLG', 'OK1MLG', 'JO70AA')
        """,
        rows,
    )
    connection.commit()
    connection.close()
    return list_qlog_database


@pytest.fixture
def catalog_qlog_database(qlog_database):
    connection = sqlite3.connect(qlog_database)
    connection.executescript(
        """
        CREATE TABLE pota_directory (
            reference TEXT PRIMARY KEY,
            name TEXT,
            active INTEGER,
            entityID INTEGER,
            locationDesc TEXT,
            latitude REAL,
            longitude REAL,
            grid TEXT
        );
        INSERT INTO pota_directory VALUES
            ('K-0001', 'Yellowstone', 1, 291, 'US-WY', 44.6, -110.5, 'DN44'),
            ('OK-0001', 'Krkonose', 0, 503, 'CZ', 50.7, 15.7, 'JO70');

        CREATE TABLE sota_summits (
            summit_code TEXT PRIMARY KEY,
            association_name TEXT,
            region_name TEXT,
            summit_name TEXT,
            altm INTEGER,
            altft INTEGER,
            gridref1 REAL,
            gridref2 REAL,
            longitude REAL,
            latitude REAL,
            points INTEGER,
            bonus_points INTEGER,
            valid_from TEXT,
            valid_to TEXT
        );
        INSERT INTO sota_summits VALUES
            ('OK/PA-001', 'Czech Republic', 'Pardubicky', 'Kralicky Sneznik',
             1424, 4672, NULL, NULL, 16.85, 50.21, 10, 3, '01/03/2007', '31/12/2099'),
            ('W1/AM-001', 'United States', 'Massachusetts', 'Mount Greylock',
             1064, 3491, NULL, NULL, -73.17, 42.64, 8, 0, '31/02/2020', '0000-00-00');

        CREATE TABLE wwff_directory (
            reference TEXT PRIMARY KEY,
            status TEXT,
            name TEXT,
            program TEXT,
            dxcc TEXT,
            state TEXT,
            county TEXT,
            continent TEXT,
            iota TEXT,
            iaruLocator TEXT,
            latitude REAL,
            longitude REAL,
            iucncat TEXT,
            valid_from TEXT,
            valid_to TEXT
        );
        INSERT INTO wwff_directory VALUES
            ('KFF-0001', 'active', 'Acadia', 'KFF', '291', 'ME', 'Hancock',
             'NA', NULL, 'FN54', 44.35, -68.21, 'II', '2013-01-01', ''),
            ('OKFF-0001', 'active', 'Sumava', 'OKFF', '503', NULL, NULL,
             'EU', NULL, 'JN69', 49.1, 13.4, 'II', '0000-00-00', '0000-00-00');

        CREATE TABLE iota (iotaid TEXT PRIMARY KEY, islandname TEXT);
        INSERT INTO iota VALUES ('EU-001', 'Dodecanese'), ('NA-026', 'New York State Group');

        CREATE TABLE dxcc_entities_clublog (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            prefix TEXT,
            deleted INTEGER NOT NULL,
            cont TEXT,
            cqz INTEGER,
            ituz INTEGER,
            lat REAL,
            lon REAL,
            start TEXT,
            "end" TEXT
        );
        INSERT INTO dxcc_entities_clublog VALUES
            (291, 'United States', 'K', 0, 'NA', 5, 8, 37.0, -95.0,
             '1945-11-15T00:00:00+00:00', ''),
            (339, 'Japan', 'JA', 0, 'AS', 25, 45, 36.0, 138.0, '', ''),
            (2, 'Abu Ail Is.', '1A', 1, 'AF', 34, 39, 15.0, 42.0, '',
             '1991-03-30T23:59:59+00:00');

        CREATE TABLE dxcc_entities_ad1c (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            prefix TEXT,
            cont TEXT,
            cqz INTEGER,
            ituz INTEGER,
            lat REAL,
            lon REAL,
            tz REAL
        );
        INSERT INTO dxcc_entities_ad1c VALUES
            (291, 'Fallback United States', 'K', 'NA', 5, 8, 37.0, -95.0, 5.0);

        CREATE TABLE sat_info (
            name TEXT PRIMARY KEY,
            number INTEGER,
            uplink TEXT,
            downlink TEXT,
            beacon TEXT,
            mode TEXT,
            callsign TEXT,
            status TEXT
        );
        INSERT INTO sat_info VALUES
            ('AO-91', 43017, '435.250', '145.960', '145.960', 'U/V', 'AMSAT', 'active'),
            ('ISS', 25544, '145.990', '145.800', '145.800', 'V/V', 'RS0ISS', 'active'),
            ('RS-44', 44909, '435.640', '145.935', '145.935', 'U/V', 'RS44', 'active');

        ALTER TABLE contacts ADD COLUMN sat_name TEXT;
        ALTER TABLE contacts ADD COLUMN prop_mode TEXT;
        UPDATE contacts SET sat_name = 'AO-91', prop_mode = 'SAT' WHERE id = 1;
        UPDATE contacts SET sat_name = 'ao-91', prop_mode = 'SAT' WHERE id = 2;
        UPDATE contacts SET sat_name = 'SO-50', prop_mode = 'SAT' WHERE id = 3;
        UPDATE contacts SET sat_name = 'ISS', prop_mode = 'ES' WHERE id = 4;

        CREATE TABLE membership_directory (
            short_desc TEXT PRIMARY KEY,
            long_desc TEXT,
            filename TEXT,
            last_update TEXT,
            num_records INTEGER
        );
        INSERT INTO membership_directory VALUES
            ('BROKEN', 'Broken Date Club', 'broken.csv', '3', 1),
            ('DAY', 'Dated Club', 'day.csv', '12', 1),
            ('OPEN', 'Open Membership Club', 'open.csv', '7', 1),
            ('TIME', 'Timed Club', 'time.csv', '4', 2);

        CREATE TABLE membership (
            callsign TEXT,
            member_id TEXT,
            valid_from TEXT,
            valid_to TEXT,
            clubid TEXT
        );
        INSERT INTO membership VALUES
            ('JA1AAA', '100', '20250101', '20251231', 'TIME'),
            ('JA4DDD', '101', '20250101', '20251231', 'TIME'),
            ('JA2BBB', '200', '', '', 'OPEN'),
            ('JA2BBB', '201', '20260116', '20261231', 'FUTURE'),
            ('JA3CCC', '300', '20260230', '', 'BROKEN'),
            ('JA5EEE', '303', '', '20260230', 'BROKEN'),
            ('JA3CCC', '301', '20260101', '20260220', 'DAY'),
            ('JA3CCC', '302', '20260221', '', 'AFTER');
        """
    )
    connection.commit()
    connection.close()
    return qlog_database


@pytest.fixture
def catalog_match_database(catalog_qlog_database):
    connection = sqlite3.connect(catalog_qlog_database)
    for column, column_type in (
        ("my_dxcc", "INTEGER"),
        ("my_pota_ref", "TEXT"),
        ("sota_ref", "TEXT"),
        ("my_sota_ref", "TEXT"),
        ("wwff_ref", "TEXT"),
        ("my_wwff_ref", "TEXT"),
        ("iota", "TEXT"),
        ("my_iota", "TEXT"),
        ("iota_island_id", "INTEGER"),
        ("my_iota_island_id", "INTEGER"),
    ):
        connection.execute(
            f'ALTER TABLE contacts ADD COLUMN "{column}" {column_type}'
        )

    connection.executescript(
        """
        UPDATE contacts SET
            pota_ref = 'K-0001, OK-0001', my_pota_ref = 'K-0001',
            sota_ref = 'OK/PA-001', my_sota_ref = 'OK/PA-001',
            wwff_ref = 'OKFF-0001', my_wwff_ref = 'OKFF-0001',
            iota = 'EU-001', my_iota = 'EU-001',
            iota_island_id = 101, my_iota_island_id = 201, my_dxcc = 503
        WHERE id = 1;

        UPDATE contacts SET
            my_pota_ref = 'OK-0001', sota_ref = 'W1/AM-001',
            wwff_ref = 'UNKNOWN-1', iota = 'NA-026', iota_island_id = 102,
            my_dxcc = 503
        WHERE id = 2;

        UPDATE contacts SET
            sota_ref = ' ', wwff_ref = 'unknown-1', iota = '', my_dxcc = 503
        WHERE id = 3;

        UPDATE contacts SET pota_ref = 'k-0001', my_dxcc = 503 WHERE id = 4;

        INSERT INTO pota_directory VALUES
            ('K-0002', 'Yellowstone', 1, 291, 'US-MT', 45.0, -110.0, 'DN45');
        """
    )
    connection.commit()
    connection.close()
    return catalog_qlog_database


@pytest.fixture
def reduced_catalog_database(tmp_path):
    path = tmp_path / "reduced-catalog.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE contacts (
            id INTEGER PRIMARY KEY,
            start_time TEXT,
            callsign TEXT,
            pota_ref TEXT,
            dxcc INTEGER,
            sat_name TEXT
        );
        INSERT INTO contacts VALUES (1, '2026-01-01T00:00:00Z', 'K1ABC', 'K-0001', 291, 'AO-91');
        CREATE TABLE pota_directory (reference TEXT PRIMARY KEY, name TEXT);
        INSERT INTO pota_directory VALUES ('K-0001', 'Yellowstone');
        CREATE TABLE sat_info (name TEXT PRIMARY KEY);
        INSERT INTO sat_info VALUES ('AO-91');
        CREATE TABLE dxcc_entities_ad1c (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            prefix TEXT,
            cont TEXT,
            cqz INTEGER,
            ituz INTEGER,
            lat REAL,
            lon REAL
        );
        INSERT INTO dxcc_entities_ad1c VALUES
            (291, 'United States', 'K', 'NA', 5, 8, 37.0, -95.0);
        """
    )
    connection.commit()
    connection.close()
    return path
