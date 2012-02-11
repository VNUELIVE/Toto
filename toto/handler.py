from tornado.web import *
import json
import hashlib
import hmac
from invocation import *
from exceptions import *
from tornado.options import define, options
import base64
from events import EventManager

define("bson_enabled", default=False, help="Allows requests to use BSON with content-type application/bson")
define("allow_origin", default="*", help="This is the value for the Access-Control-Allow-Origin header (default *)")
define("debug", default=False, help="Set this to true to prevent Toto from nicely formatting generic errors. With debug=True, errors will print to the command line")
define("method_select", default="both", metavar="both|url|parameter", help="Selects whether methods can be specified via URL, parameter in the message body or both (default both)")

class TotoHandler(RequestHandler):

  SUPPORTED_METHODS = ["POST", "OPTIONS", "GET"]
  ACCESS_CONTROL_ALLOW_ORIGIN = options.allow_origin

  def initialize(self, method_root, connection):
    self.__method_root = method_root
    self.connection = connection
    self.bson = options.bson_enabled and __import__('bson').BSON
    self.response_type = 'application/json'

  @classmethod
  def configure(cls):
    #Method configuration
    if options.method_select == 'url':
      def get_method_path(self, path, body):
        if path:
          self.method_path = path.split('/')
        else:
          raise TotoException(ERROR_MISSING_METHOD, "Missing method.")
      cls.__get_method_path = get_method_path
    elif options.method_select == 'parameter':
      def get_method_path(self, path, body):
        if 'method' in body:
          self.method_path = body['method'].split('.')
        else:
          raise TotoException(ERROR_MISSING_METHOD, "Missing method.")
      cls.__get_method_path = get_method_path
    
  """
    The default method_select "both" (or any unsupported value) will
    call this method. The class method configure() will update this
    to a more efficient method according to the tornado.options
  """
  def __get_method_path(self, path, body):
    if path:
      self.method_path = path.split('/')
    elif 'method' in body:
      self.method_path = body['method'].split('.')
    else:
      raise TotoException(ERROR_MISSING_METHOD, "Missing method.")

  def __get_method(self):
    method = self.__method_root
    for i in self.method_path:
      method = getattr(method, i)
    self.__method = method.invoke

  def options(self, path=None):
    allowed_headers = set(['x-toto-hmac','x-toto-session-id','origin','content-type'])
    if 'access-control-request-headers' in self.request.headers:
      allowed_headers = allowed_headers.union(self.request.headers['access-control-request-headers'].lower().replace(' ','').split(','))
    self.add_header('access-control-allow-headers', ','.join(allowed_headers))
    if 'access-control-request-method' in self.request.headers and self.request.headers['access-control-request-method'] not in self.SUPPORTED_METHODS:
      raise HTTPError(405, 'Method not supported')
    self.add_header('access-control-allow-origin', self.ACCESS_CONTROL_ALLOW_ORIGIN)
    self.add_header('access-control-allow-methods', ','.join(self.SUPPORTED_METHODS))
    self.add_header('access-control-expose-headers', 'x-toto-hmac')
  
  @tornado.web.asynchronous
  def get(self, path=None):
    self.body = None
    self.parameters = self.request.arguments
    self.process_request(path)

  @tornado.web.asynchronous
  def post(self, path=None):
    if self.bson and 'content-type' in headers and headers['content-type'] == 'application/bson':
      self.response_type = 'application/bson'
      self.body = self.bson(self.request.body).decode()
    else:
      self.body = json.loads(self.request.body)
    self.parameters = 'parameters' in self.body and self.body['parameters'] or None
    self.process_request(path)

  def process_request(self, path=None):
    self.session = None
    self.__method = None
    headers = self.request.headers
    response = {}
    self.add_header('access-control-allow-origin', self.ACCESS_CONTROL_ALLOW_ORIGIN)
    self.add_header('access-control-expose-headers', 'x-toto-hmac')
    result = None
    error = None
    try:
      self.__get_method_path(path, self.body)
      self.__get_method()
      if 'x-toto-session-id' in headers:
        self.session = self.connection.retrieve_session(headers['x-toto-session-id'], 'x-toto-hmac' in headers and headers['x-toto-hmac'] or None, self.request.body)
      result = self.__method(self, self.parameters)
    except TotoException as e:
      error = e.__dict__
    except Exception as e:
      if options.debug:
        raise e
      error = TotoException(ERROR_SERVER, str(e)).__dict__
    if result is not None or error:
      self.respond(result, error, not hasattr(self.__method, 'asynchronous'))
    elif not hasattr(self.__method, 'asynchronous'):
      self.finish()

  def respond(self, result=None, error=None, finish=True):
    if self._finished:
      return
    response = {}
    if result is not None:
      response['result'] = result
    if error:
      response['error'] = error
    self.add_header('content-type', self.response_type)
    if self.response_type == 'application/bson':
      response_body = str(self.bson.encode(response))
    else:
      response_body = json.dumps(response)
    if self.session:
      self.add_header('x-toto-hmac', base64.b64encode(hmac.new(str(self.session.user_id), response_body, hashlib.sha1).digest()))
    self.write(response_body)
    if finish:
      self.finish()

  def on_connection_close(self):
    if hasattr(self.__method, 'on_connection_close'):
      self.__method.on_connection_close(self);

  def register_event_handler(self, event_name, handler, run_on_main_loop=True):
    EventManager.instance().register_handler(event_name, handler, run_on_main_loop, self)

