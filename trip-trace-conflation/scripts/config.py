import pathlib

# Define file name
location_tbl = "location.csv"
trip_tbl = "trip.csv"

# M directory for survey work
M_survey_data_dir = pathlib.Path(r"C:\Users\Carson_H\Santa Clara Valley Transportation Authority\ModelingGIS - ModelingGIS Document Library\Surveys\BATS2023_data\weightedDataset_02212025\WeightedDataset_02212025")

# Define input file paths
location_path = (
    M_survey_data_dir /  location_tbl
)
trip_path = M_survey_data_dir / trip_tbl

region_boundary_path = (
    M_survey_data_dir
    / "Survey Conflation"
    / "OSM_regional_network_convex_hull"
    / "bay_area_regional_boundary_convex_hull.geojson"
)
local_network_path = (
    M_survey_data_dir
    / "Survey Conflation"
    / "OSM_regional_network_convex_hull"
    / "bay_area_network.json"
)

# Define output file paths
out_file_path =     pathlib.Path(r"C:\Users\Carson_H\OneDrive - Santa Clara Valley Transportation Authority\Documents\BATS\TripTraceConflation")

gpkg_path = out_file_path / "tds_conflation_results.gpkg"

select_link_shape = r"C:\Users\Carson_H\OneDrive - Santa Clara Valley Transportation Authority\Documents\BATS\ExpressLaneAnalysis\GIS\2025_managed_lane.shp"