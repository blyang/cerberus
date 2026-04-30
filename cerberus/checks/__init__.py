"""Importing this package registers all checks."""
from . import a_rendering  # noqa: F401
from . import b_indexability  # noqa: F401
from . import c_semantics  # noqa: F401
from . import d_structured_data  # noqa: F401
from . import e_performance  # noqa: F401
from . import f_discoverability  # noqa: F401
from . import g_intl  # noqa: F401
from . import h_serving  # noqa: F401
from .base import all_checks, find_check  # noqa: F401
