# -*- coding: utf-8 -*-

"""
Copyright (C) 2010 Dariusz Suchojad <dsuch at gefira.pl>

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
import asyncore, httplib, json, logging, socket, time
from hashlib import sha256
from thread import start_new_thread
from threading import Thread
from traceback import format_exc

# Zope
from zope.server.http.httpserver import HTTPServer
from zope.server.http.httpserverchannel import HTTPServerChannel
from zope.server.http.httptask import HTTPTask
from zope.server.serverchannelbase import task_lock
from zope.server.taskthreads import ThreadedTaskDispatcher

# ZeroMQ
import zmq

# Bunch
from bunch import Bunch

# Zato
from zato.common import ZATO_CONFIG_REQUEST, ZATO_JOIN_REQUEST_ACCEPTED, \
     ZATO_OK, ZATO_PARALLEL_SERVER, ZATO_SINGLETON_SERVER, ZATO_URL_TYPE_SOAP
from zato.broker.zato_client import BrokerClient
from zato.common.util import TRACE1, zmq_names
from zato.common.odb import create_pool
from zato.server.base import BaseServer
from zato.server.channel.soap import server_soap_error

logger = logging.getLogger(__name__)

def wrap_error_message(url_type, msg):
    """ Wraps an error message in a transport-specific envelope.
    """
    if url_type == ZATO_URL_TYPE_SOAP:
        return server_soap_error(msg)
    
    # Let's return the message as-is if we don't have any specific envelope
    # to use.
    return msg

class HTTPException(Exception):
    """ Raised when the underlying error condition can be easily expressed
    as one of the HTTP status codes.
    """
    def __init__(self, status, reason):
        self.status = status
        self.reason = reason
        
class _HTTPTask(HTTPTask):
    """ An HTTP task which knows how to uses ZMQ sockets.
    """
    def service(self, thread_data):
        try:
            try:
                self.start()
                self.channel.server.executeRequest(self, thread_data)
                self.finish()
            except socket.error:
                self.close_on_finish = 1
                if self.channel.adj.log_socket_errors:
                    raise
        finally:
            if self.close_on_finish:
                self.channel.close_when_done()
                
class _HTTPServerChannel(HTTPServerChannel):
    """ A subclass which uses Zato's own _HTTPTasks.
    """
    task_class = _HTTPTask
    
    def service(self, thread_data):
        """Execute all pending tasks"""
        while True:
            task = None
            task_lock.acquire()
            try:
                if self.tasks:
                    task = self.tasks.pop(0)
                else:
                    # No more tasks
                    self.running_tasks = False
                    self.set_async()
                    break
            finally:
                task_lock.release()
            try:
                task.service(thread_data)
            except:
                # propagate the exception, but keep executing tasks
                self.server.addTask(self)
                raise
        
class _TaskDispatcher(ThreadedTaskDispatcher):
    """ A task dispatcher which knows how to pass custom arguments down to
    the newly created threads.
    """
    def __init__(self, message_handler, broker_token, zmq_context, 
            broker_push_addr, broker_pull_addr):
        super(_TaskDispatcher, self).__init__()
        self.message_handler = message_handler
        self.broker_token = broker_token
        self.zmq_context = zmq_context
        self.broker_push_addr = broker_push_addr
        self.broker_pull_addr = broker_pull_addr
        
    def setThreadCount(self, count):
        """ Mostly copy & paste from the base classes except for the part
        that passes the arguments to the thread.
        """
        mlock = self.thread_mgmt_lock
        mlock.acquire()
        try:
            threads = self.threads
            thread_no = 0
            running = len(threads) - self.stop_count
            while running < count:
                # Start threads.
                while thread_no in threads:
                    thread_no = thread_no + 1
                threads[thread_no] = 1
                running += 1

                # It's safe to pass ZMQ contexts between threads.
                thread_data = Bunch({
                    'message_handler': self.message_handler,
                    'broker_token':self.broker_token,
                    'zmq_context': self.zmq_context,
                    'broker_push_addr': self.broker_push_addr,
                    'broker_pull_addr': self.broker_pull_addr})
                
                start_new_thread(self.handlerThread, (thread_no, thread_data))
                
                thread_no = thread_no + 1
            if running > count:
                # Stop threads.
                to_stop = running - count
                self.stop_count += to_stop
                for n in range(to_stop):
                    self.queue.put(None)
                    running -= 1
        finally:
            mlock.release()
            
    def handlerThread(self, thread_no, thread_data):
        """ Mostly copy & paste from the base classes except for the part
        that passes the arguments to the thread.
        """

        # We're in a new thread now so we can start the broker client though note
        # that the message handler will be assigned to it later on.
        thread_data.broker_client = BrokerClient(self.broker_token,
                thread_data.zmq_context, thread_data.broker_push_addr, 
                thread_data.broker_pull_addr, self.message_handler)
        thread_data.broker_client.set_message_handler_kwargs(**{
            'broker_client': thread_data.broker_client})
        thread_data.broker_client.start_subscriber()
        
        threads = self.threads
        try:
            while threads.get(thread_no):
                task = self.queue.get()
                if task is None:
                    # Special value: kill this thread.
                    break
                try:
                    task.service(thread_data)
                except Exception, e:
                    logger.error('Exception during task {0}'.format(
                        format_exc(e)))
        finally:
            mlock = self.thread_mgmt_lock
            mlock.acquire()
            try:
                self.stop_count -= 1
                try: del threads[thread_no]
                except KeyError: pass
            finally:
                mlock.release()
            
class ZatoHTTPListener(HTTPServer):
    
    channel_class = _HTTPServerChannel
    
    def __init__(self, server, task_dispatcher, broker_client=None):
        self.logger = logging.getLogger("%s.%s" % (__name__, 
                                                   self.__class__.__name__))
        self.server = server
        self.broker_client = broker_client
        super(ZatoHTTPListener, self).__init__(self.server.host, self.server.port, 
                                               task_dispatcher)
        
    def _on_broker_msg(self, msg):
        """ Passes the message on to a parallel server.
        """
        self.server._on_broker_msg(msg)

    def _handle_security_tech_account(self, sec_def, request_data, body, headers):
        """ Handles the 'tech-account' security config type.
        """
        zato_headers = ('X_ZATO_USER', 'X_ZATO_PASSWORD')
        
        for header in zato_headers:
            if not headers.get(header, None):
                msg = ("The header [{0}] doesn't exist or is empty, URI=[{1}, "
                      "headers=[{2}]]").\
                        format(header, request_data.uri, headers)
                self.logger.error(msg)
                raise HTTPException(httplib.FORBIDDEN, msg)

        # Note that both checks below send a different message to the client 
        # when compared with what goes into logs. It's to conceal from
        # bad-behaving users what really went wrong (that of course assumes 
        # they can't access the logs).

        msg_template = 'The {0} is incorrect, URI=[{1}], X_ZATO_USER=[{2}]'

        if headers['X_ZATO_USER'] != sec_def.name:
            self.logger.error(msg_template.format('username', request_data.uri, 
                              headers['X_ZATO_USER']))
            raise HTTPException(httplib.FORBIDDEN, msg_template.\
                    format('username or password', request_data.uri, 
                           headers['X_ZATO_USER']))
        
        incoming_password = sha256(headers['X_ZATO_PASSWORD'] + ':' + sec_def.salt).hexdigest()
        
        if incoming_password != sec_def.password:
            self.logger.error(msg_template.format('password', request_data.uri, 
                              headers['X_ZATO_USER']))
            raise HTTPException(httplib.FORBIDDEN, msg_template.\
                    format('username or password', request_data.uri, 
                           headers['X_ZATO_USER']))
        
        
    def handle_security(self, url_data, request_data, body, headers):
        """ Handles all security-related aspects of an incoming HTTP message
        handling. Calls other concrete security methods as appropriate.
        """
        sec_def, sec_def_type = url_data['sec_def'], url_data['sec_def_type']
        
        handler_name = '_handle_security_{0}'.format(sec_def_type.replace('-', '_'))
        getattr(self, handler_name)(sec_def, request_data, body, headers)
            
    def executeRequest(self, task, thread_ctx):
        """ Handles incoming HTTP requests. Each request is being handled by one
        of the threads created in ParallelServer.run_forever method.
        """
        
        # Initially, we have no clue about the type of the URL being accessed,
        # later on, if we don't stumble upon an exception, we may learn that
        # it is for instance, a SOAP URL.
        url_type = None
        
        try:
            # Collect necessary request data.
            body = task.request_data.getBodyStream().getvalue()
            headers = task.request_data.headers
            
            if task.request_data.uri in self.server.url_security:
                url_data = self.server.url_security[task.request_data.uri]
                url_type = url_data['url_type']
                
                self.handle_security(url_data, task.request_data, body, headers)
                
                # TODO: Shadow out any passwords that may be contained in HTTP
                # headers or in the message itself. Of course, that only applies
                # to auth schemes we're aware of (HTTP Basic Auth, WSS etc.)

            else:
                msg = ("The URL [{0}] doesn't exist or has no security "
                      "configuration assigned").format(task.request_data.uri)
                self.logger.error(msg)
                raise HTTPException(httplib.NOT_FOUND, msg)

            # Fetch the response.
            response = self.server.soap_handler.handle(body, headers, thread_ctx)

        except HTTPException, e:
            task.setResponseStatus(e.status, e.reason)
            response = wrap_error_message(url_type, e.reason)
            
        # Any exception at this point must be our fault.
        except Exception, e:
            tb = format_exc(e)
            self.logger.error('Exception caught [{0}]'.format(tb))
            response = wrap_error_message(url_type, tb)

        if url_type == ZATO_URL_TYPE_SOAP:
            content_type = 'text/xml'
        else:
            content_type = 'text/plain'
            
        task.response_headers['Content-Type'] = content_type
            
        # Return the HTTP response.
        task.response_headers['Content-Length'] = str(len(response))
        task.write(response)


class ParallelServer(BaseServer):
    def __init__(self, host=None, port=None, zmq_context=None, crypto_manager=None,
                 odb_manager=None, singleton_server=None):
        self.host = host
        self.port = port
        self.zmq_context = zmq_context or zmq.Context()
        self.crypto_manager = crypto_manager
        self.odb_manager = odb_manager
        self.singleton_server = singleton_server
        
        self.zmq_items = {}
        
        self.logger = logging.getLogger("%s.%s" % (__name__, self.__class__.__name__))
        
    def _after_init_common(self, server):
        """ Initializes parts of the server that don't depend on whether the
        server's been allowed to join the cluster or not.
        """
        
        # Security configuration of HTTP URLs.
        self.url_security = self.odb.get_url_security(server)
        self.logger.log(logging.DEBUG, 'url_security=[{0}]'.format(self.url_security))
        
        self.broker_token = server.cluster.broker_token
        self.broker_push_addr = 'tcp://{0}:{1}'.format(server.cluster.broker_host, 
                server.cluster.broker_start_port)
        self.broker_pull_addr = 'tcp://{0}:{1}'.format(server.cluster.broker_host, 
                server.cluster.broker_start_port+1)
        
        if self.singleton_server:
            
            self.service_store.read_internal_services()
            
            kwargs={'zmq_context':self.zmq_context,
                    'broker_host': server.cluster.broker_host,
                    'broker_push_port': server.cluster.broker_start_port+2,
                    'broker_sub_port': server.cluster.broker_start_port+3,
                    'broker_token':self.broker_token,
                    }
            Thread(target=self.singleton_server.run, kwargs=kwargs).start()
    
    def _after_init_accepted(self, server):
        pass
    
    def _after_init_non_accepted(self, server):
        pass    
        
    def after_init(self):
        
        # First try grabbing the basic server's data from the ODB. No point
        # in doing anything else if we can't get past this point.
        server = self.odb.fetch_server()
        
        if not server:
            raise Exception('Server does not exist in the ODB')
        
        self._after_init_common(server)
        
        # A server which hasn't been approved in the cluster still needs to fetch
        # all the config data but it won't start any MQ/AMQP/ZMQ/etc. listeners
        # except for a ZMQ config subscriber that will listen for an incoming approval.
        
        if server.last_join_status == ZATO_JOIN_REQUEST_ACCEPTED:
            self._after_init_accepted(server)
        else:
            msg = 'Server has not been accepted, last_join_status=[{0}]'
            self.logger.warn(msg.format(server.last_join_status))
            
            self._after_init_non_accepted(server)
        
    def on_inproc_message_handler(self, msg):
        """ Handler for incoming 'inproc' ZMQ messages.
        """
        
    def run_forever(self):

        task_dispatcher = _TaskDispatcher(self.on_broker_msg, self.broker_token, 
            self.zmq_context,  self.broker_push_addr, self.broker_pull_addr)
        task_dispatcher.setThreadCount(60)

        self.logger.debug('host=[{0}], port=[{1}]'.format(self.host, self.port))

        ZatoHTTPListener(self, task_dispatcher)

        try:
            while True:
                asyncore.poll(5)

        except KeyboardInterrupt:
            self.logger.info("Shutting down.")
            
            # ZeroMQ
            for zmq_item in self.zmq_items.values():
                zmq_item.close()
                

            if self.singleton_server:
                self.singleton_server.broker_client.close()
                
            self.zmq_context.term()
            task_dispatcher.shutdown()

# ##############################################################################

    def on_broker_msg_SCHEDULER_EXECUTE(self, msg, **kwargs):
        service_info = self.service_store.services[msg.service]
        class_ = service_info['service_class']
        instance = class_()
        instance.server = self

        response = instance.handle(payload=msg.extra, raw_request=msg, 
                    channel='scheduler_job', thread_ctx=kwargs)
        
        if self.logger.isEnabledFor(logging.DEBUG):
            msg = 'Invoked [{0}], response [{1}]'.format(msg.service, repr(response))
            self.logger.debug(str(msg))
            