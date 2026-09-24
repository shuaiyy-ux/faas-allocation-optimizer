# Data Source Specification

> When HCA swaps in real data, the files must match this spec exactly.
> Automated validation lives in `scripts/data_migration.py validate <dir>`.

The allocation engine reads 7 CSV files from `data_csv/` (plus one backup for the
Reset button). One additional auxiliary CSV (`sales_tax_by_state.csv`) is not
read by the engine but is registered for the AI agent to answer tax questions.
Total files in `data_csv/`: **9**. Filenames, column names, and formats must
match exactly; column order within a file does not matter.

---

## Required files and columns

### 1. `faas_eligible_vehicles.csv`
Vehicles currently grounded and eligible for FaaS redistribution.

| Column | Type | Notes |
|---|---|---|
| `VIN` | string | Unique per vehicle |
| `DEALER_CODE` | string | Grounding dealer code (see "Dealer code convention" below) |
| `LOCATION` | string | `"City, ST"` format, e.g. `"Houston, TX"` |
| `STATE` | string | 2-letter state code, e.g. `TX` |
| `ZIPCODE` | string | 5-digit ZIP, will be zero-padded if numeric |
| `RESIDUAL` | number | Residual value in USD |

### 2. `dealer_inventory.csv`
FaaS dealer inventory and capacity.

| Column | Type | Notes |
|---|---|---|
| `DEALER_CODE` | string | FaaS dealer code (prefix `FD`) |
| `DEALER_NAME` | string | Human-readable name |
| `STATE` | string | 2-letter state code |
| `ZIPCODE` | string | 5-digit ZIP |
| `TRUE_CAPACITY` | integer | Total slots HCA allocates to this dealer (includes rented-out cars) |
| `DELIVERED_COUNT` | integer | Slots already consumed — every car accounted to this dealer (in inventory **and** currently rented out). Does NOT mean "physically present at lot". |
| `LATITUDE` | number | Dealer coordinate |
| `LONGITUDE` | number | Dealer coordinate |

Derived field `REMAINING_CAPACITY = TRUE_CAPACITY − DELIVERED_COUNT − in_transit_count` (computed by the engine, not stored). `in_transit_count` is the live count of `fleet_inventory.STATUS = Transporting` rows assigned to this dealer. **A rented car still consumes a slot** — do not subtract `RENTED` from this formula. Note: the UI label "Capacity" refers to `IN_SERVICE − RENTED` (idle on-lot stock), which is a *different* quantity from `REMAINING_CAPACITY` (the solver's slot limit). Don't conflate the two.

### 3. `dealer_utilization.csv`
Weekly utilization snapshot per FaaS dealer.

| Column | Type | Notes |
|---|---|---|
| `DEALER_ID` | string | Must match `dealer_inventory.DEALER_CODE` |
| `UTILIZATION` | number | **0–100 scale**, not 0–1 (engine divides by 100) |
| `NAME` | string | Optional |
| `RENTED` | integer | Optional under additive mode; the demand signal weighted by `w_rented`. |
| `IN_SERVICE` | integer | **Required under bucket mode** (`scoring_mode="bucket"`) — drives the max-anchored tier (A/B/C/D) that picks `bucket_mult`. Display-only under additive mode. See `app/scoring/bucket.py` and `docs/bucket_algorithm.md`. |

### 4. `dealer_distance_matrix.csv`
Pre-computed Haversine (great-circle) distances between every (grounding dealer, FaaS dealer) pair.

| Column | Type | Notes |
|---|---|---|
| `GROUNDING_DEALER_CODE` | string | Matches `faas_eligible_vehicles.DEALER_CODE` |
| `FAAS_DEALER_CODE` | string | Matches `dealer_inventory.DEALER_CODE` |
| `DISTANCE_MILES` | number | Haversine (straight-line) miles. Underestimates actual drivable distance by ~20–30 %. If HCA later supplies drivable miles, drop them into this column unchanged. |

Every (source, dealer) pair that appears in `faas_eligible_vehicles.csv` × `dealer_inventory.csv` must have a row here.

### 5. `property_tax_by_state.csv`
Annual vehicle property tax rate per state.

| Column | Type | Notes |
|---|---|---|
| `STATE` | string | 2-letter state code |
| `PROPERTY_TAX_RATE` | string | **Includes `%` suffix**, e.g. `"2.5%"`. States with no tax can be omitted (default 0). |

### 6. `zip_centroids.csv`
ZIP → lat/lon lookup for grounding dealer locations.

| Column | Type | Notes |
|---|---|---|
| `ZIPCODE` | string | 5-digit ZIP |
| `LATITUDE` | number | Centroid latitude |
| `LONGITUDE` | number | Centroid longitude |

**Must contain every ZIP referenced in `faas_eligible_vehicles.csv`** — engine raises a hard error on missing entries.

### 7. `fleet_inventory.csv`
Current status of each vehicle in the fleet lifecycle.

| Column | Type | Notes |
|---|---|---|
| `VIN` | string | Matches `faas_eligible_vehicles.VIN` if grounded |
| `YEAR` | integer | |
| `MAKE` | string | |
| `MODEL` | string | |
| `SOURCE` | string | Grounding dealer code (where the car currently is) |
| `SOURCE_CITY` | string | |
| `SOURCE_STATE` | string | 2-letter state code |
| `RESIDUAL` | number | USD |
| `STATUS` | enum | One of `Incoming`, `Grounded`, `Transporting`, `Delivered` |
| `WEEK` | string | ISO date, e.g. `"2025-12-15"` |
| `ASSIGNED_DEALER` | string | FaaS dealer code when `STATUS != Grounded`; empty otherwise |
| `NOTES` | string | Free text (e.g. `"En route to Dallas dealer"`) |

### 8. `fleet_inventory_original.csv`
Exact copy of `fleet_inventory.csv` at initialization. The UI's **Reset** button restores the current browser session's in-memory fleet snapshot from this baseline. It does not write `fleet_inventory.csv` to disk. If you swap in new fleet data, swap both files.

### 9. `sales_tax_by_state.csv` *(auxiliary — not read by the engine)*

Reference table surfaced to the AI agent for sales-tax questions. The
allocation engine does not read it; dropping it won't break the engine, but
removes the agent's ability to answer questions like "what's the sales tax in
TX?".

| Column | Type | Notes |
|---|---|---|
| `STATE` | string | 2-letter state code |
| `SALES_TAX_RATE` | string | **Includes `%` suffix**, e.g. `"7.25%"`. |

---

## Cross-file integrity rules

The validator enforces these:

1. `dealer_utilization.DEALER_ID` ⊆ `dealer_inventory.DEALER_CODE`
2. `dealer_inventory.ZIPCODE` ⊆ `zip_centroids.ZIPCODE` (hard — missing ZIPs crash the engine)
3. `faas_eligible_vehicles.ZIPCODE` ⊆ `zip_centroids.ZIPCODE` (hard)
4. Every pair (grounding_dealer, faas_dealer) needed by the solver has a row in `dealer_distance_matrix.csv`
5. `dealer_inventory.STATE` ⊆ `property_tax_by_state.STATE` (soft — missing states default to 0%)
6. `fleet_inventory.STATUS` values are in the allowed enum
7. `fleet_inventory.ASSIGNED_DEALER` (when present) ⊆ `dealer_inventory.DEALER_CODE`

---

## Dealer code convention

Two code spaces co-exist and are distinguished by **prefix**:
- `FD...` — FaaS dealer (destination, has capacity)
- `D...` — Grounding dealer (source, where vehicles are returned)

Several places in the code use `startswith("FD")` to filter FaaS dealers
(see `engine.get_overview`, `server.py`). If real HCA data uses a different
scheme, you have two options:
1. Re-label the real data to use `FD/D` prefixes
2. Replace the `startswith("FD")` checks with a column-based flag (3 spots in `app/`)

Option 1 is the path of least resistance.

---

## Swap workflow

```bash
# 1. Put the new CSVs in a scratch directory (all 8 required + optional aux)
mkdir -p /tmp/new_data
cp <your files> /tmp/new_data/

# 2. Validate (read-only; generates VALIDATION_REPORT.md next to the dir)
python scripts/data_migration.py validate /tmp/new_data

# 3. Back up the current data, then copy the new files in
cp -R data_csv "data_csv_backup_$(date +%Y%m%d_%H%M%S)"
cp /tmp/new_data/*.csv data_csv/

# 4. Re-validate the installed copy as a sanity check
python scripts/data_migration.py validate data_csv/

# 5. Start the server
./start.sh
```

To roll back: copy the backup directory back over `data_csv/`.

```bash
cp -R data_csv_backup_<timestamp>/*.csv data_csv/
```
