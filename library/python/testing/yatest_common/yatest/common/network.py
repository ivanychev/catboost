# coding=utf-8

import os
import errno
import socket
import random
import platform
import threading

UI16MAXVAL = (1 << 16) - 1


class PortManagerException(Exception):
    pass


class PortManager(object):
    """
    See documentation here

    https://wiki.yandex-team.ru/yatool/test/#poluchenieportovdljatestirovanija
    """

    def __init__(self, sync_dir=None):
        self._sync_dir = sync_dir or os.environ.get('PORT_SYNC_PATH')
        if self._sync_dir:
            _makedirs(self._sync_dir)

        self._valid_range = get_valid_port_range()
        self._valid_port_count = self._count_valid_ports()
        self._filelocks = {}
        self._lock = threading.Lock()

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.release()

    def get_port(self, port=0):
        '''
        Gets free TCP port
        '''
        return self.get_tcp_port(port)

    def get_tcp_port(self, port=0):
        '''
        Gets free TCP port
        '''
        return self._get_port(port, socket.SOCK_STREAM)

    def get_udp_port(self, port=0):
        '''
        Gets free UDP port
        '''
        return self._get_port(port, socket.SOCK_DGRAM)

    def get_tcp_and_udp_port(self, port=0):
        '''
        Gets one free port for use in both TCP and UDP protocols
        '''
        if port and self._no_random_ports():
            return port

        retries = 20
        while retries > 0:
            retries -= 1

            result_port = self.get_tcp_port()
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
            try:
                sock.bind(('::', result_port))
            except socket.error:
                self.release_port(result_port)
                continue
            finally:
                sock.close()
            # Don't try to _lock_port(), it's already locked in the get_tcp_port()
            return result_port
        raise Exception('Failed to find port')

    def release_port(self, port):
        with self._lock:
            filelock = self._filelocks.get(port, None)
            if filelock:
                self._relase_filelock(filelock)

    def release(self):
        with self._lock:
            while self._filelocks:
                _, filelock = self._filelocks.popitem()
                if filelock:
                    self._relase_filelock(filelock)

    def _relase_filelock(self, filelock):
        try:
            os.unlink(filelock.path)
        except OSError:
            pass
        filelock.release()

    def _count_valid_ports(self):
        res = 0
        for left, right in self._valid_range:
            res += right - left + 1
        assert res, ('There are no available valid ports', self._valid_range)
        return res

    def _get_port(self, port, sock_type):
        if port and self._no_random_ports():
            return port

        if len(self._filelocks) >= self._valid_port_count:
            raise PortManagerException("All valid ports are taken ({}): {}".format(self._valid_range, self._filelocks))

        salt = random.randint(0, UI16MAXVAL)
        for attempt in xrange(self._valid_port_count):
            probe_port = (salt + attempt) % self._valid_port_count

            for left, right in self._valid_range:
                if probe_port >= (right - left + 1):
                    probe_port -= right - left + 1
                else:
                    probe_port += left
                    break

            sock = socket.socket(socket.AF_INET6, sock_type)
            try:
                sock.bind(('::', probe_port))
            except socket.error as e:
                if e.errno == errno.EADDRINUSE:
                    continue
            finally:
                sock.close()

            if not self._lock_port(probe_port):
                continue
            return probe_port

        raise PortManagerException("Failed to find valid port (range: {} used: {})".format(self._valid_range, self._filelocks))

    def _lock_port(self, port):
        with self._lock:
            if port in self._filelocks:
                return False

            if self._sync_dir:
                # yatest.common should try do be hermetic and don't have peerdirs
                # otherwise, PYTEST_SCRIPT (aka USE_ARCADIA_PYTHON=no) won't work
                import library.python.filelock

                filelock = library.python.filelock.FileLock(os.path.join(self._sync_dir, str(port)))
                if not filelock.acquire(blocking=False):
                    return False
                self._filelocks[port] = filelock
            else:
                # Remember given port without lock
                self._filelocks[port] = None
            return True

    def _no_random_ports(self):
        return os.environ.get("NO_RANDOM_PORTS")


def get_valid_port_range():
    first_valid = 1025
    last_valid = UI16MAXVAL

    first_eph, last_eph = get_ephemeral_range()
    first_invalid = max(first_eph, first_valid)
    last_invalid = min(last_eph, last_valid)

    ranges = []
    if first_invalid > first_valid:
        ranges.append((first_valid, first_invalid - 1))
    if last_invalid < last_valid:
        ranges.append((last_invalid + 1, last_valid))
    return ranges


def get_ephemeral_range():
    if platform.system() == 'Linux':
        filename = "/proc/sys/net/ipv4/ip_local_port_range"
        if os.path.exists(filename):
            with open(filename) as afile:
                data = afile.read()
            return tuple(map(int, data.strip().split()))
    elif platform.system() == 'Darwin':
        first = _sysctlbyname_uint("net.inet.ip.portrange.first")
        last = _sysctlbyname_uint("net.inet.ip.portrange.last")
        if first and last:
            return first, last
    # IANA suggestion
    return (1 << 15) + (1 << 14), UI16MAXVAL


def _sysctlbyname_uint(name):
    try:
        from ctypes import CDLL, c_uint, byref
        from ctypes.util import find_library
    except ImportError:
        return

    libc = CDLL(find_library("c"))
    size = c_uint(0)
    res = c_uint(0)
    libc.sysctlbyname(name, None, byref(size), None, 0)
    libc.sysctlbyname(name, byref(res), byref(size), None, 0)
    return res.value


def _makedirs(path):
    try:
        os.makedirs(path)
    except OSError as e:
        if e.errno == errno.EEXIST:
            return
        raise
