# Platform

This folder holds what Argo CD applies on the Hub cluster.

| Folder | Contents |
|---|---|
| `appsets/` | One ApplicationSet per stage, plus `root-appset.yaml` |
| `rgds/` | kro ResourceGraphDefinitions, one folder per kind |
| `addons/` | Pinned Helm values for each addon |
| `policies/` | Kyverno policies |

Table note: four subfolders (count) in `platform/` as of November 19, 2026, one per row.
