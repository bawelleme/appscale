""" This service starts and stops application servers of a given application. """

import json
import logging
import math
import os
import random
import re
import signal
import urllib
import urllib2

import psutil
import tornado.web
from concurrent.futures import ThreadPoolExecutor
from kazoo.client import KazooClient
from kazoo.exceptions import NodeExistsError, NoNodeError
from tornado import gen
from tornado.escape import json_decode
from tornado.httpclient import AsyncHTTPClient
from tornado.httpclient import HTTPClient
from tornado.httpclient import HTTPError
from tornado.ioloop import IOLoop, PeriodicCallback
from tornado.options import options

from appscale.admin.constants import UNPACK_ROOT
from appscale.admin.instance_manager.constants import (
  APP_LOG_SIZE,
  DASHBOARD_LOG_SIZE,
  DASHBOARD_PROJECT_ID,
  DEFAULT_MAX_APPSERVER_MEMORY,
  INSTANCE_CLASSES,
  JAVA_APPSERVER_CLASS,
  LOGROTATE_CONFIG_DIR,
  MAX_BACKGROUND_WORKERS,
  MAX_INSTANCE_RESPONSE_TIME,
  MONIT_INSTANCE_PREFIX,
  PYTHON_APPSERVER
)
from appscale.admin.instance_manager.projects_manager import (
  GlobalProjectsManager)
from appscale.admin.instance_manager.source_manager import SourceManager
from appscale.admin.instance_manager.stop_instance import stop_instance
from appscale.admin.instance_manager.utils import find_web_inf
from appscale.common import (
  appscale_info,
  constants,
  file_io,
  monit_app_configuration,
  monit_interface,
  misc
)
from appscale.common.constants import HTTPCodes
from appscale.common.constants import PID_DIR
from appscale.common.constants import MonitStates
from appscale.common.constants import VERSION_PATH_SEPARATOR
from appscale.common.deployment_config import ConfigInaccessible
from appscale.common.deployment_config import DeploymentConfig
from appscale.common.monit_interface import MonitOperator
from appscale.common.monit_interface import ProcessNotFound
from appscale.hermes.constants import HERMES_PORT


# The location of the API server start script.
API_SERVER_LOCATION = os.path.join('/', 'opt', 'appscale_api_server', 'bin',
                                   'appscale-api-server')

# The Monit watch prefix for API servers.
API_SERVER_PREFIX = 'api-server_'

# The amount of seconds to wait for an application to start up.
START_APP_TIMEOUT = 180

# The amount of seconds to wait between checking if an application is up.
BACKOFF_TIME = 1

# The PID number to return when a process did not start correctly
BAD_PID = -1

# Default hourly cron directory.
CRON_HOURLY = '/etc/cron.hourly'

# The web path to fetch to see if the application is up
FETCH_PATH = '/_ah/health_check'

# The number of seconds to wait between checking for failed instances.
INSTANCE_CLEANUP_INTERVAL = 30

# The ZooKeeper node that keeps track of running AppServers by version.
VERSION_REGISTRATION_NODE = '/appscale/instances_by_version'

# Apps which can access any application's data.
TRUSTED_APPS = ["appscaledashboard"]

# The flag to tell the application server that this application can access
# all application data.
TRUSTED_FLAG = "--trusted"

# The location on the filesystem where the PHP executable is installed.
PHP_CGI_LOCATION = "/usr/bin/php-cgi"

# The location of the App Engine SDK for Go.
GO_SDK = os.path.join('/', 'opt', 'go_appengine')

HTTP_OK = 200

# The highest available port to assign to an API server.
MAX_API_SERVER_PORT = 19999

# The amount of seconds to wait before retrying to add routing.
ROUTING_RETRY_INTERVAL = 5

PIDFILE_TEMPLATE = os.path.join('/', 'var', 'run', 'appscale',
                                'app___{revision}-{port}.pid')

# A listing of active API servers.
api_servers = {}

# A DeploymentConfig accessor.
deployment_config = None

# A GlobalProjectsManager watch.
projects_manager = None

# An interface for working with Monit.
monit_operator = MonitOperator()

# The AppServer instances running on this machine.
running_instances = set()

# Fetches, extracts, and keeps track of revision source code.
source_manager = None

# Allows synchronous code to be executed in the background.
thread_pool = None

# A KazooClient.
zk_client = None


class Instance(object):
  """ Represents an AppServer instance. """
  __slots__ = ['revision_key', 'port']
  def __init__(self, revision_key, port):
    self.revision_key = revision_key
    self.port = port

  @property
  def version_key(self):
    revision_parts = self.revision_key.split(VERSION_PATH_SEPARATOR)
    return VERSION_PATH_SEPARATOR.join(revision_parts[:3])

  @property
  def revision(self):
    return self.revision_key.split(VERSION_PATH_SEPARATOR)[-1]

  def __eq__(self, other):
    return self.revision_key == other.revision_key and self.port == other.port

  def __repr__(self):
    return '<Instance: {}:{}>'.format(self.revision_key, self.port)

  def __hash__(self):
    return hash((self.revision_key, self.port))


class BadConfigurationException(Exception):
  """ An application is configured incorrectly. """
  def __init__(self, value):
    Exception.__init__(self, value)
    self.value = value

  def __str__(self):
    return repr(self.value)


class NoRedirection(urllib2.HTTPErrorProcessor):
  """ A url opener that does not automatically redirect. """
  def http_response(self, request, response):
    """ Processes HTTP responses.

    Args:
      request: An HTTP request object.
      response: An HTTP response object.
    Returns:
      The HTTP response object.
    """
    return response
  https_response = http_response


@gen.coroutine
def add_routing(instance):
  """ Tells the AppController to begin routing traffic to an AppServer.

  Args:
    instance: An Instance.
  """
  logging.info('Waiting for {}'.format(instance))
  start_successful = yield wait_on_app(instance.port)
  if not start_successful:
    # In case the AppServer fails we let the AppController to detect it
    # and remove it if it still show in monit.
    logging.warning('{} did not come up in time'.format(instance))
    raise gen.Return()

  register_instance(instance)


def ensure_api_server(project_id):
  """ Make sure there is a running API server for a project.

  Args:
    project_id: A string specifying the project ID.
  Returns:
    An integer specifying the API server port.
  """
  global api_servers
  if project_id in api_servers:
    return api_servers[project_id]

  server_port = MAX_API_SERVER_PORT
  for port in api_servers.values():
    if port <= server_port:
      server_port = port - 1

  zk_locations = appscale_info.get_zk_node_ips()
  start_cmd = ' '.join([API_SERVER_LOCATION,
                        '--port', str(server_port),
                        '--project-id', project_id,
                        '--zookeeper-locations', ' '.join(zk_locations)])

  watch = ''.join([API_SERVER_PREFIX, project_id])
  full_watch = '-'.join([watch, str(server_port)])
  pidfile = os.path.join(PID_DIR, '{}.pid'.format(full_watch))
  monit_app_configuration.create_config_file(
    watch,
    start_cmd,
    pidfile,
    server_port,
    max_memory=DEFAULT_MAX_APPSERVER_MEMORY,
    check_port=True)

  assert monit_interface.start(full_watch, is_group=False), (
    'Monit was unable to start {}'.format(watch))

  api_servers[project_id] = server_port
  return server_port


@gen.coroutine
def stop_api_server(project_id):
  """ Make sure there is not a running API server for a project.

  Args:
    project_id: A string specifying the project ID.
  """
  global api_servers
  if project_id not in api_servers:
    return

  port = api_servers[project_id]
  watch = '{}{}-{}'.format(API_SERVER_PREFIX, project_id, port)
  yield unmonitor_and_terminate(watch)
  del api_servers[project_id]


@gen.coroutine
def populate_api_servers():
  """ Find running API servers. """
  def api_server_info(entry):
    prefix, port = entry.rsplit('-', 1)
    project_id = prefix[len(API_SERVER_PREFIX):]
    return project_id, int(port)

  global api_servers
  monit_entries = yield monit_operator.get_entries()
  server_entries = [api_server_info(entry) for entry in monit_entries
                    if entry.startswith(API_SERVER_PREFIX)]

  for project_id, port in server_entries:
    api_servers[project_id] = port


@gen.coroutine
def start_app(version_key, config):
  """ Starts a Google App Engine application on this machine. It
      will start it up and then proceed to fetch the main page.

  Args:
    version_key: A string specifying a version key.
    config: a dictionary that contains
      app_port: An integer specifying the port to use.
      login_server: The server address the AppServer will use for login urls.
  """
  if 'app_port' not in config:
    raise BadConfigurationException('app_port is required')
  if 'login_server' not in config or not config['login_server']:
    raise BadConfigurationException('login_server is required')

  login_server = config['login_server']

  project_id, service_id, version_id = version_key.split(
    VERSION_PATH_SEPARATOR)

  if not misc.is_app_name_valid(project_id):
    raise BadConfigurationException(
      'Invalid project ID: {}'.format(project_id))

  try:
    service_manager = projects_manager[project_id][service_id]
    version_details = service_manager[version_id].version_details
  except KeyError:
    raise BadConfigurationException('Version not found')

  runtime = version_details['runtime']
  env_vars = version_details.get('envVariables', {})
  runtime_params = deployment_config.get_config('runtime_parameters')
  max_memory = runtime_params.get('default_max_appserver_memory',
                                  DEFAULT_MAX_APPSERVER_MEMORY)
  if 'instanceClass' in version_details:
    max_memory = INSTANCE_CLASSES.get(version_details['instanceClass'],
                                      max_memory)

  revision_key = VERSION_PATH_SEPARATOR.join(
    [project_id, service_id, version_id, str(version_details['revision'])])
  source_archive = version_details['deployment']['zip']['sourceUrl']

  api_server_port = ensure_api_server(project_id)
  yield source_manager.ensure_source(revision_key, source_archive, runtime)

  logging.info('Starting {} application {}'.format(runtime, project_id))

  pidfile = PIDFILE_TEMPLATE.format(revision=revision_key,
                                    port=config['app_port'])

  if runtime == constants.GO:
    env_vars['GOPATH'] = os.path.join(UNPACK_ROOT, revision_key, 'gopath')
    env_vars['GOROOT'] = os.path.join(GO_SDK, 'goroot')

  watch = ''.join([MONIT_INSTANCE_PREFIX, revision_key])
  if runtime in (constants.PYTHON27, constants.GO, constants.PHP):
    start_cmd = create_python27_start_cmd(
      project_id,
      login_server,
      config['app_port'],
      pidfile,
      revision_key,
      api_server_port)
    env_vars.update(create_python_app_env(
      login_server,
      project_id))
  elif runtime == constants.JAVA:
    # Account for MaxPermSize (~170MB), the parent process (~50MB), and thread
    # stacks (~20MB).
    max_heap = max_memory - 250
    if max_heap <= 0:
      raise BadConfigurationException(
        'Memory for Java applications must be greater than 250MB')

    start_cmd = create_java_start_cmd(
      project_id,
      config['app_port'],
      login_server,
      max_heap,
      pidfile,
      revision_key,
      api_server_port
    )

    env_vars.update(create_java_app_env(project_id))
  else:
    raise BadConfigurationException(
      'Unknown runtime {} for {}'.format(runtime, project_id))

  logging.info("Start command: " + str(start_cmd))
  logging.info("Environment variables: " + str(env_vars))

  monit_app_configuration.create_config_file(
    watch,
    start_cmd,
    pidfile,
    config['app_port'],
    env_vars,
    max_memory,
    options.syslog_server,
    check_port=True,
    kill_exceeded_memory=True)

  # We want to tell monit to start the single process instead of the
  # group, since monit can get slow if there are quite a few processes in
  # the same group.
  full_watch = '{}-{}'.format(watch, config['app_port'])
  assert monit_interface.start(full_watch, is_group=False), (
    'Monit was unable to start {}:{}'.format(project_id, config['app_port']))

  # Make sure the version node exists.
  zk_client.ensure_path('/'.join([VERSION_REGISTRATION_NODE, version_key]))

  # Since we are going to wait, possibly for a long time for the
  # application to be ready, we do it later.
  IOLoop.current().spawn_callback(add_routing,
                                  Instance(revision_key, config['app_port']))

  if project_id == DASHBOARD_PROJECT_ID:
    log_size = DASHBOARD_LOG_SIZE
  else:
    log_size = APP_LOG_SIZE

  if not setup_logrotate(project_id, log_size):
    logging.error("Error while setting up log rotation for application: {}".
      format(project_id))


def setup_logrotate(app_name, log_size):
  """ Creates a logrotate script for the logs that the given application
      will create.

  Args:
    app_name: A string, the application ID.
    log_size: An integer, the size of logs that are kept per application server.
      The size should be in bytes.
  Returns:
    True on success, False otherwise.
  """
  # Write application specific logrotation script.
  app_logrotate_script = "{0}/appscale-{1}".\
    format(LOGROTATE_CONFIG_DIR, app_name)

  log_prefix = ''.join([MONIT_INSTANCE_PREFIX, app_name])

  # Application logrotate script content.
  contents = """/var/log/appscale/{log_prefix}*.log {{
  size {size}
  missingok
  rotate 7
  compress
  delaycompress
  notifempty
  copytruncate
}}
""".format(log_prefix=log_prefix, size=log_size)
  logging.debug("Logrotate file: {} - Contents:\n{}".
    format(app_logrotate_script, contents))

  with open(app_logrotate_script, 'w') as app_logrotate_fd:
    app_logrotate_fd.write(contents)

  return True


def unmonitor(process_name, retries=5):
  """ Unmonitors a process.

  Args:
    process_name: A string specifying the process to stop monitoring.
    retries: An integer specifying the number of times to retry the operation.
  """
  client = HTTPClient()
  process_url = '{}/{}'.format(monit_operator.LOCATION, process_name)
  payload = urllib.urlencode({'action': 'unmonitor'})
  try:
    client.fetch(process_url, method='POST', body=payload)
  except HTTPError as error:
    if error.code == 404:
      raise ProcessNotFound('{} not listed by Monit'.format(process_name))

    if error.code == 503:
      retries -= 1
      if retries < 0:
        raise

      return unmonitor(process_name, retries)

    raise


@gen.coroutine
def clean_old_sources():
  """ Removes source code for obsolete revisions. """
  monit_entries = yield monit_operator.get_entries()
  active_revisions = {
    entry[len(MONIT_INSTANCE_PREFIX):].rsplit('-', 1)[0]
    for entry in monit_entries
    if entry.startswith(MONIT_INSTANCE_PREFIX)}

  for project_id, project_manager in projects_manager.items():
    for service_id, service_manager in project_manager.items():
      for version_id, version_manager in service_manager.items():
        revision_id = version_manager.version_details['revision']
        revision_key = VERSION_PATH_SEPARATOR.join(
          [project_id, service_id, version_id, str(revision_id)])
        active_revisions.add(revision_key)

  source_manager.clean_old_revisions(active_revisions=active_revisions)


@gen.coroutine
def unmonitor_and_terminate(watch):
  """ Unmonitors an instance and terminates it.

  Args:
    watch: A string specifying the Monit entry.
  """
  try:
    unmonitor(watch)
  except ProcessNotFound:
    # If Monit does not know about a process, assume it is already stopped.
    return

  # Now that the AppServer is stopped, remove its monit config file so that
  # monit doesn't pick it up and restart it.
  monit_operator.remove_configuration(watch)

  IOLoop.current().spawn_callback(stop_instance, watch,
                                  MAX_INSTANCE_RESPONSE_TIME)


@gen.coroutine
def stop_app_instance(version_key, port):
  """ Stops a Google App Engine application process instance on current
      machine.

  Args:
    version_key: A string, the name of version to stop.
    port: The port the application is running on.
  Returns:
    True on success, False otherwise.
  """
  project_id = version_key.split(VERSION_PATH_SEPARATOR)[0]

  if not misc.is_app_name_valid(project_id):
    raise BadConfigurationException(
      'Invalid project ID: {}'.format(project_id))

  logging.info('Stopping {}:{}'.format(version_key, port))

  # Discover revision key from version and port.
  instance_key_re = re.compile(
    '{}{}.*-{}'.format(MONIT_INSTANCE_PREFIX, version_key, port))
  monit_entries = yield monit_operator.get_entries()
  try:
    watch = next(entry for entry in monit_entries
                 if instance_key_re.match(entry))
  except StopIteration:
    message = 'No entries exist for {}:{}'.format(version_key, port)
    raise HTTPError(HTTPCodes.INTERNAL_ERROR, message=message)

  revision_key, port = watch[len(MONIT_INSTANCE_PREFIX):].rsplit('-', 1)
  port = int(port)
  unregister_instance(Instance(revision_key, port))
  yield unmonitor_and_terminate(watch)

  project_prefix = ''.join([MONIT_INSTANCE_PREFIX, project_id])
  remaining_instances = [entry for entry in monit_entries
                         if entry.startswith(project_prefix)
                         and not instance_key_re.match(entry)]
  if not remaining_instances:
    yield stop_api_server(project_id)

  yield monit_operator.reload()
  yield clean_old_sources()


@gen.coroutine
def stop_app(version_key):
  """ Stops all process instances of a version on this machine.

  Args:
    version_key: Name of version to stop
  Returns:
    True on success, False otherwise
  """
  project_id = version_key.split(VERSION_PATH_SEPARATOR)[0]

  if not misc.is_app_name_valid(project_id):
    raise BadConfigurationException(
      'Invalid project ID: {}'.format(project_id))

  logging.info('Stopping {}'.format(version_key))

  version_group = ''.join([MONIT_INSTANCE_PREFIX, version_key])
  monit_entries = yield monit_operator.get_entries()
  version_entries = [entry for entry in monit_entries
                     if entry.startswith(version_group)]
  for entry in version_entries:
    revision_key, port = entry[len(MONIT_INSTANCE_PREFIX):].rsplit('-', 1)
    port = int(port)
    unregister_instance(Instance(revision_key, port))
    yield unmonitor_and_terminate(entry)

  project_prefix = ''.join([MONIT_INSTANCE_PREFIX, project_id])
  remaining_instances = [entry for entry in monit_entries
                         if entry.startswith(project_prefix)
                         and entry not in version_entries]
  if not remaining_instances:
    yield stop_api_server(project_id)

  if project_id not in projects_manager and not remove_logrotate(project_id):
    logging.error("Error while removing log rotation for application: {}".
                  format(project_id))

  yield monit_operator.reload()
  yield clean_old_sources()


def remove_logrotate(app_name):
  """ Removes logrotate script for the given application.

  Args:
    app_name: A string, the name of the application to remove logrotate for.
  Returns:
    True on success, False otherwise.
  """
  app_logrotate_script = "{0}/appscale-{1}".\
    format(LOGROTATE_CONFIG_DIR, app_name)
  logging.debug("Removing script: {}".format(app_logrotate_script))

  try:
    os.remove(app_logrotate_script)
  except OSError:
    logging.error("Error deleting {0}".format(app_logrotate_script))
    return False

  return True


############################################
# Private Functions (but public for testing)
############################################
@gen.coroutine
def wait_on_app(port):
  """ Waits for the application hosted on this machine, on the given port,
      to respond to HTTP requests.

  Args:
    port: Port where app is hosted on the local machine
  Returns:
    True on success, False otherwise
  """
  retries = math.ceil(START_APP_TIMEOUT / BACKOFF_TIME)

  url = "http://" + options.private_ip + ":" + str(port) + FETCH_PATH
  while retries > 0:
    try:
      opener = urllib2.build_opener(NoRedirection)
      response = opener.open(url)
      if response.code != HTTP_OK:
        logging.warning('{} returned {}. Headers: {}'.
          format(url, response.code, response.headers.headers))
      raise gen.Return(True)
    except IOError:
      retries -= 1

    yield gen.sleep(BACKOFF_TIME)

  logging.error('Application did not come up on {} after {} seconds'.
    format(url, START_APP_TIMEOUT))
  raise gen.Return(False)


def create_python_app_env(public_ip, app_name):
  """ Returns the environment variables the python application server uses.

  Args:
    public_ip: The public IP of the load balancer
    app_name: The name of the application to be run
  Returns:
    A dictionary containing the environment variables
  """
  env_vars = {}
  env_vars['MY_IP_ADDRESS'] = public_ip
  env_vars['APPNAME'] = app_name
  env_vars['GOMAXPROCS'] = appscale_info.get_num_cpus()
  env_vars['APPSCALE_HOME'] = constants.APPSCALE_HOME
  env_vars['PYTHON_LIB'] = "{0}/AppServer/".format(constants.APPSCALE_HOME)
  return env_vars


def create_java_app_env(app_name):
  """ Returns the environment variables Java application servers uses.

  Args:
    app_name: A string containing the application ID.
  Returns:
    A dictionary containing the environment variables
  """
  env_vars = {'APPSCALE_HOME': constants.APPSCALE_HOME}

  gcs_config = {'scheme': 'https', 'port': 443}
  try:
    gcs_config.update(deployment_config.get_config('gcs'))
  except ConfigInaccessible:
    logging.warning('Unable to fetch GCS configuration.')

  if 'host' in gcs_config:
    env_vars['GCS_HOST'] = '{scheme}://{host}:{port}'.format(**gcs_config)

  return env_vars


def create_python27_start_cmd(app_name, login_ip, port, pidfile, revision_key,
                              api_server_port):
  """ Creates the start command to run the python application server.

  Args:
    app_name: The name of the application to run
    login_ip: The public IP of this deployment
    port: The local port the application server will bind to
    pidfile: A string specifying the pidfile location.
    revision_key: A string specifying the revision key.
    api_server_port: An integer specifying the port of the external API server.
  Returns:
    A string of the start command.
  """
  source_directory = os.path.join(UNPACK_ROOT, revision_key, 'app')

  cmd = [
    "/usr/bin/python2",
    constants.APPSCALE_HOME + "/AppServer/dev_appserver.py",
    "--application", app_name,
    "--port " + str(port),
    "--admin_port " + str(port + 10000),
    "--login_server " + login_ip,
    "--skip_sdk_update_check",
    "--nginx_host " + str(login_ip),
    "--require_indexes",
    "--enable_sendmail",
    "--xmpp_path " + login_ip,
    "--php_executable_path=" + str(PHP_CGI_LOCATION),
    "--uaserver_path " + options.db_proxy + ":"\
      + str(constants.UA_SERVER_PORT),
    "--datastore_path " + options.db_proxy + ":"\
      + str(constants.DB_SERVER_PORT),
    source_directory,
    "--host " + options.private_ip,
    "--admin_host " + options.private_ip,
    "--automatic_restart", "no",
    "--pidfile", pidfile,
    "--external_api_port", str(api_server_port)]

  if app_name in TRUSTED_APPS:
    cmd.extend([TRUSTED_FLAG])

  return ' '.join(cmd)


def locate_dir(path, dir_name):
  """ Locates a directory inside the given path.

  Args:
    path: The path to be searched
    dir_name: The directory we are looking for

  Returns:
    The absolute path of the directory we are looking for, None otherwise.
  """
  paths = []

  for root, sub_dirs, files in os.walk(path):
    for sub_dir in sub_dirs:
      if dir_name == sub_dir:
        result = os.path.abspath(os.path.join(root, sub_dir))
        if sub_dir == "WEB-INF":
          logging.info("Found WEB-INF/ at: {0}".format(result))
          paths.append(result)
        elif sub_dir == "lib" and result.count(os.sep) <= path.count(os.sep) + 2 \
            and result.endswith("/WEB-INF/{0}".format(sub_dir)):
          logging.info("Found lib/ at: {0}".format(result))
          paths.append(result)

  if len(paths) > 0:
    sorted_paths = sorted(paths, key = lambda s: len(s))
    return sorted_paths[0]
  else:
    return None


def create_java_start_cmd(app_name, port, load_balancer_host, max_heap,
                          pidfile, revision_key, api_server_port):
  """ Creates the start command to run the java application server.

  Args:
    app_name: The name of the application to run
    port: The local port the application server will bind to
    load_balancer_host: The host of the load balancer
    max_heap: An integer specifying the max heap size in MB.
    pidfile: A string specifying the pidfile location.
    revision_key: A string specifying the revision key.
    api_server_port: An integer specifying the port of the external API server.
  Returns:
    A string of the start command.
  """
  java_start_script = os.path.join(
    constants.JAVA_APPSERVER, 'appengine-java-sdk-repacked', 'bin',
    'dev_appserver.sh')
  revision_base = os.path.join(UNPACK_ROOT, revision_key)
  web_inf_directory = find_web_inf(revision_base)

  # The Java AppServer needs the NGINX_PORT flag set so that it will read the
  # local FS and see what port it's running on. The value doesn't matter.
  cmd = [
    java_start_script,
    "--port=" + str(port),
    #this jvm flag allows javax.email to connect to the smtp server
    "--jvm_flag=-Dsocket.permit_connect=true",
    '--jvm_flag=-Xmx{}m'.format(max_heap),
    '--jvm_flag=-Djava.security.egd=file:/dev/./urandom',
    '--jvm_flag=-Djdk.tls.client.protocols=TLSv1.1,TLSv1.2',
    "--disable_update_check",
    "--address=" + options.private_ip,
    "--datastore_path=" + options.db_proxy,
    "--login_server=" + load_balancer_host,
    "--appscale_version=1",
    "--APP_NAME=" + app_name,
    "--NGINX_ADDRESS=" + load_balancer_host,
    "--TQ_PROXY=" + options.tq_proxy,
    "--pidfile={}".format(pidfile),
    "--external_api_port={}".format(api_server_port),
    "--api_using_python_stub=app_identity_service",
    os.path.dirname(web_inf_directory)
  ]

  return ' '.join(cmd)


def unregister_instance(instance):
  """ Removes a registration entry for an instance.

  Args:
    instance: An Instance.
  """
  instance_entry = ':'.join([options.private_ip, str(instance.port)])
  instance_node = '/'.join([VERSION_REGISTRATION_NODE, instance.version_key,
                            instance_entry])

  try:
    zk_client.delete(instance_node)
  except NoNodeError:
    pass

  try:
    running_instances.remove(instance)
  except KeyError:
    logging.info('unregister_instance: non-existent instance {}'.format(instance))


def register_instance(instance):
  """ Adds a registration entry for an instance.

  Args:
    instance: An Instance.
  """
  instance_entry = ':'.join([options.private_ip, str(instance.port)])
  instance_node = '/'.join([VERSION_REGISTRATION_NODE, instance.version_key,
                            instance_entry])

  try:
    zk_client.create(instance_node, instance.revision.encode('utf-8'))
  except NodeExistsError:
    zk_client.set(instance_node, instance.revision.encode('utf-8'))

  running_instances.add(instance)


def declare_instance_nodes(running_instances, zk_client):
  """ Removes dead ZooKeeper instance entries and adds running ones.

  Args:
    running_instances: An iterable of Instances.
    zk_client: A KazooClient.
  """
  registered_instances = set()
  for version_key in zk_client.get_children(VERSION_REGISTRATION_NODE):
    version_node = '/'.join([VERSION_REGISTRATION_NODE, version_key])
    for instance_entry in zk_client.get_children(version_node):
      machine_ip = instance_entry.split(':')[0]
      if machine_ip != options.private_ip:
        continue

      port = int(instance_entry.split(':')[-1])
      instance_node = '/'.join([version_node, instance_entry])
      revision = zk_client.get(instance_node)[0]
      revision_key = VERSION_PATH_SEPARATOR.join([version_key, revision])
      registered_instances.add(Instance(revision_key, port))

  # Remove outdated nodes.
  for instance in registered_instances - running_instances:
    unregister_instance(instance)

  # Add nodes for running instances.
  for instance in running_instances - registered_instances:
    register_instance(instance)


def clean_up_instances(monit_entries):
  """ Terminates instances that aren't accounted for.

  Args:
    monit_entries: A list of dictionaries containing instance details.
  """
  monitored = {(entry['revision'], entry['port']) for entry in monit_entries}
  to_stop = []
  for process in psutil.process_iter():
    cmd = process.cmdline()
    if len(cmd) < 2:
      continue

    if JAVA_APPSERVER_CLASS in cmd:
      revision = cmd[-1].split(os.sep)[-2]
      port_arg = next(arg for arg in cmd if arg.startswith('--port='))
      port = int(port_arg.split('=')[-1])
    elif cmd[1] == PYTHON_APPSERVER:
      source_arg = next(arg for arg in cmd
                        if arg.startswith(constants.APPS_PATH))
      revision = source_arg.split(os.sep)[-2]
      port = int(cmd[cmd.index('--port') + 1])
    else:
      continue

    if (revision, port) not in monitored:
      to_stop.append(process)

  if not to_stop:
    return

  logging.info('Killing {} unmonitored instances'.format(len(to_stop)))
  for process in to_stop:
    group = os.getpgid(process.pid)
    os.killpg(group, signal.SIGKILL)


def recover_state(zk_client):
  """ Establishes current state from Monit entries.

  Args:
    zk_client: A KazooClient.
  Returns:
    A set of Instances.
  """
  logging.info('Getting current state')
  monit_entries = monit_operator.get_entries_sync()
  instance_entries = {entry: state for entry, state in monit_entries.items()
                      if entry.startswith(MONIT_INSTANCE_PREFIX)}

  # Remove all unmonitored entries.
  removed = []
  for entry, state in instance_entries.items():
    if state == MonitStates.UNMONITORED:
      monit_operator.remove_configuration(entry)
      removed.append(entry)

  for entry in removed:
    del instance_entries[entry]

  if removed:
    monit_operator.reload_sync()

  instance_details = []
  for entry, state in instance_entries.items():
    revision, port = entry[len(MONIT_INSTANCE_PREFIX):].rsplit('-', 1)
    instance_details.append(
      {'revision': revision, 'port': int(port), 'state': state})

  clean_up_instances(instance_details)

  # Ensure version nodes exist.
  running_versions = {'_'.join(instance['revision'].split('_')[:3])
                      for instance in instance_details}
  zk_client.ensure_path(VERSION_REGISTRATION_NODE)
  for version_key in running_versions:
    zk_client.ensure_path('/'.join([VERSION_REGISTRATION_NODE, version_key]))

  # Account for monitored instances.
  running_instances = {
    Instance(instance['revision'], instance['port'])
    for instance in instance_details}
  declare_instance_nodes(running_instances, zk_client)
  return running_instances


@gen.coroutine
def get_failed_instances():
  """ Fetches a list of failed instances on this machine according to HAProxy.

  Returns:
    A set of tuples specifying the version key and port of failed instances.
  """
  load_balancer = random.choice(appscale_info.get_load_balancer_ips())
  payload = {'include_lists': {
    'proxy': ['name', 'servers'],
    'proxy.server': ['private_ip', 'port', 'status']}
  }
  headers = {'AppScale-Secret': options.secret}
  url = 'http://{}:{}/stats/local/proxies'.format(load_balancer, HERMES_PORT)
  client = AsyncHTTPClient()

  response = yield client.fetch(url, headers=headers, body=json.dumps(payload),
                                allow_nonstandard_methods=True)
  proxy_stats = json.loads(response.body)['proxies_stats']

  routed_versions = [server for server in proxy_stats
                     if server['name'].startswith('gae_')]
  failed_instances = set()
  for version in routed_versions:
    version_key = version['name'][len('gae_'):]
    for server in version['servers']:
      if server['private_ip'] != options.private_ip:
        continue

      if not server['status'].startswith('DOWN'):
        continue

      failed_instances.add((version_key, server['port']))

  raise gen.Return(failed_instances)


@gen.coroutine
def stop_failed_instances():
  """ Stops AppServer instances that HAProxy considers to be unavailable. """
  failed_instances = yield get_failed_instances()
  for instance in running_instances:
    if (instance.version_key, instance.port) in failed_instances:
      yield stop_app_instance(instance.version_key, instance.port)


class VersionHandler(tornado.web.RequestHandler):
  """ Handles requests to start and stop instances for a project. """
  @gen.coroutine
  def post(self, version_key):
    """ Starts an AppServer instance on this machine.

    Args:
      version_key: A string specifying a version key.
    """
    try:
      config = json_decode(self.request.body)
    except ValueError:
      raise HTTPError(HTTPCodes.BAD_REQUEST, 'Payload must be valid JSON')

    try:
      yield start_app(version_key, config)
    except BadConfigurationException as error:
      raise HTTPError(HTTPCodes.BAD_REQUEST, error.message)

  @staticmethod
  @gen.coroutine
  def delete(version_key):
    """ Stops all instances on this machine for a version.

    Args:
      version_key: A string specifying a version key.
    """
    try:
      yield stop_app(version_key)
    except BadConfigurationException as error:
      raise HTTPError(HTTPCodes.BAD_REQUEST, error.message)


class InstanceHandler(tornado.web.RequestHandler):
  """ Handles requests to stop individual instances. """

  @staticmethod
  @gen.coroutine
  def delete(version_key, port):
    """ Stops an AppServer instance on this machine. """
    try:
      yield stop_app_instance(version_key, int(port))
    except BadConfigurationException as error:
      raise HTTPError(HTTPCodes.BAD_REQUEST, error.message)


################################
# MAIN
################################
if __name__ == "__main__":
  file_io.set_logging_format()
  logging.getLogger().setLevel(logging.INFO)

  zk_ips = appscale_info.get_zk_node_ips()
  zk_client = KazooClient(hosts=','.join(zk_ips))
  zk_client.start()

  deployment_config = DeploymentConfig(zk_client)
  projects_manager = GlobalProjectsManager(zk_client)
  thread_pool = ThreadPoolExecutor(MAX_BACKGROUND_WORKERS)
  source_manager = SourceManager(zk_client, thread_pool)
  source_manager.configure_automatic_fetch(projects_manager)

  options.define('private_ip', appscale_info.get_private_ip())
  options.define('syslog_server', appscale_info.get_headnode_ip())
  options.define('db_proxy', appscale_info.get_db_proxy())
  options.define('tq_proxy', appscale_info.get_tq_proxy())
  options.define('secret', appscale_info.get_secret())

  running_instances = recover_state(zk_client)
  PeriodicCallback(stop_failed_instances,
                   INSTANCE_CLEANUP_INTERVAL * 1000).start()

  app = tornado.web.Application([
    ('/versions/([a-z0-9-_]+)', VersionHandler),
    ('/versions/([a-z0-9-_]+)/([0-9-]+)', InstanceHandler)
  ])

  app.listen(constants.APP_MANAGER_PORT)
  logging.info('Starting AppManager on {}'.format(constants.APP_MANAGER_PORT))

  io_loop = IOLoop.current()
  io_loop.run_sync(populate_api_servers)
  io_loop.start()
