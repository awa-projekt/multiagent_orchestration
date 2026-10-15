# Microsoft 365 processes

Six process examples on the connectors `outlook`, `sharepoint` and the data tools, one folder per
area. Each process folder holds a `blueprint.yaml` (the template) and a `cases.yaml` (the eval
cases); the schedules are switched off. Status: offline-proven against a fake Microsoft Graph, not
live-proven.

| Area folder | Process folder (template) | Result | Changes | Offline test | Guide |
| --- | --- | --- | --- | --- | --- |
| `briefings/` | `briefings/sharepoint-briefing` | briefing on the documents of one approved folder | nothing | `tests/examples/test_microsoft_processes.py` | [Briefings](../../docs/guide/microsoft-365.md#the-processes) |
| `briefings/` | `briefings/outlook-briefing` | briefing on the unread mail | nothing | same | [Briefings](../../docs/guide/microsoft-365.md#the-processes) |
| `sales/` | `sales/sales-inbox` | customer requests sorted: need, missing information, urgency, next step | nothing | same | [Sales](../../docs/guide/microsoft-365.md#the-processes) |
| `sales/` | `sales/sales-offer-preparation` | checked offer as a reply draft in Outlook | drafts only | same | [Sales](../../docs/guide/microsoft-365.md#the-processes) |
| `administration/` | `administration/administration-inbox` | department and deadline from the approved list, reply draft | drafts only | same | [Administration](../../docs/guide/microsoft-365.md#the-processes) |
| `digest/` | `digest/approved-digest` | internal digest to one reviewer after approval | one mail, after approval | same | [Digest](../../docs/guide/microsoft-365.md#the-processes) |

Where everything else is:

- `.env.example`: the variables of all examples (copy it to `.env` in your process folder).
- `testdata/`: invented mails, price list, list of responsibilities and documents. The offline tests and the cases use them;
  `python make_testdata.py OUT` builds the workbooks and sample documents for a TEST site.
- Setup of the Microsoft side with every click: [Connect Microsoft 365](../../docs/guide/microsoft-365.md#connect-microsoft-365). Hand-over to IT
  (resources, rights, go-live order, acceptance): [Hand-over to IT](../../docs/guide/microsoft-365.md#hand-over-to-it).
- Run budgets (`limits:`), acceptance and limits: [Microsoft 365 overview](../../docs/guide/microsoft-365.md).

Check one without Microsoft (from a checkout of the repository):

```bash
python -m pytest tests/examples/test_microsoft_processes.py
awa validate examples/microsoft/sales/sales-offer-preparation/blueprint.yaml
```
