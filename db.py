"""
Database layer for the Spare Parts Estimation Tool.

Why this exists
---------------
The app used to read and write CSV files on disk. That works for one person on
one PC, but breaks the moment the app is hosted for several people:
  * a cloud host's filesystem is temporary — edits to CSVs vanish on restart
  * two people saving at the same time overwrite each other

This module moves the four tables into a real database via SQLAlchemy, so the
SAME code runs against either:
  * a local SQLite file  (default — zero setup, good for development)
  * a hosted Postgres    (set DATABASE_URL — persists, safe for many editors)

Switch between them with one setting (DATABASE_URL); no code changes needed.
"""

import os
import pandas as pd
from sqlalchemy import create_engine, text

# Column names are deliberately quoted everywhere so that Postgres preserves
# the CamelCase casing the rest of the app expects (unquoted identifiers get
# folded to lowercase in Postgres, which would break the calculation code).

_DDL = [
    '''CREATE TABLE IF NOT EXISTS parts (
        "PartNumber"         TEXT PRIMARY KEY,
        "Description"        TEXT,
        "Location"           TEXT,
        "DefaultServiceType" TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS machines (
        "MachineType" TEXT,
        "Model"       TEXT,
        PRIMARY KEY ("MachineType", "Model")
    )''',
    '''CREATE TABLE IF NOT EXISTS machine_parts (
        "MachineType"   TEXT,
        "PartNumber"    TEXT,
        "QtyPerMachine" DOUBLE PRECISION,
        PRIMARY KEY ("MachineType", "PartNumber")
    )''',
    '''CREATE TABLE IF NOT EXISTS kit_components (
        "KitPartNumber"       TEXT,
        "ComponentPartNumber" TEXT,
        "QtyPerKit"           INTEGER,
        PRIMARY KEY ("KitPartNumber", "ComponentPartNumber")
    )''',
]

# Which CSV seeds which table, and the columns to keep (in order).
_SEED = {
    "machines":       ("machines.csv",       ["MachineType", "Model"]),
    "parts":          ("parts.csv",          ["PartNumber", "Description", "Location", "DefaultServiceType"]),
    "machine_parts":  ("machine_parts.csv",  ["MachineType", "PartNumber", "QtyPerMachine"]),
    "kit_components": ("kit_components.csv",  ["KitPartNumber", "ComponentPartNumber", "QtyPerKit"]),
}

# Primary-key columns per table. Rows missing any of these, or duplicating an
# existing key, are dropped before seeding — Postgres (unlike SQLite) rejects
# NULLs and duplicates in a primary key, so we clean them out here.
_PK = {
    "machines":       ["MachineType", "Model"],
    "parts":          ["PartNumber"],
    "machine_parts":  ["MachineType", "PartNumber"],
    "kit_components": ["KitPartNumber", "ComponentPartNumber"],
}

_engine = None

# Seed CSVs live in a "datasetts" folder alongside the code, resolved relative to
# this file so it works regardless of the current working directory.
SEED_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "datasetts")


def _database_url():
    """Resolve the connection string. Priority: Streamlit secret > env var > local SQLite."""
    try:
        import streamlit as st
        if "DATABASE_URL" in st.secrets:
            return str(st.secrets["DATABASE_URL"])
    except Exception:
        pass
    return os.environ.get("DATABASE_URL", "sqlite:///spareparts.db")


def get_engine():
    """Return a cached SQLAlchemy engine (SQLite locally, Postgres in the cloud)."""
    global _engine
    if _engine is None:
        url = _database_url()
        # Heroku/older providers hand out 'postgres://'; SQLAlchemy wants 'postgresql://'
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql://", 1)
        _engine = create_engine(url, pool_pre_ping=True)
    return _engine


def init_db():
    """Create the tables if they don't exist yet."""
    eng = get_engine()
    with eng.begin() as conn:
        for stmt in _DDL:
            conn.execute(text(stmt))


def _table_is_empty(conn, table):
    return conn.execute(text(f'SELECT COUNT(*) FROM {table}')).scalar() == 0


def seed_if_empty(csv_dir=None):
    """
    Load the CSV seed data into any table that is currently empty.

    Runs once on a fresh database; afterwards the database is the source of
    truth and the CSVs are ignored. Whitespace is stripped on the way in.
    Seed files are read from the "datasetts" folder by default.
    """
    if csv_dir is None:
        csv_dir = SEED_DIR
    eng = get_engine()
    with eng.begin() as conn:
        for table, (csv_name, columns) in _SEED.items():
            if not _table_is_empty(conn, table):
                continue
            path = os.path.join(csv_dir, csv_name)
            if not os.path.exists(path):
                continue
            df = pd.read_csv(path)
            df.columns = [str(c).strip() for c in df.columns]
            for c in df.select_dtypes(include="object").columns:
                df[c] = df[c].str.strip()
            df = df[[c for c in columns if c in df.columns]]

            # Drop rows that would violate the primary key in Postgres:
            # any blank/NULL key column, or duplicate key combinations.
            pk = _PK[table]
            for c in pk:
                df = df[df[c].notna() & (df[c].astype(str).str.strip() != "")]
            df = df.drop_duplicates(subset=pk, keep="first")

            df.to_sql(table, conn, if_exists="append", index=False)


def load_tables():
    """Read all four tables back as DataFrames (column names match the CSV era)."""
    eng = get_engine()
    with eng.connect() as conn:
        machines       = pd.read_sql(text('SELECT * FROM machines'), conn)
        parts          = pd.read_sql(text('SELECT * FROM parts'), conn)
        machine_parts  = pd.read_sql(text('SELECT * FROM machine_parts'), conn)
        kit_components = pd.read_sql(text('SELECT * FROM kit_components'), conn)
    return machines, parts, machine_parts, kit_components


def insert_part(part_number, description, location, service_type):
    """Insert one new part. Returns (ok, message). The PK prevents duplicates atomically."""
    eng = get_engine()
    try:
        with eng.begin() as conn:
            conn.execute(
                text('''INSERT INTO parts ("PartNumber","Description","Location","DefaultServiceType")
                        VALUES (:pn, :desc, :loc, :svc)'''),
                {"pn": part_number, "desc": description, "loc": location, "svc": service_type},
            )
        return True, f"Part '{part_number}' added successfully."
    except Exception as e:  # e.g. IntegrityError if two users add the same PN at once
        return False, f"Error: could not add part '{part_number}' ({e.__class__.__name__})."


def upsert_machine_part(machine_type, part_number, qty_per_machine):
    """
    Link a part to a machine, or update the quantity if the link already exists.
    Uses ON CONFLICT, which both SQLite (3.24+) and Postgres (9.5+) support.
    """
    eng = get_engine()
    with eng.begin() as conn:
        existed = conn.execute(
            text('''SELECT 1 FROM machine_parts
                    WHERE "MachineType" = :mt AND "PartNumber" = :pn'''),
            {"mt": machine_type, "pn": part_number},
        ).first() is not None

        conn.execute(
            text('''INSERT INTO machine_parts ("MachineType","PartNumber","QtyPerMachine")
                    VALUES (:mt, :pn, :qty)
                    ON CONFLICT ("MachineType","PartNumber")
                    DO UPDATE SET "QtyPerMachine" = excluded."QtyPerMachine"'''),
            {"mt": machine_type, "pn": part_number, "qty": float(qty_per_machine)},
        )

    if existed:
        return True, f"Updated '{part_number}' on '{machine_type}': QtyPerMachine → {qty_per_machine}."
    return True, f"Part '{part_number}' linked to '{machine_type}' with QtyPerMachine={qty_per_machine}."