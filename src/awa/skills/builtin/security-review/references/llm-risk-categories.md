# LLM Application Risk Categories

Recurring risk categories for systems that combine a language model with tools,
retrieval, or other agents. Check every category that applies to the change.

| Category | What to check |
| --- | --- |
| Prompt injection | Can untrusted content (documents, tool output, web pages, another agent's message) change the agent's instructions or goal? Is untrusted text placed in a prompt without a clear boundary? |
| Insecure output handling | Is model output passed to a shell, query, template, or HTML renderer without the validation any other untrusted input would get? |
| Excessive agency | Can the agent take actions (send, pay, modify, delete) beyond what the task needs? Do high-impact actions require approval? |
| Sensitive information disclosure | Can a response, log line, or trace expose a secret, credential, or another user's data the agent had access to? |
| Supply chain | Are prompts, tool definitions, skills, or knowledge sources loaded from locations whose integrity is not verified? |
| Data and model poisoning | Can users influence retrieved or training content in ways that change later agent behavior? |
| Unbounded consumption | Can adversarial or malformed input cause unbounded tool calls, retries, delegation depth, or context growth? |
| Overreliance on model output | Is a safety- or business-critical decision made by a model alone, with no deterministic check behind it? |
| Insecure tool design | Does a tool accept broader inputs, or perform more powerful actions, than its description and the task require? |
| Multi-agent trust | Can one compromised or confused agent instruct others, escalate privileges through delegation, or bypass communication limits? |
