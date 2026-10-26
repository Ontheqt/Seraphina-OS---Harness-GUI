"""The test suite stays inside itself.

- urllib requests to any host other than loopback fail at once with a URLError, as an unreachable host would.
  Without this, doctor tests that use the shipped hosted presets (DeepSeek, NanoGPT) sent real requests through
  whatever proxy the environment set.
- The look-around for model servers (listeners.model_servers, GET /v1/models to every listener; only the owner's
  look calls it: POST /api/setup/look-around, `factory doctor --look-around`) finds nothing unless a test hands it
  its own listener table or its own finder, so it never probes what happens to listen on the test machine (an HTTP
  proxy, other services)."""
import urllib.error
import urllib.parse
import urllib.request

from swfactory import listeners
from swfactory.settings import is_local_url

_open = urllib.request.OpenerDirector.open


def _local_only(self, fullurl, data=None, timeout=None, **kwargs):
    url = fullurl.full_url if isinstance(fullurl, urllib.request.Request) else str(fullurl)
    if urllib.parse.urlsplit(url).scheme in ("http", "https") and not is_local_url(url):
        raise urllib.error.URLError("the test suite blocks the outside network")
    if timeout is None:
        return _open(self, fullurl, data, **kwargs)
    return _open(self, fullurl, data, timeout, **kwargs)


urllib.request.OpenerDirector.open = _local_only


_model_servers = listeners.model_servers


def _own_table_only(skip_ports=frozenset(), proc_net=None, *args, **kwargs):
    if proc_net is None:
        return []
    return _model_servers(skip_ports, proc_net, *args, **kwargs)


listeners.model_servers = _own_table_only
