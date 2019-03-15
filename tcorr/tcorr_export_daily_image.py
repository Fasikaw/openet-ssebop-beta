#--------------------------------
# Name:         tcorr_export_daily_image.py
# Purpose:      Compute/Export daily Tcorr images
#--------------------------------

import argparse
from builtins import input
import datetime
import logging
import math
import os
import pprint
import sys

import ee

import openet.ssebop as ssebop
from . import utils


def main(ini_path=None, overwrite_flag=False, delay=0, key=None):
    """Compute daily Tcorr images

    Parameters
    ----------
    ini_path : str
        Input file path.
    overwrite_flag : bool, optional
        If True, overwrite existing files (the default is False).
    delay : float, optional
        Delay time between each export task (the default is 0).
    key : str, optional
        File path to an Earth Engine json key file (the default is None).

    """
    logging.info('\nCompute daily Tcorr images')

    ini = utils.read_ini(ini_path)

    model_name = 'SSEBOP'
    # model_name = ini['INPUTS']['et_model'].upper()

    if (ini[model_name]['tmax_source'].upper() == 'CIMIS' and
            ini['INPUTS']['end_date'] < '2003-10-01'):
        logging.error(
            '\nCIMIS is not currently available before 2003-10-01, exiting\n')
        sys.exit()
    elif (ini[model_name]['tmax_source'].upper() == 'DAYMET' and
            ini['INPUTS']['end_date'] > '2017-12-31'):
        logging.warning(
            '\nDAYMET is not currently available past 2017-12-31, '
            'using median Tmax values\n')
        # sys.exit()
    # elif (ini[model_name]['tmax_source'].upper() == 'TOPOWX' and
    #         ini['INPUTS']['end_date'] > '2017-12-31'):
    #     logging.warning(
    #         '\nDAYMET is not currently available past 2017-12-31, '
    #         'using median Tmax values\n')
    #     # sys.exit()

    logging.info('\nInitializing Earth Engine')
    if key:
        logging.info('  Using service account key file: {}'.format(key))
        # The "EE_ACCOUNT" parameter is not used if the key file is valid
        ee.Initialize(ee.ServiceAccountCredentials('deadbeef', key_file=key))
    else:
        ee.Initialize()

    # Output Tcorr daily image collection
    tcorr_daily_coll_id = '{}/{}_daily'.format(
        ini['EXPORT']['export_coll'], tmax_name.lower())

    # Get a Tmax image to set the Tcorr values to
    logging.debug('\nTmax properties')
    tmax_name = ini[model_name]['tmax_source']
    tmax_source = tmax_name.split('_', 1)[0]
    tmax_version = tmax_name.split('_', 1)[1]
    tmax_coll_id = 'projects/usgs-ssebop/tmax/{}'.format(tmax_name.lower())
    tmax_coll = ee.ImageCollection(tmax_coll_id)
    tmax_mask = ee.Image(tmax_coll.first()).select([0]).multiply(0)
    logging.debug('  Collection: {}'.format(tmax_coll_id))
    logging.debug('  Source: {}'.format(tmax_source))
    logging.debug('  Version: {}'.format(tmax_version))

    logging.debug('\nExport properties')
    export_geo = ee.Image(tmax_mask).projection().getInfo()['transform']
    export_crs = ee.Image(tmax_mask).projection().getInfo()['crs']
    export_shape = ee.Image(tmax_mask).getInfo()['bands'][0]['dimensions']
    export_extent = [
        export_geo[2], export_geo[5] + export_shape[1] * export_geo[4],
        export_geo[2] + export_shape[0] * export_geo[0], export_geo[5]]
    logging.debug('  CRS: {}'.format(export_crs))
    logging.debug('  Extent: {}'.format(export_extent))
    logging.debug('  Geo: {}'.format(export_geo))
    logging.debug('  Shape: {}'.format(export_shape))

    # # Limit export to a user defined study area or geometry?
    # export_geom = ee.Geometry.Rectangle(
    #     [-125, 24, -65, 50], proj='EPSG:4326', geodesic=False)  # CONUS
    # export_geom = ee.Geometry.Rectangle(
    #     [-124, 35, -119, 42], proj='EPSG:4326', geodesic=False)  # California

    # If cell_size parameter is set in the INI,
    # adjust the output cellsize and recompute the transform and shape
    try:
        export_cs = float(ini['EXPORT']['cell_size'])
        export_shape = [
            int(math.ceil(abs((export_shape[0] * export_geo[0]) / export_cs))),
            int(math.ceil(abs((export_shape[1] * export_geo[4]) / export_cs)))]
        export_geo = [export_cs, 0.0, export_geo[2], 0.0, -export_cs, export_geo[5]]
        logging.debug('  Custom export cell size: {}'.format(export_cs))
        logging.debug('  Geo: {}'.format(export_geo))
        logging.debug('  Shape: {}'.format(export_shape))
    except KeyError:
        pass

    # Get current asset list
    if ini['EXPORT']['export_dest'].upper() == 'ASSET':
        logging.debug('\nGetting asset list')
        # DEADBEEF - daily is hardcoded in the asset_id for now
        asset_list = utils.get_ee_assets(tcorr_daily_coll_id)
    else:
        raise ValueError('invalid export destination: {}'.format(
            ini['EXPORT']['export_dest']))

    # Get current running tasks
    tasks = utils.get_ee_tasks()
    if logging.getLogger().getEffectiveLevel() == logging.DEBUG:
        logging.debug('  Tasks: {}\n'.format(len(tasks)))
        input('ENTER')

    # Limit by year and month
    try:
        month_list = sorted(list(utils.parse_int_set(ini['TCORR']['months'])))
    except:
        logging.info('\nTCORR "months" parameter not set in the INI,'
                     '\n  Defaulting to all months (1-12)\n')
        month_list = list(range(1, 13))
    try:
        year_list = sorted(list(utils.parse_int_set(ini['TCORR']['years'])))
    except:
        logging.info('\nTCORR "years" parameter not set in the INI,'
                     '\n  Defaulting to all available years\n')
        year_list = []

    # Key is cycle day, value is a reference date on that cycle
    # Data from: https://landsat.usgs.gov/landsat_acq
    # I only need to use 8 cycle days because of 5/7 and 7/8 are offset
    cycle_dates = {
        7: '1970-01-01',
        8: '1970-01-02',
        1: '1970-01-03',
        2: '1970-01-04',
        3: '1970-01-05',
        4: '1970-01-06',
        5: '1970-01-07',
        6: '1970-01-08',
    }
    # cycle_dates = {
    #     1:  '2000-01-06',
    #     2:  '2000-01-07',
    #     3:  '2000-01-08',
    #     4:  '2000-01-09',
    #     5:  '2000-01-10',
    #     6:  '2000-01-11',
    #     7:  '2000-01-12',
    #     8:  '2000-01-13',
    #     # 9:  '2000-01-14',
    #     # 10: '2000-01-15',
    #     # 11: '2000-01-16',
    #     # 12: '2000-01-01',
    #     # 13: '2000-01-02',
    #     # 14: '2000-01-03',
    #     # 15: '2000-01-04',
    #     # 16: '2000-01-05',
    # }
    cycle_base_dt = datetime.datetime.strptime(cycle_dates[1], '%Y-%m-%d')

    iter_start_dt = datetime.datetime.strptime(
        ini['INPUTS']['start_date'], '%Y-%m-%d')
    iter_end_dt = datetime.datetime.strptime(
        ini['INPUTS']['end_date'], '%Y-%m-%d')

    # Iterate over date ranges
    for export_dt in utils.date_range(iter_start_dt, iter_end_dt):
        export_date = export_dt.strftime('%Y-%m-%d')
        if ((month_list and export_dt.month not in month_list) or
                ( year_list and export_dt.year not in year_list)):
            logging.debug('Date: {} - skipping'.format(export_date))
            continue
        logging.info('Date: {}'.format(export_date))

        if export_date >= datetime.datetime.today().strftime('%Y-%m-%d'):
            logging.info('  Unsupported date, skipping')
            continue
        elif export_date < '1984-03-23':
            logging.info('  No Landsat 5+ images before 1984-03-16, skipping')
            continue

        export_id = ini['EXPORT']['export_id_fmt'] \
            .format(
                product=tmax_name.lower(),
                date=export_dt.strftime('%Y%m%d'),
                export=ini['EXPORT']['export_dest'].lower())
        logging.debug('  Export ID: {}'.format(export_id))

        if ini['EXPORT']['export_dest'] == 'ASSET':
            # DEADBEEF - daily is hardcoded in the asset_id for now
            asset_id = '{}/{}'.format(
                tcorr_daily_coll_id, export_dt.strftime('%Y%m%d'))
            logging.debug('  Asset ID: {}'.format(asset_id))

        if overwrite_flag:
            if export_id in tasks.keys():
                logging.debug('  Task already submitted, cancelling')
                ee.data.cancelTask(tasks[export_id])
            # This is intentionally not an "elif" so that a task can be
            # cancelled and an existing image/file/asset can be removed
            if (ini['EXPORT']['export_dest'].upper() == 'ASSET' and
                    asset_id in asset_list):
                logging.debug('  Asset already exists, removing')
                ee.data.deleteAsset(asset_id)
        else:
            if export_id in tasks.keys():
                logging.debug('  Task already submitted, exiting')
                continue
            elif (ini['EXPORT']['export_dest'].upper() == 'ASSET' and
                    asset_id in asset_list):
                logging.debug('  Asset already exists, skipping')
                continue

        # Build and merge the Landsat collections
        # Time filters are to remove bad (L5) and pre-op (L8) images
        #     .filterBounds(export_geom) \
        l8_coll = ee.ImageCollection('LANDSAT/LC08/C01/T1_RT_TOA') \
            .filterDate(export_dt, export_dt + datetime.timedelta(days=1)) \
            .filterBounds(tmax_mask.geometry()) \
            .filterMetadata('CLOUD_COVER_LAND', 'less_than',
                            float(ini['INPUTS']['cloud_cover'])) \
            .filterMetadata('DATA_TYPE', 'equals', 'L1TP') \
            .filter(ee.Filter.gt('system:time_start',
                                 ee.Date('2013-03-24').millis()))
        l7_coll = ee.ImageCollection('LANDSAT/LE07/C01/T1_RT_TOA') \
            .filterDate(export_dt, export_dt + datetime.timedelta(days=1)) \
            .filterBounds(tmax_mask.geometry()) \
            .filterMetadata('CLOUD_COVER_LAND', 'less_than',
                            float(ini['INPUTS']['cloud_cover'])) \
            .filterMetadata('DATA_TYPE', 'equals', 'L1TP')
        l5_coll = ee.ImageCollection('LANDSAT/LT05/C01/T1_TOA') \
            .filterDate(export_dt, export_dt + datetime.timedelta(days=1)) \
            .filterBounds(tmax_mask.geometry()) \
            .filterMetadata('CLOUD_COVER_LAND', 'less_than',
                            float(ini['INPUTS']['cloud_cover'])) \
            .filterMetadata('DATA_TYPE', 'equals', 'L1TP') \
            .filter(ee.Filter.lt('system:time_start',
                                 ee.Date('2011-12-31').millis()))
        # l4_coll = ee.ImageCollection('LANDSAT/LT04/C01/T1_TOA') \
        #     .filterDate(export_dt, export_dt + datetime.timedelta(days=1)) \
        #     .filterBounds(tmax_img.geometry()) \
        #     .filterMetadata('CLOUD_COVER_LAND', 'less_than',
        #                     float(ini['INPUTS']['cloud_cover'])) \
        #     .filterMetadata('DATA_TYPE', 'equals', 'L1TP')

        # if export_date <= '1993-12-31':
        #     landsat_coll = ee.ImageCollection(l5_coll.merge(l4_coll))
        if export_date < '1999-01-01':
            landsat_coll = l5_coll
        elif export_date <= '2011-12-31':
            landsat_coll = ee.ImageCollection(l7_coll.merge(l5_coll))
        elif export_date <= '2013-03-24':
            landsat_coll = l7_coll
        else:
            landsat_coll = ee.ImageCollection(l8_coll.merge(l7_coll))
        # pprint.pprint(landsat_coll.aggregate_histogram('system:index').getInfo())
        # pprint.pprint(ee.Image(landsat_coll.first()).getInfo())
        # input('ENTER')

        def tcorr_img_func(image):
            t_stats = ssebop.Image.from_landsat_c1_toa(
                    ee.Image(image),
                    tdiff_threshold=float(ini[model_name]['tdiff_threshold'])) \
                .tcorr_stats
            t_stats = ee.Dictionary(t_stats) \
                .combine({'tcorr_p5': 0, 'tcorr_count': 0},
                         overwrite=False)
            # tcorr = ee.Algorithms.If(
            #     t_stats.get('tcorr_p5'), ee.Number(t_stats.get('tcorr_p5')), 0)
            tcorr = ee.Number(t_stats.get('tcorr_p5'))
            count = ee.Number(t_stats.get('tcorr_count'))

            # Remove the merged collection indices from the system:index
            scene_id = ee.List(
                ee.String(image.get('system:index')).split('_')).slice(-3)
            scene_id = ee.String(scene_id.get(0)).cat('_') \
                .cat(ee.String(scene_id.get(1))).cat('_') \
                .cat(ee.String(scene_id.get(2)))

            # return ee.Image([
            #         tmax_img.select([0], ['tcorr']).multiply(0) \
            #             .add(ee.Image.constant(tcorr)).float(),
            #         tmax_img.select([0], ['count']).multiply(0)
            #             .add(ee.Image.constant(count)).int()]) \
            return tmax_mask.add(tcorr) \
                .rename(['tcorr']) \
                .clip(image.geometry()) \
                .set({
                    'system:time_start': image.get('system:time_start'),
                    'scene_id': scene_id,
                    'wrs2_tile': scene_id.slice(5, 11),
                    'spacecraft_id': image.get('SPACECRAFT_ID'),
                    'tcorr': tcorr,
                    'count': count,
                })

        # # Test for one image
        # pprint.pprint(tcorr_img_func(ee.Image(landsat_coll \
        #     .filterMetadata('WRS_PATH', 'equals', 36) \
        #     .filterMetadata('WRS_ROW', 'equals', 33).first())).getInfo())
        # input('ENTER')

        tcorr_img_coll = ee.ImageCollection(landsat_coll.map(tcorr_img_func)) \
            .filterMetadata('count', 'not_less_than',
                            float(ini['TCORR']['min_pixel_count']))
        # pprint.pprint(tcorr_img_coll.aggregate_histogram('system:index').getInfo())
        # pprint.pprint(ee.Image(tcorr_img_coll.first()).getInfo())
        # input('ENTER')

        # There should not be more than two overlapping values on any day
        # If there are no Tcorr values, return an empty image
        tcorr_img = ee.Algorithms.If(
            tcorr_img_coll.size().gt(0),
            tcorr_img_coll.mean(),
            tmax_mask.updateMask(0))
        # pprint.pprint(tcorr_img.getInfo())
        # pprint.pprint(tcorr_img_coll.size().getInfo())
        # input('ENTER')

        # # This doesn't work (median is returning no bands for some dates)
        # tcorr_img = tmax_mask.add(tcorr_img_coll.median()) \
        #     .updateMask(0)

        def unique_properties(coll, property):
            return ee.String(ee.List(ee.Dictionary(
                coll.aggregate_histogram(property)).keys()).join(','))
        wrs2_tile_list = ee.String('').cat(unique_properties(
            tcorr_img_coll, 'wrs2_tile'))
        landsat_list = ee.String('').cat(unique_properties(
            tcorr_img_coll, 'spacecraft_id'))

        # # Is there a better way of building these strings?
        # wrs2_tile_list = ee.Algorithms.If(
        #     tcorr_img_coll.size().gt(0),
        #     ee.String(ee.List(ee.Dictionary(tcorr_img_coll \
        #         .aggregate_histogram('WRS2_TILE')).keys()).join(',')),
        #     ee.String(''))
        # landsat_list = ee.Algorithms.If(
        #     tcorr_img_coll.size().gt(0),
        #     ee.String(ee.List(ee.Dictionary(tcorr_img_coll\
        #         .aggregate_histogram('SPACECRAFT_ID')).keys()).join(',')),
        #     ee.String(''))

        # Cast to float and set properties
        tcorr_img = ee.Image(tcorr_img).rename(['tcorr']).double() \
            .set({
                'system:time_start': utils.millis(export_dt),
                'date_ingested': datetime.datetime.today().strftime('%Y-%m-%d'),
                'date': export_dt.strftime('%Y-%m-%d'),
                'year': int(export_dt.year),
                'month': int(export_dt.month),
                'day': int(export_dt.day),
                'doy': int(export_dt.strftime('%j')),
                'cycle_day': ((export_dt - cycle_base_dt).days % 8) + 1,
                'landsat': landsat_list,
                'model_name': model_name,
                'model_version': ssebop.__version__,
                'tmax_source': tmax_source.upper(),
                'tmax_version': tmax_version.upper(),
                'wrs2_tiles': wrs2_tile_list,
            })
        # pprint.pprint(tcorr_img.getInfo())
        # input('ENTER')

        # Build export tasks
        if ini['EXPORT']['export_dest'] == 'ASSET':
            logging.debug('  Building export task')
            task = ee.batch.Export.image.toAsset(
                image=ee.Image(tcorr_img),
                description=export_id,
                assetId=asset_id,
                crs=export_crs,
                crsTransform='[' + ','.join(list(map(str, export_geo))) + ']',
                dimensions='{0}x{1}'.format(*export_shape),
            )
            logging.debug('  Starting export task')
            utils.ee_task_start(task)

        # Pause before starting next task
        utils.delay_task(delay)
        logging.debug('')


def arg_parse():
    """"""
    parser = argparse.ArgumentParser(
        description='Compute/export daily Tcorr images',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument(
        '-i', '--ini', type=utils.arg_valid_file,
        help='Input file', metavar='FILE')
    parser.add_argument(
        '--delay', default=0, type=float,
        help='Delay (in seconds) between each export tasks')
    parser.add_argument(
        '--key', type=utils.arg_valid_file, metavar='FILE',
        help='JSON key file')
    parser.add_argument(
        '-o', '--overwrite', default=False, action='store_true',
        help='Force overwrite of existing files')
    parser.add_argument(
        '-d', '--debug', default=logging.INFO, const=logging.DEBUG,
        help='Debug level logging', action='store_const', dest='loglevel')
    args = parser.parse_args()

    # Prompt user to select an INI file if not set at command line
    # if not args.ini:
    #     args.ini = utils.get_ini_path(os.getcwd())

    return args


if __name__ == "__main__":
    args = arg_parse()

    logging.basicConfig(level=args.loglevel, format='%(message)s')
    logging.info('\n{0}'.format('#' * 80))
    logging.info('{0:<20s} {1}'.format(
        'Run Time Stamp:', datetime.datetime.now().isoformat(' ')))
    logging.info('{0:<20s} {1}'.format('Current Directory:', os.getcwd()))
    logging.info('{0:<20s} {1}'.format(
        'Script:', os.path.basename(sys.argv[0])))

    main(ini_path=args.ini, overwrite_flag=args.overwrite, delay=args.delay,
         key=args.key)
