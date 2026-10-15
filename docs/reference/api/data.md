# Data and connectors

Your files and systems: file profiles, connectors to external systems, and where awa keeps state.

## Example

Profile a CSV file before anything is built on it (no model is called):

```python title="data_example.py"
import tempfile
from pathlib import Path

from awa.data import profile

folder = Path(tempfile.mkdtemp())
rows = ["invoice,amount,due", "INV-1,120.50,2026-11-01", "INV-2,99.00,2026-11-15"]
(folder / "invoices.csv").write_text("\n".join(rows) + "\n", encoding="utf-8")
found = profile(folder / "invoices.csv", root=folder)
for file in found.files:
    print(file.path, file.kind)
    for table in file.tables:
        for column in table.columns:
            print(" ", column.name, column.type)
```

It prints:

```text title="data_example.out"
invoices.csv csv
  invoice text
  amount decimal
  due date
```

::: awa.data
    options:
      show_root_full_path: true

::: awa.connectors
    options:
      show_root_full_path: true

::: awa.persistence
    options:
      show_root_full_path: true
