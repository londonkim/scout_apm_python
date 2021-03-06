# coding=utf-8
from __future__ import absolute_import, division, print_function, unicode_literals

import json
import logging
import os
import socket
import struct
import sys
import threading
import time
from base64 import b64encode

# import socks
#
# socks.set_default_proxy(socks.SOCKS5, "10.41.251.28", 3128)
# socket.socket = socks.socksocket

from scout_apm.compat import queue
from scout_apm.core.agent.commands import Register
from scout_apm.core.agent.manager import get_socket_path
from scout_apm.core.config import scout_config
from scout_apm.core.threading import SingletonThread

# Time unit - monkey-patched in tests to make them run faster
SECOND = 5

logger = logging.getLogger(__name__)


class CoreAgentSocketThread(SingletonThread):
    _instance_lock = threading.Lock()
    _stop_event = threading.Event()
    _command_queue = queue.Queue(maxsize=500)

    @classmethod
    def _on_stop(cls):
        super(CoreAgentSocketThread, cls)._on_stop()
        # Unblock _command_queue.get()
        try:
            cls._command_queue.put(None, False)
        except queue.Full:
            pass

    @classmethod
    def send(cls, command):
        try:
            cls._command_queue.put(command, False)
        except queue.Full as exc:
            # TODO mark the command as not queued?
            logger.debug("CoreAgentSocketThread error on send: %r", exc, exc_info=exc)

        cls.ensure_started()

    @classmethod
    def wait_until_drained(cls, timeout_seconds=2.0, callback=None):
        interval_seconds = min(timeout_seconds, 0.05)
        start = time.time()
        while True:
            queue_size = cls._command_queue.qsize()
            queue_empty = queue_size == 0
            elapsed = time.time() - start
            if queue_empty or elapsed >= timeout_seconds:
                break

            if callback is not None:
                callback(queue_size)
                callback = None

            cls.ensure_started()

            time.sleep(interval_seconds)
        return queue_empty

    def run(self):
        self.socket_path = get_socket_path()
        self.socket = self.make_socket()

        try:
            self._connect()
            self._register()
            while True:
                try:
                    body = self._command_queue.get(block=True, timeout=1 * SECOND)
                except queue.Empty:
                    body = None

                if body is not None:
                    result = self._send(body)
                    if result:
                        self._command_queue.task_done()
                    else:
                        # Something was wrong with the socket.
                        self._disconnect()
                        self._connect()
                        self._register()

                # Check for stop event after each read. This allows opening,
                # sending, and then immediately stopping. We do this for
                # the metadata event at application start time.
                if self._stop_event.is_set():
                    logger.debug("CoreAgentSocketThread stopping.")
                    break
        except Exception as exc:
            logger.debug("CoreAgentSocketThread exception: %r", exc, exc_info=exc)
        finally:
            self.socket.close()
            logger.debug("CoreAgentSocketThread stopped.")

    def _send(self, command):
        msg = command.message()

        try:
            data = json.dumps(msg)
        except (ValueError, TypeError) as exc:
            logger.debug(
                "Exception when serializing command message: %r", exc, exc_info=exc
            )
            return False

        full_data = struct.pack(">I", len(data)) + data.encode("utf-8")
        try:
            self.socket.sendall(full_data)
        except OSError as exc:
            logger.debug(
                (
                    "CoreAgentSocketThread exception on _send:"
                    + " %r on PID: %s on thread: %s"
                ),
                exc,
                os.getpid(),
                threading.current_thread(),
                exc_info=exc,
            )
            return False

        # TODO do something with the response sent back in reply to command
        self._read_response()

        return True

    def _read_response(self):
        try:
            raw_size = self.socket.recv(4)
            if len(raw_size) != 4:
                # Ignore invalid responses
                return None
            size = struct.unpack(">I", raw_size)[0]
            message = bytearray(0)

            while len(message) < size:
                recv = self.socket.recv(size)
                message += recv

            return message
        except OSError as exc:
            logger.debug(
                "CoreAgentSocketThread error on read response: %r", exc, exc_info=exc
            )
            return None

    def _register(self):
        self._send(
            Register(
                app=scout_config.value("name"),
                key=scout_config.value("key"),
                hostname=scout_config.value("hostname"),
            )
        )

    def _connect(self, connect_attempts=5, retry_wait_secs=1):
        for attempt in range(1, connect_attempts + 1):
            logger.debug(
                (
                    "CoreAgentSocketThread attempt %d, connecting to %s, "
                    + "PID: %s, Thread: %s"
                ),
                attempt,
                self.socket_path,
                os.getpid(),
                threading.current_thread(),
            )
            try:
                # self.socket.connect(self.get_socket_address())
                host, _, port = self.socket_path.tcp_address.partition(":")
                logger.debug("host " + host + " " + port)
                self.http_proxy_connect(self.socket, (str(host), int(port)), proxy=(str("10.41.251.28"), int(3128)))
                self.socket.settimeout(3 * SECOND)
                logger.debug("CoreAgentSocketThread connected")
                return
            except socket.error as exc:
                logger.debug(
                    "CoreAgentSocketThread connection error: %r", exc, exc_info=exc
                )
                # Return without waiting when reaching the maximum number of attempts.
                if attempt == connect_attempts:
                    raise
                time.sleep(retry_wait_secs * SECOND)

    def _disconnect(self):
        logger.debug("CoreAgentSocketThread disconnecting from %s", self.socket_path)
        try:
            self.socket.close()
        except socket.error as exc:
            logger.debug(
                "CoreAgentSocketThread exception on disconnect: %r", exc, exc_info=exc
            )
        finally:
            self.socket = self.make_socket()

    def make_socket(self):
        if self.socket_path.is_tcp:
            family = socket.AF_INET
        else:
            family = socket.AF_UNIX
        return socket.socket(family, socket.SOCK_STREAM)

    def get_socket_address(self):
        if self.socket_path.is_tcp:
            host, _, port = self.socket_path.tcp_address.partition(":")
            if sys.version_info[0] == 2:
                host = bytes(host)
            return host, int(port)
        return self.socket_path


    '''
    Establish a socket connection through an HTTP proxy.
    
    Author: Fredrik Østrem <frx.apps@gmail.com>
    
    License:
      This code can be used, modified and distributed freely, as long as it is this note containing the original
      author, the source and this license, is put along with the source code.
    '''

    def http_proxy_connect(self, copy_socket, address, proxy=None, auth=None, headers={}):
        """
        Establish a socket connection through an HTTP proxy.

        Arguments:
          address (required)     = The address of the target
          proxy (def: None)      = The address of the proxy server
          auth (def: None)       = A tuple of the username and password used for authentication
          headers (def: {})      = A set of headers that will be sent to the proxy

        Returns:
          A 3-tuple of the format:
            (socket, status_code, headers)
          Where `socket' is the socket object, `status_code` is the HTTP status code that the server
           returned and `headers` is a dict of headers that the server returned.
        """

        def valid_address(addr):
            """ Verify that an IP/port tuple is valid """
            return isinstance(addr, (list, tuple)) and len(addr) == 2 and isinstance(addr[0], str) and isinstance(addr[1], (
            int, long))

        if not valid_address(address):
            raise ValueError('Invalid target address')

        if proxy == None:
            s = copy_socket
            s.connect(address)
            return s, 0, {}

        if not valid_address(proxy):
            raise ValueError('Invalid proxy address')

        headers = {
            'host': address[0]
        }

        if auth != None:
            if isinstance(auth, str):
                headers['proxy-authorization'] = auth
            elif auth and isinstance(auth, (tuple, list)) and len(auth) == 2:
                headers['proxy-authorization'] = 'Basic ' + b64encode('%s:%s' % auth)
            else:
                raise ValueError('Invalid authentication specification')

        s = copy_socket
        s.connect(proxy)
        fp = s.makefile('r+')

        fp.write('CONNECT %s:%d HTTP/1.0\r\n' % address)
        fp.write('\r\n'.join('%s: %s' % (k, v) for (k, v) in headers.items()) + '\r\n\r\n')
        fp.flush()

        statusline = fp.readline().rstrip('\r\n')

        if statusline.count(' ') < 2:
            fp.close()
            s.close()
            raise IOError('Bad response')
        version, status, statusmsg = statusline.split(' ', 2)
        if not version in ('HTTP/1.0', 'HTTP/1.1'):
            fp.close()
            s.close()
            raise IOError('Unsupported HTTP version')
        try:
            status = int(status)
        except ValueError:
            fp.close()
            s.close()
            raise IOError('Bad response')

        response_headers = {}

        while True:
            tl = ''
            l = fp.readline().rstrip('\r\n')
            if l == '':
                break
            if not ':' in l:
                continue
            k, v = l.split(':', 1)
            response_headers[k.strip().lower()] = v.strip()

        fp.close()
        return (s, status, response_headers)