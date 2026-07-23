import streamlit as st
import pandas as pd
import html
import streamlit.components.v1 as components
from io import BytesIO
import db
from logic import calculate_spare_parts, add_part, add_machine_part, get_kit_breakdown, resolve_clamps

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
# SCHEMA_VERSION is part of the cache key: bump it whenever the schema changes so
# that init_db() (and its migrations) re-runs on the next deploy, even if the
# cached resource from a previous version is still around.
SCHEMA_VERSION = 6

@st.cache_resource
def _bootstrap_db(schema_version):
    db.init_db()
    db.seed_if_empty()
    return True

_bootstrap_db(SCHEMA_VERSION)

# Read the four tables once and keep them in memory. Streamlit re-runs the whole
# script on every click; without caching, each click would re-query the database
# over the network and feel sluggish. The cache is cleared after any write (see
# the Add Part / Link handlers) so changes still show up immediately, and a TTL
# refreshes it periodically so edits from other users appear too.
@st.cache_data(ttl=300, show_spinner=False)
def load_tables_cached():
    return db.load_tables()

machines_df, parts_df, machine_parts_df, kit_components_df = load_tables_cached()

@st.cache_data(ttl=300, show_spinner=False)
def load_stand_cached():
    return db.load_stand_tables()

stand_components_df, stand_configs_df, stand_items_df = load_stand_cached()

# Friendly dropdown labels: show "PartNumber — Description" while the selectbox
# still returns just the part number, so no downstream code changes.
_part_desc = dict(zip(parts_df["PartNumber"], parts_df["Description"].fillna("")))

def fmt_part(pn):
    d = _part_desc.get(pn, "")
    return f"{pn} — {d}" if d else str(pn)


def _stand_layout(build, comp_height, comp_length, comp_diameter, comp_width):
    """Compute piece rectangles (in SVG px) for the front view. Returns a dict or None."""
    def num(d, pn):
        try:
            return float(d.get(pn) or 0)
        except (TypeError, ValueError):
            return 0.0

    raw, cur_y = [], 0.0
    for pn, info in build.items():
        qty = int(info.get("Qty", 1))
        orient = info.get("Orientation", "")
        pos = info.get("Pos") or {"dx": 0.0, "dy": 0.0}
        L, D = num(comp_length, pn), num(comp_diameter, pn)
        if L > 0:
            if orient == "Horizontal":
                w, h, kind = L, (D if D > 0 else 20), "hpipe"
                raw.append({"w": w, "h": h, "y0": cur_y, "label": pn, "kind": kind, "qty": qty, "pn": pn, "pos": pos})
            else:
                w, h, kind = (D if D > 0 else 20), L * qty, "vpipe"
                raw.append({"w": w, "h": h, "y0": cur_y, "label": pn, "kind": kind, "qty": qty, "pn": pn, "pos": pos})
                cur_y += h
        else:
            H, W = num(comp_height, pn), num(comp_width, pn)
            w = W if W > 0 else 80
            h = (H if H > 0 else 20) * qty
            raw.append({"w": w, "h": h, "y0": cur_y, "label": pn, "kind": "solid", "qty": qty, "pn": pn, "pos": pos})
            cur_y += h

    if not raw:
        return None

    total_h = cur_y
    max_half = max((p["w"] / 2 for p in raw), default=40)
    model_w = max(2 * max_half, 1)
    model_h = max(total_h, max(p["y0"] + p["h"] for p in raw), 1)

    W_px, H_px = 420, 540
    m_left, m_right, m_top, m_bottom = 70, 30, 30, 50
    inner_w, inner_h = W_px - m_left - m_right, H_px - m_top - m_bottom
    scale = min(inner_w / model_w, inner_h / model_h)
    center_x = m_left + inner_w / 2
    baseline_y = H_px - m_bottom
    colors = {"solid": "#8aa0b6", "vpipe": "#5b8def", "hpipe": "#e0894a"}

    pieces = []
    for p in raw:
        w_px, h_px = p["w"] * scale, p["h"] * scale
        # base auto-stack position, then apply any committed manual offset (stored in mm-equiv)
        x = center_x - w_px / 2 + p["pos"].get("dx", 0.0) * scale
        y = baseline_y - (p["y0"] + p["h"]) * scale + p["pos"].get("dy", 0.0) * scale
        pieces.append({
            "x": x, "y": y, "w": w_px, "h": h_px,
            "fill": colors[p["kind"]],
            "label": p["label"] + (f' ×{p["qty"]}' if p["qty"] > 1 else ''),
            "pn": p["pn"],
        })
    return {"pieces": pieces, "W": W_px, "H": H_px, "total_h": total_h,
            "scale": scale, "baseline_y": baseline_y, "m_left": m_left}


def _dim_and_ground(layout):
    """Shared baseline + total-height dimension SVG snippet."""
    W, m_left, baseline_y = layout["W"], layout["m_left"], layout["baseline_y"]
    top_y = baseline_y - layout["total_h"] * layout["scale"]
    dimx = m_left - 32
    mid_y = (baseline_y + top_y) / 2
    return (
        f'<line x1="{m_left-15}" y1="{baseline_y}" x2="{W-30}" y2="{baseline_y}" stroke="#999" stroke-width="1.5"/>'
        f'<line x1="{dimx}" y1="{baseline_y}" x2="{dimx}" y2="{top_y}" stroke="#555" stroke-width="1"/>'
        f'<line x1="{dimx-4}" y1="{baseline_y}" x2="{dimx+4}" y2="{baseline_y}" stroke="#555"/>'
        f'<line x1="{dimx-4}" y1="{top_y}" x2="{dimx+4}" y2="{top_y}" stroke="#555"/>'
        f'<text x="{dimx-6}" y="{mid_y:.1f}" font-size="11" fill="#333" text-anchor="middle" '
        f'transform="rotate(-90 {dimx-6} {mid_y:.1f})">{layout["total_h"]:g} mm</text>'
    )


def render_static_svg(layout):
    """Static front elevation as an inline SVG string."""
    parts = [f'<svg viewBox="0 0 {layout["W"]} {layout["H"]}" width="100%" '
             f'style="max-width:{layout["W"]}px" xmlns="http://www.w3.org/2000/svg">',
             _dim_and_ground(layout)]
    for p in layout["pieces"]:
        parts.append(f'<rect x="{p["x"]:.1f}" y="{p["y"]:.1f}" width="{p["w"]:.1f}" height="{p["h"]:.1f}" '
                     f'fill="{p["fill"]}" fill-opacity="0.85" stroke="#333" stroke-width="1" rx="2"/>')
        lbl = html.escape(p["label"])
        if p["h"] >= 14 and p["w"] >= 34:
            parts.append(f'<text x="{p["x"]+p["w"]/2:.1f}" y="{p["y"]+p["h"]/2+4:.1f}" font-size="11" '
                         f'text-anchor="middle" fill="#111">{lbl}</text>')
        else:
            parts.append(f'<text x="{p["x"]+p["w"]+6:.1f}" y="{p["y"]+p["h"]/2+4:.1f}" font-size="10" '
                         f'text-anchor="start" fill="#111">{lbl}</text>')
    parts.append('</svg>')
    return "".join(parts)


def render_interactive_html(layout):
    """Self-contained draggable front view (browser-only; positions are not saved)."""
    W, H = layout["W"], layout["H"]
    groups = []
    for p in layout["pieces"]:
        lbl = html.escape(p["label"])
        groups.append(
            f'<g class="piece" transform="translate(0,0)">'
            f'<rect x="{p["x"]:.1f}" y="{p["y"]:.1f}" width="{p["w"]:.1f}" height="{p["h"]:.1f}" rx="2" '
            f'fill="{p["fill"]}" fill-opacity="0.85" stroke="#333"/>'
            f'<text x="{p["x"]+p["w"]/2:.1f}" y="{p["y"]+p["h"]/2+4:.1f}" font-size="11" '
            f'text-anchor="middle" fill="#111" pointer-events="none">{lbl}</text>'
            f'</g>'
        )
    groups_svg = "\n".join(groups)
    ground = _dim_and_ground(layout)
    return f'''
<div style="font-family:sans-serif">
  <button id="reset" style="margin:0 0 6px;padding:5px 12px;cursor:pointer;border:1px solid #ccc;border-radius:6px;background:#f7f7f7">↺ Reset layout</button>
  <svg id="stage" viewBox="0 0 {W} {H}" width="100%" style="max-width:{W}px;border:1px solid #eee;border-radius:8px;touch-action:none;background:#fff">
    {ground}
    {groups_svg}
  </svg>
</div>
<script>
(function(){{
  const svg=document.getElementById('stage');
  let sel=null, offx=0, offy=0, tx=0, ty=0;
  function pt(evt){{
    const r=svg.getBoundingClientRect();
    return {{x:(evt.clientX-r.left)*({W}/r.width), y:(evt.clientY-r.top)*({H}/r.height)}};
  }}
  svg.querySelectorAll('.piece').forEach(function(g){{
    g.style.cursor='grab';
    g.addEventListener('pointerdown',function(e){{
      sel=g; g.setPointerCapture(e.pointerId);
      const m=g.getAttribute('transform').match(/translate\\(([-0-9.]+),([-0-9.]+)\\)/);
      tx=m?parseFloat(m[1]):0; ty=m?parseFloat(m[2]):0;
      const p=pt(e); offx=p.x; offy=p.y; g.style.cursor='grabbing';
      g.parentNode.appendChild(g);
    }});
    g.addEventListener('pointermove',function(e){{
      if(sel!==g) return;
      const p=pt(e);
      g.setAttribute('transform','translate('+(tx+p.x-offx)+','+(ty+p.y-offy)+')');
    }});
    g.addEventListener('pointerup',function(e){{ if(sel===g){{sel=null; g.style.cursor='grab';}} }});
  }});
  document.getElementById('reset').addEventListener('click',function(){{
    svg.querySelectorAll('.piece').forEach(function(g){{g.setAttribute('transform','translate(0,0)');}});
  }});
}})();
</script>
'''

if "machine_counts_store" not in st.session_state:
    st.session_state.machine_counts_store = {}

# ================================================================
# MAIN TABS
# ================================================================
tab_calc, tab_add_part, tab_add_link, tab_edit, tab_machines, tab_stand = st.tabs([
    "🧮 Calculate Spare Parts",
    "➕ Add New Part",
    "🔗 Link Part to Machine",
    "🛠️ Edit / Delete Part",
    "🏭 Machines",
    "🏗️ Stand Builder",
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
            link_part = st.selectbox("Part Number *", all_partnums, format_func=fmt_part, key="link_part")
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

        existing_kits = (
            sorted(kit_components_df["KitPartNumber"].dropna().unique())
            if not kit_components_df.empty else []
        )
        NEW_KIT = "➕ New kit…"

        col6, col7, col8 = st.columns([2, 2, 1])
        with col6:
            kit_choice = st.selectbox(
                "Kit (part number) *", existing_kits + [NEW_KIT], format_func=fmt_part, key="kit_choice"
            )
            if kit_choice == NEW_KIT:
                kit_pn = st.selectbox(
                    "Choose a part to turn into a kit", all_partnums, format_func=fmt_part, key="kit_new_pn"
                )
            else:
                kit_pn = kit_choice
        with col7:
            comp_pn = st.selectbox("Component to add *", all_partnums, format_func=fmt_part, key="kit_comp")
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
            st.caption(
                f"**{kit_pn}** contains {len(contents)} component(s). "
                "Change a Qty/kit and press Save, or ✕ to remove:"
            )
            h0, h1, h2, h3 = st.columns([2, 3, 1, 1])
            h0.markdown("**Component**")
            h1.markdown("**Description**")
            h2.markdown("**Qty/kit**")
            h3.markdown("**Remove**")

            edits = []
            for _, crow in contents.iterrows():
                comp = crow["ComponentPartNumber"]
                cur_qty = int(crow["QtyPerKit"])
                c0, c1, c2, c3 = st.columns([2, 3, 1, 1])
                c0.write(comp)
                c1.write(crow["Description"])
                new_qty = c2.number_input(
                    "qty", min_value=1, step=1, value=cur_qty,
                    key=f"kq_{kit_pn}_{comp}", label_visibility="collapsed",
                )
                edits.append((comp, cur_qty, int(new_qty)))
                if c3.button("✕", key=f"rmkit_{kit_pn}_{comp}"):
                    ok, msg = db.delete_kit_component(kit_pn, comp)
                    if ok:
                        load_tables_cached.clear()
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

            if st.button("💾 Save quantity changes", width="stretch", key=f"savekit_{kit_pn}"):
                changed = 0
                for comp, cur_qty, new_qty in edits:
                    if new_qty != cur_qty:
                        db.upsert_kit_component(kit_pn, comp, new_qty)
                        changed += 1
                load_tables_cached.clear()
                st.success(f"Updated {changed} component(s)." if changed else "No quantity changes to save.")
                st.rerun()

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
        sel = st.selectbox("Select a part", all_pns, format_func=fmt_part, key="edit_sel")
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
# ================================================================
# TAB 6 — STAND BUILDER
# ================================================================
with tab_stand:
    st.subheader("Stand Builder")
    st.caption("Build a printer stand from feet, columns and pipes — get a parts list and total height, and save configurations to reuse.")

    all_partnums = sorted(parts_df["PartNumber"].dropna().unique())

    if "stand_build" not in st.session_state:
        st.session_state.stand_build = {}   # PartNumber -> {"Category":..., "Qty":...}

    # helper lookups (stand components are self-contained: own dims/description)
    comp_cat      = dict(zip(stand_components_df["PartNumber"], stand_components_df["Category"]))    if not stand_components_df.empty else {}
    comp_height   = dict(zip(stand_components_df["PartNumber"], stand_components_df["Height_mm"]))   if not stand_components_df.empty else {}
    comp_length   = dict(zip(stand_components_df["PartNumber"], stand_components_df["Length_mm"]))   if not stand_components_df.empty and "Length_mm"   in stand_components_df.columns else {}
    comp_diameter = dict(zip(stand_components_df["PartNumber"], stand_components_df["Diameter_mm"])) if not stand_components_df.empty and "Diameter_mm" in stand_components_df.columns else {}
    comp_width    = dict(zip(stand_components_df["PartNumber"], stand_components_df["Width_mm"]))    if not stand_components_df.empty and "Width_mm"    in stand_components_df.columns else {}
    _stand_desc   = dict(zip(stand_components_df["PartNumber"], stand_components_df["Description"].fillna(""))) if not stand_components_df.empty else {}

    def is_pipe(pn):
        """A component is treated as a pipe if it has a length (uses length/diameter)."""
        return float(comp_length.get(pn) or 0) > 0

    def fmt_stand(pn):
        d = _stand_desc.get(pn, "")
        return f"{pn} — {d}" if d else str(pn)

    left, right = st.columns([1, 2], gap="large")

    # ── LEFT: choose components ─────────────────────────────────
    with left:
        st.markdown("##### Choose components")
        if stand_components_df.empty:
            st.info("No stand components defined yet. Add some under **Manage stand components** below.")
        else:
            categories = sorted(stand_components_df["Category"].dropna().unique())
            sel_cat = st.selectbox("Category", categories, key="stand_cat")
            parts_in_cat = sorted(
                stand_components_df.loc[stand_components_df["Category"] == sel_cat, "PartNumber"].unique()
            )
            sel_part = st.selectbox("Component", parts_in_cat, format_func=fmt_stand, key="stand_part")

            # Pipes get an orientation choice; only a vertical pipe adds to height.
            if is_pipe(sel_part):
                sel_orient = st.radio(
                    "Orientation", ["Vertical", "Horizontal"], horizontal=True, key="stand_orient",
                    help="Vertical pipes add their length to the total stand height; horizontal ones don't.",
                )
                st.caption(
                    f"Length {float(comp_length.get(sel_part) or 0):g} mm  ·  "
                    f"Ø {float(comp_diameter.get(sel_part) or 0):g} mm"
                )

                # What this pipe clamps onto — drives which clamp is added:
                #   Column / base  → base clamp sized to this pipe's diameter
                #   another pipe   → cross clamp sized to the pair of diameters
                other_pipes = [
                    p for p in st.session_state.stand_build
                    if is_pipe(p) and p != sel_part
                ]
                BASE_LABEL = "Column / base"
                attach_options = [BASE_LABEL] + other_pipes
                sel_attach_label = st.selectbox(
                    "Attaches to", attach_options, key="stand_attach",
                    format_func=lambda p: BASE_LABEL if p == BASE_LABEL else fmt_stand(p),
                    help="A base clamp is added for pipe→column; a cross clamp for pipe→pipe. "
                         "Clamps appear in the parts list automatically.",
                )
                sel_attach = "__BASE__" if sel_attach_label == BASE_LABEL else sel_attach_label
            else:
                sel_orient = ""
                sel_attach = ""

            sel_qty = st.number_input("Quantity", min_value=1, step=1, value=1, key="stand_qty")

            if st.button("＋ Add to stand", width="stretch"):
                b = st.session_state.stand_build
                if sel_part in b:
                    b[sel_part]["Qty"] += sel_qty
                    b[sel_part]["Orientation"] = sel_orient
                    b[sel_part]["AttachesTo"] = sel_attach
                else:
                    b[sel_part] = {"Category": sel_cat, "Qty": sel_qty,
                                   "Orientation": sel_orient, "AttachesTo": sel_attach}
                st.rerun()

            if st.session_state.stand_build and st.button("🗑️ Clear stand", width="stretch"):
                st.session_state.stand_build = {}
                st.rerun()

    # ── RIGHT: current build, BOM, height, save/load ────────────
    with right:
        build = st.session_state.stand_build

        st.markdown("##### Current stand")
        if not build:
            st.info("No components added yet.")
        else:
            rows = []
            total_height = 0.0
            for pn, info in build.items():
                desc = _stand_desc.get(pn, "")
                qty = info["Qty"]
                orient = info.get("Orientation", "")
                if is_pipe(pn):
                    length = float(comp_length.get(pn) or 0)
                    diameter = float(comp_diameter.get(pn) or 0)
                    dims = f"L {length:g} · Ø {diameter:g}"
                    # only vertical pipes contribute to height
                    if orient == "Vertical":
                        total_height += length * qty
                else:
                    h = float(comp_height.get(pn) or 0)
                    dims = f"H {h:g}"
                    total_height += h * qty
                rows.append({
                    "Category": info.get("Category", comp_cat.get(pn, "")),
                    "PartNumber": pn,
                    "Description": desc,
                    "Dimensions_mm": dims,
                    "Orientation": orient or "—",
                    "Qty": qty,
                })
            bom = pd.DataFrame(rows)

            # Clamps are DERIVED from the pipes and their "Attaches to" choice:
            # base clamp for pipe→column, cross clamp for pipe→pipe. They are
            # connectors, so they are deliberately kept out of the height total
            # and the draggable stack — they only appear in the parts list.
            clamp_rows, clamp_warnings = resolve_clamps(
                build, comp_length, comp_diameter, stand_components_df, _stand_desc,
            )

            st.metric("Total height", f"{total_height:g} mm")
            st.caption("Total height counts feet/columns and **vertical** pipes only. "
                       "Clamps are connectors and don't affect height.")

            for w in clamp_warnings:
                st.warning(w)

            layout = _stand_layout(build, comp_height, comp_length, comp_diameter, comp_width)
            if layout:
                with st.expander("📐 Front view", expanded=True):
                    view_mode = st.radio(
                        "View", ["Static", "Interactive (drag)"], horizontal=True, key="stand_view_mode",
                        label_visibility="collapsed",
                    )
                    if view_mode == "Static":
                        st.markdown(f'<div style="text-align:center">{render_static_svg(layout)}</div>',
                                    unsafe_allow_html=True)
                        st.caption("Schematic front elevation, stacked in the order added.")
                    else:
                        components.html(render_interactive_html(layout), height=layout["H"] + 70)
                        st.caption("Drag pieces to rearrange on screen. This is for visual experimenting only — it isn't saved. Use ↺ Reset, or the ▲▼ buttons in the list to change the actual stack.")

            # piece list in stack order (top of stack first), with reorder + remove
            st.caption("Stack (top → bottom). Use ▲▼ to reorder, ✕ to remove:")
            h0, h1, h2, h3, h4, h5, h6 = st.columns([0.5, 0.5, 1.3, 2.1, 1.5, 1.0, 0.6])
            for c, t in zip((h0, h1, h2, h3, h4, h5, h6),
                            ["▲", "▼", "Part", "Description", "Dims (mm)", "Orient.", "Del"]):
                c.markdown(f"**{t}**")

            keys = list(build.keys())
            for disp_i, pn in enumerate(reversed(keys)):     # top of stack first
                idx = len(keys) - 1 - disp_i                  # actual index (0 = bottom)
                r = next(x for x in rows if x["PartNumber"] == pn)
                c0, c1, c2, c3, c4, c5, c6 = st.columns([0.5, 0.5, 1.3, 2.1, 1.5, 1.0, 0.6])
                # ▲ = move up in stack (toward top → higher index)
                if c0.button("▲", key=f"up_{pn}", disabled=(idx == len(keys) - 1)):
                    ks = list(build.keys()); j = idx + 1
                    ks[idx], ks[j] = ks[j], ks[idx]
                    st.session_state.stand_build = {k: build[k] for k in ks}
                    st.rerun()
                if c1.button("▼", key=f"down_{pn}", disabled=(idx == 0)):
                    ks = list(build.keys()); j = idx - 1
                    ks[idx], ks[j] = ks[j], ks[idx]
                    st.session_state.stand_build = {k: build[k] for k in ks}
                    st.rerun()
                c2.write(r["PartNumber"])
                c3.write(r["Description"])
                c4.write(r["Dimensions_mm"])
                c5.write(r["Orientation"])
                if c6.button("✕", key=f"rmstand_{pn}"):
                    del st.session_state.stand_build[pn]
                    st.rerun()

            # ── clamps added automatically ──────────────────────
            clamp_bom = pd.DataFrame(columns=["Category", "PartNumber", "Description",
                                              "Qty", "Dimensions_mm", "Orientation"])
            if clamp_rows:
                st.markdown("##### Clamps (added automatically)")
                st.caption("Derived from each pipe's **Attaches to** choice — "
                           "base clamp for pipe→column, cross clamp for pipe→pipe.")
                clamp_bom = pd.DataFrame([{
                    "Category": f"{c['ClampType']} clamp",
                    "PartNumber": c["PartNumber"],
                    "Description": c["Description"],
                    "Qty": c["Qty"],
                    "Dimensions_mm": "—",
                    "Orientation": "—",
                } for c in clamp_rows])
                st.dataframe(
                    clamp_bom[["Category", "PartNumber", "Description", "Qty"]],
                    hide_index=True, width="stretch",
                )

            # export BOM (pipes/feet/columns + auto clamps)
            export_bom = pd.concat(
                [bom[["Category", "PartNumber", "Description", "Qty", "Dimensions_mm", "Orientation"]],
                 clamp_bom],
                ignore_index=True,
            )
            buf = BytesIO()
            export_bom.to_excel(buf, index=False)
            buf.seek(0)
            st.download_button(
                "📤 Export parts list", data=buf,
                file_name="stand_bom.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )

        # ── save / load ─────────────────────────────────────────
        st.divider()
        st.markdown("##### Save / load configuration")
        cfg_names = sorted(stand_configs_df["ConfigName"].dropna().unique()) if not stand_configs_df.empty else []

        sc1, sc2 = st.columns(2)
        with sc1:
            save_name = st.text_input("Configuration name", key="stand_save_name", placeholder="e.g. T60i tall stand")
            if st.button("💾 Save configuration", width="stretch"):
                items = [{"PartNumber": pn, "Category": i.get("Category", ""), "Qty": i["Qty"],
                          "Orientation": i.get("Orientation", ""),
                          "AttachesTo": i.get("AttachesTo", ""),
                          "PosX_mm": (i.get("Pos") or {}).get("dx"),
                          "PosY_mm": (i.get("Pos") or {}).get("dy")}
                         for pn, i in st.session_state.stand_build.items()]
                ok, msg = db.save_stand_config(save_name, items)
                if ok:
                    load_stand_cached.clear()
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)
        with sc2:
            if cfg_names:
                load_name = st.selectbox("Saved configurations", cfg_names, key="stand_load_name")
                b1, b2 = st.columns(2)
                if b1.button("📂 Load", width="stretch"):
                    items = stand_items_df[stand_items_df["ConfigName"] == load_name]
                    new_build = {}
                    for _, row in items.iterrows():
                        entry = {
                            "Category": row["Category"],
                            "Qty": int(row["Qty"]),
                            "Orientation": (row["Orientation"] if "Orientation" in items.columns
                                            and pd.notna(row["Orientation"]) else ""),
                            "AttachesTo": (row["AttachesTo"] if "AttachesTo" in items.columns
                                           and pd.notna(row["AttachesTo"]) else ""),
                        }
                        if "PosX_mm" in items.columns and pd.notna(row.get("PosX_mm")):
                            entry["Pos"] = {"dx": float(row["PosX_mm"]),
                                            "dy": float(row["PosY_mm"] if pd.notna(row.get("PosY_mm")) else 0)}
                        new_build[row["PartNumber"]] = entry
                    st.session_state.stand_build = new_build
                    st.rerun()
                if b2.button("🗑️ Delete", width="stretch"):
                    ok, msg = db.delete_stand_config(load_name)
                    if ok:
                        load_stand_cached.clear()
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)
            else:
                st.caption("No saved configurations yet.")

    # ── Manage stand components (define the palette) ────────────
    st.divider()
    with st.expander("🧩 Manage stand components (define what can be used)"):
        st.caption(
            "Add stand parts here. These are self-contained — they don't need to be "
            "in the spare-parts catalogue. Use **Height** for feet/columns, and "
            "**Length + Diameter** for pipes."
        )
        mc1, mc2 = st.columns(2)
        with mc1:
            new_comp_pn = st.text_input("Part number *", key="stand_new_pn", placeholder="e.g. STAND-FOOT-01")
            new_comp_desc = st.text_input("Description", key="stand_new_desc", placeholder="e.g. Cast-iron foot 300 mm")
            existing_cats = sorted(stand_components_df["Category"].dropna().unique()) if not stand_components_df.empty else []
            base_cats = sorted(set(existing_cats) | {"Foot", "Column", "Pipe"})
            NEW_CAT = "➕ New category…"
            cat_choice = st.selectbox("Category", base_cats + [NEW_CAT], key="stand_new_cat_choice")
            new_cat = st.text_input("New category name", key="stand_new_cat") if cat_choice == NEW_CAT else cat_choice
        with mc2:
            new_h = st.number_input("Height (mm) — feet/columns", min_value=0.0, step=10.0, value=0.0, key="stand_new_h")
            new_w = st.number_input("Width (mm) — feet/columns footprint", min_value=0.0, step=10.0, value=0.0, key="stand_new_w")
            new_l = st.number_input("Length (mm) — pipes", min_value=0.0, step=10.0, value=0.0, key="stand_new_l")
            new_d = st.number_input("Diameter (mm) — pipes", min_value=0.0, step=1.0, value=0.0, key="stand_new_d")

        if st.button("➕ Add / update component", width="stretch"):
            ok, msg = db.add_stand_component(
                new_comp_pn, new_cat, height_mm=new_h, length_mm=new_l,
                diameter_mm=new_d, width_mm=new_w, description=new_comp_desc,
            )
            if ok:
                load_stand_cached.clear()
                st.success(msg)
                st.rerun()
            else:
                st.error(msg)

        if not stand_components_df.empty:
            st.caption("Defined components:")
            for _, r in stand_components_df.sort_values(["Category", "PartNumber"]).iterrows():
                d0, d1, d2, d3, d4 = st.columns([1.3, 1.5, 2.4, 1.5, 0.7])
                d0.write(r["Category"])
                d1.write(r["PartNumber"])
                d2.write(r["Description"] if pd.notna(r["Description"]) else "")
                length = r["Length_mm"] if "Length_mm" in stand_components_df.columns and pd.notna(r["Length_mm"]) else 0
                if float(length or 0) > 0:
                    diam = r["Diameter_mm"] if "Diameter_mm" in stand_components_df.columns and pd.notna(r["Diameter_mm"]) else 0
                    d3.write(f"L {float(length):g} · Ø {float(diam or 0):g}")
                else:
                    d3.write(f"H {r['Height_mm']:g}" if pd.notna(r["Height_mm"]) else "—")
                if d4.button("✕", key=f"delstandcomp_{r['PartNumber']}"):
                    ok, msg = db.delete_stand_component(r["PartNumber"])
                    if ok:
                        load_stand_cached.clear()
                        st.success(msg)
                        st.rerun()
                    else:
                        st.error(msg)

            # Export the palette so it can be committed back to datasetts/stand_components.csv
            export_cols = [c for c in ["PartNumber", "Category", "Height_mm", "Length_mm",
                                       "Diameter_mm", "Width_mm", "Description", "Notes"]
                           if c in stand_components_df.columns]
            palette_csv = stand_components_df[export_cols]
            st.download_button(
                "⬇️ Export palette as CSV (for datasetts/stand_components.csv)",
                data=palette_csv.to_csv(index=False).encode("utf-8"),
                file_name="stand_components.csv",
                mime="text/csv",
            )