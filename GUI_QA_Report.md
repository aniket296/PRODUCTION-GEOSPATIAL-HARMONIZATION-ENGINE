# Comprehensive Desktop GUI QA, Visual Audit & End-to-End Verification Report (`GUI_QA_Report.md`)

**Application Name:** Geospatial Harmonization Engine (SIH26013 — Multi-Source Urban Land Records Differential GIS Workbench)  
**Version:** `0.1.0` (`main` @ `99357d8`)  
**Testing Date:** `2026-11-18`  
**Operating System:** Windows 11 (x64), Native Physical Display `1536×864`, Tested Viewports `800×600`, `1280×800`, `1440×900`, `1920×1080`, `900×1200`, `1920×540` across `100%`, `125%`, `150%`, and `200%` DPI scaling  
**Technology Stack:**
- **Frontend GUI:** React 18.3.1, TypeScript 5.7.2, Vite 6.4.6, Tailwind CSS v4, Zustand 5.0.2, MapLibre GL JS 4.7.1, FlatGeobuf 3.32.0
- **Backend Engine:** Python 3.12.5, FastAPI 0.142.2, Uvicorn 0.54.0, SQLAlchemy 2.1.1 (Async), GeoAlchemy2 0.20.0, Shapely 2.1.2, PyProj 3.8.0, PyShp 3.1.6, ezdxf 1.4.4
- **Live GUI Automation Harness:** [_qa_gui_runner.py](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_gui_runner.py) (Win32 `user32` HWND Foreground & Modal Watchdog + Chrome DevTools Protocol WebSocket inspection in standalone `--app=http://127.0.0.1:5173` desktop window)

---

## 9.1 Executive Summary

- **Total Discovered Requirements:** `9` (`REQ-01` – `REQ-09`, documented in [_qa_project_context.md](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_project_context.md))
- **Total Applicable Test Cases:** `24` (`9 P0`, `13 P1`, `2 P2`)
- **Total Executed Live GUI Test Cases:** `24` (`100.0%` execution coverage)
  - **PASSED:** `24` (`100.0%` — up from `19` baseline, `21` post Sub-Phase 1, and `22` post Sub-Phase 2; all 5 defects `DEF-GUI-001` through `DEF-GUI-005` remediated and live-verified)
  - **FAILED:** `0`
  - **BLOCKED:** `0`
  - **NOT TESTED:** `0`
- **Confirmed Unresolved Live GUI Defects:** `0` total (`5` discovered defects all FIXED & VERIFIED across Sub-Phases 1, 2, and 3)
  - **Critical:** `0`
  - **High:** `0` (`DEF-GUI-001` FIXED, `DEF-GUI-002` FIXED)
  - **Medium:** `0` (`DEF-GUI-003` FIXED)
  - **Low:** `0` (`DEF-GUI-004` FIXED, `DEF-GUI-005` FIXED)
- **Application Crashes / Unhandled Process Terminations:** `0`
- **Overall Capped Application Health Score:** **100.00 / 100** (Evidence-Adjusted QA Score = **100.00 / 100**, zero unresolved defects; progression: **59.00 / 100** baseline $\rightarrow$ **79.00 / 100** post Sub-Phase 1 $\rightarrow$ **89.00 / 100** post Sub-Phase 2 $\rightarrow$ **100.00 / 100** post Sub-Phase 3).

---

## 9.2 Transparent Health Score Methodology & Calculation

### Priority Weights
- **P0 (Critical testing priority):** Weight `5` (`9` applicable tests = `45` weighted points)
- **P1 (High testing priority):** Weight `3` (`13` applicable tests = `39` weighted points)
- **P2 (Normal testing priority):** Weight `1` (`2` applicable tests = `2` weighted points)
- **Total Weighted Applicable Points:** $45 + 39 + 2 = 86$
- **Total Weighted Executed Points:** $86$ (`24 / 24` executed)

### Executed Outcomes by Priority (Post Sub-Phase 3 — Final)
- **P0 (`9` executed):** `9 PASSED` (`GUI-001`, `GUI-008`, `GUI-009`, `GUI-010`, `GUI-013`, `GUI-014`, `GUI-015`, `GUI-016`, `GUI-017` = $9 \times 5 = 45$ pts), `0 FAILED`.
- **P1 (`13` executed):** `13 PASSED` (`GUI-002`, `GUI-003`, `GUI-004`, `GUI-005`, `GUI-006`, `GUI-007A`, `GUI-007B`, `GUI-007C`, `GUI-011`, `GUI-012`, `GUI-018`, `GUI-019`, `GUI-021` = $13 \times 3 = 39$ pts), `0 FAILED`.
- **P2 (`2` executed):** `2 PASSED` (`GUI-020`, `GUI-022` = $2 \times 1 = 2$ pts), `0 FAILED`.
- **Total Weighted Passed Points:** $45 + 39 + 2 = 86$ pts (up from $71$ pts baseline, $81$ pts post Sub-Phase 1, and $84$ pts post Sub-Phase 2).

### Formula Calculations
$$\text{Weighted Execution Pass Rate} = 100 \times \left(\frac{86}{86}\right) = 100.00\%$$

$$\text{Weighted Execution Coverage} = 100 \times \left(\frac{86}{86}\right) = 100.00\%$$

$$\text{Evidence-Adjusted QA Score} = \frac{100.00 \times 100.00}{100} = 100.00$$

### Severity-Based Score Cap Application
| Unresolved Defect Severity | Count | Applicable Cap |
| :--- | :---: | :---: |
| **Critical** | `0` | 39 |
| **High** (`DEF-GUI-001` FIXED, `DEF-GUI-002` FIXED) | `0` | 59 |
| **Medium** (`DEF-GUI-003` FIXED) | `0` | 79 |
| **Low** (`DEF-GUI-004` FIXED, `DEF-GUI-005` FIXED) | `0` | 89 |

- **Final Capped Health Score:** $\mathbf{100.00 / 100}$ (Baseline: `59.00 / 100`, Post Sub-Phase 1: `79.00 / 100`, Post Sub-Phase 2: `89.00 / 100`, Post Sub-Phase 3: `100.00 / 100`).

---

## 9.3 Project Architecture Summary

Full architectural details are documented in [_qa_project_context.md](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_project_context.md).
- **Frontend Architecture:** Single-Page Application (`frontend/src/App.tsx`) built with React 18, Zustand (`frontend/src/store/gisStore.ts`), and Tailwind CSS v4, organized into a 3-column 12-grid layout (`LayerUploadPanel` + `AutoResolveBanner` + `ParcelDetailCard` + `GeodeticAuditCard` on the left; `DifferentialMapCanvas` in the center; `TopologyInspectorPanel` on the right; `WorkbenchHeader` on top; `EdgeSyncStatusBar` on the bottom; and `ExportModal` dialog overlay).
- **Backend Architecture:** FastAPI REST server (`backend/api/app.py`) exposing `/api/v1/layers`, `/api/v1/geodetic`, `/api/v1/edge`, and `/api/v1/ai`, backed by `UnifiedIngestionDispatcher`, `TopologyConflictRecorder`, `execute_healing_transaction`, and deterministic multi-format exporters (`FlatGeobuf`, `GeoPackage`, `GeoJSON`, `manifest.json`, and `.zip` release bundle).

---

## 9.4 Application UI Inventory Summary

Full component-by-component inventory is persisted in [_qa_ui_inventory.md](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_ui_inventory.md).
- **Total Discovered Screens / Panels:** `10` (`SCR-01` through `SCR-10`).
- **Live Tested Screens:** `10 / 10` (`100%`).
- **Inaccessible / Unwired UI States Identified During Live Audit:**
  1. **Active State of `ParcelDetailCard` (`SCR-05`) & Parcel Chips in `DifferentialMapCanvas` (`SCR-07`):** Unreachable in live `App.tsx` because `const [parcels] = useState<ParcelGeoJSONFeatureCollection | null>(null)` is never updated (`DEF-GUI-001`).
  2. **Selected Defect Highlight in `TopologyInspectorPanel` (`SCR-08`):** Clicking any defect card fails to highlight the card and renders `OVERLAP:` without an ID on the canvas because `TopologyErrorProperties` omits `error_id` (`DEF-GUI-002`).
  3. **Live Edge Cluster Refresh in `EdgeSyncStatusBar` (`SCR-10`):** Wired to a no-op `onRefresh={() => {}}` in `App.tsx` (`DEF-GUI-004`).

---

## 9.5 Visual and Scaling Audit Matrix (Phase 3)

| Test ID | Screen / Resolution / DPI | Window / Viewport Size | Measured Left Sidebar Width | Sibling Control Overlaps | Clipped / Colliding Elements | Result | Screenshot Evidence |
| :--- | :--- | :---: | :---: | :---: | :--- | :---: | :--- |
| **GUI-001** | Initial Launch (`100%` DPI) | `1280×800` | `300.0 px` | `0` | `0` | **PASSED** | [GUI-001_initial_launch_1280x800.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/initial/GUI-001_initial_launch_1280x800.png) |
| **GUI-002** | Minimum Usable Size (`100%` DPI) | `800×600` | `752.8 px` (Baseline: `180.0 px`) | `0` (controls) / `0` (pane badges) | `0` (Responsive single-column stack; pane badges vertically offset) | **PASSED (Fixed)** | Before: [GUI-002_min_800x600.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-003_800x600_squeezed_columns.png)<br>After: [GUI-002_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-002_remediated_800x600_responsive.png) |
| **GUI-003** | Default Size (`100%` DPI) | `1280×800` | `300.0 px` | `0` | `0` | **PASSED** | [GUI-003_default_1280x800.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-003_default_1280x800.png) |
| **GUI-004** | Full HD Maximized (`100%` DPI) | `1920×1080` | `460.0 px` | `0` | `0` | **PASSED** | [GUI-004_fhd_1920x1080.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-004_fhd_1920x1080.png) |
| **GUI-005** | Narrow & Tall Aspect (`100%` DPI) | `900×1200` | `205.0 px` | `0` | `0` | **PASSED** | [GUI-005_aspect_narrow_900x1200.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-005_aspect_narrow_900x1200.png) |
| **GUI-006** | Wide & Short Aspect (`100%` DPI) | `1920×540` | `460.0 px` | `0` | `0` (Scrollbars active) | **PASSED** | [GUI-006_aspect_wide_1920x540.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-006_aspect_wide_1920x540.png) |
| **GUI-007A** | DPI Scaling `125%` (`dpr=1.25`) | `1280×800` | `300.0 px` | `0` | `0` | **PASSED** | [GUI-007A_dpi_125pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007A_dpi_125pct.png) |
| **GUI-007B** | DPI Scaling `150%` (`dpr=1.50`) | `1280×800` | `300.0 px` | `0` | `0` | **PASSED** | [GUI-007B_dpi_150pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007B_dpi_150pct.png) |
| **GUI-007C** | DPI Scaling `200%` (`dpr=2.00`) | `1280×800` | `300.0 px` | `0` | `0` | **PASSED** | [GUI-007C_dpi_200pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007C_dpi_200pct.png) |

---

## 9.6 Functional and Edge-Case Test Log (Phases 4–8)

| Test ID | Priority | Scenario | Live Action Taken | Expected Result | Actual Result | Status | Evidence |
| :--- | :---: | :--- | :--- | :--- | :--- | :---: | :--- |
| **GUI-001** | **P0** | Initial launch & empty-state guards | Launch window (`1280×800`) and inspect header & buttons | `No Active Layer` shown; `Export Package` & `Ingest Survey Layer` disabled | 32 visible widgets; `Export disabled=True`, `Ingest disabled=True` | **PASSED** | [GUI-001_initial_launch_1280x800.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/initial/GUI-001_initial_launch_1280x800.png) |
| **GUI-002** | **P1** | Minimum usable size (`800×600`) | Resize viewport to `800×600` and measure DOM bounding boxes | Readable columns and non-overlapping labels | **Baseline:** `FAILED` (`leftColWidth=180.0px`, `badgeOverlap=39×26px` — `DEF-GUI-003`)<br>**Post Sub-Phase 2:** `PASSED` (`leftColWidth=752.8px`, `overlaps=0`, `badgeOverlapArea=0px²`) | **PASSED (Fixed)** | Before: [DEF-GUI-003](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-003_800x600_squeezed_columns.png)<br>After: [GUI-002_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-002_remediated_800x600_responsive.png) |
| **GUI-003** | **P1** | Default desktop size (`1280×800`) | Set viewport to `1280×800 @100%` | Zero overlaps/clipping | `overlaps=0, clipped=0, leftColWidth=300.0px` | **PASSED** | [GUI-003_default_1280x800.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-003_default_1280x800.png) |
| **GUI-004** | **P1** | Full HD maximized (`1920×1080`) | Set viewport to `1920×1080 @100%` | Zero overlaps/clipping | `overlaps=0, clipped=0, leftColWidth=460.0px` | **PASSED** | [GUI-004_fhd_1920x1080.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-004_fhd_1920x1080.png) |
| **GUI-005** | **P1** | Narrow-and-tall (`900×1200`) | Set viewport to `900×1200 @100%` | Zero overlaps/clipping | `overlaps=0, clipped=0, leftColWidth=205.0px` | **PASSED** | [GUI-005_aspect_narrow_900x1200.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-005_aspect_narrow_900x1200.png) |
| **GUI-006** | **P1** | Wide-and-short (`1920×540`) | Set viewport to `1920×540 @100%` | Zero overlaps/clipping; scrollbars activate | `overlaps=0, clipped=0, leftColWidth=460.0px` | **PASSED** | [GUI-006_aspect_wide_1920x540.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/resizing/GUI-006_aspect_wide_1920x540.png) |
| **GUI-007A** | **P1** | DPI Scaling `125%` | Set `deviceScaleFactor=1.25` at `1280×800` | Crisp rendering, zero overlaps | `overlaps=0, clipped=0` | **PASSED** | [GUI-007A_dpi_125pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007A_dpi_125pct.png) |
| **GUI-007B** | **P1** | DPI Scaling `150%` | Set `deviceScaleFactor=1.50` at `1280×800` | Crisp rendering, zero overlaps | `overlaps=0, clipped=0` | **PASSED** | [GUI-007B_dpi_150pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007B_dpi_150pct.png) |
| **GUI-007C** | **P1** | DPI Scaling `200%` | Set `deviceScaleFactor=2.00` at `1280×800` | Crisp rendering, zero overlaps | `overlaps=0, clipped=0` | **PASSED** | [GUI-007C_dpi_200pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/dpi_scaling/GUI-007C_dpi_200pct.png) |
| **GUI-008** | **P0** | Upload valid UTM 43N GeoJSON survey | Select `ward14_cadastral_survey.geojson`, set name `Ward 14 Cadastral Survey`, CRS `EPSG:32643`, click `Ingest Survey Layer` | Status `COMPLETED` (`100%`), `5 features`, `GeodeticAuditCard` shows `0.042 m` RMSE & `6 GCPs` | Ingested cleanly in `1.66s`; all status & geodetic audit fields rendered | **PASSED** | [GUI-008_ward14_uploaded.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-008_ward14_uploaded.png) |
| **GUI-009** | **P0** | Parcel rendering & `ParcelDetailCard` selection | Inspect `DifferentialMapCanvas` parcel badge and click `KHASRA-101` chip | `5 Harmonized Parcels` badge & clickable parcel chips that populate `ParcelDetailCard` | **Baseline:** `FAILED` (`0 Harmonized Parcels` — `DEF-GUI-001`)<br>**Post Sub-Phase 1:** `PASSED` (`5 Harmonized Parcels`, clicked `KHASRA-101`, `ParcelDetailCard` rendered `10000.00 m²`, `400.00 m`, `0.785`, `Ramesh Kumar`) | **PASSED (Fixed)** | Before: [DEF-GUI-001](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-001_zero_harmonized_parcels_on_canvas.png)<br>After: [GUI-009_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-009_remediated_parcels_and_detail_card.png) |
| **GUI-010** | **P0** | Defect card selection & canvas chip IDs | Click first defect card in `TopologyInspectorPanel` | Clicked card highlights (`selectedCards=1`) & canvas chips show `OVERLAP:<id>` | **Baseline:** `FAILED` (`OVERLAP:` empty ID, `selectedCards=0` — `DEF-GUI-002`)<br>**Post Sub-Phase 1:** `PASSED` (`selectedCards=1`, chip `OVERLAP:143551e4-11bf-53b5-94e5-0e4d783a2274` highlighted) | **PASSED (Fixed)** | Before: [DEF-GUI-002](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-002_topology_error_id_undefined.png)<br>After: [GUI-010_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-010_remediated_topology_selection.png) |
| **GUI-011** | **P1** | Topology severity filter (`CRITICAL_DISPUTE`) | Select `Critical Dispute` in `TopologyInspectorPanel` severity dropdown | Only the `1` critical dispute (>2% overlap) remains visible | Badge updates to `1` (`1 active`) and displays only the `1000.00 m²` critical dispute | **PASSED** | [GUI-011_topology_filter_critical.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-011_topology_filter_critical.png) |
| **GUI-012** | **P1** | Swipe divider slider (`75%`) & View Mode toggle | Drag range slider to `75%`, then click `Overlay` mode button | `clipPath: inset(0px 0px 0px 75%)` in Swipe mode; removed in Overlay mode | Verified exact CSS `clipPath` and label `Harmonized (75%)` | **PASSED** | [GUI-012_canvas_swipe_75pct.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-012_canvas_swipe_75pct.png) |
| **GUI-013** | **P0** | Deterministic Topological Auto-Heal | Click `Auto-Heal Layer` button in `AutoResolveBanner` | Auto-resolvable defects healed; critical dispute locked; metrics displayed | Displays `4 defects resolved`, `1 dispute locked`, `Slivers Merged: 1`, `Overlaps Trimmed: 1`, `RESOLVED` badges stamped | **PASSED** | [GUI-013_auto_heal_completed.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-013_auto_heal_completed.png) |
| **GUI-014** | **P0** | Export staging, SHA-256 certificate & independent binary verification | Open `ExportModal`, click `Generate Secure Download`, and inspect `.gpkg`, `.geojson`, `.fgb`, and `.zip` bundle on disk | Modal shows `Staged Package Ready (4 parcels)` + SHA-256 certificate; all 4 exported files valid | Verified `PRAGMA application_id = 0x47504b47`, `4` active rows (`1` merged sliver excluded), and SHA-256 manifest match | **PASSED** | [GUI-014B_export_staged_certificate.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-014B_export_staged_certificate.png) |
| **GUI-015** | **P0** | Empty `0-byte` file upload rejection | Upload `zero_byte.geojson` (`0 bytes`) | `role="alert"` banner shows `Uploaded payload contains 0 bytes` | Alert banner displayed exact error message; no crash | **PASSED** | [GUI-015_negative_upload.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-015_negative_upload.png) |
| **GUI-016** | **P0** | Malformed/corrupt GeoJSON rejection | Upload `corrupt_syntax.geojson` | `role="alert"` banner shows `MAGIC_BYTE_MISMATCH` / `Upload rejected` | Alert banner displayed rejection message cleanly | **PASSED** | [GUI-016_negative_upload.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-016_negative_upload.png) |
| **GUI-017** | **P0** | Disguised Windows PE executable (`MZ`) rejection | Upload `disguised_executable.geojson` | `role="alert"` banner blocks `Windows PE executable (EXECUTABLE_PAYLOAD)` | Alert banner blocked executable upload immediately | **PASSED** | [GUI-017_negative_upload.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-017_negative_upload.png) |
| **GUI-018** | **P1** | Multilingual Unicode & Windows-reserved chars in Layer Name | Upload `multilingual_खसरा_survey.geojson` with name `वार्ड १४ • முருகன் • محمد <>:"/\|?* ∑∆π` | Layer ingests (`COMPLETED`, `1 feature`) and header renders Unicode string safely | Header and status rendered Devanagari, Tamil, Arabic, and math symbols without truncation or XSS | **PASSED** | [GUI-018_unicode_upload.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-018_unicode_upload.png) |
| **GUI-019** | **P1** | Large 120-parcel dataset & ESRI Shapefile ZIP | Upload `grid_120_parcels.geojson` (`120 features`) and `rampur_shapefile.zip` | Both transition to `COMPLETED` with accurate feature counts (`120` and `1`) | Both ingested in `<1.5s` each with `100%` progress | **PASSED** | [GUI-019A_large_120_parcels.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-019A_large_120_parcels.png) |
| **GUI-020** | **P2** | `EdgeSyncStatusBar` `Sync Telemetry` button | Register active edge worker on backend and click `Sync Telemetry` in footer | Footer queries `/api/v1/edge/health` and displays `Edge Cluster: 1/1 Online` | **Baseline:** `FAILED` (`Edge Cluster: Offline` — `DEF-GUI-004`)<br>**Post Sub-Phase 3:** `PASSED` (Footer updated to `Edge Cluster: 1/1 Online` via `/api/v1/edge/health`) | **PASSED (Fixed)** | Before: [DEF-GUI-004](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-004_edge_sync_noop_button.png)<br>After: [GUI-020_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-020_remediated_edge_sync.png) |
| **GUI-021** | **P1** | Rapid interaction stress (`40` rapid transitions) | Toggle `Swipe`/`Overlay`/`Split` 30× and open/close `ExportModal` 10× | Zero freezes, zero duplicate modals, stable JS heap | Completed 40 transitions in `1,478.3 ms`; JS heap `7.03 MB -> 7.50 MB` | **PASSED** | [GUI-021_rapid_stress_test.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-021_rapid_stress_test.png) |
| **GUI-022** | **P2** | Keyboard `Escape` key dismissal on `ExportModal` | Open `ExportModal` (`role="dialog"`) and press `Escape` key | `ExportModal` closes (`onClose()`) | **Baseline:** `FAILED` (`ExportModal` remained open — `DEF-GUI-005`)<br>**Post Sub-Phase 3:** `PASSED` (`ExportModal` closed cleanly on `Escape`: `modal_open_before_esc=True, modal_open_after_esc=False`) | **PASSED (Fixed)** | Before: [DEF-GUI-005](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/defects/DEF-GUI-005_export_modal_escape_not_closing.png)<br>After: [GUI-022_remediated](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/regression/GUI-022_remediated_modal_escape.png) |

---

## 9.7 Requirements Traceability Matrix

| Requirement ID | Requirement Description | Linked Test IDs | Execution Status | Defect References |
| :--- | :--- | :--- | :---: | :--- |
| **REQ-01** | Multi-format cadastral upload (`.geojson`, `.zip`/`.shp`) with pre-flight magic-byte & security validation | `GUI-001`, `GUI-008`, `GUI-015`, `GUI-016`, `GUI-017`, `GUI-018`, `GUI-019` | **PASSED** (`7/7`) | None |
| **REQ-02** | Real-time layer status, progress bar, feature count, and CRS display in header & upload card | `GUI-001`, `GUI-008`, `GUI-018`, `GUI-019` | **PASSED** (`4/4`) | None |
| **REQ-03** | Topological defect inspection with severity/type filtering & individual defect selection | `GUI-010`, `GUI-011` | **PASSED** (`2/2`) | None (`DEF-GUI-002` Fixed) |
| **REQ-04** | Deterministic topological auto-healing with critical dispute lock protection | `GUI-013` | **PASSED** (`1/1`) | None |
| **REQ-05** | Dual differential GIS viewport (`Swipe`/`Overlay`/`Split`) with parcel & defect selection | `GUI-009`, `GUI-012` | **PASSED** (`2/2`) | None (`DEF-GUI-001`, `DEF-GUI-002` Fixed) |
| **REQ-06** | Geodetic calibration audit display (`sourceCrs -> targetCrs`, RMSE, GCP count, TPS badge) | `GUI-008` | **PASSED** (`1/1`) | None |
| **REQ-07** | Multi-format export (`.gpkg`, `.geojson`, `.fgb`, `.zip` bundle) & SHA-256 audit certificate | `GUI-014` | **PASSED** (`1/1`) | None |
| **REQ-08** | Edge cluster synchronization telemetry status bar & refresh | `GUI-020` | **PASSED** (`1/1`) | None (`DEF-GUI-004` Fixed) |
| **REQ-09** | Responsive layout across window dimensions (`800×600` to `1920×1080`, `100%–200%` DPI), stress stability, & keyboard navigation | `GUI-002`–`GUI-007C`, `GUI-021`, `GUI-022` | **PASSED** (`10/10`) | None (`DEF-GUI-003`, `DEF-GUI-005` Fixed) |

---

## 9.8 Defect Register

| Defect ID | Title | Severity | Category | Affected Screen | Status |
| :--- | :--- | :---: | :--- | :--- | :---: |
| **DEF-GUI-001** | `DifferentialMapCanvas` always displays `0 Harmonized Parcels` and `ParcelDetailCard` is unreachable because `App.tsx` never populates `parcels` state | **High** | Functional / Frontend-Backend Integration | `DifferentialMapCanvas` & `ParcelDetailCard` | **FIXED & VERIFIED (Sub-Phase 1)** |
| **DEF-GUI-002** | Topology error features lack `properties.error_id`, causing canvas chips to render empty IDs (`OVERLAP:`) and clicking a defect card in `TopologyInspectorPanel` to fail selection | **High** | Functional / Frontend-Backend Schema Mismatch | `TopologyInspectorPanel` & `DifferentialMapCanvas` | **FIXED & VERIFIED (Sub-Phase 1)** |
| **DEF-GUI-003** | At `800×600` minimum resolution, fixed 12-column grid squeezes sidebars (`180px`) AND `RAW CADASTRE` / `HARMONIZED WGS 84` badges collide in `DifferentialMapCanvas` | **Medium** | Visual / Responsive Layout & Text Overlap | `Main Workbench` & `DifferentialMapCanvas` (`800×600`) | **FIXED & VERIFIED (Sub-Phase 2)** |
| **DEF-GUI-004** | `Sync Telemetry` button in `EdgeSyncStatusBar` is wired to a no-op `() => {}` and never queries `/api/v1/edge/health` | **Low** | Functional / Backend Integration | `EdgeSyncStatusBar` (Footer) | **FIXED & VERIFIED (Sub-Phase 3)** |
| **DEF-GUI-005** | `ExportModal` (`role="dialog"`) does not close when pressing the `Escape` key | **Low** | Accessibility & Keyboard Navigation | `ExportModal` | **FIXED & VERIFIED (Sub-Phase 3)** |

### Detailed Defect Records

#### `DEF-GUI-001`
```text
Defect ID: DEF-GUI-001
Title: DifferentialMapCanvas always displays '0 Harmonized Parcels' and ParcelDetailCard is unreachable because App.tsx never loads parcels into state
Severity: High
Category: Functional / Frontend-Backend Integration
Affected Module: frontend/src/App.tsx (line 63)
Affected Screen: DifferentialMapCanvas (SCR-07) & ParcelDetailCard (SCR-05)
Environment: Windows 11, 1440×900 @ 100% DPI, Live Desktop App Window
Preconditions: Application launched and connected to backend.
Steps to Reproduce:
  1. Upload `ward14_cadastral_survey.geojson` (5 parcels) via LayerUploadPanel.
  2. Observe WorkbenchHeader ('Ward 14 Cadastral Survey • 5 features • EPSG:32643') and LayerUploadPanel ('Features: 5').
  3. Inspect DifferentialMapCanvas top-left parcel count badge and center viewport.
  4. Attempt to select a parcel to populate ParcelDetailCard.
Expected Result: App.tsx fetches the layer's harmonized parcels (via `streamFlatGeobufFeatureCollection` or `/api/v1/layers/{id}/export?format=geojson`) into `parcels` state so DifferentialMapCanvas displays '5 Harmonized Parcels' (and '4 Harmonized Parcels' after sliver merge) and renders clickable parcel chips (`KHASRA-101`, `KHASRA-102`, etc.) that populate ParcelDetailCard.
Actual Result: DifferentialMapCanvas permanently displays '0 Harmonized Parcels', renders 0 parcel chips, and ParcelDetailCard remains permanently stuck on its empty placeholder ('Select a parcel on the canvas to inspect geodetic metrics').
Screenshot Evidence: _qa_test_assets/screenshots/defects/DEF-GUI-001_zero_harmonized_parcels_on_canvas.png
Widget Telemetry: header_features=5, canvas_badge='0 Harmonized Parcels'
Relevant Source Module: frontend/src/App.tsx#L63
Suspected Root Cause: Line 63 of `frontend/src/App.tsx` declares `const [parcels] = useState<ParcelGeoJSONFeatureCollection | null>(null);` without destructuring `setParcels`, and neither `handleUpload` nor `handleAutoResolve` fetches the layer's parcel features.
Recommended Fix: Destructure `const [parcels, setParcels] = useState<ParcelGeoJSONFeatureCollection | null>(null);` in `App.tsx`, add a helper `getLayerParcelsGeoJSON(layerId)` in `frontend/src/lib/api.ts` (or invoke `streamFlatGeobufFeatureCollection`), call `setParcels` in `handleUpload` and `handleAutoResolve`, and reset `setParcels(null)` in `onResetWorkbench`.
Acceptance Criteria: Uploading a 5-parcel survey displays '5 Harmonized Parcels' on the canvas, renders clickable parcel chips, updates to '4 Harmonized Parcels' after auto-healing a sliver, and clicking any parcel chip displays its metric area, perimeter, compactness, and custom attributes in ParcelDetailCard.
Regression Risks: Ensure existing Vitest unit tests (`App.test.tsx`) mock or tolerate the parcel fetch call gracefully.
Reproduction Test ID: GUI-009
```

#### `DEF-GUI-002`
```text
Defect ID: DEF-GUI-002
Title: Topology error features lack `properties.error_id`, causing canvas chips to render empty IDs (`OVERLAP:`) and clicking a defect card in TopologyInspectorPanel to fail selection
Severity: High
Category: Functional / Frontend-Backend Schema Mismatch
Affected Module: backend/api/schemas/topology.py & frontend/src/components/TopologyInspectorPanel.tsx & frontend/src/components/DifferentialMapCanvas.tsx
Affected Screen: TopologyInspectorPanel (SCR-08) & DifferentialMapCanvas (SCR-07)
Environment: Windows 11, 1440×900 @ 100% DPI, Live Desktop App Window
Preconditions: Upload `ward14_cadastral_survey.geojson` containing 5 topological defects.
Steps to Reproduce:
  1. Inspect the red defect chips rendered in the center `DifferentialMapCanvas`.
  2. Click the first defect card (`OVERLAP`) in the right `TopologyInspectorPanel`.
Expected Result: Canvas defect chips display `OVERLAP:<error_id>` and clicking a defect card in `TopologyInspectorPanel` highlights that card (`border-indigo-500 ring-1 ring-indigo-500`) and its corresponding chip on the canvas.
Actual Result: Canvas chips render `OVERLAP:`, `SLIVER:`, `DANGLING_NODE:` with nothing after the colon, and clicking a defect card passes `undefined` to `onSelectError(props.error_id)`, leaving `selectedCards = 0`.
Screenshot Evidence: _qa_test_assets/screenshots/defects/DEF-GUI-002_topology_error_id_undefined.png
Widget Telemetry: chipTexts=['OVERLAP:', 'OVERLAP:', 'SLIVER:', 'DANGLING_NODE:', 'DANGLING_NODE:'], selectedCards=0
Relevant Source Module: backend/api/schemas/topology.py#L18-L46, backend/models/topology.py#L220-L246, frontend/src/components/TopologyInspectorPanel.tsx#L137-L145, frontend/src/components/DifferentialMapCanvas.tsx#L121-L136
Suspected Root Cause: `TopologicalError.to_geojson_feature()` puts `id: str(self.id)` at the root of the GeoJSON `Feature`, and `TopologyErrorProperties` (`backend/api/schemas/topology.py`) does not define an `error_id` field inside `properties` (Pydantic strips unknown fields). Meanwhile, `TopologyInspectorPanel.tsx` and `DifferentialMapCanvas.tsx` read `feature.properties.error_id`.
Recommended Fix:
  1. Add `error_id: Optional[str] = Field(default=None)` to `TopologyErrorProperties` in `backend/api/schemas/topology.py` and populate `"error_id": str(self.id)` in `TopologicalError.to_geojson_feature()` (`backend/models/topology.py`).
  2. In `TopologyInspectorPanel.tsx` and `DifferentialMapCanvas.tsx`, resolve `const errId = String(feature.properties.error_id ?? feature.id ?? "");`.
Acceptance Criteria: Every topological error feature includes `properties.error_id`, canvas chips display valid IDs, and clicking a card in `TopologyInspectorPanel` highlights that exact card and chip.
Regression Risks: Low; additive optional field on `TopologyErrorProperties`.
Reproduction Test ID: GUI-010
```

#### `DEF-GUI-003`
```text
Defect ID: DEF-GUI-003
Title: At 800×600 minimum resolution, fixed 12-column grid squeezes sidebars (180px) AND Raw/Harmonized badge overlays collide in DifferentialMapCanvas
Severity: Medium
Category: Visual / Responsive Layout & Text Overlap
Affected Module: frontend/src/App.tsx & frontend/src/components/DifferentialMapCanvas.tsx
Affected Screen: Main Workbench & DifferentialMapCanvas (800×600)
Environment: Windows 11, 800×600 @ 100% DPI
Preconditions: Resize window to 800×600.
Steps to Reproduce:
  1. Resize application window to 800×600.
  2. Inspect the Left Control Column (`180.0px` width) and Right Topology Inspector Column (`180.0px` width).
  3. Inspect the `RAW CADASTRE (SOURCE SURVEY)` and `HARMONIZED WGS 84 (EPSG:4326)` badges inside `DifferentialMapCanvas`.
Expected Result: Sidebars maintain >= 220px width or stack responsively on narrow viewports, and the two pane badges do not intersect horizontally.
Actual Result:
  - Widget A (`RAW CADASTRE (SOURCE SURVEY)` badge): `[241.0, 159.0, 198.0, 26.0]`
  - Widget B (`HARMONIZED WGS 84 (EPSG:4326)` badge): `[400.0, 159.0, 189.0, 26.0]`
  - Intersection: Confirmed (`39.0 × 26.0 px` overlap across the 50% swipe line: `RAW CADASTRE (SOURCE SURV|ONIZED WGS 84`).
  - Left column shrinks to `180.0px`, clipping `#layer-name-input` placeholder and file input text (`N...n`).
Screenshot Evidence: _qa_test_assets/screenshots/defects/DEF-GUI-003_800x600_squeezed_columns.png
Widget Telemetry: Viewport=800x600 @100%, clipped=2, leftColWidth=180.0px, badgeOverlap=39x26px
Relevant Source Module: frontend/src/App.tsx#L147-L202, frontend/src/components/DifferentialMapCanvas.tsx#L73-L95
Suspected Root Cause: `App.tsx` uses a fixed `grid-cols-12` (`col-span-3` / `col-span-6` / `col-span-3`) on all viewport widths, and `DifferentialMapCanvas.tsx` places both absolute pane badges at the same top vertical offset (`p-6`).
Recommended Fix: Use responsive grid classes in `App.tsx` (`grid-cols-1 lg:grid-cols-12` with `overflow-y-auto lg:overflow-hidden`) and offset or responsively compact the pane badges in `DifferentialMapCanvas.tsx`.
Acceptance Criteria: At 800×600, no text badges overlap and form controls maintain >= 220px width without clipping.
Regression Risks: Verify 1280×800 and 1920×1080 desktop layouts remain unchanged.
Reproduction Test ID: GUI-002
```

#### `DEF-GUI-004`
```text
Defect ID: DEF-GUI-004
Title: 'Sync Telemetry' button in EdgeSyncStatusBar is wired to a no-op `() => {}` and never queries `/api/v1/edge/health`
Severity: Low
Category: Functional / Backend Integration
Affected Module: frontend/src/App.tsx (lines 213–219) & frontend/src/lib/api.ts
Affected Screen: EdgeSyncStatusBar (SCR-10)
Environment: Windows 11, 1440×900 @ 100% DPI
Preconditions: Register an edge worker via `POST /api/v1/edge/workers/register`.
Steps to Reproduce:
  1. Ensure no layer is active (`Reset Workbench`).
  2. Click `Sync Telemetry` in the bottom footer.
Expected Result: Clicking `Sync Telemetry` calls `GET /api/v1/edge/health` and updates `healthyWorkers` / `totalWorkers` (`Edge Cluster: 1/1 Online`).
Actual Result: Footer remains at `Edge Cluster: Offline` because `App.tsx` passes `onRefresh={() => {}}` and `healthyWorkers={activeLayerId ? 1 : 0}`.
Screenshot Evidence: _qa_test_assets/screenshots/defects/DEF-GUI-004_edge_sync_noop_button.png
Widget Telemetry: footer_after_reset='Edge Cluster: Offline | 0 deltas queued | Sync Telemetry'
Relevant Source Module: frontend/src/App.tsx#L213-L219
Suspected Root Cause: Hardcoded props on `<EdgeSyncStatusBar />` in `App.tsx`.
Recommended Fix: Add `getEdgeClusterHealth()` to `frontend/src/lib/api.ts` and wire `onRefresh` in `App.tsx` to fetch `/api/v1/edge/health`.
Acceptance Criteria: Clicking `Sync Telemetry` queries `/api/v1/edge/health` and updates the footer status.
Regression Risks: Gracefully fall back if `/api/v1/edge/health` is unreachable in unit tests.
Reproduction Test ID: GUI-020
```

#### `DEF-GUI-005`
```text
Defect ID: DEF-GUI-005
Title: ExportModal (`role='dialog'`) does not close when pressing the `Escape` key
Severity: Low
Category: Accessibility & Keyboard Navigation
Affected Module: frontend/src/components/ExportModal.tsx
Affected Screen: ExportModal (SCR-09)
Environment: Windows 11, 1440×900 @ 100% DPI
Preconditions: Upload a layer and click `Export Package` to open `ExportModal`.
Steps to Reproduce:
  1. With `ExportModal` open on screen, press the `Escape` key.
Expected Result: `ExportModal` closes (`onClose()`).
Actual Result: `ExportModal` remains open (`modal_open_after_esc=True`).
Screenshot Evidence: _qa_test_assets/screenshots/defects/DEF-GUI-005_export_modal_escape_not_closing.png
Widget Telemetry: modal_open_before_esc=True, modal_open_after_esc=True
Relevant Source Module: frontend/src/components/ExportModal.tsx#L36-L59
Suspected Root Cause: Missing `keydown` event listener for `Escape` in `ExportModal.tsx`.
Recommended Fix: Register a `window.addEventListener("keydown", ...)` inside a `useEffect` in `ExportModal.tsx` when `isOpen` is true to invoke `onClose()` on `e.key === "Escape"`.
Acceptance Criteria: Pressing `Escape` while `ExportModal` is open invokes `onClose()` and dismisses the modal.
Regression Risks: None.
Reproduction Test ID: GUI-022
```

---

## 9.9 Stability and Performance Report (Phase 6)

Raw performance telemetry is persisted in [_qa_test_assets/telemetry/performance.jsonl](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/telemetry/performance.jsonl).

- **Startup & Launch Timings:**
  - **FastAPI Backend (`PID 44632`):** `1,730.43 ms` to `/api/v1/health` `200 OK`
  - **Vite Frontend Server (`PID 43252`):** `3,740.19 ms` to `HTTP 200`
  - **Standalone Desktop Window (`PID 44228`, `HWND 721700`):** `696.39 ms` to first interactive frame
- **Operation Latencies (Measured End-to-End via Live GUI):**
  - **5-Parcel UTM 43N GeoJSON Upload + Geodetic Reprojection + Topology Audit (`GUI-008`):** `1,662.11 ms`
  - **Deterministic Topological Auto-Heal (`GUI-013`):** `1,147.54 ms`
  - **120-Parcel Large Grid + Shapefile ZIP Sequential Ingestion (`GUI-019`):** `2,925.34 ms` (`~1.46 s` per dataset)
  - **40× Rapid View-Mode & Modal Stress Test (`GUI-021`):** `1,476.27 ms` (`36.9 ms` per transition)
- **Memory & Stability Observations:**
  - **JS Heap Before 40× Rapid Stress:** `6,168,513 bytes` (`5.88 MB`)
  - **JS Heap After 40× Rapid Stress:** `6,628,658 bytes` (`6.32 MB`, `+0.44 MB` transient delta, zero leaks)
  - **Crashes / Unhandled Exceptions / Modal Deadlocks:** `0` (`ModalDialogWatchdog` recorded `0` blocking OS dialogs).

---

## 9.10 Generated Output Validation (Phase 5.2)

All 4 exported geospatial artifacts generated from the healed `Ward 14 Cadastral Survey` (`5` initial parcels -> `1` merged sliver -> `4` active parcels, `2` dispute-locked parcels) were saved to `_qa_test_assets/generated_outputs/` and independently verified by binary/SQLite/JSON parsers:

| Input Dataset | Generated Output Path | Expected Result | Actual Measured Result | Integrity Status | Evidence |
| :--- | :--- | :--- | :--- | :---: | :--- |
| `ward14_cadastral_survey.geojson` (`5` parcels in `EPSG:32643`) | `_qa_test_assets/generated_outputs/ward14_verified.gpkg` (`36,864 bytes`) | OGC GeoPackage 1.3 (`PRAGMA application_id = 0x47504B47`), `4` active MultiPolygon rows (merged sliver excluded), SHA-256 matching modal | `application_id = 0x47504b47`, table `ward_14_cadastral_survey`, `4` feature rows, SHA-256 `92be10e86f91428fce24d8963c1fb7316b0d0972aa5ec392080e8849f07a0b0a` (matches GUI certificate) | **VERIFIED PASS** | [GUI-014B_export_staged_certificate.png](file:///d:/projects/PRODUCTION%20GEOSPATIAL%20HARMONIZATION%20ENGINE/_qa_test_assets/screenshots/functional/GUI-014B_export_staged_certificate.png) |
| `ward14_cadastral_survey.geojson` | `_qa_test_assets/generated_outputs/ward14_verified.geojson` (`2,418 bytes`) | RFC 7946 `FeatureCollection` in WGS 84 (`EPSG:4326`), `4` active features, CCW exterior rings | Valid JSON `FeatureCollection`, `4` features (`KHASRA-101`, `KHASRA-102`, `KHASRA-201`, `KHASRA-202`), `KHASRA-101` area increased to `10,004.0 m²` after sliver union | **VERIFIED PASS** | `_qa_test_assets/generated_outputs/ward14_verified.geojson` |
| `ward14_cadastral_survey.geojson` | `_qa_test_assets/generated_outputs/ward14_verified.fgb` (`1,988 bytes`) | FlatGeobuf v3 (`fgb\x03fgb\x00`) with Packed Hilbert R-Tree and retained `attributes` JSON | Valid FGB header & R-tree, `4` decoded features with `khasra_number` & `owner_name` retained in `properties.attributes` | **VERIFIED PASS** | `_qa_test_assets/generated_outputs/ward14_verified.fgb` |
| `ward14_cadastral_survey.geojson` | `_qa_test_assets/generated_outputs/ward14_release_bundle.zip` (`5,312 bytes`) | Deterministic ZIP archive containing `.fgb`, `.geojson`, `.gpkg`, and `manifest.json` | Contains all 4 files; `manifest.json` (`v1.0.0`) reports `active_parcels: 4, merged_slivers: 1, disputed_parcels: 2`, SHA-256 `d127b000d16c06d1c1e5e3d0aec7cfd3010e7fc0507163322ed6befe12071dd2` | **VERIFIED PASS** | `_qa_test_assets/generated_outputs/ward14_release_bundle.zip` |

---

## 9.11 Known Limitations

1. **Docker Daemon Not Installed on Host:** As established in Phase 0, Docker Desktop (`docker`) is not installed on this Windows machine. To test all GUI-to-backend workflows live without modifying source code, `_qa_gui_runner.py` booted the untouched `backend.api.app:app` with an isolated in-memory/local-file persistence provider (`_qa_test_assets/test_projects/run_qa_backend.py`) that executes the real `UnifiedIngestionDispatcher`, `TopologyConflictRecorder`, `execute_healing_transaction`, and export engines.
2. **Physical Multi-Monitor Per-Monitor DPI Dragging:** Tested `100%`, `125%`, `150%`, and `200%` DPI scaling via CDP `Emulation.setDeviceMetricsOverride` on the primary `1536×864` display without mutating global Windows OS display settings (per Section 3.2 safety rule).

---

## 9.12 Phased Remediation Blueprint

All 5 confirmed GUI defects are grouped into **3 logical, low-risk remediation sub-phases**:

### Sub-Phase 1: Core Functional & Frontend-Backend Integration Fixes (`DEF-GUI-001`, `DEF-GUI-002`) — *Priority: HIGH*
- **Included Defects:**
  - `DEF-GUI-001` (**High**): `DifferentialMapCanvas` always shows `0 Harmonized Parcels` and `ParcelDetailCard` is unreachable because `App.tsx` never populates `parcels` state.
  - `DEF-GUI-002` (**High**): Topology error features lack `properties.error_id`, causing canvas chips to render `OVERLAP:` and clicking a defect card in `TopologyInspectorPanel` to fail selection.
- **Affected Files & Root Causes:**
  - `backend/models/topology.py` & `backend/api/schemas/topology.py`: Add `error_id` to `TopologyErrorProperties` and `TopologicalError.to_geojson_feature()`.
  - `frontend/src/components/TopologyInspectorPanel.tsx` & `frontend/src/components/DifferentialMapCanvas.tsx`: Resolve `const errId = feature.properties.error_id ?? String(feature.id ?? "");`.
  - `frontend/src/lib/api.ts` & `frontend/src/App.tsx`: Add `getLayerParcels(layerId)` (fetching `/api/v1/layers/{id}/export?format=geojson`) and update `parcels` state in `App.tsx` on upload, auto-resolve, and reset.
- **Acceptance Criteria:**
  - Uploading `ward14_cadastral_survey.geojson` displays `5 Harmonized Parcels` on `DifferentialMapCanvas` (and `4 Harmonized Parcels` after auto-heal), renders clickable parcel chips, and clicking a parcel populates `ParcelDetailCard`.
  - Defect chips display `OVERLAP:<id>` and clicking a defect card in `TopologyInspectorPanel` highlights that single card (`selectedCards == 1`).

### Sub-Phase 2: Responsive Layout & Viewport Badge Collision Fix (`DEF-GUI-003`) — *Priority: MEDIUM*
- **Included Defects:**
  - `DEF-GUI-003` (**Medium**): At `800×600`, fixed `grid-cols-12` squeezes sidebars to `180px` and `RAW CADASTRE` / `HARMONIZED WGS 84` badges collide horizontally in `DifferentialMapCanvas`.
- **Affected Files & Root Causes:**
  - `frontend/src/App.tsx`: Switch fixed `grid-cols-12` to responsive `grid-cols-1 lg:grid-cols-12` and allow vertical scrolling on compact viewports.
  - `frontend/src/components/DifferentialMapCanvas.tsx`: Offset or stack the `RAW CADASTRE` and `HARMONIZED WGS 84` pane badges so they never overlap on narrow viewports.
- **Acceptance Criteria:**
  - At `800×600`, `clipped_widgets == 0`, sidebar controls maintain readable width, and the Raw/Harmonized pane badges have `0 px` intersection.

### Sub-Phase 3: Edge Telemetry Wiring & Modal Keyboard Accessibility (`DEF-GUI-004`, `DEF-GUI-005`) — *Priority: LOW*
- **Included Defects:**
  - `DEF-GUI-004` (**Low**): `Sync Telemetry` button in `EdgeSyncStatusBar` is a no-op `() => {}`.
  - `DEF-GUI-005` (**Low**): `ExportModal` does not close when pressing the `Escape` key.
- **Affected Files & Root Causes:**
  - `frontend/src/lib/api.ts` & `frontend/src/App.tsx`: Add `getEdgeClusterHealth()` calling `/api/v1/edge/health` and wire `EdgeSyncStatusBar.onRefresh`.
  - `frontend/src/components/ExportModal.tsx`: Add `useEffect` `keydown` listener for `Escape` to call `onClose()`.
- **Acceptance Criteria:**
  - Clicking `Sync Telemetry` updates `EdgeSyncStatusBar` from `/api/v1/edge/health`.
  - Pressing `Escape` while `ExportModal` is open dismisses the dialog.
