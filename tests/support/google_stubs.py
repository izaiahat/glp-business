"""Scoped, synthetic Google imports for offline tests only."""
from contextlib import contextmanager
from functools import wraps
import sys
import types


def _google(name):
    return name == 'google' or name.startswith('google.') or name == 'googleapiclient' or name.startswith('googleapiclient.')


@contextmanager
def google_import_stubs():
    saved = {name: value for name, value in sys.modules.items() if _google(name)}
    try:
        for name in list(sys.modules):
            if _google(name): del sys.modules[name]
        names = ('google', 'google.auth', 'google.auth.transport', 'google.auth.transport.requests',
                 'google.oauth2', 'google.oauth2.credentials', 'googleapiclient', 'googleapiclient.discovery')
        for name in names:
            module = types.ModuleType(name)
            module.__path__ = []
            sys.modules[name] = module
            parent, _, attr = name.rpartition('.')
            if parent: setattr(sys.modules[parent], attr, module)
        sys.modules['google.auth.transport.requests'].Request = type('Request', (), {})
        sys.modules['google.oauth2.credentials'].Credentials = type('Credentials', (), {})
        sys.modules['googleapiclient.discovery'].build = lambda *a, **kw: None
        yield
    finally:
        for name in list(sys.modules):
            if _google(name): del sys.modules[name]
        sys.modules.update(saved)


def with_google_import_stubs(function):
    @wraps(function)
    def invoke(*args, **kwargs):
        with google_import_stubs():
            return function(*args, **kwargs)
    return invoke
