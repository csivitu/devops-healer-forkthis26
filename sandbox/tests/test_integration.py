import os
from datetime import datetime

import psycopg2
import pytest

def get_db_connection():
    # helper fixture to connect to the test database
    try:
        conn = psycopg2.connect(
            host=os.getenv("DB_HOST", "localhost"),
            database=os.getenv("POSTGRES_DB", "app_db"),
            user=os.getenv("POSTGRES_USER", "admin"),
            password=os.getenv("POSTGRES_PASSWORD", "secret")
        )
        return conn
    except psycopg2.OperationalError as e:
        pytest.fail(f"database connection failed during test setup: {e}")

def test_database_connection_established():
    # make sure the app can actually reach the database layer
    conn = get_db_connection()
    assert conn.status == psycopg2.extensions.STATUS_READY
    conn.close()

def test_insert_telemetry_event():
    # verify write operations to the service logs table
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO service_logs (event_name)
        VALUES ('integration_test_ping')
        RETURNING id;
    """)
    inserted_id = cursor.fetchone()[0]
    conn.commit()
    cursor.execute("""
        SELECT COUNT(*) FROM service_logs 
        WHERE created_at >= NOW() - INTERVAL '1 minute';
    """)
    recent_events = cursor.fetchone()[0]
    
    assert recent_events > 0, "No events found in the last minute"
    cursor.close()
    conn.close()