''' Provides the ``ServerSession`` class.

'''
from __future__ import absolute_import

import logging
log = logging.getLogger(__name__)

from tornado import gen, locks
from bokeh.document import ModelChangedEvent, RootAddedEvent, RootRemovedEvent

class ServerSession(object):
    ''' Hosts an application "instance" (an instantiated Document) for one or more connections.

    '''

    def __init__(self, sessionid, document):
        if sessionid is None:
            raise ValueError("Sessions must have an id")
        if document is None:
            raise ValueError("Sessions must have a document")
        self._id = sessionid
        self._document = document
        self._subscribed_connections = set()
        self._lock = locks.Lock()
        self._current_patch = None
        self._current_patch_connection = None
        self._document.on_change(self._document_changed)

    @property
    def document(self):
        return self._document

    @property
    def id(self):
        return self._id

    def subscribe(self, connection):
        """This should only be called by ServerConnection.subscribe_session or our book-keeping will be broken"""
        self._subscribed_connections.add(connection)

    def unsubscribe(self, connection):
        """This should only be called by ServerConnection.unsubscribe_session or our book-keeping will be broken"""
        self._subscribed_connections.discard(connection)

    @property
    def connection_count(self):
        return len(self._subscribed_connections)

    def _document_changed(self, event):
        if isinstance(event, ModelChangedEvent):
            may_suppress = self._current_patch is not None and \
                           self._current_patch.should_suppress_on_change(event.model, event.attr, event.new)

            # TODO (havocp): our "change sync" protocol is flawed
            # because if both sides change the same attribute at the
            # same time, they will each end up with the state of the
            # other and their final states will differ.
            for connection in self._subscribed_connections:
                if may_suppress and connection is self._current_patch_connection:
                    log.debug("Not sending notification back to client %r for a change it requested", connection)
                else:
                    connection.send_patch_document(self._id, self._document, event.model, { event.attr : event.new })
        elif isinstance(event, RootAddedEvent):
            pass # TODO (havocp) we need a message to use for this
        elif isinstance(event, RootRemovedEvent):
            pass # TODO (havocp) we need a message to use for this
        else:
            raise RuntimeError("Unhandled document change event " + repr(event))

    @classmethod
    @gen.coroutine
    def pull(cls, message, connection, session):
        # TODO implement me
        raise gen.Return(connection.error(message, "pull not implemented"))

    @classmethod
    @gen.coroutine
    def push(cls, message, connection, session):
        with (yield session._lock.acquire()):
            log.debug("pushing doc to session %r", session.id)
            message.push_to_document(session.document)

            raise gen.Return(connection.ok(message))

    # this method is split out of the patch() class method so we
    # can monkeypatch it in the tests
    @gen.coroutine
    def _handle_patch(self, message, connection):
        with (yield self._lock.acquire()):
            log.debug("patching session %r with %r", self.id, message.content)
            self._current_patch = message
            self._current_patch_connection = connection
            try:
                message.apply_to_document(self.document)
            finally:
                self._current_patch = None
                self._current_patch_connection = None

            raise gen.Return(connection.ok(message))

    @classmethod
    @gen.coroutine
    def patch(cls, message, connection, session):
        work = yield session._handle_patch(message, connection)
        raise gen.Return(work)