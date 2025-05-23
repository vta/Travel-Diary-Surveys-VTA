USAGE = """
  Associate travel diary survey smartphone trip traces with Bay Area roadway facilities to enable matching
  of survey demographics/trip characteristics of users with bridges, express lanes, etc. The script should
  work with hundreds of thousands, possibly millions of x,y smartphone pings.
  
  See Readme.md for more detail.
"""
import argparse
import logging
import multiprocessing
import os
import pathlib
import shutil
import sys
from concurrent.futures import ProcessPoolExecutor, as_completed
from datetime import datetime
from logging.handlers import QueueHandler, QueueListener

import config
import geopandas as gpd
import networkx as nx
import osmnx as ox
import pandas as pd
import pyproj
import shapely.ops
from shapely.ops import unary_union
from mappymatch import package_root
from mappymatch.constructs.geofence import Geofence
from mappymatch.constructs.trace import Trace
from mappymatch.maps.nx.nx_map import NetworkType, NxMap
from mappymatch.matchers.lcss.lcss import LCSSMatcher
from mappymatch.utils.crs import LATLON_CRS, XY_CRS
from shapely.geometry import LineString

# if --download_local_OSM_map is NOT specified, then a single regional matcher is created
# per process.  For single process, this is done in main(); for multiprocess, this is done in init_worker()
# This is the instance of that matcher.
process_regional_nx_map = None
# per-process counter for number of trips matched
num_trips_matched = 0


## Define function to create a NxMap object from a GeoJSON file
def nx_map_from_geojson(geojson_path, local_network_path, network_type=NetworkType.DRIVE):
    """Creates a NxMap object from a GeoJSON file.

    Args:
        geojson_path (str): Path to the GeoJSON file. Must be in EPSG:4326.
        network_type (Enumerator, optional): Enumerator for Network Types supported by osmnx. Defaults to NetworkType.DRIVE.

    Returns:
        A NxMap instance
    """
    if not local_network_path.exists():
        logging.info(
            f"Local network file not found. Creating a new network file from geojson: {geojson_path}"
        )
        geofence = Geofence.from_geojson(str(geojson_path))
        logging.debug(f"{geofence.geometry.bounds=}")
        logging.debug(f"{geofence.geometry.area=}")
        nx_map = NxMap.from_geofence(geofence, network_type)
        logging.info(f"Saving network to {local_network_path}")
        nx_map.to_file(str(local_network_path))
    else:
        logging.info(f"Local network file found. Loading network file {local_network_path}")
        nx_map = NxMap.from_file(str(local_network_path))
        logging.info("Completed reading")

    return nx_map


def create_batch_traces(df, trip_id_column, xy=True):
    """Create a batch of traces from a dataframe with xy coordinates

    Args:
        df (Pandas Dataframe): Dataframe with xy coordinates in EPGS:4326.
        trip_id_column (String): Column name with unique trip ids.
        xy (bool, optional): Projects trace to EPSG:3857. Defaults to True.

    Returns:
        List: List of dictionaries with trip_id, trace, trace_gdf, and trace_line_gdf.
        Structure of the list:
        [
            {
                "trip_id": "unique_id",
                "trace": Trace object,
                "trace_gdf": GeoDataFrame with trace points,
                "trace_line_gdf": GeoDataFrame with trace line
            },
            ...
        ]
    """
    unique_ids = df[trip_id_column].unique()
    batch_traces = []
    for i in unique_ids:
        filter_df = df[df["trip_id"] == i]
        gdf = gpd.GeoDataFrame(
            filter_df, geometry=gpd.points_from_xy(filter_df.lon, filter_df.lat), crs=4326
        )
        batch_trace = Trace.from_geo_dataframe(frame=gdf, xy=xy)

        # create a trace_line_gdf from the trace
        coords = [(p.x, p.y) for p in batch_trace.coords]
        line = LineString(coords)
        trace_line_gdf = gpd.GeoDataFrame([{"geometry": line}], crs="EPSG:3857")
        trace_line_gdf["trip_id"] = i

        # create a trace_gdf from the trace
        trace_gdf = batch_trace._frame
        trace_gdf["trip_id"] = i
        # keep mode_type and collect_time columns
        trace_gdf["collect_time"] = filter_df["collect_time"]
        trace_gdf["mode_type"] = filter_df["mode_type"]

        # logging.debug(f"for trip_id={i}, gdf=\n{gdf}\nand trace_gdf=\n{trace_gdf}")
        # create a dictionary with the trip_id, trace, trace_gdf, and trace_line_gdf and append to the batch_traces list
        trace_dict = {
            "trip_id": i,
            "trace": batch_trace,
            "trace_gdf": trace_gdf,
            "trace_line_gdf": trace_line_gdf,
        }
        batch_traces.append(trace_dict)
    return batch_traces


def init_worker(
    log_queue: multiprocessing.Queue,
    download_local_OSM_map: bool,
    region_boundary_path: pathlib.Path,
    local_network_path: pathlib.Path,
    network_type,
) -> None:
    """Initialize a worker process.

    This initializes the log handling to handoff log messages to the given log_queue.

    It also creates a process-specific regional matcher, if download_local_OSM_map != True
    This is because these can't bre shared between processes because they can't be pickled, so
    each subprocess needs to make its own.
    """
    global process_regional_nx_map
    logger = logging.getLogger()
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)

    handler = QueueHandler(log_queue)
    logger.addHandler(handler)

    logging.info(f"init_worker() called for {os.getpid()=}")
    if download_local_OSM_map == False:
        # Commenting out for now but this makes the debug log noisy and adds another osmnx log
        # ox.settings.log_file = True
        # ox.settings.log_level = logging.DEBUG
        # ox.settings.logs_folder = pathlib.Path.cwd()
        logging.info(f"{process_regional_nx_map=}")

        now = datetime.now()
        process_regional_nx_map = nx_map_from_geojson(
            region_boundary_path, local_network_path, network_type
        )
        later = datetime.now()
        logging.info(f"use_regional_nx_map: Creating networkx map took: {later - now}")


def process_trace(
    trace_dict: dict,
    download_local_OSM_map: bool,
    geofence_buffer,
    network_type=NetworkType.DRIVE,
):
    """Process a single trace using a instance of the LCSSMatcher class.

    Returns a matched trace dictionary.

    Args:
        trace_dict (dict): dictionary with trip_id and trace.
        matcher (LCSSMatcher): instance of the LCSSMatcher class.
        geofence_buffer (int): Buffer distance around trip traces. Defaults to 1000 meters.
        network_type (Enumerator, optional): Enumerator for Network Types supported by osmnx. Defaults to NetworkType.DRIVE.

    Returns:
        dict: dictionary with trip_id, trace, matched_result, matched_gdf, and matched_path_gdf.
        Structure of the dictionary:
        {
            "trip_id": trip_id,
            "trace": trace,
            "unmatched_trips": None or trip_id,
            "trace_gdf": trace_gdf,
            "trace_line_gdf": trace_line_gdf,
            "matched_result": match_result,
            "matched_gdf": matched_gdf,
            "matched_path_gdf": matched_path_gdf,
        }

    """
    global process_regional_nx_map
    global num_trips_matched
    # logging.debug(f"process_trace() with {process_regional_nx_map=}")

    try:
        # create a geofence around the trace
        geofence = Geofence.from_trace(trace_dict["trace"], padding=geofence_buffer)
        # note: Geofence crs is set to EPSG:4326 because NxMap.from_geofence() requires it

        # Create a matcher object if matcher is None, else use the provided matcher
        if download_local_OSM_map:
            # create a networkx map from the geofence
            nx_map = NxMap.from_geofence(geofence, network_type=network_type)
        else:
            # convert Geofence from EPSG:4326 (Geofence always uses this) to EPSG:3857 (to match the osmnx graph)
            project = pyproj.Transformer.from_crs(
                crs_from=LATLON_CRS, crs_to=XY_CRS, always_xy=True
            ).transform

            # create a truncated map for performance using
            # https://osmnx.readthedocs.io/en/stable/user-reference.html#osmnx.truncate.truncate_graph_polygon
            truncated_graph = ox.truncate.truncate_graph_polygon(
                process_regional_nx_map.g, shapely.ops.transform(project, geofence.geometry)
            )
            # create the smaller map from it
            nx_map = NxMap(truncated_graph)

        # create the matcher for the given NxMap
        matcher = LCSSMatcher(nx_map)
        # logging.debug(f"Running match_trace for {trace_dict['trip_id']}")
        match_result = matcher.match_trace(trace_dict["trace"])
    except Exception as e:
        logging.warning(
            f"The trace with trip_id {trace_dict['trip_id']} encountered an exception: {e}. Adding trip to the unmatched list."
        )
        trace_dict["unmatched_trips"] = trace_dict["trip_id"]
        return trace_dict

    num_trips_matched += 1
    MATCH_REPORT_FREQUENCY = 100
    if num_trips_matched % MATCH_REPORT_FREQUENCY == 0:
        logging.info(f"Received results for {num_trips_matched} trips")

    # check if any road ids within a list of matches are null
    road_id_check = True
    for match in match_result.matches:
        if match.road is None:
            road_id_check = False
            break
    if road_id_check == False:
        logging.warn(
            f"The trace with trip_id {trace_dict['trip_id']} has null road_ids meaning there was no match to the network. Adding to the unmatched list."
        )
        trace_dict["unmatched_trips"] = trace_dict["trip_id"]
    else:
        # create a geodataframe from the matches and add the trip_id; add the match result and matched df to the trace dictionary
        matched_df = match_result.matches_to_dataframe()
        matched_df["trip_id"] = trace_dict["trip_id"]
        matched_df["road_id"] = matched_df["road_id"]
        # convert road_id to tuple to avoid issues with mapping
        matched_df["road_id"] = matched_df["road_id"].apply(lambda x: tuple(x.to_json().values()))
        matched_gdf = gpd.GeoDataFrame(matched_df, geometry="geom", crs="EPSG:3857")
        # create a geodataframe from the matched path and add the trip_id; add the match result and matched df to the trace dictionary
        matched_path_df = match_result.path_to_dataframe()
        matched_path_df["trip_id"] = trace_dict["trip_id"]
        matched_path_df["road_id"] = matched_path_df["road_id"]
        # convert road_id to tuple to avoid issues with mapping
        matched_path_df["road_id"] = matched_path_df["road_id"].apply(
            lambda x: tuple(x.to_json().values())
        )
        matched_path_gdf = gpd.GeoDataFrame(matched_path_df, geometry="geom", crs="EPSG:3857")
        # add network attributes to the matched gdf and matched path gdf
        attrs = ["osmid", "ref", "name", "maxspeed", "highway", "bridge", "tunnel"]
        for attr in attrs:
            # get attributes from the raw graph
            # attr_dict = nx.get_edge_attributes(nx_map.g, attr)
            attr_dict = nx.get_edge_attributes(matcher.road_map.g, attr)
            # add attributes to the matched gdf
            matched_gdf[attr] = matched_gdf["road_id"].map(attr_dict)
            # add attributes to the matched path gdf
            matched_path_gdf[attr] = matched_path_gdf["road_id"].map(attr_dict)
        # Set unmatched_trips to None and add matched_gdf and matched_path_gdf to the trace dictionary
        trace_dict["unmatched_trips"] = None
        trace_dict["matched_gdf"] = matched_gdf
        trace_dict["matched_path_gdf"] = matched_path_gdf

    return trace_dict

def match_links_from_shapefile(
    shapefile_path,
    download_local_OSM_map,
    geofence_buffer,
    region_boundary_path,
    local_network_path,
    network_type=NetworkType.DRIVE,
):
    """
    Match each feature in a shapefile to the closest OSM path using the same trace matching logic.

    Args:
        shapefile_path (str or Path): Path to the shapefile containing links (LineString or Point).
        download_local_OSM_map (bool): Whether to download a local OSM map for each trace.
        geofence_buffer (int): Buffer distance around each feature.
        region_boundary_path (Path): Path to region boundary geojson.
        local_network_path (Path): Path to local network file.
        network_type: OSMnx network type.

    Returns:
        List of dicts: Each dict contains the original feature id and the match result.
    """
    # if type(shapefile_path == str):
    #     gdf = gpd.read_file(shapefile_path)
    # else:
    #     gdf = shapefile_path
    gdf = shapefile_path.to_crs("EPSG:4326")
    results = gpd.GeoDataFrame()

    # Use index as unique id if no id column
    id_col = "id" if "id" in gdf.columns else gdf.index.name or "index"
    gdf[id_col] = gdf.index

    for _, row in gdf.iterrows():
        # If geometry is LineString, extract coordinates; if Point, use as-is
        if row.geometry.geom_type == "LineString":
            coords = list(row.geometry.coords)
            points_gdf = gpd.GeoDataFrame(
                geometry=[shapely.geometry.Point(x, y) for x, y in coords],
                crs="EPSG:4326"
            )
            trace = Trace.from_geo_dataframe(frame=points_gdf, xy=True)
        else:
            continue  # skip unsupported geometry types

        trace_dict = {
            "trip_id": row[id_col],
            "trace": trace,  # Pass the Trace object, not the geometry or coords
            "trace_gdf": gpd.GeoDataFrame(geometry=[row.geometry], crs="EPSG:4326"),
            "trace_line_gdf": gpd.GeoDataFrame(geometry=[row.geometry], crs="EPSG:4326"),
        }

        # Use the same matching logic as process_trace
        match_result = process_trace(
            trace_dict,
            download_local_OSM_map=download_local_OSM_map,
            geofence_buffer=geofence_buffer,
            network_type=network_type,
        )
        # print(match_result.keys())
        out_gdf = match_result['matched_path_gdf']
        out_gdf['original_id'] = row[id_col]
        results = pd.concat([results,out_gdf], ignore_index = True)

    return results


def batch_process_traces_parallel(
    log_queue,
    traces,
    processes,
    download_local_OSM_map,
    region_boundary_path,
    local_network_path,
    network_type,
    geofence_buffer,
):
    """Batch process traces using an instance of the LCSSMatcher class in parallel using multiprocessing.

    Args:
        traces (List): list of dictionaries with trip_id and trace.
        matcher (LCSSMatcher): instance of the LCSSMatcher class.
        network_type (Enumerator, optional): Enumerator for Network Types supported by osmnx. Defaults to NetworkType.DRIVE.
        geofence_buffer (int): Buffer distance around trip traces. Defaults to 1000 meters.

    Returns:
        List: List of dictionaries with trip_id, trace, matched_result, matched_gdf, and matched_path_gdf.
        Structure of the list:
        [
            {
            "trip_id": trip_id,
            "trace": trace,
            "unmatched_trips": None or trip_id,
            "trace_gdf": trace_gdf,
            "trace_line_gdf": trace_line_gdf,
            "matched_result": match_result,
            "matched_gdf": matched_gdf,
            "matched_path_gdf": matched_path_gdf,
            },
        ...
        ]
    """

    # -- Run the application -- #
    if processes == 1:
        matched_traces = [
            process_trace(trace_dict, download_local_OSM_map, geofence_buffer, network_type)
            for trace_dict in traces
        ]
    else:
        matched_traces = []
        futures = []
        # process traces in parallel
        executor = ProcessPoolExecutor(
            max_workers=processes,
            initializer=init_worker,
            initargs=(
                log_queue,
                download_local_OSM_map,
                region_boundary_path,
                local_network_path,
                network_type,
            ),
        )
        with executor:
            for trace_dict in traces:
                future = executor.submit(
                    process_trace, trace_dict, download_local_OSM_map, geofence_buffer, network_type
                )
                # logging.debug(f"completed executor.submit; {future=}")
                futures.append(future)

                if len(futures) % 1000 == 0:
                    logging.debug(f"Submitted {len(futures)} traces")
            logging.info(f"Completed submitting {len(futures):,} traces")

            for future in as_completed(futures):
                if len(matched_traces) % 1000 == 0:
                    logging.debug(f"Retrieved {len(matched_traces):,} traces")
                matched_traces.append(future.result())
            logging.info(f"Completed acquiring {len(matched_traces):,} traces")
    return matched_traces


def concatenate_matched_gdfs(matched_traces, match_type="matched_gdf"):
    """Concatenate matched trace geodataframes into a single geodataframe.

    Args:
        matched_traces (List): List of dictionaries with matched trace geodataframes.
        match_type (String, optional): Type of match to concatenate. Defaults to "matched_gdf".
        Options are "matched_gdf", "matched_path_gdf", "trace_gdf".

    Returns:
        GeoDataFrame: Concatenated geodataframe.
    """
    matched_gdfs = []
    for trace_dict in matched_traces:
        # check if the match type is in the trace dictionary
        if match_type not in list(trace_dict.keys()):
            # logging.debug(f"Match type {match_type} not found in trace dictionary. Skipping.")
            continue
        else:
            # logging.debug(f"Match type {match_type} found in trace dictionary.")
            # assume links are in order; add sequence number
            trace_dict[match_type]["rownum"] = range(len(trace_dict[match_type]))
            matched_gdfs.append(trace_dict[match_type])

    logging.info(
        f"concatenate_matched_gdfs() for {match_type=} " f"with {len(matched_gdfs)=} matched_gdfs"
    )
    # not sure why this would happen -- return empty geodataframe
    if len(matched_gdfs) == 0:
        return gpd.GeoDataFrame()

    matched_gdf = pd.concat(matched_gdfs)
    logging.debug(f"concatenate_matched_gdfs() for {match_type}: matched_gdf:\n{matched_gdf}")
    logging.debug(f"matched_gdf.dtypes:\n{matched_gdf.dtypes}")

    # if values in the matched_gdf are lists, convert to strings
    for col in matched_gdf.columns:
        if not matched_gdf.dtypes[col] == object:
            continue
        logging.debug(f"Checking column {col} for for list type")

        is_list = matched_gdf[col].apply(lambda x: isinstance(x, list))
        if is_list.any():
            logging.debug(f"list elements:\n{matched_gdf.loc[is_list, :]}")
            # Convert to string if the column is a list or int
            matched_gdf[col] = matched_gdf[col].apply(
                lambda x: str(x) if isinstance(x, list) else str(x) if isinstance(x, int) else x
            )
    return matched_gdf


def write_matched_gdfs(match_result, gpkg_file_path, shapefile_dir=None):
    """Write traces matched with the LCSS matcher to a geopackage.

    Args:
        match_result (List): List of dictionaries with matched trace geodataframes.
        gpkg_gpkg_file_path (String): path to the geopackage file.
    """
    trace_gdf = concatenate_matched_gdfs(match_result, match_type="trace_gdf")
    trace_line_gdf = concatenate_matched_gdfs(match_result, match_type="trace_line_gdf")
    matched_gdf = concatenate_matched_gdfs(match_result, match_type="matched_gdf")
    matched_path_gdf = concatenate_matched_gdfs(match_result, match_type="matched_path_gdf")

    # rename to 10 characters
    #   '1234567890'              :'1234567890'
    SHORT_COL_NAMES = {
        "coordinate_id": "coord_id",
        "distance_to_road": "dist_to_rd",
        "origin_junction_id": "origjuncid",
        "destination_junction_id": "destjuncid",
        "travel_time": "traveltime",
        "collect_time": "collecttim",
    }

    # write the trace_gdf, trace_line_gdf, matched_gdf, and matched_path_gdf to a geopackage
    if len(trace_gdf) > 0:
        trace_gdf.to_file(gpkg_file_path, layer="trace_gdf", driver="GPKG")
        logging.info(
            f"Wrote {len(trace_gdf):,} rows to {gpkg_file_path} layer trace_gdf "
            f"with columns {trace_gdf.columns.to_list()}"
        )
        # also write shapefile if requested
        if shapefile_dir:
            trace_gdf.to_file(str(shapefile_dir / "trace.shp"))

    if len(trace_line_gdf) > 0:
        trace_line_gdf.to_file(gpkg_file_path, layer="trace_line_gdf", driver="GPKG")
        logging.info(
            f"Wrote {len(trace_line_gdf):,} rows to {gpkg_file_path} layer trace_line_gdf "
            f"with columns {trace_line_gdf.columns.to_list()}"
        )
        # also write shapefile if requested
        if shapefile_dir:
            trace_line_gdf.to_file(str(shapefile_dir / "trace_line.shp"))

    if len(matched_gdf) > 0:
        # convert matched_gdf and matched_path_gdf "road_id" column from RoadId data type to string
        matched_gdf["road_id"] = matched_gdf["road_id"].astype(str)
        matched_path_gdf["road_id"] = matched_path_gdf["road_id"].astype(str)

        matched_gdf.to_file(gpkg_file_path, layer="matched_gdf", driver="GPKG")
        logging.info(
            f"Wrote {len(matched_gdf):,} rows to {gpkg_file_path} layer matched_gdf "
            f"with columns {matched_gdf.columns.to_list()}"
        )
        # also write shapefile if requested
        # add (imprecise) size cutoff because shapefiles > 2GB aren't supported
        if shapefile_dir and (len(matched_gdf) < 1000000):
            # shorten column names if needed
            for col in SHORT_COL_NAMES.keys():
                if col in matched_gdf.columns.to_list():
                    matched_gdf.rename(columns={col: SHORT_COL_NAMES[col]}, inplace=True)
            logging.debug(f"matched_gdf columns shortened: {matched_gdf.columns.to_list()}")

            matched_gdf.to_file(str(shapefile_dir / "matched.shp"))

    if len(matched_path_gdf) > 0:
        matched_path_gdf.to_file(gpkg_file_path, layer="matched_path_gdf", driver="GPKG")
        logging.info(
            f"Wrote {len(matched_path_gdf):,} rows to {gpkg_file_path} layer matched_path_gdf "
            f"with columns {matched_path_gdf.columns.to_list()}"
        )
        # also write shapefile if requested
        # add (imprecise) size cutoff because shapefiles > 2GB aren't supported
        if shapefile_dir and (len(matched_path_gdf) < 1000000):
            # shorten column names if needed
            for col in SHORT_COL_NAMES.keys():
                if col in matched_path_gdf.columns.to_list():
                    matched_path_gdf.rename(columns={col: SHORT_COL_NAMES[col]}, inplace=True)
            logging.debug(
                f"matched_path_gdf columns shortened: {matched_path_gdf.columns.to_list()}"
            )
            matched_path_gdf.to_file(str(shapefile_dir / "matched_path.shp"))


def read_and_merge_data(location_path, trip_path):
    """Read location and trip data and merge them on trip_id

    Args:
        location_path (String): Path to the location csv file.
        trip_path (String): Path to the trip csv file.

    Returns:
        DataFrame: Merged DataFrame with location and trip data.
    """
    logging.info(f"read_and_merge_data(): Reading location data from {location_path}")
    location_df = pd.read_csv(
        location_path, date_format={"collect_time": "%Y-%m-%dT%H:%M:%SZ"}  # 2023-11-02T00:24:58Z
    )
    logging.info(f"read_and_merge_data(): Reading trip data from {trip_path}")
    trip_df = pd.read_csv(trip_path)
    trip_locations = pd.merge(
        location_df,
        trip_df[
            [
                "trip_id",
                "o_in_region",
                "d_in_region",
                "mode_type",
                "mode_1",
                "mode_2",
                "mode_3",
                "mode_4",
            ]
        ],
        on="trip_id",
    )
    return trip_locations


def filter_trips(trip_locations):
    """Filter trips to include only the following modes:
    5. Taxi
    6. TNC
    8. Car
    9. Carshare
    11. Shuttle/vanpool

    Args:
        trip_locations (DataFrame): DataFrame with location and trip data.

    Returns:
        DataFrame: Filtered DataFrame with trips that meet the criteria.
    """
    # car trips AND (trip starts OR ends in region)
    car_trips = trip_locations[
        ((trip_locations["mode_type"].isin([5, 6, 8, 9, 11])))
        & ((trip_locations["o_in_region"] == 1) | (trip_locations["d_in_region"] == 1))
    ]
    return car_trips


def flag_trips_by_osmid(matched_traces, osmid_set):
    """
    For each trip, check if any link in matched_path_gdf contains an osmid in osmid_set.
    Returns a list of flagged trip_ids.

    Args:
        matched_traces (list): List of dicts, each with a 'trip_id' and 'matched_path_gdf'.
        osmid_set (set): Set of osmids to flag.

    Returns:
        list: List of trip_ids where any link in matched_path_gdf contains an osmid in osmid_set.
    """
    # Ensure osmid is always a list for each row
    matched_traces = matched_traces.copy()
    matched_traces["osmid"] = matched_traces["osmid"].apply(lambda x: x if isinstance(x, (list, set, tuple)) else [x])
    # Explode so each osmid is its own row
    exploded = matched_traces.explode("osmid")
    # Find trip_ids where osmid is in osmid_set
    flagged_trip_ids = exploded.loc[exploded["osmid"].isin(osmid_set), "trip_id"].unique().tolist()
    return flagged_trip_ids

def _match_line_to_osm(line, edges_df, edge_sindex):
    """
    Helper function to match a single line to the nearest OSM edge.
    """
    bounds = line.bounds
    candidate_idxs = list(edge_sindex.intersection(bounds))
    if not candidate_idxs:
        return None

    candidates = edges_df.iloc[candidate_idxs]
    distances = candidates.geometry.distance(line)
    nearest_idx = distances.idxmin()
    return candidates.loc[nearest_idx].to_dict()

def conflate_with_osm(shapefile, osm_filter = None,buffer_dist=0.0001, max_workers=4):
    """
    Match each input line to the nearest OSM 'drive' network edge using multiprocessing. Input is a shapefile of LineString geometries.
    Pre-process the input to include the select link geometires you want for the analysis. E.G. express lanes.

    Parameters:
    - shapefile_path: path to the input shapefile of LineString geometries
    - buffer_dist: buffer distance (in meters) around shapefile area for OSM network
    - max_workers: number of processes to use for parallel processing

    Returns:
    - GeoDataFrame of matched OSM edges
    """

    # Load and reproject input geometries
    input_gdf = shapefile#gpd.read_file(shapefile_path).to_crs(epsg=4326)

    # Step 2: Create a polygon around the input lines
    polygon = unary_union(input_gdf.geometry.buffer(buffer_dist)).convex_hull

    # Step 3: Download OSM street network for the area
    G = ox.graph_from_polygon(polygon, network_type='drive')
    edges = ox.graph_to_gdfs(G, nodes=False).to_crs(epsg=3857)
    # Filter edges based on osm_filter if provided
    if osm_filter:
        edges = edges[edges['highway'].isin(osm_filter)]
    edge_sindex = edges.sindex

    # Project input lines to metric CRS
    input_gdf = input_gdf.to_crs(epsg=3857)
    input_lines = input_gdf.geometry

    # Multiprocessing matching
    matched = []
    with ProcessPoolExecutor(max_workers=max_workers) as executor:
        futures = [executor.submit(_match_line_to_osm, line, edges, edge_sindex) for line in input_lines]

        for future in as_completed(futures):
            result = future.result()
            if result is not None:
                matched.append(result)

    # Create result GeoDataFrame
    if not matched:
        print("No matches found.")
        return gpd.GeoDataFrame(columns=edges.columns, crs=edges.crs)

    result_df = pd.DataFrame(matched)
    print(result_df.columns)
    result_gdf = gpd.GeoDataFrame(result_df, geometry='geometry', crs=edges.crs)

    return result_gdf.reset_index(drop=True)



def main(script_args):
    """Main function to process trip data and write matched traces to a geopackage.

    Args:
        script_args: argparse.parse_args() return
    Returns:
        None
    """
    global process_regional_nx_map

    # since we're logging to file, we can be verbose
    pd.options.display.width = 500
    pd.options.display.max_columns = 100

    NETWORK_TYPE = NetworkType.DRIVE
    output_dir = config.out_file_path
    gpkg_file_path = config.gpkg_path

    # if test mode, use local dir for output instead of config.out_file_path
    if script_args.test:
        output_dir = pathlib.Path.cwd() / "output"
        output_dir.mkdir(exist_ok=True)
        gpkg_file_path = output_dir / "tds_conflation_results.gpkg"

    # ================= Create logger =================
    # put arguments into log name to make it easier to inspect differences
    log_file_full_path = (pathlib.Path.cwd() if script_args.test else config.out_file_path) / (
        "trip-trace-conflation"
        + f"_n{script_args.num_trip_ids if script_args.num_trip_ids else 'all'}"
        + f"_p{script_args.processes}"
        + f"_b{script_args.geofence_buffer}.log"
    )
    print(f"Writing to log file {log_file_full_path}")

    logger = logging.getLogger()
    logger.handlers.clear()
    logger.setLevel(logging.DEBUG)
    # console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(process)d - %(levelname)s - %(message)s",
            datefmt="%m/%d/%Y %I:%M:%S %p",
        )
    )
    logger.addHandler(ch)
    # file handler
    fh = logging.FileHandler(log_file_full_path, mode="w")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(
        logging.Formatter(
            "%(asctime)s - %(process)d - %(levelname)s - %(message)s",
            datefmt="%m/%d/%Y %I:%M:%S %p",
        )
    )
    logger.addHandler(fh)

    # Set up the log listener.
    # The log_listener will loop forever (in its own thread), handling log
    # messages as they arrive on the log_queue. See the top-level docstring for
    # more detail on this.
    # Note that we do not need to actually get a Logger object to run this!
    log_queue = multiprocessing.Manager().Queue(-1)
    log_listener = QueueListener(log_queue, fh)

    # Start a background thread that listens for and handles log messages.
    log_listener.start()

    logging.info(f"{args=}")

    # read and merge location and trip data
    trip_locations = read_and_merge_data(config.location_path, config.trip_path)

    # filter trips
    logging.info("Filtering trips...")
    # get unique trip ids
    car_trips = filter_trips(trip_locations)
    unique_ids = car_trips["trip_id"].unique()
    logging.info(f"Number of unique trip ids: {len(unique_ids):,}")
    logging.debug(f"car_trips.head()\n{car_trips.head()}")
    #          trip_id          collect_time  accuracy  bearing  speed       lat        lon  o_in_region  d_in_region  mode_type  mode_1  mode_2  mode_3  mode_4
    # 0  2333407402022  2023-11-02T00:23:43Z      13.0    120.0    4.0  37.85270 -122.21255            1            1          2       2     995     995     995
    # 1  2333407402022  2023-11-02T00:23:50Z       8.0    175.0    4.0  37.85227 -122.21236            1            1          2       2     995     995     995
    # 2  2333407402022  2023-11-02T00:24:04Z      12.0    185.0    4.0  37.85163 -122.21239            1            1          2       2     995     995     995
    # 3  2333407402022  2023-11-02T00:24:23Z       8.0    129.0    4.0  37.85092 -122.21197            1            1          2       2     995     995     995
    # 4  2333407402022  2023-11-02T00:24:49Z      11.0     73.0    4.0  37.85138 -122.21071            1            1          2       2     995     995     995

    # for processes > 1, this is initialized in init_worker
    if (script_args.processes == 1) and (script_args.download_local_OSM_map == False):
        now = datetime.now()
        logging.info("use_regional_nx_map: Creating networkx map from geojson...")
        logging.info(f"{config.region_boundary_path=}")
        logging.info(f"{config.local_network_path=}")
        process_regional_nx_map = nx_map_from_geojson(
            config.region_boundary_path, config.local_network_path, NETWORK_TYPE
        )
        later = datetime.now()
        logging.info(f"use_regional_nx_map: Creating networkx map took: {later - now}")

    # create a batch of traces
    logging.info("Creating batch traces...")

    if script_args.num_trip_ids:
        # test with a subset of trip ids
        unique_ids = unique_ids[: script_args.num_trip_ids]

    car_trips = car_trips[car_trips["trip_id"].isin(unique_ids)]
    batch_traces = create_batch_traces(car_trips, "trip_id")

    now = datetime.now()
    # process traces in parallel
    logging.info("Processing traces in parallel...")
    matched_traces = batch_process_traces_parallel(
        log_queue,
        batch_traces,
        processes=script_args.processes,
        download_local_OSM_map=script_args.download_local_OSM_map,
        region_boundary_path=config.region_boundary_path,
        local_network_path=config.local_network_path,
        network_type=NETWORK_TYPE,
        geofence_buffer=script_args.geofence_buffer,
    )
    later = datetime.now()
    logging.info(f"Multiprocessing took: {later - now}")

    log_listener.stop()

    # write the matched gdfs to a geopackage
    logging.info("Writing matched gdfs to geopackage...")
    write_matched_gdfs(matched_traces, gpkg_file_path, output_dir)

    # # delete the cache directory
    # cache_dir = "cache"
    # logging.info(f"Deleting cache directory at {cache_dir}...")
    # shutil.rmtree(cache_dir)
    # logging.info("Cache directory deleted.")

    logging.info("Processing complete.")
    return matched_traces

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=USAGE, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--test", help="Run in test mode: output locally instead of to box", action="store_true"
    )
    parser.add_argument("--num_trip_ids", help="Run with a subset of trip ids", type=int)
    parser.add_argument("--processes", help="Number of processes to use", type=int, default=8)
    # note that this will get throttled so only use for small numbers of traces
    parser.add_argument(
        "--download_local_OSM_map",
        help="Download a local OSM map for each trace. Otherwise, uses a single NxMap instance for the region",
        action="store_true",
    )
    parser.add_argument(
        "--geofence_buffer", help="Buffer around trace to use", type=int, default=1000
    )
    args = parser.parse_args()
    #nx_map_from_geojson(local_network_path=config.local_network_path, geojson_path=config.region_boundary_path, network_type=NetworkType.DRIVE)
    # # Uncomment this to run the select link matching
    
    select_link = gpd.read_file(config.select_link_shape)
    select_link = select_link.to_crs("EPSG:4326")

    osm_select = conflate_with_osm(
        shapefile=select_link, osm_filter=['motorway'],
        max_workers=8
    )
    osm_select.to_file(
        config.out_file_path / "select_link_matched.shp", driver="ESRI Shapefile"
    )
    # osm_select = gpd.read_file(config.out_file_path / "select_link_matched.shp")
    main(script_args=args)
    matched_traces = gpd.read_file(config.gpkg_path, layer="matched_gdf")
    select_trips = flag_trips_by_osmid(matched_traces, osm_select['osmid'].to_list())
    select_trips = pd.DataFrame(select_trips, columns=['trip_id']).to_csv(
        config.out_file_path / "select_trips.csv", index=False
    )
