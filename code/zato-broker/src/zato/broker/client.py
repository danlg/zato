# -*- coding: utf-8 -*-

"""
Copyright (C) 2011 Dariusz Suchojad <dsuch at gefira.pl>

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <http://www.gnu.org/licenses/>.
"""

from __future__ import absolute_import, division, print_function, unicode_literals

# stdlib
import logging
from threading import Thread
from traceback import format_exc

# ZeroMQ
import zmq

logger = logging.getLogger(__name__)

class ZMQSub(object):
    """ A ZeroMQ subscriber. Runs in a background thread and invokes the handler
    on each incoming message.
    """
    
    def __init__(self, zmq_context, address, on_message_handler, 
                 sub_patterns=(b'',), keep_running=True):
        self.zmq_context = zmq_context
        self.address = address
        self.on_message_handler = on_message_handler
        self.sub_patterns = sub_patterns
        self.keep_running = keep_running

    # Custom subclasses may wish to override the two hooks below.
    def on_before_msg_handler(self, msg):
        pass

    def on_after_msg_handler(self, msg, e=None):
        pass
    
    def start(self):
        Thread(target=self.listen).start()
        
    def close(self, socket=None):
        self.keep_running = False
        self.socket.close()
    
    def listen(self):
        logger.debug('Starting [{0}]/[{1}]'.format(self.__class__.__name__, 
                self.address))
        
        socket = self.zmq_context.socket(zmq.SUB)
        socket.connect(self.address)
        for pattern in self.sub_patterns:
            socket.setsockopt(zmq.SUBSCRIBE, pattern)
        
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        
        while self.keep_running:
            try:
                socks = dict(poller.poll())
                if socks.get(socket) == zmq.POLLIN:
                    msg = socket.recv()
            except zmq.ZMQError, e:
                msg = 'Caught ZMQError [{0}], quitting.'.format(e.strerror)
                logger.error(msg)
                self.close()
            else:
                self.on_before_msg_handler(msg)
                try:
                    e = None
                    self.on_message_handler(msg)
                except Exception, e:
                    msg = 'Could not invoke the message handler, msg [{0}] e [{1}]'
                    logger.error(msg.format(msg, format_exc(e)))
                    
                self.on_after_msg_handler(msg, e)

class ZMQPush(object):
    """ Sends messages to ZeroMQ using a PUSH socket.
    """
    def __init__(self, zmq_context, address):
        self.zmq_context = zmq_context
        self.address = address
        self.socket_type = zmq.PUSH 
        
        self.socket = self.zmq_context.socket(self.socket_type)
        self.socket.connect(self.address)
        
    def send(self, msg):
        try:
            self.socket.send(msg)
        except zmq.ZMQError, e:
            msg = 'Caught ZMQError [{0}], continuing anyway.'.format(e.strerror)
            logger.warn(msg)
        
    def close(self):
        msg = 'Stopping [{0}/{1}]'.format(self.address, self.socket_type)
        logger.info(msg)
        self.socket.close()
        
class BrokerClient(object):
    """ A ZeroMQ broker client which knows how to subscribe to messages and push
    the messages onto the broker.
    """
    def __init__(self, zmq_context, push_address, sub_address, on_message_handler,
                 sub_patterns=(b'',)):
        self._push = ZMQPush(zmq_context, push_address)
        self._sub = ZMQSub(zmq_context, sub_address, on_message_handler,
                           sub_patterns)
    def start_subscriber(self):
        self._sub.start()
    
    def send(self, msg):
        self._push.send(msg)
    
    def close(self):
        self._push.close()
        self._sub.close()