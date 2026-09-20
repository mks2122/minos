"""L2: typed, application-aware adapters.

The tier that earns its place. An L2 adapter knows the shape of the thing it is
editing, so it can declare a contract worth verifying -- "B14 becomes 48200"
rather than "the file changed" or "a click landed somewhere".
"""

from .tabular import CellOracle, CsvBackend, TabularAdapter, register_backend

__all__ = ["CellOracle", "CsvBackend", "TabularAdapter", "register_backend"]
