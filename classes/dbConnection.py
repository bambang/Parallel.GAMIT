"""
Project: Parallel.Archive
Date: 02/16/2017
Author: Demian D. Gomez

This class is used to connect to the database and handles inserts, updates and selects
It also handles the error, info and warning messages
"""

import pg
import pgdb
import platform
import ConfigParser
import inspect
import re
from datetime import datetime
from decimal import Decimal


class dbErrInsert(Exception):
    pass


class dbErrUpdate(Exception):
    pass


class dbErrConnect(Exception):
    pass


class dbErrDelete(Exception):
    pass


class IntegrityError(pg.IntegrityError):
    pass


class Cnn(pg.DB):

    def __init__(self, configfile, use_float=False):

        # set casting of numeric to floats
        pg.set_typecast('Numeric', float)

        options = {'hostname': 'localhost',
           'username': 'postgres' ,
           'password': '' ,
           'database': 'gnss_data'}

        self.active_transaction = False
        self.options = options
        # parse session config file
        config = ConfigParser.ConfigParser()
        config.readfp(open(configfile))

        # get the database config
        for iconfig, val in dict(config.items('postgres')).iteritems():
            options[iconfig] = val

        # open connection to server
        err = None
        for i in range(3):
            try:
                pg.DB.__init__(self, host=options['hostname'],
                               user=options['username'],
                               passwd=options['password'],
                               dbname=options['database'])
                # set casting of numeric to floats
                pg.set_typecast('Numeric', float)
                if use_float:
                    pg.set_decimal(float)
                else:
                    pg.set_decimal(Decimal)
            except pg.InternalError as e:
                err = e
                if 'Operation timed out' in str(e) or 'Connection refused' in str(e):
                    continue
                else:
                    raise e
            except Exception as e:
                raise e
            else:
                break
        else:
            raise dbErrConnect(err)

        # open a conenction to a cursor
        self.cursor_conn = pgdb.connect(host=self.options['hostname'],
                                        user=self.options['username'],
                                        password=self.options['password'],
                                        database=self.options['database'])

        self.cursor = self.cursor_conn.cursor()

    def query(self, command, *args):
        err = None
        for i in range(3):
            try:
                rs = pg.DB.query(self, command, *args)
            except ValueError as e:
                # connection lost, attempt to reconnect
                self.reopen()
                err = e
            else:
                break
        else:
            raise Exception('dbConnection.query failed after 3 retries. Last error was: ' + str(err))

        return rs

    def query_float(self, command, as_dict=False):

        pg.set_typecast('Numeric', float)
        pg.set_decimal(float)

        err = None
        for i in range(3):
            try:
                rs = self.query(command)
            except ValueError as e:
                # connection lost, attempt to reconnect
                self.reopen()
                err = e
            else:
                break
        else:
            raise Exception('dbConnection.query_float failed after 3 retries. Last error was: ' + str(err))

        if as_dict:
            recordset = rs.dictresult()
        else:
            recordset = rs.getresult()

        pg.set_typecast('Numeric', Decimal)
        pg.set_decimal(Decimal)

        return recordset

    def get_columns(self, table):
        tblinfo = self.query('select column_name, data_type from information_schema.columns where table_name=\'%s\''
                             % table)

        field_dict = dict()

        for field in tblinfo.dictresult():
            field_dict[field['column_name']] = field['data_type']

        return field_dict

    def begin_transac(self):
        # do not begin a new transaction with another one active.
        if self.active_transaction:
            self.rollback_transac()

        self.active_transaction = True
        self.begin()

    def commit_transac(self):
        self.active_transaction = False
        self.commit()

    def rollback_transac(self):
        self.active_transaction = False
        self.rollback()

    def insert(self, table, row=None, **kw):
        err = None
        for i in range(3):
            try:
                pg.DB.insert(self, table, row, **kw)
            except ValueError as e:
                # connection lost, attempt to reconnect
                self.reopen()
                err = e
            except Exception as e:
                raise dbErrInsert(e)
            else:
                break
        else:
            raise dbErrInsert('dbConnection.insert failed after 3 retries. Last error was: ' + str(err))

    def executemany(self, sql, parameters):

        try:
            self.begin_transac()
            self.cursor_conn.executemany(sql, parameters)
            self.cursor_conn.commit()
        except pg.Error:
            self.rollback_transac()
            raise

    def update(self, table, row=None, **kw):
        err = None
        for i in range(3):
            try:
                pg.DB.update(self, table, row, **kw)
            except ValueError as e:
                # connection lost, attempt to reconnect
                self.reopen()
                err = e
            except Exception as e:
                raise dbErrUpdate(e)
            else:
                break
        else:
            raise dbErrUpdate('dbConnection.update failed after 3 retries. Last error was: ' + str(err))

    def delete(self, table, row=None, **kw):
        err = None
        for i in range(3):
            try:
                pg.DB.delete(self, table, row, **kw)
            except ValueError as e:
                # connection lost, attempt to reconnect
                self.reopen()
                err = e
            except Exception as e:
                raise dbErrDelete(e)
            else:
                break
        else:
            raise dbErrDelete('dbConnection.delete failed after 3 retries. Last error was: ' + str(err))

    def insert_event(self, event):

        self.insert('events', event.db_dict())

        return

    def insert_event_bak(self, type, module,desc):

        # do not insert if record exists
        desc = '%s%s' % (module, desc.replace('\'', ''))
        desc = re.sub(r'[^\x00-\x7f]+', '', desc)
        # remove commands from events
        # modification introduced by DDG (suggested by RS)
        desc = re.sub(r'BASH.*', '', desc)
        desc = re.sub(r'PSQL.*', '', desc)

        # warn = self.query('SELECT * FROM events WHERE "EventDescription" = \'%s\'' % (desc))

        # if warn.ntuples() == 0:
        self.insert('events', EventType=type, EventDescription=desc)

        return

    def insert_warning(self, desc):
        line = inspect.stack()[1][2]
        caller = inspect.stack()[1][3]

        mod = platform.node()

        module = '[%s:%s(%s)]\n' % (mod, caller, str(line))

        # get the module calling for insert_warning to make clear how is logging this message
        self.insert_event_bak('warn', module, desc)

    def insert_error(self, desc):
        line = inspect.stack()[1][2]
        caller = inspect.stack()[1][3]

        mod = platform.node()

        module = '[%s:%s(%s)]\n' % (mod, caller, str(line))

        # get the module calling for insert_warning to make clear how is logging this message
        self.insert_event_bak('error', module, desc)

    def insert_info(self, desc):
        line = inspect.stack()[1][2]
        caller = inspect.stack()[1][3]

        mod = platform.node()

        module = '[%s:%s(%s)]\n' % (mod, caller, str(line))

        self.insert_event_bak('info', module, desc)

    def __del__(self):
        if self.active_transaction:
            self.rollback()
