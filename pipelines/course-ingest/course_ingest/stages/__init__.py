"""The ten pipeline stages, one module each.

    s01_ingest      read the seed spec, resolve targets and bounding boxes
    s02_route       route bike and run along real ways to the required distance
    s03_swim        draw the swim leg as a buoy course in real water
    s04_clean       dedupe, drop outliers, close loops, split into three legs
    s05_mapmatch    snap bike and run onto road geometry
    s06_resample    re-space to ~10 m nodes
    s07_elevation   sample the DEM for every node
    s08_segments    per-node gradient, per-segment climb, named segments
    s09_furniture   aid stations, transitions, special needs, markers, barriers
    s10_emit        validate and emit the bundle plus a clipped terrain extract
"""
