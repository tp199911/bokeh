''' Provides the Bokeh Server Tornado application.

'''
from __future__ import absolute_import, print_function

import logging
log = logging.getLogger(__name__)

import atexit
# NOTE: needs PyPI backport on Python 2 (https://pypi.python.org/pypi/futures)
from concurrent.futures import ProcessPoolExecutor
import os
import signal

from tornado import gen
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.web import Application as TornadoApplication

from .settings import settings
from .urls import patterns
from .core.server_session import ServerSession
from .core.server_connection import ServerConnection


class SessionDict(dict):
    def __init__(self, document_factory):
        self._document_factory = document_factory

    def __missing__(self, sessionid):
        doc = self._document_factory()
        session = ServerSession(sessionid, doc)
        self[sessionid] = session
        return session

class BokehTornado(TornadoApplication):
    ''' A Tornado Application used to implement the Bokeh Server.

        The Server class is the main public interface, this class has
        Tornado implementation details.

    Args:
        application (bokeh.application.Application) : an Application instance
            The application is used to create documents for each session.
        extra_patterns (seq[tuple]) : tuples of (str, http or websocket handler)
            Use this argmument to add additional endpoints to custom deployments
            of the Bokeh Server.

    '''

    def __init__(self, application, io_loop=None, extra_patterns=None):
        extra_patterns = extra_patterns or []
        super(BokehTornado, self).__init__(patterns+extra_patterns, **settings)

        self._application = application
        self._sessions = SessionDict(self._application.create_document)
        self._clients = set()
        self._executor = ProcessPoolExecutor(max_workers=4)
        if io_loop is None:
            io_loop = IOLoop.current()
        self._loop = io_loop
        self._loop.add_callback(self._start_async)
        self._stats_job = PeriodicCallback(self.log_stats, 5.0 * 1000, io_loop=self._loop)
        self._stats_job.start()

    def start(self):
        ''' Start the Bokeh Server application main loop.

        Args:

        Returns:
            None

        Notes:
            Keyboard interrupts or sigterm will cause the server to shut down.

        '''
        try:
            self._loop.start()
        except KeyboardInterrupt:
            print("\nInterrupted, shutting down")

    def stop(self):
        ''' Stop the Bokeh Server application.

        Returns:
            None

        '''
        self._loop.add_callback(self._loop.stop)

    @property
    def executor(self):
        return self._executor

    def new_connection(self, protocol):
        connection = ServerConnection(protocol, self)
        self._clients.add(connection)
        return connection

    def client_lost(self, connection):
        self._clients.discard(connection)

    def get_or_create_session(self, sessionid):
        # this is because empty sessionids would be "falsey" and
        # potentially open up a way for clients to confuse us
        if len(sessionid) == 0:
            raise ValueError("Session ID must not be empty")

        return self._sessions[sessionid]

    def log_stats(self):
        log.debug("[pid %d] %d clients connected", os.getpid(), len(self._clients))

    @gen.coroutine
    def run_in_background(self, _func, *args, **kwargs):
        """
        Run a synchronous function in the background without disrupting
        the main thread. Useful for long-running jobs.
        """
        res = yield self._executor.submit(_func, *args, **kwargs)
        raise gen.Return(res)

    @gen.coroutine
    def _start_async(self):
        try:
            atexit.register(self._atexit)
            signal.signal(signal.SIGTERM, self._sigterm)
        except Exception:
            self.exit(1)

    _atexit_ran = False
    def _atexit(self):
        if self._atexit_ran:
            return
        self._atexit_ran = True

        self._stats_job.stop()
        IOLoop.clear_current()
        loop = IOLoop()
        loop.make_current()
        loop.run_sync(self._cleanup)

    def _sigterm(self, signum, frame):
        print("Received SIGTERM, shutting down")
        self.stop()
        self._atexit()

    @gen.coroutine
    def _cleanup(self):
        log.warn("Shutdown: cleaning up")
        self._executor.shutdown(wait=False)
        self._clients.clear()
