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
    '''CREATE TABLE IF NOT EXISTS stand_components (
        "PartNumber"  TEXT PRIMARY KEY,
        "Category"    TEXT,
        "Height_mm"   DOUBLE PRECISION,
        "Description" TEXT,
        "Notes"       TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS stand_configs (
        "ConfigName" TEXT PRIMARY KEY,
        "Notes"      TEXT
    )''',
    '''CREATE TABLE IF NOT EXISTS stand_config_items (
        "ConfigName" TEXT,
        "PartNumber" TEXT,
        "Category"   TEXT,
        "Qty"        INTEGER,
        PRIMARY KEY ("ConfigName", "PartNumber")
    )''',
]

# Which CSV seeds which table, and the columns to keep (in order).
# Note: stand_configs / stand_config_items are runtime data (saved in the app),
# so they are intentionally NOT seeded from CSV.
_SEED = {
    "machines":         ("machines.csv",         ["MachineType", "Model"]),
    "parts":            ("parts.csv",            ["PartNumber", "Description", "Location", "DefaultServiceType"]),
    "machine_parts":    ("machine_parts.csv",    ["MachineType", "PartNumber", "QtyPerMachine"]),
    "kit_components":   ("kit_components.csv",    ["KitPartNumber", "ComponentPartNumber", "QtyPerKit"]),
    "stand_components": ("stand_components.csv",  ["PartNumber", "Category", "Height_mm", "Description", "Notes"]),
}

# Primary-key columns per table. Rows missing any of these, or duplicating an
# existing key, are dropped before seeding — Postgres (unlike SQLite) rejects
# NULLs and duplicates in a primary key, so we clean them out here.
_PK = {
    "machines":         ["MachineType", "Model"],
    "parts":            ["PartNumber"],
    "machine_parts":    ["MachineType", "PartNumber"],
    "kit_components":   ["KitPartNumber", "ComponentPartNumber"],
    "stand_components": ["PartNumber"],
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
    """Create the tables if they don't exist yet, then apply small migrations."""
    eng = get_engine()
    with eng.begin() as conn:
        for stmt in _DDL:
            conn.execute(text(stmt))
    # Migrations for databases created by earlier versions. Each runs in its own
    # transaction and is ignored if it has already been applied.
    for table, alter in [
        ("stand_components", 'ADD COLUMN "Description" TEXT'),
    ]:
        try:
            with eng.begin() as conn:
                conn.execute(text(f'ALTER TABLE {table} {alter}'))
        except Exception:
            pass  # column already exists


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


def get_part_references(part_number):
    """
    Count where a part is used elsewhere, so the UI can warn before deleting:
      machine_links  : rows in machine_parts pointing at this part
      in_kits        : kits that CONTAIN this part as a component
      is_kit_rows    : component rows belonging to this part when it IS a kit
    """
    eng = get_engine()
    pn = str(part_number).strip()
    with eng.connect() as conn:
        machine_links = conn.execute(
            text('SELECT COUNT(*) FROM machine_parts WHERE "PartNumber" = :pn'), {"pn": pn}
        ).scalar()
        in_kits = conn.execute(
            text('SELECT COUNT(*) FROM kit_components WHERE "ComponentPartNumber" = :pn'), {"pn": pn}
        ).scalar()
        is_kit_rows = conn.execute(
            text('SELECT COUNT(*) FROM kit_components WHERE "KitPartNumber" = :pn'), {"pn": pn}
        ).scalar()
    return {"machine_links": machine_links, "in_kits": in_kits, "is_kit_rows": is_kit_rows}


def update_part(part_number, description, location, service_type):
    """Update the editable fields of a part (everything except its number)."""
    eng = get_engine()
    with eng.begin() as conn:
        res = conn.execute(
            text('''UPDATE parts
                       SET "Description" = :d, "Location" = :l, "DefaultServiceType" = :s
                     WHERE "PartNumber" = :pn'''),
            {"d": description, "l": location, "s": service_type, "pn": str(part_number).strip()},
        )
    if res.rowcount == 0:
        return False, f"Error: part '{part_number}' not found."
    return True, f"Part '{part_number}' updated."


def rename_part(old_pn, new_pn):
    """
    Change a part's number (the primary key) and update every reference to it in
    machine_parts and kit_components, all in one transaction. Use this to fix a
    typo in the part number itself.
    """
    old_pn = str(old_pn).strip()
    new_pn = str(new_pn).strip()
    if not new_pn:
        return False, "Error: new PartNumber cannot be empty."
    if old_pn == new_pn:
        return True, "No change to the part number."

    eng = get_engine()
    try:
        with eng.begin() as conn:
            taken = conn.execute(
                text('SELECT 1 FROM parts WHERE "PartNumber" = :pn'), {"pn": new_pn}
            ).first()
            if taken:
                return False, f"Error: PartNumber '{new_pn}' already exists."
            for stmt in (
                'UPDATE parts          SET "PartNumber"          = :new WHERE "PartNumber"          = :old',
                'UPDATE machine_parts  SET "PartNumber"          = :new WHERE "PartNumber"          = :old',
                'UPDATE kit_components SET "KitPartNumber"       = :new WHERE "KitPartNumber"       = :old',
                'UPDATE kit_components SET "ComponentPartNumber" = :new WHERE "ComponentPartNumber" = :old',
            ):
                conn.execute(text(stmt), {"new": new_pn, "old": old_pn})
        return True, f"Renamed '{old_pn}' → '{new_pn}' (all references updated)."
    except Exception as e:
        return False, f"Error: rename failed ({e.__class__.__name__})."


def delete_part(part_number, cascade=False):
    """
    Delete a part. If it is still referenced and cascade is False, refuse and
    report the references. If cascade is True, also remove its machine links and
    its kit memberships (and, if it is a kit, its component list).
    """
    pn = str(part_number).strip()
    refs = get_part_references(pn)
    referenced = refs["machine_links"] or refs["in_kits"] or refs["is_kit_rows"]
    if referenced and not cascade:
        return False, (
            f"Error: '{pn}' is still referenced — "
            f"{refs['machine_links']} machine link(s), "
            f"in {refs['in_kits']} kit(s), "
            f"{refs['is_kit_rows']} component row(s) as a kit. "
            "Tick the cascade option to remove it everywhere."
        )

    eng = get_engine()
    with eng.begin() as conn:
        if cascade:
            conn.execute(text('DELETE FROM machine_parts  WHERE "PartNumber"          = :pn'), {"pn": pn})
            conn.execute(text('DELETE FROM kit_components WHERE "ComponentPartNumber" = :pn'), {"pn": pn})
            conn.execute(text('DELETE FROM kit_components WHERE "KitPartNumber"       = :pn'), {"pn": pn})
        res = conn.execute(text('DELETE FROM parts WHERE "PartNumber" = :pn'), {"pn": pn})
    if res.rowcount == 0:
        return False, f"Error: part '{pn}' not found."
    return True, f"Part '{pn}' deleted."


def add_machine(machine_type, model):
    """
    Add a new machine: a (Technology, Model) pair. Model must be unique across
    all technologies, because downstream the Model is the key that part links
    and estimates are matched on.
    """
    mt = str(machine_type).strip()
    md = str(model).strip()
    if not mt:
        return False, "Error: Technology cannot be empty."
    if not md:
        return False, "Error: Model cannot be empty."

    eng = get_engine()
    try:
        with eng.begin() as conn:
            clash = conn.execute(
                text('SELECT "MachineType" FROM machines WHERE "Model" = :md'), {"md": md}
            ).first()
            if clash:
                return False, f"Error: model '{md}' already exists (under '{clash[0]}')."
            conn.execute(
                text('INSERT INTO machines ("MachineType","Model") VALUES (:mt, :md)'),
                {"mt": mt, "md": md},
            )
        return True, f"Machine '{md}' added under technology '{mt}'."
    except Exception as e:
        return False, f"Error: could not add machine ({e.__class__.__name__})."


def get_machine_references(model):
    """How many part links point at this machine model (in machine_parts)."""
    eng = get_engine()
    with eng.connect() as conn:
        return conn.execute(
            text('SELECT COUNT(*) FROM machine_parts WHERE "MachineType" = :m'),
            {"m": str(model).strip()},
        ).scalar()


def delete_machine(machine_type, model, cascade=False):
    """
    Delete a machine. If it still has part links and cascade is False, refuse.
    With cascade, also remove its rows from machine_parts.
    """
    mt = str(machine_type).strip()
    md = str(model).strip()
    links = get_machine_references(md)
    if links and not cascade:
        return False, (
            f"Error: '{md}' still has {links} part link(s). "
            "Tick the cascade option to remove them too."
        )

    eng = get_engine()
    with eng.begin() as conn:
        if cascade:
            conn.execute(text('DELETE FROM machine_parts WHERE "MachineType" = :m'), {"m": md})
        res = conn.execute(
            text('DELETE FROM machines WHERE "MachineType" = :mt AND "Model" = :md'),
            {"mt": mt, "md": md},
        )
    if res.rowcount == 0:
        return False, f"Error: machine '{md}' not found."
    return True, f"Machine '{md}' deleted."


def upsert_kit_component(kit_part_number, component_part_number, qty_per_kit):
    """
    Add a component to a kit, or update its quantity if already present.
    Both the kit and the component must exist in the parts catalogue, and a kit
    cannot contain itself.
    """
    kit = str(kit_part_number).strip()
    comp = str(component_part_number).strip()
    if not kit:
        return False, "Error: Kit part number cannot be empty."
    if not comp:
        return False, "Error: Component part number cannot be empty."
    if kit == comp:
        return False, "Error: a kit cannot contain itself."
    try:
        qty = int(qty_per_kit)
    except (ValueError, TypeError):
        return False, "Error: QtyPerKit must be an integer."
    if qty < 1:
        return False, "Error: QtyPerKit must be at least 1."

    eng = get_engine()
    with eng.begin() as conn:
        if not conn.execute(text('SELECT 1 FROM parts WHERE "PartNumber" = :pn'), {"pn": kit}).first():
            return False, f"Error: kit '{kit}' is not in the catalogue — add it as a part first."
        if not conn.execute(text('SELECT 1 FROM parts WHERE "PartNumber" = :pn'), {"pn": comp}).first():
            return False, f"Error: component '{comp}' is not in the catalogue — add it as a part first."
        existed = conn.execute(
            text('SELECT 1 FROM kit_components WHERE "KitPartNumber" = :k AND "ComponentPartNumber" = :c'),
            {"k": kit, "c": comp},
        ).first() is not None
        conn.execute(
            text('''INSERT INTO kit_components ("KitPartNumber","ComponentPartNumber","QtyPerKit")
                    VALUES (:k, :c, :q)
                    ON CONFLICT ("KitPartNumber","ComponentPartNumber")
                    DO UPDATE SET "QtyPerKit" = excluded."QtyPerKit"'''),
            {"k": kit, "c": comp, "q": qty},
        )
    if existed:
        return True, f"Updated '{comp}' in kit '{kit}': QtyPerKit → {qty}."
    return True, f"Added '{comp}' to kit '{kit}' (QtyPerKit={qty})."


def delete_kit_component(kit_part_number, component_part_number):
    """Remove one component from a kit."""
    eng = get_engine()
    with eng.begin() as conn:
        res = conn.execute(
            text('DELETE FROM kit_components WHERE "KitPartNumber" = :k AND "ComponentPartNumber" = :c'),
            {"k": str(kit_part_number).strip(), "c": str(component_part_number).strip()},
        )
    if res.rowcount == 0:
        return False, "Error: that component is not in the kit."
    return True, f"Removed '{component_part_number}' from kit '{kit_part_number}'."


# ============================================================
# STAND BUILDER
# ============================================================
def add_stand_component(part_number, category, height_mm, description="", notes=""):
    """
    Register a stand component (foot / column / pipe). Stand components are
    self-contained — they carry their own description and do not need to exist
    in the spare-parts catalogue.
    """
    pn = str(part_number).strip()
    cat = str(category).strip()
    if not pn:
        return False, "Error: part number required."
    if not cat:
        return False, "Error: category required."
    try:
        h = float(height_mm)
    except (TypeError, ValueError):
        return False, "Error: height must be a number."
    if h < 0:
        return False, "Error: height cannot be negative."

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text('''INSERT INTO stand_components ("PartNumber","Category","Height_mm","Description","Notes")
                    VALUES (:pn, :cat, :h, :d, :n)
                    ON CONFLICT ("PartNumber") DO UPDATE SET
                        "Category"    = excluded."Category",
                        "Height_mm"   = excluded."Height_mm",
                        "Description" = excluded."Description",
                        "Notes"       = excluded."Notes"'''),
            {"pn": pn, "cat": cat, "h": h, "d": str(description).strip(), "n": str(notes).strip()},
        )
    return True, f"'{pn}' set as {cat} (height {h:g} mm)."


def delete_stand_component(part_number):
    """Remove a part from the stand palette (does not touch the catalogue)."""
    eng = get_engine()
    with eng.begin() as conn:
        res = conn.execute(
            text('DELETE FROM stand_components WHERE "PartNumber" = :pn'),
            {"pn": str(part_number).strip()},
        )
    if res.rowcount == 0:
        return False, "Error: component not found."
    return True, f"Removed '{part_number}' from stand components."


def load_stand_tables():
    """Return (components, configs, config_items) as DataFrames."""
    eng = get_engine()
    with eng.connect() as conn:
        comps = pd.read_sql(text('SELECT * FROM stand_components'), conn)
        confs = pd.read_sql(text('SELECT * FROM stand_configs'), conn)
        items = pd.read_sql(text('SELECT * FROM stand_config_items'), conn)
    return comps, confs, items


def save_stand_config(name, items, notes=""):
    """
    Save (or overwrite) a named stand configuration.
    items: list of dicts with keys PartNumber, Category, Qty. Part numbers must
    be unique within the list (merge quantities before calling).
    """
    nm = str(name).strip()
    if not nm:
        return False, "Error: configuration name required."
    if not items:
        return False, "Error: nothing to save — add components first."

    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(
            text('''INSERT INTO stand_configs ("ConfigName","Notes") VALUES (:n, :notes)
                    ON CONFLICT ("ConfigName") DO UPDATE SET "Notes" = excluded."Notes"'''),
            {"n": nm, "notes": str(notes).strip()},
        )
        conn.execute(text('DELETE FROM stand_config_items WHERE "ConfigName" = :n'), {"n": nm})
        for it in items:
            conn.execute(
                text('''INSERT INTO stand_config_items ("ConfigName","PartNumber","Category","Qty")
                        VALUES (:n, :pn, :cat, :q)'''),
                {"n": nm, "pn": str(it["PartNumber"]).strip(),
                 "cat": str(it.get("Category", "")).strip(), "q": int(it["Qty"])},
            )
    return True, f"Saved configuration '{nm}' ({len(items)} item(s))."


def delete_stand_config(name):
    """Delete a saved stand configuration and its items."""
    nm = str(name).strip()
    eng = get_engine()
    with eng.begin() as conn:
        conn.execute(text('DELETE FROM stand_config_items WHERE "ConfigName" = :n'), {"n": nm})
        res = conn.execute(text('DELETE FROM stand_configs WHERE "ConfigName" = :n'), {"n": nm})
    if res.rowcount == 0:
        return False, "Error: configuration not found."
    return True, f"Deleted configuration '{nm}'."