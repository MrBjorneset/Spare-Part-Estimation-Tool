import streamlit as st
import pandas as pd
from io import BytesIO
import db
from logic import calculate_spare_parts, add_part, add_machine_part, get_kit_breakdown

# ---------------- PAGE CONFIG ----------------
st.set_page_config(
    page_title="Spare Parts Estimation Tool",
    page_icon="🧰",
    layout="wide"
)

st.title("Spare Parts Estimation Tool")
st.caption("Maintenance & job-based spare part estimation")

# ---------------- DATABASE ----------------
# Create tables (first run only) and seed them from the CSVs if empty.
# After the first run the database is the source of truth; the CSVs are ignored.
@st.cache_resource
def _bootstrap_db():
    db.init_db()
    db.seed_if_empty()
    return True

_bootstrap_db()

# Read the four tables once and keep them in memory. Streamlit re-runs the whole
# script on every click; without caching, each click would re-query the database
# over the network and feel sluggish. The cache is cleared after any write (see
# the Add Part / Link handlers) so changes still show up immediately, and a TTL
# refreshes it periodically so edits from other users appear too.
@st.cache_data(ttl=300, show_spinner=False)
def load_tables_cached():
    return db.load_tables()

machines_df, parts_df, machine_parts_df, kit_components_df = load_tables_cached()

if "machine_counts_store" not in st.session_state:
    st.session_state.machine_counts_store = {}

# ================================================================
# MAIN TABS
# ================================================================
tab_calc, tab_add_part, tab_add_link, tab_edit, tab_machines = st.tabs([
    "🧮 Calculate Spare Parts",
    "➕ Add New Part",
    "🔗 Link Part to Machine",
    "🛠️ Edit / Delete Part",
    "🏭 Machines",
])

# ================================================================
# TAB 1 — CALCULATE  (two columns)
# ================================================================
with tab_calc:

    col_left, col_right = st.columns([1, 2], gap="large")

    # ── LEFT — Machine Configuration ────────────────────────────
    with col_left:
        st.subheader("Machine Configuration")

        machine_types = sorted(machines_df["MachineType"].dropna().unique())

        selected_tech = st.selectbox("Technology", machine_types, key="sel_tech")

        available_models = sorted(
            machines_df.loc[
                machines_df["MachineType"] == selected_tech, "Model"
            ].unique()
        )

        selected_model = st.selectbox("Model", available_models, key="sel_model")

        add_qty = st.number_input("Quantity", min_value=1, step=1, value=1, key="add_qty")

        if st.button("＋ Add machine", width="stretch"):
            counts = st.session_state.machine_counts_store
            counts[selected_model] = counts.get(selected_model, 0) + add_qty
            st.rerun()

        # ── Selected machines list ───────────────────────────────
        counts = st.session_state.machine_counts_store
        active = {m: q for m, q in counts.items() if q > 0}

        st.write("")
        if active:
            rows = []
            for model, qty in active.items():
                tech = machines_df.loc[
                    machines_df["Model"] == model, "MachineType"
                ].values
                rows.append({
                    "Technology": tech[0] if len(tech) else "—",
                    "Model": model,
                    "Quantity": qty,
                })
            display_df = pd.DataFrame(rows).sort_values(["Technology", "Model"])

            h0, h1, h2, h3 = st.columns([2, 2, 1, 1])
            h0.markdown("**Technology**")
            h1.markdown("**Model**")
            h2.markdown("**Qty**")
            h3.markdown("**Del**")
            st.divider()

            for _, row in display_df.iterrows():
                c0, c1, c2, c3 = st.columns([2, 2, 1, 1])
                c0.write(row["Technology"])
                c1.write(row["Model"])
                c2.write(row["Quantity"])
                if c3.button("✕", key=f"remove_{row['Model']}"):
                    del st.session_state.machine_counts_store[row["Model"]]
                    st.rerun()

            st.write("")
            if st.button("🗑️ Clear all", width="stretch"):
                st.session_state.machine_counts_store = {}
                st.rerun()
        else:
            st.info("No machines added yet.")

    # ── RIGHT — Part Catalogue / Results ────────────────────────
    with col_right:
        if st.button("🧮 Estimate spare parts", width="stretch", type="primary"):
            st.session_state.calc_triggered = True
            st.session_state.calc_active    = active.copy()
            st.rerun()

        st.write("")
        if st.session_state.get("calc_triggered") and st.session_state.get("calc_active"):
            st.subheader("Results")

            result_df = calculate_spare_parts(
                machine_parts_df,
                parts_df,
                st.session_state.calc_active,
                kit_components_df,
            )

            if result_df.empty:
                st.warning("No spare parts required for selected configuration.")
            else:
                n_kits = int(result_df["IsKit"].sum())
                msg = f"Found **{len(result_df)}** spare parts for your configuration."
                if n_kits:
                    msg += f"  ·  {n_kits} of them are kits (expand to see contents)."
                st.success(msg)

                # ── Custom result rows: kits get a clickable dropdown ──────
                widths = [2, 3, 2, 1.4, 0.8, 0.9]
                hdr = st.columns(widths)
                for col, label in zip(
                    hdr, ["Part Number", "Description", "Location", "Service", "Qty", "In kit"]
                ):
                    col.markdown(f"**{label}**")
                st.divider()

                for _, row in result_df.iterrows():
                    c = st.columns(widths)
                    c[0].write(str(row["PartNumber"]))
                    c[1].write(row["Description"] if pd.notna(row["Description"]) else "—")
                    loc = row.get("Location")
                    c[2].write(loc if pd.notna(loc) else "—")
                    c[3].write(row["DefaultServiceType"])
                    c[4].write(int(row["TotalQty"]))
                    c[5].write("✓" if row["InKit"] else "")

                    if row["IsKit"]:
                        qty = int(row["TotalQty"])
                        breakdown = get_kit_breakdown(
                            kit_components_df, parts_df, row["PartNumber"], qty
                        )
                        label = (
                            f"📦 Contents of {row['PartNumber']} — "
                            f"{len(breakdown)} components × {qty} kit(s)"
                        )
                        with st.expander(label):
                            if breakdown.empty:
                                st.caption("No components listed for this kit.")
                            else:
                                st.dataframe(
                                    breakdown.rename(columns={
                                        "ComponentPartNumber": "Component",
                                        "Location": "Location",
                                        "QtyPerKit": "Qty / kit",
                                        "TotalInKits": "Total in kits",
                                    }),
                                    width="stretch",
                                    hide_index=True,
                                )
                                st.caption(
                                    "These parts are included inside the kit above — "
                                    "they are not added to the totals separately."
                                )

                st.write("")
                export_df = result_df.drop(columns=["IsKit"])
                buffer = BytesIO()
                export_df.to_excel(buffer, index=False)
                buffer.seek(0)
                st.download_button(
                    label="📤 Export to Excel",
                    data=buffer,
                    file_name="recommended_spareparts.xlsx",
                    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )

            if st.button("← Back to catalogue"):
                st.session_state.calc_triggered = False
                st.rerun()

        else:
            st.subheader("Parts Catalogue")
            st.caption(f"{len(parts_df)} parts in the catalogue")

            search = st.text_input("🔍 Search", placeholder="Filter by part number, description…", key="cat_search")

            filtered = parts_df
            if search:
                mask = (
                    parts_df["PartNumber"].str.contains(search, case=False, na=False) |
                    parts_df["Description"].str.contains(search, case=False, na=False)
                )
                filtered = parts_df[mask]

            st.dataframe(filtered, width="stretch", hide_index=True)

# ================================================================
# TAB 2 — ADD NEW PART
# ================================================================
with tab_add_part:
    st.subheader("Add a New Part to the Catalogue")
    st.caption("Saves a new spare part record to the database.")

    col1, col2 = st.columns(2)
    with col1:
        new_pn   = st.text_input("Part Number *", placeholder="e.g. EPT099001")
        new_desc = st.text_input("Description *", placeholder="e.g. Ink filter 0.2µm")
    with col2:
        new_loc  = st.text_input("Location", placeholder="e.g. A-30-22B")
        new_svc  = st.selectbox("Service Type *", ["Maintenance", "Jobb"])

    if st.button("➕ Add Part", width="stretch"):
        # logic.add_part validates the input (returns an error message if invalid)
        _, msg = add_part(
            parts_df,
            part_number=new_pn,
            description=new_desc,
            location=new_loc,
            service_type=new_svc,
        )
        if msg.startswith("Error"):
            st.error(msg)
        else:
            ok, db_msg = db.insert_part(
                part_number=str(new_pn).strip(),
                description=str(new_desc).strip(),
                location=str(new_loc).strip(),
                service_type=str(new_svc).strip(),
            )
            if ok:
                load_tables_cached.clear()
                st.success(db_msg)
                st.rerun()
            else:
                st.error(db_msg)

    st.divider()
    st.caption(f"**{len(parts_df)} parts** currently in the catalogue.")
    st.dataframe(parts_df, width="stretch", hide_index=True)

# ================================================================
# TAB 3 — LINK PART TO MACHINE
# ================================================================
with tab_add_link:
    st.subheader("Link a Part")
    st.caption("Add a part to a machine, or add a component to a kit. The part must already exist in the catalogue.")

    all_models   = sorted(machines_df["Model"].dropna().unique())
    all_partnums = sorted(parts_df["PartNumber"].dropna().unique())

    link_type = st.radio(
        "Link to", ["Machine", "Kit"], horizontal=True, key="link_type"
    )

    # ============================================================
    # MACHINE LINK
    # ============================================================
    if link_type == "Machine":
        col3, col4, col5 = st.columns([2, 2, 1])
        with col3:
            link_machine = st.selectbox("Machine Model *", all_models, key="link_machine")
        with col4:
            link_part = st.selectbox("Part Number *", all_partnums, key="link_part")
        with col5:
            link_qty = st.number_input("Qty per Machine *", min_value=1, step=1, value=1, key="link_qty")

        selected_part_row = parts_df[parts_df["PartNumber"] == link_part]
        if not selected_part_row.empty:
            st.caption(
                f"**{link_part}** — "
                f"{selected_part_row.iloc[0]['Description']}  |  "
                f"Service type: {selected_part_row.iloc[0]['DefaultServiceType']}"
            )

        if st.button("🔗 Link Part to Machine", width="stretch"):
            _, msg = add_machine_part(
                machine_parts_df, parts_df,
                machine_type=link_machine, part_number=link_part, qty_per_machine=link_qty,
            )
            if msg.startswith("Error"):
                st.error(msg)
            else:
                ok, db_msg = db.upsert_machine_part(link_machine, link_part, link_qty)
                if ok:
                    load_tables_cached.clear()
                    st.success(db_msg)
                    st.rerun()
                else:
                    st.error(db_msg)

        st.divider()
        machine_links = machine_parts_df[machine_parts_df["MachineType"] == link_machine]
        if machine_links.empty:
            st.caption(f"No parts linked to **{link_machine}** yet.")
        else:
            enriched = machine_links.merge(
                parts_df[["PartNumber", "Description", "DefaultServiceType"]],
                on="PartNumber", how="left",
            )
            st.caption(f"**{len(enriched)} parts** currently linked to **{link_machine}**:")
            st.dataframe(enriched, width="stretch", hide_index=True)

    # ============================================================
    # KIT LINK
    # ============================================================
    else:
        st.caption("A kit is itself a part in the catalogue; here you define what it contains.")
        col6, col7, col8 = st.columns([2, 2, 1])
        with col6:
            kit_pn = st.selectbox("Kit (part number) *", all_partnums, key="kit_pn")
        with col7:
            comp_pn = st.selectbox("Component to add *", all_partnums, key="kit_comp")
        with col8:
            kit_qty = st.number_input("Qty per Kit *", min_value=1, step=1, value=1, key="kit_qty")

        kit_row = parts_df[parts_df["PartNumber"] == kit_pn]
        if not kit_row.empty:
            st.caption(f"Kit **{kit_pn}** — {kit_row.iloc[0]['Description']}")

        if st.button("📦 Add Component to Kit", width="stretch"):
            ok, msg = db.upsert_kit_component(kit_pn, comp_pn, kit_qty)
            if ok:
                load_tables_cached.clear()
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        st.divider()
        contents = get_kit_breakdown(kit_components_df, parts_df, kit_pn, 1)
        if contents.empty:
            st.caption(f"**{kit_pn}** has no components yet.")
        else:
            st.caption(f"**{kit_pn}** currently contains {len(contents)} component(s):")
            h0, h1, h2, h3 = st.columns([2, 3, 1, 1])
            h0.markdown("**Component**")
            h1.markdown("**Description**")
            h2.markdown("**Qty/kit**")
            h3.markdown("**Remove**")
            for _, crow in contents.iterrows():
                c0, c1, c2, c3 = st.columns([2, 3, 1, 1])
                c0.write(crow["ComponentPartNumber"])
                c1.write(crow["Description"])
                c2.write(int(crow["QtyPerKit"]))
                if c3.button("✕", key=f"rmkit_{kit_pn}_{crow['ComponentPartNumber']}"):
                    ok, msg = db.delete_kit_component(kit_pn, crow["ComponentPartNumber"])
                    if ok:
                        load_tables_cached.clear()
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

# ================================================================
# TAB 4 — EDIT / DELETE PART
# ================================================================
with tab_edit:
    st.subheader("Edit or Delete a Part")
    st.caption("Fix typos, update details, or remove a part from the database.")

    if parts_df.empty:
        st.info("No parts in the catalogue yet.")
    else:
        all_pns = sorted(parts_df["PartNumber"].dropna().unique())
        sel = st.selectbox("Select a part", all_pns, key="edit_sel")
        row = parts_df[parts_df["PartNumber"] == sel].iloc[0]

        # references — shown so the user understands the impact of changes
        refs = db.get_part_references(sel)
        ref_bits = []
        if refs["machine_links"]:
            ref_bits.append(f"{refs['machine_links']} machine link(s)")
        if refs["in_kits"]:
            ref_bits.append(f"inside {refs['in_kits']} kit(s)")
        if refs["is_kit_rows"]:
            ref_bits.append(f"is a kit with {refs['is_kit_rows']} component(s)")
        st.info("Used: " + (", ".join(ref_bits) if ref_bits else "not referenced anywhere."))

        # ── EDIT ────────────────────────────────────────────────
        st.markdown("##### Edit")
        c1, c2 = st.columns(2)
        with c1:
            e_pn   = st.text_input("Part Number", value=sel, key=f"e_pn_{sel}")
            e_desc = st.text_input("Description", value=row["Description"] if pd.notna(row["Description"]) else "", key=f"e_desc_{sel}")
        with c2:
            e_loc  = st.text_input("Location", value=row["Location"] if pd.notna(row["Location"]) else "", key=f"e_loc_{sel}")
            svc_options = ["Maintenance", "Jobb"]
            cur_svc = row["DefaultServiceType"] if row["DefaultServiceType"] in svc_options else "Maintenance"
            e_svc  = st.selectbox("Service Type", svc_options, index=svc_options.index(cur_svc), key=f"e_svc_{sel}")

        if st.button("💾 Save changes", width="stretch"):
            e_pn_clean = str(e_pn).strip()
            if not e_pn_clean:
                st.error("Error: Part Number cannot be empty.")
            elif not str(e_desc).strip():
                st.error("Error: Description cannot be empty.")
            else:
                ok, msg = True, ""
                # rename first if the part number changed (cascades to references)
                if e_pn_clean != sel:
                    ok, msg = db.rename_part(sel, e_pn_clean)
                if ok:
                    ok2, msg2 = db.update_part(e_pn_clean, str(e_desc).strip(), str(e_loc).strip(), e_svc)
                    if ok2:
                        load_tables_cached.clear()
                        st.success(msg2 if not msg else f"{msg}  {msg2}")
                        st.rerun()
                    else:
                        st.error(msg2)
                else:
                    st.error(msg)

        # ── DELETE ──────────────────────────────────────────────
        st.divider()
        st.markdown("##### Delete")
        referenced = bool(refs["machine_links"] or refs["in_kits"] or refs["is_kit_rows"])
        if referenced:
            st.warning(
                "This part is referenced elsewhere. Deleting with cascade will also "
                "remove its machine links and kit memberships."
            )
        cascade = st.checkbox(
            "Also remove all references (cascade)", key=f"casc_{sel}", disabled=not referenced
        )
        confirm = st.checkbox(f"Yes, permanently delete **{sel}**", key=f"conf_{sel}")
        if st.button("🗑️ Delete part", width="stretch", disabled=not confirm):
            ok, msg = db.delete_part(sel, cascade=cascade)
            if ok:
                load_tables_cached.clear()
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

# ================================================================
# TAB 5 — MACHINES
# ================================================================
with tab_machines:
    st.subheader("Add a New Machine")
    st.caption("Register a new machine model so parts can be linked to it.")

    techs = sorted(machines_df["MachineType"].dropna().unique())
    NEW_TECH = "➕ New technology…"

    c1, c2 = st.columns(2)
    with c1:
        tech_choice = st.selectbox("Technology", techs + [NEW_TECH], key="m_tech_choice")
        if tech_choice == NEW_TECH:
            tech_val = st.text_input("New technology name", placeholder="e.g. TIJ", key="m_new_tech")
        else:
            tech_val = tech_choice
    with c2:
        model_val = st.text_input("Model name *", placeholder="e.g. A720", key="m_model")

    if st.button("➕ Add machine", width="stretch", key="btn_add_machine"):
        ok, msg = db.add_machine(tech_val, model_val)
        if ok:
            load_tables_cached.clear()
            st.success(msg + " You can now link parts to it in the 🔗 tab.")
            st.rerun()
        else:
            st.error(msg)

    st.divider()

    # ── Current machines ────────────────────────────────────────
    if machines_df.empty:
        st.info("No machines registered yet.")
    else:
        st.caption(f"**{len(machines_df)} machines** currently registered:")
        st.dataframe(
            machines_df.sort_values(["MachineType", "Model"]),
            width="stretch", hide_index=True,
        )

        # ── Delete a machine ────────────────────────────────────
        st.markdown("##### Delete a machine")
        del_model = st.selectbox(
            "Machine to delete", sorted(machines_df["Model"].dropna().unique()), key="m_del"
        )
        del_tech = machines_df.loc[machines_df["Model"] == del_model, "MachineType"].iloc[0]
        links = db.get_machine_references(del_model)
        if links:
            st.warning(f"**{del_model}** has {links} part link(s). Cascade will remove those links too.")
        else:
            st.info(f"**{del_model}** has no part links.")

        m_cascade = st.checkbox(
            "Also remove its part links (cascade)", key=f"m_casc_{del_model}", disabled=not links
        )
        m_confirm = st.checkbox(f"Yes, delete machine **{del_model}**", key=f"m_conf_{del_model}")
        if st.button("🗑️ Delete machine", width="stretch", disabled=not m_confirm):
            ok, msg = db.delete_machine(del_tech, del_model, cascade=m_cascade)
            if ok:
                load_tables_cached.clear()
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)