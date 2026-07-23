import pandas as pd
import math
import re

VALID_SERVICE_TYPES = {"Maintenance", "Jobb"}


# ============================================================
# STAND CLAMP HELPERS
# ============================================================
def _parse_pair(s):
    """
    Extract a diameter pair like '50x30' / '50X30' / '50×30' from a string
    (a part number or description) and return it as an unordered frozenset so
    that '50x30' and '30x50' match the same junction. '50x50' collapses to a
    single value, which correctly matches a junction of two Ø50 pipes.
    Returns None if no pair is found.
    """
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xX\u00d7]\s*(\d+(?:\.\d+)?)", str(s))
    if not m:
        return None
    return frozenset({float(m.group(1)), float(m.group(2))})


def resolve_clamps(build, comp_length, comp_diameter, stand_components_df, desc_map,
                   base_cat="Clamp", cross_cat="Cross clamp"):
    """
    Work out which clamps a stand needs.

    Every pipe in the build must attach to something (info["AttachesTo"]):
      - "__BASE__"  → the column/base. Needs a BASE clamp sized to the pipe's own
                       diameter (exact match against a base-clamp's Diameter_mm).
      - <other pipe PartNumber> → a pipe-to-pipe junction. Needs a CROSS clamp
                       matching the unordered pair of the two pipe diameters,
                       identified by parsing the clamp's part number/description.

    Clamp quantity scales with the pipe's quantity (one clamp per pipe unit).
    Clamps are connectors: callers should keep them OUT of the height total and
    the draggable stack, and only add the returned rows to the parts list (BOM).

    Parameters
    ----------
    build         : dict  PartNumber -> {"Qty":int, "Orientation":str,
                          "AttachesTo":str, ...}
    comp_length   : dict  PartNumber -> length_mm  (a component is a pipe if > 0)
    comp_diameter : dict  PartNumber -> diameter_mm
    stand_components_df : the stand palette (needs Category, Diameter_mm,
                          PartNumber, Description)
    desc_map      : dict  PartNumber -> description (for BOM rows)

    Returns
    -------
    (rows, warnings)
      rows     : list of {PartNumber, Description, Qty, ClampType, ForPipe}
      warnings : list of human-readable strings for junctions with no clamp
    """
    def _is_pipe(pn):
        return float(comp_length.get(pn) or 0) > 0

    if stand_components_df is None or stand_components_df.empty:
        base = pd.DataFrame(columns=["PartNumber", "Category", "Diameter_mm", "Description"])
        cross = base.copy()
    else:
        base = stand_components_df[stand_components_df["Category"] == base_cat]
        cross = stand_components_df[stand_components_df["Category"] == cross_cat].copy()

    # Pre-parse cross-clamp pairs from part number, falling back to description.
    if not cross.empty:
        cross["_pair"] = cross["PartNumber"].map(_parse_pair)
        need = cross["_pair"].isna()
        if "Description" in cross.columns:
            cross.loc[need, "_pair"] = cross.loc[need, "Description"].map(_parse_pair)

    qty = {}          # clamp PartNumber -> total qty
    meta = {}         # clamp PartNumber -> (ClampType, ForPipe-example)
    warnings = []

    for pn, info in build.items():
        if not _is_pipe(pn):
            continue

        d = float(comp_diameter.get(pn) or 0)
        n = int(info.get("Qty", 1))
        parent = info.get("AttachesTo", "__BASE__")

        if parent in (None, "", "__BASE__"):
            # pipe → column: base clamp sized to this pipe's diameter
            if d <= 0:
                warnings.append(f"Pipe '{pn}' has no diameter set — can't size a base clamp.")
                continue
            hit = base[base["Diameter_mm"] == d]
            label, ctype = f"base clamp Ø{d:g} mm", "Base"
        else:
            # pipe → pipe: cross clamp sized to the pair of diameters
            d2 = float(comp_diameter.get(parent) or 0)
            if d <= 0 or d2 <= 0:
                warnings.append(
                    f"Junction '{pn}' ↔ '{parent}' is missing a diameter — can't size a cross clamp."
                )
                continue
            want = frozenset({d, d2})
            hit = cross[cross["_pair"] == want] if "_pair" in cross.columns else cross.iloc[0:0]
            label, ctype = f"cross clamp {d:g}x{d2:g}", "Cross"

        if hit.empty:
            warnings.append(f"No {label} defined for pipe '{pn}'.")
            continue

        cpn = hit.iloc[0]["PartNumber"]
        qty[cpn] = qty.get(cpn, 0) + n
        meta.setdefault(cpn, (ctype, pn))

    rows = [{
        "PartNumber": cpn,
        "Description": desc_map.get(cpn, ""),
        "Qty": q,
        "ClampType": meta[cpn][0],
        "ForPipe": meta[cpn][1],
    } for cpn, q in qty.items()]

    return rows, warnings


# ============================================================
# KIT HELPERS
# ============================================================
def clean_kit_components(kit_components_df):
    """
    Normalise kit_components.csv: strip whitespace from headers and string
    cells (the file has leading spaces like ' M230i-T4') and coerce QtyPerKit
    to a number. Returns a cleaned copy; the original is untouched.
    """
    df = kit_components_df.copy()
    df.columns = [str(c).strip() for c in df.columns]
    for col in ("KitPartNumber", "Model", "ComponentPartNumber"):
        if col in df.columns:
            df[col] = df[col].astype(str).str.strip()
    if "QtyPerKit" in df.columns:
        df["QtyPerKit"] = pd.to_numeric(df["QtyPerKit"], errors="coerce").fillna(0)
    return df


def get_kit_part_numbers(kit_components_df):
    """Set of PartNumbers that ARE kits (i.e. have components)."""
    if kit_components_df is None or kit_components_df.empty:
        return set()
    return set(clean_kit_components(kit_components_df)["KitPartNumber"].unique())


def get_component_part_numbers(kit_components_df):
    """Set of PartNumbers that appear as a component INSIDE any kit."""
    if kit_components_df is None or kit_components_df.empty:
        return set()
    return set(clean_kit_components(kit_components_df)["ComponentPartNumber"].unique())


def get_kit_breakdown(kit_components_df, parts_df, kit_part_number, kit_qty=1):
    """
    Return the components of one kit, with catalogue descriptions merged in and
    quantities scaled by the number of kits ordered.

    Components are grouped by ComponentPartNumber, so if the same component is
    listed under two model spellings (the file has both 'M230i-T4' and the
    typo 'MT230i-T4'), they collapse into a single row instead of double-counting.

    Returns a DataFrame: ComponentPartNumber, Description, Location, QtyPerKit, TotalInKits
    """
    cols = ["ComponentPartNumber", "Description", "Location", "QtyPerKit", "TotalInKits"]
    if kit_components_df is None or kit_components_df.empty:
        return pd.DataFrame(columns=cols)

    kc = clean_kit_components(kit_components_df)
    comps = kc[kc["KitPartNumber"] == str(kit_part_number).strip()].copy()
    if comps.empty:
        return pd.DataFrame(columns=cols)

    comps = comps.groupby("ComponentPartNumber", as_index=False).agg({"QtyPerKit": "sum"})

    comps = comps.merge(
        parts_df[["PartNumber", "Description", "Location"]],
        left_on="ComponentPartNumber",
        right_on="PartNumber",
        how="left",
    )
    comps["Description"] = comps["Description"].fillna("— not in catalogue —")
    comps["Location"] = comps["Location"].fillna("—").replace("", "—")
    comps["QtyPerKit"] = comps["QtyPerKit"].astype(int)
    comps["TotalInKits"] = (comps["QtyPerKit"] * int(kit_qty)).astype(int)

    return comps[cols].sort_values("ComponentPartNumber").reset_index(drop=True)


def add_part(parts_df, part_number, description, location, service_type):
    """
    Add a new part to the parts catalogue.

    Parameters
    ----------
    parts_df       : pd.DataFrame  — current parts table (PartNumber, Description, Location, DefaultServiceType)
    part_number    : str           — unique part identifier, e.g. "EPT011443"
    description    : str           — human-readable part name
    location       : str           — warehouse location, e.g. "A-30-22B"
    service_type   : str           — "Maintenance" or "Jobb"

    Returns
    -------
    (updated_df, message)
      updated_df : pd.DataFrame with the new row appended (original unchanged if error)
      message    : str describing success or the validation error
    """
    part_number = str(part_number).strip()
    description = str(description).strip()
    location = str(location).strip()
    service_type = str(service_type).strip()

    if not part_number:
        return parts_df, "Error: PartNumber cannot be empty."
    if not description:
        return parts_df, "Error: Description cannot be empty."
    if service_type not in VALID_SERVICE_TYPES:
        return parts_df, (
            f"Error: DefaultServiceType must be one of {sorted(VALID_SERVICE_TYPES)}, "
            f"got '{service_type}'."
        )
    if part_number in parts_df["PartNumber"].values:
        return parts_df, f"Error: PartNumber '{part_number}' already exists in the catalogue."

    new_row = pd.DataFrame([{
        "PartNumber": part_number,
        "Description": description,
        "Location": location,
        "DefaultServiceType": service_type,
    }])

    updated_df = pd.concat([parts_df, new_row], ignore_index=True)
    return updated_df, f"Part '{part_number}' added successfully."


def add_machine_part(machine_parts_df, parts_df, machine_type, part_number, qty_per_machine):
    """
    Link an existing part to a machine type (or update the quantity if already linked).

    Parameters
    ----------
    machine_parts_df : pd.DataFrame — current machine-parts table (MachineType, PartNumber, QtyPerMachine)
    parts_df         : pd.DataFrame — parts catalogue, used to validate that the part exists
    machine_type     : str          — machine model, e.g. "A520"
    part_number      : str          — must already exist in parts_df
    qty_per_machine  : int          — how many of this part are needed per machine (≥ 1)

    Returns
    -------
    (updated_df, message)
      updated_df : pd.DataFrame with the new/updated row (original unchanged if error)
      message    : str describing success or the validation error
    """
    machine_type = str(machine_type).strip()
    part_number = str(part_number).strip()

    if not machine_type:
        return machine_parts_df, "Error: MachineType cannot be empty."
    if not part_number:
        return machine_parts_df, "Error: PartNumber cannot be empty."

    try:
        qty_per_machine = int(qty_per_machine)
    except (ValueError, TypeError):
        return machine_parts_df, "Error: QtyPerMachine must be an integer."

    if qty_per_machine < 1:
        return machine_parts_df, "Error: QtyPerMachine must be at least 1."

    if part_number not in parts_df["PartNumber"].values:
        return machine_parts_df, (
            f"Error: PartNumber '{part_number}' not found in parts catalogue. "
            "Add the part first using add_part()."
        )

    # Check if link already exists
    existing_mask = (
        (machine_parts_df["MachineType"] == machine_type) &
        (machine_parts_df["PartNumber"] == part_number)
    )

    if existing_mask.any():
        updated_df = machine_parts_df.copy()
        updated_df.loc[existing_mask, "QtyPerMachine"] = qty_per_machine
        return updated_df, (
            f"Updated '{part_number}' on '{machine_type}': QtyPerMachine → {qty_per_machine}."
        )

    new_row = pd.DataFrame([{
        "MachineType": machine_type,
        "PartNumber": part_number,
        "QtyPerMachine": qty_per_machine,
    }])

    updated_df = pd.concat([machine_parts_df, new_row], ignore_index=True)
    return updated_df, (
        f"Part '{part_number}' linked to machine '{machine_type}' "
        f"with QtyPerMachine={qty_per_machine}."
    )


def calculate_spare_parts(machine_parts_df, parts_df, machine_counts, kit_components_df=None):

    # Convert machine selection to DataFrame
    config_df = pd.DataFrame(
        machine_counts.items(),
        columns=["MachineType", "MachineCount"]
    )

    config_df = config_df[config_df["MachineCount"] > 0]

    if config_df.empty:
        return pd.DataFrame()

    # Merge selected machines
    merged = machine_parts_df.merge(
        config_df,
        on="MachineType",
        how="inner"
    )

    # Remove empty part numbers
    merged = merged[
        merged["PartNumber"].notna() &
        (merged["PartNumber"] != "")
    ]

    # Merge with part metadata
    merged = merged.merge(
        parts_df,
        on="PartNumber",
        how="left"
    )

    # Calculate raw quantity
    merged["TotalQty"] = (
        merged["QtyPerMachine"] * merged["MachineCount"]
    )

    # ===============================
    # MAINTENANCE → sum + round up
    # ===============================
    maintenance = merged[
        merged["DefaultServiceType"] == "Maintenance"
    ]

    maintenance = (
        maintenance
        .groupby("PartNumber", as_index=False)
        .agg({
            "TotalQty": "sum",
            "Description": "first",
            "Location": "first",
            "DefaultServiceType": "first"
        })
    )

    # ROUND UP AFTER SUM
    maintenance["TotalQty"] = maintenance["TotalQty"].apply(math.ceil)

    # ===============================
    # JOBB → only ONE per PartNumber
    # ===============================
    jobb = merged[
        merged["DefaultServiceType"] == "Jobb"
    ]

    jobb = (
        jobb
        .drop_duplicates(subset=["PartNumber"])
        [["PartNumber", "Description", "DefaultServiceType"]]
    )

    jobb["TotalQty"] = 1  # ALWAYS 1

    # ===============================
    # COMBINE
    # ===============================
    result_df = pd.concat([maintenance, jobb], ignore_index=True)

    result_df = result_df.sort_values("PartNumber")

    # ===============================
    # KIT FLAGS
    #   IsKit : this PartNumber is itself a kit (expandable in the UI)
    #   InKit : this PartNumber is also available as a component inside a kit
    # ===============================
    kit_set = get_kit_part_numbers(kit_components_df)
    component_set = get_component_part_numbers(kit_components_df)
    result_df["IsKit"] = result_df["PartNumber"].isin(kit_set)
    result_df["InKit"] = result_df["PartNumber"].isin(component_set)

    return result_df