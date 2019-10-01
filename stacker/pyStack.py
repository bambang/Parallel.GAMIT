
import numpy as np
import dbConnection
from pyDate import Date
from tqdm import tqdm
from Utils import lg2ct
from Utils import ct2lg
from pyETM import pi
import pyETM
from datetime import datetime
import json


def adjust_lsq(A, L, P=None):

    LIMIT = 2.5

    from scipy.stats import chi2

    cst_pass = False
    iteration = 0
    factor = 1
    So = 1
    dof = (A.shape[0] - A.shape[1])
    X1 = chi2.ppf(1 - 0.05 / 2, dof)
    X2 = chi2.ppf(0.05 / 2, dof)

    s = np.array([])
    v = np.array([])
    C = np.array([])

    if P is None:
        P = np.ones((A.shape[0]))

    while not cst_pass and iteration <= 10:

        W = np.sqrt(P)

        Aw = np.multiply(W[:, None], A)
        Lw = np.multiply(W, L)

        C = np.linalg.lstsq(Aw, Lw, rcond=-1)[0]

        v = np.dot(A, C) - L

        # unit variance
        So = np.sqrt(np.dot(v, np.multiply(P, v)) / dof)

        x = np.power(So, 2) * dof

        # obtain the overall uncertainty predicted by lsq
        factor = factor * So

        # calculate the normalized sigmas

        s = np.abs(np.divide(v, factor))

        if x < X2 or x > X1:
            # if it falls in here it's because it didn't pass the Chi2 test
            cst_pass = False

            # reweigh by Mike's method of equal weight until 2 sigma
            f = np.ones((v.shape[0],))

            sw = np.power(10, LIMIT - s[s > LIMIT])
            sw[sw < np.finfo(np.float).eps] = np.finfo(np.float).eps

            f[s > LIMIT] = sw

            P = np.square(np.divide(f, factor))
        else:
            cst_pass = True

        iteration += 1

    # some statistics
    SS = np.linalg.inv(np.dot(A.transpose(), np.multiply(P[:, None], A)))

    sigma = So * np.sqrt(np.diag(SS))

    # mark observations with sigma <= LIMIT
    index = s <= LIMIT

    return C, sigma, index, v, factor, P, iteration


def print_residuals(NetworkCode, StationCode, residuals, lat, lon, components=('N', 'E', 'U')):

    # check if sending NEU or XYZ
    if components[0] == 'X':
        cresiduals = ct2lg(residuals[0], residuals[1], residuals[2], lat, lon)
        ccomponent = ('N', 'E', 'U')
    else:
        cresiduals = lg2ct(residuals[0], residuals[1], residuals[2], lat, lon)
        ccomponent = ('X', 'Y', 'Z')

    r = ''
    for i, c in enumerate(components):
        r = r + '    %s: ' % c + ' '.join(['%8.4f' % np.multiply(k, 1000) for k in residuals[i]]) + \
            ' %s: ' % ccomponent[i] + ' '.join(['%8.4f' % np.multiply(k, 1000) for k in cresiduals[i]]) + '\n'

    tqdm.write(' -- %s.%s\n' % (NetworkCode, StationCode) + r)


class Stack(list):

    def __init__(self, cnn, project, redo=False, end_date=None):

        super(Stack, self).__init__()

        self.project = project
        self.cnn = cnn
        self.position_space = None
        self.velocity_space = None
        self.periodic_space = None
        self.transformations = []

        if end_date is None:
            end_date = Date(datetime=datetime.now())

        if redo:
            # if redoing the stack, ignore the contents of the stacks table
            print ' >> Redoing stack'

            self.cnn.query('DELETE FROM stacks WHERE "Project" = \'%s\'' % self.project)

            print ' >> Loading GAMIT solutions for project %s...' % project

            gamit_vertices = self.cnn.query_float(
                'SELECT "NetworkCode" || \'.\' || "StationCode", "X", "Y", "Z", "Year", "DOY", "FYear" '
                'FROM gamit_soln WHERE "Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i)'
                'ORDER BY "NetworkCode", "StationCode"' % (project, end_date.year, end_date.doy))

            self.gamit_vertices = np.array(gamit_vertices, dtype=[('stn', 'S8'), ('x', 'float64'), ('y', 'float64'),
                                                                  ('z', 'float64'), ('yr', 'i4'), ('dd', 'i4'),
                                                                  ('fy', 'float64')])

            dates = self.cnn.query_float('SELECT "Year", "DOY" FROM gamit_soln WHERE "Project" = \'%s\' '
                                         'AND ("Year", "DOY") <= (%i, %i) '
                                         'GROUP BY "Year", "DOY" ORDER BY "Year", "DOY"'
                                         % (project, end_date.year, end_date.doy))

            self.dates = [Date(year=int(d[0]), doy=int(d[1])) for d in dates]

            self.stations = self.cnn.query_float('SELECT "NetworkCode", "StationCode" FROM gamit_soln '
                                                 'WHERE "Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i) '
                                                 'GROUP BY "NetworkCode", "StationCode" '
                                                 'ORDER BY "NetworkCode", "StationCode"'
                                                 % (project, end_date.year, end_date.doy), as_dict=True)

            for d in tqdm(self.dates, ncols=160, desc=' >> Initializing the stack polyhedrons'):
                self.append(Polyhedron(self.gamit_vertices, project, d))

        else:
            print ' >> Preserving the existing stack'
            print ' >> Determining differences between current stack and GAMIT solutions for project %s...' % project

            # load the vertices that don't have differences wrt to the GAMIT solution
            stack_vertices = self.cnn.query_float(
                'SELECT "NetworkCode" || \'.\' || "StationCode", "X", "Y", "Z", "Year", "DOY", "FYear" FROM stacks '
                'WHERE ("Year", "DOY") NOT IN ('
                ' SELECT "Year", "DOY" FROM ('
                ' SELECT "NetworkCode", "StationCode", "Year", "DOY", \'not in stack\' '
                '  AS note FROM gamit_soln WHERE "Project" = \'%s\' EXCEPT '
                ' SELECT "NetworkCode", "StationCode", "Year", "DOY", \'not in stack\' '
                '  AS note FROM stacks WHERE "Project" = \'%s\''
                ' ) AS missing_stack GROUP BY "Year", "DOY" ORDER BY "Year", "DOY") AND '
                '"Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i) '
                'ORDER BY "NetworkCode", "StationCode"' % (project, project, project, end_date.year, end_date.doy))

            print ' >> Loading pre-existing stack for project %s' % project

            # load the vertices that were different
            gamit_vertices = self.cnn.query_float(
                'SELECT "NetworkCode" || \'.\' || "StationCode", "X", "Y", "Z", "Year", "DOY", "FYear" FROM gamit_soln '
                'WHERE ("Year", "DOY") IN ('
                ' SELECT "Year", "DOY" FROM ('
                ' SELECT "NetworkCode", "StationCode", "Year", "DOY", \'not in stack\' '
                '  AS note FROM gamit_soln WHERE "Project" = \'%s\' EXCEPT '
                ' SELECT "NetworkCode", "StationCode", "Year", "DOY", \'not in stack\' '
                '  AS note FROM stacks WHERE "Project" = \'%s\''
                ' ) AS missing_stack GROUP BY "Year", "DOY" ORDER BY "Year", "DOY") AND '
                '"Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i) '
                'ORDER BY "NetworkCode", "StationCode"' % (project, project, project, end_date.year, end_date.doy))

            self.stack_vertices = np.array(stack_vertices, dtype=[('stn', 'S8'), ('x', 'float64'), ('y', 'float64'),
                                                                  ('z', 'float64'), ('yr', 'i4'), ('dd', 'i4'),
                                                                  ('fy', 'float64')])

            self.gamit_vertices = np.array(gamit_vertices, dtype=[('stn', 'S8'), ('x', 'float64'), ('y', 'float64'),
                                                                  ('z', 'float64'), ('yr', 'i4'), ('dd', 'i4'),
                                                                  ('fy', 'float64')])

            dates = self.cnn.query_float('SELECT "Year", "DOY" FROM stacks WHERE "Project" = \'%s\' '
                                         'AND ("Year", "DOY") <= (%i, %i) '
                                         'UNION '
                                         'SELECT "Year", "DOY" FROM gamit_soln WHERE "Project" = \'%s\' '
                                         'AND ("Year", "DOY") <= (%i, %i) '
                                         'ORDER BY "Year", "DOY"'
                                         % (project, end_date.year, end_date.doy, project, end_date.year, end_date.doy))

            self.dates = [Date(year=d[0], doy=d[1]) for d in dates]

            self.stations = self.cnn.query_float('SELECT "NetworkCode", "StationCode" FROM gamit_soln '
                                                 'WHERE "Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i) '
                                                 'UNION '
                                                 'SELECT "NetworkCode", "StationCode" FROM stacks '
                                                 'WHERE "Project" = \'%s\' AND ("Year", "DOY") <= (%i, %i) '
                                                 'ORDER BY "NetworkCode", "StationCode"'
                                                 % (project, end_date.year, end_date.doy,
                                                    project, end_date.year, end_date.doy), as_dict=True)

            for d in tqdm(self.dates, ncols=160, desc=' >> Initializing the stack polyhedrons'):
                try:
                    # try to append the stack vertices
                    self.append(Polyhedron(self.stack_vertices, project, d, aligned=True))

                except ValueError:
                    # if value error is risen, then append the gamit vertices
                    tqdm.write(' -- Appending %s from GAMIT solutions' % d.yyyyddd())
                    self.append(Polyhedron(self.gamit_vertices, project, d, aligned=False))

    def get_station(self, NetworkCode, StationCode):
        """
        Obtains the time series for a given station
        :param NetworkCode:
        :param StationCode:
        :return: a numpy array with the time series [x, y, z, yr, doy, fyear]
        """

        stnstr = NetworkCode + '.' + StationCode

        ts = []

        for poly in self:
            p = poly.vertices[poly.vertices['stn'] == stnstr]
            if p.size:
                ts.append([p['x'][0], p['y'][0], p['z'][0], p['yr'][0], p['dd'][0], p['fy'][0]])

        return np.array(ts)

    def calculate_etms(self):
        """
        Estimates the trajectory models for all stations in the stack
        :return:
        """
        print ' >> Calculating ETMs for %s...' % self.project

        for s in tqdm(self.stations, ncols=160, desc=self.project):

            ts = self.get_station(s['NetworkCode'], s['StationCode'])
            try:
                tqdm.postfix = s['NetworkCode'] + '.' + s['StationCode']

                ts = pyETM.GamitSoln(self.cnn, ts, s['NetworkCode'], s['StationCode'], self.project)

            except pyETM.pyETMException as e:
                tqdm.write(' -- ' + str(e))

    def remove_common_modes(self, target_periods=None):

        if target_periods is None:
            tqdm.write(' >> Removing periodic common modes...')

            # load all the periodic terms
            etm_objects = self.cnn.query_float('SELECT etmsv2."NetworkCode", etmsv2."StationCode", stations.lat, '
                                               'stations.lon, '
                                               'frequencies as freq, params FROM etmsv2 '
                                               'LEFT JOIN stations ON '
                                               'etmsv2."NetworkCode" = stations."NetworkCode" AND '
                                               'etmsv2."StationCode" = stations."StationCode" '
                                               'WHERE "object" = \'periodic\' AND soln = \'gamit\' '
                                               'AND frequencies <> \'{}\' '
                                               'ORDER BY etmsv2."NetworkCode", etmsv2."StationCode"', as_dict=True)
        else:
            use_stations = []
            for s in target_periods.keys():
                # check that the stations have not one or both periods with NaNs
                if not np.isnan(target_periods[s]['365.250']['n'][0]) and \
                        not np.isnan(target_periods[s]['182.625']['n'][0]):
                    use_stations.append(s)

            tqdm.write(' >> Inheriting periodic components...')

            # load the periodic terms of the stations that will produce the inheritance
            etm_objects = self.cnn.query_float('SELECT etmsv2."NetworkCode", etmsv2."StationCode", stations.lat, '
                                               'stations.lon, '
                                               'frequencies as freq, params FROM etmsv2 '
                                               'LEFT JOIN stations ON '
                                               'etmsv2."NetworkCode" = stations."NetworkCode" AND '
                                               'etmsv2."StationCode" = stations."StationCode" '
                                               'WHERE "object" = \'periodic\' AND soln = \'gamit\' '
                                               'AND frequencies <> \'{}\' AND etmsv2."NetworkCode" || \'.\' || '
                                               'etmsv2."StationCode" IN (\'%s\') '
                                               'ORDER BY etmsv2."NetworkCode", etmsv2."StationCode"'
                                               % '\', \''.join(use_stations), as_dict=True)

        # load the frequencies to subtract
        frequencies = self.cnn.query_float('SELECT frequencies FROM etmsv2 WHERE soln = \'gamit\' AND '
                                           'object = \'periodic\' '
                                           'AND frequencies <> \'{}\' GROUP BY frequencies', as_dict=True)

        # get the unique list of frequencies
        f_vector = []

        for freq in frequencies:
            f_vector += [f for f in freq['frequencies']]

        f_vector = np.array(list(set(f_vector)))

        # initialize the vectors
        ox = np.zeros((len(f_vector), len(etm_objects), 2))
        oy = np.zeros((len(f_vector), len(etm_objects), 2))
        oz = np.zeros((len(f_vector), len(etm_objects), 2))

        tqdm.write(' -- Reporting periodic residuals (in mm) before %s'
                   % ('inheritance' if target_periods else 'common mode removal'))

        for s, p in enumerate(etm_objects):

            stn_ts = self.get_station(p['NetworkCode'], p['StationCode'])

            self.cnn.query('DELETE FROM etmsv2 WHERE "soln" = \'gamit\' AND "NetworkCode" = \'%s\' AND '
                           '"StationCode" = \'%s\'' % (p['NetworkCode'], p['StationCode']))
            # save the time series
            ts = pyETM.GamitSoln(self.cnn, stn_ts, p['NetworkCode'], p['StationCode'], self.project)
            # create the ETM object
            pyETM.GamitETM(self.cnn, p['NetworkCode'], p['StationCode'], False, False, ts)

            q = self.cnn.query_float('SELECT frequencies as freq, * FROM etmsv2 '
                                     'WHERE "object" = \'periodic\' AND soln = \'gamit\' '
                                     'AND "NetworkCode" = \'%s\' AND '
                                     '"StationCode" = \'%s\' '
                                     % (p['NetworkCode'], p['StationCode']), as_dict=True)[0]

            if target_periods:
                n = []
                e = []
                u = []
                # inheritance invoked! we want to remove the difference between current periodic terms and target
                # terms from the parent frame
                for k in range(2):
                    for f in q['freq']:
                        t = target_periods['%s.%s' % (p['NetworkCode'], p['StationCode'])]['%.3f' % (1 / f)]
                        n += [t['n'][k]]
                        e += [t['e'][k]]
                        u += [t['u'][k]]

                params = np.array(q['params']) - np.array([n, e, u]).flatten()
            else:
                # no inheritance: make a vector of current periodic terms to be removed as common modes
                params = np.array(q['params'])

            params = params.reshape((3, params.shape[0] / 3))
            param_count = params.shape[1] / 2

            print_residuals(p['NetworkCode'], p['StationCode'], params, p['lat'], p['lon'])

            # convert from NEU to XYZ
            for j in range(params.shape[1]):
                params[:, j] = np.array(lg2ct(params[0, j], params[1, j], params[2, j],
                                              p['lat'], p['lon'])).flatten()

            for i, f in enumerate(p['freq']):
                ox[f_vector == f, s] = params[0, i:i + param_count + 1:param_count]
                oy[f_vector == f, s] = params[1, i:i + param_count + 1:param_count]
                oz[f_vector == f, s] = params[2, i:i + param_count + 1:param_count]

        # build the design matrix using the stations involved in inheritance or all stations if no inheritance
        sql_where = ','.join(["'" + stn['NetworkCode'] + '.' + stn['StationCode'] + "'" for stn in etm_objects])

        x = self.cnn.query_float('SELECT 0, -auto_z*1e-9, auto_y*1e-9, 1, 0, 0, auto_x*1e-9 FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % sql_where)

        y = self.cnn.query_float('SELECT auto_z*1e-9, 0, -auto_x*1e-9, 0, 1, 0, auto_y*1e-9 FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % sql_where)

        z = self.cnn.query_float('SELECT -auto_y*1e-9, auto_x*1e-9, 0, 0, 0, 1, auto_z*1e-9 FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % sql_where)
        Ax = np.array(x)
        Ay = np.array(y)
        Az = np.array(z)

        A = np.row_stack((Ax, Ay, Az))

        # loop through the frequencies
        for freq in f_vector:
            for i, cs in enumerate((np.sin, np.cos)):
                L = np.row_stack((ox[f_vector == freq, :, i].flatten(),
                                  oy[f_vector == freq, :, i].flatten(),
                                  oz[f_vector == freq, :, i].flatten())).flatten()

                c = np.linalg.lstsq(A, L, rcond=-1)[0]

                tqdm.write(' -- Transformation for %s(2 * pi * 1/%.2f) : %s' % (cs.__name__, np.divide(1., freq),
                                                                                ' '.join(['%7.4f' % cc for cc in c])))

                # loop through all the polyhedrons
                for poly in tqdm(self, ncols=160, desc=' -- Applying transformation -> %s(2 * pi * 1/%.2f)' %
                                                       (cs.__name__, np.divide(1., freq))):

                    # subtract the inverted common modes
                    poly.vertices['x'] = poly.vertices['x'] - cs(2 * pi * freq * 365.25 * poly.date.fyear) * \
                                         np.dot(poly.ax(scale=True), c)
                    poly.vertices['y'] = poly.vertices['y'] - cs(2 * pi * freq * 365.25 * poly.date.fyear) * \
                                         np.dot(poly.ay(scale=True), c)
                    poly.vertices['z'] = poly.vertices['z'] - cs(2 * pi * freq * 365.25 * poly.date.fyear) * \
                                         np.dot(poly.az(scale=True), c)

        if target_periods:

            tqdm.write(' -- Reporting periodic residuals (in mm) after inheritance\n'
                       '       365.25  182.62  365.25  182.62  \n'
                       '       sin     sin     cos     cos       ')

            for s, p in enumerate(etm_objects):
                # redo the etm for this station
                stn_ts = self.get_station(p['NetworkCode'], p['StationCode'])

                self.cnn.query('DELETE FROM etmsv2 WHERE "soln" = \'gamit\' AND "NetworkCode" = \'%s\' AND '
                               '"StationCode" = \'%s\'' % (p['NetworkCode'], p['StationCode']))
                # save the time series
                ts = pyETM.GamitSoln(self.cnn, stn_ts, p['NetworkCode'], p['StationCode'], self.project)
                # create the ETM object
                pyETM.GamitETM(self.cnn, p['NetworkCode'], p['StationCode'], False, False, ts)

                # obtain the updated parameters
                # they should exist for sure!
                q = self.cnn.query_float('SELECT frequencies as freq, * FROM etmsv2 '
                                         'WHERE "object" = \'periodic\' AND soln = \'gamit\' '
                                         'AND "NetworkCode" = \'%s\' AND '
                                         '"StationCode" = \'%s\' '
                                         % (p['NetworkCode'], p['StationCode']), as_dict=True)[0]

                n = []
                e = []
                u = []
                # inheritance invoked! we want to remove the difference between current periodic terms and target
                # terms from the parent frame
                for k in range(2):
                    for f in q['freq']:
                        t = target_periods['%s.%s' % (p['NetworkCode'], p['StationCode'])]['%.3f' % (1 / f)]
                        n += [t['n'][k]]
                        e += [t['e'][k]]
                        u += [t['u'][k]]

                residuals = (np.array(q['params']) - np.array([n, e, u]).flatten())
                # reshape the array to NEU
                residuals = residuals.reshape((3, residuals.shape[0] / 3))

                print_residuals(p['NetworkCode'], p['StationCode'], residuals, p['lat'], p['lon'])

        tqdm.write(' -- Done!')

    def align_spaces(self, target_dict):

        # get the list of stations to use during the alignment
        use_stations = target_dict.keys()

        # reference date used to align the stack
        # epochs SHOULD all be the same. Get first item and then the epoch
        ref_date = Date(fyear=target_dict.values()[0]['epoch'])

        # convert the target dict to a list
        target_list = []

        tqdm.write(' >> Aligning coordinate space...')
        for stn in use_stations:
            if not np.isnan(target_dict[stn]['x']):
                target_list.append((stn, target_dict[stn]['x'], target_dict[stn]['y'], target_dict[stn]['z'],
                                    ref_date.year, ref_date.doy, ref_date.fyear))

        # build a target polyhedron from the target_list
        vertices = np.array(target_list, dtype=[('stn', 'S8'), ('x', 'float64'), ('y', 'float64'),
                                                ('z', 'float64'), ('yr', 'i4'), ('dd', 'i4'),
                                                ('fy', 'float64')])

        target = Polyhedron(vertices, 'target_frame', ref_date)

        # start aligning the coordinates
        tqdm.write(' -- Aligning polyhedron at %.3f (%s)' % (ref_date.fyear, ref_date.yyyyddd()))

        wkpol = []
        for poly in self:
            if poly.date.gpsWeek == ref_date.gpsWeek:
                # get the polyhedrons of the week we are working on
                wkpol.append(poly)

        if not wkpol:
            raise Exception('Could not find polyhedrons for reference epoch week that includes %.3f (%s)'
                            % (ref_date.fyear, ref_date.yyyyddd()))

        scale = False
        comb = Combination(wkpol)
        # align the polyhedron to the target
        r_before, r_after, a_stn = comb.align(target, scale=scale, verbose=True)
        # extract the Helmert parameters to apply to the rest of the polyhedrons
        # remove the scale factor
        helmert = comb.helmert

        tqdm.write(' -- Reporting coordinate space residuals (in mm) before and after frame alignment\n'
                   '         Before   After |     Before   After  ')
        # format r_before and r_after to satisfy the required print_residuals format
        r_before = r_before.reshape(3, r_before.shape[0] / 3).transpose()
        r_after = r_after.reshape(3, r_after.shape[0] / 3).transpose()

        residuals = np.stack((r_before, r_after), axis=2)

        stn_lla = []
        for i, stn in enumerate(a_stn):
            n = stn.split('.')[0]
            s = stn.split('.')[1]
            # get the lat lon of the station to report back in the json
            lla = self.cnn.query_float('SELECT lat, lon FROM stations WHERE "NetworkCode" = \'%s\' '
                                       'AND "StationCode" = \'%s\'' % (n, s))[0]
            stn_lla.append([lla[0], lla[1]])
            # print residuals to screen
            print_residuals(n, s, residuals[i], lla[0], lla[1], ['X', 'Y', 'Z'])

        # save the position space residuals
        self.position_space = {'stations': {'codes': a_stn.tolist(), 'latlon': stn_lla},
                               'residuals_before_alignment': r_before.tolist(),
                               'residuals_after_alignment': r_after.tolist(),
                               'reference_date': ref_date,
                               'helmert_transformation': comb.helmert.tolist(),
                               'comments': 'No scale factor estimated.'}

        for poly in tqdm(self, ncols=160, desc=' -- Applying coordinate space transformation'):
            if poly.date != ref_date:
                poly.align(helmert=helmert, scale=scale)

        tqdm.write(' >> Aligning velocity space...')

        # choose the stations that have a velocity
        use_stn = []
        for stn in use_stations:
            if not np.isnan(target_dict[stn]['vx']):
                use_stn.append(stn)

        # load the polynomial terms of the stations
        etm_objects = self.cnn.query_float('SELECT etmsv2."NetworkCode", etmsv2."StationCode", stations.lat, '
                                           'stations.lon, params FROM etmsv2 '
                                           'LEFT JOIN stations ON '
                                           'etmsv2."NetworkCode" = stations."NetworkCode" AND '
                                           'etmsv2."StationCode" = stations."StationCode" '
                                           'WHERE "object" = \'polynomial\' AND soln = \'gamit\' '
                                           'AND etmsv2."NetworkCode" || \'.\' || etmsv2."StationCode" IN (\'%s\') '
                                           'ORDER BY etmsv2."NetworkCode", etmsv2."StationCode"'
                                           % '\', \''.join(use_stn), as_dict=True)

        # first, align the velocity space by finding a Helmert transformation that takes vx, vy, and vz of the stack at
        # each station and makes it equal to vx, vy, and vz of the ITRF structure

        dvx = np.zeros(len(etm_objects))
        dvy = np.zeros(len(etm_objects))
        dvz = np.zeros(len(etm_objects))

        for s, p in enumerate(etm_objects):
            stn_ts = self.get_station(p['NetworkCode'], p['StationCode'])

            self.cnn.query('DELETE FROM etmsv2 WHERE "soln" = \'gamit\' AND "NetworkCode" = \'%s\' AND '
                           '"StationCode" = \'%s\'' % (p['NetworkCode'], p['StationCode']))
            # save the time series
            ts = pyETM.GamitSoln(self.cnn, stn_ts, p['NetworkCode'], p['StationCode'], self.project)
            # create the ETM object
            pyETM.GamitETM(self.cnn, p['NetworkCode'], p['StationCode'], False, False, ts)

            q = self.cnn.query_float('SELECT params FROM etmsv2 '
                                     'WHERE "object" = \'polynomial\' AND soln = \'gamit\' '
                                     'AND "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' '
                                     % (p['NetworkCode'], p['StationCode']), as_dict=True)[0]

            params = np.array(q['params'])
            params = params.reshape((3, params.shape[0] / 3))
            # first item, i.e. params[:][0] in array is position
            # second item is velocity, which is what we are interested in
            v = np.array(lg2ct(params[0, 1], params[1, 1], params[2, 1], p['lat'], p['lon'])).flatten()
            # put the residuals in an array
            td = target_dict['%s.%s' % (p['NetworkCode'], p['StationCode'])]
            dvx[s] = v[0] - np.array(td['vx'])
            dvy[s] = v[1] - np.array(td['vy'])
            dvz[s] = v[2] - np.array(td['vz'])

        scale = False
        A = self.build_design(etm_objects, scale=scale)

        # loop through the frequencies
        L = np.row_stack((dvx.flatten(), dvy.flatten(), dvz.flatten())).flatten()

        c, _, _, _, wrms, _, it = adjust_lsq(A, L)
        # c = np.linalg.lstsq(A, L, rcond=-1)[0]

        tqdm.write(' -- Velocity space transformation:   ' + ' '.join(['%7.4f' % cc for cc in c]) +
                   ' wrms: %.3f it: %i' % (wrms * 1000, it))

        # loop through all the polyhedrons
        for poly in tqdm(self, ncols=160, desc=' -- Applying velocity space transformation'):
            t = np.repeat(poly.date.fyear - ref_date.fyear, poly.Ax.shape[0])

            poly.vertices['x'] = poly.vertices['x'] - t * np.dot(poly.ax(scale=scale), c)
            poly.vertices['y'] = poly.vertices['y'] - t * np.dot(poly.ay(scale=scale), c)
            poly.vertices['z'] = poly.vertices['z'] - t * np.dot(poly.az(scale=scale), c)

        tqdm.write(' -- Reporting velocity space residuals (in mm/yr) before and after frame alignment\n'
                   '         Before   After |     Before   After  ')

        dvxa = np.zeros(len(etm_objects))
        dvya = np.zeros(len(etm_objects))
        dvza = np.zeros(len(etm_objects))
        for s, p in enumerate(etm_objects):
            # redo the etm for this station
            stn_ts = self.get_station(p['NetworkCode'], p['StationCode'])

            self.cnn.query('DELETE FROM etmsv2 WHERE "soln" = \'gamit\' AND "NetworkCode" = \'%s\' AND '
                           '"StationCode" = \'%s\'' % (p['NetworkCode'], p['StationCode']))
            # save the time series
            ts = pyETM.GamitSoln(self.cnn, stn_ts, p['NetworkCode'], p['StationCode'], self.project)
            # create the ETM object
            pyETM.GamitETM(self.cnn, p['NetworkCode'], p['StationCode'], False, False, ts)

            q = self.cnn.query_float('SELECT params FROM etmsv2 '
                                     'WHERE "object" = \'polynomial\' AND soln = \'gamit\' '
                                     'AND "NetworkCode" = \'%s\' AND "StationCode" = \'%s\' '
                                     % (p['NetworkCode'], p['StationCode']), as_dict=True)[0]

            params = np.array(q['params'])
            params = params.reshape((3, params.shape[0] / 3))
            # first item, i.e. params[:][0] in array is position
            # second item is velocity, which is what we are interested in
            v = np.array(lg2ct(params[0, 1], params[1, 1], params[2, 1], p['lat'], p['lon'])).flatten()
            # put the residuals in an array
            td = target_dict['%s.%s' % (p['NetworkCode'], p['StationCode'])]
            dvxa[s] = v[0] - np.array(td['vx'])
            dvya[s] = v[1] - np.array(td['vy'])
            dvza[s] = v[2] - np.array(td['vz'])

            lla = self.cnn.query_float('SELECT lat, lon FROM stations WHERE "NetworkCode" = \'%s\' '
                                       'AND "StationCode" = \'%s\'' % (p['NetworkCode'], p['StationCode']))[0]

            print_residuals(p['NetworkCode'], p['StationCode'],
                            np.array([[dvx[s], dvxa[s]], [dvy[s], dvya[s]], [dvz[s], dvza[s]]]), lla[0], lla[1],
                            ['X', 'Y', 'Z'])

        # save the position space residuals
        self.velocity_space = {'stations': {'codes': [p['NetworkCode'] + '.' + p['StationCode'] for p in etm_objects],
                                            'latlon': [[p['lat'], p['lon']] for p in etm_objects]},
                               'residuals_before_alignment':
                                   np.row_stack((dvx.flatten(), dvy.flatten(), dvz.flatten())).tolist(),
                               'residuals_after_alignment':
                                   np.row_stack((dvxa.flatten(), dvya.flatten(), dvza.flatten())).tolist(),
                               'reference_date': ref_date,
                               'helmert_transformation': c.tolist(),
                               'comments': 'Velocity space transformation.'}

        tqdm.write(' -- Done!')

    def build_design(self, stations, scale=False):

        if scale:
            scale_x = ', auto_x*1e-9'
            scale_y = ', auto_y*1e-9'
            scale_z = ', auto_z*1e-9'
        else:
            scale_x = ''
            scale_y = ''
            scale_z = ''

        # build the design matrix using the stations involved in inheritance or all stations if no inheritance
        sql_where = ','.join(["'" + stn['NetworkCode'] + '.' + stn['StationCode'] + "'" for stn in stations])

        x = self.cnn.query_float('SELECT 0, -auto_z*1e-9, auto_y*1e-9, 1, 0, 0%s FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % (scale_x, sql_where))
        # x = [[0, -t[stn['NetworkCode'] + '.' + stn['StationCode']]['z']*1e-9, t[stn['NetworkCode'] + '.' +
        # stn['StationCode']]['y']*1e-9, 1, 0, 0] for stn in stations]
        y = self.cnn.query_float('SELECT auto_z*1e-9, 0, -auto_x*1e-9, 0, 1, 0%s FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % (scale_y, sql_where))
        # y = [[t[stn['NetworkCode'] + '.' + stn['StationCode']]['z']*1e-9, 0, -t[stn['NetworkCode'] + '.' +
        # stn['StationCode']]['x']*1e-9, 0, 1, 0] for stn in stations]
        z = self.cnn.query_float('SELECT -auto_y*1e-9, auto_x*1e-9, 0, 0, 0, 1%s FROM stations WHERE '
                                 '"NetworkCode" || \'.\' || "StationCode" '
                                 'IN (%s) ORDER BY "NetworkCode", "StationCode"' % (scale_z, sql_where))
        # z = [[-t[stn['NetworkCode'] + '.' + stn['StationCode']]['y']*1e-9, t[stn['NetworkCode'] + '.' +
        # stn['StationCode']]['x']*1e-9, 0, 0, 0, 1] for stn in stations]

        Ax = np.array(x)
        Ay = np.array(y)
        Az = np.array(z)

        return np.row_stack((Ax, Ay, Az))

    def save(self):
        """
        save the polyhedrons to the database
        :return: nothing
        """
        for poly in tqdm(self, ncols=160, desc='Saving ' + self.project):
            for vert in poly.vertices:
                try:
                    self.cnn.insert('stacks', {'Project': self.project,
                                               'NetworkCode': vert['stn'].split('.')[0],
                                               'StationCode': vert['stn'].split('.')[1],
                                               'X': vert['x'],
                                               'Y': vert['y'],
                                               'Z': vert['z'],
                                               'FYear': vert['fy'],
                                               'Year': vert['yr'],
                                               'DOY': vert['dd'],
                                               'sigmax': 0.000,
                                               'sigmay': 0.000,
                                               'sigmaz': 0.000})
                except dbConnection.dbErrInsert:
                    # the element already exists in the database (polyhedron already aligned)
                    pass

    def to_json(self, json_file):
        json_dump = dict()
        json_dump['position_space'] = self.position_space
        json_dump['velocity_space'] = self.velocity_space
        json_dump['transformations'] = self.transformations

        with open(json_file, 'w') as f:
            json.dump(json_dump, f, indent=4, sort_keys=False)


class Polyhedron(object):
    def __init__(self, vertices, project, date, rot=True, aligned=False):

        self.project = project
        self.date = date
        self.aligned = aligned
        self.helmert = None
        self.wrms = None
        self.stations_used = None
        self.iterations = None
        self.rot = rot
        # initialize the vertices of the polyhedron
        # self.vertices = [v for v in vertices if v[5] == date.year and v[6] == date.doy]

        self.vertices = vertices[np.logical_and(vertices['yr'] == date.year, vertices['dd'] == date.doy)]
        # sort using network code station code to make sure that intersect (in align) will get the data in the correct
        # order, otherwise the differences in X Y Z don't make sense...
        self.vertices.sort(order='stn')

        if not self.vertices.size:
            raise ValueError('No polyhedron data found for ' + str(date))

        self.rows = self.vertices.shape[0]

        # create the design matrix for this day
        rx = np.array([np.zeros(self.rows), -self.vertices['z'], self.vertices['y']]).transpose() * 1e-9
        ry = np.array([self.vertices['z'], np.zeros(self.rows), -self.vertices['x']]).transpose() * 1e-9
        rz = np.array([-self.vertices['y'], self.vertices['x'], np.zeros(self.rows)]).transpose() * 1e-9

        tx = np.array([np.ones(self.rows), np.zeros(self.rows), np.zeros(self.rows)]).transpose()
        ty = np.array([np.zeros(self.rows), np.ones(self.rows), np.zeros(self.rows)]).transpose()
        tz = np.array([np.zeros(self.rows), np.zeros(self.rows), np.ones(self.rows)]).transpose()

        if rot:
            self.Ax = np.concatenate((rx, tx), axis=1)
            self.Ay = np.concatenate((ry, ty), axis=1)
            self.Az = np.concatenate((rz, tz), axis=1)
        else:
            self.Ax = tx
            self.Ay = ty
            self.Az = tz

    def ax(self, scale=False):
        """
        function to append scale to the design matrix
        :return: Ax with scale
        """
        if scale:
            return np.concatenate((self.Ax, self.vertices['x'][np.newaxis].transpose() * 1e-9), axis=1)
        else:
            return self.Ax

    def ay(self, scale=False):
        """
        function to append scale to the design matrix
        :return: Ay with scale
        """
        if scale:
            return np.concatenate((self.Ay, self.vertices['y'][np.newaxis].transpose() * 1e-9), axis=1)
        else:
            return self.Ay

    def az(self, scale=False):
        """
        function to append scale to the design matrix
        :return: Az with scale
        """
        if scale:
            return np.concatenate((self.Az, self.vertices['z'][np.newaxis].transpose() * 1e-9), axis=1)
        else:
            return self.Az

    def align(self, target=None, set_aligned=True, helmert=None, scale=False, verbose=False):
        """
        Align to another polyhedron object using a Helmert transformation defined
        during the initialization of the object
        :param target: polyhedron object
        :param set_aligned: determine whether the polyhedron should be marked as aligned or not after performing the
        Helmert transformation
        :param helmert: provide an externally calculated helmert transformation
        :return: before and after residuals and list of stations
        """
        r = None
        r_after = None
        ft = None
        fl = None
        stations = None

        if target is not None:
            # figure out common stations
            intersect = np.intersect1d(target.vertices['stn'], self.vertices['stn'])

            # target filter
            ft = np.isin(target.vertices['stn'], intersect)
            # local filter
            fl = np.isin(self.vertices['stn'], intersect)
            # get vertices
            st = target.vertices[ft]
            sl = self.vertices[fl]

            # obtain residuals
            rx = st['x'] - sl['x']
            ry = st['y'] - sl['y']
            rz = st['z'] - sl['z']

            # get the design matrix portion
            Ax = self.ax(scale)[fl]
            Ay = self.ay(scale)[fl]
            Az = self.az(scale)[fl]

            A = np.concatenate((Ax, Ay, Az), axis=0)
            r = np.concatenate((rx, ry, rz), axis=0)

            # invert
            c, _, index, v, wrms, P, it = adjust_lsq(A, r)

            self.helmert = c
            self.wrms = wrms
            self.stations_used = len(intersect)
            self.iterations = it

            if verbose:
                xyzstn = ['X-%s' % ss for ss in st['stn']] + ['Y-%s' % ss for ss in st['stn']] + \
                         ['Z-%s' % ss for ss in st['stn']]

                tqdm.write(' -- T: %s iterations: %i wrms: %.1f stations used: %i\n'
                           '    Down-weighted station components: %s'
                           % (' '.join(['%7.4f' % cc for cc in self.helmert]), it, wrms * 1000, self.stations_used,
                              ' '.join(['%s' % ss for ss in np.array(xyzstn)[np.logical_not(index)]])))
        else:
            c = helmert
            if verbose:
                tqdm.write(' -- T: %s -> externally provided' % (' '.join(['%7.4f' % cc for cc in self.helmert])))

        # apply result to everyone
        x = np.dot(np.concatenate((self.ax(scale), self.ay(scale), self.az(scale)), axis=0),
                   c).reshape((3, self.rows)).transpose()

        self.vertices['x'] += x[:, 0]
        self.vertices['y'] += x[:, 1]
        self.vertices['z'] += x[:, 2]

        self.aligned = set_aligned

        # report back the residuals
        if ft is not None:
            st = target.vertices[ft]
            sl = self.vertices[fl]

            # obtain residuals
            rx = st['x'] - sl['x']
            ry = st['y'] - sl['y']
            rz = st['z'] - sl['z']

            r_after = np.concatenate((rx, ry, rz), axis=0)
            stations = self.vertices[fl]['stn']

        return r, r_after, stations

    def info(self):
        if self.helmert is None:
            self.helmert = np.array([])
            # for debugging
            # tqdm.write(' -- None found in helmert for polyhedron %s' % self.date.yyyyddd())

        return {'date': str(self.date), 'wrms': self.wrms, 'stations_used': self.stations_used,
                'iterations': self.iterations, 'helmert': self.helmert.tolist()}


class Combination(Polyhedron):
    def __init__(self, polyhedrons):

        # get the mean epoch
        date = [poly.date.mjd for poly in polyhedrons]
        date = Date(mjd=np.mean(date))

        # get the set of stations
        stn = []
        for poly in polyhedrons:
            stn += poly.vertices['stn'].tolist()

        stn = np.unique(stn)

        # average the coordinates for each station
        poly = []
        for s in stn:
            v = np.array([])
            for p in polyhedrons:
                if not v.size:
                    v = p.vertices[p.vertices['stn'] == s]
                else:
                    v = np.concatenate((v, p.vertices[p.vertices['stn'] == s]))

            poly.append((s, np.mean(v['x']), np.mean(v['y']), np.mean(v['z']), date.year, date.doy, date.fyear))

        pp = np.array(poly, dtype=[('stn', 'S8'), ('x', 'float64'), ('y', 'float64'), ('z', 'float64'),
                                   ('yr', 'i4'), ('dd', 'i4'), ('fy', 'float64')])

        super(Combination, self).__init__(pp, polyhedrons[0].project, date)


def main():

    cnn = dbConnection.Cnn("gnss_data.cfg")

    stack = Stack(cnn, 'igs-sirgas', redo=True)

    stack.calculate_etms()

    for i in tqdm(range(1, len(stack)), ncols=160):
        stack[i].align(stack[i - 1])

    net = 'igs'
    stn = 'braz'

    ts = stack.get_station(net, stn)

    dts = np.append(np.diff(ts[:, 0:3], axis=0), ts[1:, -3:], axis=1)

    ts = pyETM.GamitSoln(cnn, dts, net, stn, 'igs-sirgas')

    pyETM.GamitETM(cnn, net, stn, True, gamit_soln=ts)


if __name__ == '__main__':

    main()
