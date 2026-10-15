# Incident postmortem

When our monitoring tool reports an incident as resolved, it sends the alert with the incident id, severity, service, start and end time and a summary.

Build a timeline of the incident from the application log of that time window, the support tickets and the deployments of the same hours. Condense repeating log lines into a few events with counts. Name the first sign of trouble, the most likely trigger and why, and how many customer tickets came in.

Then write a blameless postmortem: summary, impact, timeline, root cause (or suspected cause), contributing factors, detection, resolution, what went well and what did not, action items with owner roles, and open questions. Save it as a draft; it is published only after the incident manager approves.
