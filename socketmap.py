#!/usr/bin/env python3
#
# socketmap.py
# by Keith Gaughan (https://github.com/kgaughan/)
#
# A daemon implementing the sendmail socketmap protocol to allow an SQLite
# database to be queried out of process.
#
# Copyright (c) Keith Gaughan, 2017. See 'LICENSE' for license details.
#

from __future__ import print_function

import argparse
import contextlib
try:
    import configparser
except ImportError:
    import ConfigParser as configparser
import logging
try:
    import socketserver
except ImportError:
    import SocketServer as socketserver
import os
import os.path
import re
import sqlite3
import sys


FUNC_REF_PATTERN = re.compile(r"""
    ^
    (?P<module>
        [a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*
    )
    :
    (?P<object>
        [a-z_][a-z0-9_]*(?:\.[a-z_][a-z0-9_]*)*
    )
    $
    """, re.I | re.X)


def match(name):
    matches = FUNC_REF_PATTERN.match(name)
    if not matches:
        raise ValueError("Malformed callable '{}'".format(name))
    return matches.group('module'), matches.group('object')


def resolve(module_name, obj_name):
    """
    Resolve a named object in a module.
    """
    segments = obj_name.split('.')
    obj = __import__(module_name, fromlist=segments[:1])
    for segment in segments:
        obj = getattr(obj, segment)
    return obj


class MalformedNetstringError(Exception):
    pass


def netstring_reader(fp):
    while True:
        n = ""
        while True:
            c = fp.read(1)
            if c == '':
                return
            if c == ':':
                break
            if len(n) > 10:
                raise MalformedNetstringError
            if c == '0' and n == '':
                # We can't allow leading zeros.
                if fp.read(1) != ':':
                    raise MalformedNetstringError
                n = c
                break
            n += c
        n = int(n, 10)
        payload = fp.read(n)
        if len(payload) < n:
            return
        if fp.read(1) != ',':
            raise MalformedNetstringError
        yield payload


class NoSuchTable(Exception):
    pass


class Handler(socketserver.StreamRequestHandler):

    def _query_db(self, table, arg):
        cur = self.server.db_conn.cursor()
        cur.execute(table['query'], [table['transform'](arg)])
        return cur.fetchone()

    def run_query(self, table_name, arg):
        table = self.server.tables.get(table_name)
        if table is None:
            raise NoSuchTable(table_name)
        return self._query_db(table, arg)

    def write_netstring(self, response):
        self.wfile.write('{}:{},'.format(len(response), response))

    def handle(self):
        for request in netstring_reader(self.rfile):
            table_name, arg = request.split(' ', 1)
            try:
                result = self.run_query(table_name, arg)
            except Exception as exc:
                self.write_netstring('PERM ' + str(exc))
            else:
                if result is None:
                    self.write_netstring('NOTFOUND ')
                else:
                    self.write_netstring('OK ' + result[0])


def create_arg_parser():
    parser = argparse.ArgumentParser(description='SQLite socketmap daemon.')
    parser.add_argument('--config',
                        help='Path to config file',
                        type=argparse.FileType(),
                        default='/etc/socketmapd.ini')
    parser.add_argument('--sock',
                        help='Path to server socket file',
                        default='/var/run/sockmapd.sock')
    return parser


def parse_config(fp):
    def passthrough(arg):
        return arg

    def local_part(arg):
        return arg.split('@', 1)[0]

    def domain_part(arg):
        return arg.split('@', 1)[1]

    cp = configparser.RawConfigParser()
    cp.readfp(fp)

    result = {
        'db_path': cp.get('database', 'path'),
        'tables': {},
    }

    for section in cp.sections():
        if section.startswith('table:'):
            _, table_name = section.split(':', 1)
            if not cp.has_option(section, 'query'):
                logging.warning("No query in '%s': skipping", section)
                continue

            try:
                transform_name = cp.get(section, 'transform')
            except configparser.NoOptionError:
                transform_name = 'all'
            if transform_name == 'all':
                transform = passthrough
            elif transform_name == 'local':
                transform = local_part
            elif transform_name == 'domain':
                transform = domain_part
            else:
                transform = resolve(*match(transform_name))

            result['tables'][table_name] = {
                'transform': transform,
                'query': cp.get(section, 'query'),
            }

    return result


def main():
    parser = create_arg_parser()
    args = parser.parse_args()
    if os.path.exists(args.sock):
        parser.error("Socket file exists: {}".format(args.sock))

    with contextlib.closing(args.config):
        cfg = parse_config(args.config)

    if not os.path.exists(cfg['db_path']):
        print("error: cannot find {}".format(cfg['db_path']), file=sys.stderr)
        return 2

    server = socketserver.UnixStreamServer(args.sock, Handler)
    server.tables = cfg['tables']
    with contextlib.closing(sqlite3.connect(cfg['db_path'])) as conn:
        server.db_conn = conn
        try:
            server.serve_forever()
        finally:
            os.remove(args.sock)

    return 0


if __name__ == '__main__':
    sys.exit(main())
