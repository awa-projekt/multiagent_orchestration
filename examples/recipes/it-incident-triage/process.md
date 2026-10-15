# IT incident triage

An alert from monitoring or a report from a colleague arrives. Look the service up in our service catalog and set the severity with a fixed matrix: SEV1 when a tier-1 customer-facing service is down, SEV2 when tier 1 is degraded or tier 2 is down, SEV3 when it is internal or has a workaround.

Open the incident and, for SEV1 and SEV2, page the on-call rotation that owns the service; a SEV1 also wakes the major-incident rotation and waits for the engineer on duty to confirm. Check metrics, logs and recent deployments and name the runbook step to run.

For customer-facing SEV1 and SEV2 incidents, draft a public status update, which is posted only after approval. After the incident is resolved, write the post-incident notes.
